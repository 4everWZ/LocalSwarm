"""Codex delegation policy. Levels describe CLI effort, not token counts."""
from dataclasses import dataclass


EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
TASK_LEVELS = ("routine", "standard", "demanding", "complex")
# Deployment policy for the current Codex family, ordered from light to strong.
TIERS = (
    ("gpt-6-luna",),
    ("gpt-6.1-terra",),
    ("gpt-6.2-sol", "gpt-6.1-sol", "gpt-6-sol"),
    ("gpt-6.2-astra", "gpt-6-astra"),
)
RANK = {model: tier for tier, family in enumerate(TIERS) for model in family}
ALIASES = {"light": "low", "extra high": "xhigh", "extra-high": "xhigh"}


def effort_level(value):
    value = value.strip().lower()
    value = ALIASES.get(value, value)
    if value not in EFFORTS:
        raise ValueError("unknown Codex reasoning effort")
    return EFFORTS.index(value)


def model_rank(model):
    if model not in RANK:
        raise ValueError(f"model {model!r} has no LocalSwarm Codex budget tier")
    return RANK[model]


@dataclass(frozen=True)
class Budget:
    model: str
    effort: str

    def __post_init__(self):
        model_rank(self.model)
        object.__setattr__(self, "effort", EFFORTS[effort_level(self.effort)])

    def maximum(self, model):
        rank, own_rank = model_rank(model), model_rank(self.model)
        if rank > own_rank or (rank == own_rank and TIERS[rank].index(model) < TIERS[rank].index(self.model)):
            raise ValueError(f"worker model {model!r} exceeds coordinator model {self.model!r}")
        main = effort_level(self.effort)
        # Ultra is a coordinator setting. Workers use at most Max, and a lower
        # tier can trade one effort step for the smaller model, up to Extra High.
        return min(main, 4) if rank == own_rank else min(main + 1, 3)

    def select(self, task_level="standard", model="default", effort=None, *, ceiling=None):
        if task_level not in TASK_LEVELS:
            raise ValueError("unknown task level")
        if model in ("", "default", "auto"):
            rank = model_rank(self.model)
            model = TIERS[rank - 1][0] if rank and task_level != "complex" else self.model
        maximum = self.maximum(model)
        if ceiling is not None:
            maximum = min(maximum, ceiling.maximum(model))
        defaults = {"routine": 1, "standard": 2, "demanding": maximum, "complex": maximum}
        requested = defaults[task_level] if effort is None else effort_level(effort)
        if effort is not None and requested > maximum:
            raise ValueError(f"reasoning effort {effort!r} exceeds the coordinator delegation budget for {model!r}")
        return model, EFFORTS[min(requested, maximum)]
