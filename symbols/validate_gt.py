"""Validate symbol GT manifests and run the committed synthetic self-check."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .gt_schema import ValidationError, load_document
from .score_gt import score_documents

MANIFEST_SCHEMA_VERSION = 1


def load_manifest(path: str | Path) -> tuple[Path, dict[str, Any]]:
    manifest_path = Path(path).resolve()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"{manifest_path}: cannot read manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ValidationError(f"{manifest_path}: manifest must be an object")
    if manifest.get("schemaVersion") != MANIFEST_SCHEMA_VERSION:
        raise ValidationError(
            f"{manifest_path}: unsupported manifest schemaVersion "
            f"{manifest.get('schemaVersion')!r}; expected {MANIFEST_SCHEMA_VERSION}"
        )
    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise ValidationError(f"{manifest_path}: fixtures must be a non-empty array")
    return manifest_path.parent, manifest


def _resolved(base: Path, fixture: dict[str, Any], key: str) -> Path:
    value = fixture.get(key)
    if not isinstance(value, str) or not value:
        raise ValidationError(f"manifest fixture {fixture.get('id')!r}: missing {key}")
    path = (base / value).resolve()
    if not path.is_file():
        raise ValidationError(f"manifest fixture {fixture.get('id')!r}: missing {path}")
    return path


def validate_manifest(path: str | Path, *, self_check: bool = False) -> list[dict[str, Any]]:
    base, manifest = load_manifest(path)
    results: list[dict[str, Any]] = []
    fixture_ids: set[str] = set()
    for index, fixture in enumerate(manifest["fixtures"]):
        if not isinstance(fixture, dict):
            raise ValidationError(f"manifest fixture {index}: expected object")
        fixture_id = fixture.get("id")
        if not isinstance(fixture_id, str) or not fixture_id:
            raise ValidationError(f"manifest fixture {index}: missing id")
        if fixture_id in fixture_ids:
            raise ValidationError(f"manifest fixture {index}: duplicate id {fixture_id!r}")
        fixture_ids.add(fixture_id)
        gt = load_document(_resolved(base, fixture, "gt"))
        if "visual" in fixture:
            _resolved(base, fixture, "visual")
        result: dict[str, Any] = {"id": fixture_id, "valid": True}
        if self_check:
            prediction = load_document(_resolved(base, fixture, "prediction"))
            score = score_documents(gt, prediction)
            expected = fixture.get("selfCheckExpected")
            if expected == "perfect":
                metrics = score["instances"]
                if (
                    score["legendRows"]["recall"] != 1.0
                    or metrics["recall"] != 1.0
                    or metrics["bindingPrecision"] != 1.0
                    or metrics["duplicates"]
                    or metrics["misses"]
                    or metrics["falseBindings"]
                ):
                    raise ValidationError(
                        f"manifest fixture {fixture_id!r}: perfect self-check failed: {score}"
                    )
            result["score"] = score
        results.append(result)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "manifest",
        nargs="?",
        type=Path,
        default=Path(__file__).parent / "fixtures" / "MANIFEST.json",
    )
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args(argv)
    try:
        results = validate_manifest(args.manifest, self_check=args.self_check)
    except ValidationError as exc:
        parser.exit(2, f"validation failed: {exc}\n")
    mode = "self-check" if args.self_check else "validation"
    print(f"GT {mode} passed: {len(results)} fixture(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

