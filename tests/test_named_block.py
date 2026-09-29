"""D3: PR_KV / PR_VD join by unique block name, not H4 blockdef."""

from __future__ import annotations

from dwg_symbols.ideal_md import _wells_type_labels
from dwg_symbols.named_block import (
    NAMED_BLOCK_EVIDENCE,
    field_legend_type_label,
    resolve_named_block_legend,
)
from dwg_symbols.schema import (
    Evidence,
    LegendEntry,
    PageResult,
    Point,
    SymbolBinding,
    SymbolInstance,
    UnknownSymbolCluster,
    stable_id,
)


def _entry(entry_id: str, label: str) -> LegendEntry:
    return LegendEntry(
        id=entry_id,
        page=1,
        label=label,
        status="extracted",
        source_kind="dwg_vector_legend",
        source_texts=(label,),
        confidence=0.92,
    )


def _instance(
    instance_id: str,
    block_name: str,
    *,
    status: str = "unresolved",
    legend_entry_id: str | None = None,
    reason: str | None = "GEOLOGY_NO_LEGEND_JOIN",
) -> SymbolInstance:
    return SymbolInstance(
        id=instance_id,
        page=1,
        source_kind="dwg_insert_candidate",
        status=status,
        position=Point(x=10.0, y=20.0, space="paper", units="mm"),
        source_handle=instance_id,
        source_space="modelspace",
        layer="Probe",
        signature=f"blockdef-v1:{instance_id}",
        block_name=block_name,
        role="field_candidate",
        legend_entry_id=legend_entry_id,
        classification_reason=reason if status in {"unresolved", "unclassified"} else None,
        confidence=1.0 if status == "confirmed" else 0.0,
    )


def _cluster(*instances: SymbolInstance) -> list[UnknownSymbolCluster]:
    grouped: dict[str, list[SymbolInstance]] = {}
    for item in instances:
        if item.status in {"unresolved", "unclassified"}:
            grouped.setdefault(item.signature, []).append(item)
    return [
        UnknownSymbolCluster(
            id=f"US-{index}",
            page=1,
            signature=signature,
            instance_ids=tuple(item.id for item in items),
            reason="GEOLOGY_NO_LEGEND_JOIN",
            representative_instance_id=items[0].id,
        )
        for index, (signature, items) in enumerate(sorted(grouped.items()))
    ]


def _page(*instances: SymbolInstance, entries: list[LegendEntry]) -> PageResult:
    bindings: list[SymbolBinding] = []
    for item in instances:
        if item.status == "confirmed" and item.legend_entry_id:
            bindings.append(
                SymbolBinding(
                    id=f"SB-{item.id}",
                    page=1,
                    instance_id=item.id,
                    legend_entry_id=item.legend_entry_id,
                    status="confirmed",
                    confidence=1.0,
                    evidence=(
                        Evidence(
                            kind="exact_block_definition",
                            score=1.0,
                            source_ids=(item.legend_entry_id,),
                            detail="circle H4",
                        ),
                    ),
                )
            )
    return PageResult(
        document_id="DOC-test",
        document_path="kr1.dwg",
        page=1,
        completeness="partial",
        legend_entries=list(entries),
        symbol_instances=list(instances),
        symbol_bindings=bindings,
        unknown_symbols=_cluster(*instances),
    )


def test_field_legend_type_label_drops_sample_numbers() -> None:
    assert (
        field_legend_type_label("123 образец грунта с ненарушенной структурой")
        == "образец грунта с ненарушенной структурой"
    )
    assert field_legend_type_label("329 проба воды и ее номер") == "проба воды и ее номер"
    assert field_legend_type_label("скв. 1") == "скв. 1"


def test_pr_kv_and_pr_vd_join_unique_rows() -> None:
    entries = [
        _entry("LE-skv", "скв. 1"),
        _entry("LE-kv", "123 образец грунта с ненарушенной структурой"),
        _entry("LE-tr", "435 образец грунта с нарушенной структурой"),
        _entry("LE-vd", "329 проба воды и ее номер"),
    ]
    result = resolve_named_block_legend(
        _page(
            _instance("SI-kv", "PR_KV"),
            _instance("SI-vd", "PR_VD"),
            _instance("SI-cpe", "CPE"),
            _instance("SI-tr", "PR_TR"),
            _instance(
                "SI-circle",
                "круг1",
                status="confirmed",
                legend_entry_id="LE-skv",
                reason=None,
            ),
            entries=entries,
        )
    )
    by_id = {item.id: item for item in result.symbol_instances}
    assert by_id["SI-kv"].status == "confirmed"
    assert by_id["SI-kv"].legend_entry_id == "LE-kv"
    assert by_id["SI-vd"].status == "confirmed"
    assert by_id["SI-vd"].legend_entry_id == "LE-vd"
    assert by_id["SI-cpe"].status == "unresolved"
    assert by_id["SI-cpe"].legend_entry_id is None
    assert by_id["SI-tr"].status == "unresolved"
    assert by_id["SI-circle"].status == "confirmed"
    kinds = [item.evidence[0].kind for item in result.symbol_bindings]
    assert kinds.count(NAMED_BLOCK_EVIDENCE) == 2
    assert kinds.count("exact_block_definition") == 1
    assert "123" not in result.symbol_bindings[-1].evidence[0].detail
    assert "329" not in result.symbol_bindings[-1].evidence[0].detail
    unknown_ids = {item_id for cluster in result.unknown_symbols for item_id in cluster.instance_ids}
    assert unknown_ids == {"SI-cpe", "SI-tr"}


def test_ambiguous_water_row_stays_unresolved() -> None:
    entries = [
        _entry("LE-vd-a", "329 проба воды и ее номер"),
        _entry("LE-vd-b", "проба воды повтор"),
    ]
    result = resolve_named_block_legend(
        _page(_instance("SI-vd", "PR_VD"), entries=entries)
    )
    assert result.symbol_instances[0].status == "unresolved"
    assert result.symbol_bindings == []


def test_cme_cce_cge_stay_unresolved() -> None:
    entries = [_entry("LE-skv", "скв. 1"), _entry("LE-num", "номер скважины")]
    result = resolve_named_block_legend(
        _page(
            _instance("SI-cme", "CME"),
            _instance("SI-cce", "CCE"),
            _instance("SI-cge", "CGE"),
            entries=entries,
        )
    )
    assert all(item.status == "unresolved" for item in result.symbol_instances)
    assert result.symbol_bindings == []


def test_ideal_wells_section_is_not_soil_list() -> None:
    labels = _wells_type_labels(
        [
            {"id": "LE-kv", "label": "123 образец грунта с ненарушенной структурой"},
            {"id": "LE-clay", "label": "Глина коричневая aQIII"},
        ],
        [
            {"id": "SI-kv", "legendEntryId": "LE-kv", "blockName": "PR_KV"},
            {"id": "SI-circle", "legendEntryId": "LE-clay", "blockName": "круг1"},
        ],
        [
            {
                "instanceId": "SI-kv",
                "legendEntryId": "LE-kv",
                "evidence": [{"kind": NAMED_BLOCK_EVIDENCE}],
            },
            {
                "instanceId": "SI-circle",
                "legendEntryId": "LE-clay",
                "evidence": [{"kind": "exact_block_definition"}],
            },
        ],
    )
    assert labels == ["образец грунта с ненарушенной структурой"]
    assert "123" not in " ".join(labels)
    assert "Глина" not in " ".join(labels)


def test_stable_binding_id_uses_named_evidence() -> None:
    entry_id = "LE-kv"
    instance_id = "SI-kv"
    first = stable_id("SB", "DOC-test", 1, instance_id, entry_id, NAMED_BLOCK_EVIDENCE)
    second = stable_id("SB", "DOC-test", 1, instance_id, entry_id)
    assert first != second
