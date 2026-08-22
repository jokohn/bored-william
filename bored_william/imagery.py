"""Stage 04 -- render one panorama through the virtual camera."""

import urllib.error
from urllib.parse import urlencode

from . import http
from .errors import ImageMissing

ENDPOINT = "https://streetviewpixels-pa.googleapis.com/v1/thumbnail"
PERMALINK = "https://www.google.com/maps/@"

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
