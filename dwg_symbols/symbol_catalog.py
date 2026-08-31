"""Unique block-signature catalog from existing sidecars (no DWG conversion).

Groups INSERT instances by definition signature, counts application, and assigns
draft taxonomy shelves. Pipeline roles come from the current furniture
classifier; catalog sectionId is not a new sidecar role.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path
from typing import Any, Mapping

from . import SCHEMA_VERSION
from .gost_welds import WELD_GOST_REASON, WELDING_LAYERS
from .furniture import (
    AXIS_ATTRIBUTE_REASON,
    AXIS_LAYER_REASON,
    BUILDING_LABEL_BLOCK_REASON,
    CONSTRUCTION_LAYERS,
    HEIGHT_MARK_BLOCK_REASON,
    SERVICE_LAYER_REASON,
    classify_instance,
    _folded,
    _is_anonymous_name,
    _is_geology_mark,
)
from .schema import SymbolInstance, symbol_instance_from_dict


PIPELINE_ROLES = (
    "legend",
    "sheet_furniture",
    "drawing_object",
    "drawing_annotation",
    "specification_mark",
    "unknown",
)
_AXIS_REASONS = frozenset({AXIS_LAYER_REASON, AXIS_ATTRIBUTE_REASON})
_DIMENSION_REASONS = frozenset({SERVICE_LAYER_REASON, HEIGHT_MARK_BLOCK_REASON})

REBAR_LAYERS = frozenset({"armatura", "metiz"})
EQUIPMENT_LAYERS = frozenset(
    {
        "технология",
        "ар_мебель",
        "ар_сантех",
        "santehnika",
        "santeh",
        "i-furn",
    }
)
_EQUIPMENT_NAME_PREFIXES = (
    "трап",
    "m_bath_",
    "uni_taz",
    "moyka",
    "мойка",
    "m_chair",
    "chair-task",
)
_EQUIPMENT_NAME_TOKENS = ("рабочее место", "поручень")

SECTION_META: tuple[dict[str, str], ...] = (
    {
        "sectionId": "legend",
        "title": "Легенда листа/проекта",
        "pipelineRole": "legend",
        "gostCheck": "not_applicable",
    },
    {
        "sectionId": "sheet_furniture",
        "title": "Оформление листа",
        "pipelineRole": "sheet_furniture",
        "gostCheck": "worth",
    },
    {
        "sectionId": "drawing_object",
        "title": "Объект контекста",
        "pipelineRole": "drawing_object",
        "gostCheck": "not_applicable",
    },
    {
        "sectionId": "drawing_annotation",
        "title": "Аннотация оформления",
        "pipelineRole": "drawing_annotation",
        "gostCheck": "worth",
    },
    {
        "sectionId": "specification_mark",
        "title": "Марка-код",
        "pipelineRole": "specification_mark",
        "gostCheck": "not_applicable",
    },
    {
        "sectionId": "named_equipment",
        "title": "Именованное оборудование / мебель / сантехника",
        "pipelineRole": "unknown",
        "gostCheck": "not_applicable",
    },
    {
        "sectionId": "repeating_construction",
        "title": "Конструкция-повтор",
        "pipelineRole": "unknown",
        "gostCheck": "not_applicable",
    },
    {
        "sectionId": "geology_mark",
        "title": "Геознак колонки скважины",
        "pipelineRole": "unknown",
        "gostCheck": "not_applicable",
    },
    {
        "sectionId": "welding",
        "title": "Сварка",
        "pipelineRole": "unknown",
        "gostCheck": "worth",
    },
    {
        "sectionId": "rebar_fasteners",
        "title": "Арматура / метизы",
        "pipelineRole": "unknown",
        "gostCheck": "later",
    },
    {
        "sectionId": "building_code",
        "title": "Коды здания",
        "pipelineRole": "unknown",
        "gostCheck": "not_applicable",
    },
    {
        "sectionId": "slope_mark",
        "title": "Уклон",
        "pipelineRole": "unknown",
        "gostCheck": "later",
    },
    {
        "sectionId": "stamp_unclassified",
        "title": "Штамп без атрибутов",
        "pipelineRole": "unknown",
        "gostCheck": "later",
    },
    {
        "sectionId": "anonymous_other",
        "title": "Прочий безымянный блок",
        "pipelineRole": "unknown",
        "gostCheck": "not_applicable",
    },
    {
        "sectionId": "named_other",
        "title": "Прочее именованное",
        "pipelineRole": "unknown",
        "gostCheck": "not_applicable",
    },
)
_SECTION_BY_ID = {item["sectionId"]: item for item in SECTION_META}


def _is_named_equipment(name: str | None) -> bool:
    folded = _folded(name)
    if not folded or _is_anonymous_name(folded):
        return False
    if any(token in folded for token in _EQUIPMENT_NAME_TOKENS):
        return True
    return any(folded == prefix or folded.startswith(prefix) for prefix in _EQUIPMENT_NAME_PREFIXES)


def _pipeline_role(instance: SymbolInstance) -> str:
    if instance.role == "legend_exemplar" or instance.status in {"confirmed", "probable"}:
        return "legend"
    if instance.role in {
        "sheet_furniture",
        "drawing_object",
        "drawing_annotation",
        "specification_mark",
    }:
        return instance.role
    return "unknown"


def _primary_pipeline_role(roles: Mapping[str, int]) -> str:
    present = [role for role in PIPELINE_ROLES if roles.get(role)]
    return present[0] if present else "unknown"


def _most_common(counter: Counter[str]) -> str | None:
    if not counter:
        return None
    ranked = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    return ranked[0][0]


def _unknown_section(
    *,
    names: Counter[str],
    layers: Counter[str],
    geology: bool,
) -> str:
    if geology:
        return "geology_mark"
    layer_keys = {_folded(layer) for layer in layers}
    if layer_keys & WELDING_LAYERS:
        return "welding"
    if layer_keys & REBAR_LAYERS:
        return "rebar_fasteners"
    representative = _most_common(names) or ""
    folded_name = _folded(representative)
    if folded_name == "уклон" or folded_name.startswith("уклон "):
        return "slope_mark"
    if folded_name.startswith("зд-"):
        return "building_code"
    if folded_name == "штамп":
        return "stamp_unclassified"
    named_equipment = any(_is_named_equipment(name) for name in names)
    if not named_equipment and layer_keys & EQUIPMENT_LAYERS:
        named_equipment = any(not _is_anonymous_name(name) for name in names)
    if named_equipment:
        return "named_equipment"
    anonymous = all(_is_anonymous_name(name) for name in names) if names else True
    if anonymous and layer_keys & CONSTRUCTION_LAYERS:
        return "repeating_construction"
    if anonymous:
        return "anonymous_other"
    return "named_other"


def _section_id(pipeline_role: str, names: Counter[str], layers: Counter[str], geology: bool) -> str:
    layer_keys = {_folded(layer) for layer in layers}
    if pipeline_role == "drawing_annotation" and layer_keys & WELDING_LAYERS:
        return "welding"
    if pipeline_role != "unknown":
        return pipeline_role
    return _unknown_section(names=names, layers=layers, geology=geology)


def _gost_check(section_id: str, pipeline_role: str, reasons: Counter[str]) -> str:
    if reasons.get(WELD_GOST_REASON):
        return "worth"
    if pipeline_role == "drawing_annotation":
        if reasons.get(BUILDING_LABEL_BLOCK_REASON) and not any(
            reasons.get(reason) for reason in _DIMENSION_REASONS
        ):
            return "not_applicable"
        return "worth"
    if pipeline_role == "specification_mark":
        if any(reasons.get(reason) for reason in _AXIS_REASONS):
            return "worth"
        return "not_applicable"
    return _SECTION_BY_ID[section_id]["gostCheck"]


def _rationale(
    section_id: str,
    pipeline_role: str,
    gost_check: str,
    reasons: Counter[str],
) -> str:
    if pipeline_role == "legend":
        return "Пункт легенды комплекта: смысл из подписи легенды, не из ГОСТ оформления."
    if pipeline_role == "sheet_furniture":
        return "Оформление листа (штамп / рамка / подпись): графику сверять с ГОСТ 21.101."
    if pipeline_role == "drawing_object":
        return "Объект контекста по имени и слою; ГОСТ оформление не расшифровывает изделие."
    if pipeline_role == "drawing_annotation":
        if reasons.get(WELD_GOST_REASON):
            return "Сварной знак: форма совпала с таблицей ГОСТ 2.312."
        if gost_check == "not_applicable":
            return "Подпись корпуса (АБК): словарь объекта, не справочник ГОСТ."
        return "Размер / отметка как приём оформления: очередь ГОСТ, число не читаем."
    if pipeline_role == "specification_mark":
        if any(reasons.get(reason) for reason in _AXIS_REASONS):
            return "Графика кружка оси — очередь ГОСТ; буква/цифра — схема осей комплекта."
        return "Марка-код (номерация): экспликация / ведомость, не ГОСТ."
    texts = {
        "named_equipment": (
            "Именованное оборудование / мебель / сантехника: ведомость ТХ, не ГОСТ."
        ),
        "repeating_construction": (
            "Повторяющаяся конструкция (фахверк / стойки и аналоги) на безымянном блоке; "
            "не условный знак ГОСТ."
        ),
        "geology_mark": "Геознак колонки скважины: легенда ИГИ, не ГОСТ 21.101.",
        "welding": (
            "Сварка: уникальный тип в очереди ГОСТ 2.312; в конвейере остаётся unknown."
        ),
        "rebar_fasteners": "Арматура / метизы: серая зона (конструкция или типовой знак).",
        "building_code": "Код здания ЗД-*: список корпусов проекта, не ГОСТ.",
        "slope_mark": (
            "Уклон: в легенде есть уклоноуказатель, правило не трогаем; ГОСТ позже."
        ),
        "stamp_unclassified": "Имя «Штамп» без атрибутов оформления: серая зона.",
        "anonymous_other": (
            "Безымянный *U / A$: ни ГОСТ, ни спецификация сами не подпишут."
        ),
        "named_other": "Прочее именованное: в каталоге, без среза классификатором.",
    }
    return texts.get(section_id, "Черновая полка каталога.")


def _page_meta(
    instances_path: Path,
    payload: Mapping[str, Any],
    relative_cache: dict[Path, str],
) -> dict[str, Any]:
    page = int(payload.get("page") or 1)
    summary_path = instances_path.with_name("summary.json")
    document_id = ""
    document_path = ""
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        document_id = str(summary.get("documentId") or "")
        document_path = str(summary.get("documentPath") or "")
        page = int(summary.get("page") or page)
    document_dir = instances_path.parent.parent.parent
    if document_dir not in relative_cache:
        application = document_dir / "document_application.json"
        relative_path = ""
        if application.is_file():
            record = json.loads(application.read_text(encoding="utf-8"))
            relative_path = str(record.get("relativePath") or "")
        relative_cache[document_dir] = relative_path
    relative_path = relative_cache[document_dir]
    if not document_path:
        document_path = instances_path.as_posix()
    if not document_id:
        document_id = document_path
    return {
        "page": page,
        "documentId": document_id,
        "documentPath": document_path,
        "relativePath": relative_path,
    }


def catalog_symbol_types(root: str | Path) -> dict[str, Any]:
    """Build a unique-signature catalog from symbol_instances.json sidecars."""

    names: dict[str, Counter[str]] = defaultdict(Counter)
    layers: dict[str, Counter[str]] = defaultdict(Counter)
    raw_roles: dict[str, Counter[str]] = defaultdict(Counter)
    pipeline_roles: dict[str, Counter[str]] = defaultdict(Counter)
    statuses: dict[str, Counter[str]] = defaultdict(Counter)
    reasons: dict[str, Counter[str]] = defaultdict(Counter)
    geology: dict[str, bool] = defaultdict(bool)
    occurrences: dict[str, Counter[tuple[str, str, str, int]]] = defaultdict(Counter)
    page_keys: set[tuple[str, int]] = set()
    documents: set[str] = set()
    instance_count = 0

    relative_cache: dict[Path, str] = {}
    for path in sorted(Path(root).rglob("symbol_instances.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        meta = _page_meta(path, payload, relative_cache)
        page = int(meta["page"])
        page_keys.add((str(meta["documentId"]), page))
        documents.add(str(meta["documentId"]))
        location = (
            str(meta["documentId"]),
            str(meta["documentPath"]),
            str(meta["relativePath"]),
            page,
        )
        for item in payload.get("items") or []:
            instance = classify_instance(symbol_instance_from_dict(item, page=page))
            signature = instance.signature
            instance_count += 1
            names[signature][instance.block_name or ""] += 1
            layers[signature][instance.layer] += 1
            raw_roles[signature][instance.role] += 1
            pipeline_roles[signature][_pipeline_role(instance)] += 1
            statuses[signature][instance.status] += 1
            if instance.classification_reason:
                reasons[signature][instance.classification_reason] += 1
            if _is_geology_mark(instance):
                geology[signature] = True
            occurrences[signature][location] += 1

    types: list[dict[str, Any]] = []
    for signature in names:
        role = _primary_pipeline_role(pipeline_roles[signature])
        section_id = _section_id(role, names[signature], layers[signature], geology[signature])
        gost_check = _gost_check(section_id, role, reasons[signature])
        type_occurrences = [
            {
                "documentId": document_id,
                "documentPath": document_path,
                "relativePath": relative_path or None,
                "page": page,
                "count": count,
            }
            for (document_id, document_path, relative_path, page), count in sorted(
                occurrences[signature].items(),
                key=lambda item: (item[0][2] or item[0][1], item[0][3], item[0][0]),
            )
        ]
        types.append(
            {
                "signature": signature,
                "blockName": _most_common(names[signature]) or None,
                "blockNames": [name for name, _count in names[signature].most_common() if name],
                "layers": [layer for layer, _count in layers[signature].most_common()],
                "instanceCount": sum(names[signature].values()),
                "pageCount": len(occurrences[signature]),
                "documentCount": len({item[0] for item in occurrences[signature]}),
                "occurrences": type_occurrences,
                "sectionId": section_id,
                "pipelineRole": role,
                "roles": dict(sorted(raw_roles[signature].items())),
                "pipelineRoles": dict(sorted(pipeline_roles[signature].items())),
                "statuses": dict(sorted(statuses[signature].items())),
                "reasons": dict(sorted(reasons[signature].items())),
                "gostCheck": gost_check,
                "rationale": _rationale(section_id, role, gost_check, reasons[signature]),
            }
        )
    types.sort(key=lambda item: (-int(item["instanceCount"]), str(item["signature"])))

    section_counts: dict[str, dict[str, int]] = {
        item["sectionId"]: {"types": 0, "instances": 0} for item in SECTION_META
    }
    role_counts: Counter[str] = Counter()
    gost_counts: Counter[str] = Counter()
    for item in types:
        section_counts[item["sectionId"]]["types"] += 1
        section_counts[item["sectionId"]]["instances"] += int(item["instanceCount"])
        role_counts[item["pipelineRole"]] += 1
        gost_counts[item["gostCheck"]] += 1

    return {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "symbol_type_catalog",
        "source": str(root),
        "pages": len(page_keys),
        "documents": len(documents),
        "instances": instance_count,
        "uniqueTypes": len(types),
        "sections": [
            {
                **item,
                "typeCount": section_counts[item["sectionId"]]["types"],
                "instanceCount": section_counts[item["sectionId"]]["instances"],
            }
            for item in SECTION_META
        ],
        "byPipelineRole": dict(sorted(role_counts.items())),
        "byGostCheck": dict(sorted(gost_counts.items())),
        "types": types,
    }
