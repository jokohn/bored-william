"""Aiming math. No network, no state.

The virtual camera is derived, never hand-entered. Yaw in particular must be
recomputed for every capture: panoramas at one location sit metres apart, and
across a single site's history that moved the required bearing by ~7 degrees --
wider than a tight field of view.
"""

import math

# Metres per degree of latitude. Good to ~0.1% over the range this tool sees,
# and the aiming tolerance is measured in degrees, so the spheroid is overkill.
M_PER_DEG_LAT = 111320.0

# The panorama is 16384 px around, so 16384 / 360 gives its native angular
# resolution. Requesting more pixels per degree upscales; requesting fewer
# discards real detail. This is the hard ceiling on billboard legibility --
# framing cannot move it, because both the panorama's resolution and the
# board's angular size are fixed.
PX_PER_DEGREE = 16384 / 360.0


def project(lat, lng, bearing_deg, distance_m):
    """Point `distance_m` from (lat, lng) along `bearing_deg`.

    Used to stand a synthetic target where the billboard is, by walking out
    along the reference panorama's own heading.
    """
    theta = math.radians(bearing_deg)
    d_lat = (distance_m * math.cos(theta)) / M_PER_DEG_LAT
    d_lng = (distance_m * math.sin(theta)) / (
        M_PER_DEG_LAT * math.cos(math.radians(lat))
    )
    return lat + d_lat, lng + d_lng


def bearing(from_lat, from_lng, to_lat, to_lng):
    """Great-circle forward azimuth, degrees clockwise from north."""
    p1 = math.radians(from_lat)
    p2 = math.radians(to_lat)
    d_lng = math.radians(to_lng - from_lng)
    y = math.sin(d_lng) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(d_lng)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def distance_m(from_lat, from_lng, to_lat, to_lng):
    """Haversine distance in metres."""
    r = 6371008.8
    p1, p2 = math.radians(from_lat), math.radians(to_lat)
    d_p = p2 - p1
    d_l = math.radians(to_lng - from_lng)
    a = math.sin(d_p / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(d_l / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def pitch_for(height_m, distance):
    """Pitch that centres a target `height_m` above the camera.

    NEGATIVE IS UP. The imagery endpoint and the Maps URL format disagree on
    this sign; see maps_url_pitch().
    """
    if distance <= 0:
        return 0.0
    return -math.degrees(math.atan2(height_m, distance))


def maps_url_pitch(view_pitch_deg):
    """Convert imagery-endpoint pitch to Maps URL pitch.

    The imagery endpoint treats negative as up; the `api=1` Maps URL format
    treats positive as up. Skipping this negation aims every published
    permalink at the pavement while the images themselves look correct, which
    makes it the easiest bug here to ship unnoticed.
    """
    return -view_pitch_deg


def width_for_fov(fov_deg):
    """Image width at the panorama's native sampling rate."""
    return int(round(fov_deg * PX_PER_DEGREE))


def angular_width(board_width_m, distance):
    """How many degrees of frame a board of a given width occupies."""
    if distance <= 0:
        return 0.0
    return 2 * math.degrees(math.atan((board_width_m / 2.0) / distance))
