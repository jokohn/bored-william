"""Stage 04 -- render one panorama through the virtual camera."""

import urllib.error
from urllib.parse import urlencode

from . import http
from .errors import ImageMissing

ENDPOINT = "https://streetviewpixels-pa.googleapis.com/v1/thumbnail"
PERMALINK = "https://www.google.com/maps/@"

# The endpoint silently caps rendered output at 1024 px wide: w=1600, w=2048 and
# w=3000 all return HTTP 200 with a 1024x682 image. Requesting more than this
# does not fail, it just quietly downscales -- which means the recorded width
# has to come from the delivered bytes, not from the request, and that a field
# of view above ~22 degrees samples below the panorama's native resolution.
MAX_DELIVERED_WIDTH = 1024

# The api=1 Maps URL format accepts 10-100 degrees and silently misbehaves
# outside it, so permalinks are clamped even when the capture used a wider fov.
_PERMALINK_FOV = (10.0, 100.0)


def image_url(pano_id, yaw_deg, pitch_deg, width, height, fov_deg):
    """Imagery request. `pitch_deg` follows this endpoint: negative is up."""
    return "%s?%s" % (
        ENDPOINT,
        urlencode(
            {
                "cb_client": "maps_sv.tactile",
                "panoid": pano_id,
                "yaw": round(yaw_deg, 2),
                "pitch": round(pitch_deg, 2),
                "w": width,
                "h": height,
                # thumbfov must be an integer -- "45.0" and "45.5" both return
                # 400. yaw and pitch accept decimals, which makes the
                # inconsistency easy to miss.
                "thumbfov": int(round(fov_deg)),
            }
        ),
    )


def permalink(pano_id, yaw_deg, maps_pitch_deg, fov_deg):
    """Documented api=1 permalink.

    `maps_pitch_deg` must already be converted via geo.maps_url_pitch --
    this format treats positive as up, the imagery endpoint treats negative
    as up, and nothing downstream will catch the confusion.
    """
    fov = min(max(fov_deg, _PERMALINK_FOV[0]), _PERMALINK_FOV[1])
    return "%s?%s" % (
        PERMALINK,
        urlencode(
            {
                "api": 1,
                "map_action": "pano",
                "pano": pano_id,
                "heading": round(yaw_deg, 2),
                "pitch": round(maps_pitch_deg, 2),
                "fov": round(fov, 2),
            }
        ),
    )


def jpeg_dimensions(blob):
    """(width, height) read from a JPEG's own header.

    The recorded dimensions must describe the image on disk, not the one that
    was asked for. Parsing the SOF marker keeps the fetch stage stdlib-only
    rather than pulling in an imaging library for two integers.
    """
    i, n = 2, len(blob)
    while i + 9 < n:
        if blob[i] != 0xFF:
            i += 1
            continue
        marker = blob[i + 1]
        # SOF0-SOF15, excluding the non-frame markers DHT/JPG/DAC at C4/C8/CC
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(blob[i + 5:i + 7], "big")
            width = int.from_bytes(blob[i + 7:i + 9], "big")
            return width, height
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        i += 2 + int.from_bytes(blob[i + 2:i + 4], "big")
    return None, None


def fetch_image(url):
    """Return JPEG bytes. Raises ImageMissing if the panorama is gone."""
    try:
        body, _ = http.fetch(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ImageMissing(
                "panorama unavailable (404) - it was listed during "
                "enumeration but has since been retired"
            ) from exc
        # Any other HTTP status becomes a row-level error rather than killing
        # the run. A systematic fault shows up as every row failing with the
        # same code, which the run summary reports.
        raise ImageMissing("HTTP %d fetching imagery" % exc.code) from exc
    if not body:
        raise ImageMissing("empty image response")
    return body
