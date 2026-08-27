"""Recover a billboard's real position from where it lands in several frames.

Every capture at a site was aimed at the same synthetic target -- a point
projected along the reference link's heading at an assumed distance. When that
assumption is wrong the board drifts off frame centre, and the drift differs
per capture because the panoramas sit metres apart. That difference is signal:
each frame gives a bearing to the true board, and bearings from different
positions intersect where the board actually is.

Pure geometry, stdlib only, so it can be tested without spending a call.
"""

import math

M_PER_DEG_LAT = 111320.0

# Below this the rays are near-parallel and the intersection is numerically
# meaningless -- a site whose panoramas are strung out along the line of sight
# gives no depth information no matter how many frames it contributes.
MIN_BEARING_SPREAD_DEG = 0.8

# A roadside board outside this band is a solution that went wrong, not a
# board. Freeway setbacks run from a few metres at the shoulder to a couple of
# hundred at the far side of an interchange.
MIN_DISTANCE_M = 5.0
MAX_DISTANCE_M = 400.0

# Boards sit above the roadway; a solved height outside this is a bad fit.
MIN_HEIGHT_M = -2.0
MAX_HEIGHT_M = 40.0


class Unsolvable(Exception):
    """Not enough usable geometry. The site keeps its default assumptions
    rather than inheriting a number nobody can defend."""


def view_angles(cx, cy, width, height, fov_deg, view_yaw_deg, view_pitch_deg):
    """True (bearing, elevation) of a point at pixel (cx, cy) in a frame.

    The rendered frame is a rectilinear projection, so pixel offset is not
    linear in angle -- it goes through the focal length. At a 45 degree field
    of view the small-angle shortcut is off by around a degree at the frame
    edge, which is the same order as the error being measured.

    `view_pitch_deg` follows the imagery endpoint's convention where negative
    is up, so the frame centre sits at elevation `-view_pitch_deg`.
    """
    focal_px = (width / 2.0) / math.tan(math.radians(fov_deg) / 2.0)
    dx = cx - width / 2.0
    dy = cy - height / 2.0

    bearing = view_yaw_deg + math.degrees(math.atan2(dx, focal_px))
    # Vertical angle is measured against the distance to the pixel in the
    # image plane, which grows with horizontal offset.
    elevation = -view_pitch_deg - math.degrees(
        math.atan2(dy, math.hypot(focal_px, dx))
    )
    return bearing % 360.0, elevation


def to_local(lat, lng, lat0, lng0):
    """Metres east/north of a reference point."""
    x = (lng - lng0) * M_PER_DEG_LAT * math.cos(math.radians(lat0))
    y = (lat - lat0) * M_PER_DEG_LAT
    return x, y


def to_latlng(x, y, lat0, lng0):
    lat = lat0 + y / M_PER_DEG_LAT
    lng = lng0 + x / (M_PER_DEG_LAT * math.cos(math.radians(lat0)))
    return lat, lng


def _bearing_spread(bearings):
    """Angular range covered by the rays, handling wraparound.

    Two frames that see the board at the same bearing constrain direction but
    not depth, however far apart the cameras are.
    """
    ref = bearings[0]
    rel = [((b - ref + 180.0) % 360.0) - 180.0 for b in bearings]
    return max(rel) - min(rel)


def intersect(rays):
    """Least-squares intersection of 2-D rays.

    `rays` is a sequence of (x, y, bearing_deg). Returns (x, y, rms_residual_m).

    Minimises the summed squared perpendicular distance from the solution to
    each ray, which is the standard closed form: with A_i = I - d_i d_i^T for
    unit direction d_i, the solution satisfies (sum A_i) x = sum A_i p_i.
    """
    if len(rays) < 2:
        raise Unsolvable("need at least two rays, got %d" % len(rays))

    spread = _bearing_spread([b for _, _, b in rays])
    if spread < MIN_BEARING_SPREAD_DEG:
        raise Unsolvable(
            "bearing spread %.2f deg is below the %.1f deg floor -- the "
            "cameras give no depth" % (spread, MIN_BEARING_SPREAD_DEG)
        )

    m00 = m01 = m11 = b0 = b1 = 0.0
    for px, py, bearing in rays:
        theta = math.radians(bearing)
        dx, dy = math.sin(theta), math.cos(theta)   # bearing 0 = north = +y
        a00, a01, a11 = 1.0 - dx * dx, -dx * dy, 1.0 - dy * dy
        m00 += a00
        m01 += a01
        m11 += a11
        b0 += a00 * px + a01 * py
        b1 += a01 * px + a11 * py

    det = m00 * m11 - m01 * m01
    if abs(det) < 1e-9:
        raise Unsolvable("degenerate ray geometry")

    x = (m11 * b0 - m01 * b1) / det
    y = (m00 * b1 - m01 * b0) / det

    # RMS perpendicular distance from the solution to each ray.
    total = 0.0
    for px, py, bearing in rays:
        theta = math.radians(bearing)
        dx, dy = math.sin(theta), math.cos(theta)
        vx, vy = x - px, y - py
        along = vx * dx + vy * dy
        total += (vx - along * dx) ** 2 + (vy - along * dy) ** 2
    return x, y, math.sqrt(total / len(rays))


def solve(observations):
    """Recover a board's position, distance and height from its sightings.

    Each observation is a dict with `lat`, `lng`, `bearing`, `elevation`.
    Returns distance and height suitable for the fetch stage's per-row
    columns, plus the diagnostics needed to decide whether to trust them.
    """
    if len(observations) < 2:
        raise Unsolvable("need at least two sightings, got %d" % len(observations))

    lat0 = sum(o["lat"] for o in observations) / len(observations)
    lng0 = sum(o["lng"] for o in observations) / len(observations)

    rays = []
    for o in observations:
        x, y = to_local(o["lat"], o["lng"], lat0, lng0)
        rays.append((x, y, o["bearing"]))

    bx, by, residual = intersect(rays)
    board_lat, board_lng = to_latlng(bx, by, lat0, lng0)

    distances, heights = [], []
    for o, (px, py, _) in zip(observations, rays):
        d = math.hypot(bx - px, by - py)
        if d <= 0:
            continue
        distances.append(d)
        heights.append(d * math.tan(math.radians(o["elevation"])))

    if not distances:
        raise Unsolvable("solution coincides with the camera positions")

    distance = sum(distances) / len(distances)
    # Median height: a single badly-bounded frame skews a mean, and the
    # vertical angle is the noisier of the two measurements because boards are
    # much wider than they are tall.
    heights.sort()
    mid = len(heights) // 2
    height = (heights[mid] if len(heights) % 2
              else (heights[mid - 1] + heights[mid]) / 2.0)

    if not (MIN_DISTANCE_M <= distance <= MAX_DISTANCE_M):
        raise Unsolvable("solved distance %.1f m is outside the plausible band"
                         % distance)
    if not (MIN_HEIGHT_M <= height <= MAX_HEIGHT_M):
        raise Unsolvable("solved height %.1f m is outside the plausible band"
                         % height)

    return {
        "board_lat": round(board_lat, 7),
        "board_lng": round(board_lng, 7),
        "distance_m": round(distance, 1),
        "height_m": round(height, 1),
        "rays": len(rays),
        "bearing_spread_deg": round(_bearing_spread([r[2] for r in rays]), 2),
        "residual_m": round(residual, 2),
    }
