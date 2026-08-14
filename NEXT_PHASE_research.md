# NEXT-PHASE RESEARCH AGENDA — local PDF→MD to Gemini parity

Written 2026-06-28 for the next session. Repo root:
`/Users/dahaniglikovdarkhan/Documents/repos/PTO_Bannov/`.

---

## THE GOAL (unchanged, stated plainly)

Convert Russian construction PDFs (drawings, dense tables, штампы) → Markdown.
**Gemini's output is the ideal / ground truth.** Find the **cheapest model we can run
LOCALLY** (tested via API for now) + the **automation** that together produce output
**as good as Gemini** on the hard pages. Measure everything on
`real_doc_testset/` with `score_real_testset.py` against the Gemini-quality checklists.

**Acceptance:** the local pipeline's score on the 6 hard pages approaches Gemini
(~100% by construction). Today we are at **~64%** (the deliberately-hardest pages).

---

## WHERE WE ARE (end of this session)

Best local pipeline = **3-stage type-aware synthesis**, all parts local-deployable:
`qwen3-vl-8b` (vision/structure) + tiled Tesseract (dense text) + `qwen3-32b-text`
(fuse, drawings only; keep 8b draft for tables). Hard 6 pages: **~46% → 64%**, matching
the cloud-Sonnet ceiling. Shipped behind flags `--ocr-contribute` + `--synthesize`
(default OFF, production untouched). Full detail: `HANDOFF_2026-06-28_synthesis_pipeline.md`
+ `real_doc_testset/RESULTS.md`.

---

## WHAT THIS SESSION COVERED (don't repeat)

- ✅ Model survey via available APIs: Qwen3-VL 8b/30b/32b/235b, GLM — none beats 8b; size
  is not the lever. InternVL3/MiniCPM NOT API-reachable here.
- ✅ OCR contributor (layer 1): recovers штамп `doc_code` (3/6 pages), wired into engine.
- ✅ Synthesis (layer 2): the big lever; local qwen3-32b matches cloud Sonnet; wired into engine.
- ✅ Negative results: bigger VLM, llama-3.3-70b synth, local table-formatter — none help.
- 🔄 **Gemma 4 — STARTED, not finished** (see Phase 0 below).

---

## THE NEXT PHASE — uncovered research, prioritized

### ★ Phase 0 — GEMMA 4 — ✅ CLOSED 2026-06-29 — hypothesis NOT validated
> **RESULT (4 fair runs, all pure Gemma, 0 fallback): median 47.2%** — statistically tied
> with qwen3-vl-8b (~46%). No native Cyrillic-density edge from the Google lineage. SAME
> штамп blind spot, SAME dense-table weakness (КР3 ~30%), SAME nondeterminism. **BUT** the
> per-page CEILING is much higher: run 2 hit ПБ **93.8%** AND ОДИ **93.8%** (8b never beat
> ~50/~13) — Gemma CAN read the dense marks, just not reliably (ПБ 31–94, ОДИ 12–94).
> → Don't swap base model on the median. DO carry Gemma into Phase 1's re-roll gate: its
> good draws are dramatically better, so median-of-N+re-roll harvests more from Gemma than 8b.
> Detail: `real_doc_testset/RESULTS.md` (Gemma 4 section). `gemma4-31b` dense variant NOT yet run.

**Why:** Gemma and **Gemini are both Google** — Gemma may share the lineage/training that
makes Gemini good at exactly these Russian construction docs, in a way Qwen (Alibaba) does
not. And we NEVER tested Gemma fairly: the only prior run (Gemma-3-27b) was crippled by an
8192-token provider cap that chopped its tables. Gemma **4** is a generation newer, on
OpenRouter, vision-capable, with a local-deployable MoE variant (`gemma4-26b` = 26B/**4B
active**) and a dense `gemma4-31b`. Smoke test: **clean Cyrillic, no latinization** (the
failure that killed qwen2.5-72b) — promising — but at low-res single-call it paraphrased.
**Registered** in `pdf_to_markdown.py` PROVIDERS as `gemma4-26b` / `gemma4-31b` (ADR-044).

**Goal:** does Gemma 4 beat qwen3-vl-8b as the VISION model on the hard pages?
**Do:**
1. Finish the launched run: `out_gemma4_26b.md` (PID was 92680). Then ≥4 runs (it's likely
   nondeterministic too), median + range. `score_real_testset.py --combined out_gemma4_26b.md`.
2. Same for `gemma4-31b` (dense; may read dense marks better; watch free-tier 429 → use paid).
3. **Key checks beyond the score:** (a) Cyrillic clean across all pages? (b) does it READ the
   dense marks 8b/Tesseract miss (C2000-AP8, ШУПН, signal tables 101–134)? — if Gemma reads
   those, it breaks the wall the synthesis can't. (c) hallucination/paraphrase rate (the
   low-res smoke test invented text — verify the tiled high-res run doesn't).
4. If Gemma 4 wins on vision → swap it in as `--provider`, re-measure the full synthesis stack.
   If it reads the dense marks → it may be the single-model path that beats the 3-stage hack.
**Payoff:** potentially the biggest lever — a Google model that natively matches Gemini's
Cyrillic + density handling would shortcut the whole compensation pipeline.

### Phase 1 — local, high-ROI, low effort (expected 64% → ~78%)
1. **Štamp TEXT extraction.** PROVEN this session: the corner OCR cleanly reads
   `КУРСКРЕГИОНПРОЕКТ` (design_org), `Жуковский` (object_name), `Насосная` — but the
   contributor only emits CODE tokens (letter+digit), so these WORDS are dropped. They are
   missed on 5/6 pages. **Do:** extend `ocr_contributor` to surface the full штамп-corner text
   block (not just codes) into the synthesis input. **Goal:** recover design_org / object_name
   / sheet_title / building_subtitle everywhere.
2. **doc_code propagation + confusion-aware match.** КР1→`КРУ`, КР4→`КРА` are single-char
   Tesseract confusions. A real multi-page document shares ONE doc_code → read it cleanly on
   any page, propagate to all; + fuzzy-match the trailing token against the known prefix
   `NN-XXX-N/NN-`. **Goal:** doc_code on 6/6, not 3/6.
3. **≥4-run medians + draft-quality re-roll gate.** 8b drafts are wildly nondeterministic
   (this session: ПБ synth 37.5% one run, ОДИ 81.2% another). **Do:** always measure
   median-of-≥4; add a gate that re-rolls the 8b draft if its output is suspiciously thin
   before synthesis (synthesis on a degenerate draft underperforms). **Goal:** a stable,
   trustworthy number + protection against thin-draft synthesis.

### Phase 2 — local, more engineering (→ ~85%)
4. **Targeted high-DPI re-read** (the agent-manager / self-correction idea, see
   `[[pdf-agent-manager-self-correction]]` memory). Detect a page region still sparse /
   `(не читается)` → re-render JUST that region at high DPI → re-read with the VLM. This
   attacks ПБ's signal tables and dense cells WITHOUT a bigger model. **Goal:** recover the
   structured dense content tiling shrinks too far.
5. **Geometric table extraction.** The general-LLM synthesis REGRESSED tables (КР3 53→13) and
   `--table-mode` didn't beat 8b. Different approach: use Tesseract **TSV output (bounding
   boxes)** to reconstruct the grid geometrically (rows/cols from x/y), then fill cells —
   structure from geometry, not from an LLM. **Goal:** close the table-structure gap (КР3/КР5
   col headers + body values) locally.

### Phase 3 — the genuine ceiling (the last ~10–15%) — A DECISION, not just code
6. Bucket D: dense stylized marks (C2000-AP8, ШУПН/ШУЗ, signal tables) that **neither 8b NOR
   Tesseract reads**. Options, pick one:
   - **Selective escalation:** send ONLY the few unreadable crops to a large VLM
     (Gemini-Flash / qwen-235b). Breaks "pure local" for ~5% of content; best accuracy/cost.
   - **Better local VLM:** deploy InternVL3 / MiniCPM-V / a dedicated OCR model LOCALLY
     (Ollama / MLX / vLLM) — NOT API-reachable here, so this needs local install + a new
     provider. Gemma 4 (Phase 0) may also clear this.
   - **Accept the ceiling:** ship ~85% local + flag the unreadable regions for a human.

### Phase 4 — measurement hygiene (parallel, ongoing)
7. **Eye-check the 🟡 draft ground truths** (КР1/КР3/КР5). Their `ref.md` came from Gemini-Pro
   and is unverified — scores on those pages aren't fully trustworthy until a human confirms
   against `pages/*.png`. **This is a YOU task** (flagged NEEDS_HUMAN_EYECHECK).
8. Beyond the 6 hard pages: validate the pipeline on a sample of NORMAL pages too (these 6 are
   worst-case; the practical doc-wide number should be much higher than 64%).

---

## HOW TO RUN (reference)
```bash
cd "testing local models for compute vision/real_doc_testset"
# Gemma 4 vision test (Phase 0):
python3 ../../от_Владимира_Михailовича/tools/pdf_to_markdown.py real_testset.pdf \
  --provider gemma4-26b --no-auto-fallback --no-ocr-check --no-highdpi-repair \
  --data-first --no-table-render --freq-penalty 0.4 --deromanize-mixed --cap-native-dpi \
  --no-resume -o out_gemma4_26b.md
python3 score_real_testset.py --combined out_gemma4_26b.md
# Full local synthesis stack (current best, ~64%):
#   add  --ocr-contribute --synthesize qwen3-32b-text   (and use --provider qwen3-vl-8b)
```

## ONE-LINE SUMMARY OF THE GOAL FOR NEXT SESSION
> Test Gemma 4 fairly (Google's model, same lineage as the Gemini ground truth) as the vision
> model; then bank the cheap local wins (štamp text, doc_code propagation); then decide whether
> the last ~15% (dense schematic marks) is worth escalating to a large VLM or accepting as the
> local ceiling. Measure everything on `real_doc_testset` against Gemini.
