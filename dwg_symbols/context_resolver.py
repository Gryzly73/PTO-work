"""H4c: conservative layer/legend context for unmatched closed profiles."""

from __future__ import annotations

from collections import defaultdict
import re
from typing import Any, Iterable

from .geometry_resolver import GeometryProfile, closed_profile
from .schema import Evidence, PageResult, Point, SymbolBinding, SymbolInstance, stable_id


_MAX_CONTEXT_BINDINGS = 1000
_GENERIC = {
    "стен",
    "перегородк",
    "панел",
    "лист",
    "гост",
    "материал",
    "конструкц",
}
_ENDINGS = (
    "иями",
    "ями",
    "ами",
    "ого",
    "его",
    "ов",
    "ев",
    "ей",
    "ый",
    "ий",
    "ая",
    "ое",
    "ые",
    "ы",
    "и",
    "а",
    "я",
)


def _stem(token: str) -> str:
    value = token.casefold().replace("ё", "е")
    for ending in _ENDINGS:
        if value.endswith(ending) and len(value) - len(ending) >= 4:
            return value[: -len(ending)]
    return value


def context_terms(text: str) -> set[str]:
    terms = {
        _stem(token)
        for token in re.findall(r"[0-9a-zа-яё]+", text.casefold())
        if len(token) >= 4
    }
    return {term for term in terms if term not in _GENERIC and len(term) >= 4}


def _inside(
    point: tuple[float, float],
    bbox: tuple[float, float, float, float] | None,
) -> bool:
    return bool(
        bbox
        and bbox[0] <= point[0] < bbox[2]
        and bbox[1] <= point[1] < bbox[3]
    )


def resolve_layer_context(
    result: PageResult,
    primitives: Iterable[dict[str, Any]],
) -> PageResult:
    """Add probable matches only for a unique lexical layer/legend overlap."""

    anomalies = [
        code
        for code in result.anomaly_codes
        if code
        not in {"H4_BLOCK_AND_GEOMETRY_RESOLVER_ONLY"}
    ]
    anomalies.append("H4_BLOCK_GEOMETRY_CONTEXT_RESOLVER_ONLY")

    profiles = [
        profile
        for primitive in primitives
        if (profile := closed_profile(primitive)) is not None
    ]
    legend_boxes = [
        entry.bbox for entry in result.legend_entries if entry.bbox is not None
    ]
    already_bound_entries = {
        binding.legend_entry_id for binding in result.symbol_bindings
    }
    unmatched_entries = [
        entry
        for entry in result.legend_entries
        if entry.id not in already_bound_entries
        and entry.label
        and len(entry.geometry_signatures) == 1
    ]
    terms_by_entry = {
        entry.id: context_terms(entry.label or "") for entry in unmatched_entries
    }

    existing_geometry = {
        tuple(round(value, 2) for value in instance.bbox)
        for instance in result.symbol_instances
        if instance.bbox is not None
        and instance.source_kind
        in {"dwg_closed_profile_candidate", "dwg_context_profile_candidate"}
    }
    candidates_by_entry: dict[str, list[GeometryProfile]] = defaultdict(list)
    ambiguous_profiles = 0
    for profile in profiles:
        if any(_inside(profile.center, bbox) for bbox in legend_boxes):
            continue
        rounded_bbox = tuple(round(value, 2) for value in profile.bbox)
        if rounded_bbox in existing_geometry:
            continue
        layer_terms = context_terms(profile.layer)
        scored = [
            (len(layer_terms & terms_by_entry[entry.id]), entry.id)
            for entry in unmatched_entries
        ]
        best = max((score for score, _ in scored), default=0)
        if best < 1:
            continue
        winners = [entry_id for score, entry_id in scored if score == best]
        if len(winners) != 1:
            ambiguous_profiles += 1
            continue
        candidates_by_entry[winners[0]].append(profile)

    added_instances: list[SymbolInstance] = []
    added_bindings: list[SymbolBinding] = []
    seen: set[tuple[str, tuple[float, float, float, float]]] = set()
    for entry in unmatched_entries:
        for profile in candidates_by_entry.get(entry.id, ()):
            rounded_bbox = tuple(round(value, 2) for value in profile.bbox)
            key = (entry.id, rounded_bbox)
            if key in seen:
                continue
            seen.add(key)
            overlap = sorted(context_terms(entry.label or "") & context_terms(profile.layer))
            handle = stable_id(
                "CH",
                result.document_id,
                result.page,
                entry.id,
                rounded_bbox,
                profile.layer,
            )
            instance_id = stable_id(
                "SI", result.document_id, result.page, "context", handle
            )
            symbol_type_id = stable_id("ST", result.document_id, entry.id)
            instance = SymbolInstance(
                id=instance_id,
                page=result.page,
                source_kind="dwg_context_profile_candidate",
                status="probable",
                position=Point(profile.center[0], profile.center[1], "paper", "mm"),
                source_handle=handle,
                source_space="sheet_primitives",
                layer=profile.layer or "0",
                signature=profile.signature,
                role="field_candidate",
                bbox=profile.bbox,
                legend_entry_id=entry.id,
                symbol_type_id=symbol_type_id,
                confidence=0.8,
            )
            added_instances.append(instance)
            added_bindings.append(
                SymbolBinding(
                    id=stable_id(
                        "SB", result.document_id, result.page, instance_id, entry.id
                    ),
                    page=result.page,
                    instance_id=instance_id,
                    legend_entry_id=entry.id,
                    status="probable",
                    confidence=0.8,
                    evidence=(
                        Evidence(
                            kind="layer_label_context",
                            score=0.8,
                            source_ids=(entry.id,),
                            detail=(
                                f"Closed profile layer {profile.layer!r} uniquely "
                                f"overlaps legend terms {overlap}; geometry style is "
                                f"{profile.style_signature}."
                            ),
                        ),
                    ),
                )
            )
            if len(added_bindings) >= _MAX_CONTEXT_BINDINGS:
                anomalies.append("H4_CONTEXT_BINDING_BUDGET_EXCEEDED")
                break
        if len(added_bindings) >= _MAX_CONTEXT_BINDINGS:
            break

    if ambiguous_profiles:
        anomalies.append("H4_LAYER_CONTEXT_AMBIGUOUS")
    if not added_bindings:
        anomalies.append("H4_NO_LAYER_CONTEXT_BINDINGS")

    result.symbol_instances.extend(added_instances)
    result.symbol_bindings.extend(added_bindings)
    result.anomaly_codes = sorted(set(anomalies))
    result.validate()
    return result
