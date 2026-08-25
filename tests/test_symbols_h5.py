from pathlib import Path

import pymupdf
import pytest

from symbols.artifacts import page_sidecar_dir
from symbols.pipeline import SymbolsPipelineConfig, run_page_symbols
from symbols.vlm import LegendAssistError, LegendVlmConfig, assist_page_legend


class _FakeBackend:
    def __init__(self, *, invalid: bool = False) -> None:
        self.invalid = invalid
        self.calls = 0

    @property
    def trace_info(self) -> dict:
        return {"backend": "fake", "calls": self.calls}

    def locate(self, page_image):
        self.calls += 1
        return {
            "regions": [
                {
                    "bbox": [100, 100, 900, 500],
                    "confidence": 0.92,
                }
            ]
        }

    def read_rows(self, legend_image):
        self.calls += 1
        text_x0 = 1100 if self.invalid else 400
        return {
            "rows": [
                {
                    "name": "Кран шаровый",
                    "rowBBox": [50, 100, 950, 300],
                    "textBBox": [text_x0, 110, 930, 290],
                    "confidence": 0.88,
                },
                {
                    "name": "Манометр",
                    "rowBBox": [50, 500, 950, 700],
                    "textBBox": [400, 510, 930, 690],
                    "confidence": 0.86,
                },
            ]
        }


def _silent_legend_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=400, height=300)
    page.draw_circle((90, 66), 8)
    page.draw_line((82, 66), (98, 66))
    page.draw_line((90, 58), (90, 74))
    page.draw_rect((82, 98, 98, 114))
    document.save(path)
    document.close()


def _config() -> LegendVlmConfig:
    return LegendVlmConfig(enabled=True, model="fake", max_calls_per_page=2)


def test_vlm_legend_rows_are_validated_and_persisted(tmp_path: Path) -> None:
    source = tmp_path / "silent.pdf"
    output = tmp_path / "output"
    _silent_legend_pdf(source)

    result, entries = assist_page_legend(
        source,
        page_number=1,
        output_root=output,
        config=_config(),
        backend=_FakeBackend(),
    )

    assert result.status == "found"
    assert result.detection_sources == ("vlm_region", "vlm_rows", "validated")
    assert [entry.name_raw for entry in entries] == [
        "Кран шаровый",
        "Манометр",
    ]
    assert all(entry.source_kind == "vlm_text" for entry in entries)
    assert all(entry.symbol_bbox_pdf.x1 < entry.text_bbox_pdf.x0 for entry in entries)
    page_dir = page_sidecar_dir(output, 1)
    assert (page_dir / "vlm_legend_trace.json").exists()
    assert len(list((page_dir / "symbol_crops" / "legend").glob("*.png"))) == 4


def test_malformed_vlm_geometry_is_rejected_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "silent.pdf"
    output = tmp_path / "output"
    _silent_legend_pdf(source)

    with pytest.raises(LegendAssistError):
        assist_page_legend(
            source,
            page_number=1,
            output_root=output,
            config=_config(),
            backend=_FakeBackend(invalid=True),
        )

    trace = (page_sidecar_dir(output, 1) / "vlm_legend_trace.json").read_text(
        encoding="utf-8"
    )
    assert '"status": "rejected"' in trace
    assert not (page_sidecar_dir(output, 1) / "legend_entries.json").exists()


def test_vlm_only_legend_cannot_create_confirmed_instances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "silent.pdf"
    output = tmp_path / "output"
    _silent_legend_pdf(source)
    backend = _FakeBackend()

    def fake_assist(document, *, page_number, output_root, config):
        return assist_page_legend(
            document,
            page_number=page_number,
            output_root=output_root,
            config=config,
            backend=backend,
        )

    monkeypatch.setattr("symbols.pipeline.assist_page_legend", fake_assist)
    artifacts = run_page_symbols(
        source,
        page_number=1,
        output_root=output,
        config=SymbolsPipelineConfig(legend_vlm=_config()),
    )

    assert backend.calls == 2
    assert len(artifacts.legend_entries) == 2
    assert not any(
        instance.status == "confirmed" for instance in artifacts.symbol_instances
    )
