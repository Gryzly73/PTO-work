"""Strict ground-truth validation and exact-key scoring for the H1 harness."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .artifacts import load_items


class HarnessError(ValueError):
    """Malformed fixture manifest or prediction artifact."""


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HarnessError(f"{path}: expected non-empty string")
    return value


def _string_list(value: Any, path: str) -> list[str]:
    if not isinstance(value, list):
        raise HarnessError(f"{path}: expected array")
    result = [_string(item, f"{path}[]") for item in value]
    if len(result) != len(set(result)):
        raise HarnessError(f"{path}: duplicate values")
    return result


def validate_manifest(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise HarnessError("$: expected object")
    if data.get("schemaVersion") != 1:
        raise HarnessError("$.schemaVersion: expected 1")
    gates = data.get("qualityGates", {})
    if not isinstance(gates, dict):
        raise HarnessError("$.qualityGates: expected object")
    supported_gates = {
        "minInstanceRecall",
        "minBindingPrecision",
        "maxFalseConfirmedBindings",
    }
    unknown_gates = set(gates) - supported_gates
    if unknown_gates:
        raise HarnessError(f"$.qualityGates: unsupported gates {sorted(unknown_gates)}")
    for name in ("minInstanceRecall", "minBindingPrecision"):
        if name in gates and (
            isinstance(gates[name], bool)
            or not isinstance(gates[name], (int, float))
            or not 0 <= gates[name] <= 1
        ):
            raise HarnessError(f"$.qualityGates.{name}: expected number from 0 to 1")
    if "maxFalseConfirmedBindings" in gates and (
        isinstance(gates["maxFalseConfirmedBindings"], bool)
        or not isinstance(gates["maxFalseConfirmedBindings"], int)
        or gates["maxFalseConfirmedBindings"] < 0
    ):
        raise HarnessError(
            "$.qualityGates.maxFalseConfirmedBindings: expected non-negative integer"
        )
    fixtures = data.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise HarnessError("$.fixtures: expected non-empty array")
    fixture_ids: set[str] = set()
    for index, fixture in enumerate(fixtures):
        path = f"$.fixtures[{index}]"
        if not isinstance(fixture, dict):
            raise HarnessError(f"{path}: expected object")
        fixture_id = _string(fixture.get("id"), f"{path}.id")
        if fixture_id in fixture_ids:
            raise HarnessError(f"{path}.id: duplicate {fixture_id!r}")
        fixture_ids.add(fixture_id)
        _string(fixture.get("documentPath"), f"{path}.documentPath")
        page = fixture.get("page")
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise HarnessError(f"{path}.page: expected positive integer")
        if fixture.get("split") not in {"development", "validation", "holdout"}:
            raise HarnessError(
                f"{path}.split: expected development, validation or holdout"
            )
        expected = fixture.get("expected")
        if not isinstance(expected, dict):
            raise HarnessError(f"{path}.expected: expected object")
        _string_list(expected.get("instanceKeys", []), f"{path}.expected.instanceKeys")
        _string_list(
            expected.get("unknownInstanceKeys", []),
            f"{path}.expected.unknownInstanceKeys",
        )
        for collection in ("bindings", "relationships"):
            if not isinstance(expected.get(collection, []), list):
                raise HarnessError(f"{path}.expected.{collection}: expected array")
        for binding_index, binding in enumerate(expected.get("bindings", [])):
            binding_path = f"{path}.expected.bindings[{binding_index}]"
            if not isinstance(binding, dict):
                raise HarnessError(f"{binding_path}: expected object")
            _string(binding.get("instanceKey"), f"{binding_path}.instanceKey")
            _string(binding.get("legendLabel"), f"{binding_path}.legendLabel")
        for relation_index, relation in enumerate(expected.get("relationships", [])):
            relation_path = f"{path}.expected.relationships[{relation_index}]"
            if not isinstance(relation, dict):
                raise HarnessError(f"{relation_path}: expected object")
            _string(relation.get("sourceKey"), f"{relation_path}.sourceKey")
            _string(relation.get("targetKey"), f"{relation_path}.targetKey")
            _string(relation.get("type"), f"{relation_path}.type")
            if not isinstance(relation.get("directed"), bool):
                raise HarnessError(f"{relation_path}.directed: expected boolean")
    return data


def load_manifest(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(f"{source}: cannot load manifest: {exc}") from exc
    return validate_manifest(data)


def _key(instance: dict[str, Any]) -> str:
    return f"{instance.get('sourceSpace', '')}|{instance.get('sourceHandle', '')}"


def _prf(expected: Iterable[Any], actual: Iterable[Any]) -> dict[str, Any]:
    expected_set, actual_set = set(expected), set(actual)
    true_positive = len(expected_set & actual_set)
    false_positive = len(actual_set - expected_set)
    false_negative = len(expected_set - actual_set)
    precision = true_positive / (true_positive + false_positive) if actual_set else (
        1.0 if not expected_set else 0.0
    )
    recall = true_positive / (true_positive + false_negative) if expected_set else 1.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    return {
        "tp": true_positive,
        "fp": false_positive,
        "fn": false_negative,
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
    }


def score_fixture(fixture: dict[str, Any], page_dir: str | Path) -> dict[str, Any]:
    root = Path(page_dir)
    instances = load_items(root / "symbol_instances.json")
    legends = load_items(root / "legend_entries.json")
    bindings = load_items(root / "symbol_bindings.json")
    unknown_clusters = load_items(root / "unrecognized_symbols.json")
    relationships = load_items(root / "relationships.json")

    instance_by_id = {item["id"]: item for item in instances}
    legend_by_id = {item["id"]: item for item in legends}
    expected = fixture["expected"]

    actual_instance_keys = {_key(item) for item in instances}
    expected_instance_keys = set(expected.get("instanceKeys", []))

    unknown_ids = {
        instance_id
        for cluster in unknown_clusters
        for instance_id in cluster.get("instanceIds", [])
    }
    actual_unknown_keys = {
        _key(instance_by_id[item_id])
        for item_id in unknown_ids
        if item_id in instance_by_id
    }

    actual_bindings = set()
    false_confirmed = 0
    expected_bindings = {
        (item["instanceKey"], item["legendLabel"])
        for item in expected.get("bindings", [])
    }
    for binding in bindings:
        instance = instance_by_id.get(binding.get("instanceId"))
        legend = legend_by_id.get(binding.get("legendEntryId"))
        if not instance or not legend:
            continue
        pair = (_key(instance), legend.get("label"))
        actual_bindings.add(pair)
        if binding.get("status") == "confirmed" and pair not in expected_bindings:
            false_confirmed += 1

    actual_relationships = set()
    for relation in relationships:
        source = instance_by_id.get(relation.get("sourceId"))
        target = instance_by_id.get(relation.get("targetId"))
        if source and target:
            actual_relationships.add(
                (
                    _key(source),
                    _key(target),
                    relation.get("type"),
                    bool(relation.get("directed")),
                )
            )
    expected_relationships = {
        (
            item["sourceKey"],
            item["targetKey"],
            item["type"],
            item["directed"],
        )
        for item in expected.get("relationships", [])
    }

    return {
        "fixtureId": fixture["id"],
        "instances": _prf(expected_instance_keys, actual_instance_keys),
        "bindings": _prf(expected_bindings, actual_bindings),
        "unknownInstances": _prf(
            expected.get("unknownInstanceKeys", []), actual_unknown_keys
        ),
        "relationships": _prf(expected_relationships, actual_relationships),
        "falseConfirmedBindings": false_confirmed,
    }


def score_manifest(
    manifest: dict[str, Any], predictions_root: str | Path
) -> dict[str, Any]:
    root = Path(predictions_root)
    results = []
    for fixture in manifest["fixtures"]:
        page_dir = root / fixture["id"] / "dwg_symbols" / f"page_{fixture['page']:04d}"
        results.append(score_fixture(fixture, page_dir))
    totals: dict[str, dict[str, Any]] = {}
    for metric in ("instances", "bindings", "unknownInstances", "relationships"):
        tp = sum(item[metric]["tp"] for item in results)
        fp = sum(item[metric]["fp"] for item in results)
        fn = sum(item[metric]["fn"] for item in results)
        precision = tp / (tp + fp) if tp + fp else (1.0 if not fn else 0.0)
        recall = tp / (tp + fn) if tp + fn else 1.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0
        )
        totals[metric] = {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": round(precision, 6),
            "recall": round(recall, 6),
            "f1": round(f1, 6),
        }
    false_confirmed = sum(item["falseConfirmedBindings"] for item in results)
    gates = manifest.get("qualityGates", {})
    checks = {
        "minInstanceRecall": (
            totals["instances"]["recall"] >= gates["minInstanceRecall"]
            if "minInstanceRecall" in gates
            else True
        ),
        "minBindingPrecision": (
            totals["bindings"]["precision"] >= gates["minBindingPrecision"]
            if "minBindingPrecision" in gates
            else True
        ),
        "maxFalseConfirmedBindings": (
            false_confirmed <= gates["maxFalseConfirmedBindings"]
            if "maxFalseConfirmedBindings" in gates
            else True
        ),
    }
    return {
        "schemaVersion": 1,
        "fixtures": results,
        "totals": totals,
        "falseConfirmedBindings": false_confirmed,
        "qualityGates": {
            "passed": all(checks.values()),
            "checks": checks,
        },
    }
