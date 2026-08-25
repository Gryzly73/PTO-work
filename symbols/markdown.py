"""Stable public JSON and compact Markdown projections of page sidecars."""

from __future__ import annotations

from collections import Counter
from typing import Any

from . import SYMBOLS_SCHEMA_VERSION
from .artifacts import PageArtifacts
from .schema import PageSymbolsSummary


def _summary(artifacts: PageArtifacts) -> PageSymbolsSummary:
    return artifacts.summary or PageSymbolsSummary(
        page=artifacts.page,
        legend_entry_count=len(artifacts.legend_entries),
        symbol_type_count=len(artifacts.symbol_types),
        symbol_instance_count=len(artifacts.symbol_instances),
        unclassified_count=len(artifacts.unclassified_symbols),
        unmatched_legend_entry_count=len(artifacts.unmatched_legend_entries),
        conflict_count=len(artifacts.conflicts),
    )


def page_symbols_to_dict(artifacts: PageArtifacts) -> dict[str, Any]:
    """Return the structured API projection; bbox and crop references stay intact."""

    return {
        "schemaVersion": SYMBOLS_SCHEMA_VERSION,
        "page": artifacts.page,
        "summary": _summary(artifacts).to_dict(),
        "legendEntries": [item.to_dict() for item in artifacts.legend_entries],
        "symbolCandidates": [item.to_dict() for item in artifacts.symbol_candidates],
        "symbolTypes": [item.to_dict() for item in artifacts.symbol_types],
        "symbolInstances": [item.to_dict() for item in artifacts.symbol_instances],
        "unclassifiedSymbols": [
            item.to_dict() for item in artifacts.unclassified_symbols
        ],
        "unmatchedLegendEntries": [
            item.to_dict() for item in artifacts.unmatched_legend_entries
        ],
        "conflicts": [item.to_dict() for item in artifacts.conflicts],
    }


def _bullet_lines(values: list[str], empty: str) -> list[str]:
    return [f"- {value}" for value in values] if values else [f"- {empty}"]


def page_symbols_to_markdown(artifacts: PageArtifacts) -> str:
    """Build the four client-facing result blocks required by the S8 contract."""

    entries = {item.id: item for item in artifacts.legend_entries}
    instance_counts = Counter(
        item.symbol_type_id
        for item in artifacts.symbol_instances
        if item.symbol_type_id is not None
    )

    classified: list[str] = []
    unclassified: list[str] = []
    for symbol_type in sorted(artifacts.symbol_types, key=lambda item: item.id):
        legend = entries.get(symbol_type.matched_legend_entry_id or "")
        count = instance_counts.get(symbol_type.id, 0)
        if symbol_type.status == "confirmed" and legend is not None:
            name = legend.name_raw or legend.name_normalized
            if name:
                classified.append(f"{name} — {count} экз. (`{symbol_type.id}`)")
                continue
        unclassified.append(
            f"`{symbol_type.id}` — {count} экз., статус `{symbol_type.status}`"
        )

    unmatched = [
        f"{item.name_raw or item.name_normalized or item.id} (`{item.id}`)"
        for item in sorted(
            artifacts.unmatched_legend_entries, key=lambda item: item.id
        )
    ]
    conflicts = [
        f"`{item.id}` — {item.reason}"
        for item in sorted(artifacts.conflicts, key=lambda item: item.id)
    ]

    parts = ["## Условные обозначения", "", "### Расшифровано по документу"]
    parts.extend(_bullet_lines(classified, "подтверждённых соответствий нет"))
    parts += ["", "### Нерасшифрованные типы и количества"]
    parts.extend(_bullet_lines(unclassified, "нерасшифрованных типов нет"))
    parts += ["", "### Расшифровки без экземпляров"]
    parts.extend(_bullet_lines(unmatched, "нет"))
    parts += ["", "### Конфликты"]
    parts.extend(_bullet_lines(conflicts, "нет"))
    return "\n".join(parts).rstrip() + "\n"
