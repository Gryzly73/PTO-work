#!/usr/bin/env python3
"""Score local-VLM outputs against the real-document hard-page test set.

This test set is built from the genuinely hard pages of the 21-PDF "Стадия П"
run — pages that errored / needed high-DPI repair / were flagged. Each page has
a `ground_truth/<fixture>.meta.json` checklist of tokens that MUST appear when
the page is transcribed correctly (see README.md for provenance + GT quality
tiers).

Usage:
  # self-check: score each reference .ref.md against its OWN checklist
  python3 score_real_testset.py --self-check

  # score a directory of model outputs (files named <fixture>.md)
  python3 score_real_testset.py path/to/outputs_dir

Matcher semantics mirror ../score_against_ground_truth.py exactly.
"""
import json, sys, glob, os, re
from pathlib import Path

HERE = Path(__file__).resolve().parent
GT_DIR = HERE / "ground_truth"


def split_pages(text: str) -> dict:
    """Split a combined pipeline md into {page_number: section_text} on '## Страница N'."""
    parts = re.split(r'(?m)^(##\s+Страница\s+\d+.*)$', text)
    pages = {}
    for i in range(1, len(parts), 2):
        m = re.search(r'Страница\s+(\d+)', parts[i])
        if m:
            pages[int(m.group(1))] = (parts[i + 1] if i + 1 < len(parts) else "")
    return pages


def run_check(check: dict, text: str) -> bool:
    """Evaluate one checklist rule against the model output text. AND across keys."""
    lo = text.lower()
    if "contains" in check and check["contains"] not in text:
        return False
    if "not_contains" in check:
        for needle in check["not_contains"]:
            if needle in text:
                return False
    if "all_contain" in check:
        for needle in check["all_contain"]:
            if needle not in text:
                return False
    if "any_contains" in check:
        if not any(n in text for n in check["any_contains"]):
            return False
    if "any_contains_ci" in check:
        if not any(n.lower() in lo for n in check["any_contains_ci"]):
            return False
    if "and_any" in check:
        if not any(n in text for n in check["and_any"]):
            return False
    return True


def score_text(checklist: dict, text: str):
    passed, missed = 0, []
    for name, check in checklist.items():
        if run_check(check, text):
            passed += 1
        else:
            missed.append(name)
    return passed, len(checklist), missed


def load_fixtures():
    fixtures = {}
    for mp in sorted(GT_DIR.glob("*.meta.json")):
        meta = json.loads(mp.read_text(encoding="utf-8"))
        fixtures[mp.stem.replace(".meta", "")] = meta
    return fixtures


def main():
    args = sys.argv[1:]
    self_check = "--self-check" in args
    combined = "--combined" in args
    args = [a for a in args if not a.startswith("--")]
    fixtures = load_fixtures()

    page_texts = {}
    if combined:
        if not args:
            print("ERROR: --combined needs a path to the pipeline md"); return 2
        order = json.loads((HERE / "order.json").read_text(encoding="utf-8"))
        full = Path(args[0]).read_text(encoding="utf-8", errors="replace")
        pages = split_pages(full)
        # map fixture -> its page text via order.json (page_index -> fixture)
        for idx, fx in order.items():
            page_texts[fx] = pages.get(int(idx), "")

    print(f"{'fixture':16} {'label':8} {'GT quality':22} {'score':>9}  missed")
    print("=" * 100)
    total_p = total_t = 0
    for fx, meta in fixtures.items():
        checklist = meta["checklist"]
        if self_check:
            src = GT_DIR / f"{fx}.ref.md"
            text = src.read_text(encoding="utf-8", errors="replace") if src.exists() else ""
        elif combined:
            text = page_texts.get(fx, "")
        else:
            if not args:
                print("ERROR: pass an outputs dir, --combined <md>, or --self-check"); return 2
            cand = Path(args[0]) / f"{fx}.md"
            text = cand.read_text(encoding="utf-8", errors="replace") if cand.exists() else ""
        p, t, missed = score_text(checklist, text)
        total_p += p; total_t += t
        pct = 100 * p / t if t else 0
        miss = ", ".join(missed[:6]) + (" …" if len(missed) > 6 else "")
        print(f"{fx:16} {meta.get('label',''):8} {meta.get('ground_truth_quality',''):22} {p:2}/{t:<2} {pct:5.1f}%  {miss}")
    print("=" * 100)
    print(f"TOTAL {total_p}/{total_t} = {100*total_p/total_t:.1f}%"
          + ("   (self-check: md→own checklist)" if self_check else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
