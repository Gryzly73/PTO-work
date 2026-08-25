import json
from pathlib import Path

from PIL import Image
import pytest

from symbols.artifacts import load_page_artifacts, page_sidecar_dir
from symbols.geometry import BBox
from symbols.gt_schema import load_document
from symbols.legend_rows import (
    FirstAvailableTextReader,
    TextSpan,
    extract_legend,
    main,
)
from symbols.score_gt import bbox_iou


FIXTURES = Path(__file__).parents[1] / "symbols" / "fixtures"
SYNTHETIC_VISUAL = FIXTURES / "synthetic_legend.svg"
SYNTHETIC_GT = FIXTURES / "synthetic.gt.json"
REGION = BBox(390, 35, 570, 140)


class FakeReader:
    def __init__(self, spans):
        self.spans = tuple(spans)
        self.calls = 0

    def read(self, _page, _region_pdf):
        self.calls += 1
        return self.spans


def test_synthetic_fixture_extracts_all_gt_rows_and_crops(tmp_path: Path) -> None:
    entries = extract_legend(
        SYNTHETIC_VISUAL,
        page_number=1,
        region_pdf=REGION,
        region_id="legend-main",
        output_root=tmp_path,
    )

    assert [entry.name_raw for entry in entries] == ["Valve V1", "Sensor S1"]
    assert [entry.status for entry in entries] == ["extracted", "extracted"]
    assert all(entry.source_kind == "pdf_text" for entry in entries)
    gt_rows = load_document(SYNTHETIC_GT)["pages"][0]["legend"]["rows"]
    assert all(
        max(bbox_iou(row["bbox"], entry.bbox_pdf.to_list()) for entry in entries) >= 0.5
        for row in gt_rows
    )

    page_dir = page_sidecar_dir(tmp_path, 1)
    for entry in entries:
        raw_path = page_dir / entry.raw_crop
        normalized_path = page_dir / entry.normalized_crop
        assert raw_path.is_file()
        assert normalized_path.is_file()
        with Image.open(raw_path) as raw:
            assert raw.width > raw.height
        with Image.open(normalized_path) as normalized:
            assert normalized.size == (96, 96)
            assert normalized.mode == "L"

    loaded = load_page_artifacts(tmp_path, 1)
    assert loaded.legend_entries == entries
    assert loaded.summary.legend_entry_count == 2


def test_layout_keeps_text_unreadable_row_with_raw_crop(tmp_path: Path) -> None:
    reader = FakeReader(
        [TextSpan("Valve V1", BBox(440, 50, 494, 69), source_kind="fake")]
    )
    entries = extract_legend(
        SYNTHETIC_VISUAL,
        page_number=1,
        region_pdf=REGION,
        region_id="legend-main",
        output_root=tmp_path,
        reader=reader,
    )

    assert reader.calls == 1
    assert len(entries) == 2
    assert entries[0].status == "extracted"
    assert entries[1].status == "text_unreadable"
    assert entries[1].name_raw is None
    assert entries[1].text_bbox_pdf is None
    crop = page_sidecar_dir(tmp_path, 1) / entries[1].raw_crop
    assert crop.is_file()


def test_rejects_excessive_text_and_one_point_symbol_columns(
    tmp_path: Path,
) -> None:
    reader = FakeReader(
        [
            TextSpan("X" * 161, BBox(440, 50, 560, 69), source_kind="fake"),
            TextSpan("Too close", BBox(398, 100, 450, 119), source_kind="fake"),
        ]
    )

    entries = extract_legend(
        SYNTHETIC_VISUAL,
        page_number=1,
        region_pdf=REGION,
        region_id="legend-main",
        output_root=tmp_path,
        reader=reader,
    )

    assert entries == []
    crop_root = page_sidecar_dir(tmp_path, 1) / "symbol_crops"
    assert not list(crop_root.rglob("*.png"))


def test_first_available_reader_prefers_first_nonempty_adapter() -> None:
    first = FakeReader([TextSpan("PDF text", BBox(1, 1, 10, 10))])
    fallback = FakeReader([TextSpan("OCR text", BBox(1, 1, 10, 10))])
    reader = FirstAvailableTextReader(first, fallback)

    assert [span.text for span in reader.read(None, BBox(0, 0, 20, 20))] == [
        "PDF text"
    ]
    assert first.calls == 1
    assert fallback.calls == 0


def test_extraction_is_byte_stable(tmp_path: Path) -> None:
    kwargs = {
        "page_number": 1,
        "region_pdf": REGION,
        "region_id": "legend-main",
        "output_root": tmp_path,
    }
    first = extract_legend(SYNTHETIC_VISUAL, **kwargs)
    page_dir = page_sidecar_dir(tmp_path, 1)
    first_json = (page_dir / "legend_entries.json").read_bytes()
    first_crops = {
        path.name: path.read_bytes()
        for path in (page_dir / "symbol_crops" / "legend").iterdir()
    }

    second = extract_legend(SYNTHETIC_VISUAL, **kwargs)
    assert second == first
    assert (page_dir / "legend_entries.json").read_bytes() == first_json
    assert {
        path.name: path.read_bytes()
        for path in (page_dir / "symbol_crops" / "legend").iterdir()
    } == first_crops


def test_cli_accepts_fixture_provided_region(tmp_path: Path, capsys) -> None:
    assert (
        main(
            [
                str(SYNTHETIC_VISUAL),
                "--gt",
                str(SYNTHETIC_GT),
                "--output",
                str(tmp_path),
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "legendEntryCount": 2,
        "page": 1,
        "regionId": "legend-main",
        "statuses": ["extracted", "extracted"],
    }


def test_pb_local_smoke_is_opt_in(tmp_path: Path) -> None:
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
    region = gt["pages"][0]["legend"]["region"]
    if region["status"] != "found":
        pytest.skip("local PB fixture has no annotated legend region")

    entries = extract_legend(
        visual,
        page_number=1,
        region_pdf=BBox.from_list(region["bbox"]),
        region_id=region["id"],
        output_root=tmp_path,
    )
    assert entries
    assert (page_sidecar_dir(tmp_path, 1) / "legend_entries.json").is_file()
