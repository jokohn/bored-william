# bored-william

A batch capture tool that turns Google Street View share links into a reproducible image-and-metadata dataset of roadside billboards across their full capture history.

Give it one link per billboard. It returns every historical panorama at that location — often 20–30 captures going back to 2009 — each aimed at the board, with the metadata needed to reproduce the shot.

```
link, one per billboard  ->  29 images + 34 metadata columns, 2009-07 .. 2025-10
```

The images and manifest are intended as input to an LLM extraction step (advertiser, creative, category). That step is deliberately **not** part of this tool.

See [SPEC.md](SPEC.md) for the full design.

## Install

Python 3.9+, no third-party dependencies.

```bash
pip install -e .
```

Or run without installing:

```bash
python -m bored_william --input boards.csv --outdir ./out
```

## Quickstart

Your input CSV needs one column, `link`:

```csv
link,site_label,disambiguation_hint
https://maps.app.goo.gl/5RhfcELDdrqcAERB8,Zenni SFO,right of Shell sign
```

`site_label` names the billboard *structure*, not the advertiser — the advertiser changes with every capture. `disambiguation_hint` is free text passed through to the manifest so the extraction step can tell which board is the subject when several are in frame. Any other columns you add are carried through untouched.

Survey what exists before committing to a full pull:

```bash
python -m bored_william --input boards.csv --outdir ./out --dry-run
```

```
Zenni SFO                     29 captures  2009-07..2025-10  US-101
```

Then capture:

```bash
python -m bored_william --input boards.csv --outdir ./out --group-by-site --public
```

## Options

| Flag | Default | |
|---|---|---|
| `--fov DEG` | `45` | Whole degrees only — the imagery endpoint rejects fractions |
| `--assumed-distance M` | `90` | Panorama to board. Low sensitivity; see below |
| `--assumed-height M` | `8` | Board centre above camera |
| `--assumed-board-width M` | `14.63` | A 48 ft bulletin. Only affects legibility estimates |
| `--rate-limit MS` | `250` | Spacing between requests, floor 100 |
| `--group-by-site` | off | One directory per site under `images/` |
| `--include-photospheres` | off | User-contributed panoramas, excluded by default |
| `--public` | off | Also write `manifest_public.csv` |
| `--include-neighbors` | off | Also write `neighbor_panos.csv` (~220 rows per site) |
| `--dates-only` | off | Enumerate captures, fetch no images |
| `--resume` | off | Skip captures already recorded as `ok` |
| `--dry-run` | off | Resolve and enumerate, write nothing |

## Why the framing is wide

Tightening the field of view **cannot** improve billboard legibility. The panorama has a fixed angular resolution of 45.5 px/°, and the board subtends a fixed angle from a given panorama; neither changes when you zoom. Cropping tighter just discards surroundings.

So the default shoots wide at native sampling (45° → 2048 px), which costs nothing in detail and buys a large tolerance for aiming error. A distance estimate wrong by a factor of two still lands the board comfortably in frame.

Aim is recomputed for every capture. Panoramas at one location sit metres apart, and across a single site's history that moved the required bearing by nearly 7° — wider than a tight field of view. Reusing a saved heading pushes older captures off-frame.

## Output

```
out/
  images/
    zenni-sfo/                  # with --group-by-site
      zenni-sfo_2025-10_0YxzCIFH.jpg
  manifest.csv                  # one row per capture, 34 columns
  manifest_public.csv           # with --public
  neighbor_panos.csv            # with --include-neighbors
  run_meta.json
```

Failures never drop rows. Every input row produces at least one manifest row, carrying `ok` or an error code in `status` — `NOT_STREETVIEW`, `LINK_UNRESOLVED`, `IMAGE_MISSING`, and so on.

## Redistribution

Street View imagery is Google's copyrighted work. **The manifest is the publishable artifact, not the image directory.** Panorama ids, coordinates, dates and hashes are facts; the JPEGs are not yours to redistribute in bulk.

`--public` supports this: it writes a manifest with the live scrape URL stripped, keeping every column that is a fact or a hash. Publish that plus this tool, and anyone can regenerate byte-identical images and verify them against `image_sha256`. Distribute the recipe, not the pie.

`copyright_string` travels with every row so the attribution obligation stays attached to the data.

Collection relies on undocumented Google endpoints, which is against the Maps Terms of Service. That is a contract matter rather than a criminal one, and the practical remedy is rate-limiting or blocking — but be deliberate about it, keep the request rate polite, and consider the official Street View Static API if you need a defensible provenance story for published work.

## Known limits

- **Dates are month-precision.** There is no day. `date_precision` records this explicitly.
- **Capture cadence is uneven and non-random** — denser in recent years, with multi-year gaps. Any per-year trend partly measures Google's driving schedule, so weight accordingly.
- **Fine print is often unreadable** at freeway setback distances regardless of framing. Advertiser names and headlines are reliable; taglines, URLs and phone numbers are best-effort. Use `px_per_degree` and `est_distance_m` to filter low-confidence extractions.
- **The response format is undocumented** and parsed positionally. A layout change on Google's side degrades individual columns rather than crashing a run, but it can happen.

## License

MIT
