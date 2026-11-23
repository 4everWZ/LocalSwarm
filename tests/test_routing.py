import json
from pathlib import Path
import tempfile
import unittest

from budget import Budget, EFFORTS, RoutingPolicy


ASTRA = "gpt-6.2-astra"
SOL = "gpt-6.2-sol"
TERRA = "gpt-6.1-terra"
LUNA = "gpt-6-luna"
CATALOG = {model: frozenset(EFFORTS) for model in (ASTRA, SOL, TERRA, LUNA)}


class RoutingPolicyTest(unittest.TestCase):
    def setUp(self):
        self.policy = RoutingPolicy.from_file()

    def test_progression_uses_current_coordinator_and_explicit_tier(self):
        for coordinator, lower, deepest_lower in (
            (Budget(ASTRA, "medium"), SOL, ("medium", "high", "high")),
            (Budget(ASTRA, "high"), SOL, ("medium", "high", "xhigh")),
            (Budget(SOL, "Light"), TERRA, ("medium", "medium", "medium")),
            (Budget(SOL, "high"), TERRA, ("medium", "high", "xhigh")),
        ):
            with self.subTest(coordinator=coordinator):
                for level in ("routine", "standard"):
                    self.assertEqual(self.policy.select(coordinator, CATALOG, level), (lower, "medium"))
                for level, effort in zip(("standard", "demanding", "complex"), deepest_lower):
                    self.assertEqual(self.policy.select(coordinator, CATALOG, level), (lower, effort))
                self.assertEqual(
                    self.policy.select(coordinator, CATALOG, "complex", model_tier="coordinator"),
                    (coordinator.model, coordinator.effort),
                )

    def test_skips_unavailable_tiers_and_only_uses_supported_effort(self):
        coordinator = Budget(ASTRA, "high")
        catalog = {ASTRA: EFFORTS, TERRA: ("low", "medium"), LUNA: ("high",)}
        self.assertEqual(self.policy.select(coordinator, catalog, "complex"), (TERRA, "medium"))
        self.assertEqual(self.policy.select(coordinator, {ASTRA: EFFORTS, LUNA: ("high",)}),
                         (ASTRA, "medium"))
        self.assertEqual(self.policy.select(coordinator, {ASTRA: ("low", "high")}, "standard"),
                         (ASTRA, "low"))
        with self.assertRaisesRegex(ValueError, r"Allowed assignments are .*terra \(low/medium\)"):
            self.policy.select(coordinator, catalog, model=TERRA, effort="Extra High")

    def test_explicit_max_is_same_tier_only_and_ultra_is_reserved(self):
        coordinator = Budget(SOL, "ultra")
        self.assertEqual(self.policy.select(coordinator, CATALOG, "complex", model_tier="coordinator"),
                         (SOL, "xhigh"))
        self.assertEqual(self.policy.select(coordinator, CATALOG, effort="max", model_tier="coordinator"),
                         (SOL, "max"))
        for child, effort in ((SOL, "ultra"), (TERRA, "max"), (ASTRA, "low")):
            with self.subTest(model=child, effort=effort), self.assertRaisesRegex(ValueError, "exceeds"):
                self.policy.select(coordinator, CATALOG, model=child, effort=effort)
        with self.assertRaisesRegex(ValueError, "requires the active model"):
            self.policy.select(coordinator, CATALOG, model=TERRA, model_tier="coordinator")

    def test_authorized_exact_variants_and_fixed_service_ceiling(self):
        older = "gpt-6.1-sol"
        catalog = {**CATALOG, older: EFFORTS, "gpt-6.3-sol": EFFORTS}
        coordinator = Budget(SOL, "high")
        self.assertEqual(self.policy.select(coordinator, catalog, model=older, effort="high"), (older, "high"))
        for child, effort in ((older, "xhigh"), ("gpt-6.3-sol", "low")):
            with self.subTest(model=child, effort=effort), self.assertRaisesRegex(ValueError, "exceeds"):
                self.policy.select(coordinator, catalog, model=child, effort=effort)
        with self.assertRaisesRegex(ValueError, "exceeds the service ceiling"):
            self.policy.select(Budget(ASTRA, "low"), catalog, ceiling=coordinator)
        with self.assertRaisesRegex(ValueError, "exceeds the service ceiling"):
            self.policy.select(Budget(SOL, "xhigh"), catalog, ceiling=coordinator)

    def test_unclassified_models_can_delegate_to_exact_model(self):
        coordinator = Budget("new-lead", "medium")
        catalog = {SOL: EFFORTS, coordinator.model: ("low", "medium", "xhigh")}
        self.assertEqual(self.policy.select(coordinator, catalog, "complex"), (coordinator.model, "medium"))
        with self.assertRaisesRegex(ValueError, "unavailable in the Codex catalog"):
            self.policy.select(coordinator, CATALOG)
        with self.assertRaisesRegex(ValueError, "Supported efforts are low, medium, xhigh"):
            self.policy.select(Budget(coordinator.model, "high"), catalog)

    def test_new_models_can_be_registered_with_policy_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "routing.json"
            path.write_text(json.dumps({"tiers": [
                {"name": "worker", "models": ["new-*"]},
                {"name": "lead", "models": ["new-pro-v2"]},
            ]}), encoding="utf-8")
            policy = RoutingPolicy.from_file(path)
        self.assertEqual(policy.select(Budget("new-pro-v2", "high"),
                                       {"new-lite-v1": EFFORTS, "new-pro-v2": EFFORTS}, "complex"),
                         ("new-lite-v1", "xhigh"))


if __name__ == "__main__":
    unittest.main()
