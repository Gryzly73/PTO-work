#!/usr/bin/env python3
"""Focused, parameterized single-model bench — the /loop workhorse.

Unlike test_local_vlm_candidates.py (6 models, single run each), this runs ONE
model (default qwen3-vl-8b) on the 3 fixtures N times each, scores every run
against ground_truth/, and separates:
  - CONSISTENTLY-MISSED checks  → real weakness (a generic fix should target these)
  - FLAKY checks (pass some runs, fail others) → 8b nondeterminism, NOT a signal

This is the measurement contract for the self-improvement loop: it reports the
MEDIAN and WORST score per task across runs, so a change is only "better" if it
lifts the median/worst, not a lucky single run.

Vary one knob per experiment via flags and compare the printed median to the
baseline in LOOP_LEDGER.md. Keep changes GENERIC — do not tailor prompts to the
fixtures (the checklists are the held-out judge; never read them to target them).

Usage:
    python3 loop_bench.py --runs 2 --tag baseline
    python3 loop_bench.py --runs 3 --temp 0.0 --freq-penalty 0.4 --tag freqpen04
    python3 loop_bench.py --prompt-set plain --tag plain_prompt

Outputs to ./results_loop/<tag>/:
    <tag>.<LABEL>.md            — the median-scoring run (archived for diffing)
    <tag>.run{k}.<LABEL>.md     — every run (so you can diff run-to-run noise)
    <tag>.scores.json           — per-run scores + median/worst + missed/flaky
"""

import argparse
import base64
import json
import os
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = Path("/Users/dahaniglikovdarkhan/Documents/repos/PTO_Bannov/"
             "от_Владимира_Михайловича/tools")
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(HERE))


def _load_dotenv() -> None:
    env_path = TOOLS.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

# Prompt sets imported from the engine so the bench tracks production prompts.
from pdf_to_markdown import (  # noqa: E402
    SYSTEM_PROMPT,
    DATA_FIRST_SYSTEM_PROMPT,
)
# Scoring reused from the canonical scorer so bench and report agree.
from score_against_ground_truth import load_fixtures, score_model_on_task  # noqa: E402

HF_TOKEN = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_API_KEY")
if not HF_TOKEN:
    print("HF_TOKEN missing in .env", file=sys.stderr)
    sys.exit(1)

MODEL_ID = "Qwen/Qwen3-VL-8B-Instruct:novita"

# Per-task user prompts — GENERIC, principle-based (no fixture content). These
# mirror what the page-level pipeline asks; keep them universal across drawings.
TASK_PROMPTS = {
    "TEXT": (
        "Это страница пояснительной записки к проектной документации. "
        "Извлеки ВЕСЬ видимый текст по правилам системы-промпта. "
        "Сохрани заголовки, абзацы, нумерацию. НЕ переводи, НЕ перефразируй. "
        "Текстовый слой PDF: (пусто)"
    ),
    "TABLE": (
        "Это страница с расчётной таблицей. Извлеки таблицу так, чтобы во всех "
        "строках было одинаковое число столбцов. Сохрани все числа, единицы, "
        "заголовки точно. Многоуровневую шапку склеивай через ' — '. Итоговые "
        "строки (суммы/итоги) обязательны. НЕ округляй, НЕ меняй разделители, "
        "кириллицу НЕ латинизируй. Пустую ячейку оставь пустой, не выдумывай. "
        "Текстовый слой PDF: (пусто)"
    ),
    "DRAWING": (
        "Это чертёж/схема. Извлеки ВСЁ видимое: марки и номера (формат "
        "номер/буква, напр. 119/Г), текст штампа/легенды/выносок, оси и "
        "отметки, размерные цепи, спецификации узлов (позиция; наименование; "
        "точная марка ИЛИ «(не читается)»). НЕ выдумывай, кириллицу НЕ "
        "латинизируй. Текстовый слой PDF: (пусто)"
    ),
}

FIXTURE_IMAGE = {
    "TEXT": "01_text_only.png",
    "TABLE": "02_full_of_tables.png",
    "DRAWING": "03_big_drawing.png",
}


def encode_image(path: Path) -> str:
    return f"data:image/png;base64,{base64.b64encode(path.read_bytes()).decode()}"


def call_model(system_prompt: str, user_prompt: str, image_url: str,
               temp: float, freq_penalty: float, max_tokens: int,
               max_retries: int = 3):
    from openai import OpenAI
    client = OpenAI(base_url="https://router.huggingface.co/v1", api_key=HF_TOKEN)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": [
            {"type": "text", "text": user_prompt},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]},
    ]
    kwargs = dict(model=MODEL_ID, messages=messages,
                  temperature=temp, max_tokens=max_tokens)
    if freq_penalty:
        kwargs["frequency_penalty"] = freq_penalty
    last_err = ""
    for attempt in range(max_retries):
        t0 = time.time()
        try:
            resp = client.chat.completions.create(**kwargs)
            return resp.choices[0].message.content or "", time.time() - t0, ""
        except Exception as e:
            last_err = str(e)[:300]
            if attempt < max_retries - 1 and any(
                    s in last_err.lower()
                    for s in ("502", "503", "504", "timeout", "rate", "temporarily")):
                time.sleep(2 ** (attempt + 1))
                continue
            return "", time.time() - t0, last_err
    return "", 0.0, last_err


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=2, help="runs per fixture (≥2 to gauge noise)")
    ap.add_argument("--temp", type=float, default=0.0)
    ap.add_argument("--freq-penalty", type=float, default=0.0)
    ap.add_argument("--max-tokens", type=int, default=16000)
    ap.add_argument("--prompt-set", choices=["data-first", "plain"], default="data-first")
    ap.add_argument("--tasks", default="TEXT,TABLE,DRAWING",
                    help="comma list subset, e.g. TABLE,DRAWING")
    ap.add_argument("--tag", default="run", help="label for this config")
    args = ap.parse_args()

    system_prompt = (DATA_FIRST_SYSTEM_PROMPT if args.prompt_set == "data-first"
                     else SYSTEM_PROMPT)
    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]
    fixtures = load_fixtures()
    out_dir = HERE / "results_loop" / args.tag
    out_dir.mkdir(parents=True, exist_ok=True)

    config = dict(model=MODEL_ID, runs=args.runs, temp=args.temp,
                  freq_penalty=args.freq_penalty, max_tokens=args.max_tokens,
                  prompt_set=args.prompt_set, tasks=tasks, tag=args.tag)
    print(f"config: {json.dumps(config, ensure_ascii=False)}")

    report = {"config": config, "tasks": {}}
    for label in tasks:
        if label not in fixtures:
            print(f"  skip {label}: no fixture", file=sys.stderr)
            continue
        checklist = fixtures[label]["checklist"]
        total = len(checklist)
        img_url = encode_image(HERE / FIXTURE_IMAGE[label])
        user_prompt = TASK_PROMPTS[label]

        run_scores, run_missed, errors = [], [], []
        best_run, best_p = None, -1
        for k in range(args.runs):
            text, dt, err = call_model(system_prompt, user_prompt, img_url,
                                       args.temp, args.freq_penalty, args.max_tokens)
            run_path = out_dir / f"{args.tag}.run{k+1}.{label}.md"
            run_path.write_text(text, encoding="utf-8")
            if err:
                errors.append(err)
                print(f"  {label} run{k+1}: ERR {err[:90]}")
                continue
            passed, _, missed = score_model_on_task(run_path, checklist)
            run_scores.append(passed)
            run_missed.append(set(missed))
            if passed > best_p:
                best_p, best_run = passed, text
            print(f"  {label} run{k+1}: {passed}/{total} ({100*passed/total:.0f}%) "
                  f"{dt:.0f}s  missed: {', '.join(missed) if missed else '—'}")

        if not run_scores:
            report["tasks"][label] = {"error": "all runs failed", "errors": errors}
            continue

        # Median/worst over runs; consistently-missed vs flaky checks.
        median_p = statistics.median(run_scores)
        worst_p = min(run_scores)
        always_missed = set.intersection(*run_missed) if run_missed else set()
        ever_missed = set.union(*run_missed) if run_missed else set()
        flaky = ever_missed - always_missed
        # Archive the best run as the canonical <tag>.<LABEL>.md for diffing.
        (out_dir / f"{args.tag}.{label}.md").write_text(best_run or "", encoding="utf-8")

        report["tasks"][label] = {
            "total": total, "runs": run_scores,
            "median": median_p, "worst": worst_p, "best": best_p,
            "median_pct": round(100 * median_p / total, 1),
            "worst_pct": round(100 * worst_p / total, 1),
            "always_missed": sorted(always_missed),
            "flaky": sorted(flaky),
            "errors": errors,
        }
        print(f"  >> {label}: median {median_p}/{total} ({100*median_p/total:.0f}%) "
              f"worst {worst_p}/{total} | stable-miss: "
              f"{', '.join(sorted(always_missed)) or '—'} | flaky: "
              f"{', '.join(sorted(flaky)) or '—'}")

    # Aggregate median across tasks (the loop's headline number).
    medians = [v["median_pct"] for v in report["tasks"].values() if "median_pct" in v]
    report["avg_median_pct"] = round(sum(medians) / len(medians), 1) if medians else None
    (out_dir / f"{args.tag}.scores.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\navg median across tasks: {report['avg_median_pct']}%")
    print(f"wrote {out_dir / f'{args.tag}.scores.json'}")


if __name__ == "__main__":
    main()
