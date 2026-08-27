"""Measure where each billboard actually is, from imagery already captured.

The fetch stage aims at a synthetic target projected along the reference link's
heading at an assumed distance. When the assumption is wrong the board drifts
off frame centre -- and at a tight field of view it drifts clean out of frame.
Supplying the real setback per site fixes that, but measuring 177 boards by
hand is exactly the data entry this project set out to avoid.

It does not have to be measured by hand. A wide-field corpus already frames
every board; running the gate over a handful of its captures gives a bounding
box per frame, each box gives a bearing to the board, and bearings taken from
panoramas metres apart intersect where the board really stands.

Output is the input CSV with `board_lat`, `board_lng` and `assumed_height`
filled in, ready to hand back to the fetch stage.
"""

import argparse
import csv
import json
import os
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from .. import __version__
from . import prompts, triangulate
from .client import DEFAULT_MODEL, Reader, RefusalError
from .crop import dimensions
from .schema import GateResult, Readability

# Sightings per site. Two is the theoretical minimum, but boards get occluded
# and gates return nothing, so this oversamples to leave usable rays after
# attrition. Beyond about six the intersection stops improving -- the panoramas
# are clustered, so extra frames add little new baseline.
DEFAULT_PER_SITE = 6

# A ray whose frame reported these is not a sighting of the board.
UNUSABLE = {Readability.NOT_IN_FRAME, Readability.FULLY_OBSTRUCTED}


def build_parser():
    p = argparse.ArgumentParser(
        prog="bored-william calibrate",
        description="Measure each billboard's real position from an existing "
                    "wide-field capture set, so a tighter re-fetch can aim at "
                    "it directly.",
    )
    p.add_argument("--manifest", required=True,
                   help="manifest.csv from a wide-field fetch run")
    p.add_argument("--boards", required=True,
                   help="the billboards.csv that produced it")
    p.add_argument("--outdir", required=True)
    p.add_argument("--images-root", default=None,
                   help="root for manifest image_file paths "
                        "(default: the manifest's directory)")
    p.add_argument("--per-site", type=int, default=DEFAULT_PER_SITE, metavar="N",
                   help="frames to sight per site (default: %(default)s)")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--concurrency", type=int, default=4, metavar="N")
    p.add_argument("--dry-run", action="store_true",
                   help="report the work list and cost shape, call nothing")
    return p


def pick_frames(manifest_path, images_root, per_site):
    """Choose sighting frames per site, spread across the capture history.

    Frames are taken evenly across the site's date range rather than
    consecutively. Adjacent captures often come from nearly the same spot,
    which adds rays without adding baseline -- and baseline is the only thing
    that turns bearings into a position.
    """
    by_link = defaultdict(list)
    with open(manifest_path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r.get("status") != "ok" or not r.get("image_file"):
                continue
            by_link[r["source_link"]].append(r)

    chosen = {}
    for link, rows in by_link.items():
        rows.sort(key=lambda r: r.get("capture_date", ""))
        if len(rows) <= per_site:
            picked = rows
        else:
            step = (len(rows) - 1) / (per_site - 1)
            picked = [rows[int(round(i * step))] for i in range(per_site)]
        for r in picked:
            r["abs_path"] = os.path.join(images_root, r["image_file"])
        chosen[link] = picked
    return chosen


def sight(row, reader):
    """Gate one frame and convert its bounding box into a bearing.

    Returns an observation, or None when the frame does not contain a usable
    view of the board.
    """
    width, height = dimensions(row["abs_path"])
    gate = reader.gate(
        row["abs_path"],
        prompts.gate_user_text(width, height, None),
        GateResult,
    )
    if gate.bbox is None or (set(gate.readability) & UNUSABLE):
        return None

    cx = gate.bbox.x + gate.bbox.w / 2.0
    cy = gate.bbox.y + gate.bbox.h / 2.0
    bearing, elevation = triangulate.view_angles(
        cx, cy, width, height,
        float(row["fov_deg"]),
        float(row["view_yaw_deg"]),
        float(row["view_pitch_deg"]),
    )
    return {
        "lat": float(row["pano_lat"]),
        "lng": float(row["pano_lng"]),
        "bearing": bearing,
        "elevation": elevation,
        "capture_date": row.get("capture_date", ""),
    }


def calibrate_site(link, rows, reader):
    """All sightings for one site, solved into a position."""
    observations, failures = [], []
    for row in rows:
        try:
            obs = sight(row, reader)
        except RefusalError as exc:
            failures.append("refusal: %s" % exc)
            continue
        except Exception as exc:  # noqa: BLE001 - a bad frame is not a bad site
            failures.append("%s: %s" % (type(exc).__name__, exc))
            continue
        if obs is None:
            failures.append("no usable board in frame")
        else:
            observations.append(obs)

    result = {
        "source_link": link,
        "frames_tried": len(rows),
        "sightings": len(observations),
        "status": "ok",
        "note": "",
    }
    try:
        result.update(triangulate.solve(observations))
    except triangulate.Unsolvable as exc:
        result.update({"status": "UNSOLVED", "note": str(exc)})
    if failures and result["status"] == "ok":
        result["note"] = "%d frame(s) unusable" % len(failures)
    elif failures:
        result["note"] = "%s; %d frame(s) unusable" % (result["note"], len(failures))
    return result


def write_outputs(outdir, boards_path, results):
    """calibration.csv for the diagnostics, plus a ready-to-run boards CSV.

    The calibrated CSV keeps every original column and fills only the aiming
    ones, so it drops straight back into the fetch stage. Unsolved sites keep
    whatever they already had rather than inheriting a fabricated number.
    """
    by_link = {r["source_link"]: r for r in results}

    diag_cols = ["source_link", "status", "sightings", "frames_tried",
                 "board_lat", "board_lng", "distance_m", "height_m",
                 "bearing_spread_deg", "residual_m", "note"]
    with open(os.path.join(outdir, "calibration.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=diag_cols, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow(r)

    with open(boards_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        original = [r for r in reader
                    if any((v or "").strip() for v in r.values())]
        cols = list(reader.fieldnames or [])
    for extra in ("board_lat", "board_lng", "assumed_height"):
        if extra not in cols:
            cols.append(extra)

    filled = 0
    for row in original:
        got = by_link.get((row.get("link") or "").strip())
        if got and got["status"] == "ok":
            row["board_lat"] = got["board_lat"]
            row["board_lng"] = got["board_lng"]
            row["assumed_height"] = got["height_m"]
            filled += 1

    out = os.path.join(outdir, "billboards_calibrated.csv")
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(original)
    return filled, len(original), out


def main(argv=None):
    opts = build_parser().parse_args(argv)
    images_root = opts.images_root or os.path.dirname(
        os.path.abspath(opts.manifest)) or "."

    frames = pick_frames(opts.manifest, images_root, opts.per_site)
    if not frames:
        raise SystemExit("error: no ok rows with images in %s" % opts.manifest)
    total_frames = sum(len(v) for v in frames.values())

    if opts.dry_run:
        print("%d sites, %d frames to sight (%d per site)"
              % (len(frames), total_frames, opts.per_site))
        print("one gate call per frame, at low effort")
        return 0

    os.makedirs(opts.outdir, exist_ok=True)
    reader = Reader(model=opts.model).with_prompts(
        prompts.GATE_SYSTEM, prompts.EXTRACT_SYSTEM, prompts.DERIVE_SYSTEM
    )

    results = []
    lock = threading.Lock()
    started = datetime.now(timezone.utc)

    with ThreadPoolExecutor(max_workers=opts.concurrency) as pool:
        futures = {
            pool.submit(calibrate_site, link, rows, reader): link
            for link, rows in frames.items()
        }
        for i, future in enumerate(as_completed(futures), 1):
            link = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                result = {"source_link": link, "status": "ERROR",
                          "sightings": 0, "frames_tried": len(frames[link]),
                          "note": "%s: %s" % (type(exc).__name__, exc)}
            with lock:
                results.append(result)
            print("[%d/%d] %-46s %s  %s" % (
                i, len(frames), link[-46:], result["status"],
                ("%.0f m" % result["distance_m"]) if result.get("distance_m") else ""),
                file=sys.stderr)

    filled, total, out_path = write_outputs(opts.outdir, opts.boards, results)
    finished = datetime.now(timezone.utc)
    solved = [r for r in results if r["status"] == "ok"]

    meta = {
        "tool": "bored-william-calibrate",
        "version": __version__,
        "prompt_version": prompts.PROMPT_VERSION,
        "model": opts.model,
        "per_site": opts.per_site,
        "sites": len(frames),
        "frames_sighted": total_frames,
        "solved": len(solved),
        "unsolved": len(results) - len(solved),
        "rows_filled": filled,
        "usage": reader.usage.as_dict(),
        "started_utc": started.isoformat(timespec="seconds"),
        "finished_utc": finished.isoformat(timespec="seconds"),
        "elapsed_s": round((finished - started).total_seconds(), 1),
    }
    with open(os.path.join(opts.outdir, "calibrate_meta.json"), "w",
              encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)

    print("\n%d/%d sites solved, %d rows filled in %s"
          % (len(solved), len(results), filled, os.path.basename(out_path)),
          file=sys.stderr)
    if solved:
        ds = sorted(r["distance_m"] for r in solved)
        print("  distance: min %.0f m, median %.0f m, max %.0f m"
              % (ds[0], ds[len(ds) // 2], ds[-1]), file=sys.stderr)
    return 0
