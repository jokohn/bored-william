"""Output table: append-safe writes and resume.

Mirrors the fetch stage's manifest writer, and for the same reason -- a run
over thousands of images will be interrupted, and restarting from scratch here
costs money rather than just time. Opening the file in truncate mode after
reading it to decide what to skip is exactly the bug that made `--resume`
destructive in stage 1; this opens for append when resuming.
"""

import csv
import json
import threading

from .schema import COLUMNS


def _existing_header(path):
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            return next(csv.reader(fh), None) or None
    except FileNotFoundError:
        return None


def completed(path):
    """Images already recorded as ok, for --resume.

    Only `ok` rows count. A previous failure should be retried, not inherited.
    """
    done = set()
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("status") == "ok" and row.get("image_file"):
                    done.add(row["image_file"])
    except FileNotFoundError:
        pass
    return done


def encode(value):
    """CSV-safe scalar. Enums become their value, lists become JSON."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (list, tuple)):
        return json.dumps([encode(v) for v in value])
    if hasattr(value, "value"):          # enum member
        return value.value
    if hasattr(value, "model_dump"):     # pydantic model, e.g. BBox
        return json.dumps(value.model_dump())
    return value


class Writer:
    """Row-at-a-time, flushed immediately, safe across worker threads.

    Flushing per row means an interrupted run leaves a resumable file rather
    than a buffer that never reached disk.
    """

    def __init__(self, path, append=False):
        header = _existing_header(path) if append else None
        self.columns = header or COLUMNS
        self._fh = open(path, "a" if header else "w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(
            self._fh, fieldnames=self.columns, extrasaction="ignore"
        )
        if not header:
            self._writer.writeheader()
            self._fh.flush()
        self._lock = threading.Lock()

    def write(self, row):
        encoded = {k: encode(v) for k, v in row.items()}
        with self._lock:
            self._writer.writerow(encoded)
            self._fh.flush()

    def close(self):
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
