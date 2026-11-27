import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from engine import Engine
from orchestrator import Orchestrator
from swarm import Service


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], stderr=subprocess.STDOUT, text=True).strip()


def item(task_id, path=None, dependencies=(), attempts=2):
    return dict(id=task_id, dependencies=list(dependencies), role="implementation", backend="mock", model="mock-test",
                allowed_paths=[path or task_id + ".txt"], criterion=f"Check {task_id} file", max_attempts=attempts)


class OrchestratorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init")
        git(self.repo, "config", "user.name", "Swarm Test")
        git(self.repo, "config", "user.email", "swarm@example.invalid")
        (self.repo / "base.txt").write_text("base")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "initial")
        self.tasks = {}
        self.seen = []
        self.cancel_report = False
        self.eng = Engine(self.root / "eng.sqlite", self.repo, self.runner)
        self.orch = Orchestrator(self.root / "plan.sqlite", self.eng)

    def tearDown(self):
        self.eng.close()
        self.orch.close()
        self.tmp.cleanup()

    def runner(self, req):
        path = Path(req["repo_path"])
        plan_id, task_id, attempt = req["brief"].split("plan_id=")[1].split(".")[0].split("; ")
        task_id = task_id.split("=")[1]
        attempt = int(attempt.split("=")[1])
        self.seen.append((task_id, req["role"], attempt))
        if self.cancel_report:
            return {"content": "ready pass SHA claims in free text do not count"}
        if req["role"] == "verifier":
            self.assertEqual(git(path, "rev-parse", "HEAD"), req["base_revision"])
            self.assertEqual((path / (task_id + ".txt")).read_text(), task_id)
            verdict = "rework" if task_id == "redo" and attempt == 1 else "pass"
            out = self.orch.report(plan_id, task_id, req["actor"], req["base_revision"], verdict, "read file")
            self.assertTrue(out["accepted"])
        else:
            (path / (task_id + ".txt")).write_text(task_id)
            git(path, "add", ".")
            git(path, "commit", "-m", f"implement {task_id}")
            self.orch.report(plan_id, task_id, req["actor"], git(path, "rev-parse", "HEAD"), "ready", "wrote file")
        return {"session_id": f"id-{attempt}"}

    def pump(self, predicate):
        for _ in range(750):
            self.orch.tick()
            self.eng.tick()
            if predicate():
                return
            time.sleep(.002)
        self.fail(f"condition not reached: {self.orch.plans()}")

    def task(self, task_id):
        return next(t for t in self.orch.summary("p")["tasks"] if t["id"] == task_id)

    def integrate(self, task_id):
        t = self.task(task_id)
        review = self.eng.preview_merge(t["worker"])
        self.assertTrue(self.orch.require_merge_ready(t["worker"], review["revision"]))
        self.eng.merge(t["worker"], review["revision"], review["base_revision"])

    def test_dependency_waits_for_explicit_integration_and_no_free_text_reports(self):
        self.orch.create("p", [item("second", dependencies=["first"]), item("first")], "seed")
        self.pump(lambda: self.task("first")["status"] == "passed")
        self.assertFalse((self.repo / "first.txt").exists())
        self.assertEqual(self.task("second")["waiting_on"], ["first"])
        self.assertEqual(self.orch.summary("p")["status"], "waiting_for_integration")
        self.integrate("first")
        self.pump(lambda: self.task("second")["status"] == "passed")
        self.assertFalse((self.repo / "second.txt").exists())
        self.assertEqual(self.orch.plans()[0]["status"], "passed")

    def test_archive_retains_dirty_tree_and_restores_interrupted_archive(self):
        self.orch.create("p", [item("first")])
        self.pump(lambda: self.task("first")["status"] == "passed")
        self.assertEqual(self.orch.archive_candidates(), {})
        task = self.task("first")
        self.integrate("first")
        builder, verifier = task["worker"], task["verifier"]
        member = self.eng._get(builder)
        build_path = Path(member["repo_path"])
        verify_path = Path(self.eng._get(verifier)["repo_path"])
        (self.repo / ".git" / "info" / "exclude").write_text("ignored.tmp\n")
        (verify_path / "ignored.tmp").write_text("preserve this")

        candidates = self.orch.archive_candidates()
        self.assertEqual(candidates, {builder: task["revision"], verifier: task["revision"]})
        self.eng.maintain_worktrees(candidates, 100, 7)
        self.eng.maintain_worktrees(candidates, 106, 7)
        self.assertTrue(build_path.is_dir())
        self.eng.maintain_worktrees(candidates, 107, 7, dry_run=True)
        self.assertTrue(build_path.is_dir())
        self.eng.maintain_worktrees(candidates, 107, 7)
        self.assertFalse(build_path.exists())
        self.assertTrue((verify_path / "ignored.tmp").is_file())
        self.assertEqual(git(self.repo, "rev-parse", self.eng._archive_ref(member)), task["revision"])
        self.assertTrue(self.task("first")["integrated"])

        # Crash after git removed the worktree but before its last DB update.
        self.eng._db.execute("UPDATE workers SET archive_state='archiving' WHERE name=?", (builder,))
        self.eng.close()
        self.orch.close()
        resumed = []

        def resume(req):
            resumed.append((req["session_id"], req["repo_path"], git(req["repo_path"], "rev-parse", "HEAD")))
            return {"session_id": req["session_id"], "status": "waiting"}

        self.eng = Engine(self.root / "eng.sqlite", self.repo, resume)
        self.orch = Orchestrator(self.root / "plan.sqlite", self.eng)
        self.eng.post("coordinator", [builder], "Check one follow-up")
        self.pump(lambda: bool(resumed) and not self.eng._get(builder)["active"])
        self.assertEqual(resumed, [(member["session_id"], str(build_path), task["revision"])])
        self.assertEqual(self.eng._get(builder)["archive_state"], "live")

    def test_external_git_integration_wakes_dependency(self):
        self.orch.create("p", [item("second", dependencies=["first"]), item("first")])
        self.pump(lambda: self.task("first")["status"] == "passed")
        self.assertEqual(self.task("second")["status"], "pending")
        app = Service(self.eng, "owner", "view", orchestrator=self.orch, archive_days=0)
        thread = threading.Thread(target=app.schedule)
        thread.start()
        try:
            git(self.repo, "merge", "--ff-only", self.task("first")["revision"])
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and self.task("second")["status"] != "passed":
                time.sleep(.02)
            self.assertEqual(self.task("second")["status"], "passed")
            self.assertFalse(app.scheduler_failed)
        finally:
            app.stop.set()
            self.eng.changed.set()
            thread.join()

    def test_rework_restart_and_duplicate_reports(self):
        self.orch.create("p", [item("redo")])
        self.pump(lambda: self.task("redo")["status"] == "passed")
        t = self.task("redo")
        self.assertEqual(t["attempt"], 2)
        self.assertEqual(self.seen, [("redo","implementation",1),("redo","verifier",1),
                                     ("redo","implementation",2),("redo","verifier",2)])
        self.assertTrue(self.orch.report("p", "redo", t["verifier"], t["revision"], "pass", "read file")["duplicate"])
        self.orch.close()
        self.orch = Orchestrator(self.root / "plan.sqlite", self.eng)
        self.assertEqual(self.orch.tick(), [])
        self.assertEqual(self.task("redo"), t)
        self.assertEqual(len(self.eng.members()), 4)
        old_worker = self.orch._names("p", "redo", 1)[0]
        with self.assertRaises(ValueError):
            self.orch.require_merge_ready(old_worker, t["revision"])
        with self.assertRaises(ValueError):
            self.orch.require_merge_ready(t["worker"], "a" * 40)
        self.assertTrue(self.orch.require_merge_ready(t["worker"], t["revision"]))
        self.assertFalse(self.orch.require_merge_ready("manual", t["revision"]))

    def test_cycle_unknown_dependency_and_wrong_actor_fail(self):
        with self.assertRaises(ValueError):
            self.orch.create("p", [item("self", dependencies=["self"])])
        with self.assertRaises(ValueError):
            self.orch.create("p", [item("self", dependencies=["unknown"])])
        self.orch.create("p", [item("first")])
        self.orch.tick()
        with self.assertRaisesRegex(ValueError, "actor"):
            self.orch.report("p", "first", "spoof", "a"*40, "ready", "free text")

    def test_no_report_blocks_and_propagates_even_if_output_says_pass(self):
        self.cancel_report = True
        self.orch.create("p", [item("first", attempts=1), item("second", dependencies=["first"])])
        self.pump(lambda: self.orch.summary("p")["status"] == "blocked")
        self.assertIn("without a structured report", self.task("first")["evidence"])
        self.assertEqual(self.task("second")["attempt"], 1)
        self.assertEqual(len(self.eng.members()), 1)

    def test_independent_tasks_schedule_in_deterministic_order(self):
        self.orch.create("p", [item("z"), item("a")])
        changes = self.orch.tick()
        self.assertEqual([x["task_id"] for x in changes], ["a", "z"])
        self.assertEqual(len(self.eng.members()), 2)
        self.orch.close()
        self.orch = Orchestrator(self.root / "plan.sqlite", self.eng)
        self.assertEqual(self.orch.tick(), [])
        self.assertEqual(len(self.eng.members()), 2)


if __name__ == "__main__":
    unittest.main()
