"""Atomic page-sidecar persistence for symbol extraction."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
from typing import Any, Generic, Iterable, Mapping, TypeVar

from . import SYMBOLS_SCHEMA_VERSION
from .schema import (
    LegendEntry,
    PageSymbolsSummary,
    SchemaError,
    SymbolCandidate,
    SymbolConflict,
    SymbolEntity,
    SymbolInstance,
    SymbolType,
)


class ArtifactError(ValueError):
    """Malformed sidecar or unsafe artifact path."""


EntityT = TypeVar("EntityT", bound=SymbolEntity)


@dataclass(frozen=True, slots=True)
class SidecarSpec(Generic[EntityT]):
    filename: str
    kind: str
    entity_type: type[EntityT]


LEGEND_ENTRIES = SidecarSpec("legend_entries.json", "legendEntries", LegendEntry)
SYMBOL_CANDIDATES = SidecarSpec(
    "symbol_candidates.json", "symbolCandidates", SymbolCandidate
)
SYMBOL_TYPES = SidecarSpec("symbol_types.json", "symbolTypes", SymbolType)
SYMBOL_INSTANCES = SidecarSpec(
    "symbol_instances.json", "symbolInstances", SymbolInstance
)
UNCLASSIFIED_SYMBOLS = SidecarSpec(
    "unclassified_symbols.json", "unclassifiedSymbols", SymbolInstance
)
UNMATCHED_LEGEND_ENTRIES = SidecarSpec(
    "unmatched_legend_entries.json", "unmatchedLegendEntries", LegendEntry
)
CONFLICTS = SidecarSpec("conflicts.json", "conflicts", SymbolConflict)

ALL_LIST_SIDECARS = (
    LEGEND_ENTRIES,
    SYMBOL_CANDIDATES,
    SYMBOL_TYPES,
    SYMBOL_INSTANCES,
    UNCLASSIFIED_SYMBOLS,
    UNMATCHED_LEGEND_ENTRIES,
    CONFLICTS,
)


@dataclass(slots=True)
class PageArtifacts:
    page: int
    legend_entries: list[LegendEntry] = field(default_factory=list)
    symbol_candidates: list[SymbolCandidate] = field(default_factory=list)
    symbol_types: list[SymbolType] = field(default_factory=list)
    symbol_instances: list[SymbolInstance] = field(default_factory=list)
    unclassified_symbols: list[SymbolInstance] = field(default_factory=list)
    unmatched_legend_entries: list[LegendEntry] = field(default_factory=list)
    conflicts: list[SymbolConflict] = field(default_factory=list)
    summary: PageSymbolsSummary | None = None

    def __post_init__(self) -> None:
        if isinstance(self.page, bool) or not isinstance(self.page, int) or self.page < 1:
            raise ArtifactError("page must be a positive integer")
        if self.summary is not None and self.summary.page != self.page:
            raise ArtifactError("summary page does not match page artifacts")
        for entities in self._collections().values():
            for entity in entities:
                entity_page = getattr(entity, "page", self.page)
                if entity_page != self.page:
                    raise ArtifactError(
                        f"entity {getattr(entity, 'id', '?')} belongs to page "
                        f"{entity_page}, expected {self.page}"
                    )

    def _collections(self) -> dict[SidecarSpec[Any], list[Any]]:
        return {
            LEGEND_ENTRIES: self.legend_entries,
            SYMBOL_CANDIDATES: self.symbol_candidates,
            SYMBOL_TYPES: self.symbol_types,
            SYMBOL_INSTANCES: self.symbol_instances,
            UNCLASSIFIED_SYMBOLS: self.unclassified_symbols,
            UNMATCHED_LEGEND_ENTRIES: self.unmatched_legend_entries,
            CONFLICTS: self.conflicts,
        }


def page_sidecar_dir(root: str | Path, page: int) -> Path:
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ArtifactError("page must be a positive integer")
    return Path(root) / "symbols" / f"page_{page:04d}"


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def atomic_write_bytes(path: str | Path, content: bytes) -> None:
    """Durably replace one file; a failed write leaves its old value intact."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_write_json(path: str | Path, value: Mapping[str, Any]) -> None:
    atomic_write_bytes(path, _json_bytes(value))


def _sidecar_payload(
    page: int, spec: SidecarSpec[EntityT], entities: Iterable[EntityT]
) -> dict[str, Any]:
    items = list(entities)
    ids = [getattr(item, "id", None) for item in items]
    if any(item_id is not None for item_id in ids) and len(set(ids)) != len(ids):
        raise ArtifactError(f"{spec.kind} contains duplicate IDs")
    return {
        "schemaVersion": SYMBOLS_SCHEMA_VERSION,
        "page": page,
        "kind": spec.kind,
        "items": [entity.to_dict() for entity in items],
    }


def write_sidecar(
    page_dir: str | Path,
    page: int,
    spec: SidecarSpec[EntityT],
    entities: Iterable[EntityT],
) -> Path:
    destination = Path(page_dir) / spec.filename
    atomic_write_json(destination, _sidecar_payload(page, spec, entities))
    return destination


def load_sidecar(
    page_dir: str | Path, page: int, spec: SidecarSpec[EntityT]
) -> list[EntityT]:
    source = Path(page_dir) / spec.filename
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"cannot read {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise ArtifactError(f"{source}: sidecar root must be an object")
    expected_keys = {"schemaVersion", "page", "kind", "items"}
    if set(value) != expected_keys:
        raise ArtifactError(
            f"{source}: expected fields {sorted(expected_keys)}, got {sorted(value)}"
        )
    version = value["schemaVersion"]
    if version != SYMBOLS_SCHEMA_VERSION:
        raise ArtifactError(
            f"{source}: unsupported schemaVersion {version!r}; "
            f"expected {SYMBOLS_SCHEMA_VERSION}"
        )
    if value["page"] != page:
        raise ArtifactError(
            f"{source}: page {value['page']!r} does not match expected {page}"
        )
    if value["kind"] != spec.kind:
        raise ArtifactError(
            f"{source}: kind {value['kind']!r} does not match {spec.kind!r}"
        )
    if not isinstance(value["items"], list):
        raise ArtifactError(f"{source}: items must be an array")
    if any(not isinstance(item, Mapping) for item in value["items"]):
        raise ArtifactError(f"{source}: every item must be an object")
    try:
        entities = [spec.entity_type.from_dict(item) for item in value["items"]]
    except (SchemaError, TypeError) as exc:
        raise ArtifactError(f"{source}: invalid {spec.kind}: {exc}") from exc
    ids = [getattr(item, "id", None) for item in entities]
    if any(item_id is not None for item_id in ids) and len(set(ids)) != len(ids):
        raise ArtifactError(f"{source}: duplicate IDs")
    return entities


def _summary_payload(summary: PageSymbolsSummary) -> dict[str, Any]:
    return {
        "schemaVersion": SYMBOLS_SCHEMA_VERSION,
        "page": summary.page,
        "kind": PageSymbolsSummary.KIND,
        "summary": summary.to_dict(),
    }


def write_page_artifacts(root: str | Path, artifacts: PageArtifacts) -> Path:
    """Write a complete set of independently atomic page sidecars."""

    page_dir = page_sidecar_dir(root, artifacts.page)
    for spec, entities in artifacts._collections().items():
        write_sidecar(page_dir, artifacts.page, spec, entities)
    summary = artifacts.summary or PageSymbolsSummary(
        page=artifacts.page,
        legend_entry_count=len(artifacts.legend_entries),
        symbol_type_count=len(artifacts.symbol_types),
        symbol_instance_count=len(artifacts.symbol_instances),
        unclassified_count=len(artifacts.unclassified_symbols),
        unmatched_legend_entry_count=len(artifacts.unmatched_legend_entries),
        conflict_count=len(artifacts.conflicts),
    )
    atomic_write_json(page_dir / "summary.json", _summary_payload(summary))
    (page_dir / "symbol_crops").mkdir(parents=True, exist_ok=True)
    return page_dir


def load_page_artifacts(root: str | Path, page: int) -> PageArtifacts:
    page_dir = page_sidecar_dir(root, page)
    collections = {
        spec: load_sidecar(page_dir, page, spec) for spec in ALL_LIST_SIDECARS
    }
    summary_path = page_dir / "summary.json"
    try:
        summary_data = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"cannot read {summary_path}: {exc}") from exc
    if not isinstance(summary_data, dict):
        raise ArtifactError(f"{summary_path}: summary root must be an object")
    if summary_data.get("schemaVersion") != SYMBOLS_SCHEMA_VERSION:
        raise ArtifactError(
            f"{summary_path}: unsupported schemaVersion "
            f"{summary_data.get('schemaVersion')!r}; expected {SYMBOLS_SCHEMA_VERSION}"
        )
    if (
        summary_data.get("page") != page
        or summary_data.get("kind") != PageSymbolsSummary.KIND
        or not isinstance(summary_data.get("summary"), dict)
    ):
        raise ArtifactError(f"{summary_path}: invalid summary envelope")
    try:
        summary = PageSymbolsSummary.from_dict(summary_data["summary"])
    except SchemaError as exc:
        raise ArtifactError(f"{summary_path}: invalid summary: {exc}") from exc
    return PageArtifacts(
        page=page,
        legend_entries=collections[LEGEND_ENTRIES],
        symbol_candidates=collections[SYMBOL_CANDIDATES],
        symbol_types=collections[SYMBOL_TYPES],
        symbol_instances=collections[SYMBOL_INSTANCES],
        unclassified_symbols=collections[UNCLASSIFIED_SYMBOLS],
        unmatched_legend_entries=collections[UNMATCHED_LEGEND_ENTRIES],
        conflicts=collections[CONFLICTS],
        summary=summary,
    )


def write_crop(page_dir: str | Path, name: str, content: bytes) -> Path:
    """Atomically write a crop below ``symbol_crops`` without path traversal."""

    relative = PurePosixPath(name.replace("\\", "/"))
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ArtifactError("crop name must be a safe relative path")
    destination = Path(page_dir) / "symbol_crops" / Path(*relative.parts)
    atomic_write_bytes(destination, content)
    return destination
