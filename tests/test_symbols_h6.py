from pathlib import Path

from PIL import Image, ImageDraw

from symbols.artifacts import PageArtifacts, page_sidecar_dir
from symbols.geometry import BBox
from symbols.schema import LegendEntry, SymbolConflict, SymbolInstance, SymbolType
from symbols.vlm import SymbolReviewVlmConfig, review_page_symbols


class _FakeReviewBackend:
    def __init__(self, choice: str) -> None:
        self.choice = choice
        self.calls = 0

    @property
    def trace_info(self) -> dict:
        return {"backend": "fake-review", "calls": self.calls}

    def choose(self, board, prompt):
        self.calls += 1
        assert "LE-A" in prompt and "LE-B" in prompt
        return {"choice": self.choice, "confidence": 0.84}


def _write_mark(path: Path, kind: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (96, 96), "white")
    draw = ImageDraw.Draw(image)
    if kind == "circle":
        draw.ellipse((20, 20, 76, 76), outline="black", width=5)
    else:
        draw.rectangle((20, 20, 76, 76), outline="black", width=5)
    image.save(path)


def _entry(entry_id: str, name: str, crop: str) -> LegendEntry:
    return LegendEntry(
        id=entry_id,
        page=1,
        region_id="legend",
        position=entry_id,
        name_raw=name,
        name_normalized=name.casefold(),
        bbox_pdf=BBox(10, 10, 100, 40),
        symbol_bbox_pdf=BBox(10, 10, 35, 40),
        text_bbox_pdf=BBox(40, 10, 100, 40),
        raw_crop=crop,
        normalized_crop=crop,
        source_kind="pdf_text",
        source_document="fixture.pdf",
        status="extracted",
        confidence=1.0,
    )


def _artifacts(output: Path) -> PageArtifacts:
    page_dir = page_sidecar_dir(output, 1)
    _write_mark(page_dir / "symbol_crops/legend/a.png", "circle")
    _write_mark(page_dir / "symbol_crops/legend/b.png", "square")
    _write_mark(page_dir / "symbol_crops/candidates/type.png", "circle")
    entries = [
        _entry("LE-A", "Круглый знак", "symbol_crops/legend/a.png"),
        _entry("LE-B", "Квадратный знак", "symbol_crops/legend/b.png"),
    ]
    instances = [
        SymbolInstance(
            id=f"SI-{index}",
            page=1,
            bbox_pdf=BBox(120 + index * 20, 50, 135 + index * 20, 65),
            raw_crop="symbol_crops/candidates/type.png",
            normalized_crop="symbol_crops/candidates/type.png",
            source_kinds=("connected_component",),
            symbol_type_id="ST-A",
            status="conflicting",
            geometry_confidence=0.8,
            classification_confidence=0.7,
        )
        for index in range(2)
    ]
    symbol_type = SymbolType(
        id="ST-A",
        representative_crop="symbol_crops/candidates/type.png",
        instance_ids=tuple(item.id for item in instances),
        status="conflicting",
        candidate_legend_entry_ids=("LE-A", "LE-B"),
    )
    conflict = SymbolConflict(
        id="CF-A",
        page=1,
        kind="ambiguous_legend_binding",
        entity_ids=("ST-A", *(item.id for item in instances)),
        candidate_legend_entry_ids=("LE-A", "LE-B"),
        reason="equivalent deterministic evidence",
    )
    return PageArtifacts(
        page=1,
        legend_entries=entries,
        symbol_types=[symbol_type],
        symbol_instances=instances,
        conflicts=[conflict],
    )


def _config() -> SymbolReviewVlmConfig:
    return SymbolReviewVlmConfig(
        enabled=True,
        model="fake",
        max_calls_per_page=1,
        minimum_repeats=2,
    )


def test_h6_choice_is_probable_and_never_confirmed(tmp_path: Path) -> None:
    output = tmp_path / "output"
    backend = _FakeReviewBackend("LE-A")

    reviewed, metrics = review_page_symbols(
        _artifacts(output),
        output_root=output,
        config=_config(),
        backend=backend,
    )

    assert metrics["callCount"] == 1
    assert metrics["resolvedTypeCount"] == 1
    assert reviewed.symbol_types[0].status == "probable"
    assert reviewed.symbol_types[0].matched_legend_entry_id == "LE-A"
    assert all(item.status == "probable" for item in reviewed.symbol_instances)
    assert not any(item.status == "confirmed" for item in reviewed.symbol_instances)
    assert reviewed.conflicts[0].status == "resolved"
    assert reviewed.symbol_types[0].classification_evidence[-1].kind == "vlm_review"


def test_h6_reuses_cache_without_provider_call(tmp_path: Path) -> None:
    output = tmp_path / "output"
    first_backend = _FakeReviewBackend("LE-A")
    artifacts = _artifacts(output)
    review_page_symbols(
        artifacts,
        output_root=output,
        config=_config(),
        backend=first_backend,
    )
    second_backend = _FakeReviewBackend("LE-A")

    reviewed, metrics = review_page_symbols(
        artifacts,
        output_root=output,
        config=_config(),
        backend=second_backend,
    )

    assert second_backend.calls == 0
    assert metrics["cacheHitCount"] == 1
    assert reviewed.symbol_types[0].matched_legend_entry_id == "LE-A"


def test_h6_rejects_unknown_id_and_preserves_conflict(tmp_path: Path) -> None:
    output = tmp_path / "output"

    reviewed, metrics = review_page_symbols(
        _artifacts(output),
        output_root=output,
        config=_config(),
        backend=_FakeReviewBackend("LE-UNKNOWN"),
    )

    assert metrics["rejectedResponseCount"] == 1
    assert metrics["resolvedTypeCount"] == 0
    assert reviewed.symbol_types[0].status == "conflicting"
    assert reviewed.symbol_types[0].matched_legend_entry_id is None
    assert reviewed.conflicts[0].status == "unresolved"
