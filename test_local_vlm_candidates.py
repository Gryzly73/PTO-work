#!/usr/bin/env python3
"""Single-call VLM benchmark on 3 fixture PNGs — picks the best candidate for
LOCAL deployment on a 48GB M4 Pro.

Why single-call (vs the tiled pipeline in tools/test_multiprovider.py):
- We're rating raw VLM ability on a whole page, not the pipeline.
- Tiling 9x7 per page × 6 models × 3 images blew the HF free tier in seconds.
- Local MLX inference is always single-call anyway — that's the realistic measure.

What gets compared:
- 5 small/mid VLMs that fit in 48GB at 4-bit (the local candidates)
- 1 big cloud-only reference so we see the local-vs-frontier gap

Each model gets the same SYSTEM_PROMPT (from pdf_to_markdown.py — Russian,
exact extraction) and the same per-task user prompt.

Outputs to ./results_local_bench/:
  - <model>.<task>.md       — verbatim extraction
  - <model>.<task>.meta.json — latency, tokens, cost
  - _summary.md             — comparison table + verdict scaffolding
"""

import base64
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = Path("/Users/dahaniglikovdarkhan/Documents/repos/PTO_Bannov/от_Владимира_Михайловича/tools")
sys.path.insert(0, str(TOOLS))


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

from pdf_to_markdown import SYSTEM_PROMPT  # noqa: E402


HF_TOKEN = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_API_KEY")
if not HF_TOKEN:
    print("HF_TOKEN missing in .env", file=sys.stderr)
    sys.exit(1)


@dataclass
class ModelSpec:
    name: str               # short label
    model_id: str           # HF Router model id with :provider hint
    local_size_gb: float    # rough 4-bit MLX size on disk
    in_price: float         # USD / 1M input tokens (provider list)
    out_price: float
    extra_body: dict | None = None  # provider-specific knobs (e.g. disable GLM thinking)
    max_tokens: int = 16000         # raise for thinking models that burn budget on reasoning


MODELS: list[ModelSpec] = [
    # ---- local candidates (all fit comfortably in 48GB at 4-bit) ----
    ModelSpec("qwen3-vl-8b",        "Qwen/Qwen3-VL-8B-Instruct:novita",                 5.0, 0.05, 0.20),
    ModelSpec("qwen3-vl-30b-a3b",   "Qwen/Qwen3-VL-30B-A3B-Instruct:novita",           17.0, 0.20, 0.80),
    ModelSpec("pixtral-12b",        "mistralai/Pixtral-12B-2409:hyperbolic",            7.0, 0.10, 0.10),
    ModelSpec("llama4-scout-17b",   "meta-llama/Llama-4-Scout-17B-16E-Instruct:groq",  10.0, 0.11, 0.34),
    ModelSpec("gemma3-27b",         "google/gemma-3-27b-it:featherless-ai",            17.0, 0.10, 0.20),
    # ---- cloud-only reference (won't run locally — included as "ceiling") ----
    ModelSpec("qwen2.5-vl-72b",     "Qwen/Qwen2.5-VL-72B-Instruct:ovhcloud",            0.0, 0.50, 1.50),
    # ---- 2026-07 wave: Qwen3.5 natively-multimodal line + GLM-V + MiniMax ----
    # Qwen3.5 are hybrid-thinking: without the knob they burn the whole token
    # budget on reasoning for dense tables and return empty content.
    ModelSpec("qwen3.5-9b",         "Qwen/Qwen3.5-9B:together",                         5.5, 0.05, 0.20,
              extra_body={"chat_template_kwargs": {"enable_thinking": False}}),
    ModelSpec("qwen3.5-27b",        "Qwen/Qwen3.5-27B:deepinfra",                      16.0, 0.10, 0.40,
              extra_body={"chat_template_kwargs": {"enable_thinking": False}}),
    ModelSpec("qwen3.5-35b-a3b",    "Qwen/Qwen3.5-35B-A3B:novita",                     20.0, 0.15, 0.60,
              extra_body={"chat_template_kwargs": {"enable_thinking": False}}),
    ModelSpec("glm-4.5v",           "zai-org/GLM-4.5V:novita",                          0.0, 0.60, 1.80,
              extra_body={"thinking": {"type": "disabled"}}),
    ModelSpec("glm-4.6v",           "zai-org/GLM-4.6V:zai-org",                         0.0, 0.60, 1.90,
              extra_body={"thinking": {"type": "disabled"}}),
    ModelSpec("minimax-m3",         "MiniMaxAI/MiniMax-M3:novita",                      0.0, 0.40, 1.60,
              max_tokens=32000),
    ModelSpec("qwen3.5-397b-a17b",  "Qwen/Qwen3.5-397B-A17B:novita",                    0.0, 0.60, 2.40),
]


@dataclass
class Task:
    image: str
    label: str
    user_prompt: str


TASKS: list[Task] = [
    Task(
        "01_text_only.png", "TEXT",
        "Это страница пояснительной записки к проектной документации. "
        "Извлеки ВЕСЬ видимый текст в Markdown по правилам системы-промпта. "
        "Сохрани заголовки, абзацы, нумерацию. НЕ переводи, НЕ перефразируй. "
        "Текстовый слой PDF: (пусто)",
    ),
    Task(
        "02_full_of_tables.png", "TABLE",
        "Это страница с расчётной таблицей. Извлеки таблицу как корректный "
        "Markdown с одинаковым числом столбцов во всех строках. "
        "Сохрани все числа, единицы, заголовки точно. Учти многоуровневую "
        "шапку (склеивай через ' — '). НЕ округляй числа, НЕ меняй разделители. "
        "Текстовый слой PDF: (пусто)",
    ),
    Task(
        "03_big_drawing.png", "DRAWING",
        "Это генплан/чертёж. Извлеки в Markdown ВСЁ видимое: марки и номера "
        "строений, текст в штампе/легенде/выносках, оси и отметки, размерные "
        "цепи. Раздел '## Описание изображения' — что изображено, состав, "
        "взаимное расположение. НЕ выдумывай. Текстовый слой PDF: (пусто)",
    ),
]


@dataclass
class Result:
    model: str
    model_id: str
    task: str
    image: str
    ok: bool = False
    latency_s: float = 0.0
    in_tokens: int = 0
    out_tokens: int = 0
    cost_usd: float = 0.0
    output_chars: int = 0
    error: str = ""


def encode_image(path: Path) -> str:
    return f"data:image/png;base64,{base64.b64encode(path.read_bytes()).decode()}"


def call_router(spec: ModelSpec, image_data_url: str, user_prompt: str,
                max_tokens: int = 16000, max_retries: int = 2) -> Result:
    from openai import OpenAI
    client = OpenAI(base_url="https://router.huggingface.co/v1", api_key=HF_TOKEN)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": [
            {"type": "text", "text": user_prompt},
            {"type": "image_url", "image_url": {"url": image_data_url}},
        ]},
    ]
    last_err = ""
    for attempt in range(max_retries):
        t0 = time.time()
        try:
            # Streaming: the HF Router 504s non-streamed responses that take
            # longer than ~6 min to generate (dense TABLE fixture on slow
            # providers). Chunked delivery keeps the connection alive.
            stream = client.chat.completions.create(
                model=spec.model_id, messages=messages,
                temperature=0.0, max_tokens=spec.max_tokens or max_tokens,
                stream=True, stream_options={"include_usage": True},
                **({"extra_body": spec.extra_body} if spec.extra_body else {}),
            )
            parts: list[str] = []
            usage = None
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                    parts.append(chunk.choices[0].delta.content)
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
            elapsed = time.time() - t0
            text = "".join(parts)
            in_tok = getattr(usage, "prompt_tokens", 0) or 0
            out_tok = getattr(usage, "completion_tokens", 0) or 0
            cost = (in_tok * spec.in_price + out_tok * spec.out_price) / 1_000_000
            return Result(model=spec.name, model_id=spec.model_id,
                          task="", image="", ok=True, latency_s=elapsed,
                          in_tokens=in_tok, out_tokens=out_tok, cost_usd=cost,
                          output_chars=len(text)), text
        except Exception as e:
            elapsed = time.time() - t0
            last_err = str(e)[:400]
            if attempt < max_retries - 1 and any(s in last_err.lower() for s in
                ("502", "503", "504", "timeout", "rate", "temporarily")):
                time.sleep(2 ** (attempt + 1))
                continue
            return Result(model=spec.name, model_id=spec.model_id,
                          task="", image="", ok=False, latency_s=elapsed,
                          error=last_err), ""


def main() -> None:
    models = MODELS
    tasks = TASKS
    argv = sys.argv[1:]
    while argv:
        if argv[0] == "--only" and len(argv) >= 2:
            wanted = set(argv[1].split(","))
            models = [m for m in MODELS if m.name in wanted]
            missing = wanted - {m.name for m in models}
            if missing:
                print(f"unknown model names: {sorted(missing)}", file=sys.stderr)
                sys.exit(1)
            argv = argv[2:]
        elif argv[0] == "--tasks" and len(argv) >= 2:
            wanted_tasks = set(argv[1].split(","))
            tasks = [t for t in TASKS if t.label in wanted_tasks]
            argv = argv[2:]
        else:
            print(f"usage: {sys.argv[0]} [--only name,..] [--tasks TEXT,TABLE,..]",
                  file=sys.stderr)
            sys.exit(1)

    out_dir = HERE / "results_local_bench"
    out_dir.mkdir(exist_ok=True)
    image_urls = {t.label: encode_image(HERE / t.image) for t in TASKS}

    all_results: list[Result] = []
    total_calls = len(models) * len(tasks)
    done = 0
    for spec in models:
        for task in tasks:
            done += 1
            print(f"[{done:02d}/{total_calls}] {spec.name:20s} | {task.label} ... ",
                  end="", flush=True)
            result, text = call_router(spec, image_urls[task.label], task.user_prompt)
            result.task = task.label
            result.image = task.image
            all_results.append(result)

            if result.ok:
                print(f"OK   {result.latency_s:5.1f}s  in={result.in_tokens:>5}  "
                      f"out={result.out_tokens:>5}  ${result.cost_usd:.4f}  "
                      f"{result.output_chars} chars")
                md = out_dir / f"{spec.name}.{task.label}.md"
                md.write_text(text, encoding="utf-8")
            else:
                print(f"ERR  {result.latency_s:5.1f}s  {result.error[:120]}")

            meta = out_dir / f"{spec.name}.{task.label}.meta.json"
            meta.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2))

    # ---- summary table ----
    lines = [
        "# Local VLM candidate bench\n",
        f"_Generated {time.strftime('%Y-%m-%d %H:%M:%S')}_\n",
        f"Same SYSTEM_PROMPT, single-call, no tiling/verify. Source: 3 PNG fixtures.\n",
        "## Models tested\n",
        "| Name | HF id | Local 4-bit | $ in/out per 1M |",
        "|---|---|---|---|",
    ]
    for m in models:
        size = f"{m.local_size_gb:.0f} GB" if m.local_size_gb else "cloud-only"
        lines.append(f"| {m.name} | `{m.model_id}` | {size} | ${m.in_price}/${m.out_price} |")

    lines.append("\n## Results matrix\n")
    lines.append("| Model | TEXT chars/s/$ | TABLE chars/s/$ | DRAWING chars/s/$ | Errors |")
    lines.append("|---|---|---|---|---|")
    by_model: dict[str, list[Result]] = {}
    for r in all_results:
        by_model.setdefault(r.model, []).append(r)
    for name, rs in by_model.items():
        cells = []
        errors = 0
        for label in ("TEXT", "TABLE", "DRAWING"):
            r = next((x for x in rs if x.task == label), None)
            if not r or not r.ok:
                cells.append("FAIL")
                errors += 1
            else:
                cells.append(f"{r.output_chars}ch / {r.latency_s:.1f}s / ${r.cost_usd:.4f}")
        lines.append(f"| {name} | {cells[0]} | {cells[1]} | {cells[2]} | {errors} |")

    lines.append("\n## Outputs\n")
    for name in by_model:
        for label in ("TEXT", "TABLE", "DRAWING"):
            md = out_dir / f"{name}.{label}.md"
            if md.exists():
                lines.append(f"- [{name} / {label}]({md.name})")

    lines.append("\n## Errors\n")
    for r in all_results:
        if not r.ok:
            lines.append(f"- **{r.model} / {r.task}**: {r.error}")

    lines.append("\n## Verdict (fill after reviewing outputs)\n")
    lines.append("- Best for OCR (TEXT):")
    lines.append("- Best for TABLE structure:")
    lines.append("- Best for DRAWING detail:")
    lines.append("- Best overall LOCAL pick:")
    lines.append("- Local-vs-cloud gap (vs qwen2.5-vl-72b):")

    (out_dir / "_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nSummary: {out_dir/'_summary.md'}")


if __name__ == "__main__":
    main()
