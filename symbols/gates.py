"""Fail-closed contracts and resource budgets for symbol analysis."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SymbolsStatus(str, Enum):
    """Page-level outcomes exposed by the symbols sidecars."""

    COMPLETE = "complete"
    NO_VALID_LEGEND = "no_valid_legend"
    PARTIAL = "partial"


class AnomalyCode(str, Enum):
    """Stable machine-readable reasons for a fail-closed outcome."""

    LEGEND_NOT_FOUND = "legend_not_found"
    LEGEND_HAS_NO_VALID_ROWS = "legend_has_no_valid_rows"
    LEGEND_HAS_NO_VALID_TEMPLATES = "legend_has_no_valid_templates"
    LEGEND_ENTRY_BUDGET_EXCEEDED = "legend_entry_budget_exceeded"
    TEMPLATE_COMPONENT_BUDGET_EXCEEDED = "template_component_budget_exceeded"
    TEMPLATE_DETECTION_BUDGET_EXCEEDED = "template_detection_budget_exceeded"
    CANDIDATE_COMPONENT_BUDGET_EXCEEDED = "candidate_component_budget_exceeded"
    CANDIDATE_BUDGET_EXCEEDED = "candidate_budget_exceeded"
    CROP_BUDGET_EXCEEDED = "crop_budget_exceeded"
    INSTANCE_BUDGET_EXCEEDED = "instance_budget_exceeded"


@dataclass(frozen=True, slots=True)
class SymbolsBudgets:
    """Hard limits. Exceeding a limit aborts a stage instead of truncating it."""

    max_legend_entries: int = 200
    max_template_components: int = 5_000
    max_template_detections: int = 2_000
    max_candidate_components: int = 10_000
    max_candidates: int = 2_000
    max_crop_files: int = 4_000
    max_instances: int = 2_000

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")


class BudgetExceeded(RuntimeError):
    """A stage exceeded a configured hard limit."""

    def __init__(
        self,
        *,
        stage: str,
        code: AnomalyCode,
        observed: int,
        limit: int,
    ) -> None:
        self.stage = stage
        self.code = code
        self.observed = observed
        self.limit = limit
        super().__init__(
            f"{stage}: {code.value}: observed {observed}, configured limit {limit}"
        )


def enforce_budget(
    *,
    stage: str,
    code: AnomalyCode,
    observed: int,
    limit: int,
) -> None:
    """Raise with diagnostics when work exceeds a hard budget."""

    if observed > limit:
        raise BudgetExceeded(
            stage=stage,
            code=code,
            observed=observed,
            limit=limit,
        )
