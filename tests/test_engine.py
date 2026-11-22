import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from engine import Engine


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True, stderr=subprocess.STDOUT).strip()


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.db = self.root / "board.sqlite"
        git(self.repo, "init")
        git(self.repo, "config", "user.name", "Swarm Test")
        git(self.repo, "config", "user.email", "swarm@example.invalid")
        (self.repo / "allowed.txt").write_text("base")
        (self.repo / "other.txt").write_text("base")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "initial")
        self.engine = None
        self.release = threading.Event()

    def tearDown(self):
        self.release.set()
        if self.engine:
            self.engine.close()
        self.tmp.cleanup()

    def engine_with(self, runner, max_workers=4):
        self.engine = Engine(self.db, self.repo, runner, max_workers)
        return self.engine

    @staticmethod
    def spawn(engine, name="builder"):
        return engine.spawn(name, "implementation", "make a local test change", ["allowed.txt"], "mock", "fake")

    def pump(self, until):
        for _ in range(500):
            self.engine.tick()
            if until():
                return
            time.sleep(.002)
        self.fail("condition did not arrive within one second")

    def test_persistence_and_no_coordinator_or_unaddressed_wake(self):
        calls = []
        def run(request):
            calls.append(request)
            return {"session_id": "abc", "content": "first", "mentions": ["coordinator"], "status": "waiting"}
        engine = self.engine_with(run)
        self.spawn(engine)
        with self.assertRaises(ValueError):
            self.spawn(engine, "coordinator")
        engine.post("coordinator", [], "unaddressed")
        engine.post("coordinator", ["coordinator"], "self note")
        self.pump(lambda: any(e["status"] == "waiting" for e in engine.read()))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["actor"], "builder")
        self.assertEqual(engine.members()[0]["session_id"], "abc")
        rows = engine.read()
        self.assertEqual([r["seq"] for r in rows], sorted({r["seq"] for r in rows}))
        engine.close()
        engine = self.engine_with(run)
        self.assertEqual(engine.read(), rows)
        self.assertEqual(engine.tick()["launched"], [])
        self.assertGreater(engine.post("other", [], "persistent"), rows[-1]["seq"])

    def test_coalesces_mentions_during_active_run_and_passes_session(self):
        started = threading.Event()
        entered = []
        def run(request):
            entered.append(request)
            if len(entered) == 1:
                started.set()
                self.release.wait(3)
            return {"session_id": "saved"}
        engine = self.engine_with(run, max_workers=1)
        self.spawn(engine)
        self.assertEqual(engine.tick()["launched"], ["builder"])
        self.assertTrue(started.wait(2))
        seq1 = engine.post("coordinator", ["builder"], "second")
        seq2 = engine.post("coordinator", ["builder"], "third")
        self.assertEqual(engine.tick()["launched"], [])
        self.release.set()
        self.pump(lambda: len(entered) == 2)
        self.assertEqual([e["seq"] for e in entered[1]["events"]], [seq1, seq2])
        self.assertEqual(entered[1]["session_id"], "saved")
        self.pump(lambda: self.engine.members()[0]["active"] is None)
        self.assertEqual(engine.tick()["launched"], [])

    def test_exclusive_engine_restart_retires_old_claim_but_retains_later_wake(self):
        calls = []
        def run(request):
            calls.append(request)
        engine = self.engine_with(run)
        self.spawn(engine)
        engine.close()
        with sqlite3.connect(self.db) as db:
            claimed = db.execute("SELECT max(seq) FROM events").fetchone()[0]
            db.execute("UPDATE workers SET active='prior-crashed-run',cursor=?", (claimed,))
            db.execute("INSERT INTO events(actor,mentions,content) VALUES('coordinator','[\"builder\"]','later wake')")
        engine = self.engine_with(run)
        self.assertTrue(any(e["status"] == "interrupted" for e in engine.read()))
        with self.assertRaises(RuntimeError):
            Engine(self.db, self.repo, run)
        self.pump(lambda: bool(calls))
        self.assertEqual([e["content"] for e in calls[0]["events"]], ["later wake"])

    def test_failed_runner_is_reaped_without_replaying_claim(self):
        calls = []
        def run(request):
            calls.append(request)
            raise RuntimeError("synthetic failure")
        engine = self.engine_with(run)
        self.spawn(engine)
        self.pump(lambda: any(e["status"] == "failed" for e in engine.read()))
        self.assertIsNone(engine.members()[0]["active"])
        self.assertEqual(engine.tick()["launched"], [])
        self.assertEqual(len(calls), 1)

    def test_merge_requires_exact_explicit_revision_and_valid_scope(self):
        engine = self.engine_with(lambda req: {})
        worker = self.spawn(engine)
        location = Path(worker["repo_path"])
        base = git(self.repo, "rev-parse", "HEAD")
        (location / "allowed.txt").write_text("updated")
        git(location, "add", ".")
        git(location, "commit", "-m", "scoped")
        revision = git(location, "rev-parse", "HEAD")
        self.assertEqual(engine.preview_merge("builder")["paths"], ["allowed.txt"])
        with self.assertRaises(ValueError):
            engine.merge("builder")
        with self.assertRaises(ValueError):
            engine.merge("builder", base)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), base)
        review = engine.merge("builder", revision, base)
        self.assertEqual(review["revision"], revision)
        self.assertEqual((self.repo / "allowed.txt").read_text(), "updated")

    def test_verifier_pins_exact_worker_revision(self):
        e = self.engine_with(lambda req: {})
        worktree = Path(self.spawn(e)["repo_path"])
        (worktree / "allowed.txt").write_text("candidate")
        git(worktree, "add", ".")
        git(worktree, "commit", "-m", "candidate")
        notes = e.preview_merge("builder")
        with self.assertRaisesRegex(ValueError, "changed"):
            e.spawn("review", "verifier", "Review", [], "mock", "", "builder", notes["base_revision"])
        reviewer = e.spawn("review", "verifier", "Review", [], "mock", "", "builder", notes["revision"])
        self.assertEqual(git(Path(reviewer["repo_path"]), "rev-parse", "HEAD"), notes["revision"])
        with self.assertRaisesRegex(ValueError, "verifiers"):
            e.preview_merge("review")

    def test_turn_budget_caps_wake_loop(self):
        calls = []
        self.engine = Engine(self.db, self.repo, lambda req: calls.append(req), max_turns=1)
        self.spawn(self.engine)
        self.pump(lambda: len(calls) == 1 and not self.engine.members()[0]["active"])
        self.engine.post("coordinator", ["builder"], "an excess wake")
        self.engine.tick()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.engine.read()[-1]["status"], "limited")

    def test_denies_hidden_out_of_scope_edit_even_if_reverted(self):
        engine = self.engine_with(lambda req: {})
        location = Path(self.spawn(engine)["repo_path"])
        (location / "other.txt").write_text("out of scope")
        git(location, "add", ".")
        git(location, "commit", "-m", "outside")
        (location / "other.txt").write_text("base")
        (location / "allowed.txt").write_text("updated")
        git(location, "add", ".")
        git(location, "commit", "-m", "net scoped")
        revision = git(location, "rev-parse", "HEAD")
        with self.assertRaisesRegex(ValueError, "outside worker scope"):
            engine.merge("builder", revision)
        self.assertEqual((self.repo / "allowed.txt").read_text(), "base")


if __name__ == "__main__":
    unittest.main()
