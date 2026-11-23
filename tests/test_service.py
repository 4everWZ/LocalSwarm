"""Exercise the real local HTTP and MCP surfaces with the mock runner."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

from engine import Engine
from runner import command, parse_result, run_with_launcher
from swarm import Service, make_handler, mock_runner, remote
from http.server import ThreadingHTTPServer


class ServiceTest(unittest.TestCase):
    def test_bridge_roles_replay_and_http(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            subprocess.run(["git", "init", "-q", name], check=True)
            subprocess.run(["git", "-C", name, "-c", "user.name=Test", "-c",
                            "user.email=test@example.invalid", "commit", "--allow-empty",
                            "-m", "initial"], check=True, capture_output=True)
            e = Engine(root / "board.db", root, mock_runner)
            app = Service(e, "owner-secret", "view-secret", operator="operator-secret")
            app.add_actor("worker-secret", "build")
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_port
            url = f"http://127.0.0.1:{port}"
            http = build_opener(ProxyHandler({}))
            try:
                pending = remote(port, "owner-secret", "/rpc", {"name": "swarm_spawn", "args": {
                    "name": "build", "role": "worker", "brief": "Build a local test fixture",
                    "allowed_paths": ["README.md"],
                }})
                self.assertEqual(pending["name"], "build")
                with self.assertRaisesRegex(RuntimeError, "coordinator"):
                    remote(port, "worker-secret", "/rpc", {"name": "swarm_merge",
                                                          "args": {"worker_name": "build"}})
                with self.assertRaisesRegex(RuntimeError, "operator capability"):
                    remote(port, "owner-secret", "/operator-merge", {})
                with self.assertRaisesRegex(RuntimeError, "unknown argument"):
                    remote(port, "worker-secret", "/rpc", {"name": "board_post", "args": {
                        "actor": "coordinator", "content": "spoof",
                    }})
                for _ in range(100):
                    e.tick()
                    if any(x["status"] == "waiting" for x in e.read()):
                        break
                    time.sleep(.005)
                self.assertEqual(e.members()[0]["session_id"], "mock-build")
                with self.assertRaises(HTTPError) as exc:
                    http.open(url + "/snapshot")
                self.assertEqual(exc.exception.code, 403)
                q = Request(url + "/snapshot", headers={"Cookie": "swarm_view=view-secret"})
                with http.open(q) as response:
                    snapshot = json.load(response)
                self.assertNotIn("session_id", snapshot["members"][0])
                self.assertNotIn("repo_path", snapshot["members"][0])
                since = snapshot["events"][-1]["seq"]
                remote(port, "owner-secret", "/rpc", {"name": "board_post", "args": {
                    "mentions": ["@build"], "content": "Wake up",
                }})
                e.tick()
                req = Request(url + f"/events?since={since}", headers={"Cookie": "swarm_view=view-secret"})
                with http.open(req, timeout=3) as stream:
                    self.assertEqual(stream.readline().strip(), f"id: {since + 1}".encode())
                    self.assertIn(b"Wake up", stream.readline())
                owner_file = root / "owner"
                owner_file.write_text("owner-secret")
                call = dict(jsonrpc="2.0", id=1, method="tools/list")
                cli = subprocess.run([sys.executable, "swarm.py", "--port", str(port), "mcp",
                                      "--owner-file", str(owner_file)], input=json.dumps(call) + "\n",
                                     text=True, capture_output=True, check=True)
                names = {tool["name"] for tool in json.loads(cli.stdout)["result"]["tools"]}
                self.assertTrue({"swarm_inbox", "swarm_policy", "swarm_spawn"} <= names)
            finally:
                app.stop.set()
                server.shutdown()
                server.server_close()
                thread.join()
                e.close()

    def test_provider_launch_flags_and_receipts(self):
        sid = "70bc01ab-d74d-4c87-8178-14fb0a01cd03"
        spec = {"actor":"reviewer", "role":"verifier", "backend":"codex", "repo_path":"/tmp/test",
                "brief":"Review the local docs", "events":[], "allowed_paths":[], "session_id":sid,
                "model": "gpt-6.1-terra", "reasoning_effort": "xhigh"}
        args, prompt = command(spec, 7420)
        self.assertIn("read-only", args)
        self.assertEqual(args[args.index("exec") + 1], "resume")
        self.assertEqual(args[-2:], [sid, "-"])
        self.assertIn('model_reasoning_effort="xhigh"', args)
        self.assertIn('mcp_servers.swarm.tools.swarm_report.approval_mode="approve"', args)
        stdout = "\n".join(json.dumps(x) for x in [
            {"type":"thread.started","thread_id":sid},
            {"type":"item.completed","item":{"type":"agent_message","text":"done"}},
            {"type":"turn.completed"},
        ])
        self.assertEqual(parse_result("codex", stdout, 0, sid)["content"], "done")
        self.assertEqual(parse_result("codex", stdout, 1, sid)["status"], "failed")
        spec = {**spec, "backend": "claude", "model": "default", "reasoning_effort": None}
        args, prompt = command(spec, 7420)
        self.assertIn("Read,Glob,Grep", args)
        self.assertNotIn("Bash", " ".join(args))
        self.assertEqual(args[-2:], ["--resume", sid])
        stdout = json.dumps({"type":"result","subtype":"success","session_id":sid,"result":"done"})
        self.assertEqual(parse_result("claude", stdout, 0, sid)["content"], "done")
        self.assertEqual(parse_result("claude", "{}", 0, sid)["status"], "failed")

    def test_messages_received_before_first_turn_reach_worker_prompt(self):
        req = {"actor": "builder", "role": "worker", "backend": "codex", "repo_path": "/tmp/test",
               "brief": "Update README", "allowed_paths": ["README.md"],
               "events": [{"status": "spawned", "content": "Redundant brief"},
                          {"seq": 2, "actor": "peer", "content": "Use marker cedar-429"}]}
        _, prompt = command(req, 7420)
        self.assertIn('"actor": "peer"', prompt)
        self.assertIn("cedar-429", prompt)
        self.assertNotIn("Redundant brief", prompt)

    @unittest.skipUnless(os.name == "nt", "native Windows provider shutdown")
    def test_windows_reaps_cli_that_stays_alive_after_completed_receipt(self):
        sid = "70bc01ab-d74d-4c87-8178-14fb0a01cd03"
        rows = [{"type": "thread.started", "thread_id": sid},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
                {"type": "turn.completed"}]
        transcript = "\n".join(json.dumps(row) for row in rows)
        with tempfile.TemporaryDirectory() as temp:
            launcher = Path(temp) / "launcher.py"
            launcher.write_text(f"import sys, time\nsys.stdin.read()\nprint({transcript!r}, flush=True)\ntime.sleep(60)\n",
                                encoding="utf-8")
            app = Service(None, "owner", "browser")
            req = {"actor": "builder", "role": "worker", "backend": "codex", "repo_path": temp,
                   "brief": "Test shutdown", "allowed_paths": []}
            result = run_with_launcher(req, app, 7420, launcher, timeout=10)
            self.assertEqual(result, {"session_id": sid, "status": "waiting", "content": "done"})
            self.assertFalse(app.actors)


if __name__ == "__main__":
    unittest.main()
