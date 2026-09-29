"""D0: field SOLID must not take a non-soil legend sample (УГВ / отметки / ГРАНИЦЫ)."""

from __future__ import annotations

from dwg_symbols.field_geometry import _legend_is_soil_fill, collect_field_geometry
from dwg_symbols.schema import LegendEntry, PageResult


def _entry(entry_id: str, label: str, bbox: tuple[float, float, float, float], *texts: str) -> LegendEntry:
    return LegendEntry(
        id=entry_id,
        page=1,
        label=label,
        status="extracted",
        source_kind="dwg_vector_legend",
        bbox=bbox,
        symbol_bbox=bbox,
        source_texts=texts or (label,),
    )


def _page(*entries: LegendEntry) -> PageResult:
    return PageResult(
        document_id="DOC-test",
        document_path="kr1.dwg",
        page=1,
        completeness="partial",
        legend_entries=list(entries),
        sheet_zones={"drawing_field": [50.0, 50.0, 200.0, 200.0]},
    )


def _hatch(
    points: list[tuple[float, float]],
    *,
    pattern: str,
    color: str = "aci:7",
    scale: float = 1.0,
) -> dict[str, object]:
    return {
        "type": "hatch",
        "pattern": pattern,
        "pattern_scale": scale,
        "color": color,
        "layer": "Skv",
        "lw": 0.0,
        "points": points,
    }


def test_soil_fill_classifier_rejects_non_soil_rows() -> None:
    assert not _legend_is_soil_fill(
        _entry(
            "LE-ugv",
            "абсолютная отметка уровня грунтовых вод, м дата замера",
            (0.0, 0.0, 1.0, 1.0),
            "абсолютная отметка уровня грунтовых вод, м",
            "дата замера",
        )
    )
    assert not _legend_is_soil_fill(
        _entry("LE-mouth", "абс. отметка устья, м", (0.0, 0.0, 1.0, 1.0))
    )
    assert not _legend_is_soil_fill(
        _entry("LE-borders", "Г Р А Н И Ц Ы", (0.0, 0.0, 1.0, 1.0))
    )
    assert _legend_is_soil_fill(
        _entry(
            "LE-sand",
            "Песок мелкий коричневый (ниже УГВ-водонасыщенный), aQIII",
            (0.0, 0.0, 1.0, 1.0),
        )
    )


def test_solid_hatch_does_not_take_ugv_legend() -> None:
    ugv_cell = (0.0, 0.0, 20.0, 10.0)
    items = collect_field_geometry(
        _page(
            _entry(
                "LE-ugv",
                "абсолютная отметка уровня грунтовых вод, м дата замера",
                ugv_cell,
                "абсолютная отметка уровня грунтовых вод, м",
                "дата замера",
            ),
            _entry(
                "LE-sand",
                "Песок мелкий коричневый (ниже УГВ-водонасыщенный), aQIII",
                (0.0, 20.0, 20.0, 30.0),
            ),
        ),
        hatches=[
            _hatch([(1.0, 1.0), (19.0, 1.0), (19.0, 9.0), (1.0, 9.0), (1.0, 1.0)], pattern="SOLID"),
            _hatch(
                [(80.0, 80.0), (92.0, 80.0), (92.0, 120.0), (80.0, 120.0), (80.0, 80.0)],
                pattern="SOLID",
            ),
        ],
    )
    assert len(items) == 1
    assert items[0].kind == "hatch"
    assert items[0].signature == "hatch-pattern-v1:SOLID|1.0|aci:7"
    assert items[0].legend_entry_id == ""
    assert items[0].label == ""


def test_solid_hatch_does_not_take_elevation_or_borders_header() -> None:
    items = collect_field_geometry(
        _page(
            _entry("LE-mouth", "абс. отметка устья, м", (0.0, 0.0, 20.0, 8.0)),
            _entry("LE-borders", "Г Р А Н И Ц Ы", (0.0, 10.0, 20.0, 18.0)),
        ),
        hatches=[
            _hatch([(1.0, 1.0), (19.0, 1.0), (19.0, 7.0), (1.0, 7.0), (1.0, 1.0)], pattern="SOLID"),
            _hatch([(1.0, 11.0), (19.0, 11.0), (19.0, 17.0), (1.0, 17.0), (1.0, 11.0)], pattern="SOLID"),
            _hatch(
                [(80.0, 80.0), (92.0, 80.0), (92.0, 120.0), (80.0, 120.0), (80.0, 80.0)],
                pattern="SOLID",
            ),
        ],
    )
    assert len(items) == 1
    assert items[0].legend_entry_id == ""


def test_elevation_stroke_does_not_label_field_line() -> None:
    cell = (0.0, 0.0, 20.0, 8.0)
    stroke = {
        "type": "line",
        "points": [(1.0, 4.0), (19.0, 4.0)],
        "lw": 0.25,
        "layer": "Decoration",
        "color": "#000000",
    }
    field = {
        "type": "line",
        "points": [(80.0, 80.0), (80.0, 140.0)],
        "lw": 0.25,
        "layer": "Decoration",
        "color": "#000000",
    }
    items = collect_field_geometry(
        _page(_entry("LE-mouth", "абс. отметка устья, м", cell)),
        primitives=[stroke, field],
    )
    assert items
    assert all(item.legend_entry_id == "" for item in items)


def test_unique_soil_hatch_still_labels_fill() -> None:
    soil_cell = (0.0, 0.0, 20.0, 10.0)
    items = collect_field_geometry(
        _page(_entry("LE-clay", "Глина коричневая легкая, aQIII", soil_cell)),
        hatches=[
            _hatch(
                [(1.0, 1.0), (19.0, 1.0), (19.0, 9.0), (1.0, 9.0), (1.0, 1.0)],
                pattern="GR30",
                color="aci:256",
                scale=502.5,
            ),
            _hatch(
                [(80.0, 80.0), (100.0, 80.0), (100.0, 110.0), (80.0, 110.0), (80.0, 80.0)],
                pattern="GR30",
                color="aci:256",
                scale=502.5,
            ),
        ],
    )
    assert len(items) == 1
    assert items[0].legend_entry_id == "LE-clay"
    assert items[0].label.startswith("Глина коричневая")
