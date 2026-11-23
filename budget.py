"""Select Codex assignments from account capabilities and a local tier policy."""
from dataclasses import dataclass
from fnmatch import fnmatchcase
import json
from pathlib import Path


EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
TASK_LEVELS = ("routine", "standard", "demanding", "complex")
TASK_TARGETS = {"routine": "medium", "standard": "medium", "demanding": "high", "complex": "xhigh"}
MODEL_TIERS = ("auto", "coordinator")
AUTO_MODELS = ("", "default", "auto")
ALIASES = {"light": "low", "extra high": "xhigh", "extra-high": "xhigh", "extra_high": "xhigh"}


def effort_level(value):
    value = value.strip().lower()
    value = ALIASES.get(value, value)
    if value not in EFFORTS:
        raise ValueError(f"unknown Codex reasoning effort {value!r}")
    return EFFORTS.index(value)


@dataclass(frozen=True)
class Budget:
    model: str
    effort: str

    def __post_init__(self):
        if not self.model or self.model.strip() != self.model:
            raise ValueError("expected a Codex model identifier")
        object.__setattr__(self, "effort", EFFORTS[effort_level(self.effort)])


@dataclass(frozen=True)
class RoutingPolicy:
    tiers: tuple[tuple[str, ...], ...]

    @classmethod
    def from_file(cls, path=None):
        source = Path(path) if path is not None else Path(__file__).with_name("routing-policy.json")
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"cannot read routing policy {source}: {exc}") from exc
        tiers = raw.get("tiers") if isinstance(raw, dict) else None
        if not isinstance(tiers, list) or not tiers:
            raise ValueError("routing policy requires a nonempty tiers list")
        names, selectors, configured = set(), set(), []
        for tier in tiers:
            name = tier.get("name") if isinstance(tier, dict) else None
            models = tier.get("models") if isinstance(tier, dict) else None
            if not isinstance(name, str) or not name.strip() or name in names:
                raise ValueError("routing tiers require distinct nonempty names")
            if not isinstance(models, list) or not models:
                raise ValueError(f"routing tier {name!r} requires model selectors")
            for selector in models:
                if not isinstance(selector, str) or not selector.strip() or selector in selectors:
                    raise ValueError("routing tiers require distinct nonempty model selectors")
                selectors.add(selector)
            names.add(name)
            configured.append(tuple(models))
        return cls(tuple(configured))

    def _position(self, model):
        for tier, selectors in enumerate(self.tiers):
            if model in selectors:
                return (tier, selectors.index(model), True)
        match = None
        for tier, selectors in enumerate(self.tiers):
            for preference, selector in enumerate(selectors):
                if fnmatchcase(model, selector):
                    if match is not None:
                        raise ValueError(f"model {model!r} matches more than one routing tier")
                    match = (tier, preference, False)
                    break
        return match

    def _allows_model(self, main, model):
        if model == main:
            return True
        parent, worker = self._position(main), self._position(model)
        if parent is None or worker is None:
            return False
        if worker[0] != parent[0]:
            return worker[0] < parent[0]
        # Exact IDs declare weaker versions inside a family. A glob identifies
        # the family without claiming an ordering for future versions.
        return parent[2] and worker[2] and worker[1] > parent[1]

    def _maximum(self, budget, model):
        parent, worker = self._position(budget.model), self._position(model)
        level = effort_level(budget.effort)
        if parent is not None and worker is not None and worker[0] < parent[0]:
            return min(level + 1, effort_level("xhigh"))
        return min(level, effort_level("max"))

    def validate_coordinator(self, budget, catalog, *, ceiling=None):
        self._position(budget.model)
        if budget.model not in catalog:
            raise ValueError(f"coordinator model {budget.model!r} is unavailable in the Codex catalog")
        supported = catalog[budget.model]
        if budget.effort not in supported:
            allowed = ", ".join(effort for effort in EFFORTS if effort in supported) or "none"
            raise ValueError(f"coordinator effort {budget.effort!r} is unavailable for {budget.model!r}. "
                             f"Supported efforts are {allowed}")
        if ceiling is not None and (not self._allows_model(ceiling.model, budget.model)
                                    or effort_level(budget.effort) > effort_level(ceiling.effort)):
            raise ValueError(f"coordinator {budget.model!r} at {budget.effort} exceeds the service ceiling "
                             f"of {ceiling.model!r} at {ceiling.effort}")

    def _worker_options(self, coordinator, catalog, ceiling):
        options = {}
        for model, supported in catalog.items():
            if not self._allows_model(coordinator.model, model):
                continue
            maximum = self._maximum(coordinator, model)
            if ceiling is not None:
                if not self._allows_model(ceiling.model, model):
                    continue
                maximum = min(maximum, self._maximum(ceiling, model))
            allowed = tuple(effort for effort in EFFORTS[:maximum + 1] if effort in supported)
            if allowed:
                options[model] = allowed
        return options

    def _automatic_candidates(self, coordinator, options):
        parent = self._position(coordinator.model)
        lower = []
        if parent is not None:
            for model in options:
                position = self._position(model)
                if position is not None and position[0] < parent[0]:
                    lower.append((-position[0], position[1], model))
        lower.sort(key=lambda candidate: candidate[:2])
        return [model for _, _, model in lower] + [coordinator.model]

    def select(self, coordinator, catalog, task_level="standard", model="default", effort=None, *,
               model_tier="auto", ceiling=None):
        if task_level not in TASK_LEVELS:
            raise ValueError("unknown task level")
        if model_tier not in MODEL_TIERS:
            raise ValueError("unknown model tier selection")
        self.validate_coordinator(coordinator, catalog, ceiling=ceiling)
        options = self._worker_options(coordinator, catalog, ceiling)
        automatic_model = model in AUTO_MODELS
        if model_tier == "coordinator":
            if not automatic_model and model != coordinator.model:
                raise ValueError(f"model_tier='coordinator' requires the active model {coordinator.model!r}")
            candidates = [coordinator.model]
        elif automatic_model:
            candidates = self._automatic_candidates(coordinator, options)
        else:
            candidates = [model]

        requested = effort_level(TASK_TARGETS[task_level] if effort is None else effort)
        for candidate in candidates:
            allowed = options.get(candidate, ())
            if effort is not None:
                if EFFORTS[requested] in allowed:
                    return candidate, EFFORTS[requested]
            else:
                for level in reversed(allowed):
                    if effort_level(level) <= requested:
                        return candidate, level

        legal = {candidate: options[candidate] for candidate in candidates if candidate in options}
        if not legal and model_tier == "auto" and not automatic_model:
            legal = options
        ranges = ", ".join(f"{candidate} ({'/'.join(allowed)})" for candidate, allowed in legal.items())
        target = candidates[0] if len(candidates) == 1 else "automatic candidates"
        raise ValueError(f"Codex assignment for {target!r} is unavailable or exceeds the delegation budget. "
                         f"Allowed assignments are {ranges or 'none'}")
