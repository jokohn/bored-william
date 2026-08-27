"""Local, deterministic cropping.

The model returns coordinates; the crop happens here. That keeps the operation
reproducible from a recorded number rather than hidden inside a model call,
and it is the only way to do it at all -- vision models emit text, not images.

Cropping adds no resolution. Pixels-on-billboard is fixed by the panorama's
angular resolution and the board's angular size, and neither changes when the
frame is tightened. What it buys is attention: the second billboard, the gas
station, and the traffic stop competing for the model's budget, and the
which-board-is-the-subject ambiguity disappears structurally instead of being
carried by a text hint.
"""

from PIL import Image

# Padding around the returned box, as a fraction of its size. The gate is asked
# for the face rather than the structure, and a face clipped at the edge loses
# exactly the text nearest the frame boundary.
PAD = 0.06

# A box smaller than this is not a billboard, it is a mistake -- and a bad box
# crops to a tree that the extraction pass will then describe with confidence.
#
# The floors come from the capture geometry rather than taste. A 48 ft bulletin
# at a 200 m setback subtends about 4.2 degrees horizontally and 1.25 degrees
# vertically; at the panorama's 45.5 px/degree that is roughly 190 x 57 px, and
# anything appreciably smaller is either much farther away than a roadside
# board can be or is not a board at all.
MIN_WIDTH_PX = 120
MIN_HEIGHT_PX = 40

# A box covering nearly the whole frame means the gate did not localise
# anything -- at a 45 degree field of view no roadside board fills the frame.
MAX_AREA_FRACTION = 0.9

# Freeway boards are wide. A tall, narrow box usually means a pole or a sign
# post was bounded instead of the face.
MAX_ASPECT = 12.0


class InvalidCrop(Exception):
    """The bounding box cannot be trusted. Better to fail the row than to
    hand a picture of foliage to extraction and get a confident description
    of nothing."""


def validate(bbox, image_w, image_h):
    """Return a padded, clamped (left, top, right, bottom) or raise."""
    x, y, w, h = bbox.x, bbox.y, bbox.w, bbox.h
    if w <= 0 or h <= 0:
        raise InvalidCrop("non-positive box %dx%d" % (w, h))
    if w < MIN_WIDTH_PX or h < MIN_HEIGHT_PX:
        raise InvalidCrop(
            "box too small to be a readable board: %dx%d px (floor %dx%d)"
            % (w, h, MIN_WIDTH_PX, MIN_HEIGHT_PX)
        )
    if (w * h) > (image_w * image_h * MAX_AREA_FRACTION):
        raise InvalidCrop(
            "box covers %.0f%% of the frame -- the gate did not localise"
            % (100.0 * w * h / (image_w * image_h))
        )
    aspect = max(w / h, h / w)
    if aspect > MAX_ASPECT:
        raise InvalidCrop("implausible aspect ratio %.1f:1" % aspect)

    pad_x, pad_y = int(w * PAD), int(h * PAD)
    left = max(0, x - pad_x)
    top = max(0, y - pad_y)
    right = min(image_w, x + w + pad_x)
    bottom = min(image_h, y + h + pad_y)
    if right <= left or bottom <= top:
        raise InvalidCrop("box lies outside the image")
    # A box the gate placed almost entirely off-frame clamps down to a sliver.
    if (right - left) < MIN_WIDTH_PX or (bottom - top) < MIN_HEIGHT_PX:
        raise InvalidCrop("box clamps to a sliver inside the image")
    return left, top, right, bottom


def dimensions(path):
    with Image.open(path) as im:
        return im.size


def crop_to(src_path, bbox, dest_path):
    """Crop `src_path` to `bbox` and write it. Returns the box actually used."""
    with Image.open(src_path) as im:
        box = validate(bbox, im.width, im.height)
        im.crop(box).save(dest_path, "JPEG", quality=92)
    return box
