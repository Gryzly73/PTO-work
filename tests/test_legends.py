"""D1: legend section headers are not signs; УГВ ≠ дата замера."""

from __future__ import annotations

from dataclasses import dataclass

from dwg_symbols.legends import (
    _independent_groundwater_date_captions,
    _is_legend_section_header,
    detect_legend_entries,
)


@dataclass
class _Text:
    text: str
    x: float
    y: float
    height: float = 2.5
    width: float = 0.0


def _hline(y: float) -> dict[str, object]:
    return {
        "type": "line",
        "points": [(145.0, y), (175.0, y)],
        "lw": 0.4,
        "color": "#000000",
    }


def test_section_header_is_not_extracted_sign() -> None:
    assert _is_legend_section_header("Г Р А Н И Ц Ы")
    assert _is_legend_section_header("ГРАНИЦЫ")
    assert not _is_legend_section_header("стратиграфическая")
    assert not _is_legend_section_header("литологическая")


def test_borders_heading_stays_unmatched_without_vector_signature() -> None:
    heading = _Text("Условные обозначения", x=100.0, y=200.0, height=3.0)
    borders = _Text("Г Р А Н И Ц Ы", x=180.0, y=170.0, height=2.5)
    boundary = _Text("стратиграфическая", x=180.0, y=160.0, height=2.5)
    filler = _Text("Песок мелкий коричневый, aQIII", x=180.0, y=150.0, height=2.5)
    triangle = {
        "type": "line",
        "points": [(145.0, 168.0), (135.0, 168.0), (140.0, 158.0), (145.0, 168.0)],
        "lw": 0.5,
        "color": "#000000",
    }
    entries, crops, _anomalies = detect_legend_entries(
        document_id="doc",
        page=1,
        texts=[heading, borders, boundary, filler],
        primitives=[triangle],
        paper_width=420.0,
        paper_height=297.0,
    )
    by_label = {entry.label: entry for entry in entries}
    header = by_label["Г Р А Н И Ц Ы"]
    assert header.status == "unmatched"
    assert header.signature is None
    assert header.crop_path is None
    assert header.id not in crops


def test_ugv_and_date_are_independent_captions() -> None:
    glued = [
        _Text("абсолютная отметка уровня грунтовых вод, м", x=180.0, y=140.0),
        _Text("дата замера", x=180.0, y=137.0),
    ]
    assert _independent_groundwater_date_captions(glued)
    assert not _independent_groundwater_date_captions(glued[:1])
    wrapped_ige = [
        _Text("Глина коричневая, легкая, тугопластичная, с прослоями", x=180.0, y=180.0),
        _Text("суглинка текучепласт, песка мелкого, aQIII", x=180.0, y=177.0),
    ]
    assert not _independent_groundwater_date_captions(wrapped_ige)


def test_ugv_and_measurement_date_become_two_legend_rows() -> None:
    heading = _Text("Условные обозначения", x=100.0, y=220.0, height=3.0)
    ige_a = _Text(
        "Глина коричневая, легкая, тугопластичная, с прослоями",
        x=180.0,
        y=205.0,
    )
    ige_b = _Text("суглинка текучепласт, песка мелкого, aQIII", x=180.0, y=202.0)
    mouth = _Text("абс. отметка устья, м", x=180.0, y=188.0)
    sole = _Text("абс. отметка подошвы слоя, м", x=180.0, y=178.0)
    ugv = _Text("абсолютная отметка уровня грунтовых вод, м", x=180.0, y=168.0)
    date = _Text("дата замера", x=180.0, y=165.0)
    bottom = _Text("абс. отметка забоя скважины, м", x=180.0, y=152.0)
    borders = _Text("Г Р А Н И Ц Ы", x=180.0, y=140.0)
    strat = _Text("стратиграфическая", x=180.0, y=128.0)
    lith = _Text("литологическая", x=180.0, y=118.0)
    chart = _Text("график стат. зондирования", x=180.0, y=108.0)
    primitives = [
        _hline(188.0),
        _hline(178.0),
        _hline(168.0),
        _hline(165.0),
        _hline(152.0),
        _hline(128.0),
        _hline(118.0),
        _hline(108.0),
        {
            "type": "line",
            "points": [(145.0, 205.0), (175.0, 205.0), (175.0, 198.0), (145.0, 198.0)],
            "lw": 0.4,
            "color": "#000000",
        },
    ]
    entries, _crops, _anomalies = detect_legend_entries(
        document_id="doc",
        page=1,
        texts=[
            heading,
            ige_a,
            ige_b,
            mouth,
            sole,
            ugv,
            date,
            bottom,
            borders,
            strat,
            lith,
            chart,
        ],
        primitives=primitives,
        paper_width=420.0,
        paper_height=297.0,
    )
    labels = [entry.label for entry in entries]
    assert "абсолютная отметка уровня грунтовых вод, м" in labels
    assert "дата замера" in labels
    assert "абсолютная отметка уровня грунтовых вод, м дата замера" not in labels
    ugv_entry = next(
        entry
        for entry in entries
        if entry.label == "абсолютная отметка уровня грунтовых вод, м"
    )
    date_entry = next(entry for entry in entries if entry.label == "дата замера")
    assert ugv_entry.id != date_entry.id
    assert ugv_entry.source_texts == ("абсолютная отметка уровня грунтовых вод, м",)
    assert date_entry.source_texts == ("дата замера",)
    assert "абс. отметка устья, м" in labels
    assert "абс. отметка подошвы слоя, м" in labels
    assert labels.count("абс. отметка устья, м") == 1
    assert "стратиграфическая" in labels
    assert "литологическая" in labels
    assert "график стат. зондирования" in labels
    assert "Г Р А Н И Ц Ы" in labels
    ige = next(entry for entry in entries if "Глина коричневая" in entry.label)
    assert "суглинка текучепласт" in ige.label
    assert ige.source_texts == (
        "Глина коричневая, легкая, тугопластичная, с прослоями",
        "суглинка текучепласт, песка мелкого, aQIII",
    )
