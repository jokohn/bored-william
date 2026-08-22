"""Filesystem-safe names for site directories and image files.

Windows is the strict case and the one the author runs on, so its rules drive
the implementation: reserved device names, and the silent prohibition on
trailing dots and spaces in directory names.
"""

import re
from urllib.parse import urlparse

ILLEGAL = r'<>:"/\\|?*'

# Creating a directory named any of these fails on Windows with an error that
# gives no hint about why. A site labelled "aux" is not far-fetched.
RESERVED = {"CON", "PRN", "AUX", "NUL"}
RESERVED |= {"COM%d" % i for i in range(10)}
RESERVED |= {"LPT%d" % i for i in range(10)}

MAX_LEN = 64


def slug(text, max_len=MAX_LEN):
    """Lowercase, filesystem-safe slug. Returns "" if nothing survives."""
    if not text:
        return ""
    out = text.lower()
    out = re.sub(r"[%s]" % re.escape(ILLEGAL), "-", out)
    out = re.sub(r"[\x00-\x1f\x7f]", "", out)
    out = re.sub(r"\s+", "-", out)
    out = re.sub(r"-{2,}", "-", out)
    out = out.strip("-. ")
    out = out[:max_len].strip("-. ")
    if out.upper() in RESERVED:
        out = "_" + out
    return out


def slug_from_link(link):
    """Slug a URL by its distinctive tail, dropping scheme and host.

    A short link reduces to its code; a long Maps URL to its path, which is
    unwieldy but stable and unique.
    """
    if not link:
        return ""
    try:
        parsed = urlparse(link if "//" in link else "//" + link)
        tail = (parsed.path or "").strip("/")
        if parsed.query:
            tail = tail + "-" + parsed.query
    except ValueError:
        tail = link
    return slug(tail or link)


def site_dir_name(site_label, link, fallback_pano_id):
    """Directory name for a site under --group-by-site.

    site_label wins; otherwise the link; otherwise the reference pano id,
    which is always present by the time images are written.
    """
    return (
        slug(site_label)
        or slug_from_link(link)
        or slug(fallback_pano_id)
        or "unnamed-site"
    )


def image_filename(site_slug, capture_date, pano_id):
    """`{slug}_{date}_{pano8}.jpg` -- collision-proof, still readable.

    The full form is kept even when grouping into per-site directories, so a
    file stays identifiable after being copied out of its folder.
    """
    return "%s_%s_%s.jpg" % (site_slug or "site", capture_date, pano_id[:8])


class NameAllocator:
    """Hands out unique directory names.

    Two input rows may legitimately slug to the same string -- two boards both
    labelled "shell plaza" -- so distinct links get -2, -3 suffixes rather
    than silently sharing a directory.
    """

    def __init__(self):
        self._taken = {}

    def allocate(self, base, key):
        if base in self._taken:
            if self._taken[base] == key:
                return base
            n = 2
            while "%s-%d" % (base, n) in self._taken:
                if self._taken["%s-%d" % (base, n)] == key:
                    return "%s-%d" % (base, n)
                n += 1
            base = "%s-%d" % (base, n)
        self._taken[base] = key
        return base
