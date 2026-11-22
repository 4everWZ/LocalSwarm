import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer

from engine import Engine
from orchestrator import Orchestrator
from swarm import Service, make_handler, remote


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args],
                                   text=True, stderr=subprocess.STDOUT).strip()


class PlanServiceTest(unittest.TestCase):
    def test_full_mcp_plan_checkpoint_verify_and_integrate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init", "-q")
            git(repo, "-c", "user.name=Test", "-c", "user.email=t@example.invalid",
                "commit", "--allow-empty", "-m", "initial")

            def run(req):
                if req["role"] == "verifier":
                    revision = req["base_revision"]
                    verdict = "pass" if (Path(req["repo_path"]) / "example.txt").read_text() == "ok" else "rework"
                    evidence = "Checked example.txt at the pinned revision."
                else:
                    (Path(req["repo_path"]) / "example.txt").write_text("ok")
                    revision, verdict, evidence = None, "ready", "Added example.txt."
                tok = req["actor"] + "-fixture"
                app.add_actor(tok, req["actor"])
                try:
                    args = dict(plan_id="p", task_id="example", verdict=verdict, evidence=evidence)
                    if revision:
                        args.update(revision=revision)
                    remote(server.server_port, tok, "/rpc", {"name": "swarm_report", "args": args})
                finally:
                    app.remove_actor(tok)
                return {"session_id": req["actor"], "status": "completed"}

            engine = Engine(root / "board.db", repo, run)
            plan = Orchestrator(root / "plan.db", engine)
            app = Service(engine, "coordinator", "view", operator="operator", orchestrator=plan)
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
            http = threading.Thread(target=server.serve_forever, daemon=True)
            scheduler = threading.Thread(target=app.schedule, daemon=True)
            http.start()
            scheduler.start()
            try:
                def tool(name, args):
                    return remote(server.server_port, "coordinator", "/rpc", {"name": name, "args": args})
                tool("swarm_plan", {"plan_id": "p", "goal": "Verify an example",
                     "tasks": [{"id": "example", "dependencies": [], "role": "worker", "backend": "mock",
                                "model": "fixture", "criterion": "example.txt says ok",
                                "allowed_paths": ["example.txt"]}]})
                for _ in range(170):
                    summary = tool("swarm_status", {"plan_id": "p"})
                    if summary["status"] in ("passed", "blocked"):
                        break
                    time.sleep(.02)
                self.assertEqual(summary["status"], "passed", summary)
                task = summary["tasks"][0]
                self.assertFalse(task["integrated"])
                preview = tool("swarm_merge", {"worker_name": task["worker"]})
                self.assertEqual(preview["paths"], ["example.txt"])
                self.assertEqual(preview["revision"], task["revision"])
                remote(server.server_port, "operator", "/operator-merge",
                       {"worker": task["worker"], "revision": task["revision"],
                        "base_revision": preview["base_revision"]})
                self.assertTrue(tool("swarm_status", {"plan_id": "p"})["tasks"][0]["integrated"])
                self.assertEqual((repo / "example.txt").read_text(), "ok")
            finally:
                app.stop.set()
                scheduler.join()
                engine.close()
                plan.close()
                server.shutdown()
                server.server_close()
                http.join()
