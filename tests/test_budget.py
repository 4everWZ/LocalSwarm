from pathlib import Path
import subprocess
import tempfile
import unittest

from budget import Budget
from engine import Engine
from orchestrator import Orchestrator
from swarm import Service


class BudgetTest(unittest.TestCase):
    def test_task_levels_and_explicit_overrides(self):
        cap = Budget("gpt-6.2-sol", "high")
        self.assertEqual(cap.select("routine"), ("gpt-6.1-terra", "medium"))
        self.assertEqual(cap.select(), ("gpt-6.1-terra", "high"))
        self.assertEqual(cap.select("demanding"), ("gpt-6.1-terra", "xhigh"))
        self.assertEqual(cap.select("complex"), ("gpt-6.2-sol", "high"))
        self.assertEqual(cap.select(model="gpt-6.1-terra", effort="Extra High"), ("gpt-6.1-terra", "xhigh"))
        self.assertEqual(cap.select(model="gpt-6.2-sol", effort="Light"), ("gpt-6.2-sol", "low"))
        for model, effort in [("gpt-6.2-astra", "low"), ("gpt-6.2-sol", "xhigh"),
                              ("gpt-6.1-terra", "max"), ("custom-model", "low")]:
            with self.subTest(model=model, effort=effort), self.assertRaises(ValueError):
                cap.select(model=model, effort=effort)

    def test_budget_persists_and_cross_plan_posts_skip_coordinator_context(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            subprocess.run(["git", "init", "-q", path], check=True)
            subprocess.run(["git", "-C", path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                            "commit", "--allow-empty", "-qm", "initial"], check=True)
            calls = []
            engine = Engine(root / "board.db", root, calls.append)
            plans = Orchestrator(root / "plans.db", engine)
            cap = Budget("gpt-6.2-sol", "high")
            service = Service(engine, "owner", "board", backends=("mock", "codex"), orchestrator=plans, budget=cap)
            task = {"id": "build", "dependencies": [], "backend": "codex", "role": "worker",
                    "allowed_paths": ["README.md"], "criterion": "README describes startup.",
                    "verifier_model": "gpt-6.2-sol", "verifier_reasoning_effort": "high"}
            try:
                with self.assertRaisesRegex(ValueError, "exceeds"):
                    service.tool("coordinator", "swarm_plan", {"plan_id": "invalid", "goal": "",
                        "tasks": [{**task, "verifier_reasoning_effort": "xhigh"}]})
                with self.assertRaisesRegex(ValueError, "exceeds"):
                    service.tool("coordinator", "swarm_plan", {"plan_id": "invalid", "goal": "", "tasks": [task],
                        "coordinator": {"model": "gpt-6.2-sol", "reasoning_effort": "medium"}})
                for plan_id in ("api", "docs"):
                    service.tool("coordinator", "swarm_plan", {"plan_id": plan_id, "goal": "Startup", "tasks": [task]})
                plans.tick()
                api = plans.summary("api")["tasks"][0]["worker"]
                docs = plans.summary("docs")["tasks"][0]["worker"]
                stored = {row["name"]: row for row in engine.members()}
                self.assertEqual((stored[docs]["model"], stored[docs]["reasoning_effort"]), ("gpt-6.1-terra", "high"))
                service.tool(api, "board_post", {"mentions": ["@plan:docs"], "content": "Shared setup is ready"})
                service.tool(api, "board_post", {"content": "peer debug trace"})
                service.tool(docs, "board_post", {"mentions": ["@coordinator"], "content": "One decision needed"})
                service.tool(api, "board_post", {"mentions": ["@coordinator"], "content": "a" * 1800})
                inbox = service.tool("coordinator", "swarm_inbox", {"limit": 1})
                self.assertTrue(inbox["has_more"])
                self.assertEqual([row["content"] for row in inbox["events"]], ["One decision needed"])
                next_page = service.tool("coordinator", "swarm_inbox", {"since_seq": inbox["next_seq"]})
                self.assertEqual(len(next_page["events"][0]["content"]), 1500)
                self.assertEqual([row["content"] for row in engine.inbox(docs)["events"]], ["Shared setup is ready"])
                compact = service.tool("coordinator", "swarm_status", {"plan_id": "docs", "compact": True})["tasks"][0]
                self.assertNotIn("criterion", compact)
                self.assertEqual(compact["verifier_reasoning_effort"], "high")
                engine.tick()
            finally:
                plans.close()
                engine.close()
            delivered = next(call["events"] for call in calls if call["actor"] == docs)
            self.assertTrue(any(event["content"] == "Shared setup is ready" for event in delivered))
