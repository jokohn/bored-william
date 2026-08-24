"""Stage 02 -- enumerate every historical panorama at a location.

One request per input row returns the whole capture history: panorama ids,
dates, positions and orientations, plus road name, locality and copyright.
This is the call that collapses the manual work, since the alternative is
stepping the time slider by hand once per capture.

The response is an undocumented, deeply nested JSON array with no field names,
so every access goes through `_dig`, which returns None rather than raising on
a shape change. A layout shift should degrade one column, not crash a run.
"""

import json
from dataclasses import dataclass
from urllib.parse import quote

from . import http
from .errors import PhotometaFailed

ENDPOINT = "https://www.google.com/maps/photometa/v1"

# Opaque request descriptor. The `2s` slot holds the panorama id.
_PB_TEMPLATE = (
    "!1m4!1smaps_sv.tactile!11m2!2m1!1b1!2m2!1sen!2sus!3m3!1m2!1e2!2s{pano}"
    "!4m57!1e1!1e2!1e3!1e4!1e5!1e6!1e8!1e12!2m1!1e1!4m1!1i48!5m1!1e1!5m1!1e2"
    "!6m1!1e1!6m1!1e2!9m36!1m3!1e2!2b1!3e2!1m3!1e2!2b0!3e3!1m3!1e3!2b1!3e2"
    "!1m3!1e3!2b0!3e3!1m3!1e8!2b0!3e3!1m3!1e1!2b0!3e3!1m3!1e4!2b0!3e3"
    "!1m3!1e10!2b1!3e2!1m3!1e10!2b0!3e3"
)

_JSON_PREFIX = ")]}'\n"

# Official car-collected panoramas carry this marker; user photospheres do not.
_OFFICIAL_MARKER = 2


@dataclass
class Capture:
    pano_id: str
    year: int
    month: int
    lat: float
    lng: float
    heading_deg: float = None
    tilt_deg: float = None
    roll_deg: float = None
    pano_type: str = "google"

    @property
    def date(self):
        return "%04d-%02d" % (self.year, self.month)


@dataclass
class SiteHistory:
    captures: list
    road_name: str = None
    locality_raw: str = None
    copyright_string: str = None
    neighbors: list = None


def _dig(node, *path):
    """Index into nested lists, returning None on any miss."""
    for key in path:
        try:
            node = node[key]
        except (IndexError, KeyError, TypeError):
            return None
    return node


def _pano_record(entry):
    """(pano_id, lat, lng, heading, tilt, roll, pano_type) from a list entry."""
    pano_id = _dig(entry, 0, 1)
    if not pano_id:
        return None
    marker = _dig(entry, 0, 0)
    coords = _dig(entry, 2, 0) or []
    orient = _dig(entry, 2, 2) or []
    return {
        "pano_id": pano_id,
        "lat": _dig(coords, 2),
        "lng": _dig(coords, 3),
        "heading_deg": _dig(orient, 0),
        "tilt_deg": _dig(orient, 1),
        "roll_deg": _dig(orient, 2),
        "pano_type": "google" if marker == _OFFICIAL_MARKER else "photosphere",
    }


def fetch_history(pano_id, include_neighbors=False):
    """Return a SiteHistory for the location containing `pano_id`."""
    url = "%s?authuser=0&hl=en&gl=us&pb=%s" % (
        ENDPOINT,
        quote(_PB_TEMPLATE.format(pano=pano_id), safe="!*"),
    )
    try:
        body, _ = http.fetch(url)
    except Exception as exc:
        raise PhotometaFailed("request failed for %s: %s" % (pano_id, exc)) from exc

    text = body.decode("utf-8", "replace")
    if text.startswith(_JSON_PREFIX):
        text = text[len(_JSON_PREFIX):]
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise PhotometaFailed("unparseable response for %s" % pano_id) from exc

    root = _dig(doc, 1, 0)
    if root is None:
        raise PhotometaFailed("no panorama block for %s" % pano_id)

    sub = _dig(root, 5, 0)
    pano_list = _dig(sub, 3, 0) or []
    time_travel = _dig(sub, 8) or []

    records = [_pano_record(e) for e in pano_list]

    captures = []
    seen = set()
    for entry in time_travel:
        idx = _dig(entry, 0)
        year = _dig(entry, 1, 0)
        month = _dig(entry, 1, 1)
        if idx is None or year is None or month is None:
            continue
        rec = records[idx] if 0 <= idx < len(records) else None
        if not rec or rec["lat"] is None or rec["pano_id"] in seen:
            continue
        seen.add(rec["pano_id"])
        captures.append(
            Capture(
                pano_id=rec["pano_id"],
                year=int(year),
                month=int(month),
                lat=rec["lat"],
                lng=rec["lng"],
                heading_deg=rec["heading_deg"],
                tilt_deg=rec["tilt_deg"],
                roll_deg=rec["roll_deg"],
                pano_type=rec["pano_type"],
            )
        )

    # The time-travel array lists the OTHER captures at a location and omits
    # the panorama currently being displayed -- the one the share link was
    # framed on. Left alone, every seed link loses exactly one capture, and
    # because links are framed from current imagery it is usually the most
    # recent one. Its date is carried separately from the array.
    if pano_id not in seen:
        ref_date = _dig(root, 6, 7)
        rec = next((r for r in records if r and r["pano_id"] == pano_id), None)
        if (rec and rec["lat"] is not None and isinstance(ref_date, list)
                and len(ref_date) >= 2 and ref_date[0] and ref_date[1]
                and 1 <= int(ref_date[1]) <= 12):
            captures.append(
                Capture(
                    pano_id=rec["pano_id"],
                    year=int(ref_date[0]),
                    month=int(ref_date[1]),
                    lat=rec["lat"],
                    lng=rec["lng"],
                    heading_deg=rec["heading_deg"],
                    tilt_deg=rec["tilt_deg"],
                    roll_deg=rec["roll_deg"],
                    pano_type=rec["pano_type"],
                )
            )

    captures.sort(key=lambda c: (c.year, c.month))

    neighbors = None
    if include_neighbors:
        neighbors = [r for r in records if r and r["lat"] is not None]

    return SiteHistory(
        captures=captures,
        road_name=_dig(root, 3, 2, 0, 0),
        locality_raw=_dig(root, 3, 2, 1, 0),
        copyright_string=_dig(root, 4, 0, 0, 0, 0),
        neighbors=neighbors,
    )


def split_locality(locality_raw):
    """Best-effort (city, region) from the display string.

    The response gives a localised display string, not structured fields, so
    this splits on the last comma and returns None rather than guessing when
    the shape is unfamiliar. Country is normally absent entirely and is left
    null instead of inferred.
    """
    if not locality_raw:
        return None, None
    parts = [p.strip() for p in locality_raw.split(",") if p.strip()]
    if len(parts) == 1:
        return parts[0], None
    if len(parts) >= 2:
        return ", ".join(parts[:-1]), parts[-1]
    return None, None
