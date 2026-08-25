"""IoU-based scoring for symbol GT; independent of legacy token scorers."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .gt_schema import load_document


def bbox_iou(left: list[float], right: list[float]) -> float:
    x0, y0 = max(left[0], right[0]), max(left[1], right[1])
    x1, y1 = min(left[2], right[2]), min(left[3], right[3])
    intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    left_area = (left[2] - left[0]) * (left[3] - left[1])
    right_area = (right[2] - right[0]) * (right[3] - right[1])
    union = left_area + right_area - intersection
    return intersection / union if union else 0.0


def bbox_coverage(target: list[float], region: list[float]) -> float:
    """Return the fraction of a predicted target covered by an exclusion region."""

    x0, y0 = max(target[0], region[0]), max(target[1], region[1])
    x1, y1 = min(target[2], region[2]), min(target[3], region[3])
    intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    target_area = (target[2] - target[0]) * (target[3] - target[1])
    return intersection / target_area if target_area else 0.0


def _by_page(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {page["pageId"]: page for page in document["pages"]}


def _bbox_match_count(
    expected: list[dict[str, Any]],
    predicted: list[dict[str, Any]],
    threshold: float,
) -> int:
    candidates = sorted(
        (
            (bbox_iou(left["bbox"], right["bbox"]), left_index, right_index)
            for left_index, left in enumerate(expected)
            for right_index, right in enumerate(predicted)
        ),
        reverse=True,
    )
    used_left: set[int] = set()
    used_right: set[int] = set()
    for overlap, left_index, right_index in candidates:
        if overlap < threshold:
            break
        if left_index not in used_left and right_index not in used_right:
            used_left.add(left_index)
            used_right.add(right_index)
    return len(used_left)


def score_documents(
    ground_truth: dict[str, Any],
    prediction: dict[str, Any],
    *,
    iou_threshold: float = 0.5,
) -> dict[str, Any]:
    gt_pages, pred_pages = _by_page(ground_truth), _by_page(prediction)
    gt_regions = {
        page_id: page["legend"]["region"]
        for page_id, page in gt_pages.items()
        if page["legend"]["region"]["status"] == "found"
    }
    predicted_regions = {
        page_id: page["legend"]["region"]
        for page_id, page in pred_pages.items()
        if page["legend"]["region"]["status"] == "found"
    }
    matched_regions = sum(
        1
        for page_id, region in gt_regions.items()
        if page_id in predicted_regions
        and bbox_iou(region["bbox"], predicted_regions[page_id]["bbox"]) >= iou_threshold
    )
    gt_row_count = sum(len(page["legend"]["rows"]) for page in gt_pages.values())
    found_rows = sum(
        _bbox_match_count(
            gt_page["legend"]["rows"],
            pred_pages.get(page_id, {}).get("legend", {}).get("rows", []),
            iou_threshold,
        )
        for page_id, gt_page in gt_pages.items()
    )

    matches = misses = duplicates = false_bindings = 0
    negative_false_positives = 0
    negative_by_kind: dict[str, int] = {}
    negative_ground_truth = 0
    prediction_count = 0
    for page_id, gt_page in gt_pages.items():
        gt_instances = gt_page["instances"]
        pred_instances = pred_pages.get(page_id, {}).get("instances", [])
        prediction_count += len(pred_instances)
        negatives = gt_page.get("negativeComponents", [])
        negative_ground_truth += len(negatives)
        for predicted in pred_instances:
            hit_kinds = {
                negative["kind"]
                for negative in negatives
                if bbox_coverage(predicted["bbox"], negative["bbox"]) >= iou_threshold
            }
            if hit_kinds:
                negative_false_positives += 1
                for kind in hit_kinds:
                    negative_by_kind[kind] = negative_by_kind.get(kind, 0) + 1
        candidates: list[tuple[float, int, int]] = []
        for gt_index, gt_instance in enumerate(gt_instances):
            for pred_index, pred_instance in enumerate(pred_instances):
                overlap = bbox_iou(gt_instance["bbox"], pred_instance["bbox"])
                if overlap >= iou_threshold:
                    candidates.append((overlap, gt_index, pred_index))
        used_gt: set[int] = set()
        used_pred: set[int] = set()
        for _, gt_index, pred_index in sorted(candidates, reverse=True):
            if gt_index in used_gt or pred_index in used_pred:
                continue
            used_gt.add(gt_index)
            used_pred.add(pred_index)
            matches += 1
            if (
                gt_instances[gt_index].get("typeId")
                != pred_instances[pred_index].get("typeId")
            ):
                false_bindings += 1
        misses += len(gt_instances) - len(used_gt)
        duplicates += len(pred_instances) - len(used_pred)

    for page_id, pred_page in pred_pages.items():
        if page_id not in gt_pages:
            extra = len(pred_page["instances"])
            prediction_count += extra
            duplicates += extra

    gt_instance_count = sum(len(page["instances"]) for page in gt_pages.values())
    correct_bindings = matches - false_bindings
    return {
        "iouThreshold": iou_threshold,
        "legendRegions": {
            "groundTruth": len(gt_regions),
            "predicted": len(predicted_regions),
            "matched": matched_regions,
            "recall": matched_regions / len(gt_regions) if gt_regions else 1.0,
            "precision": (
                matched_regions / len(predicted_regions)
                if predicted_regions
                else (1.0 if not gt_regions else 0.0)
            ),
        },
        "legendRows": {
            "groundTruth": gt_row_count,
            "found": found_rows,
            "recall": found_rows / gt_row_count if gt_row_count else 1.0,
        },
        "instances": {
            "groundTruth": gt_instance_count,
            "predicted": prediction_count,
            "matched": matches,
            "correctBindings": correct_bindings,
            "falseBindings": false_bindings,
            "misses": misses,
            "duplicates": duplicates,
            "recall": matches / gt_instance_count if gt_instance_count else 1.0,
            "bindingPrecision": (
                correct_bindings / matches if matches else (1.0 if not gt_instance_count else 0.0)
            ),
        },
        "negatives": {
            "groundTruth": negative_ground_truth,
            "falsePositives": negative_false_positives,
            "byKind": dict(sorted(negative_by_kind.items())),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ground_truth", type=Path)
    parser.add_argument("prediction", type=Path)
    parser.add_argument("--iou", type=float, default=0.5)
    args = parser.parse_args(argv)
    if not 0.0 < args.iou <= 1.0:
        parser.error("--iou must be in (0, 1]")
    result = score_documents(
        load_document(args.ground_truth),
        load_document(args.prediction),
        iou_threshold=args.iou,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

