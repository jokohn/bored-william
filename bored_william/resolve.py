"""Stage 01 -- resolve a share link to a panorama and a reference heading.

Short links redirect to a full Maps URL that already carries everything needed:
position, camera, and panorama id. Long-form URLs skip the round trip.
"""

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from . import http
from .errors import LinkUnresolved, NotStreetView

# @lat,lng,3a,{fov}y,{heading}h,{tilt}t -- the "3a" marks a Street View view.
_AT_SEGMENT = re.compile(
    r"@(-?\d+\.?\d*),(-?\d+\.?\d*),([\d.]+)a,([\d.]+)y,([\d.]+)h,([\d.]+)t"
)

# Panorama id inside the protobuf-ish `data=` parameter.
_DATA_PANO = re.compile(r"!1s([A-Za-z0-9_\-]{20,})")

# Fallback: the embedded thumbnail URL carries panoid= too.
_EMBEDDED_PANOID = re.compile(r"panoid(?:%3D|=)([A-Za-z0-9_\-]{20,})")

_SHORTLINK_HOSTS = {"maps.app.goo.gl", "goo.gl", "maps.google.com"}


@dataclass
class ReferenceView:
    """What the user framed when they saved the link."""

    pano_id: str
    lat: float
    lng: float
    heading_deg: float
    tilt_deg: float
    fov_deg: float
    final_url: str

    @property
    def pitch_deg(self):
        """Imagery-endpoint pitch implied by the URL's tilt (90 = horizon)."""
        return self.tilt_deg - 90.0


def _needs_resolution(link):
    host = (urlparse(link).hostname or "").lower()
    return host in _SHORTLINK_HOSTS or "/maps/@" not in link and "pano=" not in link


def _parse(url):
    """Pull a ReferenceView out of a full Maps URL, or raise NotStreetView."""
    query = parse_qs(urlparse(url).query)

    # Documented api=1 permalink form.
    if query.get("map_action") == ["pano"] and query.get("pano"):
        heading = float(query.get("heading", ["0"])[0])
        fov = float(query.get("fov", ["45"])[0])
        # api=1 pitch is positive-up; convert to tilt where 90 is the horizon.
        pitch = float(query.get("pitch", ["0"])[0])
        return ReferenceView(
            pano_id=query["pano"][0],
            lat=float(query.get("viewpoint", ["0,0"])[0].split(",")[0]),
            lng=float(query.get("viewpoint", ["0,0"])[0].split(",")[-1]),
            heading_deg=heading,
            tilt_deg=90.0 - pitch,
            fov_deg=fov,
            final_url=url,
        )

    at = _AT_SEGMENT.search(url)
    pano = _DATA_PANO.search(url) or _EMBEDDED_PANOID.search(url)
    if not at or not pano:
        raise NotStreetView(
            "not a Street View link (no panorama in URL) -- place pins and "
            "directions links have no imagery to capture: %s" % url
        )

    lat, lng, _zoom, fov, heading, tilt = at.groups()
    return ReferenceView(
        pano_id=pano.group(1),
        lat=float(lat),
        lng=float(lng),
        heading_deg=float(heading),
        tilt_deg=float(tilt),
        fov_deg=float(fov),
        final_url=url,
    )


def resolve(link):
    """Follow redirects if needed, then parse. Raises LinkUnresolved /
    NotStreetView."""
    link = link.strip()
    if not link:
        raise LinkUnresolved("empty link")

    url = link
    if _needs_resolution(link):
        try:
            body, final_url = http.fetch(link)
        except (LinkUnresolved, NotStreetView):
            raise
        except Exception as exc:
            raise LinkUnresolved("could not follow %s: %s" % (link, exc)) from exc

        url = final_url
        # Some short links land on an interstitial rather than redirecting; the
        # real destination is then only in the page body.
        if _needs_resolution(url):
            text = body.decode("utf-8", "replace")
            found = re.search(r"https://www\.google\.com/maps/[^\"'\\\s]+", text)
            if found:
                url = found.group(0).replace("\\u0026", "&").replace("&amp;", "&")

    return _parse(url)
