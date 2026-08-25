from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import random

from PIL import Image, ImageDraw
import pymupdf
import pytest

from symbols.artifacts import load_page_artifacts
from symbols.benchmark_cluster import benchmark_candidates
from symbols.candidate_detector import (
    CandidateDetectorConfig,
    detect_page_open_set,
    find_open_set_candidates,
)
from symbols.cluster import cluster_candidates
from symbols.dedupe import (
    CandidateDetection,
    bbox_iou,
    deduplicate_candidates,
)
from symbols.geometry import BBox
from symbols.gates import SymbolsBudgets
from symbols.gt_schema import load_document
from symbols.legend_layout import extract_page_legend
from symbols.pipeline import SymbolsPipelineConfig, run_page_symbols
from symbols.schema import SymbolCandidate, VisualSignature
from symbols.score_gt import bbox_coverage, score_documents
from symbols.template_matcher import match_page_templates
from symbols.visual_signature import (
    compute_visual_signature,
    signature_similarity,
)


FIXTURES = Path(__file__).parents[1] / "symbols" / "fixtures"
SYNTHETIC_VISUAL = FIXTURES / "synthetic_legend.svg"
SYNTHETIC_GT = FIXTURES / "synthetic.gt.json"


def _signature(kind: str):
    image = Image.new("L", (96, 96), 255)
    draw = ImageDraw.Draw(image)
    if kind == "x":
        draw.line((20, 20, 76, 76), fill=0, width=5)
        draw.line((76, 20, 20, 76), fill=0, width=5)
    else:
        draw.ellipse((20, 20, 76, 76), outline=0, width=5)
    return compute_visual_signature(image)


def _candidate(identifier: str, x: float, signature) -> SymbolCandidate:
    return SymbolCandidate(
        id=identifier,
        page=1,
        bbox_pdf=BBox(x, 10, x + 20, 30),
        raw_crop=f"symbol_crops/{identifier}.raw.png",
        normalized_crop=f"symbol_crops/{identifier}.normalized.png",
        source_kinds=("connected_component",),
        visual_signature=signature,
        confidence=0.8,
    )


def _candidate_noise_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=600, height=400)

    # Two repeated, multi-stroke signs that must survive primitive grouping.
    for center_x in (120, 300):
        page.draw_line((center_x - 6, 200), (center_x + 6, 200), width=0.8)
        page.draw_line((center_x, 194), (center_x, 206), width=0.8)

    # Text-adjacent geometry, dense hatching, dimensions, and a lone stroke.
    page.insert_text((42, 64), "ROOM 101", fontsize=10)
    page.draw_rect((38, 48, 82, 68), width=0.8)
    for y in range(120, 134, 2):
        page.draw_line((400, y), (430, y), width=0.5)
    page.draw_line((50, 300), (550, 300), width=0.5)
    for x in (140, 260, 380):
        page.draw_line((x - 3, 294), (x + 3, 306), width=0.5)
    page.draw_line((60, 350), (84, 350), width=0.5)

    document.save(path)
    document.close()


def test_visual_signatures_are_repeatable_and_discriminate_shapes() -> None:
    first = _signature("x")
    second = _signature("x")
    circle = _signature("circle")

    assert first == second
    assert signature_similarity(first, second) == 1.0
    assert signature_similarity(first, circle) < 0.9


def test_visual_signature_distinguishes_outline_from_solid_fill() -> None:
    outline = Image.new("L", (96, 96), 255)
    solid = Image.new("L", (96, 96), 255)
    ImageDraw.Draw(outline).ellipse((20, 20, 76, 76), outline=0, width=4)
    ImageDraw.Draw(solid).ellipse((20, 20, 76, 76), fill=0)

    outline_signature = compute_visual_signature(outline)
    solid_signature = compute_visual_signature(solid)

    assert outline_signature.method == "shape-topology-grid"
    assert outline_signature.version == 2
    assert signature_similarity(outline_signature, solid_signature) < 0.9


def test_candidate_dedupe_merges_evidence_deterministically() -> None:
    detections = [
        CandidateDetection(BBox(10, 10, 30, 30), 0.8, ("connected_component",)),
        CandidateDetection(BBox(11, 11, 31, 31), 0.9, ("vector_path",)),
        CandidateDetection(BBox(60, 10, 80, 30), 0.7, ("vector_path",)),
    ]

    forward = deduplicate_candidates(detections)
    reverse = deduplicate_candidates(reversed(detections))

    assert forward == reverse
    merged = next(item for item in forward if item.bbox_pdf.x0 < 40)
    assert merged.source_kinds == ("connected_component", "vector_path")
    assert len(forward) == 2


def test_clustering_is_deterministic_and_only_emits_repeats() -> None:
    repeated = _signature("x")
    candidates = [
        _candidate("SC-b", 40, repeated),
        _candidate("SC-single", 80, _signature("circle")),
        _candidate("SC-a", 10, repeated),
    ]

    forward = cluster_candidates(candidates)
    reverse = cluster_candidates(list(reversed(candidates)))

    assert forward == reverse
    assert len(forward) == 1
    assert forward[0].candidate_ids == ("SC-a", "SC-b")
    assert len(forward[0].instances) == 2
    assert all(item.status == "unclassified" for item in forward[0].instances)
    assert all(item.legend_entry_id is None for item in forward[0].instances)
    assert forward[0].symbol_type.matched_legend_entry_id is None


def test_cluster_backends_are_equivalent_on_reference_candidates() -> None:
    repeated = _signature("x")
    circle = _signature("circle")
    candidates = [
        _candidate("SC-x-1", 10, repeated),
        _candidate("SC-x-2", 40, repeated),
        _candidate("SC-circle-1", 70, circle),
        _candidate("SC-circle-2", 100, circle),
    ]

    brute_force = cluster_candidates(candidates, backend="brute_force")
    indexed = cluster_candidates(candidates, backend="bk_tree")

    assert indexed == brute_force


@pytest.mark.parametrize("threshold", [0.7, 0.9, 1.0])
def test_cluster_backends_are_equivalent_on_seeded_signatures(
    threshold: float,
) -> None:
    generator = random.Random(1701)
    signatures: list[VisualSignature] = []
    for _ in range(48):
        value = generator.getrandbits(1280)
        signatures.extend(
            [
                VisualSignature("shape-topology-grid", 2, f"{value:0320x}"),
                VisualSignature("shape-topology-grid", 2, f"{value:0320x}"),
            ]
        )
        mutated = value ^ (1 << generator.randrange(1280))
        signatures.append(
            VisualSignature("shape-topology-grid", 2, f"{mutated:0320x}")
        )
    candidates = [
        _candidate(f"SC-seeded-{index:03d}", float(index * 25), signature)
        for index, signature in enumerate(signatures)
    ]

    reference = cluster_candidates(
        candidates,
        backend="brute_force",
        similarity_threshold=threshold,
    )
    indexed = cluster_candidates(
        list(reversed(candidates)),
        backend="bk_tree",
        similarity_threshold=threshold,
    )

    assert indexed == reference


def test_cluster_bk_tree_falls_back_for_mixed_signature_widths() -> None:
    signatures = [
        VisualSignature("legacy", 1, "f"),
        VisualSignature("legacy", 1, "0f"),
        VisualSignature("legacy", 1, "ff"),
    ]
    candidates = [
        _candidate(f"SC-legacy-{index}", float(index * 25), signature)
        for index, signature in enumerate(signatures)
    ]

    assert cluster_candidates(candidates, backend="bk_tree") == cluster_candidates(
        candidates,
        backend="brute_force",
    )


@pytest.mark.parametrize("minimum_repeats", [1, 2, 4])
def test_cluster_backends_preserve_minimum_repeats(
    minimum_repeats: int,
) -> None:
    repeated = _signature("x")
    candidates = [
        _candidate(f"SC-repeat-{index}", float(index * 25), repeated)
        for index in range(3)
    ]

    assert cluster_candidates(
        candidates,
        backend="bk_tree",
        minimum_repeats=minimum_repeats,
    ) == cluster_candidates(
        candidates,
        backend="brute_force",
        minimum_repeats=minimum_repeats,
    )


def test_cluster_bk_tree_falls_back_for_malformed_hex() -> None:
    signatures = [
        VisualSignature("legacy", 1, "not-hex"),
        VisualSignature("legacy", 1, "0f"),
    ]
    candidates = [
        _candidate(f"SC-malformed-{index}", float(index * 25), signature)
        for index, signature in enumerate(signatures)
    ]

    assert cluster_candidates(
        candidates,
        backend="bk_tree",
        similarity_threshold=0.0,
        minimum_repeats=1,
    ) == cluster_candidates(
        candidates,
        backend="brute_force",
        similarity_threshold=0.0,
        minimum_repeats=1,
    )


def test_cluster_benchmark_reports_equivalence_and_timings() -> None:
    repeated = _signature("x")
    candidates = [
        _candidate("SC-benchmark-1", 10, repeated),
        _candidate("SC-benchmark-2", 40, repeated),
    ]

    result = benchmark_candidates(candidates, repeats=1)

    assert result["equivalent"] is True
    assert result["candidateCount"] == 2
    assert result["clusterCount"] == 1
    assert result["bruteForceSeconds"] >= 0
    assert result["bkTreeSeconds"] >= 0


def test_open_set_finds_repeated_unknown_without_negative_hits(tmp_path: Path) -> None:
    extract_page_legend(
        SYNTHETIC_VISUAL,
        page_number=1,
        output_root=tmp_path,
    )
    match_page_templates(
        SYNTHETIC_VISUAL,
        page_number=1,
        output_root=tmp_path,
    )

    artifacts = detect_page_open_set(
        SYNTHETIC_VISUAL,
        page_number=1,
        output_root=tmp_path,
    )

    assert len(artifacts.symbol_candidates) == 2
    assert len(artifacts.unclassified_symbols) == 2
    assert len({item.symbol_type_id for item in artifacts.unclassified_symbols}) == 1
    assert all(item.legend_entry_id is None for item in artifacts.unclassified_symbols)
    expected = (BBox(88, 228, 112, 252), BBox(238, 228, 262, 252))
    assert all(
        max(bbox_iou(instance.bbox_pdf, target) for instance in artifacts.unclassified_symbols)
        >= 0.7
        for target in expected
    )

    gt = load_document(SYNTHETIC_GT)
    negatives = gt["pages"][0]["negativeComponents"]
    assert all(
        bbox_coverage(instance.bbox_pdf.to_list(), negative["bbox"]) < 0.5
        for instance in artifacts.unclassified_symbols
        for negative in negatives
    )
    assert len(artifacts.symbol_instances) == 4
    reloaded = load_page_artifacts(tmp_path, 1)
    assert reloaded.unclassified_symbols == artifacts.unclassified_symbols
    assert reloaded.summary.unclassified_count == 2


def test_geometry_noise_is_rejected_before_crops_and_signatures(
    tmp_path: Path,
) -> None:
    source = tmp_path / "candidate-noise.pdf"
    output = tmp_path / "output"
    _candidate_noise_pdf(source)
    metrics: dict[str, int] = {}

    detections, _, _ = find_open_set_candidates(
        source,
        page_number=1,
        config=CandidateDetectorConfig(minimum_repeats=2),
        budgets=SymbolsBudgets(max_candidate_components=4),
        metrics=metrics,
    )

    assert len(detections) == 2
    assert all(
        max(
            bbox_iou(item.bbox_pdf, target)
            for target in (
                BBox(114, 194, 126, 206),
                BBox(294, 194, 306, 206),
            )
        )
        >= 0.5
        for item in detections
    )
    assert metrics["rawConnectedComponentCount"] > metrics["connectedComponentCount"]
    assert metrics["rawVectorPathCount"] > metrics["vectorPathCount"]
    assert metrics["filteredComponentCount"] <= 4
    assert metrics["rejectedHatchingCount"] >= 1
    assert metrics["rejectedLineNetworkCount"] >= 1
    assert metrics["rejectedSinglePrimitiveCount"] >= 1
    assert not (output / "symbols").exists()

    artifacts = detect_page_open_set(
        source,
        page_number=1,
        output_root=output,
        budgets=SymbolsBudgets(max_crop_files=4),
    )

    assert len(artifacts.symbol_candidates) == 2
    crops = list(
        (output / "symbols" / "page_0001" / "symbol_crops" / "candidates").glob(
            "*.png"
        )
    )
    assert len(crops) == 4


def test_budget_overflow_is_reported_without_silent_truncation(
    tmp_path: Path,
) -> None:
    artifacts = run_page_symbols(
        SYNTHETIC_VISUAL,
        page_number=1,
        output_root=tmp_path,
        config=SymbolsPipelineConfig(
            budgets=SymbolsBudgets(max_template_components=1)
        ),
    )

    assert artifacts.summary is not None
    assert artifacts.summary.status == "partial"
    assert artifacts.summary.anomaly_codes == (
        "template_component_budget_exceeded",
    )
    assert artifacts.symbol_candidates == []
    assert artifacts.symbol_instances == []
    page_dir = tmp_path / "symbols" / "page_0001" / "symbol_crops"
    assert not (page_dir / "instances").exists()
    assert not (page_dir / "candidates").exists()


def test_almost_empty_template_cannot_create_confirmed_instances(
    tmp_path: Path,
) -> None:
    _, entries = extract_page_legend(
        SYNTHETIC_VISUAL,
        page_number=1,
        output_root=tmp_path,
    )
    empty_template = replace(
        entries[0],
        symbol_bbox_pdf=BBox(300, 200, 310, 210),
    )

    artifacts = match_page_templates(
        SYNTHETIC_VISUAL,
        page_number=1,
        output_root=tmp_path,
        legend_entries=[empty_template],
    )

    assert artifacts.symbol_instances == []
    assert artifacts.symbol_types == []
    assert [item.id for item in artifacts.unmatched_legend_entries] == [
        empty_template.id
    ]


def test_negative_scorer_counts_false_positives_by_kind() -> None:
    ground_truth = load_document(SYNTHETIC_GT)
    prediction = deepcopy(
        load_document(FIXTURES / "synthetic.perfect.prediction.json")
    )
    prediction["pages"][0]["instances"].append(
        {
            "id": "false-text-symbol",
            "bbox": [40, 165, 80, 182],
            "status": "unclassified",
            "typeId": None,
            "legendRowId": None,
        }
    )

    metrics = score_documents(ground_truth, prediction)

    assert metrics["negatives"] == {
        "groundTruth": 3,
        "falsePositives": 1,
        "byKind": {"text": 1},
    }
    assert metrics["instances"]["duplicates"] == 1


def test_kr5_negative_fixture_is_opt_in() -> None:
    manifest_path = FIXTURES / "MANIFEST.local.json"
    if not manifest_path.exists():
        pytest.skip("local KR5 fixture manifest is not installed")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fixture = next(
        (
            item
            for item in manifest["fixtures"]
            if "KR5" in item.get("roles", []) and "negative" in item.get("roles", [])
        ),
        None,
    )
    if fixture is None:
        pytest.skip("local KR5 negative fixture is not configured")
    visual = manifest_path.parent / fixture["visual"]
    gt_path = manifest_path.parent / fixture["gt"]
    if not visual.exists() or not gt_path.exists():
        pytest.skip("local KR5 visual/GT files are not installed")

    gt = load_document(gt_path)
    detections, _, _ = find_open_set_candidates(visual, page_number=1)
    negatives = gt["pages"][0]["negativeComponents"]

    assert all(
        bbox_coverage(detection.bbox_pdf.to_list(), negative["bbox"]) < 0.5
        for detection in detections
        for negative in negatives
    )
