"""Resolve symbol types against legend rows using current-document evidence only."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from pathlib import Path
from typing import Mapping, Sequence

from PIL import Image

from .artifacts import (
    PageArtifacts,
    load_page_artifacts,
    page_sidecar_dir,
    write_page_artifacts,
)
from .matcher import (
    InstanceContext,
    MatcherConfig,
    extract_page_context,
    score_legend_matches,
)
from .schema import (
    ClassificationEvidence,
    LegendEntry,
    SymbolConflict,
    SymbolInstance,
    SymbolType,
    VisualSignature,
    stable_id,
)
from .visual_signature import compute_visual_signature


@dataclass(frozen=True, slots=True)
class ResolverConfig:
    matcher: MatcherConfig = MatcherConfig()
    confirmed_score: float = 0.75
    probable_score: float = 0.62
    minimum_confirmed_visual: float = 0.88
    minimum_semantic_evidence: float = 0.5
    conflict_margin: float = 0.06

    def __post_init__(self) -> None:
        for name in (
            "confirmed_score",
            "probable_score",
            "minimum_confirmed_visual",
            "minimum_semantic_evidence",
            "conflict_margin",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 <= value <= 1
            ):
                raise ValueError(f"{name} must be between 0 and 1")
        if self.probable_score > self.confirmed_score:
            raise ValueError("probable_score cannot exceed confirmed_score")


def _signature_from_crop(path: Path) -> VisualSignature | None:
    try:
        with Image.open(path) as image:
            return compute_visual_signature(image.copy())
    except (OSError, ValueError):
        return None


def _artifact_crop(page_dir: Path, relative: str | None) -> Path | None:
    if not relative:
        return None
    path = page_dir / Path(relative.replace("\\", "/"))
    try:
        path.resolve().relative_to(page_dir.resolve())
    except ValueError:
        return None
    return path


def load_document_signatures(
    page_dir: str | Path,
    legend_entries: Sequence[LegendEntry],
    symbol_types: Sequence[SymbolType],
) -> tuple[dict[str, VisualSignature], dict[str, VisualSignature]]:
    """Load visual evidence exclusively from the page-sidecar crops."""

    root = Path(page_dir)
    legend_signatures: dict[str, VisualSignature] = {}
    for entry in legend_entries:
        crop = _artifact_crop(root, entry.normalized_crop or entry.raw_crop)
        signature = _signature_from_crop(crop) if crop is not None else None
        if signature is not None:
            legend_signatures[entry.id] = signature

    type_signatures: dict[str, VisualSignature] = {}
    for symbol_type in symbol_types:
        signature = symbol_type.visual_signature
        if signature is None:
            crop = _artifact_crop(root, symbol_type.representative_crop)
            signature = _signature_from_crop(crop) if crop is not None else None
        if signature is not None:
            type_signatures[symbol_type.id] = signature
    return legend_signatures, type_signatures


def _direct_template_evidence(
    symbol_type: SymbolType,
    instances: Sequence[SymbolInstance],
) -> tuple[ClassificationEvidence, ...]:
    evidence: list[ClassificationEvidence] = []
    for instance in sorted(instances, key=lambda item: item.id):
        if (
            instance.legend_entry_id == symbol_type.matched_legend_entry_id
            and "document_template" in instance.source_kinds
        ):
            evidence.append(
                ClassificationEvidence(
                    kind="document_template",
                    score=instance.classification_confidence,
                    source_entity_ids=(instance.id, instance.legend_entry_id),
                    detail="instance matched a template extracted from this document",
                )
            )
    return tuple(evidence)


_INDEPENDENT_EVIDENCE_KINDS = {
    "document_position",
    "document_nearby_label",
    "document_marker",
}


def _has_independent_evidence(
    evidence: Sequence[ClassificationEvidence],
    *,
    minimum_score: float,
) -> bool:
    return any(
        item.kind in _INDEPENDENT_EVIDENCE_KINDS
        and item.score >= minimum_score
        for item in evidence
    )


def _merge_evidence(
    *groups: Sequence[ClassificationEvidence],
) -> tuple[ClassificationEvidence, ...]:
    result: list[ClassificationEvidence] = []
    seen: set[tuple[str, tuple[str, ...], str]] = set()
    for group in groups:
        for item in group:
            key = (item.kind, item.source_entity_ids, item.detail)
            if key not in seen:
                seen.add(key)
                result.append(item)
    return tuple(result)


def resolve_artifacts(
    artifacts: PageArtifacts,
    *,
    contexts: Mapping[str, InstanceContext],
    legend_signatures: Mapping[str, VisualSignature],
    type_signatures: Mapping[str, VisualSignature],
    config: ResolverConfig | None = None,
) -> PageArtifacts:
    """Resolve one page deterministically without consulting external knowledge."""

    settings = config or ResolverConfig()
    entries = sorted(artifacts.legend_entries, key=lambda item: item.id)
    matchable_entries = [
        entry for entry in entries if entry.status != "text_unreadable"
    ]
    entry_ids = {entry.id for entry in matchable_entries}
    instances_by_type: dict[str, list[SymbolInstance]] = {}
    for instance in artifacts.symbol_instances:
        if instance.symbol_type_id is not None:
            instances_by_type.setdefault(instance.symbol_type_id, []).append(instance)

    resolved_types: list[SymbolType] = []
    instance_updates: dict[str, SymbolInstance] = {}
    conflicts: list[SymbolConflict] = []
    confirmed_entry_ids: set[str] = set()

    for symbol_type in sorted(artifacts.symbol_types, key=lambda item: item.id):
        members = sorted(instances_by_type.get(symbol_type.id, []), key=lambda item: item.id)
        member_contexts = [
            contexts.get(instance.id, InstanceContext(instance.id))
            for instance in members
        ]
        signature = type_signatures.get(symbol_type.id, symbol_type.visual_signature)
        matches = score_legend_matches(
            symbol_type_id=symbol_type.id,
            type_signature=signature,
            legend_entries=matchable_entries,
            legend_signatures=legend_signatures,
            contexts=member_contexts,
            config=settings.matcher,
        )
        direct_evidence = _direct_template_evidence(symbol_type, members)
        current_id = symbol_type.matched_legend_entry_id
        current_match = next(
            (item for item in matches if item.legend_entry_id == current_id),
            None,
        )
        winner = matches[0] if matches else None
        winner_has_independent_evidence = (
            winner is not None
            and _has_independent_evidence(
                winner.evidence,
                minimum_score=settings.minimum_semantic_evidence,
            )
        )
        ambiguous = (
            len(matches) > 1
            and matches[0].score >= settings.probable_score
            and matches[1].visual_similarity >= settings.minimum_confirmed_visual
            and matches[0].score - matches[1].score <= settings.conflict_margin
        )

        status = symbol_type.status
        matched_id = current_id
        candidates = symbol_type.candidate_legend_entry_ids
        evidence = direct_evidence or symbol_type.classification_evidence
        confidence = max(
            (instance.classification_confidence for instance in members),
            default=0.0,
        )

        if current_id in entry_ids and direct_evidence and not ambiguous:
            matched_id = current_id
            candidates = (current_id,)
            evidence = _merge_evidence(
                direct_evidence,
                current_match.evidence if current_match is not None else (),
            )
            confidence = max(
                confidence,
                current_match.score if current_match is not None else 0.0,
            )
            if (
                current_match is not None
                and current_match.score >= settings.confirmed_score
                and current_match.visual_similarity
                >= settings.minimum_confirmed_visual
                and _has_independent_evidence(
                    current_match.evidence,
                    minimum_score=settings.minimum_semantic_evidence,
                )
            ):
                status = "confirmed"
                confirmed_entry_ids.add(current_id)
            else:
                status = "probable"
        elif ambiguous:
            conflict_matches = tuple(
                item
                for item in matches
                if matches[0].score - item.score <= settings.conflict_margin
                and item.visual_similarity >= settings.minimum_confirmed_visual
            )
            candidates = tuple(item.legend_entry_id for item in conflict_matches)
            status = "conflicting"
            matched_id = None
            evidence = tuple(
                evidence_item
                for item in conflict_matches
                for evidence_item in item.evidence
            )
            confidence = matches[0].score
            conflicts.append(
                SymbolConflict(
                    id=stable_id(
                        "CF",
                        artifacts.page,
                        "ambiguous-legend-binding",
                        symbol_type.id,
                        candidates,
                    ),
                    page=artifacts.page,
                    kind="ambiguous_legend_binding",
                    entity_ids=(symbol_type.id, *(item.id for item in members)),
                    candidate_legend_entry_ids=candidates,
                    reason=(
                        "multiple current-document legend rows have equivalent "
                        "visual/context evidence"
                    ),
                )
            )
        elif matches and (
            matches[0].score >= settings.confirmed_score
            and matches[0].visual_similarity >= settings.minimum_confirmed_visual
            and winner_has_independent_evidence
        ):
            winner = matches[0]
            status = "confirmed"
            matched_id = winner.legend_entry_id
            candidates = (matched_id,)
            evidence = winner.evidence
            confidence = winner.score
            confirmed_entry_ids.add(matched_id)
        elif matches and matches[0].score >= settings.probable_score:
            winner = matches[0]
            status = "probable"
            matched_id = winner.legend_entry_id
            candidates = tuple(item.legend_entry_id for item in matches[:2])
            evidence = winner.evidence
            confidence = winner.score
        elif matches:
            status = "unresolved"
            matched_id = None
            candidates = tuple(item.legend_entry_id for item in matches[:2])
            evidence = matches[0].evidence
            confidence = matches[0].score
        else:
            status = "unclassified"
            matched_id = None
            candidates = ()
            evidence = ()
            confidence = 0.0

        resolved_types.append(
            replace(
                symbol_type,
                matched_legend_entry_id=matched_id,
                candidate_legend_entry_ids=candidates,
                status=status,
                visual_signature=signature,
                classification_evidence=evidence,
            )
        )
        for instance in members:
            context = contexts.get(instance.id, InstanceContext(instance.id))
            instance_updates[instance.id] = replace(
                instance,
                legend_entry_id=matched_id,
                nearby_labels=context.nearby_labels,
                connected_line_ids=context.connected_line_ids,
                status=status,
                classification_confidence=confidence,
                classification_evidence=evidence,
            )

    resolved_instances = [
        instance_updates.get(instance.id, instance)
        for instance in sorted(artifacts.symbol_instances, key=lambda item: item.id)
    ]
    unclassified = [
        instance
        for instance in resolved_instances
        if instance.status in {"unclassified", "unresolved"}
    ]
    unmatched = [
        replace(entry, status="unmatched")
        for entry in entries
        if entry.status != "text_unreadable"
        and entry.id not in confirmed_entry_ids
    ]
    return PageArtifacts(
        page=artifacts.page,
        legend_entries=entries,
        symbol_candidates=sorted(artifacts.symbol_candidates, key=lambda item: item.id),
        symbol_types=resolved_types,
        symbol_instances=resolved_instances,
        unclassified_symbols=unclassified,
        unmatched_legend_entries=unmatched,
        conflicts=conflicts,
    )


def resolve_page_symbols(
    document: str | Path,
    *,
    page_number: int,
    output_root: str | Path,
    config: ResolverConfig | None = None,
) -> PageArtifacts:
    """Load page sidecars, add context, resolve bindings, and persist S6 outputs."""

    settings = config or ResolverConfig()
    artifacts = load_page_artifacts(output_root, page_number)
    contexts = extract_page_context(
        document,
        page_number=page_number,
        instances=artifacts.symbol_instances,
        config=settings.matcher,
    )
    legend_signatures, type_signatures = load_document_signatures(
        page_sidecar_dir(output_root, page_number),
        artifacts.legend_entries,
        artifacts.symbol_types,
    )
    resolved = resolve_artifacts(
        artifacts,
        contexts=contexts,
        legend_signatures=legend_signatures,
        type_signatures=type_signatures,
        config=settings,
    )
    write_page_artifacts(output_root, resolved)
    return resolved
