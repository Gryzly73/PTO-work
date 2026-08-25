"""Deterministic clustering of independent symbol candidates."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Literal, Sequence

from .schema import SymbolCandidate, SymbolInstance, SymbolType, stable_id
from .visual_signature import signature_similarity


@dataclass(frozen=True, slots=True)
class CandidateCluster:
    symbol_type: SymbolType
    instances: tuple[SymbolInstance, ...]
    candidate_ids: tuple[str, ...]


@dataclass(slots=True)
class _BKNode:
    """One node in an exact Hamming-distance search index."""

    value: int
    group_index: int
    children: dict[int, "_BKNode"]


def _query_hamming(
    node: _BKNode,
    value: int,
    radius: int,
    matches: list[int] | None = None,
) -> list[int]:
    if matches is None:
        matches = []
    distance = (node.value ^ value).bit_count()
    if distance <= radius:
        matches.append(node.group_index)
    lower = distance - radius
    upper = distance + radius
    for edge, child in node.children.items():
        if lower <= edge <= upper:
            _query_hamming(child, value, radius, matches)
    return matches


def _insert_hamming(node: _BKNode, value: int, group_index: int) -> None:
    while True:
        distance = (node.value ^ value).bit_count()
        child = node.children.get(distance)
        if child is None:
            node.children[distance] = _BKNode(value, group_index, {})
            return
        node = child


def _candidate_key(candidate: SymbolCandidate) -> tuple[object, ...]:
    signature = candidate.visual_signature
    return (
        candidate.page,
        candidate.bbox_pdf.y0,
        candidate.bbox_pdf.x0,
        candidate.bbox_pdf.y1,
        candidate.bbox_pdf.x1,
        signature.value if signature else "",
        candidate.id,
    )


def cluster_candidates(
    candidates: Sequence[SymbolCandidate],
    *,
    similarity_threshold: float = 0.9,
    minimum_repeats: int = 2,
    backend: Literal["brute_force", "bk_tree"] = "brute_force",
) -> list[CandidateCluster]:
    """Create unclassified types only for visually repeated candidates.

    ``brute_force`` is the correctness reference and intentionally remains the
    default until the indexed implementation has equivalence coverage.
    """

    if (
        isinstance(similarity_threshold, bool)
        or not isinstance(similarity_threshold, (int, float))
        or not 0 <= similarity_threshold <= 1
    ):
        raise ValueError("similarity_threshold must be between 0 and 1")
    if (
        isinstance(minimum_repeats, bool)
        or not isinstance(minimum_repeats, int)
        or minimum_repeats < 1
    ):
        raise ValueError("minimum_repeats must be a positive integer")
    if backend not in {"brute_force", "bk_tree"}:
        raise ValueError("backend must be 'brute_force' or 'bk_tree'")

    ordered = sorted(
        (candidate for candidate in candidates if candidate.visual_signature is not None),
        key=_candidate_key,
    )
    if not ordered:
        return []
    pages = {candidate.page for candidate in ordered}
    if len(pages) != 1:
        raise ValueError("candidates in one clustering call must belong to one page")

    signature_groups: dict[tuple[str, int, str], list[SymbolCandidate]] = {}
    for candidate in ordered:
        signature = candidate.visual_signature
        assert signature is not None
        signature_groups.setdefault(
            (signature.method, signature.version, signature.value),
            [],
        ).append(candidate)
    grouped_signatures = sorted(
        signature_groups.values(),
        key=lambda group: _candidate_key(group[0]),
    )
    parent = list(range(len(grouped_signatures)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = root(left), root(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    compatible: dict[tuple[str, int], list[int]] = {}
    for index, members in enumerate(grouped_signatures):
        signature = members[0].visual_signature
        assert signature is not None
        compatible.setdefault((signature.method, signature.version), []).append(index)

    for indices in compatible.values():
        signature_widths = {
            len(grouped_signatures[index][0].visual_signature.value) * 4
            for index in indices
        }
        hex_values: dict[int, int] = {}
        for index in indices:
            signature = grouped_signatures[index][0].visual_signature
            assert signature is not None
            try:
                hex_values[index] = int(signature.value, 16)
            except ValueError:
                break
        # A signature version should have a fixed width. Keep unusual legacy
        # or malformed data on the reference path because its normalized
        # Hamming denominator differs for each pair.
        use_reference = (
            backend == "brute_force"
            or len(signature_widths) != 1
            or len(hex_values) != len(indices)
        )
        if use_reference:
            for offset, index in enumerate(indices):
                signature = grouped_signatures[index][0].visual_signature
                assert signature is not None
                for other in indices[:offset]:
                    other_signature = grouped_signatures[other][0].visual_signature
                    assert other_signature is not None
                    if (
                        signature_similarity(signature, other_signature)
                        >= similarity_threshold
                    ):
                        union(index, other)
            continue

        signature_width = next(iter(signature_widths))
        radius = math.floor(
            (1.0 - similarity_threshold) * signature_width + 1e-12
        )
        tree: _BKNode | None = None
        for index in indices:
            value = hex_values[index]
            if tree is None:
                tree = _BKNode(value, index, {})
                continue
            for other in _query_hamming(tree, value, radius):
                # The BK query already applied the exact integer Hamming gate
                # for this fixed-width signature version.
                union(index, other)
            _insert_hamming(tree, value, index)

    grouped: dict[int, list[SymbolCandidate]] = {}
    for index, members in enumerate(grouped_signatures):
        grouped.setdefault(root(index), []).extend(members)

    result: list[CandidateCluster] = []
    page = next(iter(pages))
    for members in sorted(grouped.values(), key=lambda group: _candidate_key(group[0])):
        if len(members) < minimum_repeats:
            continue
        member_ids = tuple(candidate.id for candidate in members)
        type_id = stable_id("ST", page, "open-set", sorted(member_ids))
        instances = tuple(
            SymbolInstance(
                id=stable_id("SI", page, "open-set", candidate.id),
                page=page,
                symbol_type_id=type_id,
                legend_entry_id=None,
                bbox_pdf=candidate.bbox_pdf,
                raw_crop=candidate.raw_crop,
                normalized_crop=candidate.normalized_crop,
                source_kinds=candidate.source_kinds,
                status="unclassified",
                geometry_confidence=candidate.confidence,
                classification_confidence=0.0,
            )
            for candidate in members
        )
        representative = max(
            members,
            key=lambda candidate: (candidate.confidence, tuple(-v for v in _candidate_key(candidate)[1:5])),
        )
        result.append(
            CandidateCluster(
                symbol_type=SymbolType(
                    id=type_id,
                    representative_crop=(
                        representative.normalized_crop or representative.raw_crop
                    ),
                    instance_ids=tuple(instance.id for instance in instances),
                    status="unclassified",
                    visual_signature=representative.visual_signature,
                ),
                instances=instances,
                candidate_ids=member_ids,
            )
        )
    return result
