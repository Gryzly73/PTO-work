"""Fail-closed GOST 2.312 weld lookup by block signature.

The table is ours, not an album in the runtime. A row without ``gostCode`` is
documentation of the slice, not a match. Layer ``SVARKA`` alone never classifies.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

WELD_GOST_REASON = "WELD_GOST_SIGNATURE"
WELDING_LAYERS = frozenset({"svarka", "сварка"})
DEFAULT_TABLE_PATH = Path(__file__).with_name("gost_welds.json")

_cache: dict[str, "WeldEntry"] | None = None


@dataclass(frozen=True, slots=True)
class WeldEntry:
    signature: str
    gost_code: str
    label: str
    block_name: str = ""
    status: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.gost_code.strip())


def reset_weld_table() -> None:
    """Drop the loaded table. Tests call this after a temporary file."""

    global _cache
    _cache = None


def load_weld_table(path: str | Path | None = None) -> dict[str, WeldEntry]:
    """Load ``signature → entry``. Empty ``gostCode`` rows are kept but not applied."""

    global _cache
    source = Path(path) if path is not None else DEFAULT_TABLE_PATH
    payload = json.loads(source.read_text(encoding="utf-8"))
    entries: dict[str, WeldEntry] = {}
    for item in payload.get("welds") or []:
        signature = str(item.get("signature") or "").strip()
        if not signature:
            continue
        entries[signature] = WeldEntry(
            signature=signature,
            gost_code=str(item.get("gostCode") or "").strip(),
            label=str(item.get("label") or "").strip(),
            block_name=str(item.get("blockName") or "").strip(),
            status=str(item.get("status") or "").strip(),
        )
    _cache = entries
    return entries


def weld_table() -> dict[str, WeldEntry]:
    if _cache is None:
        return load_weld_table()
    return _cache


def weld_entry(signature: str) -> WeldEntry | None:
    return weld_table().get(signature)


def weld_reason(instance: Any, table: Mapping[str, WeldEntry] | None = None) -> str | None:
    """Return ``WELD_GOST_SIGNATURE`` when layer and enabled table row match."""

    layer = str(getattr(instance, "layer", "") or "").strip().casefold().replace("ё", "е")
    if layer not in WELDING_LAYERS:
        return None
    signature = str(getattr(instance, "signature", "") or "")
    entry = (table or weld_table()).get(signature)
    if entry is None or not entry.enabled:
        return None
    return WELD_GOST_REASON
