import json
from pathlib import Path

import pytest

from symbols.artifacts import PageArtifacts, page_sidecar_dir, write_page_artifacts
from symbols.catalog import (
    DocumentSymbolCatalog,
    build_catalog,
    finalize_document_catalog,
    load_document_catalog,
)
from symbols.geometry import BBox
from symbols.review import (
    ReviewDecision,
    ReviewDecisionLog,
    append_review_decision,
    apply_review_decisions,
    load_review_decisions,
)
from symbols.schema import (
    ClassificationEvidence,
    LegendEntry,
    SchemaError,
    SymbolInstance,
    SymbolType,
)


def _page(page: int, name: str, suffix: str, *, unknown: bool = False) -> PageArtifacts:
    legend_id = f"LE-{suffix}"
    type_id = f"ST-{suffix}"
    instance_id = f"SI-{suffix}"
    legend = LegendEntry(
        id=legend_id,
        page=page,
        region_id=f"legend-{page}",
        bbox_pdf=BBox(10, 10, 100, 30),
        symbol_bbox_pdf=BBox(10, 10, 30, 30),
        text_bbox_pdf=BBox(35, 10, 100, 30),
        raw_crop=f"symbol_crops/{legend_id}.png",
        normalized_crop=None,
        source_kind="sheet_legend",
        source_document="document.pdf",
        status="extracted",
        name_raw=name,
        name_normalized=name.casefold(),
        confidence=1.0,
    )
    evidence = ClassificationEvidence(
        kind="document_template",
        score=0.98,
        source_entity_ids=(instance_id, legend_id),
        detail="template extracted from the current document",
    )
    instance = SymbolInstance(
        id=instance_id,
        page=page,
        bbox_pdf=BBox(120, 50, 140, 70),
        raw_crop=f"symbol_crops/{instance_id}.png",
        source_kinds=("document_template",),
        status="confirmed",
        symbol_type_id=type_id,
        legend_entry_id=legend_id,
        classification_confidence=0.98,
        classification_evidence=(evidence,),
    )
    symbol_type = SymbolType(
        id=type_id,
        representative_crop=f"symbol_crops/{type_id}.png",
        instance_ids=(instance_id,),
        status="confirmed",
        matched_legend_entry_id=legend_id,
        candidate_legend_entry_ids=(legend_id,),
        classification_evidence=(evidence,),
    )
    artifacts = PageArtifacts(
        page=page,
        legend_entries=[legend],
        symbol_types=[symbol_type],
        symbol_instances=[instance],
    )
    if unknown:
        unknown_instance = SymbolInstance(
            id=f"SI-unknown-{suffix}",
            page=page,
            bbox_pdf=BBox(160, 50, 180, 70),
            raw_crop=f"symbol_crops/SI-unknown-{suffix}.png",
            source_kinds=("connected_component",),
            status="unclassified",
            symbol_type_id=f"ST-unknown-{suffix}",
        )
        unknown_type = SymbolType(
            id=f"ST-unknown-{suffix}",
            representative_crop=f"symbol_crops/ST-unknown-{suffix}.png",
            instance_ids=(unknown_instance.id,),
            status="unclassified",
        )
        artifacts.symbol_types.append(unknown_type)
        artifacts.symbol_instances.append(unknown_instance)
        artifacts.unclassified_symbols.append(unknown_instance)
    return artifacts


def _decision(
    identifier: str,
    kind: str,
    targets: tuple[str, ...],
    payload=None,
) -> ReviewDecision:
    return ReviewDecision(
        id=identifier,
        kind=kind,
        target_entity_ids=targets,
        payload=payload or {},
        actor="reviewer@example.test",
        decided_at=f"2026-08-24T20:00:{identifier[-1]}Z",
        reason=f"human decision {identifier}",
    )


def test_catalog_is_confirmed_only_order_independent_and_byte_stable(
    tmp_path: Path,
) -> None:
    first = _page(1, "Клапан", "a", unknown=True)
    second = _page(2, "Клапан", "b")
    write_page_artifacts(tmp_path, second)
    write_page_artifacts(tmp_path, first)
    legend_before = (page_sidecar_dir(tmp_path, 1) / "legend_entries.json").read_bytes()

    catalog = finalize_document_catalog(tmp_path, "document.pdf")
    first_bytes = (tmp_path / "symbols" / "document_symbol_catalog.json").read_bytes()
    repeated = finalize_document_catalog(
        tmp_path, "document.pdf", pages=[2, 1, 2]
    )
    second_bytes = (tmp_path / "symbols" / "document_symbol_catalog.json").read_bytes()

    assert catalog == repeated
    assert first_bytes == second_bytes
    assert len(catalog.entries) == 1
    assert len(catalog.entries[0].variants) == 2
    assert {item.page for item in catalog.entries[0].variants} == {1, 2}
    assert all(item.provenance for item in catalog.entries[0].variants)
    assert {item.status for item in catalog.unresolved} == {"unclassified"}
    assert (page_sidecar_dir(tmp_path, 1) / "legend_entries.json").read_bytes() == legend_before
    assert load_document_catalog(tmp_path) == catalog
    assert DocumentSymbolCatalog.from_dict(catalog.to_dict()) == catalog


def test_review_log_is_atomic_ordered_and_rejects_duplicate_ids(
    tmp_path: Path,
) -> None:
    later = _decision("RD-2", "unresolved", ("ST-a",))
    earlier = ReviewDecision(
        id="RD-1",
        kind="not_a_symbol",
        target_entity_ids=("ST-b",),
        payload={},
        actor="reviewer@example.test",
        decided_at="2026-08-24T19:00:00Z",
        reason="false positive",
    )
    append_review_decision(tmp_path, later)
    append_review_decision(tmp_path, earlier)

    loaded = load_review_decisions(tmp_path)
    assert loaded.decisions == (earlier, later)
    assert ReviewDecisionLog.from_dict(
        json.loads(
            (tmp_path / "symbols" / "review_decisions.json").read_text(
                encoding="utf-8"
            )
        )
    ) == loaded
    with pytest.raises(SchemaError, match="duplicate"):
        append_review_decision(tmp_path, earlier)


def test_review_decisions_override_mapping_without_touching_page_sidecars(
    tmp_path: Path,
) -> None:
    artifacts = _page(1, "Клапан", "a", unknown=True)
    write_page_artifacts(tmp_path, artifacts)
    catalog = build_catalog({1: artifacts}, document="document.pdf")
    page_bytes = {
        path.name: path.read_bytes()
        for path in page_sidecar_dir(tmp_path, 1).glob("*.json")
    }
    entry = catalog.entries[0]
    variant = entry.variants[0]
    decisions = (
        _decision("RD-1", "unresolved", (variant.id,)),
        _decision(
            "RD-2",
            "confirm_mapping",
            ("ST-unknown-a",),
            {
                "catalogId": "PSC-HUMAN",
                "canonicalName": "Ручной знак",
                "page": 1,
                "legendEntryId": "LE-human",
                "symbolTypeIds": ["ST-unknown-a"],
                "instanceIds": ["SI-unknown-a"],
                "crop": "symbol_crops/human.png",
            },
        ),
    )

    reviewed = apply_review_decisions(catalog, decisions)

    assert [item.catalog_id for item in reviewed.entries] == ["PSC-HUMAN"]
    assert reviewed.entries[0].review_status == "human_confirmed"
    assert reviewed.entries[0].variants[0].provenance[0].kind == "human_review"
    assert {item.status for item in reviewed.unresolved} == {"unresolved"}
    assert reviewed.applied_review_decision_ids == ("RD-1", "RD-2")
    assert {
        path.name: path.read_bytes()
        for path in page_sidecar_dir(tmp_path, 1).glob("*.json")
    } == page_bytes


def test_review_split_merge_remove_and_not_a_symbol_are_catalog_only() -> None:
    catalog = build_catalog(
        {
            1: _page(1, "Клапан A", "a"),
            2: _page(2, "Клапан B", "b"),
        },
        document="document.pdf",
    )
    first, second = catalog.entries
    merged = apply_review_decisions(
        catalog,
        (
            _decision(
                "RD-3",
                "merge_type",
                (first.catalog_id, second.catalog_id),
                {"catalogId": "PSC-MERGED", "canonicalName": "Клапаны"},
            ),
        ),
    )
    assert len(merged.entries) == 1
    assert len(merged.entries[0].variants) == 2

    variant_ids = [item.id for item in merged.entries[0].variants]
    split = apply_review_decisions(
        merged,
        (
            _decision(
                "RD-4",
                "split_type",
                ("PSC-MERGED",),
                {
                    "groups": [
                        {
                            "catalogId": "PSC-A",
                            "canonicalName": "A",
                            "variantIds": [variant_ids[0]],
                        },
                        {
                            "catalogId": "PSC-B",
                            "canonicalName": "B",
                            "variantIds": [variant_ids[1]],
                        },
                    ]
                },
            ),
        ),
    )
    assert {item.catalog_id for item in split.entries} == {"PSC-A", "PSC-B"}

    removed = apply_review_decisions(
        split,
        (
            _decision("RD-5", "remove_mapping", ("PSC-A",)),
            _decision("RD-6", "not_a_symbol", ("PSC-B",)),
        ),
    )
    assert removed.entries == ()
    assert {item.status for item in removed.unresolved} == {
        "mapping_removed",
        "not_a_symbol",
    }
