"""Read the current worker account's Codex model capabilities."""
import json
import os
from pathlib import Path
from queue import Empty, Queue
import shutil
import subprocess
import threading
import time


def desktop_codex_root():
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) / "OpenAI" / "Codex" / "bin" if local else None


def is_managed_codex(executable, root=None):
    root = desktop_codex_root() if root is None else root
    path = Path(executable).expanduser()
    return root is not None and path.name.lower() == "codex.exe" and path.parent.parent == root


def newest_desktop_codex(root):
    if root is None or not root.is_dir():
        return None
    root = root.resolve()
    candidates = []
    for directory in root.iterdir():
        binary = directory / "codex.exe"
        try:
            if binary.is_file() and binary.resolve() == binary and os.access(binary, os.X_OK):
                candidates.append((binary.stat().st_mtime_ns, directory.name, binary))
        except OSError:
            continue  # An update can remove a version while it is being inspected.
    return max(candidates)[2] if candidates else None


def resolve_codex(executable=None):
    """Resolve a trusted native CLI without running Windows shell wrappers."""
    chosen = executable
    if os.name == "nt":
        configured = os.environ.get("LOCALSWARM_CODEX_EXE")
        if not chosen and configured and not is_managed_codex(configured):
            chosen = configured
        if not chosen:
            chosen = newest_desktop_codex(desktop_codex_root())
        if not chosen:
            directories = (Path(part.strip('"')) for part in os.get_exec_path())
            chosen = next((directory / "codex.exe" for directory in directories
                           if directory.is_absolute() and (directory / "codex.exe").is_file()), None)
    else:
        chosen = chosen or shutil.which("codex")
    if not chosen:
        raise ValueError("Codex executable not found. Set --codex-executable to the worker CLI path.")
    path = Path(chosen).expanduser().resolve()
    if not path.is_file() or not os.access(path, os.X_OK) or (os.name == "nt" and path.suffix.lower() != ".exe"):
        raise ValueError("--codex-executable must point to an executable Codex CLI (.exe on Windows).")
    return path


def _model_capabilities(executable, timeout):
    replies = Queue()

    def read_stdout():
        try:
            for line in process.stdout:
                replies.put(line)
        except (OSError, UnicodeError):
            pass
        finally:
            replies.put(None)

    def send(method, params, request_id=None):
        request = {"method": method, "params": params}
        if request_id is not None:
            request["id"] = request_id
        line = json.dumps(request) + "\n"
        if len(line) > 2048:
            raise ValueError("Codex App Server returned an oversized model catalog cursor.")
        process.stdin.write(line)
        process.stdin.flush()

    def receive(request_id, method):
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                line = replies.get(timeout=remaining)
            except Empty:
                break
            if line is None:
                raise ValueError(f"Codex App Server closed before completing {method}. Check the worker CLI and login.")
            try:
                reply = json.loads(line)
            except ValueError:
                raise ValueError("Codex App Server returned invalid JSON.") from None
            if not isinstance(reply, dict) or reply.get("id") != request_id:
                continue
            if "error" in reply:
                raise ValueError(f"Codex App Server rejected {method}. Check the worker login and model provider.")
            if not isinstance(reply.get("result"), dict):
                raise ValueError(f"Codex App Server returned an invalid {method} result.")
            return reply["result"]
        raise ValueError(f"Timed out while reading Codex App Server {method}.")

    try:
        process = subprocess.Popen([str(executable), "app-server", "--listen", "stdio://"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, encoding="utf-8",
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except OSError:
        raise ValueError("Could not start the Codex App Server. Check the worker CLI executable.") from None

    reader = threading.Thread(target=read_stdout, name="codex-model-catalog", daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    try:
        send("initialize", {"clientInfo": {"name": "localswarm", "version": "1"}}, 1)
        receive(1, "initialize")
        send("initialized", {})

        request_id = 2
        params = {"modelProvider": "openai", "includeHidden": False, "limit": 100}
        models, cursors = {}, set()
        while True:
            send("model/list", params, request_id)
            page = receive(request_id, "model/list")
            if not isinstance(page.get("data"), list):
                raise ValueError("Codex App Server returned an invalid model catalog page.")
            for entry in page["data"]:
                if not isinstance(entry, dict) or not isinstance(entry.get("model"), str):
                    raise ValueError("Codex App Server returned a model without a canonical ID.")
                efforts = entry.get("supportedReasoningEfforts")
                if not isinstance(efforts, list):
                    continue
                supported = frozenset(choice["reasoningEffort"] for choice in efforts if isinstance(choice, dict)
                                      and isinstance(choice.get("reasoningEffort"), str))
                if supported and not entry.get("hidden"):
                    models.setdefault(entry["model"], supported)
            cursor = page.get("nextCursor")
            if cursor is None:
                return models
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise ValueError("Codex App Server returned an invalid model catalog cursor.")
            cursors.add(cursor)
            params["cursor"] = cursor
            request_id += 1
    except OSError:
        raise ValueError("Could not communicate with the Codex App Server. Check the worker CLI and login.") from None
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=.5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        reader.join(timeout=1)
        if not reader.is_alive():
            process.stdout.close()


class CodexCatalog:
    def __init__(self, executable, *, ttl=60, timeout=15, resolver=None):
        self.executable = executable
        self.resolver = resolver
        self.ttl, self.timeout = ttl, timeout
        self._lock = threading.Lock()
        self._cached = None
        self._expires = 0
        self._checked_missing = set()
        self._cached_executable = None

    def models(self, required=(), *, executable=None):
        required = set(required)
        with self._lock:
            binary = executable or (self.resolver() if self.resolver else self.executable)
            stale = (self._cached is None or binary != self._cached_executable
                     or time.monotonic() >= self._expires)
            if stale or required.difference(self._cached, self._checked_missing):
                self._cached = _model_capabilities(binary, self.timeout)
                self._cached_executable = binary
                if stale:
                    self._expires = time.monotonic() + self.ttl
                    self._checked_missing.clear()
                self._checked_missing.update(required - self._cached.keys())
                self._checked_missing.difference_update(self._cached)
            return dict(self._cached)
