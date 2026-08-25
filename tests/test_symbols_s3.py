import json
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw
import pytest

from symbols.artifacts import load_page_artifacts
from symbols.geometry import BBox
from symbols.gates import SymbolsBudgets
from symbols.gt_schema import load_document
from symbols.legend_layout import (
    LegendRegionResult,
    detect_legend_region,
    extract_page_legend,
    main,
)
from symbols.legend_rows import TextSpan, extract_legend
from symbols.pipeline import SymbolsPipelineConfig, run_page_symbols
from symbols.score_gt import bbox_iou, score_documents


FIXTURES = Path(__file__).parents[1] / "symbols" / "fixtures"
SYNTHETIC_VISUAL = FIXTURES / "synthetic_legend.svg"
SYNTHETIC_GT = FIXTURES / "synthetic.gt.json"


def _blank_pdf(path: Path) -> None:
    document = pymupdf.open()
    document.new_page(width=600, height=400)
    document.save(path)
    document.close()


def _heading_legend_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=400, height=300)
    page.insert_text((220, 30), "LEGEND", fontsize=12)
    page.draw_circle((235, 70), 8)
    page.insert_text((270, 74), "Valve V1", fontsize=11)
    page.draw_line((227, 105), (243, 105))
    page.draw_line((235, 97), (235, 113))
    page.insert_text((270, 109), "Sensor S1", fontsize=11)
    document.save(path)
    document.close()


def _wide_heading_legend_with_distant_content(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=1200, height=842)
    page.insert_text((124, 100), "SYMBOLS", fontsize=40)
    for index, y in enumerate((160, 205, 250), start=1):
        page.draw_circle((145, y - 4), 7)
        page.insert_text((210, y), f"Valve item {index}", fontsize=11)
        page.insert_text((900, y), f"Distant drawing annotation {index}", fontsize=11)
    document.save(path)
    document.close()


def _borderless_legend_with_one_long_rule(path: Path) -> BBox:
    document = pymupdf.open()
    page = document.new_page(width=600, height=400)
    region = BBox(40, 40, 560, 330)
    for index, y in enumerate((80, 120, 160), start=1):
        page.draw_circle((75, y - 4), 6)
        page.insert_text((130, y), f"Valve row {index}", fontsize=11)
    page.draw_line((40, 280), (560, 280), width=2)
    document.save(path)
    document.close()
    return region


def _compact_subsection_legend_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=600, height=550)
    page.insert_text((390, 35), "LEGEND", fontsize=12)

    page.draw_line((405, 75), (455, 75), width=1)
    page.insert_text((480, 79), "Boundary line", fontsize=10)

    page.draw_rect((405, 105, 455, 125), width=0.8)
    for x in range(407, 455, 8):
        page.draw_line((x, 124), (min(455, x + 18), 106), width=0.5)
    page.insert_text((480, 119), "Paved area", fontsize=10)

    page.draw_circle((430, 155), 7, width=1)
    page.insert_text((480, 159), "Survey point", fontsize=10)

    page.insert_text((410, 202), "ENGINEERING NETWORKS:", fontsize=11)

    page.draw_line((405, 238), (455, 238), width=1)
    page.insert_text((480, 242), "Water line", fontsize=10)

    page.draw_rect((405, 268, 455, 288), width=0.8)
    for y in range(271, 288, 5):
        page.draw_line((406, y), (454, y), width=0.5)
    page.insert_text((480, 282), "Drainage zone", fontsize=10)

    page.draw_circle((430, 318), 7, width=1)
    page.draw_line((423, 318), (437, 318), width=0.8)
    page.draw_line((430, 311), (430, 325), width=0.8)
    page.insert_text((480, 322), "Hydrant point", fontsize=10)
    document.save(path)
    document.close()


def _dense_linework_without_legend(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=600, height=400)
    for x in range(20, 581, 12):
        page.draw_line((x, 20), (x, 380), width=0.5)
    for y in range(20, 381, 12):
        page.draw_line((20, y), (580, y), width=0.5)
    for offset in range(-300, 601, 18):
        page.draw_line((max(20, offset), 20), (min(580, offset + 360), 380), width=0.3)
    document.save(path)
    document.close()


def _large_table_without_legend(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=600, height=400)
    for x in range(40, 561, 104):
        page.draw_line((x, 30), (x, 370))
    for y in range(30, 371, 34):
        page.draw_line((40, y), (560, y))
    for row, y in enumerate(range(52, 359, 34), start=1):
        for column, x in enumerate(range(50, 500, 104), start=1):
            page.insert_text((x, y), f"R{row}C{column}", fontsize=8)
    document.save(path)
    document.close()


def _oversized_framed_text_block(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=600, height=400)
    page.draw_rect((40, 40, 560, 340))
    for index, y in enumerate(range(65, 326, 24), start=1):
        page.draw_circle((70, y - 4), 5)
        page.insert_text((95, y), f"Schedule row {index}", fontsize=9)
    document.save(path)
    document.close()


def _text_only_legend_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=400, height=300)
    page.draw_rect((220, 40, 380, 140))
    page.draw_line((220, 90), (380, 90))
    page.insert_text((270, 70), "Valve V1", fontsize=10)
    page.insert_text((270, 120), "Sensor S1", fontsize=10)
    document.save(path)
    document.close()


def _scanned_frame_pdf(path: Path, image_path: Path) -> None:
    image = Image.new("RGB", (400, 300), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((220, 40, 380, 140), outline="black", width=2)
    draw.line((220, 90, 380, 90), fill="black", width=2)
    image.save(image_path)
    document = pymupdf.open()
    page = document.new_page(width=400, height=300)
    page.insert_image(page.rect, filename=str(image_path))
    document.save(path)
    document.close()


class _ScannedTextReader:
    def read(self, _page, _region_pdf):
        return (
            TextSpan("Valve V1", BBox(270, 55, 325, 70), source_kind="fake_ocr"),
            TextSpan("Sensor S1", BBox(270, 105, 330, 120), source_kind="fake_ocr"),
        )


def test_auto_detects_synthetic_region_and_extracts_rows(tmp_path: Path) -> None:
    result, entries = extract_page_legend(
        SYNTHETIC_VISUAL,
        page_number=1,
        output_root=tmp_path,
    )

    gt = load_document(SYNTHETIC_GT)["pages"][0]["legend"]
    assert result.status == "found"
    assert result.bbox_pdf is not None
    assert bbox_iou(result.bbox_pdf.to_list(), gt["region"]["bbox"]) >= 0.5
    assert {"frame", "row_pattern", "graphic_text_rows"} <= set(
        result.detection_sources
    )
    assert [entry.name_raw for entry in entries] == ["Valve V1", "Sensor S1"]
    assert all(
        max(bbox_iou(row["bbox"], entry.bbox_pdf.to_list()) for entry in entries)
        >= 0.5
        for row in gt["rows"]
    )


def test_low_confidence_is_explicit_not_found_and_writes_empty_sidecars(
    tmp_path: Path,
) -> None:
    source = tmp_path / "blank.pdf"
    output = tmp_path / "output"
    _blank_pdf(source)

    result, entries = extract_page_legend(
        source, page_number=1, output_root=output
    )

    assert result == LegendRegionResult("not_found", None, None, 0.0)
    assert entries == []
    artifacts = load_page_artifacts(output, 1)
    assert artifacts.legend_entries == []
    assert artifacts.summary.legend_entry_count == 0


def test_dense_plan_with_long_linework_fails_closed_before_matching(
    tmp_path: Path,
) -> None:
    source = tmp_path / "dense-plan.pdf"
    output = tmp_path / "output"
    _dense_linework_without_legend(source)

    artifacts = run_page_symbols(
        source,
        page_number=1,
        output_root=output,
    )

    assert artifacts.summary is not None
    assert artifacts.summary.status == "no_valid_legend"
    assert artifacts.summary.anomaly_codes == ("legend_not_found",)
    assert artifacts.symbol_candidates == []
    assert artifacts.symbol_instances == []
    assert artifacts.symbol_types == []
    assert not list((output / "symbols" / "page_0001" / "symbol_crops").rglob("*.png"))
    assert load_page_artifacts(output, 1).summary == artifacts.summary


def test_large_table_cannot_produce_confirmed_symbols(
    tmp_path: Path,
) -> None:
    source = tmp_path / "large-table.pdf"
    output = tmp_path / "output"
    _large_table_without_legend(source)

    artifacts = run_page_symbols(
        source,
        page_number=1,
        output_root=output,
        config=SymbolsPipelineConfig(
            budgets=SymbolsBudgets(
                max_legend_entries=1,
                max_template_components=1,
            )
        ),
    )

    assert artifacts.summary is not None
    assert artifacts.summary.status in {"no_valid_legend", "partial"}
    assert not any(item.status == "confirmed" for item in artifacts.symbol_instances)
    assert len(
        list((output / "symbols" / "page_0001" / "symbol_crops").rglob("*.png"))
    ) < 100


def test_oversized_framed_text_block_is_not_a_legend(tmp_path: Path) -> None:
    source = tmp_path / "oversized-frame.pdf"
    _oversized_framed_text_block(source)

    result = detect_legend_region(source, page_number=1)

    assert result.status == "not_found"
    assert "oversized" in result.detection_sources


def test_pipeline_stops_when_legend_has_no_valid_templates(
    tmp_path: Path,
) -> None:
    source = tmp_path / "text-only-legend.pdf"
    output = tmp_path / "output"
    _text_only_legend_pdf(source)

    artifacts = run_page_symbols(source, page_number=1, output_root=output)

    assert artifacts.summary is not None
    assert artifacts.summary.status == "no_valid_legend"
    assert artifacts.summary.anomaly_codes == ("legend_has_no_valid_templates",)
    assert artifacts.symbol_candidates == []
    assert artifacts.symbol_instances == []
    assert not (output / "symbols" / "page_0001" / "symbol_crops" / "candidates").exists()


def test_heading_and_graphic_text_rows_detect_borderless_legend(
    tmp_path: Path,
) -> None:
    source = tmp_path / "heading.pdf"
    _heading_legend_pdf(source)

    result = detect_legend_region(source, page_number=1)

    assert result.status == "found"
    assert {"heading", "row_pattern", "graphic_text_rows"} <= set(
        result.detection_sources
    )


def test_wide_heading_does_not_absorb_distant_drawing_content(
    tmp_path: Path,
) -> None:
    source = tmp_path / "wide-heading.pdf"
    _wide_heading_legend_with_distant_content(source)

    result = detect_legend_region(source, page_number=1)

    assert result.status == "found"
    assert result.bbox_pdf is not None
    assert result.bbox_pdf.x1 < 700


def test_single_usable_separator_falls_back_to_text_rows(tmp_path: Path) -> None:
    source = tmp_path / "single-rule.pdf"
    region = _borderless_legend_with_one_long_rule(source)

    entries = extract_legend(
        source,
        page_number=1,
        region_pdf=region,
        region_id="legend-main",
        output_root=tmp_path / "output",
    )

    assert [entry.name_raw for entry in entries] == [
        "Valve row 1",
        "Valve row 2",
        "Valve row 3",
    ]


def test_compact_right_column_subsection_extracts_mixed_symbol_rows(
    tmp_path: Path,
) -> None:
    source = tmp_path / "compact-subsection.pdf"
    _compact_subsection_legend_pdf(source)

    result, entries = extract_page_legend(
        source,
        page_number=1,
        output_root=tmp_path / "output",
    )

    assert result.status == "found"
    assert result.confidence >= 0.62
    assert result.bbox_pdf is not None
    assert result.bbox_pdf.x0 >= 350
    assert result.bbox_pdf.x1 <= 600
    assert [entry.name_raw for entry in entries] == [
        "Boundary line",
        "Paved area",
        "Survey point",
        "Water line",
        "Drainage zone",
        "Hydrant point",
    ]
    assert all(entry.symbol_bbox_pdf.width >= 40 for entry in entries)
    assert all(
        entry.text_bbox_pdf is not None
        and entry.symbol_bbox_pdf.x1 < entry.text_bbox_pdf.x0
        for entry in entries
    )


def test_raster_frame_uses_injected_ocr_as_supporting_evidence(
    tmp_path: Path,
) -> None:
    source = tmp_path / "scanned.pdf"
    _scanned_frame_pdf(source, tmp_path / "scanned.png")

    result = detect_legend_region(
        source, page_number=1, reader=_ScannedTextReader()
    )

    assert result.status == "found"
    assert result.bbox_pdf is not None
    assert bbox_iou(result.bbox_pdf.to_list(), [220, 40, 381, 141]) >= 0.9
    assert {"raster_frame", "row_pattern"} <= set(result.detection_sources)


def test_region_scorer_reports_precision_and_recall() -> None:
    ground_truth = load_document(SYNTHETIC_GT)
    perfect = load_document(FIXTURES / "synthetic.perfect.prediction.json")
    metrics = score_documents(ground_truth, perfect)
    assert metrics["legendRegions"] == {
        "groundTruth": 1,
        "predicted": 1,
        "matched": 1,
        "recall": 1.0,
        "precision": 1.0,
    }


def test_cli_reports_detection_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                str(SYNTHETIC_VISUAL),
                "--output",
                str(tmp_path),
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["legendStatus"] == "found"
    assert output["legendEntryCount"] == 2
    assert output["bboxPdf"] == [390.0, 35.0, 570.0, 140.0]
    assert "row_pattern" in output["detectionSources"]


def test_pb_auto_detection_is_opt_in(tmp_path: Path) -> None:
    manifest_path = FIXTURES / "MANIFEST.local.json"
    if not manifest_path.exists():
        pytest.skip("local PB fixture manifest is not installed")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fixture = next(
        (
            item
            for item in manifest["fixtures"]
            if "PB" in item.get("roles", []) and "dev" in item.get("roles", [])
        ),
        None,
    )
    if fixture is None:
        pytest.skip("local PB dev fixture is not configured")
    visual = manifest_path.parent / fixture["visual"]
    gt_path = manifest_path.parent / fixture["gt"]
    if not visual.exists() or not gt_path.exists():
        pytest.skip("local PB visual/GT files are not installed")
    gt = load_document(gt_path)
    gt_region = gt["pages"][0]["legend"]["region"]
    if gt_region["status"] != "found":
        pytest.skip("local PB fixture has no annotated legend region")

    result, entries = extract_page_legend(
        visual, page_number=1, output_root=tmp_path
    )
    assert result.status == "found"
    assert result.bbox_pdf is not None
    assert bbox_iou(result.bbox_pdf.to_list(), gt_region["bbox"]) >= 0.5
    assert entries


def test_page_35_corrective_regression_is_opt_in(tmp_path: Path) -> None:
    manifest_path = FIXTURES / "MANIFEST.local.json"
    if not manifest_path.exists():
        pytest.skip("local customer fixture manifest is not installed")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fixture = next(
        (
            item
            for item in manifest["fixtures"]
            if "IOS2" in item.get("roles", [])
            and "page35" in item.get("roles", [])
        ),
        None,
    )
    if fixture is None:
        pytest.skip("local page-35 fixture is not configured")
    visual = manifest_path.parent / fixture["visual"]
    gt_path = manifest_path.parent / fixture["gt"]
    if not visual.exists() or not gt_path.exists():
        pytest.skip("local page-35 fixture files are not installed")
    page_number = fixture.get("page", 35)

    result, entries = extract_page_legend(
        visual,
        page_number=page_number,
        output_root=tmp_path,
    )
    gt = load_document(gt_path)["pages"][0]["legend"]

    assert result.status == "found"
    assert result.confidence >= 0.62
    assert result.bbox_pdf is not None
    assert bbox_iou(result.bbox_pdf.to_list(), gt["region"]["bbox"]) >= 0.85
    assert len(entries) == len(gt["rows"]) == 26
    assert all(entry.symbol_bbox_pdf.width >= 100 for entry in entries)
    assert all(len(entry.name_raw or "") <= 160 for entry in entries)
    assert not any(
        "условные обозначения" in (entry.name_normalized or "")
        or "инженерные сети" in (entry.name_normalized or "")
        for entry in entries
    )
