"""Page-level orchestration for the opt-in symbols pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .artifacts import PageArtifacts, write_page_artifacts
from .candidate_detector import CandidateDetectorConfig, detect_page_open_set
from .gates import (
    AnomalyCode,
    BudgetExceeded,
    SymbolsBudgets,
    SymbolsStatus,
    enforce_budget,
)
from .legend_layout import DEFAULT_MIN_CONFIDENCE, extract_page_legend
from .resolver import ResolverConfig, resolve_page_symbols
from .schema import PageSymbolsSummary
from .template_matcher import TemplateMatcherConfig, match_page_templates
from .vlm import (
    LegendAssistError,
    LegendVlmConfig,
    SymbolReviewVlmConfig,
    assist_page_legend,
    review_page_symbols,
)


@dataclass(frozen=True, slots=True)
class SymbolsPipelineConfig:
    """Runtime settings kept separate from the HTTP service configuration."""

    dpi: int = 144
    legend_min_confidence: float = DEFAULT_MIN_CONFIDENCE
    template_matcher: TemplateMatcherConfig = TemplateMatcherConfig()
    candidate_detector: CandidateDetectorConfig = CandidateDetectorConfig()
    resolver: ResolverConfig = ResolverConfig()
    budgets: SymbolsBudgets = SymbolsBudgets()
    legend_vlm: LegendVlmConfig = field(default_factory=LegendVlmConfig.from_env)
    review_vlm: SymbolReviewVlmConfig = field(
        default_factory=SymbolReviewVlmConfig.from_env
    )

    def __post_init__(self) -> None:
        if isinstance(self.dpi, bool) or not isinstance(self.dpi, int) or self.dpi < 72:
            raise ValueError("dpi must be an integer of at least 72")
        if not 0 <= self.legend_min_confidence <= 1:
            raise ValueError("legend_min_confidence must be between 0 and 1")


def _extract_page_legend(
    document: str | Path,
    *,
    page_number: int,
    output_root: str | Path,
    settings: SymbolsPipelineConfig,
):
    deterministic = extract_page_legend(
        document,
        page_number=page_number,
        output_root=output_root,
        dpi=settings.dpi,
        min_confidence=settings.legend_min_confidence,
    )
    result, entries = deterministic
    readable = sum(entry.status == "extracted" for entry in entries)
    incomplete = bool(entries) and readable * 2 < len(entries)
    if (
        not settings.legend_vlm.enabled
        or (result.status == "found" and entries and not incomplete)
    ):
        return deterministic
    try:
        return assist_page_legend(
            document,
            page_number=page_number,
            output_root=output_root,
            config=settings.legend_vlm,
        )
    except LegendAssistError:
        # H5 is assistance only. Its trace contains the provider/validation
        # failure; the deterministic fail-closed result remains authoritative.
        return deterministic


def _write_status(
    output_root: str | Path,
    artifacts: PageArtifacts,
    *,
    status: SymbolsStatus,
    anomaly_codes: tuple[AnomalyCode, ...],
) -> PageArtifacts:
    """Persist a terminal fail-closed result without discarding completed work."""

    result = PageArtifacts(
        page=artifacts.page,
        legend_entries=artifacts.legend_entries,
        symbol_candidates=artifacts.symbol_candidates,
        symbol_types=artifacts.symbol_types,
        symbol_instances=artifacts.symbol_instances,
        unclassified_symbols=artifacts.unclassified_symbols,
        unmatched_legend_entries=artifacts.unmatched_legend_entries,
        conflicts=artifacts.conflicts,
        summary=PageSymbolsSummary(
            page=artifacts.page,
            legend_entry_count=len(artifacts.legend_entries),
            symbol_type_count=len(artifacts.symbol_types),
            symbol_instance_count=len(artifacts.symbol_instances),
            unclassified_count=len(artifacts.unclassified_symbols),
            unmatched_legend_entry_count=len(artifacts.unmatched_legend_entries),
            conflict_count=len(artifacts.conflicts),
            status=status.value,
            anomaly_codes=tuple(code.value for code in anomaly_codes),
        ),
    )
    write_page_artifacts(output_root, result)
    return result


def _review_resolved_page(
    artifacts: PageArtifacts,
    *,
    output_root: str | Path,
    settings: SymbolsPipelineConfig,
) -> tuple[PageArtifacts, dict[str, int]]:
    if not settings.review_vlm.enabled:
        return artifacts, {}
    # The review function rejects malformed responses per type and preserves
    # the deterministic result, so provider failures cannot fail the page.
    return review_page_symbols(
        artifacts,
        output_root=output_root,
        config=settings.review_vlm,
    )


def run_page_symbols(
    document: str | Path,
    *,
    page_number: int,
    output_root: str | Path,
    config: SymbolsPipelineConfig | None = None,
    profile_output: str | Path | None = None,
    profile_fixture_id: str | None = None,
) -> PageArtifacts:
    """Run S3-S6 in order and persist the final page sidecars.

    Budget violations are converted to a persisted ``partial`` result. Other
    errors propagate to the service boundary, which owns fault isolation.
    """

    settings = config or SymbolsPipelineConfig()
    if profile_output is not None:
        # Imported lazily to keep the normal production path independent from
        # the development-only baseline serializer.
        from .baseline import run_profiled_page, write_baseline

        artifacts, profile = run_profiled_page(
            document,
            page_number=page_number,
            output_root=output_root,
            fixture_id=profile_fixture_id or f"page-{page_number}",
            config=settings,
        )
        write_baseline(profile_output, profile)
        return artifacts

    legend_result, legend_entries = _extract_page_legend(
        document,
        page_number=page_number,
        output_root=output_root,
        settings=settings,
    )
    s3_artifacts = PageArtifacts(page=page_number, legend_entries=legend_entries)
    if legend_result.status != "found":
        return _write_status(
            output_root,
            s3_artifacts,
            status=SymbolsStatus.NO_VALID_LEGEND,
            anomaly_codes=(AnomalyCode.LEGEND_NOT_FOUND,),
        )
    if not legend_entries:
        return _write_status(
            output_root,
            s3_artifacts,
            status=SymbolsStatus.NO_VALID_LEGEND,
            anomaly_codes=(AnomalyCode.LEGEND_HAS_NO_VALID_ROWS,),
        )

    try:
        enforce_budget(
            stage="s3",
            code=AnomalyCode.LEGEND_ENTRY_BUDGET_EXCEEDED,
            observed=len(legend_entries),
            limit=settings.budgets.max_legend_entries,
        )
        template_metrics: dict[str, int] = {}
        matched = match_page_templates(
            document,
            page_number=page_number,
            output_root=output_root,
            legend_entries=legend_entries,
            dpi=settings.dpi,
            config=settings.template_matcher,
            budgets=settings.budgets,
            metrics=template_metrics,
        )
        if template_metrics.get("templateCount", 0) == 0:
            return _write_status(
                output_root,
                matched,
                status=SymbolsStatus.NO_VALID_LEGEND,
                anomaly_codes=(AnomalyCode.LEGEND_HAS_NO_VALID_TEMPLATES,),
            )
        detected = detect_page_open_set(
            document,
            page_number=page_number,
            output_root=output_root,
            legend_entries=legend_entries,
            known_instances=matched.symbol_instances,
            dpi=settings.dpi,
            config=settings.candidate_detector,
            budgets=settings.budgets,
        )
        enforce_budget(
            stage="s5",
            code=AnomalyCode.INSTANCE_BUDGET_EXCEEDED,
            observed=len(detected.symbol_instances),
            limit=settings.budgets.max_instances,
        )
        resolved = resolve_page_symbols(
            document,
            page_number=page_number,
            output_root=output_root,
            config=settings.resolver,
        )
        reviewed, _ = _review_resolved_page(
            resolved,
            output_root=output_root,
            settings=settings,
        )
        return reviewed
    except BudgetExceeded as exc:
        completed = locals().get("detected") or locals().get("matched") or s3_artifacts
        return _write_status(
            output_root,
            completed,
            status=SymbolsStatus.PARTIAL,
            anomaly_codes=(exc.code,),
        )
