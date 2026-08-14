# LOOP_LEDGER — qwen3-vl-8b extraction-accuracy self-improvement loop

Measurement contract: **bench** = `loop_bench.py` (single full image, fast signal);
**production** = `pdf_to_markdown_qwen.py` on `test_pages.pdf` + `score_production.py`
(real tile pipeline — the number that counts). 8b is nondeterministic → every
measure ≥2 runs, track **median** and **worst**, never the lucky best.

Genericity rule: every change needs a mechanism that works on an UNSEEN drawing.
Never read `ground_truth/*.meta.json` to target checks. Prompts compact/universal.

---

## ⚠️ Iteration 0 finding that reframes the whole loop (2026-06-24)

**The "dense-table tile collapse" premise was measured on a STALE `test_pages.pdf`.**
The PDF (built 2026-05-30 01:18) predated a fixture swap (PNGs updated 01:19): its
page 2 was a *portrait* genplan table, while the current `02_full_of_tables.png`
is a *landscape* air-balance table. Production was scoring the WRONG pages against
the checklists → apparent TABLE 11% / DRAWING 6%. That was a measurement artifact,
**not** a tiling failure. Rebuilt the PDF from current fixtures (commit
`test(vlm-loop): … rebuild stale test_pages.pdf`).

After the rebuild, the **current default config already achieves the loop's stated
goal**: production TABLE **94.4%** (= the 30B target ~94%), DRAWING **88.9%**, with
**zero "(не читается)" flood** in any tile. The failure mode the loop was chartered
to fix (8b flooding "(не читается)", dropping totals, latinizing codes) does not
occur with the current config (data-first + freq_penalty=0.4 + deromanize +
hierarchical merge + capped merge DPI, all landed in the 2026-06-23 sessions).
Tiling actually *helps* the table: production TABLE 94.4% > single-image bench 88.9%.

Implication: the large +8pt headroom the prompt assumed is **not available on these
fixtures** — the baseline is already near-ceiling. Remaining work is (a) narrow
generic gains on 3 genuine stable misses, and (b) building the prescribed tile-level
detect-and-rerun as **production robustness insurance** for harder real documents
(КР1 etc.), where it may matter even though it won't move the 3-fixture score.

---

## BASELINE (2026-06-24)

### Bench — `loop_bench.py --runs 3 --tag baseline` (single full image)
| Task | median | worst | stable-miss | flaky |
|---|---|---|---|---|
| TEXT | 100% (10/10) | 90% | — | page_number_28_at_top |
| TABLE | 88.9% (16/18) | 88.9% | colour_legend_present | headers_exfilter_infilter |
| DRAWING | 100% (18/18) | 83.3% | — | APT_R_valve, air_curtain_KEV, org_kurskregionproekt |
| **avg median** | **96.3%** | | | |

### Production — `pdf_to_markdown_qwen.py test_pages.pdf` (real tile pipeline)
Config = production defaults: provider qwen3-vl-8b, **data-first**, **freq_penalty 0.4**,
**deromanize-mixed on**, no-table-render, Gemini disabled on all paths.

⚠️ **The initial 2-run baseline below was MISLEADING — see the noise section.** A
later 7-run sample (iter 2) shows TABLE is wildly nondeterministic:

| Task | runs (rerun OFF) | **median** | **range** |
|---|---|---|---|
| TEXT | 90,70,70,90,90,80,80 | 80% | 70–90 |
| TABLE | 94.4,94.4,44.4,72.2,61.1,38.9,94.4 | **~72%** | **38.9–94.4 (≈55pt!)** |
| DRAWING | 88.9,88.9,94.4,94.4,94.4,55.6,88.9 | 94.4% | 55.6–94.4 |

The first two TABLE runs (94.4, 94.4) were a LUCKY draw — not representative.

---

## ⚠️⚠️ Iteration-2 finding: MEASUREMENT NOISE dominates the loop (2026-06-24)

**TABLE production variance (≈55 points, 38.9–94.4%) swamps the ±8pt signal the loop
chases.** With 2 runs you can draw any conclusion. This invalidates 2-run comparisons:
iter-1's apparent "regression 94→44/55" was **noise**, not the mechanism (44/55 are
inside the OFF band). You cannot improve what you cannot measure.

**Root cause of the variance (verified):** the TABLE page's embedded image is 3509×2480px,
but the pipeline renders it at DPI_DRAWING=500 → 24368×17222px (a **7× upscale — zero new
detail**) → `compute_tile_grid` → **63 tiles**; the drawing page → **117 tiles**. The
merge step then stitches 63–117 redundant, upscaled-blurry fragments and **drops different
table rows each run** (misses cluster on `section_*`/`room_*`/`system_code_*` ROWS). Native
resolution would need 2 tiles (table) / 3 (drawing). This is ~30× over-tiling.

**Implication / new #1 lever:** the biggest, most generic win is NOT prompt/rerun tuning —
it is **capping render DPI to the embedded raster's native resolution on image-only pages**
(generic: never render a scanned/image page above its native pixels — it only upscales).
Expected: table 63→2 tiles, drawing 117→3, → far less merge variance + cost + time. This is
iteration 3. (Caveat: only for raster-dominated pages — vector PDFs gain real detail at
higher DPI; detect via PyMuPDF embedded-image dims.)

---

## BEST-CONFIG (current champion)
The production defaults above. **No experiment has beaten it yet** — it is the
baseline AND the champion. Any change must lift production median on TABLE+DRAWING
(or TEXT worst) WITHOUT regressing the others, confirmed ≥2 runs, signal not noise.

---

## Genuine stable misses (verified in OUTPUT, not via checklists)
1. **TABLE `colour_legend_present`** — model reads all table data but never describes
   the colour legend block (0 occurrences of легенд/цвет/условн in either run).
   → generic mechanism: describe-visuals principle should emit a colour-legend
   subsection when colour coding is present. Risk: prompt bloat / overfit.
2. **DRAWING `APT_R_valve`** — model reads valves (клапан ×6) but misses this small
   Latin equipment mark. → generic mechanism: higher-DPI re-read of low-yield tiles,
   or a detect-and-rerun on tiles that returned few marks for their area.
3. **TEXT `page_number_28_at_top`** — page number in the top header zone, consistently
   dropped (also flaky law_116_FZ, stamp_code). → generic mechanism: ensure the top
   header band is captured; possibly a header-zone prompt nudge. Risk: overfit.

These are NARROW (1 check each) → max realistic fixture gain is small. Verify any
"win" is the mechanism recovering the concept, not 8b noise flipping a flaky check.

---

## ★ NORTH STAR (user, 2026-06-26): replace Gemini Flash/Pro with LOCAL model + compensation
The bare 8b is worse — accepted. Close the gap with CODE: OCR pass recovers dense
codes/numbers 8b drops; a SEPARATE table-formatter model reformats cells; post-proc
cleans. Priority output = correct CONTEXT (text + drawing description), not pixel-perfect
tables. **Measure the PIPELINE (8b + compensation), not the bare model**, on real_doc_testset.
Full plan: `HANDOFF_2026-06-26_real_testset.md`. Run-1 results: `real_doc_testset/RESULTS.md`.

## IDEA BACKLOG (highest-expected-value first)
- [ ] **★ NEW #1 — Fix HTTP 413 on big sheets** (robustness; unblocks measurement). Sheets
      with long edge >~5000px (ОДИ 10530, КР3 7446, КР1 5263) overflow the request size →
      whole-page `[Error 413]` → false ~6% (3/6 pages in run 1). DPI cap alone isn't enough.
      Cap request image by max-pixels/bytes + retry-on-413 smaller. Then re-run real_doc_testset.
- [ ] **★ NEW #2 — OCR contributor** (compensation layer 1). Promote Tesseract from checker
      (`check_ocr_completeness`) to contributor: merge codes/numbers it finds but 8b missed
      (signal codes, doc codes, dims). Target: lift ПБ p82 (read body, missed structured codes).
- [ ] **★ NEW #3 — Separate table-formatter model** (compensation layer 2, = `--merge-provider`
      ADR-037/038): qwen3-32b-text/claude reformats raw cells → clean GFM on table pages.
- [ ] **★ NEW #4 — Gemini baseline on the SAME 6 pages** = the bar to clear (we're replacing it).
- [ ] **Tune the render-DPI cap MULTIPLE (1×/2×/3× native)** (was iter 4). Iter 3 showed
      1× native (`--cap-native-dpi`) is a huge TABLE win (median 61→89, spread 55→11pt) +
      TEXT (80→100) but REGRESSES DRAWING (94→80): the small VLM reads tiny drawing marks
      better when glyphs are UPSCALED (bigger px), even though upscaling adds no real detail.
      Tables want native (merge-stability); drawings want upscale (glyph legibility). Sweep
      the cap at k×native (k=1.5/2/2.5) → find k that keeps TABLE high+tight AND restores
      DRAWING. Generic (k is a global multiple, no page-type/fixture targeting).
- [x] **Cap render DPI to native (1×) on image-only pages** — iter 3: big TABLE/TEXT win,
      DRAWING regression → kept flag OFF; superseded by the multiple-tuning item above.
- [ ] **Make "Connection error" retryable** — `_is_transient` misses bare "Connection error."
      so a transient HF/Novita network blip exhausts immediately → whole-page `[Error`
      (observed: 4/5 runs collapsed in an iter-3 burst). Add it to the transient set +
      maybe space concurrent calls. Robustness, not a fixture-score item.
- [x] **Tile detect-and-rerun (the chartered mechanism)** — built iter 1 (all sigs →
      regressed, but that was NOISE); iter 2 restricted to hard signatures only
      (`error`/`repetition`) → safe-by-construction (keep-better + only worthless tiles),
      recovered an `[Error` tile. Kept, off by default (`--tile-rerun`).
- [ ] **Low-yield-tile re-read** — if a tile's output is short relative to its pixel
      area (text density heuristic), re-render at higher DPI and re-run. Generic;
      could recover APT_R_valve and other small marks. Needs a density threshold that
      doesn't fire on genuinely sparse tiles.
- [ ] **Self-consistency on the merge** — run the table-bearing tile twice, keep the
      union of rows/marks (dedup). Cheap; targets dropped cells. Risk: hallucinated
      rows — need conservative union.
- [ ] **Describe-visuals nudge for legends** — one universal clause: "if the page has
      a colour/hatch legend, list each colour/hatch and its meaning". Principle-based,
      no domain terms. Test it doesn't bloat TEXT/TABLE or invent legends.
- [ ] **Research** (do regularly): qwen3-vl prompting; VLM dense-table extraction;
      OCR tiling overlap; image preprocessing (binarize/upscale/contrast) for OCR;
      self-consistency majority voting VLM; structured-output JSON schema for tables.

## Tried (✅ kept / ❌ reverted)
| # | hypothesis | change | bench | prod | verdict |
|---|---|---|---|---|---|
| 0 | establish baseline | rebuild stale PDF + scoring glue | 96.3% | 87.8% avg (TABLE 94.4 / DRAW 88.9) | ✅ baseline set; premise reframed |
| 1 | tile detect-and-rerun is a no-op on clean fixtures → no regression | add deterministic signature→hi-DPI-rerun policy in `_process_tile` (off by default, `--tile-rerun`), unit-tested | n/a | TABLE 94.4→44.4/55.6 with flag ON | ⚠️ verdict WRONG — "regression" was NOISE (see iter 2) |
| 2 | restrict rerun to hard sigs (error/repetition) → safe; +5-run noise probe | drop `empty`/`illegible_flood` triggers; 5× baseline noise run | n/a | TABLE OFF = 38.9–94.4 (≈55pt noise) | ✅ KEPT hard-sigs (safe-by-construction); ⭐ found noise+over-tiling root cause |
| 3 | native-DPI cap raises AND tightens TABLE distribution | `--cap-native-dpi` (cap raster pages to native px), 4 clean runs | n/a | TABLE 61→89 median, spread 55→11pt; TEXT 80→100; **DRAWING 94→80** | ⚖️ flag KEPT OFF — big TABLE/TEXT win but DRAWING regressed → tune cap multiple (iter 4) |

### Iteration 1 — tile detect-and-rerun: NEGATIVE RESULT (2026-06-24)
**Hypothesis:** the chartered policy (detect bad tile → re-run escalated, keep-better)
is a no-op on the clean fixtures (no signature → no re-run), so enabling it can't
regress the near-ceiling baseline; its value is robustness on hard real docs.

**Built (generic, off by default, `--tile-rerun`):**
- `tile_failure_signature(text)` — pure classifier: `error`/`empty`/`repetition`/
  `illegible_flood` (flood = ≥8 markers, or ≥4 AND ≥50% of lines). No doc content.
- `tile_quality_score(text)` — len − 200·illegible (repetition collapsed first), so a
  re-run is kept only if it scores higher → "never regress a tile" (per-tile).
- `_rerender_tile_hi` — re-render the clip up to RERUN_TILE_MAX_PIXEL=4000 (>2800 base).
- `_remediate_tile` — budget-capped (3/page), thread-safe, logged.
- 23 unit tests (signature, score, real re-render, end-to-end wiring w/ fake model):
  `tools/tests/test_tile_rerun.py` — ALL PASS.

**Measured (production ×2, flag ON):** TEXT 80/80 (unchanged), DRAWING 100/94.4 (fine),
but **TABLE 44.4% / 55.6%** vs baseline 94.4% — a hard, consistent regression.

**Root cause (verified in logs):** the hypothesis is FALSE. The clean TABLE page renders
at 500 DPI into many tiles; legitimately-empty MARGIN tiles trip `empty` and partial
dense-region tiles trip `illegible_flood`. The budget's first 2–3 re-runs REPLACED
table-tile fragments with hi-DPI re-reads that score "better" (longer/fewer-illegible)
but **merge worse** — the stitch drops rows/sections when fragments conflict/duplicate.
Three design flaws: (1) per-tile remediation is **merge-blind** — tile-local "better"
≠ page-level better; (2) **`empty` is not a failure** (margins are legitimately empty);
(3) the **length-based quality score** rewards verbosity, picking merge-harmful fragments.

**Verdict:** REVERTED enablement — flag stays OFF (production byte-identical). Committed
the mechanism as off-by-default, unit-tested SCAFFOLDING (the chartered architecture +
reusable detection primitives) so the next iteration can redesign it merge-aware. Also
empirically confirmed iter-0's finding: the fixtures **cannot reliably elicit a flood**
(8b nondeterministic; a 900px table strip flooded 20× once, read cleanly the next call)
— so this mechanism's real value can only be validated on a genuinely hard document.

**Next-iteration redesign (merge-aware):** re-run ONLY hard signatures (`[Error`,
repetition); DROP `empty` as a trigger entirely; and gate acceptance on the MERGED page
(or feed both original+rerun fragments to the merge and let it reconcile), never on
per-tile length. Or pivot to validating on a hard real КР1 page where floods are real.

### Iteration 2 — hard-signatures-only + noise discovery (2026-06-24)
**Change:** added `TILE_RERUN_SIGNATURES=("error","repetition")` and gated `_remediate_tile`
to those only (dropped `empty`/`illegible_flood`). 25 unit tests pass (incl. "soft signature
→ NO re-run", "error → fired+recovered"). Re-ran production ×2 with flag ON.

**Result that broke the loop's premise:** run2 scored **TABLE 66.7%** with the tile-rerun
NOT firing on the table page at all (only one firing: `r3c7 error→recovered` on the DRAWING).
So the table was processed identically to baseline yet scored 66.7, not 94.4. → ran a **5×
baseline (rerun OFF)** probe: TABLE = 44.4/72.2/61.1/38.9/94.4 → **≈55pt nondeterministic
variance** (see noise section at top). 

**Verdicts:**
- The iter-1 "regression" was NOISE — not the mechanism. Corrected the record.
- hard-sigs-only is **safe-by-construction**: keep-better guard means a re-run can't lower a
  tile's score, and it only touches worthless `[Error`/repetition tiles (replacing an `[Error`
  fragment with real content can only help the merge). It correctly recovered an `[Error`
  tile (score −inf→146). KEPT; default OFF (can't claim a fixture-measured win under the noise,
  but it's a real robustness gain on the failure cases).
- **Did NOT flip the wrapper default** — under ±55pt noise I can't responsibly auto-enable, and
  the real lever is variance reduction (over-tiling), not this. Available via `--tile-rerun`.
- ⭐ Root cause of the variance found: ~30× over-tiling from rendering raster pages far above
  native resolution → 63/117 tiles → merge drops rows nondeterministically. → iteration 3.

### Iteration 3 — native-DPI cap: big TABLE/TEXT win, DRAWING tradeoff (2026-06-24)
**Change:** `raster_native_dpi(page, dpi)` + `--cap-native-dpi` (global `RASTER_NATIVE_DPI_CAP`,
default OFF). On a page where one embedded image covers ≥`RASTER_COVER_MIN`=0.9 of the area,
cap render DPI so the page isn't rendered above the image's native pixels. Verified tile cuts:
TEXT 63→2, TABLE 63→2, DRAWING 117→6. 26 unit tests pass (`test_native_dpi_cap`).

**Measured (clean, error-free runs; ON=4 runs, OFF=5-run baseline):**
| Task | cap-ON runs | ON median (min) | OFF median | OFF range |
|---|---|---|---|---|
| TEXT | 90,100,100,100 | **100% (90)** | 80% | 70–90 |
| TABLE | 88.9,88.9,83.3,94.4 | **88.9% (83.3)** — spread 11pt | 61.1% | 38.9–94.4 (55pt) |
| DRAWING | 83.3,77.8,88.9,66.7 | **80.5% (66.7)** | 94.4% | 55.6–94.4 |

**Interpretation:** cap = the right fix for dense-raster TEXT/TABLE pages — TABLE median +28pt
and variance collapsed 55→11pt (now consistently 83–94%, ~30B-parity), TEXT +20pt. But DRAWING
regressed −14pt: the small VLM reads tiny marks (valves/sensors) better when glyphs are UPSCALED
(bigger px), even though upscaling adds no real detail. So tables want native (merge-stability),
drawings want upscale (glyph legibility) — opposite DPI prefs; a single 1× cap can't serve both.

**Verdict:** flag KEPT OFF (production byte-identical) — net avg-median +11pt but DRAWING
regression fails the no-regression rule for a blanket default. The variance fix is real and the
biggest lever found; next: tune the cap MULTIPLE (k×native) to keep the TABLE/TEXT win while
restoring DRAWING (iter 4, backlog #1).

**Side gotcha:** an iter-3 burst of fast back-to-back runs hit `[Error: Connection error.]` on
4/5 runs (everything → near-0). `_is_transient` doesn't treat bare "Connection error" as
retryable → whole-page failure. Worked around with spacing + redo-on-error in the measurement
script; logged as a robustness backlog item. **Always check for `[Error`/tiny output before
trusting a production score** (the measurement script `/tmp/run_clean.sh` does this).

## Iteration — real_doc_testset baseline + HTTP 413 fix (2026-06-26)

**Context shift:** moved off the 3 toy fixtures onto `real_doc_testset` (6 genuinely hard
pages from the 21-PDF run). North star: replace Gemini with 8b + a compensation pipeline;
measure the PIPELINE on real pages, not the bare model on toys.

**Pipeline bug found & fixed (ADR-041):** Run 1 had 3/6 pages return `[Error 413]` as their
WHOLE content → false ~6% → meaningless 35.9% total. Root cause: `_capped_merge_dpi` had a
`max(72,…)` floor that defeated the pixel cap on giant foldouts (ОДИ 146in → 72 DPI → 10530px
merge image → 413). Fix = drop the floor + cap every outgoing image to REQUEST_MAX_PIXEL=4096
in `call()` + retry-on-413 (halve the image). 6 unit tests, full suite green. Committed `7ba7c8e`.

**Baseline after fix — 4 clean runs (0 errors each), median:**
| page | median | spread | | page | median | spread |
|---|---:|---:|---|---|---:|---:|
| КР4 p59 | 93.3 | 53–93 | | КР1 p53 | 37.5 | 18–56 |
| КР5 p14 | 75.0 | 36–86 | | ПБ p82  | 40.6 | 6–50 |
| КР3 p59 | 53.4 | 13–73 | | ОДИ p14 | 9.4  | 6–13 |
| | | | | **TOTAL** | **~46%** | 38–54 |

**Findings:** (1) 413 fix holds (0 err × 4); retry observed recovering ОДИ. (2) 8b is wildly
nondeterministic on hard pages (КР3 13→73, ПБ 6→50) — single runs lie, only ОДИ is *stably* bad.
(3) **`doc_code` (штамп) missed on every page every run** — the #1 systematic, OCR-targetable gap.
Concrete ОДИ failure: 8b emits 20+ `(не читается)` + compass only; drops both tables + legend that
Gemini reads cleanly. → OCR contributor (plan #3) is the next lever. Full writeup: `real_doc_testset/RESULTS.md`.

**Backlog (carried):** bare `[Error: Connection error.]` still not in `_is_transient` (whole-page
fail on bursty runs) — make retryable.

## STOP CRITERIA
Stop with success if production TABLE+DRAWING median rises ≥+8pt and holds ≥2 runs
without regressing TEXT. Stop on dry well after 3 consecutive no-gain iterations with
backlog exhausted + research dry — then report ceiling honestly. **Given the baseline
is already near-ceiling on these fixtures, success is more likely to mean: built the
robustness mechanism (generic, no fixture regression) + documented the true ceiling.**
