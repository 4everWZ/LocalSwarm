from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from budget import Budget, EFFORTS
from engine import Engine
from orchestrator import Orchestrator
from swarm import Service


ASTRA = {"model": "gpt-6.2-astra", "reasoning_effort": "medium"}
SOL = {"model": "gpt-6.2-sol", "reasoning_effort": "high"}
TERRA = "gpt-6.1-terra"


class FixtureCatalog:
    def __init__(self):
        self.available = {model: EFFORTS for model in (ASTRA["model"], SOL["model"], TERRA)}

    def models(self, required=()):
        return self.available.copy()


def init_repo(path):
    subprocess.run(["git", "init", "-q", path], check=True)
    subprocess.run(["git", "-C", path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "--allow-empty", "-qm", "initial"], check=True)

class BudgetTest(unittest.TestCase):
    def test_budget_persists_and_cross_plan_posts_skip_coordinator_context(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            init_repo(path)
            calls = []
            engine = Engine(root / "board.db", root, calls.append)
            plans = Orchestrator(root / "plans.db", engine)
            catalog = FixtureCatalog()
            service = Service(engine, "owner", "board", backends=("mock", "codex"), orchestrator=plans, catalog=catalog)
            task = {"id": "build", "dependencies": [], "backend": "codex", "role": "worker",
                    "allowed_paths": ["README.md"], "criterion": "README describes startup.",
                    "verifier_model_tier": "coordinator", "verifier_reasoning_effort": "low"}
            first_request = {"plan_id": "api", "goal": "Startup", "tasks": [task], "coordinator": ASTRA}
            try:
                with self.assertRaisesRegex(ValueError, "active coordinator"):
                    service.tool("coordinator", "swarm_plan", {"plan_id": "invalid", "goal": "", "tasks": [task]})
                with self.assertRaisesRegex(ValueError, "delegation budget"):
                    service.tool("coordinator", "swarm_plan", {**first_request,
                        "tasks": [{**task, "verifier_reasoning_effort": "xhigh"}]})

                preview = service.tool("coordinator", "swarm_policy", {"coordinator": ASTRA})
                self.assertEqual(preview["preview"]["complex"]["auto"], {"model": SOL["model"], "reasoning_effort": "high"})
                service.tool("coordinator", "swarm_plan", first_request)
                service.tool("coordinator", "swarm_plan", {**first_request, "plan_id": "docs", "coordinator": SOL})
                manual = service.tool("coordinator", "swarm_spawn", {"name": "manual", "role": "worker",
                    "brief": "Inspect the README", "allowed_paths": ["README.md"], "backend": "codex",
                    "coordinator": ASTRA, "model_tier": "coordinator", "task_level": "complex"})
                self.assertEqual((manual["model"], manual["reasoning_effort"], manual["coordinator"]),
                                 (ASTRA["model"], "medium", ASTRA))
                with self.assertRaisesRegex(ValueError, "different definition"):
                    service.tool("coordinator", "swarm_plan", {**first_request, "coordinator": SOL})
                plans.tick()
                api = plans.summary("api")["tasks"][0]["worker"]
                docs = plans.summary("docs")["tasks"][0]["worker"]
                stored = {row["name"]: row for row in engine.members()}
                self.assertEqual(stored[api]["coordinator"], ASTRA)
                self.assertEqual(stored[docs]["coordinator"], SOL)
                self.assertEqual(service.worker_assignment(stored[api]), (SOL["model"], "medium"))
                self.assertEqual(service.worker_assignment(stored[docs]), (TERRA, "medium"))
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
                self.assertEqual((compact["verifier_model"], compact["verifier_reasoning_effort"]), (SOL["model"], "low"))
                del catalog.available[SOL["model"]]
                retry = service.tool("coordinator", "swarm_plan", first_request)
                self.assertEqual(retry["tasks"][0]["model"], SOL["model"])
                with self.assertRaisesRegex(ValueError, "Saved Codex assignment.*is unavailable"):
                    service.worker_assignment(stored[api])
                engine.tick()
            finally:
                plans.close()
                engine.close()
            delivered = next(call["events"] for call in calls if call["actor"] == docs)
            self.assertTrue(any(event["content"] == "Shared setup is ready" for event in delivered))

            reopened = Engine(root / "board.db", root, lambda _: {})
            recovered_plans = Orchestrator(root / "plans.db", reopened)
            try:
                self.assertEqual(recovered_plans.summary("api")["coordinator"], ASTRA)
                self.assertEqual(reopened._get(api)["coordinator"], ASTRA)
            finally:
                recovered_plans.close()
                reopened.close()

    def test_legacy_ceiling_requires_explicit_migration_for_old_worker(self):
        catalog = FixtureCatalog()
        old = {"backend": "codex", "model": TERRA, "reasoning_effort": "high"}
        dynamic = Service(None, "owner", "board", backends=("codex",), catalog=catalog)
        with self.assertRaisesRegex(ValueError, "legacy Codex assignment"):
            dynamic.worker_assignment(old)
        fixed = Service(None, "owner", "board", backends=("codex",), budget=Budget(SOL["model"], "high"),
                        catalog=catalog)
        self.assertEqual(fixed.worker_assignment(old), (TERRA, "high"))
        with self.assertRaisesRegex(ValueError, "exceeds the service ceiling"):
            fixed.preview_policy({"coordinator": ASTRA})

    def test_policy_previews_eligible_steps_when_catalog_has_sparse_efforts(self):
        catalog = FixtureCatalog()
        catalog.available = {SOL["model"]: ("xhigh",)}
        service = Service(None, "owner", "board", backends=("codex",), catalog=catalog)
        preview = service.preview_policy({"coordinator": {**SOL, "reasoning_effort": "xhigh"}})["preview"]
        self.assertIn("blocked", preview["standard"]["coordinator"])
        self.assertEqual(preview["complex"]["auto"], {"model": SOL["model"], "reasoning_effort": "xhigh"})

    def test_unavailable_saved_assignment_blocks_plan_and_notifies_coordinator(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            init_repo(path)

            def run(request):
                try:
                    service.worker_assignment(request)
                except ValueError as exc:
                    return {"status": "blocked", "content": str(exc)}
                raise AssertionError("An unavailable model must not start a provider turn")

            catalog = FixtureCatalog()
            engine = Engine(root / "board.db", root, run)
            plans = Orchestrator(root / "plans.db", engine)
            service = Service(engine, "owner", "board", backends=("codex",), orchestrator=plans, catalog=catalog)
            task = {"id": "build", "dependencies": [], "backend": "codex", "role": "worker",
                    "allowed_paths": ["README.md"], "criterion": "README describes startup."}
            try:
                service.tool("coordinator", "swarm_plan", {"plan_id": "docs", "goal": "Startup",
                             "tasks": [task], "coordinator": ASTRA})
                plans.tick()
                del catalog.available[SOL["model"]]
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    engine.tick()
                    plans.tick()
                    result = plans.summary("docs")
                    if result["status"] == "blocked":
                        break
                    time.sleep(.01)
                self.assertEqual(result["status"], "blocked")
                outcome = result["tasks"][0]
                self.assertEqual(outcome["attempt"], 1)
                self.assertIn("Saved Codex assignment 'gpt-6.2-sol' at 'medium' is unavailable", outcome["evidence"])
                inbox = service.tool("coordinator", "swarm_inbox", {})["events"]
                self.assertTrue(any(row["content"] == outcome["evidence"] for row in inbox))
            finally:
                plans.close()
                engine.close()
