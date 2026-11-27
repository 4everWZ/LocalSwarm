"""User-level Windows MCP entrypoint with one LocalSwarm service per Git checkout."""

from contextlib import closing, contextmanager
import hashlib
from http.client import HTTPConnection, HTTPException
import json
import msvcrt
import os
from pathlib import Path
import re
import subprocess
import sys
from threading import Event, Thread
import time
from uuid import uuid4


STATE_DIR = Path(__file__).resolve().parent
RUNTIME = STATE_DIR / "runtime"
PROJECTS = STATE_DIR / "projects"
TASK = "LocalSwarm-Dynamic-Host"
SETTINGS = json.loads((STATE_DIR / "global-settings.json").read_text(encoding="utf-8"))
PYTHON = Path(sys.executable).with_name("python.exe")
GIT_ENV = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
START_TIMEOUT = 17
LEASE_INTERVAL = 20
sys.path.insert(0, str(RUNTIME))
from swarm import TOOLS, validate


class ServiceDraining(RuntimeError):
    """The service guarantees it rejected the request before dispatch."""


class ServiceError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def post(port, token, route, data, timeout=30):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    with closing(HTTPConnection("127.0.0.1", port, timeout=timeout)) as connection:
        connection.request("POST", route, json.dumps(data).encode("utf-8"), headers)
        with connection.getresponse() as response:
            status = response.status
            payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("Invalid LocalSwarm response.")
    if status == 503 and payload.get("not_executed") is True:
        raise ServiceDraining("LocalSwarm is going to sleep before dispatch.")
    if status != 200:
        raise ServiceError(status, payload.get("error", "LocalSwarm request failed."))
    return payload


def write_json(path, value):
    pending = path.with_suffix(".pending")
    pending.write_text(json.dumps(value), encoding="utf-8")
    pending.replace(path)


def git_checkout(cwd):
    result = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"], env=GIT_ENV,
                            capture_output=True, encoding="utf-8", timeout=5,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise ValueError("Open a local Git checkout in Codex before calling LocalSwarm.")
    return Path(result.stdout.strip()).resolve(strict=True)


def project_state(repo):
    key = os.path.normcase(str(repo))
    if key == os.path.normcase(SETTINGS["previous_checkout"]):
        return STATE_DIR / "LocalSwarm"
    return PROJECTS / hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def healthy(port, owner):
    try:
        policy = post(port, owner, "/rpc", {"name": "swarm_policy", "args": {}}, timeout=1)["result"]
    except (OSError, ValueError, KeyError, HTTPException, ServiceError):
        return False
    if policy.get("ceiling") is not None or "codex" not in policy.get("backends", []):
        raise RuntimeError("The saved LocalSwarm service is not in dynamic Codex mode.")
    return True


def connect(repo, state):
    endpoint, key = state / "endpoint.json", state / "coordinator.key"
    if not endpoint.is_file() or not key.is_file():
        return None
    saved = json.loads(endpoint.read_text(encoding="utf-8"))
    if os.path.normcase(saved["repo"]) != os.path.normcase(str(repo)):
        raise RuntimeError("LocalSwarm state belongs to a different Git checkout.")
    owner = key.read_text(encoding="utf-8").strip()
    port = saved["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Invalid saved LocalSwarm port.")
    return (port, owner) if healthy(port, owner) else None


def start_service(repo, state):
    from catalog import is_managed_codex
    configured = SETTINGS.get("codex_executable")
    # Desktop installs change their versioned path on update. Only a user-selected
    # executable outside that managed directory pins the service to a file.
    pinned = ["--codex-executable", configured] if configured and not is_managed_codex(configured) else []
    log_path = state / "service.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [str(PYTHON), str(RUNTIME / "swarm.py"), "--port", "0", "serve", "--repo", str(repo),
             "--db", str(state / "board.db"), "--owner-file", str(state / "coordinator.key"),
             "--idle-seconds", "1800", *pinned,
             "--codex-launcher", str(RUNTIME / "examples/codex/windows_launcher.py")],
            cwd=repo, env=GIT_ENV, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and process.poll() is None:
            log = log_path.read_text(encoding="utf-8", errors="replace")
            port = re.search(r"^Private board: http://127\.0\.0\.1:(\d+)/open/", log, re.MULTILINE)
            if port:
                write_json(state / "endpoint.json", {
                    "port": int(port[1]), "pid": process.pid, "repo": str(repo), "status": "running",
                })
                return process
            time.sleep(.1)
        raise RuntimeError(f"LocalSwarm did not start. See {log_path}.")
    except BaseException:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        raise


def supervise():
    services = {}
    while True:
        for state, proc in tuple(services.items()):
            exit_code = proc.poll()
            if exit_code is None:
                continue
            del services[state]
            try:
                endpoint = state / "endpoint.json"
                saved = json.loads(endpoint.read_text(encoding="utf-8"))
                if saved["pid"] == proc.pid:
                    saved["status"] = "asleep" if exit_code == 0 else "offline"
                    write_json(endpoint, saved)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        requests = list(PROJECTS.glob("*/start.json"))
        previous = STATE_DIR / "LocalSwarm" / "start.json"
        if previous.is_file():
            requests.append(previous)
        for request in requests:
            state = request.parent
            try:
                repo = Path(json.loads(request.read_text(encoding="utf-8"))["repo"]).resolve(strict=True)
                if project_state(repo).resolve() != state.resolve() or git_checkout(repo) != repo:
                    raise ValueError("LocalSwarm startup request does not match a Git checkout.")
                if connect(repo, state) is not None:
                    request.unlink(missing_ok=True)
                    continue
                if state not in services:
                    services[state] = start_service(repo, state)
                    request.unlink(missing_ok=True)
            except ServiceDraining:
                # Keep the wake request queued until this generation releases the database lock.
                continue
            except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
                write_json(state / "startup-error.json", {"message": str(exc)})
                request.unlink(missing_ok=True)
        time.sleep(.25)


@contextmanager
def startup_lock(state):
    deadline = time.monotonic() + START_TIMEOUT + 3
    with (state / "startup.lock").open("a+b") as lock:
        lock.seek(0)
        while True:
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Timed out waiting for another LocalSwarm startup.") from None
                time.sleep(.15)
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def ready_service(repo, state):
    try:
        return connect(repo, state)
    except ServiceDraining:
        return None


def ensure_service(repo):
    state = project_state(repo)
    state.mkdir(parents=True, exist_ok=True)
    with startup_lock(state):
        saved = ready_service(repo, state)
        if saved is not None:
            return saved
        error = state / "startup-error.json"
        error.unlink(missing_ok=True)
        write_json(state / "start.json", {"repo": str(repo)})
        scheduler = Path(os.environ["SystemRoot"]) / "System32" / "schtasks.exe"
        subprocess.run([scheduler, "/Run", "/TN", TASK], capture_output=True, timeout=5,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if error.exists():
                raise RuntimeError(json.loads(error.read_text(encoding="utf-8"))["message"])
            saved = ready_service(repo, state)
            if saved is not None:
                return saved
            time.sleep(.15)
        raise RuntimeError(f"Timed out starting the LocalSwarm service for {repo}.")


class ChatLease:
    def __init__(self):
        self.id = uuid4().hex
        self.target = None
        self.stop = Event()
        self.thread = None

    def send(self, target, *, release=False):
        port, owner = target
        data = {"id": self.id, "release": True} if release else {"id": self.id}
        post(port, owner, "/lease", data, timeout=2)

    def hold(self, target):
        try:
            self.send(target)
        except ServiceError as exc:
            if exc.status != 404 and (exc.status != 403 or str(exc) != "unexpected origin or route"):
                raise
            # The old service rejected the unknown route and has no idle shutdown.
            return
        self.target = target
        if self.thread is None:
            self.thread = Thread(target=self.renew, daemon=True, name="localswarm-lease")
            self.thread.start()

    def renew(self):
        while not self.stop.wait(LEASE_INTERVAL):
            try:
                self.send(self.target)
            except (OSError, ValueError, RuntimeError, HTTPException):
                # Only an actual tool call can start the replacement service.
                continue

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join()
            try:
                self.send(self.target, release=True)
            except (OSError, ValueError, RuntimeError, HTTPException):
                pass  # The service expires the lease if the bridge vanished.


def call_tool(repo, name, arguments, lease):
    for attempt in range(2):
        target = ensure_service(repo)
        try:
            lease.hold(target)
        except (OSError, HTTPException, ServiceDraining):
            if attempt:
                raise
            continue  # Only the lease request was attempted.
        try:
            return post(*target, "/rpc", {"name": name, "args": arguments})["result"]
        except ServiceDraining:
            if attempt:
                raise
            # An explicit rejection before dispatch makes this retry safe.
    raise RuntimeError("LocalSwarm could not accept the request.")


def stdio():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    lease = ChatLease()
    try:
        bridge(Path.cwd(), lease)
    finally:
        lease.close()


def bridge(cwd, lease):
    for line in sys.stdin:
        req = json.loads(line)
        if "id" not in req:
            continue
        method = req["method"]
        envelope = {"jsonrpc": "2.0", "id": req["id"]}
        if method == "initialize":
            version = req["params"].get("protocolVersion")
            supported = {"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"}
            result = {"protocolVersion": version if version in supported else "2025-03-26",
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "localswarm-windows", "version": "1"},
                      "instructions": "LocalSwarm operates on the Git checkout of this Codex chat. Use the active "
                                      "chat's model and reasoning effort in the coordinator field of Codex submissions."}
        elif method == "tools/list":
            result = {"tools": [{"name": name, "description": description, "inputSchema": schema}
                                for name, (description, schema) in TOOLS.items() if name != "swarm_report"]}
        elif method == "ping":
            result = {}
        elif method == "tools/call":
            try:
                name = req["params"]["name"]
                if name not in TOOLS or name == "swarm_report":
                    raise ValueError("Unknown coordinator tool.")
                arguments = req["params"].get("arguments", {})
                validate(arguments, TOOLS[name][1])
                repo = git_checkout(cwd)
                reply = call_tool(repo, name, arguments, lease)
                if name == "swarm_policy":
                    reply["checkout"] = str(repo)
                result = {"content": [{"type": "text", "text": json.dumps(reply, ensure_ascii=False)}]}
            except (OSError, ValueError, RuntimeError, KeyError, HTTPException, subprocess.SubprocessError) as exc:
                result = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
        else:
            print(json.dumps({**envelope, "error": {"code": -32601, "message": "Method not found"}}), flush=True)
            continue
        print(json.dumps({**envelope, "result": result}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--supervise"]:
        supervise()
    else:
        stdio()
