# bored-william: reader specification

**Version** 0.1.0-draft · **Stage** 2 of 2 · **Status** design settled, not implemented

Turns the billboard images produced by the fetch stage into a machine-readable dataset
of what each board said, who was advertising, and what they were offering.

Companion to [SPEC.md](SPEC.md), which covers stage 1 (fetch).

---

## Purpose and non-goals

The fetch stage answers *where and when*. The reader answers *what*. It consumes
`manifest.csv` plus the image directory and emits a per-image observation table and a
set of dimension tables, joined on `image_file`.

### Explicit non-goals

- **No audience inference.** Demographic, behavioral, and psychographic targeting were
  considered and dropped: they are unverifiable, prone to stereotyping, and the least
  defensible part of a published dataset. Nothing forecloses them — with the
  transcription, replica, and industry in hand, anyone can run that pass later as a
  clearly-labeled derived layer, with their own assumptions and their own name on it.
- **No taxonomy codes from the vision model.** Codes are resolved offline, per distinct
  value. Models emit plausible-looking codes for taxonomies they barely know, and a
  hallucinated code is indistinguishable from a real one.
- **No web access during extraction.** Lookup happens in the resolution stage, never
  while a model is looking at an image.
- **No image generation.** The model returns bounding-box coordinates; cropping is done
  locally and deterministically.

---

## Design principles

**Observation and inference are stored separately and labeled.** An observation is
checkable against the image by anyone; an inference is not. They differ in how they can
be validated, how they can be contaminated, and how much weight a reader should give
them. Every column below carries its layer.

**Collect at the finest granularity reliably observable; classify later.** Free text is
preserved even where a code is also assigned. Two categories that mean the same thing
economically stay separate if they look different on the board — `vacant` and
`operator_promo` can be rolled up for a vacancy chart, but a collapsed field can never
be un-collapsed.

**Transcription is sealed from prior knowledge.** The model transcribes what is
rendered and never completes what isn't. A board showing only `.ai` yields `.ai`, an
unresolved advertiser, and a visible gap — not a guess drawn from whatever appeared in
a previous row. The risk being defended against is not completing *text*; it is
completing *identity*, which manufactures trends that are undetectable afterward.

**Cross-row consistency is achieved by construction, not instruction.** Per-image calls
have no shared memory, so "be consistent" is an instruction with no referent. Closed
vocabularies are enforced with structured-output enums; open vocabularies (advertiser,
product) are extracted free-form and canonicalized once, offline, in a dimension table.

---

## Input

| Source | Use |
|---|---|
| `manifest.csv` | Row set, `image_file` join key, and the objective quality signals used to audit the gate |
| `images/` | The JPEGs themselves |
| `billboards.csv` → `disambiguation_hint` | **Pass 1 only.** See Contamination controls |

Join on **`image_file`**. It is the only unique key in `manifest.csv` — `row_uuid` and
`pano_id` both repeat wherever two billboards share a panorama.

---

## Pipeline

**Pass 1 — gate.** Full image in. Returns `readability`, `bbox`, `board_state`,
`board_format`. Cheap, classification-shaped, and it decides whether to spend money on
pass 2. The bounding box is recorded, not just used, so the crop is reproducible.

**Crop.** Local, deterministic, Pillow. Cropping adds no resolution — pixels-on-board
is fixed by the panorama's angular resolution and the board's angular size. What it buys
is attention: the second billboard, the gas station, and the traffic stop competing for
the model's budget, and the "which board is the subject" ambiguity disappears
structurally rather than being papered over with a text hint.

**Pass 2 — extract.** Cropped image in. Returns `text_verbatim`, `html_replica`,
`advertiser_name_shown`, `advertiser_url_shown`, `product_named`, `offering_type`,
`operator_shown`, `language`. Skipped when the gate proves there is nothing to read.

**Pass 3 — derive.** Text only, no image. Returns `ad_copy` from `text_verbatim`. Text
calls are far cheaper than image calls, and deriving it here structurally guarantees the
ad copy is a subset of transcribed text rather than a parallel reading of the board.

**Resolution — offline.** Distinct advertiser strings are canonicalized and assigned
NAICS codes once each, in a dimension table. Roughly a few hundred lookups instead of
5,281 — and consistent by construction, since the same advertiser always resolves to the
same row.

### Gate rule

Pass 2 runs unless the gate proves there is nothing to read: `readability` contains
`not_in_frame` or `fully_obstructed`, or `board_state` is `vacant`. `obstructed_major`
still runs — a board where only the logo survives is still worth a row.

---

## Per-image table

One row per capture. `layer` is part of the schema, not documentation.

| Field | Type | Layer | Pass | Notes |
|---|---|---|---|---|
| `image_file` | path | key | — | Joins to `manifest.csv` |
| `readability` | array[enum] | observed | 1 | Always an array; empty means fully readable |
| `bbox` | 4×int | observed | 1 | `x, y, w, h` in source pixels |
| `board_state` | enum? | observed | 1 | Null when indeterminate — see the null-rate warning |
| `board_format` | enum | observed | 1 | Digital boards rotate; see Known confounds |
| `text_verbatim` | string? | observed | 2 | All text, exactly as rendered, sealed |
| `html_replica` | string? | observed | 2 | Positions, colors, fonts, image descriptions. **Opt-in via `--html`; empty otherwise** |
| `advertiser_name_shown` | string? | observed | 2 | As printed |
| `advertiser_url_shown` | string? | observed | 2 | Primary identity anchor when present |
| `product_named` | string? | observed | 2 | As printed |
| `offering_type` | enum? | observed | 2 | What is being offered, if anything |
| `operator_shown` | string? | observed | 2 | From the apron plate — never from the hint |
| `language` | enum? | observed | 2 | |
| `ad_copy` | string? | derived | 3 | The selling text, a subset of `text_verbatim` |
| `model_id` | string | provenance | — | Per pass |
| `prompt_version` | string | provenance | — | Per pass |
| `taxonomy_version` | string | provenance | — | Enums evolve; rows must say which set they used |
| `extracted_at_utc` | iso8601 | provenance | — | |
| `status` | enum | provenance | — | `ok` or an error code; rows are never dropped |
| `error_message` | string? | provenance | — | |

### `html_replica` constraints

Renders standalone in any browser with no external assets: **no JavaScript**, no webfonts,
no remote images, inline CSS only. Text carries position, size, colour, and a generic
font stack approximating the original. Illegible text is preserved as a visible
placeholder rather than guessed. Obstructions are marked in place and styled so a reader
can see they are obstructions and not part of the board's design. Images on the board are
replaced by plain-English descriptions under 280 characters.

---

## Advertiser dimension table

One row per distinct advertiser. Built from the pilot, hand-audited in volume order.

| Field | Notes |
|---|---|
| `advertiser_raw` | The extracted string, as seen |
| `advertiser_canonical` | Canonical name |
| `domain` | From `advertiser_url_shown` where available — the strongest anchor, since it comes from the image itself |
| `wikidata_id` | Optional external anchor; prefer Wikidata's structured IDs over Wikipedia prose |
| `naics_code` | Most specific confidently resolvable |
| `naics_sector` | 2-digit backstop |
| `parent_entity` | Optional. Rebrands and acquisitions across 19 years — a 2009 Esurance board is Esurance, with Allstate recorded here |
| `resolution_source` | Own site, Wikidata, or manual — citable |
| `resolution_status` | `resolved` / `unresolved`. Defunct advertisers land here and will skew early |
| `resolved_date` | |

NAICS is pinned to **vintage 2022** for every row regardless of capture date. Sectors
have been stable since 1997, but finer levels are periodically restructured — sector 51
(Information) notably so in 2022, which is the sector most over-represented in this
corpus. Classifying fresh against one vintage sidesteps that entirely. Joining to
external vintage-coded data does not: use the official concordance tables there.

---

## Vocabularies

**`readability`** — `glare`, `low_resolution`, `obstructed_minor` (50–90% of the board
visible: something blocks part of it but the gaps are fillable), `obstructed_major`
(0–50% visible: most fields unresolvable, but something identifiable survives),
`fully_obstructed`, `not_in_frame` (aim failure — the board is absent from the frame),
`digital_refresh_artifact`.

`fully_obstructed` and `not_in_frame` are deliberately distinct: one is a physical
blockage that no amount of re-aiming fixes, the other is a targeting failure that a
corrected `--assumed-distance` would recover. The fetch stage has a known systematic
version of the latter near-shoulder boards.

**`board_state`** — `commercial`, `vacant`, `operator_promo`, `political`, `psa`,
`religious`, null. `vacant` and `operator_promo` both mean unsold inventory
economically; they stay separate because they look different, and roll up at analysis
time.

**`board_format`** — `static_bulletin`, `digital_led`, `poster`.

**`offering_type`** — `physical_good`, `service`, `software`, `venue_or_experience`,
`employer_brand`, `brand_awareness_only`, `not_applicable`.

Chosen over a formal product taxonomy. GPC is a retail supply-chain classification with
essentially nothing for legal, insurance, healthcare, or campaigns — most of this
corpus. NAPCS pairs properly with NAICS and covers services, and remains available later:
`product_named` is preserved as free text, so a dimension table can be added at any point
without re-running a single image. A large share of 101 boards advertise no purchasable
thing at all — B2B SaaS aimed at other companies' engineers, employer branding, pure
brand awareness — and forcing those into product codes produces low fill rates and
strained classifications.

Every closed vocabulary ships an escape value plus a free-text companion. A category
collecting a large share of rows means the taxonomy has a gap worth fixing before the
full run.

---

## Contamination controls

1. **`disambiguation_hint` goes to pass 1 only.** The existing hints name the operator
   verbatim — *"North facing Outfront Prime Billboard…"*, *"Clear Channel Billboard…"*,
   *"Thompson Billboard"*. Passed into extraction, `operator_shown` stops being an
   observation and becomes the pipeline laundering its own input back as data. It would
   look correct, because the hints are mostly right.
2. **No cross-row context.** Nothing from a previous image enters the current call. The
   independence assumption behind every computed trend depends on it.
3. **No web or search tools in passes 1–3.**
4. **Transcription never infers unrendered characters.**
5. **No taxonomy codes from the vision model.**

---

## Validation

Automated, run over the output before any analysis:

- Every string in `text_verbatim` appears in `html_replica` — they are produced in the
  same call and must not disagree
- `ad_copy` is a subset of `text_verbatim`
- `bbox` lies within image bounds with a plausible aspect ratio
- `readability` cross-checked against the fetch stage's objective signals
  (`px_per_degree`, `est_distance_m`, `est_board_angular_width_deg`). The gate is a model
  grading its own ability to read — self-reported confidence, which is exactly the
  judgment models are least reliable at. Systematic disagreement means the gate is
  miscalibrated, and it is cheaper to learn that from 300 pilot rows than from 5,281.

---

## Known confounds

**Null `board_state` is not missing at random.** It clusters on unreadable images, and
image quality correlates with era — 2007–2011 captures are visibly worse than 2024–2026.
A vacancy rate computed per year with a drifting denominator will show a trend that is
partly a measurement artifact. Report the null rate alongside any board-state series.

**Digital boards change what a capture means.** A digital board rotates six to ten
advertisers; a capture samples one at random rather than recording "the advertiser on
that board." Digital conversion accelerated through the 2010s, so the share of
lottery-ticket captures rises across the study period. Without `board_format` there is no
way to separate "more advertisers on 101" from "more boards became digital."

**Unresolved advertisers skew early.** Defunct companies have nothing left to look up.

---

## Pilot

Run before the full corpus, on the same models production will use. The pilot's job is to
discover the vocabulary space and calibrate the gate — it is doing the *harder*,
unconstrained version of the task, so running it on a weaker model would harvest that
model's blind spots and freeze them into the taxonomy governing everything downstream.
The saving is a few dollars against a corpus costing two orders of magnitude more.

**Stratify by year, not at random.** Advertiser mix in 2008 looks nothing like 2026, and
captures are far denser in recent years, so a uniform sample would build the taxonomy
almost entirely from the modern end and handle the early corpus badly.

Deliverables: the frozen enum set, the seed advertiser table, measured cost per image
from `count_tokens`, and crop-accuracy numbers good enough to decide the pass-1 model.

---

## Cost and throughput

Rough order of magnitude for 5,281 images; confirm against `count_tokens` in the pilot.

| Configuration | Estimate |
|---|---|
| All `claude-opus-5` | ~$240 |
| All Opus, Batch API | ~$120 |

`html_replica` is the dominant cost — the only field generating hundreds to low-thousands
of output tokens, and output bills at 5× input. It is therefore **off by default** and
enabled with `--html`. When off it is dropped from the prompt *and* the output schema, not
merely left null: a nullable field is still part of the contract the model is asked to
fill, which is most of the expense. The extraction token ceiling drops accordingly.

Two levers, in order of size: the **Batch API** halves everything and this workload has
no latency requirement, and **prompt caching** on the system prompt and taxonomy block,
which is byte-identical across every call (stable block first, image after). Note that
smaller models have a higher minimum cacheable prefix, so caching that works on Opus may
silently not apply elsewhere — check `cache_read_input_tokens` rather than assuming.

### Do not economise on the gate

An earlier version of this spec proposed a cheaper model for the gate, on the reasoning
that it is "just classification." That reasoning is wrong, and measurement showed it.

The gate does not only classify — it **localises**, and that bounding box is load-bearing.
It sets the crop the extraction pass reads, and in calibration it is the sole input to
triangulation, where small angular errors are amplified into large positional ones.

A nine-site calibration run compared `claude-opus-5` against `claude-haiku-4-5`. Both
solved 7 of 9, which is exactly the trap — the headline number matched and the outputs did
not. Median solved board height was **11.0 m on Opus against 3.1 m on Haiku**; five of
Haiku's seven solutions put the board under 6 m, one at 0.7 m. The two disagreed on board
position by a median of **79 m**. On the one site with an independent estimate (~93 m, from
a hand measurement that was separately confirmed by correct framing at a 45° field of
view), Opus returned 104 m and Haiku returned 25 m.

The failure signature is visible in the diagnostics: Haiku's bearing spread was larger on
identical camera geometry, which can only come from the box moving between frames, and its
residual was four times higher. Imprecise localisation makes rays cross too early, dragging
distance and height down together.

Use the strongest available model for the gate. The saving on a full corpus is a few
dollars; a mis-calibrated corpus costs a re-fetch of every image.

Batch results return in arbitrary order — key on `custom_id`, never position.

---

## Failure modes

| Code | Cause | Handling |
|---|---|---|
| `GATE_FAILED` | Pass 1 returned unusable output | Row error, no crop attempted |
| `CROP_INVALID` | Bounding box out of bounds or implausible | Row error, pass 2 skipped |
| `NOTHING_TO_READ` | Gate proved the board is absent or blank | **Not an error** — row retained with gate fields |
| `EXTRACT_FAILED` | Pass 2 returned unusable output | Row error, gate fields retained |
| `REFUSAL` | Model declined | Row error, recorded with category |
| `API_ERROR` | Transport or rate limit after retries | Row error, resumable |

Resumable on `image_file`, following the fetch stage. A 5,281-image run will be
interrupted, and restarting from scratch costs money rather than just time.

---

## Deferred

- **NAPCS product resolution.** `product_named` is preserved as free text; the decision
  waits on pilot fill rates.
- **Ad-level industry.** Advertiser NAICS is the traceable backbone. Whether a separate
  inferred "what is being sold on *this* board" category earns its place (Google is
  information services; a Pixel board is consumer electronics) is unresolved.
- **CLI shape.** Whether this is `bored-william read` alongside a renamed
  `bored-william fetch`, or a separate entry point.
