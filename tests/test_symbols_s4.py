from dataclasses import replace
import json
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw
import pytest

from symbols.artifacts import load_page_artifacts
from symbols.dedupe import (
    TemplateDetection,
    bbox_iou,
    deduplicate_detections,
)
from symbols.geometry import BBox
from symbols.gt_schema import load_document
from symbols.legend_layout import extract_page_legend
from symbols.legend_rows import extract_legend
from symbols.schema import stable_id
from symbols.template_matcher import (
    TemplateMatcherConfig,
    _valid_template,
    find_template_detections,
    match_page_templates,
)


FIXTURES = Path(__file__).parents[1] / "symbols" / "fixtures"
SYNTHETIC_VISUAL = FIXTURES / "synthetic_legend.svg"


def test_known_document_templates_find_two_instances_and_persist_crops(
    tmp_path: Path,
) -> None:
    _, entries = extract_page_legend(
        SYNTHETIC_VISUAL,
        page_number=1,
        output_root=tmp_path,
    )

    artifacts = match_page_templates(
        SYNTHETIC_VISUAL,
        page_number=1,
        output_root=tmp_path,
    )

    assert len(artifacts.symbol_types) == 2
    assert len(artifacts.symbol_instances) == 2
    assert artifacts.unmatched_legend_entries == []
    assert all(instance.status == "probable" for instance in artifacts.symbol_instances)
    assert all(symbol_type.status == "probable" for symbol_type in artifacts.symbol_types)
    expected = (BBox(88, 88, 112, 112), BBox(188, 78, 212, 102))
    assert all(
        max(bbox_iou(instance.bbox_pdf, target) for instance in artifacts.symbol_instances)
        >= 0.7
        for target in expected
    )
    assert {instance.legend_entry_id for instance in artifacts.symbol_instances} == {
        entry.id for entry in entries
    }
    page_dir = tmp_path / "symbols" / "page_0001"
    assert all(
        (page_dir / instance.raw_crop).is_file()
        and (page_dir / instance.normalized_crop).is_file()
        for instance in artifacts.symbol_instances
    )
    reloaded = load_page_artifacts(tmp_path, 1)
    assert reloaded.symbol_instances == artifacts.symbol_instances
    assert reloaded.summary.symbol_instance_count == 2


def test_missing_document_template_is_preserved_as_unmatched(tmp_path: Path) -> None:
    _, entries = extract_page_legend(
        SYNTHETIC_VISUAL,
        page_number=1,
        output_root=tmp_path,
    )
    missing = replace(
        entries[0],
        id=stable_id("LE", 1, "missing-template"),
        symbol_bbox_pdf=BBox(35, 35, 250, 50),
        name_raw="Missing symbol",
        name_normalized="missing symbol",
    )

    artifacts = match_page_templates(
        SYNTHETIC_VISUAL,
        page_number=1,
        output_root=tmp_path,
        legend_entries=[*entries, missing],
    )

    assert [entry.id for entry in artifacts.unmatched_legend_entries] == [missing.id]
    assert artifacts.unmatched_legend_entries[0].status == "unmatched"


def test_text_unreadable_legend_row_is_not_retyped_as_unmatched(
    tmp_path: Path,
) -> None:
    _, entries = extract_page_legend(
        SYNTHETIC_VISUAL,
        page_number=1,
        output_root=tmp_path,
    )
    unreadable = replace(
        entries[0],
        id=stable_id("LE", 1, "unreadable-template"),
        status="text_unreadable",
        name_raw=None,
        name_normalized=None,
    )

    artifacts = match_page_templates(
        SYNTHETIC_VISUAL,
        page_number=1,
        output_root=tmp_path,
        legend_entries=[unreadable],
    )

    assert artifacts.legend_entries == [unreadable]
    assert artifacts.symbol_types == []
    assert artifacts.symbol_instances == []
    assert artifacts.unmatched_legend_entries == []


def test_scale_gate_is_explicit_and_configurable(tmp_path: Path) -> None:
    _, entries = extract_page_legend(
        SYNTHETIC_VISUAL,
        page_number=1,
        output_root=tmp_path,
    )

    detections, _, _ = find_template_detections(
        SYNTHETIC_VISUAL,
        page_number=1,
        legend_entries=entries,
        config=TemplateMatcherConfig(scales=(2.0,), scale_tolerance=0.05),
    )

    assert detections == []


def test_uninformative_solid_template_is_rejected_before_page_scan(
    tmp_path: Path,
) -> None:
    source = tmp_path / "solid-template.pdf"
    document = pymupdf.open()
    page = document.new_page(width=600, height=400)
    page.draw_rect((20, 20, 50, 50), color=(0, 0, 0), fill=(0, 0, 0))
    document.save(source)
    document.close()
    _, entries = extract_page_legend(
        SYNTHETIC_VISUAL,
        page_number=1,
        output_root=tmp_path / "fixture",
    )
    solid = replace(entries[0], symbol_bbox_pdf=BBox(20, 20, 50, 50))
    metrics = {}

    detections, _, _ = find_template_detections(
        source,
        page_number=1,
        legend_entries=[solid],
        metrics=metrics,
    )

    assert detections == []
    assert metrics == {
        "templateCount": 0,
        "rejectedTemplateCount": 1,
        "componentCount": 0,
        "rawDetectionCount": 0,
        "detectionCount": 0,
    }


def test_template_quality_accepts_line_hatch_and_point_but_rejects_empty() -> None:
    config = TemplateMatcherConfig()
    line = Image.new("L", (200, 50), 255)
    ImageDraw.Draw(line).line((20, 25, 180, 25), fill=0, width=1)
    hatch = Image.new("L", (100, 30), 255)
    hatch_draw = ImageDraw.Draw(hatch)
    hatch_draw.rectangle((8, 4, 92, 26), outline=0, width=1)
    for x in range(10, 92, 8):
        hatch_draw.line((x, 25, min(92, x + 20), 5), fill=0, width=1)
    point = Image.new("L", (100, 30), 255)
    point_draw = ImageDraw.Draw(point)
    point_draw.ellipse((43, 7, 57, 21), outline=0, width=2)
    point_draw.line((50, 4, 50, 24), fill=0, width=1)
    light_dotted = Image.new("L", (100, 30), 255)
    dotted_draw = ImageDraw.Draw(light_dotted)
    for x in range(8, 93, 4):
        dotted_draw.point((x, 5), fill=215)
        dotted_draw.point((x, 25), fill=215)
    for y in range(5, 26, 4):
        dotted_draw.point((8, y), fill=215)
        dotted_draw.point((92, y), fill=215)
    textured_fill = Image.new("L", (100, 30), 255)
    for y in range(5, 26):
        for x in range(8, 93):
            textured_fill.putpixel((x, y), 40 + (x * 17 + y * 29) % 140)
    solid_fill = Image.new("L", (100, 30), 255)
    ImageDraw.Draw(solid_fill).rectangle((8, 5, 92, 25), fill=80)

    assert _valid_template(line, config) is not None
    assert _valid_template(hatch, config) is not None
    assert _valid_template(point, config) is not None
    assert _valid_template(light_dotted, config) is not None
    assert _valid_template(textured_fill, config) is not None
    assert _valid_template(solid_fill, config) is None
    assert _valid_template(Image.new("L", (100, 30), 255), config) is None


def test_configured_scale_finds_larger_instance(tmp_path: Path) -> None:
    source = tmp_path / "scaled.pdf"
    document = pymupdf.open()
    page = document.new_page(width=300, height=200)
    page.draw_circle((220, 50), 8)
    page.insert_text((245, 54), "Valve", fontsize=10)
    page.draw_circle((80, 120), 10)
    document.save(source)
    document.close()
    entries = extract_legend(
        source,
        page_number=1,
        region_pdf=BBox(200, 20, 290, 80),
        region_id="legend-scaled",
        output_root=tmp_path,
    )

    detections, _, _ = find_template_detections(
        source,
        page_number=1,
        legend_entries=entries,
        config=TemplateMatcherConfig(
            scales=(1.25,),
            scale_tolerance=0.15,
            score_threshold=0.72,
        ),
    )

    assert len(detections) == 1
    assert detections[0].scale == 1.25
    assert bbox_iou(detections[0].bbox_pdf, BBox(68, 108, 92, 132)) >= 0.7


def test_dedupe_is_global_and_deterministic() -> None:
    detections = [
        TemplateDetection("LE-b", BBox(10, 10, 30, 30), 0.9, 1.0),
        TemplateDetection("LE-a", BBox(11, 11, 31, 31), 0.9, 1.0),
        TemplateDetection("LE-a", BBox(60, 10, 80, 30), 0.8, 1.0),
    ]

    forward = deduplicate_detections(detections, iou_threshold=0.5)
    reverse = deduplicate_detections(reversed(detections), iou_threshold=0.5)

    assert forward == reverse
    assert [(item.legend_entry_id, item.bbox_pdf) for item in forward] == [
        ("LE-a", BBox(60, 10, 80, 30)),
        ("LE-a", BBox(11, 11, 31, 31)),
    ]


def test_invalid_matcher_threshold_is_rejected() -> None:
    with pytest.raises(ValueError, match="score_threshold"):
        TemplateMatcherConfig(score_threshold=1.1)


def test_pb_known_templates_are_opt_in(tmp_path: Path) -> None:
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
    expected = [
        BBox.from_list(item["bbox"])
        for item in gt["pages"][0]["instances"]
        if item["status"] == "confirmed"
    ]
    if not expected:
        pytest.skip("local PB fixture has no confirmed known instances")

    _, entries = extract_page_legend(
        visual,
        page_number=1,
        output_root=tmp_path,
    )
    artifacts = match_page_templates(
        visual,
        page_number=1,
        output_root=tmp_path,
        legend_entries=entries,
    )

    assert all(
        max(bbox_iou(instance.bbox_pdf, target) for instance in artifacts.symbol_instances)
        >= 0.5
        for target in expected
    )
