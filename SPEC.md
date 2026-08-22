# bored-william — specification

**Version** 0.1.0 · **Runtime** Python 3.9+ · **Dependencies** stdlib only · **Status** reviewed, ready to implement

A batch capture tool that turns Google Street View share links into a reproducible
image-and-metadata dataset of roadside billboards across their full capture history.

---

## Naming

| Context | Form |
|---|---|
| Repository / CLI command | `bored-william` |
| Python package & module | `bored_william` |
| Output artifacts | `bored-william` in headers and `run_meta.json` |

The underscore in the package name is forced by Python identifier syntax; the hyphenated
form is canonical everywhere a hyphen is legal.

---

## Purpose and non-goals

`bored-william` takes one row per billboard and emits one row per **capture** — every
historical Street View panorama at that location, aimed at the board, with the metadata
needed to reproduce the shot. The images and manifest are then fed to an LLM for advertiser
and creative extraction, which happens **outside this tool**.

### Explicit non-goals

- No LLM calls, no OCR, no advertiser extraction — this tool only produces inputs
- No billboard detection or cropping — frames are shot wide, deliberately
- No reverse geocoding — city comes free from the metadata or not at all
- No image redistribution — the manifest is the publishable artifact
- No contact sheets or generated review pages — captures are reviewed in the file explorer

---

## Input

A CSV, one row per billboard. Two columns are meaningful; unknown columns pass through
untouched to the output so users can carry their own annotations.

| Column | Required | Notes |
|---|---|---|
| `link` | yes | Accepts `maps.app.goo.gl/*` short links or full `google.com/maps/@...` URLs. Long form skips a resolution round trip. |
| `site_label` | no | Human identifier for the billboard site. Labels the *structure*, not the ad — the advertiser changes per capture and is extracted downstream. |
| `disambiguation_hint` | no | Free text, e.g. `right of Shell sign`. Carried to the manifest so the extraction step can resolve which board is the subject when several are in frame. |

---

## Pipeline

**01 · Resolve the link.** Follow redirects to the full Maps URL. Parse out the pano ID and
the reference camera — heading, tilt and zoom from the `@lat,lng,{z}y,{h}h,{t}t` segment.
Reject non-Street-View links (place pins, directions) with a clear error rather than failing
later. *One request; skipped for long-form URLs.*

**02 · Enumerate the capture history.** One `photometa` call returns every historical
panorama at the location — ID, year, month, coordinates — plus road name, locality string,
copyright, panorama orientation and neighbours. This is one call per *input row*, not per
capture. It is the whole reason the manual work collapses. *One request per billboard.*

**03 · Locate the target.** Project a point from the reference panorama along its own heading
at an assumed distance. That synthetic coordinate stands in for the billboard; precision is
not required because framing is wide. *No requests.*

**04 · Aim and fetch each capture.** For every historical panorama: compute a fresh bearing to
the target, derive pitch, request the image at native sampling. Re-aiming per capture is
mandatory — observed yaw spread across one location's history was **6.96°**, wider than a
tight field of view. *One request per capture, rate limited.*

**05 · Write manifest and images.** Hash each image, emit a manifest row, and optionally emit
a redistribution-safe variant with internal columns stripped. *No requests.*

---

## Aiming

Three parameters define the virtual camera. All are derived; none are hand-entered.

### Target position

```
lat_t = lat_r + (d * cos θ) / 111320
lng_t = lng_r + (d * sin θ) / (111320 * cos φ_r)
```

Where `θ` is the reference link's heading and `d` is `--assumed-distance` (default **90 m**,
a typical freeway setback).

### Yaw, per capture

Standard great-circle forward azimuth from each panorama's coordinates to the target. This is
the field that must be recomputed for every capture rather than copied from the input link.

### Pitch and field of view

```
pitch = -degrees( atan2(h, d) )    h = --assumed-height, default 8 m
fov   = --fov, default 45 deg
width = round(fov * 45.5)          -> 2048 px at the default
```

**45.5 px/°** is the panorama's native angular resolution (16384 px ÷ 360°). Requesting more
upscales; requesting less discards real detail. The script computes width from fov rather
than hardcoding it.

> **⚠ Sign conventions differ between the two endpoints.**
> The imagery endpoint treats **negative pitch as up**. The Maps URL format treats
> **positive pitch as up**. The permalink writer must negate. Getting this backwards aims
> every published link at the pavement, and it is the single most likely silent bug in the build.

Tolerance is generous by design. With panoramas clustered within roughly 10 m, a distance
estimate wrong by a factor of two still shifts the bearing only about 6° — comfortably inside
a 45° frame. Shooting wide costs nothing, because tightening the frame cannot add detail:
pixels-on-billboard is fixed by the panorama's angular resolution and the board's angular
size, neither of which framing changes.

The 45° default is a starting point to be validated against a trial run on a sample of
locations before a full corridor trawl.

---

## Output layout

Flat by default. `--group-by-site` adds one directory level so a single site's captures can
be reviewed together.

```
out/                                    out/
  images/                                 images/
    zenni-sfo_2025-10_0YxzCIFH.jpg          zenni-sfo/
    zenni-sfo_2023-05_nHUNEES9.jpg            zenni-sfo_2025-10_0YxzCIFH.jpg
    shell-plaza_2025-10_1aqBu_z2.jpg          zenni-sfo_2023-05_nHUNEES9.jpg
  manifest.csv                              shell-plaza/
  manifest_public.csv   (--public)            shell-plaza_2025-10_1aqBu_z2.jpg
  neighbor_panos.csv    (--include-…)     manifest.csv
  run_meta.json                           run_meta.json

        default                              --group-by-site
```

Filenames keep their full form in both modes, so a file remains identifiable after being
moved or copied out of its directory.

### Site directory naming

Derived from `site_label` when present, otherwise from `link`:

1. Lowercase. For links, strip scheme and host first.
2. Replace `< > : " / \ | ? *`, control characters and whitespace with `-`.
3. Collapse repeated `-`; trim leading and trailing `-` and `.`
   (Windows forbids trailing dots and spaces in directory names).
4. Truncate to 64 characters.
5. If the result matches a Windows reserved device name — `CON`, `PRN`, `AUX`, `NUL`,
   `COM0`–`COM9`, `LPT0`–`LPT9` — prefix with `_`.
6. If empty after all of the above, fall back to the reference `pano_id`.

Two input rows that resolve to the same directory name but carry different links get `-2`,
`-3` suffixes. The path actually written is always recorded in `image_file`, which is
relative to `outdir` in both modes — so the manifest stays the single source of truth for
where a file landed.

---

## Output manifest

One row per capture. **public** columns are safe to redistribute — facts and hashes.
**internal** columns are withheld under `--public`.

### Identity and provenance

| Column | Type | Source and rationale | |
|---|---|---|---|
| `row_uuid` | uuid | UUIDv5 over `pano_id` — deterministic, so reruns stay diffable | public |
| `pano_id` | str | Natural key. Globally unique already | public |
| `site_label` | str? | Input, echoed | public |
| `disambiguation_hint` | str? | Input, echoed | public |
| `source_link` | url | Input, echoed | public |
| `capture_permalink` | url | Built as `maps/@?api=1&map_action=pano&pano=...` | public |
| `image_sha256` | hex | Lets others verify a regenerated image is byte-identical | public |
| `fetched_at_utc` | iso8601 | Run clock. Panoramas get re-rendered and retired | public |
| `script_version` | semver | Constant | public |

### Capture facts — free from photometa

| Column | Type | Source and rationale | |
|---|---|---|---|
| `capture_date` | YYYY-MM | Month precision only; there is no day | public |
| `date_precision` | enum | Always `month`. Explicit so downstream never assumes a day | public |
| `pano_lat` | float | Per-capture position | public |
| `pano_lng` | float | Per-capture position | public |
| `road_name` | str? | Returned verbatim, e.g. `US-101` | public |
| `locality_raw` | str? | Unparsed display string, e.g. `South San Francisco, California` | public |
| `city` | str? | Best-effort split of `locality_raw`. Nullable, never inferred | public |
| `region` | str? | Best-effort split. Country is usually absent — left null, not guessed | public |
| `pano_type` | enum | `google` or `photosphere` | public |
| `copyright_string` | str | Carries the attribution obligation with the data | public |
| `pano_heading_deg` | float | Orientation of the capture vehicle, not the virtual camera | public |
| `pano_tilt_deg` | float | As above | public |
| `pano_roll_deg` | float | As above. Roll exists only here — it is not a renderable parameter | public |

### Render parameters — reproducibility

| Column | Type | Source and rationale | |
|---|---|---|---|
| `view_yaw_deg` | float | Computed bearing to target | public |
| `view_pitch_deg` | float | Imagery-endpoint convention, negative is up | public |
| `fov_deg` | float | Without this the image cannot be reproduced | public |
| `image_width_px` | int | Derived from fov at native sampling | public |
| `image_height_px` | int | Two thirds of width | public |
| `image_file` | path | Relative to `outdir`; reflects `--group-by-site` | public |

### Quality signals — computed, no network

| Column | Type | Source and rationale | |
|---|---|---|---|
| `px_per_degree` | float | Width ÷ fov. The legibility predictor | public |
| `est_distance_m` | float | Panorama to target. Flags boards too far to read | public |
| `est_board_angular_width_deg` | float | How much frame the board occupies | public |
| `assumed_board_width_m` | float | The assumption behind the two estimates above, recorded so they can be reinterpreted | public |

### Status

| Column | Type | Source and rationale | |
|---|---|---|---|
| `status` | enum | `ok` or an error code. Rows are never silently dropped | public |
| `error_message` | str? | Populated on failure | public |
| `image_source_url` | url | The scrape endpoint. Withheld from public output — pano IDs are neutral identifiers, a column of live scrape URLs is the technique | **internal** |

Per-site capture interval is deliberately **not** a column. It is implicit in the dataset —
derivable by differencing `capture_date` within a `site_label` group — and belongs to
analysis rather than collection.

---

## Interface

```
bored-william --input boards.csv --outdir ./out [options]

  --fov DEG              default 45     wide by design, see Aiming
  --assumed-distance M   default 90     panorama to board, low sensitivity
  --assumed-height M     default 8      board centre above camera
  --rate-limit MS        default 250    between all requests
  --group-by-site                       one directory per site under images/
  --include-photospheres                include user-contributed panoramas
  --public                              also write manifest_public.csv
  --include-neighbors                   also write neighbor_panos.csv
  --dates-only                          enumerate captures, fetch no images
  --resume                              skip captures already in the manifest
  --dry-run                             resolve and enumerate, write nothing
```

User-contributed photospheres are **excluded by default** — their quality and positioning are
too variable to mix into a consistent series. `pano_type` is still recorded for any that are
included via the flag.

`--dates-only` is the cheap survey pass: one request per billboard tells you how many captures
exist and over what span, before committing to a full image pull. It is the natural first step
of a trial run.

---

## Failure modes

| Code | Cause | Handling |
|---|---|---|
| `LINK_UNRESOLVED` | Redirect chain fails or times out | Row error, continue |
| `NOT_STREETVIEW` | Place pin or directions link, no panorama | Row error with guidance |
| `PHOTOMETA_FAILED` | Malformed or empty response | Row error, retain raw for debugging |
| `NO_HISTORY` | Location has a single capture | **Not an error** — emit one row |
| `IMAGE_FORBIDDEN` | Missing browser User-Agent | Fatal, fail fast — misconfiguration, not data |
| `IMAGE_MISSING` | Panorama retired between enumeration and fetch | Row error, manifest row retained |
| `RATE_LIMITED` | 429 or 5xx | Exponential backoff, three retries, then row error |

> **Verified during prototyping.** The imagery endpoint rejects requests without a browser
> User-Agent — curl's default returns `403 PERMISSION_DENIED`. The script sets one explicitly
> rather than relying on an HTTP library's default, which is what made this work by accident
> during testing.

---

## Throughput

Requests total `rows + sum(captures)`. At the observed density of about 29 captures per site,
200 billboards is roughly **6,000 requests** — around **25 minutes** at the default 250 ms
spacing, and on the order of 400 MB of images. `--dates-only` costs 200 requests and under a
minute.

Rate limiting is deliberate and not configurable below 100 ms. Politeness is what keeps this
working.
