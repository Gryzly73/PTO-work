# real_doc_testset — hard real-page test set for the local VLM

**What this is.** A small, hand-curated test set of **genuinely hard real pages**
from the 21-PDF «Стадия П» project (Сынково СК13 / «Жуковский 1»), with
ground-truth checklists, a scorer, and a one-command baseline runner. It exists
because the 3 synthetic fixtures in `../ground_truth/` (01 text / 02 table / 03
drawing, all from ИОС4) are **near-ceiling and cannot reproduce the real failure
mode** — so they can no longer measure progress on real construction drawings.
See `../LOOP_LEDGER.md` and `../HANDOFF_2026-06-24_vlm_loop.md` for that finding.

**Why these pages.** They were selected deterministically: each `.pre-highdpi.bak`
in `от_Владимира_Михайловича/_archive_2026-06-15/md_byproducts/` is a snapshot
of the pipeline md *before* the high-DPI repair pass. Diffing each backup against
the final `md/NN_*.md` by `## Страница N` section reveals exactly which pages
**errored / needed repair / were flagged** — i.e. the hardest pages. We then
curated 6 across all content types + documents (see table).

## The set (6 pages)

| fixture | doc · page | type | GT quality | what it stresses |
|---|---|---|---|---|
| `19_PB_p82`  | ПБ p82 (Раздел 9)  | DRAWING+tables | 🟢 gold (eye-verified) | fire-pump automation schema: 100+ signal codes 101–134 / 201–211, C2000-AP8, ШУПН/ШУЗ/ШКР, symbol legend, DN100–300 |
| `21_ODI_p14` | ОДИ p14 (Раздел 11)| DRAWING+tables | 🟢 gold (eye-verified) | A1×3 general site plan: ЭКСПЛИКАЦИЯ + ТЭП tables, legend, compass. NB: this page is a **different object** (А-370 «Уссури»/Приморский, org КЫРСКРЕГИОНИНВЕСТПРОЕКТ), not Жуковский |
| `08_KR1_p53` | КР1 p53 (Раздел 4) | DRAWING | 🟡 draft (verify) | geological cross-sections, axis/borehole labels, ИГЭ layers |
| `10_KR3_p59` | КР3 p59 (Раздел 4) | TABLE | 🟡 draft (verify) | wide soil physico-mechanical table (Таблица 7.2.1): С/φ/E columns × method sub-headers × α-factors |
| `12_KR5_p14` | КР5 p14 (Раздел 4) | TABLE | 🟡 draft (verify) | rotated A4 soil-properties table; pipeline flagged a hallucination warning (over-generation test) |
| `11_KR4_p59` | КР4 p59 (Раздел 4) | DRAWING | 🟠 hand-labeled (image) | socle junction nodes «Цоколь. Узлы 1-3», elevations, embedded parts Мэ-2 L160x10. *Correction:* the pipeline md actually captured this fine (~34/345 lines are a cosmetic empty-pipe artifact) — it's a hard node drawing, not a fail-case |

**GT quality tiers**
- 🟢 `gold_eye_verified` — markdown confirmed accurate by human eye (2026-06-10 review). Trust the checklist.
- 🟡 `draft_unverified` — checklist built from automated pipeline output + image cross-check; **needs a human eye-check** (`verification_status: NEEDS_HUMAN_EYECHECK`). Some tokens may be imperfect.
- 🟠 `hand_labeled_from_image` — labeled directly off the render (no trustworthy md), Claude-vision verified.

## Layout

```
real_doc_testset/
├── README.md                 # this file
├── pages/                    # rendered PNGs (one per fixture) — see pages/README.md
├── ground_truth/             # <fixture>.ref.md (reference) + <fixture>.meta.json (checklist)
├── build_testset_pdf.py      # pages/*.png -> real_testset.pdf + order.json (fixed page order)
├── score_real_testset.py     # checklist scorer (--self-check | --combined <md> | <dir>)
├── run_baseline.sh           # ONE COMMAND: run local VLM on the set + score
├── real_testset.pdf          # combined PDF fed to the pipeline (rebuild via build_testset_pdf.py)
└── order.json                # page-index -> fixture (so the scorer maps sections back)
```

## How to use

```bash
# sanity: every checklist must be satisfiable by its own reference (~99% expected)
python3 score_real_testset.py --self-check

# run the local VLM end-to-end and score (re-run >=4x; 8b is nondeterministic)
./run_baseline.sh 1     # -> out_1.md  + score table

# score an existing pipeline md (split by '## Страница N' via order.json)
python3 score_real_testset.py --combined out_1.md
```

**Measurement discipline** (from the loop ledger): 8b production TABLE varies
~55 pts run-to-run → never conclude from <4 clean runs; always check the `[Error`
count + file size before trusting a score (a network blip fakes a near-0).

## Adding a page

1. Render it: `pages/<NN_DOC_pNN>.png` (see `build_testset_pdf.py` for the render convention).
2. Write `ground_truth/<fixture>.ref.md` (reference description) and `ground_truth/<fixture>.meta.json` (checklist — copy an existing one's schema).
3. Add the fixture to `ORDER` in `build_testset_pdf.py`, rebuild the PDF.
4. `python3 score_real_testset.py --self-check` must pass (~100%) before you trust it.

## meta.json checklist schema (matchers)

A check passes only if **all** its keys pass:
`contains` (str) · `any_contains` (≥1 of list) · `any_contains_ci` (ci ≥1) ·
`all_contain` (all of list) · `and_any` (extra AND group) · `not_contains` (none present).
⚠️ Only these keys are recognized — a check with none (e.g. a stray `matcher`/`value`)
passes vacuously and silently inflates the score. `score_real_testset.py` self-check
catches most of it; a validator is in the git history.

## Links
- Loop journal: [`../LOOP_LEDGER.md`](../LOOP_LEDGER.md)
- Loop prompt / next steps: [`../LOOP_PROMPT_iter4_and_beyond.md`](../LOOP_PROMPT_iter4_and_beyond.md)
- Latest hands-off: [`../HANDOFF_2026-06-24_vlm_loop.md`](../HANDOFF_2026-06-24_vlm_loop.md)
- Synthetic fixtures (text/table/drawing): [`../ground_truth/`](../ground_truth/)
