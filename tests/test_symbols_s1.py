import json
from pathlib import Path

import pytest

from symbols import SYMBOLS_SCHEMA_VERSION
import symbols.artifacts as artifacts_module
from symbols.artifacts import (
    ALL_LIST_SIDECARS,
    ArtifactError,
    PageArtifacts,
    atomic_write_bytes,
    load_page_artifacts,
    page_sidecar_dir,
    write_crop,
    write_page_artifacts,
)
from symbols.geometry import BBox, GeometryError, PageTransform
from symbols.schema import (
    LegendEntry,
    PageSymbolsSummary,
    SchemaError,
    SymbolCandidate,
    SymbolConflict,
    SymbolInstance,
    SymbolType,
    VisualSignature,
    stable_id,
)


def _entities():
    signature = VisualSignature("phash+contour", 1, "abc123")
    legend = LegendEntry(
        id="LE-p0001-a",
        page=1,
        region_id="legend-main",
        position="1",
        name_raw="Клапан",
        name_normalized="клапан",
        bbox_pdf=BBox(10, 20, 80, 40),
        symbol_bbox_pdf=BBox(10, 20, 30, 40),
        text_bbox_pdf=BBox(35, 20, 80, 40),
        raw_crop="symbol_crops/LE-p0001-a.raw.png",
        normalized_crop="symbol_crops/LE-p0001-a.norm.png",
        source_kind="sheet_legend",
        source_document="synthetic.pdf",
        status="extracted",
        confidence=0.9,
    )
    candidate = SymbolCandidate(
        id="SC-p0001-a",
        page=1,
        bbox_pdf=BBox(100, 100, 120, 120),
        raw_crop="symbol_crops/SC-p0001-a.raw.png",
        normalized_crop=None,
        source_kinds=("raster_component",),
        visual_signature=signature,
        confidence=0.8,
    )
    symbol_type = SymbolType(
        id="ST-p0001-a",
        representative_crop="symbol_crops/ST-p0001-a.png",
        instance_ids=("SI-p0001-a",),
        matched_legend_entry_id="LE-p0001-a",
        candidate_legend_entry_ids=("LE-p0001-a",),
        status="confirmed",
        visual_signature=signature,
    )
    instance = SymbolInstance(
        id="SI-p0001-a",
        page=1,
        symbol_type_id="ST-p0001-a",
        legend_entry_id="LE-p0001-a",
        bbox_pdf=BBox(100, 100, 120, 120),
        raw_crop="symbol_crops/SI-p0001-a.raw.png",
        normalized_crop="symbol_crops/SI-p0001-a.norm.png",
        nearby_labels=("DN200",),
        connected_line_ids=(),
        source_kinds=("raster_component",),
        status="confirmed",
        geometry_confidence=0.86,
        classification_confidence=0.95,
    )
    conflict = SymbolConflict(
        id="CF-p0001-a",
        page=1,
        kind="legend_binding",
        entity_ids=("ST-p0001-a",),
        candidate_legend_entry_ids=("LE-p0001-a", "LE-p0001-b"),
        reason="two visually equivalent legend rows",
    )
    return legend, candidate, symbol_type, instance, conflict


@pytest.mark.parametrize("entity", _entities())
def test_entity_json_round_trip(entity) -> None:
    encoded = json.loads(json.dumps(entity.to_dict(), ensure_ascii=False))
    assert type(entity).from_dict(encoded) == entity


def test_stable_ids_do_not_depend_on_process_state() -> None:
    first = stable_id("SI", 82, [522.1, 318.4, 548.9, 344.8], "raster")
    assert first == stable_id("SI", 82, [522.1, 318.4, 548.9, 344.8], "raster")
    assert first != stable_id("SI", 82, [523.1, 318.4, 548.9, 344.8], "raster")
    assert first.startswith("SI-p0082-")


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_pdf_raster_crop_round_trip_for_all_render_rotations(rotation: int) -> None:
    transform = PageTransform(600, 800, 1200, 1600, rotation)
    original = BBox(100, 200, 180, 260)
    raster = transform.pdf_to_raster(original)
    assert transform.raster_to_pdf(raster).to_list() == pytest.approx(
        original.to_list()
    )
    crop_region = BBox(
        raster.x0 - 10, raster.y0 - 15, raster.x1 + 20, raster.y1 + 25
    )
    crop_local = transform.pdf_to_crop(original, crop_region)
    assert transform.crop_to_pdf(crop_local, crop_region).to_list() == pytest.approx(
        original.to_list()
    )


def test_clockwise_rotation_matches_pillow_expand_coordinates() -> None:
    transform = PageTransform(100, 200, 100, 200, 90)
    assert transform.output_size == (200, 100)
    assert transform.pdf_to_raster(BBox(10, 20, 30, 60)) == BBox(140, 10, 180, 30)


def test_invalid_geometry_and_schema_are_rejected() -> None:
    with pytest.raises(GeometryError, match="x0 < x1"):
        BBox(10, 0, 5, 2)
    with pytest.raises(GeometryError, match="rotation"):
        PageTransform(100, 100, 100, 100, 45)
    data = _entities()[0].to_dict()
    data["confidence"] = 1.1
    with pytest.raises(SchemaError, match="between 0 and 1"):
        LegendEntry.from_dict(data)


def test_page_sidecars_round_trip_and_create_all_contract_files(
    tmp_path: Path,
) -> None:
    legend, candidate, symbol_type, instance, conflict = _entities()
    artifacts = PageArtifacts(
        page=1,
        legend_entries=[legend],
        symbol_candidates=[candidate],
        symbol_types=[symbol_type],
        symbol_instances=[instance],
        unclassified_symbols=[],
        unmatched_legend_entries=[],
        conflicts=[conflict],
    )
    page_dir = write_page_artifacts(tmp_path, artifacts)
    expected = {spec.filename for spec in ALL_LIST_SIDECARS} | {"summary.json"}
    assert expected <= {path.name for path in page_dir.iterdir()}
    assert (page_dir / "symbol_crops").is_dir()

    loaded = load_page_artifacts(tmp_path, 1)
    assert loaded.legend_entries == [legend]
    assert loaded.symbol_candidates == [candidate]
    assert loaded.symbol_types == [symbol_type]
    assert loaded.symbol_instances == [instance]
    assert loaded.conflicts == [conflict]
    assert loaded.summary == PageSymbolsSummary(
        page=1,
        legend_entry_count=1,
        symbol_type_count=1,
        symbol_instance_count=1,
        conflict_count=1,
    )


def test_sidecar_schema_version_is_rejected(tmp_path: Path) -> None:
    write_page_artifacts(tmp_path, PageArtifacts(page=1))
    source = page_sidecar_dir(tmp_path, 1) / "legend_entries.json"
    data = json.loads(source.read_text(encoding="utf-8"))
    data["schemaVersion"] = SYMBOLS_SCHEMA_VERSION + 1
    source.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ArtifactError, match="unsupported schemaVersion"):
        load_page_artifacts(tmp_path, 1)


def test_crop_write_is_atomic_and_rejects_traversal(tmp_path: Path) -> None:
    page_dir = page_sidecar_dir(tmp_path, 1)
    destination = write_crop(page_dir, "nested/crop.png", b"png")
    assert destination.read_bytes() == b"png"
    assert not list(destination.parent.glob("*.tmp"))
    with pytest.raises(ArtifactError, match="safe relative path"):
        write_crop(page_dir, "../outside.png", b"bad")


def test_failed_atomic_replace_preserves_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "sidecar.json"
    destination.write_bytes(b"old")

    def fail_replace(_source, _destination) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(artifacts_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        atomic_write_bytes(destination, b"new")
    assert destination.read_bytes() == b"old"
    assert not list(tmp_path.glob("*.tmp"))
