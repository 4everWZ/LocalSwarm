"""Local Swarm: stdio MCP bridge, durable scheduler, read-only web board."""
import argparse
import getpass
import http.cookies
import json
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


def encode(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode()


def schema(props, required=()):
    return dict(type="object", properties=props, required=list(required),
                additionalProperties=False)


STR = {"type": "string", "maxLength": 12000}
STRS = {"type": "array", "maxItems": 50, "items": STR}
TASK = schema({"id": STR, "dependencies": STRS, "role": STR,
               "backend": {"enum": ["mock", "codex", "claude"]}, "model": STR,
               "allowed_paths": STRS, "criterion": STR,
               "max_attempts": {"type": "integer", "minimum": 1, "maximum": 5},
               "verifier_backend": {"enum": ["mock", "codex", "claude"]}, "verifier_model": STR},
              ("id", "dependencies", "role", "backend", "model", "allowed_paths", "criterion"))
TOOLS = {
    "swarm_plan": ("Create a dependency plan for the authorized local work.",
                   schema({"plan_id": STR, "goal": STR,
                           "tasks": {"type": "array", "maxItems": 64, "items": TASK}},
                          ("plan_id", "goal", "tasks"))),
    "swarm_status": ("Read one plan and its task evidence, or all plans.",
                     schema({"plan_id": STR})),
    "swarm_report": ("Submit authenticated evidence. Ready without a SHA checkpoints scoped changes; pass/rework requires the exact review SHA.",
                     schema({"plan_id": STR, "task_id": STR, "revision": STR,
                             "verdict": {"enum": ["ready", "pass", "rework"]}, "evidence": STR},
                            ("plan_id", "task_id", "verdict", "evidence"))),
    "swarm_spawn": ("Start an approved local development task.",
                    schema({"name": STR, "role": {"enum": ["worker", "verifier"]},
                            "brief": STR, "allowed_paths": STRS,
                            "backend": {"enum": ["mock", "codex", "claude"]}, "model": STR,
                            "review_worker": STR, "review_revision": STR},
                           ("name", "role", "brief", "allowed_paths"))),
    "board_post": ("Post as the authenticated actor. Mention other members by name.",
                   schema({"mentions": STRS, "content": STR, "status": {"enum": ["waiting", "done"]}},
                          ("content",))),
    "board_read": ("Replay board events after sequence number; newest events are last.",
                   schema({"since_seq": {"type": "integer", "minimum": 0}})),
    "swarm_merge": ("Preview integration. An operator must separately approve the exact revision.",
                    schema({"worker_name": STR}, ("worker_name",))),
}


def validate(data, spec):
    if not isinstance(data, dict):
        raise ValueError("expected an object")
    if not (set(spec["required"]) <= data.keys() <= spec["properties"].keys()):
        raise ValueError("missing or unknown argument")
    for k, v in data.items():
        s = spec["properties"][k]
        if "enum" in s and v not in s["enum"]:
            raise ValueError(f"invalid {k}")
        typ = s.get("type")
        if typ == "string" and (not isinstance(v, str) or len(v) > s.get("maxLength", 12000)):
            raise ValueError(f"invalid {k}")
        if typ == "array":
            if not isinstance(v, list) or len(v) > s.get("maxItems", 64):
                raise ValueError(f"invalid {k}")
            for item in v:
                specitem = s["items"]
                if specitem.get("type") == "object":
                    validate(item, specitem)
                elif not isinstance(item, str) or len(item) > 300:
                    raise ValueError(f"invalid {k}")
        if typ == "integer" and (type(v) is not int or not s.get("minimum", 0) <= v <= s.get("maximum", 2**63 - 1)):
            raise ValueError(f"invalid {k}")



class Service:
    def __init__(self, engine, owner, browser, backends=("mock",), operator=None, orchestrator=None):
        self.engine, self.orchestrator = engine, orchestrator
        self.owner, self.browser, self.operator = owner, browser, operator
        self.backends = frozenset(backends)
        self.actors = {}
        self.lock = threading.RLock()
        self.actor_lock = threading.Lock()
        self.stop = threading.Event()

    def add_actor(self, token, name):
        with self.actor_lock:
            self.actors[token] = name

    def remove_actor(self, token):
        with self.actor_lock:
            self.actors.pop(token, None)

    def actor(self, token):
        if token and secrets.compare_digest(token, self.owner):
            return "coordinator"
        with self.actor_lock:
            return self.actors.get(token)

    def tool(self, actor, name, args):
        if name not in TOOLS:
            raise ValueError("unknown tool")
        validate(args, TOOLS[name][1])
        if name in ("swarm_spawn", "swarm_merge", "swarm_plan") and actor != "coordinator":
            raise PermissionError("coordinator action only")
        with self.lock:
            if name in ("swarm_plan", "swarm_status", "swarm_report") and self.orchestrator is None:
                raise ValueError("plan service unavailable")
            if name == "swarm_plan":
                for task in args["tasks"]:
                    if not {task["backend"], task.get("verifier_backend", task["backend"])} <= self.backends:
                        raise ValueError("backend not enabled by the operator")
                result = self.orchestrator.create(args["plan_id"], args["tasks"], args["goal"])
                self.engine.post("coordinator", [], f"Plan {args['plan_id']} submitted", "plan")
                return result
            if name == "swarm_status":
                return (self.orchestrator.summary(args["plan_id"]) if args.get("plan_id")
                        else self.orchestrator.plans())
            if name == "swarm_report":
                revision = args.get("revision")
                if not revision and args["verdict"] == "ready":
                    revision = self.orchestrator.checkpoint(args["plan_id"], args["task_id"], actor,
                                                           args["evidence"])
                result = self.orchestrator.report(args["plan_id"], args["task_id"], actor, revision,
                                                  args["verdict"], args["evidence"])
                if not result.get("duplicate"):
                    self.engine.post(actor, [], f"Plan {args['plan_id']}/{args['task_id']}: {args['verdict']}", "plan")
                return {**result, "revision": revision}
            if name == "swarm_spawn":
                if args["name"].startswith("plan-"):
                    raise ValueError("member names beginning with plan- are reserved")
                backend = args.get("backend", "mock")
                if backend not in self.backends:
                    raise ValueError("backend not enabled by the operator")
                return self.engine.spawn(args["name"], args["role"], args["brief"],
                                         args["allowed_paths"], backend, args.get("model", ""),
                                         review_worker=args.get("review_worker"),
                                         review_revision=args.get("review_revision"))
            if name == "board_post":
                if actor == "coordinator" and args.get("status") is not None:
                    raise ValueError("coordinator has no scheduling status")
                return self.engine.post(actor, [n.removeprefix("@") for n in args.get("mentions", [])], args["content"],
                                        args.get("status"))
            if name == "board_read":
                return self.engine.read(args.get("since_seq", 0))
            return self.engine.preview_merge(args["worker_name"])

    def schedule(self):
        while not self.stop.is_set():
            try:
                with self.lock:
                    if self.orchestrator is not None:
                        for row in self.orchestrator.tick():
                            self.engine.post("operator", [],
                                             f"{row['plan_id']}/{row['task_id']}: {row['status']}", "plan")
                    self.engine.tick()
            except Exception as e:
                print(f"scheduler stopped: {type(e).__name__}: {e}", file=sys.stderr)
                self.stop.set()
                break
            self.stop.wait(.1)


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Never log capabilities embedded in preview URLs.

        def response_headers(self, kind, length=None):
            self.send_header("Content-Type", kind)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                             "connect-src 'self'; img-src data:; base-uri 'none'; frame-ancestors 'none'")

        def send(self, code, obj, kind="application/json"):
            content = encode(obj) if kind == "application/json" else obj
            self.send_response(code)
            self.response_headers(kind, len(content))
            self.end_headers()
            self.wfile.write(content)

        def valid_origin(self):
            h = f"127.0.0.1:{self.server.server_port}"
            return (self.headers.get("Host") == h and
                    self.headers.get("Origin", f"http://{h}") == f"http://{h}")

        def view_access(self):
            c = http.cookies.SimpleCookie()
            try:
                c.load(self.headers.get("Cookie", ""))
                return "swarm_view" in c and secrets.compare_digest(
                    c["swarm_view"].value, app.browser)
            except (ValueError, http.cookies.CookieError):
                return False

        def do_GET(self):
            if not self.valid_origin():
                return self.send(403, {"error": "unexpected origin"})
            u = urlsplit(self.path)
            if u.path.startswith("/open/") and secrets.compare_digest(u.path[6:], app.browser):
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie", f"swarm_view={app.browser}; Path=/; HttpOnly; SameSite=Strict")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            if not self.view_access():
                return self.send(403, {"error": "open the private board URL printed in the terminal"})
            if u.path == "/":
                return self.send(200, Path(__file__).with_name("board.html").read_bytes(),
                                 "text/html; charset=utf-8")
            if u.path == "/snapshot":
                with app.lock:
                    safe = ("name", "role", "backend", "model", "active")
                    members = [{k: (bool(m[k]) if k == "active" else m[k])
                                for k in safe if k in m} for m in app.engine.members()]
                    data = dict(events=app.engine.read(0), members=members,
                                plans=app.orchestrator.plans() if app.orchestrator else [])
                return self.send(200, data)
            if u.path != "/events":
                return self.send(404, {"error": "not found"})
            try:
                params = parse_qs(u.query)
                since = int(self.headers.get("Last-Event-ID") or params.get("since", ["0"])[0])
                validate({"since_seq": since}, TOOLS["board_read"][1])
            except (ValueError, OverflowError):
                return self.send(400, {"error": "invalid cursor"})
            self.send_response(200)
            self.response_headers("text/event-stream; charset=utf-8")
            self.end_headers()
            self.connection.settimeout(5)
            try:
                while not app.stop.is_set():
                    with app.lock:
                        rows = app.engine.read(since)
                    if not rows:
                        self.wfile.write(b": heartbeat\n\n")
                    for row in rows:
                        since = row["seq"]
                        self.wfile.write(f"id: {since}\ndata: ".encode() + encode(row) + b"\n\n")
                    self.wfile.flush()
                    app.stop.wait(1)
            except OSError:
                return

        def do_POST(self):
            if not self.valid_origin() or self.path not in ("/rpc", "/operator-merge"):
                return self.send(403, {"error": "unexpected origin or route"})
            raw = self.headers.get("Authorization", "").removeprefix("Bearer ")
            actor = app.actor(raw)
            if self.path == "/operator-merge":
                if not app.operator or not secrets.compare_digest(raw, app.operator):
                    return self.send(403, {"error": "operator capability required"})
            elif not actor:
                return self.send(403, {"error": "invalid capability"})
            try:
                self.connection.settimeout(5)
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 100000:
                    raise ValueError("invalid body length")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("expected object")
                if self.path == "/operator-merge":
                    if set(data) != {"worker", "revision", "base_revision"} or not isinstance(data["worker"], str):
                        raise ValueError("expected worker and reviewed revision")
                    if any(not re.fullmatch("[a-f0-9]{40}|[a-f0-9]{64}", str(data[field]))
                           for field in ("revision", "base_revision")):
                        raise ValueError("exact reviewed commit hash required")
                    with app.lock:
                        if app.orchestrator:
                            app.orchestrator.require_merge_ready(data["worker"], data["revision"])
                        result = app.engine.merge(data["worker"], expected_revision=data["revision"],
                                              expected_base_revision=data["base_revision"])
                else:
                    result = app.tool(actor, data.get("name"), data.get("args", {}))
                return self.send(200, {"result": result})
            except PermissionError as e:
                return self.send(403, {"error": str(e)})
            except (ValueError, KeyError, RuntimeError, TypeError, OSError) as e:
                return self.send(400, {"error": str(e)[:500]})

    return Handler


def remote(port, token, path, data):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", encode(data),
                               {"Authorization": f"Bearer {token}",
                                "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(r, timeout=30) as resp:
            return json.load(resp)["result"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(json.load(e).get("error", "local service error")) from None


def bridge(port, token, coordinator):
    protocols = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
    for row in sys.stdin:
        req = None
        try:
            req = json.loads(row)
            if not isinstance(req, dict) or req.get("jsonrpc") != "2.0":
                raise ValueError("invalid JSON-RPC request")
            if "id" not in req:
                continue
            method, params = req["method"], req.get("params", {})
            if method == "initialize":
                p = params.get("protocolVersion")
                result = {"protocolVersion": p if p in protocols else protocols[0],
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "local-swarm", "version": "0.1.0"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                names = TOOLS if coordinator else ("board_post", "board_read", "swarm_status", "swarm_report")
                result = {"tools": [dict(name=n, description=TOOLS[n][0], inputSchema=TOOLS[n][1])
                                    for n in names]}
            elif method == "tools/call":
                try:
                    value = remote(port, token, "/rpc", {"name": params["name"],
                                                          "args": params.get("arguments", {})})
                    result = {"content": [{"type": "text", "text": encode(value).decode()}]}
                except (RuntimeError, OSError, KeyError, TypeError) as e:
                    result = {"isError": True, "content": [{"type": "text", "text": str(e)[:500]}]}
            else:
                print(encode(dict(jsonrpc="2.0", id=req["id"],
                                  error=dict(code=-32601, message="method not found"))).decode(), flush=True)
                continue
            print(encode(dict(jsonrpc="2.0", id=req["id"], result=result)).decode(), flush=True)
        except (ValueError, TypeError, KeyError, AttributeError):
            print(encode(dict(jsonrpc="2.0", id=req.get("id") if isinstance(req, dict) else None,
                              error=dict(code=-32600, message="invalid request"))).decode(), flush=True)


def mock_runner(req):
    event_list = req.get("events") or []
    if req.get("session_id"):
        return {"session_id": req["session_id"], "content": f"Mock resumed with {len(event_list)} new event(s).",
                "status": "waiting"}
    return {"session_id": f"mock-{req['actor']}", "content": "Mock initial turn exited; waiting for a mention.",
            "status": "waiting"}


def get_owner(file, create=False):
    p = Path(file).expanduser()
    if create and not p.exists():
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with os.fdopen(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            f.write(secrets.token_urlsafe(32) + "\n")
    return p.read_text().strip()


def main():
    p = argparse.ArgumentParser(prog="local-swarm")
    p.add_argument("--port", type=int, default=int(os.environ.get("SWARM_PORT", 7420)))
    s = p.add_subparsers(dest="mode", required=True)
    start = s.add_parser("serve")
    start.add_argument("--repo", required=True)
    start.add_argument("--db", required=True)
    start.add_argument("--owner-file", required=True)
    start.add_argument("--codex-launcher", type=Path)
    start.add_argument("--claude-launcher", type=Path)
    m = s.add_parser("mcp")
    m.add_argument("--owner-file")
    g = s.add_parser("merge")
    g.add_argument("worker")
    g.add_argument("--revision", required=True)
    g.add_argument("--base-revision", required=True)
    a = p.parse_args()
    if a.mode == "mcp":
        token = get_owner(a.owner_file) if a.owner_file else os.environ.get("SWARM_ACTOR_TOKEN", "")
        if not token:
            p.error("provide --owner-file or injected worker capability")
        bridge(a.port, token, bool(a.owner_file))
    elif a.mode == "merge":
        print(encode(remote(a.port, getpass.getpass("Operator code: "), "/operator-merge",
                            {"worker": a.worker, "revision": a.revision, "base_revision": a.base_revision})).decode())
    else:
        from engine import Engine
        from orchestrator import Orchestrator
        state = Path(a.db).expanduser().resolve()
        state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.umask(0o077)
        launchers = {name: launcher.resolve(strict=True) for name, launcher in
                     {"codex": a.codex_launcher, "claude": a.claude_launcher}.items() if launcher}
        for launcher in launchers.values():
            if not launcher.is_file() or not os.access(launcher, os.X_OK):
                p.error("launcher must be an operator-selected local executable")
        def run(req):
            if req["backend"] == "mock":
                return mock_runner(req)
            from runner import run_with_launcher
            return run_with_launcher(req, app, server.server_port, launchers[req["backend"]])
        eng = Engine(state, Path(a.repo).resolve(), run)
        app = Service(eng, get_owner(a.owner_file, True), secrets.token_urlsafe(32),
                      backends=("mock", *launchers), operator=secrets.token_urlsafe(24),
                      orchestrator=Orchestrator(state.with_suffix(state.suffix + ".plans"), eng))
        server = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(app))
        thread = threading.Thread(target=app.schedule, daemon=True)
        thread.start()
        print(f"Private board: http://127.0.0.1:{server.server_port}/open/{app.browser}", flush=True)
        print(f"Operator code (not in MCP): {app.operator}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            app.stop.set()
            server.server_close()
            thread.join()
            app.orchestrator.close()
            eng.close()


if __name__ == "__main__":
    main()
