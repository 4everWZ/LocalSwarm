import subprocess
import sys
import textwrap
import unittest
from unittest.mock import patch

from catalog import CodexCatalog


class CatalogTest(unittest.TestCase):
    def mock_app_server(self, script):
        launch = subprocess.Popen
        processes = []

        def fake(argv, **kwargs):
            self.assertEqual(argv, ["test-codex", "app-server", "--listen", "stdio://"])
            process = launch([sys.executable, "-u", "-c", textwrap.dedent(script)], **kwargs)
            processes.append(process)
            return process

        patcher = patch("catalog.subprocess.Popen", side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return processes

    def test_reads_paginated_capabilities_by_canonical_id(self):
        processes = self.mock_app_server("""
            import json, sys

            def respond(request, result):
                print(json.dumps({"id": request["id"], "result": result}))

            hello = json.loads(sys.stdin.readline())
            assert hello["method"] == "initialize"
            respond(hello, {})
            assert json.loads(sys.stdin.readline())["method"] == "initialized"

            first = json.loads(sys.stdin.readline())
            assert first["method"] == "model/list"
            assert first["params"]["modelProvider"] == "openai"
            assert not first["params"]["includeHidden"]
            print(json.dumps({"method": "notification", "params": {}}))
            respond(first, {"data": [{"id": "preset", "model": "actual-id",
                       "supportedReasoningEfforts": [{"reasoningEffort": "medium"}]}], "nextCursor": "p2"})

            second = json.loads(sys.stdin.readline())
            assert second["params"]["cursor"] == "p2"
            respond(second, {"data": [{"model": "other-id", "supportedReasoningEfforts": [
                       {"reasoningEffort": "low"}, {"reasoningEffort": "high"}]},
                       {"model": "hidden-id", "hidden": True,
                        "supportedReasoningEfforts": [{"reasoningEffort": "low"}]}], "nextCursor": None})
        """)
        self.assertEqual(CodexCatalog("test-codex").models(), {
            "actual-id": frozenset(("medium",)), "other-id": frozenset(("low", "high")),
        })
        self.assertEqual(processes[0].poll(), 0)

    def test_refreshes_once_for_the_same_missing_model(self):
        catalog = CodexCatalog("test-codex")
        with patch("catalog._model_capabilities", side_effect=[
            {"old": frozenset(("medium",))}, {"old": frozenset(("medium",)), "new": frozenset(("high",))},
        ]) as query:
            result = catalog.models()
            result.clear()
            self.assertIn("old", catalog.models(("old",)))
            self.assertIn("new", catalog.models(("new", "missing")))
            self.assertNotIn("missing", catalog.models(("missing",)))
            self.assertEqual(query.call_count, 2)

    def test_rpc_errors_are_sanitized(self):
        processes = self.mock_app_server("""
            import json, sys
            request = json.loads(sys.stdin.readline())
            print(json.dumps({"id": request["id"], "error": {"message": "sensitive-error-details"}}))
        """)
        with self.assertRaisesRegex(ValueError, "rejected initialize") as caught:
            CodexCatalog("test-codex").models()
        self.assertNotIn("sensitive-error-details", str(caught.exception))
        self.assertIsNotNone(processes[0].poll())

    def test_timeout_reaps_unresponsive_process(self):
        processes = self.mock_app_server("""
            import time
            time.sleep(60)
        """)
        with self.assertRaisesRegex(ValueError, "Timed out"):
            CodexCatalog("test-codex", timeout=.2).models()
        self.assertIsNotNone(processes[0].poll())
