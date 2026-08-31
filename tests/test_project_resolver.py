"""Session 3: unique KR1 blockdef on a field INSERT can be confirmed."""

from __future__ import annotations

from dwg_symbols.project_resolver import (
    catalog_from_legend_entries,
    resolve_project_exact_blocks,
)
from dwg_symbols.schema import (
    Point,
    PageResult,
    SymbolInstance,
    UnknownSymbolCluster,
)


_BLOCK = "blockdef-v1:d6f8a191a260dc0bb57dc6aff618f1dabedd6daf83d086f9c04c8db8e223763c"
_OTHER = "blockdef-v1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_KR1 = "4 - КР/Раздел ПД №4 Часть 1 (КР1)/10.1 Жуковский 1_ (КР1)_Геологические разрезы.dwg"


def _instance(instance_id: str, signature: str, *, status: str = "unresolved") -> SymbolInstance:
    return SymbolInstance(
        id=instance_id,
        page=1,
        source_kind="dwg_insert_candidate",
        status=status,
        position=Point(x=0.0, y=0.0, space="paper", units="mm"),
        source_handle=instance_id,
        source_space="modelspace:viewport:0",
        layer="Decoration",
        signature=signature,
        block_name="круг1",
        role="field_candidate",
    )


def _page(*instances: SymbolInstance) -> PageResult:
    grouped: dict[str, list[SymbolInstance]] = {}
    for item in instances:
        if item.status in {"unresolved", "unclassified"}:
            grouped.setdefault(item.signature, []).append(item)
    return PageResult(
        document_id="DOC-test",
        document_path="igr.dwg",
        page=1,
        completeness="partial",
        anomaly_codes=["LEGEND_NOT_FOUND"],
        symbol_instances=list(instances),
        unknown_symbols=[
            UnknownSymbolCluster(
                id=f"US-{index}",
                page=1,
                signature=signature,
                instance_ids=tuple(item.id for item in items),
                reason="NO_EXACT_LEGEND_BLOCK_MATCH_H4",
                representative_instance_id=items[0].id,
            )
            for index, (signature, items) in enumerate(sorted(grouped.items()))
        ],
    )


def test_unique_catalog_blockdef_on_field_is_confirmed() -> None:
    catalog = catalog_from_legend_entries(
        {
            "items": [
                {
                    "id": "LE-clay",
                    "label": "Глина коричневая aQIII",
                    "referenceSignatures": [_BLOCK],
                },
                {
                    "id": "LE-hatch",
                    "label": "штрих без блока",
                    "referenceSignatures": [],
                },
            ]
        },
        source_file=_KR1,
    )
    result = resolve_project_exact_blocks(
        _page(
            _instance("SI-field", _BLOCK),
            _instance("SI-well", _OTHER),
        ),
        catalog,
    )
    by_id = {item.id: item for item in result.symbol_instances}
    assert by_id["SI-field"].status == "confirmed"
    assert by_id["SI-field"].role == "field_candidate"
    assert by_id["SI-well"].status == "unresolved"
    assert result.symbol_bindings[0].status == "confirmed"
    assert result.symbol_bindings[0].evidence[0].kind == "project_exact_block_definition"
    assert all(entry.source_kind == "project_legend_catalog" for entry in result.legend_entries)
    assert all(entry.role != "legend_exemplar" for entry in result.symbol_instances)


def test_conflicting_blockdef_stays_unread() -> None:
    catalog = {
        "entries": [
            {
                "id": "PLE-a",
                "label": "A",
                "status": "consistent",
                "independentSourceFileCount": 1,
                "referenceSignatures": [_BLOCK],
                "sourceFiles": [_KR1],
            },
            {
                "id": "PLE-b",
                "label": "B",
                "status": "consistent",
                "independentSourceFileCount": 1,
                "referenceSignatures": [_BLOCK],
                "sourceFiles": [_KR1],
            },
        ]
    }
    result = resolve_project_exact_blocks(_page(_instance("SI-field", _BLOCK)), catalog)
    assert result.symbol_instances[0].status == "unresolved"
    assert result.symbol_bindings == []


def test_catalog_from_legend_entries_skips_rows_without_block() -> None:
    catalog = catalog_from_legend_entries(
        {
            "items": [
                {"id": "LE-1", "label": "грунт", "referenceSignatures": [_BLOCK]},
                {"id": "LE-2", "label": "скважина", "referenceSignatures": []},
            ]
        },
        source_file=_KR1,
    )
    assert [entry["id"] for entry in catalog["entries"]] == ["LE-1"]
