"""Reader entry point: gate -> crop -> extract -> derive, per image."""

import argparse
import csv
import json
import os
import random
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import anthropic

from .. import __version__
from . import crop as crop_mod
from . import prompts, store
from .client import ConfigurationError, DEFAULT_MODEL, Reader, RefusalError
from .schema import (
    NAICS_VINTAGE,
    NOTHING_TO_READ,
    TAXONOMY_VERSION,
    DeriveResult,
    GateResult,
    extract_model,
)

# Approximate, for reporting only. Real spend comes from the console; this is
# here so a pilot ends with a measured number rather than a guess carried
# forward from the spec.
PRICING = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def build_parser():
    p = argparse.ArgumentParser(
        prog="bored-william read",
        description="Turn captured billboard images into a machine-readable "
                    "dataset of what each board said and who was advertising.",
    )
    p.add_argument("--manifest", required=True, help="fetch stage manifest.csv")
    p.add_argument("--outdir", required=True, help="output directory")
    p.add_argument("--images-root", default=None,
                   help="root that manifest image_file paths are relative to "
                        "(default: the manifest's own directory)")
    p.add_argument("--boards", default=None,
                   help="billboards.csv, for disambiguation hints (gate only)")
    p.add_argument("--sample", type=int, default=None, metavar="N",
                   help="read only N images -- the pilot pass")
    p.add_argument("--stratify-by-year", action="store_true",
                   help="spread --sample evenly across capture years rather "
                        "than uniformly at random")
    p.add_argument("--seed", type=int, default=None,
                   help="sampling seed; recorded in run_meta.json")
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help="model for extract and derive (default: %(default)s)")
    p.add_argument("--gate-model", default=None,
                   help="model for the gate pass (default: same as --model). "
                        "Not a place to economise: the gate localises as well "
                        "as classifies, and a loose bounding box degrades "
                        "every stage downstream of it")
    p.add_argument("--concurrency", type=int, default=4, metavar="N",
                   help="parallel images in flight (default: %(default)s)")
    p.add_argument("--resume", action="store_true",
                   help="skip images already recorded as ok")
    p.add_argument("--no-keep-crops", action="store_true",
                   help="delete crops after reading; they are kept by default "
                        "because the advertiser audit needs the picture")
    p.add_argument("--html", action="store_true",
                   help="also produce an HTML reproduction of each board. "
                        "Off by default: it is the only field generating "
                        "hundreds to low-thousands of output tokens, and "
                        "output bills at five times input, so it roughly "
                        "doubles the cost of a run")
    p.add_argument("--dry-run", action="store_true",
                   help="resolve the work list and print it, call nothing")
    return p


def load_rows(manifest_path, images_root):
    """Readable captures from the fetch manifest, in manifest order."""
    rows = []
    with open(manifest_path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r.get("status") != "ok" or not r.get("image_file"):
                continue
            rows.append({
                "image_file": r["image_file"],
                "abs_path": os.path.join(images_root, r["image_file"]),
                "capture_date": r.get("capture_date", ""),
                "source_link": r.get("source_link", ""),
            })
    return rows


def load_hints(boards_path):
    """source_link -> disambiguation hint.

    Consumed by the gate only. See prompts.gate_user_text for why it never
    reaches extraction.
    """
    if not boards_path:
        return {}
    hints = {}
    with open(boards_path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            link = (r.get("link") or "").strip()
            hint = (r.get("disambiguation_hint") or "").strip()
            if link and hint:
                hints[link] = hint
    return hints


def pick_sample(rows, n, stratify, seed):
    """Choose n images, optionally spread across capture years.

    Stratification here allocates evenly per year rather than proportionally.
    The pilot's job is to discover the vocabulary space, and advertiser mix in
    2008 looks nothing like 2026 -- a proportional draw would build the
    taxonomy almost entirely from the recent end, where captures are far
    denser, and handle the early corpus badly.
    """
    rng = random.Random(seed)
    if n is None or n >= len(rows):
        return rows
    if not stratify:
        return rng.sample(rows, n)

    by_year = defaultdict(list)
    for r in rows:
        by_year[(r["capture_date"] or "????")[:4]].append(r)

    years = sorted(by_year)
    chosen, remaining = [], n
    # Several passes: years with fewer images than their allocation give the
    # shortfall back to the years that still have supply.
    while remaining > 0 and years:
        share = max(1, remaining // len(years))
        progressed = False
        for year in list(years):
            if remaining <= 0:
                break
            pool = by_year[year]
            take = min(share, len(pool), remaining)
            if take <= 0:
                years.remove(year)
                continue
            picked = rng.sample(pool, take)
            for item in picked:
                pool.remove(item)
            chosen.extend(picked)
            remaining -= take
            progressed = True
            if not pool:
                years.remove(year)
        if not progressed:
            break
    return chosen


def blank_row(image_file, status, message, models, now):
    from .schema import COLUMNS
    row = {c: None for c in COLUMNS}
    row.update({
        "image_file": image_file,
        "gate_model": models[0],
        "extract_model": models[1],
        "prompt_version": prompts.PROMPT_VERSION,
        "taxonomy_version": TAXONOMY_VERSION,
        "extracted_at_utc": now,
        "status": status,
        "error_message": message,
    })
    return row


def read_one(item, reader, hints, crops_dir, keep_crops, want_html=False):
    """Run the three passes for one image. Always returns a row."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    models = (reader.gate_model, reader.model)
    image_file = item["image_file"]
    row = blank_row(image_file, "ok", None, models, now)

    # --- pass 1: gate -------------------------------------------------------
    try:
        width, height = crop_mod.dimensions(item["abs_path"])
        hint = hints.get(item["source_link"])
        gate = reader.gate(
            item["abs_path"],
            prompts.gate_user_text(width, height, hint),
            GateResult,
        )
    except ConfigurationError:
        raise
    except RefusalError as exc:
        return blank_row(image_file, "REFUSAL", str(exc), models, now)
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
        return blank_row(image_file, "API_ERROR", "gate: %s" % exc, models, now)
    except Exception as exc:  # noqa: BLE001 - one bad image must not end the run
        return blank_row(image_file, "GATE_FAILED",
                         "%s: %s" % (type(exc).__name__, exc), models, now)

    row.update({
        "readability": gate.readability,
        "board_state": gate.board_state,
        "board_format": gate.board_format,
        "bbox": gate.bbox,
    })

    # Nothing left to read: not an error, and not worth paying to extract.
    # obstructed_major still proceeds -- a board where only a logo survives is
    # worth a partial row.
    unreadable = set(gate.readability) & NOTHING_TO_READ
    if unreadable or gate.bbox is None or (
        gate.board_state is not None and gate.board_state.value == "vacant"
    ):
        row["status"] = "NOTHING_TO_READ"
        return row

    # --- crop ---------------------------------------------------------------
    crop_path = os.path.join(crops_dir, os.path.basename(image_file))
    try:
        crop_mod.crop_to(item["abs_path"], gate.bbox, crop_path)
    except crop_mod.InvalidCrop as exc:
        row.update({"status": "CROP_INVALID", "error_message": str(exc)})
        return row
    except Exception as exc:  # noqa: BLE001
        row.update({"status": "CROP_INVALID",
                    "error_message": "%s: %s" % (type(exc).__name__, exc)})
        return row

    # --- pass 2: extract ----------------------------------------------------
    try:
        ex = reader.extract(crop_path, prompts.EXTRACT_USER_TEXT,
                            extract_model(want_html))
    except ConfigurationError:
        raise
    except RefusalError as exc:
        row.update({"status": "REFUSAL", "error_message": str(exc)})
        return row
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
        row.update({"status": "API_ERROR", "error_message": "extract: %s" % exc})
        return row
    except Exception as exc:  # noqa: BLE001
        row.update({"status": "EXTRACT_FAILED",
                    "error_message": "%s: %s" % (type(exc).__name__, exc)})
        return row
    finally:
        if not keep_crops and os.path.exists(crop_path):
            os.remove(crop_path)

    row.update({
        "text_verbatim": ex.text_verbatim,
        "html_replica": getattr(ex, "html_replica", None),
        "advertiser_name_shown": ex.advertiser_name_shown,
        "advertiser_url_shown": ex.advertiser_url_shown,
        "product_named": ex.product_named,
        "offering_type": ex.offering_type,
        "operator_shown": ex.operator_shown,
        "language": ex.language,
    })

    # --- pass 3: derive -----------------------------------------------------
    # Text only, no image. Deriving ad copy from the transcription rather than
    # from the board guarantees it is a subset of transcribed text instead of
    # a second, independent reading that could disagree with the first.
    if ex.text_verbatim:
        try:
            derived = reader.derive(
                prompts.derive_user_text(ex.text_verbatim), DeriveResult
            )
            row["ad_copy"] = derived.ad_copy
        except Exception as exc:  # noqa: BLE001 - keep the extraction
            row["error_message"] = "derive failed: %s" % exc
    return row


def main(argv=None):
    opts = build_parser().parse_args(argv)
    images_root = opts.images_root or os.path.dirname(
        os.path.abspath(opts.manifest)) or "."

    rows = load_rows(opts.manifest, images_root)
    if not rows:
        raise SystemExit("error: no ok rows with images in %s" % opts.manifest)

    seed = opts.seed if opts.seed is not None else random.randrange(2 ** 31)
    work = pick_sample(rows, opts.sample, opts.stratify_by_year, seed)

    if opts.dry_run:
        years = defaultdict(int)
        for item in work:
            years[(item["capture_date"] or "????")[:4]] += 1
        print("%d of %d images selected" % (len(work), len(rows)))
        for year in sorted(years):
            print("  %s  %d" % (year, years[year]))
        if opts.sample:
            print("\nseed %d%s" % (seed, ", stratified by year"
                                   if opts.stratify_by_year else ""))
        return 0

    os.makedirs(opts.outdir, exist_ok=True)
    crops_dir = os.path.join(opts.outdir, "crops")
    os.makedirs(crops_dir, exist_ok=True)
    out_path = os.path.join(opts.outdir, "readings.csv")

    if opts.resume:
        done = store.completed(out_path)
        before = len(work)
        work = [w for w in work if w["image_file"] not in done]
        print("resuming: %d of %d already read" % (before - len(work), before),
              file=sys.stderr)
    if not work:
        print("nothing to do", file=sys.stderr)
        return 0

    hints = load_hints(opts.boards)
    reader = Reader(model=opts.model, gate_model=opts.gate_model,
                    extract_max_tokens=None if opts.html else 4000)
    reader.with_prompts(prompts.GATE_SYSTEM,
                        prompts.extract_system(opts.html),
                        prompts.DERIVE_SYSTEM)

    counts = defaultdict(int)
    lock = threading.Lock()
    started = datetime.now(timezone.utc)

    try:
        with store.Writer(out_path, append=opts.resume) as writer:
          with ThreadPoolExecutor(max_workers=opts.concurrency) as pool:
              futures = {
                  pool.submit(read_one, item, reader, hints, crops_dir,
                              not opts.no_keep_crops, opts.html): item
                  for item in work
              }
              for i, future in enumerate(as_completed(futures), 1):
                  item = futures[future]
                  try:
                      row = future.result()
                  except ConfigurationError:
                      # A rejected request is not a property of this image. Let
                      # it out rather than writing thousands of identical rows.
                      raise
                  except Exception as exc:  # noqa: BLE001
                      row = blank_row(item["image_file"], "ERROR",
                                      "%s: %s" % (type(exc).__name__, exc),
                                      (reader.gate_model, reader.model),
                                      datetime.now(timezone.utc).isoformat(
                                          timespec="seconds"))
                  writer.write(row)
                  with lock:
                      counts[row["status"]] += 1
                  print("[%d/%d] %-52s %s" % (i, len(work),
                                              item["image_file"][-52:],
                                              row["status"]), file=sys.stderr)
    except ConfigurationError as exc:
        print("\nfatal: %s" % exc, file=sys.stderr)
        print("\nThis fails identically on every image, so the run stopped "
              "rather than repeating it. Rows already written are kept; "
              "rerun with --resume once the configuration is fixed.",
              file=sys.stderr)
        return 2

    finished = datetime.now(timezone.utc)
    usage = reader.usage.as_dict()
    rate_in, rate_out = PRICING.get(opts.model, (None, None))
    est_cost = None
    if rate_in:
        est_cost = round(
            usage["input_tokens"] / 1e6 * rate_in
            + usage["output_tokens"] / 1e6 * rate_out, 2)

    meta = {
        "tool": "bored-william-read",
        "version": __version__,
        "prompt_version": prompts.PROMPT_VERSION,
        "taxonomy_version": TAXONOMY_VERSION,
        "naics_vintage": NAICS_VINTAGE,
        "html_replica_enabled": bool(opts.html),
        "argv": sys.argv[1:] if argv is None else list(argv),
        "model": opts.model,
        "gate_model": opts.gate_model or opts.model,
        "sample_seed": seed if opts.sample else None,
        "stratified_by_year": bool(opts.stratify_by_year and opts.sample),
        "images_read": len(work),
        "status_counts": dict(counts),
        "usage": usage,
        "estimated_cost_usd": est_cost,
        "started_utc": started.isoformat(timespec="seconds"),
        "finished_utc": finished.isoformat(timespec="seconds"),
        "elapsed_s": round((finished - started).total_seconds(), 1),
    }
    with open(os.path.join(opts.outdir, "read_meta.json"), "w",
              encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)

    print("\n%d images in %ss" % (len(work), meta["elapsed_s"]), file=sys.stderr)
    for status in sorted(counts):
        print("  %-18s %d" % (status, counts[status]), file=sys.stderr)
    if est_cost is not None:
        print("  %-18s ~$%.2f (%d calls, %s in / %s out)"
              % ("estimated cost", est_cost, usage["calls"],
                 f"{usage['input_tokens']:,}", f"{usage['output_tokens']:,}"),
              file=sys.stderr)
    return 0
