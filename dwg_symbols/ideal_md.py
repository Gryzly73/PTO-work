"""Render an IDEAL-frame markdown sheet description from an existing sidecar.

Reads a reviewed page directory (no DWG conversion, no VLM). Empty slots stay
«не прочитано». Views and note bodies are not invented: missing ``sheetScenes``
yields one «лист целиком» block, not four fake cuts. The source DWG path from
sidecar ``documentPath`` is a markdown link when ``new_files/`` is in the tree.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from . import SCHEMA_VERSION
from .artifacts import load_items
from .circle_key import circled_number, legend_display_line, parse_circle_block_name
from .furniture import GEOLOGY_NO_JOIN_REASON

UNREAD = "не прочитано"
WHOLE_SHEET_TITLE = "лист целиком"
GAP_VIEWS = "виды не нарезаны"
GAP_NOTES = "примечания не читаем"
GAP_WELLS = "скважины без стыка"
GAP_FOUNDATION = "фундамент-сваи не читаем"
GAP_SKV = "номера СКВ не читаем"
GAP_TABLE = "таблица 7.2.1 не читаем"

_STAMP_ROWS = (
    ("Шифр", "code"),
    ("Стадия", "stage"),
    ("Лист", "sheet"),
    ("Листов", "sheetsTotal"),
    ("Название", "title"),
    ("Объект", "objectName"),
    ("Организация", "org"),
)

_SLOT_HEADINGS = (
    ("what", "**ЧТО изображено:**"),
    ("ofWhat", "**ИЗ ЧЕГО состоит:**"),
    ("where", "**ГДЕ расположены элементы:**"),
    ("how", "**КАК связаны:**"),
)

_SCENE_SLOT_KEYS = {
    "what": ("what",),
    "ofWhat": ("ofWhat", "parts"),
    "where": ("where",),
    "how": ("how", "related"),
}

_ROMAN_LINE = re.compile(r"([IVXLC]+)\s*[-–—−]\s*([IVXLC]+)", re.I)
_SECTION_TITLE = re.compile(r"(?i)разрез")
_SCHEME_TITLE = re.compile(r"(?i)схем\w*\s+расположен")


def resolve_page_dir(source: str | Path) -> Path:
    """Accept a page sidecar, a fixture folder, or ``summary.json``."""

    path = Path(source)
    if path.is_file():
        if path.name == "summary.json":
            return path.parent
        raise FileNotFoundError(f"{path}: expected a page directory or summary.json")
    if (path / "summary.json").is_file():
        return path
    pages = sorted(path.glob("dwg_symbols/page_*/summary.json"))
    if not pages:
        pages = sorted(path.glob("page_*/summary.json"))
    if len(pages) == 1:
        return pages[0].parent
    if len(pages) > 1:
        raise ValueError(f"{path}: multiple page sidecars; pass one page directory")
    raise FileNotFoundError(f"{path}: no summary.json page sidecar")


def default_ideal_path(source: str | Path, page_dir: Path) -> Path:
    origin = Path(source)
    if origin.is_file():
        origin = origin.parent
    if origin.is_dir() and origin.resolve() != page_dir.resolve():
        if (origin / "dwg_symbols").is_dir() or (origin / "summary.json").is_file():
            return origin / "ideal.md"
    if page_dir.parent.name == "dwg_symbols":
        return page_dir.parent.parent / "ideal.md"
    return page_dir / "ideal.md"


def render_ideal_markdown(
    source: str | Path,
    *,
    markdown_path: str | Path | None = None,
) -> str:
    """Build IDEAL-frame markdown from a page sidecar or fixture directory."""

    page_dir = resolve_page_dir(source)
    out_path = (
        Path(markdown_path)
        if markdown_path is not None
        else default_ideal_path(source, page_dir)
    )
    summary = _load_summary(page_dir)
    page = int(summary.get("page") or 1)
    legends = _optional_items(page_dir / "legend_entries.json")
    instances = _optional_items(page_dir / "symbol_instances.json")
    bindings = _optional_items(page_dir / "symbol_bindings.json")
    clusters = _optional_items(page_dir / "unrecognized_symbols.json")
    scenes = _load_scenes(page_dir, summary)
    notes = _load_notes(page_dir, summary)
    parts = [
        f"## Страница {page}",
        "",
        _drawing_line(summary.get("documentPath"), out_path, page_dir),
        "",
        _stamp_section(summary.get("titleBlock")),
        "",
        _legend_section(legends, instances, bindings),
    ]
    notes_section = _notes_section(notes)
    if notes_section:
        parts.extend(["", notes_section])
    parts.extend(
        [
            "",
            _description_section(scenes, instances, summary.get("sheetZones")),
            "",
            _gaps_section(scenes, notes, instances, clusters),
        ]
    )
    text = "\n".join(parts).rstrip() + "\n"
    return text


def write_ideal_md(source: str | Path, destination: str | Path | None = None) -> Path:
    """Write ``ideal.md`` next to the fixture, or to ``destination``."""

    page_dir = resolve_page_dir(source)
    if destination is None:
        path = default_ideal_path(source, page_dir)
    else:
        path = Path(destination)
        if path.exists() and path.is_dir():
            path = path / "ideal.md"
        elif not path.suffix:
            path = path / "ideal.md"
    markdown = render_ideal_markdown(page_dir, markdown_path=path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8", newline="\n")
    return path


def _load_summary(page_dir: Path) -> dict[str, Any]:
    path = page_dir / "summary.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: summary must be an object")
    version = payload.get("schemaVersion")
    if version is not None and version != SCHEMA_VERSION:
        raise ValueError(f"{path}: unsupported schemaVersion {version!r}")
    return payload


def _optional_items(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return load_items(path)


def _load_collection(page_dir: Path, filename: str, summary_key: str, summary: Mapping[str, Any]) -> list[Any]:
    path = page_dir / filename
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            items = payload.get("items")
            if isinstance(items, list):
                return list(items)
        if isinstance(payload, list):
            return list(payload)
        return []
    extra = summary.get(summary_key)
    if isinstance(extra, list):
        return list(extra)
    return []


def _load_scenes(page_dir: Path, summary: Mapping[str, Any]) -> list[dict[str, Any]]:
    items = _load_collection(page_dir, "sheet_scenes.json", "sheetScenes", summary)
    scenes: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, Mapping):
            scenes.append(dict(item))
        elif isinstance(item, str) and item.strip():
            scenes.append({"title": item.strip()})
    return scenes


def _load_notes(page_dir: Path, summary: Mapping[str, Any]) -> list[str]:
    items = _load_collection(page_dir, "notes_texts.json", "notesTexts", summary)
    notes: list[str] = []
    for item in items:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, Mapping):
            text = str(item.get("text") or "").strip()
        else:
            text = ""
        if text:
            notes.append(text)
    return notes


def _slot(value: Any) -> str:
    text = str(value or "").strip()
    return text if text else UNREAD


_WORK_PREFIX = "/work/"


def _repo_relative_drawing(document_path: str) -> str:
    posix = str(document_path or "").replace("\\", "/").strip()
    if posix.startswith(_WORK_PREFIX):
        posix = posix[len(_WORK_PREFIX) :]
    marker = "new_files/"
    index = posix.find(marker)
    if index >= 0:
        return posix[index:]
    return posix.lstrip("/")


def _tree_root_with_new_files(start: Path) -> Path | None:
    for parent in [start.resolve(), *start.resolve().parents]:
        if (parent / "new_files").is_dir():
            return parent
    return None


def _drawing_line(document_path: Any, from_file: Path, search_from: Path) -> str:
    raw = str(document_path or "").strip()
    if not raw:
        return f"Чертёж: `{UNREAD}`"
    name = Path(raw.replace("\\", "/")).name or UNREAD
    repo_rel = _repo_relative_drawing(raw)
    from_dir = from_file.resolve().parent
    root = _tree_root_with_new_files(from_dir) or _tree_root_with_new_files(
        search_from
    )
    if root is None or not repo_rel.startswith("new_files/"):
        return f"Чертёж: `{name}`"
    target = (root / repo_rel).resolve()
    href = Path(os.path.relpath(target, from_dir)).as_posix()
    return f"Чертёж: [`{name}`](<{href}>)"


def _stamp_section(title_block: Any) -> str:
    block = title_block if isinstance(title_block, Mapping) else {}
    rows = "\n".join(
        f"| {label} | {_slot(block.get(key))} |" for label, key in _STAMP_ROWS
    )
    return (
        "### Основная надпись\n\n"
        "| Графа | Значение |\n"
        "|---|---|\n"
        f"{rows}"
    )


def _legend_labels(
    entries: Iterable[Mapping[str, Any]],
    instances: Iterable[Mapping[str, Any]] = (),
    bindings: Iterable[Mapping[str, Any]] = (),
) -> list[str]:
    instance_list = list(instances)
    binding_list = list(bindings)
    labels: list[str] = []
    for entry in entries:
        label = legend_display_line(entry, instance_list, binding_list)
        if label:
            labels.append(label)
    return labels


def _bullet_list(items: Iterable[str]) -> str:
    lines = [f"*   {item}" for item in items]
    return "\n".join(lines) if lines else f"*   {UNREAD}"


def _legend_section(
    entries: Iterable[Mapping[str, Any]],
    instances: Iterable[Mapping[str, Any]] = (),
    bindings: Iterable[Mapping[str, Any]] = (),
) -> str:
    labels = _legend_labels(entries, instances, bindings)
    return "### УСЛОВНЫЕ ОБОЗНАЧЕНИЯ\n\n" + _bullet_list(labels)


def _notes_section(notes: list[str]) -> str:
    if not notes:
        return ""
    return "### Примечание:\n\n" + "\n\n".join(notes)


def _scene_title(scene: Mapping[str, Any]) -> str:
    title = str(scene.get("title") or "").strip()
    return title if title else UNREAD


def _id_list(scene: Mapping[str, Any], *keys: str) -> list[str]:
    for key in keys:
        value = scene.get(key)
        if isinstance(value, list):
            return [str(item) for item in value if item]
    return []


def _instances_by_id(
    instances: Iterable[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    return {str(item.get("id") or ""): item for item in instances if item.get("id")}


def _compact_span(numbers: list[int]) -> str:
    unique = sorted(set(numbers))
    if not unique:
        return ""
    runs: list[str] = []
    start = prev = unique[0]
    for number in unique[1:]:
        if number == prev + 1:
            prev = number
            continue
        runs.append(f"{start}–{prev}" if start != prev else str(start))
        start = prev = number
    runs.append(f"{start}–{prev}" if start != prev else str(start))
    return ", ".join(runs)


def _axis_parts(instances: Iterable[Mapping[str, Any]]) -> str:
    digits: list[int] = []
    letters: list[str] = []
    for item in instances:
        axis = item.get("axis") if isinstance(item.get("axis"), Mapping) else {}
        digit = str(axis.get("digit") or "").strip()
        letter = str(axis.get("letter") or "").strip()
        if digit.isdigit():
            digits.append(int(digit))
        elif letter:
            letters.append(letter)
    if digits:
        return f"оси {_compact_span(digits)}"
    unique_letters = list(dict.fromkeys(letters))
    if not unique_letters:
        return ""
    label = "ось" if len(unique_letters) == 1 else "оси"
    return f"{label} {', '.join(unique_letters)}"


def _layer_parts(instances: Iterable[Mapping[str, Any]]) -> str:
    ordered: list[int] = []
    seen: set[int] = set()
    ranked = sorted(
        instances,
        key=lambda item: (
            -float((item.get("position") or {}).get("y") or 0.0),
            float((item.get("position") or {}).get("x") or 0.0),
            str(item.get("id") or ""),
        ),
    )
    for item in ranked:
        number = parse_circle_block_name(item.get("blockName"))
        if number is None or number in seen:
            continue
        seen.add(number)
        ordered.append(number)
    if not ordered:
        return ""
    return "слои " + "".join(circled_number(number) for number in ordered)


def _cluster_parts(
    scene: Mapping[str, Any],
    instances: Mapping[str, Mapping[str, Any]],
) -> str:
    soils = [
        instances[item_id]
        for item_id in _id_list(scene, "soilIds", "soil_ids")
        if item_id in instances
    ]
    axes = [
        instances[item_id]
        for item_id in _id_list(scene, "axisIds", "axis_ids")
        if item_id in instances
    ]
    bits = [part for part in (_axis_parts(axes), _layer_parts(soils)) if part]
    return ", ".join(bits)


def _what_from_title(title: str) -> str:
    text = title.strip()
    if not text or text == UNREAD:
        return ""
    if _SCHEME_TITLE.search(text):
        return text
    match = _ROMAN_LINE.search(text)
    if _SECTION_TITLE.search(text) or match:
        if match:
            line = f"{match.group(1).upper()}-{match.group(2).upper()}"
            return f"Геологический разрез по линии {line}"
        return "Геологический разрез"
    return text


def _as_zone_bbox(value: Any) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    try:
        box = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if len(box) != 4 or box[2] < box[0] or box[3] < box[1]:
        return None
    return box  # type: ignore[return-value]


def _where_from_zones(
    scene: Mapping[str, Any],
    zones: Mapping[str, Any] | None,
) -> str:
    payload = zones if isinstance(zones, Mapping) else {}
    field = _as_zone_bbox(payload.get("drawing_field"))
    legend = _as_zone_bbox(payload.get("legend"))
    parts: list[str] = []
    if field is not None:
        parts.append("поле чертежа")
    if legend is not None:
        scene_box = _as_zone_bbox(scene.get("bbox"))
        right_of_scene = scene_box is not None and legend[0] >= scene_box[2] - 1.0
        right_of_field = field is not None and legend[0] >= (field[0] + field[2]) / 2
        parts.append("легенда справа" if right_of_scene or right_of_field else "легенда")
    return "; ".join(parts)


def _scene_slots(
    scene: Mapping[str, Any] | None,
    instances: Iterable[Mapping[str, Any]] = (),
    zones: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    payload = scene if isinstance(scene, Mapping) else {}
    by_id = _instances_by_id(instances)
    derived = {
        "what": _what_from_title(_scene_title(payload) if payload else ""),
        "ofWhat": _cluster_parts(payload, by_id),
        "where": _where_from_zones(payload, zones),
        "how": _cluster_parts(payload, by_id),
    }
    slots: dict[str, str] = {}
    for name, keys in _SCENE_SLOT_KEYS.items():
        value = ""
        for key in keys:
            value = str(payload.get(key) or "").strip()
            if value:
                break
        slots[name] = value or derived[name] or UNREAD
    return slots


def _description_block(
    title: str,
    scene: Mapping[str, Any] | None,
    instances: Iterable[Mapping[str, Any]] = (),
    zones: Mapping[str, Any] | None = None,
) -> str:
    slots = _scene_slots(scene, instances, zones)
    body = "\n\n".join(
        f"{heading}\n{slots[name]}" for name, heading in _SLOT_HEADINGS
    )
    return f"### Описание: {title}\n\n{body}"


def _description_section(
    scenes: list[dict[str, Any]],
    instances: Iterable[Mapping[str, Any]] = (),
    zones: Mapping[str, Any] | None = None,
) -> str:
    if scenes:
        blocks = [
            _description_block(_scene_title(scene), scene, instances, zones)
            for scene in scenes
        ]
    else:
        blocks = [_description_block(WHOLE_SHEET_TITLE, None)]
    return "## Описание изображения\n\n" + "\n\n".join(blocks)


def _has_geology_no_join(
    instances: Iterable[Mapping[str, Any]],
    clusters: Iterable[Mapping[str, Any]],
) -> bool:
    for item in instances:
        if str(item.get("classificationReason") or "") == GEOLOGY_NO_JOIN_REASON:
            return True
    for cluster in clusters:
        if str(cluster.get("reason") or "") == GEOLOGY_NO_JOIN_REASON:
            return True
    return False


def _gaps_section(
    scenes: list[dict[str, Any]],
    notes: list[str],
    instances: Iterable[Mapping[str, Any]],
    clusters: Iterable[Mapping[str, Any]],
) -> str:
    gaps: list[str] = []
    if not scenes:
        gaps.append(GAP_VIEWS)
    if not notes:
        gaps.append(GAP_NOTES)
    if _has_geology_no_join(instances, clusters):
        gaps.append(GAP_WELLS)
    if any(_id_list(scene, "soilIds", "soil_ids") for scene in scenes):
        gaps.append(GAP_FOUNDATION)
        gaps.append(GAP_SKV)
        gaps.append(GAP_TABLE)
    return "### Пропуски\n\n" + _bullet_list(gaps)
