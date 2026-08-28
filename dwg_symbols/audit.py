"""H0 package audit: files, units, layouts, blocks and XREF completeness."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Iterable


SUPPORTED_SUFFIXES = {".dwg", ".dxf"}
UNIT_NAMES = {
    1: "inch",
    2: "foot",
    4: "mm",
    5: "cm",
    6: "m",
    9: "micron",
    14: "dm",
    15: "dam",
    16: "hm",
}


def discover_drawings(root: str | Path) -> list[Path]:
    package_root = Path(root)
    return sorted(
        (
            path
            for path in package_root.rglob("*")
            if path.is_file() and path.suffix.casefold() in SUPPORTED_SUFFIXES
        ),
        key=lambda item: str(item).casefold(),
    )


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _file_index(files: Iterable[Path]) -> dict[str, list[Path]]:
    result: dict[str, list[Path]] = {}
    for path in files:
        result.setdefault(path.name.casefold(), []).append(path)
        result.setdefault(path.stem.casefold(), []).append(path)
    return result


def _resolve_xref(
    source: Path,
    xref_path: str,
    block_name: str,
    index: dict[str, list[Path]],
) -> list[Path]:
    candidates: list[Path] = []
    if xref_path:
        normalized = xref_path.replace("\\", "/")
        direct = source.parent / Path(normalized)
        if direct.exists():
            candidates.append(direct.resolve())
        leaf = Path(normalized).name
        candidates.extend(index.get(leaf.casefold(), []))
        candidates.extend(index.get(Path(leaf).stem.casefold(), []))
    candidates.extend(index.get(block_name.casefold(), []))
    unique = {str(item.resolve()).casefold(): item.resolve() for item in candidates}
    return sorted(unique.values(), key=lambda item: str(item).casefold())


def audit_document(
    path: str | Path,
    *,
    package_root: str | Path,
    package_files: Iterable[Path] | None = None,
    deep: bool = True,
) -> dict[str, Any]:
    source = Path(path)
    root = Path(package_root)
    base: dict[str, Any] = {
        "path": _relative(source, root),
        "extension": source.suffix.casefold(),
        "sizeBytes": source.stat().st_size,
        "status": "not_inspected" if not deep else "pending",
        "anomalyCodes": [],
    }
    if not deep:
        return base

    files = list(package_files) if package_files is not None else discover_drawings(root)
    index = _file_index(files)
    try:
        import ezdxf

        from dwg_sheets import to_dxf

        dxf_path = to_dxf(source)
        doc = ezdxf.readfile(str(dxf_path))
        unit_code = int(doc.header.get("$INSUNITS", 0) or 0)
        if unit_code == 0:
            unit_status = "unspecified"
            base["anomalyCodes"].append("UNITS_UNSPECIFIED")
        elif unit_code not in UNIT_NAMES:
            unit_status = "unsupported"
            base["anomalyCodes"].append("UNITS_UNSUPPORTED")
        else:
            unit_status = "known"

        entity_counts: Counter[str] = Counter()
        insert_count = 0
        for entity in doc.modelspace():
            kind = entity.dxftype()
            entity_counts[kind] += 1
            insert_count += int(kind == "INSERT")

        xrefs: list[dict[str, Any]] = []
        for block in doc.blocks:
            flags = int(block.block.dxf.get("flags", 0) or 0)
            if not flags & 4:
                continue
            xref_path = str(block.block.dxf.get("xref_path", "") or "")
            resolved = _resolve_xref(source, xref_path, block.name, index)
            if not resolved:
                base["anomalyCodes"].append("XREF_UNRESOLVED")
            xrefs.append(
                {
                    "blockName": block.name,
                    "declaredPath": xref_path or None,
                    "resolvedCandidates": [_relative(item, root) for item in resolved],
                    "status": "resolved" if len(resolved) == 1 else (
                        "ambiguous" if resolved else "missing"
                    ),
                }
            )
            if len(resolved) > 1:
                base["anomalyCodes"].append("XREF_AMBIGUOUS")

        base.update(
            {
                "status": "inspected",
                "convertedPath": _relative(dxf_path, root),
                "dxfVersion": str(doc.dxfversion),
                "units": {
                    "code": unit_code,
                    "name": UNIT_NAMES.get(unit_code),
                    "status": unit_status,
                },
                "layouts": [
                    layout.name for layout in doc.layouts if layout.name != "Model"
                ],
                "blockDefinitions": sum(1 for _ in doc.blocks),
                "modelspaceEntities": sum(entity_counts.values()),
                "modelspaceEntityTypes": dict(sorted(entity_counts.items())),
                "modelspaceInserts": insert_count,
                "xrefs": xrefs,
            }
        )
    except Exception as exc:
        base["status"] = "error"
        base["anomalyCodes"].append("DOCUMENT_READ_FAILED")
        base["error"] = f"{type(exc).__name__}: {exc}"
    base["anomalyCodes"] = sorted(set(base["anomalyCodes"]))
    return base


def audit_package(root: str | Path, *, deep: bool = False) -> dict[str, Any]:
    package_root = Path(root)
    drawings = discover_drawings(package_root)
    documents = [
        audit_document(
            path,
            package_root=package_root,
            package_files=drawings,
            deep=deep,
        )
        for path in drawings
    ]
    sections: Counter[str] = Counter()
    for path in drawings:
        relative = path.relative_to(package_root)
        section = relative.parts[0] if len(relative.parts) > 1 else "."
        sections[section] += 1
    anomaly_counts = Counter(
        code for document in documents for code in document["anomalyCodes"]
    )
    return {
        "schemaVersion": 1,
        "packageRoot": str(package_root.resolve()),
        "mode": "deep" if deep else "shallow",
        "summary": {
            "documents": len(documents),
            "sections": dict(sorted(sections.items())),
            "statuses": dict(Counter(item["status"] for item in documents)),
            "anomalies": dict(sorted(anomaly_counts.items())),
        },
        "documents": documents,
    }
