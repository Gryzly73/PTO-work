from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageDraw

from symbols.artifacts import PageArtifacts, load_page_artifacts
from symbols.candidate_detector import detect_page_open_set
from symbols.geometry import BBox
from symbols.legend_layout import extract_page_legend
from symbols.matcher import InstanceContext
from symbols.resolver import resolve_artifacts, resolve_page_symbols
from symbols.schema import LegendEntry, SymbolInstance, SymbolType
from symbols.score_gt import score_documents
from symbols.template_matcher import match_page_templates
from symbols.visual_signature import compute_visual_signature


FIXTURES = Path(__file__).parents[1] / "symbols" / "fixtures"
SYNTHETIC_VISUAL = FIXTURES / "synthetic_legend.svg"


def _signature():
    image = Image.new("L", (64, 64), 255)
    draw = ImageDraw.Draw(image)
    draw.ellipse((12, 12, 52, 52), outline=0, width=4)
    return compute_visual_signature(image)


def _entry(identifier: str, position: str) -> LegendEntry:
    return LegendEntry(
        id=identifier,
        page=1,
        region_id="legend-main",
        bbox_pdf=BBox(200, 20, 290, 45),
        symbol_bbox_pdf=BBox(200, 20, 225, 45),
        text_bbox_pdf=BBox(230, 20, 290, 45),
        raw_crop=f"symbol_crops/{identifier}.png",
        normalized_crop=f"symbol_crops/{identifier}.normalized.png",
        source_kind="sheet_legend",
        source_document="synthetic.pdf",
        status="extracted",
        position=position,
        name_raw=f"Symbol {position}",
        name_normalized=f"symbol {position.lower()}",
        confidence=1.0,
    )


def _ambiguous_artifacts() -> tuple[PageArtifacts, object]:
    signature = _signature()
    instance = SymbolInstance(
        id="SI-open",
        page=1,
        bbox_pdf=BBox(50, 50, 75, 75),
        raw_crop="symbol_crops/SI-open.png",
        source_kinds=("connected_component",),
        status="unclassified",
        symbol_type_id="ST-open",
        geometry_confidence=0.9,
    )
    symbol_type = SymbolType(
        id="ST-open",
        representative_crop="symbol_crops/ST-open.png",
        instance_ids=(instance.id,),
        status="unclassified",
        visual_signature=signature,
    )
    entries = [_entry("LE-a", "A1"), _entry("LE-b", "B1")]
    return (
        PageArtifacts(
            page=1,
            legend_entries=entries,
            symbol_types=[symbol_type],
            symbol_instances=[instance],
            unclassified_symbols=[instance],
        ),
        signature,
    )


def test_resolver_keeps_synthetic_unknown_and_requires_independent_confirmation(
    tmp_path: Path,
) -> None:
    _, entries = extract_page_legend(
        SYNTHETIC_VISUAL,
        page_number=1,
        output_root=tmp_path,
    )
    match_page_templates(
        SYNTHETIC_VISUAL,
        page_number=1,
        output_root=tmp_path,
    )
    detect_page_open_set(
        SYNTHETIC_VISUAL,
        page_number=1,
        output_root=tmp_path,
    )

    resolved = resolve_page_symbols(
        SYNTHETIC_VISUAL,
        page_number=1,
        output_root=tmp_path,
    )

    confirmed = [
        instance for instance in resolved.symbol_instances
        if instance.status == "confirmed"
    ]
    probable = [
        instance for instance in resolved.symbol_instances
        if instance.status == "probable"
    ]
    assert confirmed == []
    assert len(probable) == 2
    assert all(
        any(item.kind == "document_template" for item in instance.classification_evidence)
        for instance in probable
    )
    assert len(resolved.unclassified_symbols) == 2
    assert all(item.legend_entry_id is None for item in resolved.unclassified_symbols)
    assert resolved.conflicts == []
    assert {item.id for item in resolved.unmatched_legend_entries} == {
        item.id for item in entries
    }

    reloaded = load_page_artifacts(tmp_path, 1)
    assert reloaded.symbol_types == resolved.symbol_types
    assert reloaded.symbol_instances == resolved.symbol_instances
    assert reloaded.unclassified_symbols == resolved.unclassified_symbols


def test_equivalent_document_rows_create_conflict_not_false_binding() -> None:
    artifacts, signature = _ambiguous_artifacts()

    resolved = resolve_artifacts(
        artifacts,
        contexts={"SI-open": InstanceContext("SI-open")},
        legend_signatures={"LE-a": signature, "LE-b": signature},
        type_signatures={"ST-open": signature},
    )

    assert resolved.symbol_types[0].status == "conflicting"
    assert resolved.symbol_types[0].matched_legend_entry_id is None
    assert resolved.symbol_instances[0].status == "conflicting"
    assert resolved.symbol_instances[0].legend_entry_id is None
    assert len(resolved.conflicts) == 1
    assert resolved.conflicts[0].candidate_legend_entry_ids == ("LE-a", "LE-b")
    assert {entry.id for entry in resolved.unmatched_legend_entries} == {
        "LE-a",
        "LE-b",
    }


def test_resolver_does_not_retype_text_unreadable_row_as_unmatched() -> None:
    artifacts, signature = _ambiguous_artifacts()
    unreadable = replace(
        artifacts.legend_entries[0],
        id="LE-unreadable",
        status="text_unreadable",
        name_raw=None,
        name_normalized=None,
    )
    artifacts.legend_entries = [unreadable]

    resolved = resolve_artifacts(
        artifacts,
        contexts={"SI-open": InstanceContext("SI-open")},
        legend_signatures={"LE-unreadable": signature},
        type_signatures={"ST-open": signature},
    )

    assert resolved.legend_entries == [unreadable]
    assert resolved.symbol_types[0].status == "unclassified"
    assert resolved.symbol_types[0].matched_legend_entry_id is None
    assert resolved.unmatched_legend_entries == []


def test_document_position_breaks_visual_tie_with_auditable_evidence() -> None:
    artifacts, signature = _ambiguous_artifacts()

    resolved = resolve_artifacts(
        artifacts,
        contexts={
            "SI-open": InstanceContext("SI-open", nearby_labels=("A1",))
        },
        legend_signatures={"LE-a": signature, "LE-b": signature},
        type_signatures={"ST-open": signature},
    )

    symbol_type = resolved.symbol_types[0]
    assert symbol_type.status == "confirmed"
    assert symbol_type.matched_legend_entry_id == "LE-a"
    assert any(
        item.kind == "document_position"
        for item in symbol_type.classification_evidence
    )
    assert [entry.id for entry in resolved.unmatched_legend_entries] == ["LE-b"]


def test_line_context_alone_cannot_confirm_a_binding() -> None:
    artifacts, signature = _ambiguous_artifacts()
    artifacts.legend_entries = [artifacts.legend_entries[0]]

    resolved = resolve_artifacts(
        artifacts,
        contexts={
            "SI-open": InstanceContext(
                "SI-open",
                connected_line_ids=("LN-common",),
            )
        },
        legend_signatures={"LE-a": signature},
        type_signatures={"ST-open": signature},
    )

    assert resolved.symbol_types[0].status == "probable"
    assert resolved.symbol_instances[0].status == "probable"
    assert any(
        item.kind == "document_line_context"
        for item in resolved.symbol_types[0].classification_evidence
    )


def test_single_digit_position_is_not_independent_evidence() -> None:
    artifacts, signature = _ambiguous_artifacts()
    numeric = _entry("LE-numeric", "1")
    artifacts.legend_entries = [numeric]

    resolved = resolve_artifacts(
        artifacts,
        contexts={"SI-open": InstanceContext("SI-open", nearby_labels=("1",))},
        legend_signatures={numeric.id: signature},
        type_signatures={"ST-open": signature},
    )

    assert resolved.symbol_types[0].status == "probable"
    assert not any(
        item.kind == "document_position"
        for item in resolved.symbol_types[0].classification_evidence
    )


def test_s6_gate_reports_false_binding_and_miss_separately() -> None:
    ground_truth = {
        "pages": [
            {
                "pageId": "p1",
                "legend": {"region": {"status": "not_found"}, "rows": []},
                "instances": [
                    {"bbox": [0, 0, 10, 10], "typeId": "correct"},
                    {"bbox": [20, 0, 30, 10], "typeId": "missing"},
                ],
                "negativeComponents": [],
            }
        ]
    }
    prediction = {
        "pages": [
            {
                "pageId": "p1",
                "legend": {"region": {"status": "not_found"}, "rows": []},
                "instances": [
                    {"bbox": [0, 0, 10, 10], "typeId": "wrong"},
                ],
            }
        ]
    }

    metrics = score_documents(ground_truth, prediction)

    assert metrics["instances"]["falseBindings"] == 1
    assert metrics["instances"]["misses"] == 1
