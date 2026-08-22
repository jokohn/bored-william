"""Stage 05 -- the manifest.

The manifest, not the image directory, is the publishable artifact: Street View
imagery is Google's copyrighted work, while panorama ids, coordinates, dates
and hashes are facts. `--public` strips the one column that is neither -- the
live scrape URL, which is the collection technique rather than a finding.
"""

import csv
import uuid

# Stable namespace so row_uuid is reproducible across machines and runs.
_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "bored-william")

# Withheld under --public. Panorama ids are neutral identifiers; a column of
# working scrape URLs is the method itself.
INTERNAL = {"image_source_url"}

COLUMNS = [
    # Identity and provenance
    "row_uuid",
    "pano_id",
    "site_label",
    "disambiguation_hint",
    "source_link",
    "capture_permalink",
    "image_sha256",
    "fetched_at_utc",
    "script_version",
    # Capture facts
    "capture_date",
    "date_precision",
    "pano_lat",
    "pano_lng",
    "road_name",
    "locality_raw",
    "city",
    "region",
    "pano_type",
    "copyright_string",
    "pano_heading_deg",
    "pano_tilt_deg",
    "pano_roll_deg",
    # Render parameters
    "view_yaw_deg",
    "view_pitch_deg",
    "fov_deg",
    "image_width_px",
    "image_height_px",
    "image_file",
    # Quality signals
    "px_per_degree",
    "est_distance_m",
    "est_board_angular_width_deg",
    "assumed_board_width_m",
    # Status
    "status",
    "error_message",
    "image_source_url",
]

PUBLIC_COLUMNS = [c for c in COLUMNS if c not in INTERNAL]


def _existing_header(path):
    """Column order of an existing manifest, or None if there isn't one.

    Reusing the file's own header keeps appended rows aligned even if the
    column list has changed between versions.
    """
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            header = next(csv.reader(fh), None)
    except FileNotFoundError:
        return None
    return header or None


def row_uuid(pano_id):
    """Deterministic id, so reruns stay diffable instead of churning."""
    return str(uuid.uuid5(_NAMESPACE, pano_id))


class ManifestWriter:
    """Writes manifest.csv, and optionally the public variant alongside it.

    Rows are flushed as they are produced so an interrupted run leaves a
    resumable file rather than nothing.
    """

    def __init__(self, path, public_path=None, passthrough=(), append=False):
        self.columns = COLUMNS + [c for c in passthrough if c not in COLUMNS]
        public_cols = [c for c in self.columns if c not in INTERNAL]

        # Appending is what makes --resume safe. Opening "w" here would
        # truncate a manifest whose rows were just read to decide what to
        # skip, silently destroying the work the resume exists to preserve.
        existing = _existing_header(path) if append else None
        if existing:
            self.columns = existing
            public_cols = [c for c in existing if c not in INTERNAL]

        self._fh = open(path, "a" if existing else "w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=self.columns, extrasaction="ignore")
        if not existing:
            self._writer.writeheader()

        self._public_fh = None
        if public_path:
            pub_existing = _existing_header(public_path) if append else None
            if pub_existing:
                public_cols = pub_existing
            self._public_fh = open(
                public_path, "a" if pub_existing else "w", newline="", encoding="utf-8"
            )
            self._public_writer = csv.DictWriter(
                self._public_fh, fieldnames=public_cols, extrasaction="ignore"
            )
            if not pub_existing:
                self._public_writer.writeheader()

    def write(self, row):
        self._writer.writerow(row)
        self._fh.flush()
        if self._public_fh:
            self._public_writer.writerow(row)
            self._public_fh.flush()

    def close(self):
        self._fh.close()
        if self._public_fh:
            self._public_fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def completed_pano_ids(path):
    """Panorama ids already captured successfully, for --resume.

    Only `ok` rows count: a previous failure should be retried, not skipped.
    """
    done = set()
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("status") == "ok" and row.get("pano_id"):
                    done.add(row["pano_id"])
    except FileNotFoundError:
        pass
    return done


def write_neighbors(path, rows):
    """Neighbouring panoramas, for walking a corridor programmatically."""
    cols = ["site_label", "pano_id", "lat", "lng", "pano_type"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
