"""Command line entry point and run orchestration."""

import argparse
import csv
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

from . import __version__, geo, http, imagery, manifest, naming, photometa
from .errors import BoredWilliamError, ImageForbidden

DEFAULT_FOV = 45
DEFAULT_DISTANCE_M = 90.0
DEFAULT_HEIGHT_M = 8.0
DEFAULT_BOARD_WIDTH_M = 14.63  # a 48 ft bulletin
DEFAULT_RATE_LIMIT_MS = 250

INPUT_COLUMNS = {"link", "site_label", "disambiguation_hint"}


def build_parser():
    p = argparse.ArgumentParser(
        prog="bored-william",
        description=(
            "Turn Street View share links into a reproducible image-and-"
            "metadata dataset of roadside billboards across their full "
            "capture history."
        ),
    )
    p.add_argument("--input", required=True, help="CSV with a `link` column")
    p.add_argument("--outdir", required=True, help="output directory")
    # Whole degrees only: the imagery endpoint returns 400 for a fractional
    # thumbfov. Accepting a float here and rounding it silently would make
    # fov_deg in the manifest disagree with what was actually requested.
    p.add_argument("--fov", type=int, default=DEFAULT_FOV,
                   help="field of view in whole degrees (default: %(default)s)")
    p.add_argument("--assumed-distance", type=float, default=DEFAULT_DISTANCE_M,
                   metavar="M", help="panorama to board (default: %(default)s)")
    p.add_argument("--assumed-height", type=float, default=DEFAULT_HEIGHT_M,
                   metavar="M", help="board centre above camera (default: %(default)s)")
    p.add_argument("--assumed-board-width", type=float, default=DEFAULT_BOARD_WIDTH_M,
                   metavar="M", help="board width, for legibility estimates "
                                     "(default: %(default)s)")
    p.add_argument("--rate-limit", type=int, default=DEFAULT_RATE_LIMIT_MS,
                   metavar="MS", help="spacing between requests, floor 100 "
                                      "(default: %(default)s)")
    p.add_argument("--group-by-site", action="store_true",
                   help="one directory per site under images/")
    p.add_argument("--include-photospheres", action="store_true",
                   help="include user-contributed panoramas (excluded by default)")
    p.add_argument("--public", action="store_true",
                   help="also write manifest_public.csv")
    p.add_argument("--include-neighbors", action="store_true",
                   help="also write neighbor_panos.csv")
    p.add_argument("--dates-only", action="store_true",
                   help="enumerate captures, fetch no images")
    p.add_argument("--resume", action="store_true",
                   help="skip captures already recorded as ok")
    p.add_argument("--dry-run", action="store_true",
                   help="resolve and enumerate, write nothing")
    return p


def read_input(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or "link" not in reader.fieldnames:
            raise SystemExit("error: %s has no `link` column" % path)
        passthrough = [c for c in reader.fieldnames if c not in INPUT_COLUMNS]
        return list(reader), passthrough


def _blank_row(src, status, message, version, now):
    """A row for an input that failed before any capture was reachable."""
    row = {c: "" for c in manifest.COLUMNS}
    row.update(src.get("_passthrough", {}))
    row.update({
        "site_label": src.get("site_label") or "",
        "disambiguation_hint": src.get("disambiguation_hint") or "",
        "source_link": src.get("link") or "",
        "fetched_at_utc": now,
        "script_version": version,
        "date_precision": "month",
        "status": status,
        "error_message": message,
    })
    return row


def process_row(src, opts, writer, allocator, images_root, counters, neighbor_rows):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    link = (src.get("link") or "").strip()
    site_label = (src.get("site_label") or "").strip()
    hint = (src.get("disambiguation_hint") or "").strip()
    passthrough = src.get("_passthrough", {})

    try:
        ref = resolve_reference(link)
        history = photometa.fetch_history(
            ref.pano_id, include_neighbors=opts.include_neighbors
        )
    except ImageForbidden:
        raise
    except BoredWilliamError as exc:
        counters["failed_sites"] += 1
        writer.write(_blank_row(src, exc.code, str(exc), __version__, now))
        return
    except Exception as exc:  # noqa: BLE001 - one bad row must not end the run
        counters["failed_sites"] += 1
        writer.write(_blank_row(src, "ERROR", "%s: %s" % (type(exc).__name__, exc),
                                __version__, now))
        return

    captures = history.captures
    if not opts.include_photospheres:
        captures = [c for c in captures if c.pano_type == "google"]

    if not captures:
        counters["no_history"] += 1
        row = _blank_row(src, "NO_HISTORY", "no historical captures enumerated",
                         __version__, now)
        row.update({
            "pano_id": ref.pano_id,
            "row_uuid": manifest.row_uuid(ref.pano_id),
            "pano_lat": ref.lat,
            "pano_lng": ref.lng,
            "road_name": history.road_name or "",
            "locality_raw": history.locality_raw or "",
            "copyright_string": history.copyright_string or "",
        })
        writer.write(row)
        return

    # Stage 03: stand a synthetic target where the billboard is, by walking out
    # along the heading the user framed. Precision is not needed -- the frame is
    # wide enough that a distance estimate wrong by 2x still lands the board.
    target_lat, target_lng = geo.project(
        ref.lat, ref.lng, ref.heading_deg, opts.assumed_distance
    )

    site_slug = naming.site_dir_name(site_label, link, ref.pano_id)
    site_slug = allocator.allocate(site_slug, link)

    if opts.include_neighbors and history.neighbors:
        for n in history.neighbors:
            neighbor_rows.append({
                "site_label": site_label or site_slug,
                "pano_id": n["pano_id"],
                "lat": n["lat"],
                "lng": n["lng"],
                "pano_type": n["pano_type"],
            })

    city, region = photometa.split_locality(history.locality_raw)

    for cap in captures:
        if opts.resume and cap.pano_id in counters["done"]:
            counters["skipped"] += 1
            continue

        distance = geo.distance_m(cap.lat, cap.lng, target_lat, target_lng)
        yaw = geo.bearing(cap.lat, cap.lng, target_lat, target_lng)
        pitch = geo.pitch_for(opts.assumed_height, distance)
        width = geo.width_for_fov(opts.fov)
        height = int(round(width * 2 / 3))

        src_url = imagery.image_url(cap.pano_id, yaw, pitch, width, height, opts.fov)

        row = {c: "" for c in manifest.COLUMNS}
        row.update(passthrough)
        row.update({
            "row_uuid": manifest.row_uuid(cap.pano_id),
            "pano_id": cap.pano_id,
            "site_label": site_label,
            "disambiguation_hint": hint,
            "source_link": link,
            "capture_permalink": imagery.permalink(
                cap.pano_id, yaw, geo.maps_url_pitch(pitch), opts.fov
            ),
            "fetched_at_utc": now,
            "script_version": __version__,
            "capture_date": cap.date,
            "date_precision": "month",
            "pano_lat": cap.lat,
            "pano_lng": cap.lng,
            "road_name": history.road_name or "",
            "locality_raw": history.locality_raw or "",
            "city": city or "",
            "region": region or "",
            "pano_type": cap.pano_type,
            "copyright_string": history.copyright_string or "",
            "pano_heading_deg": _num(cap.heading_deg),
            "pano_tilt_deg": _num(cap.tilt_deg),
            "pano_roll_deg": _num(cap.roll_deg),
            "view_yaw_deg": round(yaw, 2),
            "view_pitch_deg": round(pitch, 2),
            "fov_deg": opts.fov,
            "px_per_degree": round(width / opts.fov, 2),
            "est_distance_m": round(distance, 1),
            "est_board_angular_width_deg": round(
                geo.angular_width(opts.assumed_board_width, distance), 2
            ),
            "assumed_board_width_m": opts.assumed_board_width,
            "image_source_url": src_url,
            "status": "ok",
        })

        if opts.dates_only:
            counters["enumerated"] += 1
            writer.write(row)
            continue

        row.update({"image_width_px": width, "image_height_px": height})

        rel_dir = os.path.join("images", site_slug) if opts.group_by_site else "images"
        filename = naming.image_filename(site_slug, cap.date, cap.pano_id)
        rel_path = os.path.join(rel_dir, filename)

        try:
            blob = imagery.fetch_image(src_url)
        except ImageForbidden:
            raise
        except BoredWilliamError as exc:
            counters["failed_images"] += 1
            row.update({"status": exc.code, "error_message": str(exc)})
            writer.write(row)
            continue
        except Exception as exc:  # noqa: BLE001 - keep going, record the fault
            counters["failed_images"] += 1
            row.update({"status": "ERROR",
                        "error_message": "%s: %s" % (type(exc).__name__, exc)})
            writer.write(row)
            continue

        abs_dir = os.path.join(images_root, site_slug) if opts.group_by_site else images_root
        os.makedirs(abs_dir, exist_ok=True)
        with open(os.path.join(abs_dir, filename), "wb") as fh:
            fh.write(blob)

        row.update({
            "image_file": rel_path.replace("\\", "/"),
            "image_sha256": hashlib.sha256(blob).hexdigest(),
        })
        counters["captured"] += 1
        writer.write(row)


def resolve_reference(link):
    from . import resolve as _resolve
    return _resolve.resolve(link)


def _num(value):
    return "" if value is None else round(float(value), 4)


def main(argv=None):
    opts = build_parser().parse_args(argv)
    http.configure(opts.rate_limit)

    rows, passthrough = read_input(opts.input)

    for src in rows:
        src["_passthrough"] = {c: src.get(c, "") for c in passthrough}

    counters = {
        "captured": 0, "enumerated": 0, "skipped": 0,
        "failed_sites": 0, "failed_images": 0, "no_history": 0,
        "done": set(),
    }

    if opts.dry_run:
        for src in rows:
            try:
                ref = resolve_reference((src.get("link") or "").strip())
                hist = photometa.fetch_history(ref.pano_id)
                caps = hist.captures
                if not opts.include_photospheres:
                    caps = [c for c in caps if c.pano_type == "google"]
                span = "%s..%s" % (caps[0].date, caps[-1].date) if caps else "none"
                print("%-28s %3d captures  %s  %s" % (
                    (src.get("site_label") or "-")[:28], len(caps), span,
                    hist.road_name or ""))
                counters["enumerated"] += len(caps)
            except BoredWilliamError as exc:
                print("%-28s %s: %s" % ((src.get("site_label") or "-")[:28],
                                        exc.code, exc))
                counters["failed_sites"] += 1
        print("\ndry run: %d captures across %d sites, %d failed"
              % (counters["enumerated"], len(rows), counters["failed_sites"]))
        return 0

    os.makedirs(opts.outdir, exist_ok=True)
    images_root = os.path.join(opts.outdir, "images")
    if not opts.dates_only:
        os.makedirs(images_root, exist_ok=True)

    manifest_path = os.path.join(opts.outdir, "manifest.csv")
    if opts.resume:
        counters["done"] = manifest.completed_pano_ids(manifest_path)

    public_path = os.path.join(opts.outdir, "manifest_public.csv") if opts.public else None
    allocator = naming.NameAllocator()
    neighbor_rows = []
    started = datetime.now(timezone.utc)

    try:
        with manifest.ManifestWriter(
            manifest_path, public_path, passthrough, append=opts.resume
        ) as writer:
            for i, src in enumerate(rows, 1):
                label = src.get("site_label") or src.get("link") or "?"
                print("[%d/%d] %s" % (i, len(rows), label[:60]), file=sys.stderr)
                process_row(src, opts, writer, allocator, images_root,
                            counters, neighbor_rows)
    except ImageForbidden as exc:
        print("\nfatal: %s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted - manifest.csv holds completed rows; "
              "rerun with --resume", file=sys.stderr)
        return 130

    if opts.include_neighbors and neighbor_rows:
        manifest.write_neighbors(
            os.path.join(opts.outdir, "neighbor_panos.csv"), neighbor_rows)

    finished = datetime.now(timezone.utc)
    meta = {
        "tool": "bored-william",
        "version": __version__,
        "argv": sys.argv[1:] if argv is None else list(argv),
        "started_utc": started.isoformat(timespec="seconds"),
        "finished_utc": finished.isoformat(timespec="seconds"),
        "elapsed_s": round((finished - started).total_seconds(), 1),
        "input_rows": len(rows),
        "captures_written": counters["captured"] or counters["enumerated"],
        "captures_skipped": counters["skipped"],
        "sites_failed": counters["failed_sites"],
        "images_failed": counters["failed_images"],
        "sites_without_history": counters["no_history"],
    }
    with open(os.path.join(opts.outdir, "run_meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)

    print("\n%d captures from %d sites in %ss"
          % (meta["captures_written"], len(rows), meta["elapsed_s"]), file=sys.stderr)
    if counters["failed_sites"] or counters["failed_images"]:
        print("%d sites and %d images failed - see the status column"
              % (counters["failed_sites"], counters["failed_images"]), file=sys.stderr)
    return 0
