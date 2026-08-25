"""Reproducible, non-public S3-S6 profiles for symbols development."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from time import perf_counter
from typing import Any, Mapping, Sequence

import pymupdf

from .artifacts import (
    PageArtifacts,
    atomic_write_json,
    load_page_artifacts,
)
from .candidate_detector import detect_page_open_set
from .gates import AnomalyCode, SymbolsStatus, enforce_budget
from .legend_layout import LegendRegionResult
from .pipeline import (
    SymbolsPipelineConfig,
    _extract_page_legend,
    _review_resolved_page,
    _write_status,
)
from .resolver import resolve_page_symbols
from .template_matcher import match_page_templates


BASELINE_VERSION = 1


def _elapsed_ms(started: float) -> float:
    return round((perf_counter() - started) * 1000.0, 3)


def _histogram(values: Sequence[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _bbox_metrics(bbox: Any) -> dict[str, Any]:
    return {
        "bboxPdf": [round(value, 3) for value in bbox.to_list()],
        "width": round(bbox.width, 3),
        "height": round(bbox.height, 3),
        "area": round(bbox.width * bbox.height, 3),
    }


def _row_metrics(artifacts: PageArtifacts) -> list[dict[str, Any]]:
    return [
        {
            "id": entry.id,
            "status": entry.status,
            "sourceKind": entry.source_kind,
            "textLength": len(entry.name_raw or ""),
            "row": _bbox_metrics(entry.bbox_pdf),
            "symbol": _bbox_metrics(entry.symbol_bbox_pdf),
        }
        for entry in sorted(artifacts.legend_entries, key=lambda item: item.id)
    ]


def _entity_ids(artifacts: PageArtifacts) -> dict[str, list[str]]:
    return {
        "legendEntries": sorted(item.id for item in artifacts.legend_entries),
        "symbolCandidates": sorted(item.id for item in artifacts.symbol_candidates),
        "symbolTypes": sorted(item.id for item in artifacts.symbol_types),
        "symbolInstances": sorted(item.id for item in artifacts.symbol_instances),
        "conflicts": sorted(item.id for item in artifacts.conflicts),
    }


def _summary(artifacts: PageArtifacts) -> dict[str, Any]:
    if artifacts.summary is not None:
        return artifacts.summary.to_dict()
    return {
        "page": artifacts.page,
        "legendEntryCount": len(artifacts.legend_entries),
        "symbolTypeCount": len(artifacts.symbol_types),
        "symbolInstanceCount": len(artifacts.symbol_instances),
        "unclassifiedCount": len(artifacts.unclassified_symbols),
        "unmatchedLegendEntryCount": len(artifacts.unmatched_legend_entries),
        "conflictCount": len(artifacts.conflicts),
        "status": "complete",
    }


def _candidate_sources(artifacts: PageArtifacts) -> dict[str, int]:
    return _histogram(
        [source for item in artifacts.symbol_candidates for source in item.source_kinds]
    )


def _config_fingerprint(config: SymbolsPipelineConfig) -> str:
    config_payload = asdict(config)
    if not config.legend_vlm.enabled:
        # The disabled H5 path is intentionally byte-compatible with H4.
        config_payload.pop("legend_vlm", None)
    if not config.review_vlm.enabled:
        config_payload.pop("review_vlm", None)
    payload = json.dumps(
        config_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def deterministic_projection(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Return the stable portion used to compare repeated runs."""

    projected = json.loads(json.dumps(profile))
    for stage in projected.get("stages", {}).values():
        if isinstance(stage, dict):
            for key in tuple(stage):
                if key == "elapsedMs" or key.endswith("ElapsedMs"):
                    stage.pop(key)
    projected.pop("reproducibilityDigest", None)
    return projected


def _with_digest(profile: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(
        deterministic_projection(profile),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    profile["reproducibilityDigest"] = hashlib.sha256(encoded).hexdigest()
    return profile


def write_baseline(path: str | Path, profile: Mapping[str, Any]) -> Path:
    destination = Path(path)
    atomic_write_json(destination, dict(profile))
    return destination


def run_profiled_page(
    document: str | Path,
    *,
    page_number: int,
    output_root: str | Path,
    fixture_id: str,
    config: SymbolsPipelineConfig | None = None,
    expectation: Mapping[str, str] | None = None,
) -> tuple[PageArtifacts, dict[str, Any]]:
    """Run S3-S6 and return a separate development profile."""

    settings = config or SymbolsPipelineConfig()
    with pymupdf.open(Path(document)) as opened:
        if page_number > opened.page_count:
            raise ValueError(
                f"page_number {page_number} exceeds document page count "
                f"{opened.page_count}"
            )
        page = opened[page_number - 1]
        page_area = float(page.rect.width) * float(page.rect.height)

    started = perf_counter()
    legend_result, legend_entries = _extract_page_legend(
        document,
        page_number=page_number,
        output_root=output_root,
        settings=settings,
    )
    s3_elapsed = _elapsed_ms(started)
    s3_artifacts = PageArtifacts(page=page_number, legend_entries=legend_entries)
    if legend_result.status != "found" or not legend_entries:
        anomaly = (
            AnomalyCode.LEGEND_NOT_FOUND
            if legend_result.status != "found"
            else AnomalyCode.LEGEND_HAS_NO_VALID_ROWS
        )
        resolved = _write_status(
            output_root,
            s3_artifacts,
            status=SymbolsStatus.NO_VALID_LEGEND,
            anomaly_codes=(anomaly,),
        )
        empty_counts: dict[str, int] = {}
        profile = {
            "baselineVersion": BASELINE_VERSION,
            "fixtureId": fixture_id,
            "page": page_number,
            "profileSource": "pipeline_run",
            "clusteringBackend": settings.candidate_detector.clustering_backend,
            "configFingerprint": _config_fingerprint(settings),
            "stages": {
                "s3": {
                    "elapsedMs": s3_elapsed,
                    **legend_result.to_dict(),
                    "regionAreaRatio": None,
                    "legendEntryCount": len(legend_entries),
                    "rows": _row_metrics(s3_artifacts),
                },
                "s4": {"elapsedMs": 0.0, "skipped": True, **empty_counts},
                "s5": {"elapsedMs": 0.0, "skipped": True, **empty_counts},
                "s6": {"elapsedMs": 0.0, "skipped": True, **empty_counts},
            },
            "entityIds": _entity_ids(resolved),
            "summary": _summary(resolved),
        }
        if expectation is not None:
            profile["expectation"] = dict(expectation)
        return resolved, _with_digest(profile)

    enforce_budget(
        stage="s3",
        code=AnomalyCode.LEGEND_ENTRY_BUDGET_EXCEEDED,
        observed=len(legend_entries),
        limit=settings.budgets.max_legend_entries,
    )

    s4_counts: dict[str, int] = {}
    started = perf_counter()
    matched = match_page_templates(
        document,
        page_number=page_number,
        output_root=output_root,
        legend_entries=legend_entries,
        dpi=settings.dpi,
        config=settings.template_matcher,
        budgets=settings.budgets,
        metrics=s4_counts,
    )
    s4_elapsed = _elapsed_ms(started)
    if s4_counts.get("templateCount", 0) == 0:
        resolved = _write_status(
            output_root,
            matched,
            status=SymbolsStatus.NO_VALID_LEGEND,
            anomaly_codes=(AnomalyCode.LEGEND_HAS_NO_VALID_TEMPLATES,),
        )
        region = legend_result.bbox_pdf
        profile = {
            "baselineVersion": BASELINE_VERSION,
            "fixtureId": fixture_id,
            "page": page_number,
            "profileSource": "pipeline_run",
            "clusteringBackend": settings.candidate_detector.clustering_backend,
            "configFingerprint": _config_fingerprint(settings),
            "stages": {
                "s3": {
                    "elapsedMs": s3_elapsed,
                    **legend_result.to_dict(),
                    "regionAreaRatio": (
                        round(region.width * region.height / page_area, 8)
                        if region is not None and page_area
                        else None
                    ),
                    "legendEntryCount": len(legend_entries),
                    "rows": _row_metrics(s3_artifacts),
                },
                "s4": {
                    "elapsedMs": s4_elapsed,
                    **s4_counts,
                    "instancesByStatus": {},
                    "typesByStatus": {},
                },
                "s5": {"elapsedMs": 0.0, "skipped": True},
                "s6": {"elapsedMs": 0.0, "skipped": True},
            },
            "entityIds": _entity_ids(resolved),
            "summary": _summary(resolved),
        }
        if expectation is not None:
            profile["expectation"] = dict(expectation)
        return resolved, _with_digest(profile)

    s5_counts: dict[str, int] = {}
    started = perf_counter()
    detected = detect_page_open_set(
        document,
        page_number=page_number,
        output_root=output_root,
        legend_entries=legend_entries,
        known_instances=matched.symbol_instances,
        dpi=settings.dpi,
        config=settings.candidate_detector,
        budgets=settings.budgets,
        metrics=s5_counts,
    )
    s5_elapsed = _elapsed_ms(started)

    started = perf_counter()
    resolved = resolve_page_symbols(
        document,
        page_number=page_number,
        output_root=output_root,
        config=settings.resolver,
    )
    s6_elapsed = _elapsed_ms(started)
    h6_started = perf_counter()
    reviewed, h6_counts = _review_resolved_page(
        resolved,
        output_root=output_root,
        settings=settings,
    )
    h6_elapsed = _elapsed_ms(h6_started)

    region = legend_result.bbox_pdf
    profile: dict[str, Any] = {
        "baselineVersion": BASELINE_VERSION,
        "fixtureId": fixture_id,
        "page": page_number,
        "profileSource": "pipeline_run",
        "clusteringBackend": settings.candidate_detector.clustering_backend,
        "configFingerprint": _config_fingerprint(settings),
        "stages": {
            "s3": {
                "elapsedMs": s3_elapsed,
                **legend_result.to_dict(),
                "regionAreaRatio": (
                    round(region.width * region.height / page_area, 8)
                    if region is not None and page_area
                    else None
                ),
                "legendEntryCount": len(legend_entries),
                "rows": _row_metrics(s3_artifacts),
            },
            "s4": {
                "elapsedMs": s4_elapsed,
                **s4_counts,
                "instancesByStatus": _histogram(
                    [item.status for item in matched.symbol_instances]
                ),
                "typesByStatus": _histogram(
                    [item.status for item in matched.symbol_types]
                ),
            },
            "s5": {
                "elapsedMs": s5_elapsed,
                **s5_counts,
                "candidatesBySource": _candidate_sources(detected),
                "candidatesByStatus": _histogram(
                    [item.status for item in detected.symbol_candidates]
                ),
                "instancesByStatus": _histogram(
                    [item.status for item in detected.symbol_instances]
                ),
            },
            "s6": {
                "elapsedMs": s6_elapsed,
                "instancesByStatus": _histogram(
                    [item.status for item in resolved.symbol_instances]
                ),
                "typesByStatus": _histogram(
                    [item.status for item in resolved.symbol_types]
                ),
                "conflictsByKind": _histogram(
                    [item.kind for item in resolved.conflicts]
                ),
                "unmatchedLegendEntryCount": len(
                    resolved.unmatched_legend_entries
                ),
            },
        },
        "entityIds": _entity_ids(reviewed),
        "summary": _summary(reviewed),
    }
    if settings.review_vlm.enabled:
        profile["stages"]["h6"] = {
            "elapsedMs": h6_elapsed,
            **h6_counts,
            "instancesByStatus": _histogram(
                [item.status for item in reviewed.symbol_instances]
            ),
            "typesByStatus": _histogram(
                [item.status for item in reviewed.symbol_types]
            ),
        }
    if expectation is not None:
        profile["expectation"] = dict(expectation)
    return reviewed, _with_digest(profile)


def profile_existing_artifacts(
    output_root: str | Path,
    *,
    page_number: int,
    fixture_id: str,
    expectation: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Create a deterministic snapshot when the customer PDF is unavailable."""

    artifacts = load_page_artifacts(output_root, page_number)
    profile: dict[str, Any] = {
        "baselineVersion": BASELINE_VERSION,
        "fixtureId": fixture_id,
        "page": page_number,
        "profileSource": "existing_sidecars",
        "clusteringBackend": "unknown",
        "configFingerprint": "legacy-unrecorded",
        "stages": {
            "s3": {
                "elapsedMs": None,
                "legendStatus": (
                    "found" if artifacts.legend_entries else "not_found"
                ),
                "legendEntryCount": len(artifacts.legend_entries),
                "rows": _row_metrics(artifacts),
            },
            "s4": {
                "elapsedMs": None,
                "templateCount": len(artifacts.legend_entries),
                "instancesByStatus": _histogram(
                    [
                        item.status
                        for item in artifacts.symbol_instances
                        if item.status != "unclassified"
                    ]
                ),
            },
            "s5": {
                "elapsedMs": None,
                "candidateCount": len(artifacts.symbol_candidates),
                "candidatesBySource": _candidate_sources(artifacts),
                "candidatesByStatus": _histogram(
                    [item.status for item in artifacts.symbol_candidates]
                ),
                "unclassifiedCount": len(artifacts.unclassified_symbols),
            },
            "s6": {
                "elapsedMs": None,
                "instancesByStatus": _histogram(
                    [item.status for item in artifacts.symbol_instances]
                ),
                "typesByStatus": _histogram(
                    [item.status for item in artifacts.symbol_types]
                ),
                "conflictsByKind": _histogram(
                    [item.kind for item in artifacts.conflicts]
                ),
                "unmatchedLegendEntryCount": len(
                    artifacts.unmatched_legend_entries
                ),
            },
        },
        "entityIds": _entity_ids(artifacts),
        "summary": _summary(artifacts),
    }
    if expectation is not None:
        profile["expectation"] = dict(expectation)
    return _with_digest(profile)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Profile symbols stages S3-S6.")
    parser.add_argument("--document", type=Path)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--page", type=int, required=True)
    parser.add_argument("--fixture-id", required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--from-sidecars", action="store_true")
    args = parser.parse_args(argv)

    if args.from_sidecars:
        profile = profile_existing_artifacts(
            args.output_root,
            page_number=args.page,
            fixture_id=args.fixture_id,
        )
    else:
        if args.document is None:
            parser.error("--document is required unless --from-sidecars is used")
        _, profile = run_profiled_page(
            args.document,
            page_number=args.page,
            output_root=args.output_root,
            fixture_id=args.fixture_id,
        )
    write_baseline(args.profile, profile)
    print(json.dumps(profile, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
