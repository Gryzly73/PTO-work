"""H4b: rotation-invariant closed-profile matching for exploded DWG geometry."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
import hashlib
import json
import math
from typing import Any, Iterable

from .schema import Evidence, PageResult, Point, SymbolBinding, SymbolInstance, stable_id


_MAX_GEOMETRY_BINDINGS = 2000


@dataclass(frozen=True, slots=True)
class GeometryProfile:
    signature: str
    style_signature: str
    bbox: tuple[float, float, float, float]
    center: tuple[float, float]
    layer: str
    color: str
    lineweight: float
    thickness: float
    length: float


def _distance(first: tuple[float, float], second: tuple[float, float]) -> float:
    return math.hypot(second[0] - first[0], second[1] - first[1])


def _close(first: float, second: float, tolerance: float = 0.08) -> bool:
    return abs(first - second) <= max(first, second, 1e-9) * tolerance


def closed_profile(primitive: dict[str, Any]) -> GeometryProfile | None:
    """Return a style/width profile for a four-sided closed rectangle."""

    points = [
        (float(point[0]), float(point[1]))
        for point in primitive.get("points", ())
    ]
    if len(points) < 5 or _distance(points[0], points[-1]) > 1e-4:
        return None
    points = points[:-1]
    deduped = [points[0]]
    for point in points[1:]:
        if _distance(point, deduped[-1]) > 1e-6:
            deduped.append(point)
    if len(deduped) != 4:
        return None

    vectors = [
        (
            deduped[(index + 1) % 4][0] - deduped[index][0],
            deduped[(index + 1) % 4][1] - deduped[index][1],
        )
        for index in range(4)
    ]
    lengths = [math.hypot(x, y) for x, y in vectors]
    if min(lengths) < 0.25:
        return None
    if not _close(lengths[0], lengths[2]) or not _close(lengths[1], lengths[3]):
        return None
    for index in range(4):
        first = vectors[index]
        second = vectors[(index + 1) % 4]
        cosine = abs(
            (first[0] * second[0] + first[1] * second[1])
            / (lengths[index] * lengths[(index + 1) % 4])
        )
        if cosine > 0.08:
            return None

    pair_a = (lengths[0] + lengths[2]) / 2
    pair_b = (lengths[1] + lengths[3]) / 2
    thickness, length = sorted((pair_a, pair_b))
    if length / thickness < 3.0 or not 0.4 <= thickness <= 12.0:
        return None

    color = str(primitive.get("color") or "#000000").lower()
    lineweight = round(float(primitive.get("lw") or 0.0), 2)
    normalized_thickness = round(thickness, 1)
    payload = json.dumps(
        {
            "version": 1,
            "topology": "closed_rectangle",
            "color": color,
            "lineweight": lineweight,
            "thickness": normalized_thickness,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    style_payload = json.dumps(
        {
            "version": 1,
            "topology": "closed_rectangle",
            "color": color,
            "lineweight": lineweight,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    xs = [point[0] for point in deduped]
    ys = [point[1] for point in deduped]
    bbox = (min(xs), min(ys), max(xs), max(ys))
    return GeometryProfile(
        signature=f"linear-profile-v1:{hashlib.sha256(payload).hexdigest()}",
        style_signature=f"linear-style-v1:{hashlib.sha256(style_payload).hexdigest()}",
        bbox=bbox,
        center=((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2),
        layer=str(primitive.get("layer") or ""),
        color=color,
        lineweight=lineweight,
        thickness=normalized_thickness,
        length=round(length, 1),
    )


def _inside(
    point: tuple[float, float],
    bbox: tuple[float, float, float, float] | None,
) -> bool:
    return bool(
        bbox
        and bbox[0] <= point[0] < bbox[2]
        and bbox[1] <= point[1] < bbox[3]
    )


def resolve_geometry_profiles(
    result: PageResult,
    primitives: Iterable[dict[str, Any]],
) -> PageResult:
    """Add probable bindings for unique legend rectangle-style profiles."""

    anomalies = [
        code
        for code in result.anomaly_codes
        if code != "H4_EXACT_BLOCK_RESOLVER_ONLY"
    ]
    anomalies.append("H4_BLOCK_AND_GEOMETRY_RESOLVER_ONLY")

    profiles = [
        profile
        for primitive in primitives
        if (profile := closed_profile(primitive)) is not None
    ]
    profiles_by_entry: dict[str, list[GeometryProfile]] = {}
    updated_legends = []
    for entry in result.legend_entries:
        samples = [
            profile
            for profile in profiles
            if _inside(profile.center, entry.symbol_bbox)
        ]
        unique_signatures = tuple(sorted({profile.signature for profile in samples}))
        profiles_by_entry[entry.id] = samples
        updated_legends.append(
            replace(entry, geometry_signatures=unique_signatures)
        )
        if len(unique_signatures) > 1:
            anomalies.append("H4_COMPOSITE_GEOMETRY_LEGEND_ENTRY")

    sample_by_signature = {
        profile.signature: profile
        for samples in profiles_by_entry.values()
        for profile in samples
    }
    style_entries: dict[str, list[str]] = defaultdict(list)
    for entry in updated_legends:
        # Block exemplars already provide stronger evidence. Geometry matching
        # is reserved for exploded legend rows.
        if entry.reference_signatures or len(entry.geometry_signatures) != 1:
            continue
        sample = sample_by_signature[entry.geometry_signatures[0]]
        style_entries[sample.style_signature].append(entry.id)
    conflicts = {
        style_signature
        for style_signature, entry_ids in style_entries.items()
        if len(entry_ids) > 1
    }
    if conflicts:
        anomalies.append("H4_GEOMETRY_PROFILE_LEGEND_CONFLICT")
    resolvable = {
        style_signature: entry_ids[0]
        for style_signature, entry_ids in style_entries.items()
        if len(entry_ids) == 1
    }

    legend_boxes = [
        entry.bbox for entry in updated_legends if entry.bbox is not None
    ]
    existing_keys = {
        (
            instance.signature,
            tuple(round(value, 2) for value in instance.bbox)
            if instance.bbox
            else None,
        )
        for instance in result.symbol_instances
    }
    added_instances: list[SymbolInstance] = []
    added_bindings: list[SymbolBinding] = []
    seen: set[tuple[str, tuple[float, float, float, float]]] = set()
    for profile in profiles:
        entry_id = resolvable.get(profile.style_signature)
        if entry_id is None:
            continue
        if any(_inside(profile.center, bbox) for bbox in legend_boxes):
            continue
        rounded_bbox = tuple(round(value, 2) for value in profile.bbox)
        key = (profile.style_signature, rounded_bbox)
        if key in seen or key in existing_keys:
            continue
        seen.add(key)
        handle = stable_id(
            "GH",
            result.document_id,
            result.page,
            profile.signature,
            rounded_bbox,
            profile.layer,
        )
        instance_id = stable_id(
            "SI", result.document_id, result.page, "geometry", handle
        )
        symbol_type_id = stable_id("ST", result.document_id, entry_id)
        sample = next(
            item
            for item in profiles_by_entry[entry_id]
            if item.style_signature == profile.style_signature
        )
        width_ratio = profile.thickness / sample.thickness
        instance = SymbolInstance(
            id=instance_id,
            page=result.page,
            source_kind="dwg_closed_profile_candidate",
            status="probable",
            position=Point(profile.center[0], profile.center[1], "paper", "mm"),
            source_handle=handle,
            source_space="sheet_primitives",
            layer=profile.layer or "0",
            signature=profile.signature,
            role="field_candidate",
            bbox=profile.bbox,
            legend_entry_id=entry_id,
            symbol_type_id=symbol_type_id,
            confidence=0.85,
        )
        added_instances.append(instance)
        added_bindings.append(
            SymbolBinding(
                id=stable_id("SB", result.document_id, result.page, instance_id, entry_id),
                page=result.page,
                instance_id=instance_id,
                legend_entry_id=entry_id,
                status="probable",
                confidence=0.85,
                evidence=(
                    Evidence(
                        kind="normalized_geometry_style",
                        score=0.85,
                        source_ids=(entry_id,),
                        detail=(
                            "Closed rectangular primitive has the same rotation-"
                            "invariant color and lineweight style "
                            f"{profile.style_signature}; paper-width ratio to "
                            f"the legend sample is {width_ratio:.3f}, run length "
                            f"is {profile.length} mm."
                        ),
                    ),
                ),
            )
        )
        if len(added_bindings) >= _MAX_GEOMETRY_BINDINGS:
            anomalies.append("H4_GEOMETRY_BINDING_BUDGET_EXCEEDED")
            break

    if not resolvable:
        anomalies.append("H4_NO_UNIQUE_GEOMETRY_PROFILES")
    if not added_bindings:
        anomalies.append("H4_NO_GEOMETRY_PROFILE_BINDINGS")

    result.legend_entries = updated_legends
    result.symbol_instances.extend(added_instances)
    result.symbol_bindings.extend(added_bindings)
    result.anomaly_codes = sorted(set(anomalies))
    result.validate()
    return result
