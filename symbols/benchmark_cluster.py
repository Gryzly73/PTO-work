"""Reproducible equivalence and speed gate for saved symbol candidates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from time import perf_counter
from typing import Sequence

from .artifacts import load_page_artifacts
from .cluster import cluster_candidates
from .schema import SymbolCandidate


def benchmark_candidates(
    candidates: Sequence[SymbolCandidate],
    *,
    similarity_threshold: float = 0.9,
    minimum_repeats: int = 2,
    repeats: int = 3,
) -> dict[str, object]:
    """Compare both exact backends and return best-of-N wall-clock timings."""

    if repeats < 1:
        raise ValueError("repeats must be positive")
    kwargs = {
        "similarity_threshold": similarity_threshold,
        "minimum_repeats": minimum_repeats,
    }
    reference = cluster_candidates(candidates, backend="brute_force", **kwargs)
    indexed = cluster_candidates(candidates, backend="bk_tree", **kwargs)
    equivalent = indexed == reference

    timings: dict[str, list[float]] = {"brute_force": [], "bk_tree": []}
    for _ in range(repeats):
        for backend in ("brute_force", "bk_tree"):
            started = perf_counter()
            cluster_candidates(candidates, backend=backend, **kwargs)
            timings[backend].append(perf_counter() - started)
    brute_seconds = min(timings["brute_force"])
    indexed_seconds = min(timings["bk_tree"])
    return {
        "candidateCount": len(candidates),
        "signatureCount": sum(
            candidate.visual_signature is not None for candidate in candidates
        ),
        "clusterCount": len(reference),
        "equivalent": equivalent,
        "repeats": repeats,
        "bruteForceSeconds": round(brute_seconds, 6),
        "indexedBackend": "bk_tree",
        "bkTreeSeconds": round(indexed_seconds, 6),
        "speedup": round(brute_seconds / indexed_seconds, 3)
        if indexed_seconds
        else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--page", type=int, required=True)
    parser.add_argument("--threshold", type=float, default=0.9)
    parser.add_argument("--minimum-repeats", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--minimum-speedup", type=float, default=5.0)
    args = parser.parse_args(argv)

    artifacts = load_page_artifacts(args.output_root, args.page)
    result = benchmark_candidates(
        artifacts.symbol_candidates,
        similarity_threshold=args.threshold,
        minimum_repeats=args.minimum_repeats,
        repeats=args.repeats,
    )
    result["minimumSpeedup"] = args.minimum_speedup
    passed = bool(result["equivalent"]) and (
        result["speedup"] is not None
        and float(result["speedup"]) >= args.minimum_speedup
    )
    result["passed"] = passed
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
