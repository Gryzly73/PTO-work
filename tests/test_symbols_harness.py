from pathlib import Path

import pytest

from symbols.gt_schema import (
    EXPECTED_STATUSES,
    INSTANCE_STATUSES,
    REGION_STATUSES,
    ROW_STATUSES,
    ValidationError,
    load_document,
    validate_document,
)
from symbols.score_gt import bbox_iou, score_documents
from symbols.validate_gt import validate_manifest

FIXTURES = Path(__file__).parents[1] / "symbols" / "fixtures"


def test_iou_identity_disjoint_and_partial() -> None:
    assert bbox_iou([0, 0, 10, 10], [0, 0, 10, 10]) == 1.0
    assert bbox_iou([0, 0, 10, 10], [20, 20, 30, 30]) == 0.0
    assert bbox_iou([0, 0, 10, 10], [5, 0, 15, 10]) == pytest.approx(1 / 3)


def test_status_vocabularies_are_separated() -> None:
    assert REGION_STATUSES == {"found", "not_found"}
    assert ROW_STATUSES == {"extracted", "text_unreadable", "unmatched"}
    assert INSTANCE_STATUSES == {
        "confirmed",
        "probable",
        "unresolved",
        "conflicting",
        "unclassified",
    }
    assert EXPECTED_STATUSES == {"annotated", "not_found", "skip"}
    assert "unmatched" not in INSTANCE_STATUSES


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda data: data.update(schemaVersion=999), "unsupported version"),
        (
            lambda data: data["pages"][0]["legend"]["rows"][0].update(
                bbox=[10, 10, 5, 20]
            ),
            "x0 < x1",
        ),
        (
            lambda data: data["pages"][0]["instances"][0].update(
                legendRowId="missing-row"
            ),
            "unknown row",
        ),
    ],
)
def test_validation_failures(mutation, message: str) -> None:
    data = load_document(FIXTURES / "synthetic.gt.json")
    mutation(data)
    with pytest.raises(ValidationError, match=message):
        validate_document(data)


def test_annotated_page_requires_found_region() -> None:
    data = load_document(FIXTURES / "synthetic.gt.json")
    data["pages"][0]["legend"]["region"].update(status="not_found", bbox=None)
    with pytest.raises(ValidationError, match="annotated page requires a found region"):
        validate_document(data)


@pytest.mark.parametrize("expected_status", ["not_found", "skip"])
def test_non_annotated_page_requires_empty_detection_results(
    expected_status: str,
) -> None:
    data = load_document(FIXTURES / "synthetic.gt.json")
    page = data["pages"][0]
    page["expectedStatus"] = expected_status
    page["legend"]["region"].update(status="not_found", bbox=None)
    with pytest.raises(ValidationError, match="not_found region requires empty rows"):
        validate_document(data)

    page["legend"]["rows"] = []
    with pytest.raises(
        ValidationError, match=f"{expected_status} page requires empty instances"
    ):
        validate_document(data)

    page["instances"] = []
    assert validate_document(data) is data


def test_row_outcomes_validate_their_payloads() -> None:
    unmatched = load_document(FIXTURES / "synthetic.gt.json")
    unmatched_page = unmatched["pages"][0]
    unmatched_page["legend"]["rows"][0]["status"] = "unmatched"
    unmatched_page["instances"] = [
        item
        for item in unmatched_page["instances"]
        if item["legendRowId"] != "row-valve"
    ]
    assert validate_document(unmatched) is unmatched

    unreadable = load_document(FIXTURES / "synthetic.gt.json")
    unreadable_page = unreadable["pages"][0]
    unreadable_page["legend"]["rows"][1].update(
        status="text_unreadable", label=None, typeId=None
    )
    unreadable_page["instances"] = [
        item
        for item in unreadable_page["instances"]
        if item["legendRowId"] != "row-sensor"
    ]
    assert validate_document(unreadable) is unreadable


@pytest.mark.parametrize("status", ["confirmed", "probable", "conflicting"])
def test_typed_instance_outcomes_are_supported(status: str) -> None:
    data = load_document(FIXTURES / "synthetic.gt.json")
    data["pages"][0]["instances"][0]["status"] = status
    assert validate_document(data) is data


@pytest.mark.parametrize("status", ["unresolved", "unclassified"])
def test_untyped_instance_outcomes_cannot_invent_type(status: str) -> None:
    data = load_document(FIXTURES / "synthetic.gt.json")
    instance = data["pages"][0]["instances"][0]
    instance.update(status=status, typeId=None, legendRowId=None)
    assert validate_document(data) is data
    instance["typeId"] = "valve-v1"
    with pytest.raises(ValidationError, match="must use null"):
        validate_document(data)


def test_confirmed_instance_type_must_agree_with_legend_row() -> None:
    data = load_document(FIXTURES / "synthetic.gt.json")
    data["pages"][0]["instances"][0]["typeId"] = "sensor-s1"
    with pytest.raises(ValidationError, match="disagrees with instance type"):
        validate_document(data)


def test_bad_prediction_is_structurally_valid_false_binding() -> None:
    prediction = load_document(FIXTURES / "synthetic.bad.prediction.json")
    assert prediction["pages"][0]["instances"][0]["legendRowId"] == "row-sensor"


def test_perfect_and_bad_prediction_metrics() -> None:
    gt = load_document(FIXTURES / "synthetic.gt.json")
    perfect = score_documents(
        gt, load_document(FIXTURES / "synthetic.perfect.prediction.json")
    )
    assert perfect["legendRows"]["recall"] == 1.0
    assert perfect["instances"] == {
        "groundTruth": 4,
        "predicted": 4,
        "matched": 4,
        "correctBindings": 4,
        "falseBindings": 0,
        "misses": 0,
        "duplicates": 0,
        "recall": 1.0,
        "bindingPrecision": 1.0,
    }

    bad = score_documents(gt, load_document(FIXTURES / "synthetic.bad.prediction.json"))
    assert bad["legendRows"]["recall"] == 0.5
    assert bad["instances"]["falseBindings"] == 1
    assert bad["instances"]["misses"] == 2
    assert bad["instances"]["duplicates"] == 1


def test_synthetic_manifest_self_check_resolves_relative_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = validate_manifest(FIXTURES / "MANIFEST.json", self_check=True)
    assert result[0]["score"]["instances"]["bindingPrecision"] == 1.0


def test_local_customer_fixtures_are_opt_in() -> None:
    manifest = FIXTURES / "MANIFEST.local.json"
    if not manifest.exists():
        pytest.skip("local customer fixture manifest is not installed")
    assert validate_manifest(manifest)

