import json
from pathlib import Path

from symbols.baseline import deterministic_projection
from symbols.candidate_detector import CandidateDetectorConfig
from symbols.pipeline import run_page_symbols


ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "symbols" / "fixtures"
BASELINES = FIXTURES / "baselines"
SYNTHETIC_VISUAL = FIXTURES / "synthetic_legend.svg"


def _load_baseline(name: str) -> dict:
    return json.loads((BASELINES / name).read_text(encoding="utf-8"))


def test_reference_clustering_backend_is_the_production_default() -> None:
    assert CandidateDetectorConfig().clustering_backend == "brute_force"


def test_synthetic_profile_is_reproducible(tmp_path: Path) -> None:
    expected = _load_baseline("synthetic.baseline.json")
    profile_path = tmp_path / "synthetic.baseline.json"

    run_page_symbols(
        SYNTHETIC_VISUAL,
        page_number=1,
        output_root=tmp_path,
        profile_output=profile_path,
        profile_fixture_id="synthetic-two-row-legend",
    )
    actual = json.loads(profile_path.read_text(encoding="utf-8"))

    assert deterministic_projection(actual) == deterministic_projection(expected)
    assert actual["reproducibilityDigest"] == expected["reproducibilityDigest"]
    assert all(
        actual["stages"][stage]["elapsedMs"] >= 0
        for stage in ("s3", "s4", "s5", "s6")
    )
    assert actual["stages"]["s5"]["clusterElapsedMs"] >= 0
