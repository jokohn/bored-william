"""Error codes emitted into the manifest's `status` column.

Rows are never silently dropped: every input row produces at least one manifest
row, carrying either `ok` or one of these codes.
"""


class BoredWilliamError(Exception):
    """Base class. `code` lands in the manifest, `args[0]` in error_message."""

    code = "ERROR"


class LinkUnresolved(BoredWilliamError):
    code = "LINK_UNRESOLVED"


class NotStreetView(BoredWilliamError):
    code = "NOT_STREETVIEW"


class PhotometaFailed(BoredWilliamError):
    code = "PHOTOMETA_FAILED"


class ImageMissing(BoredWilliamError):
    code = "IMAGE_MISSING"


class RateLimited(BoredWilliamError):
    code = "RATE_LIMITED"


class ImageForbidden(BoredWilliamError):
    """Fatal. A 403 means the User-Agent was rejected, which is a
    misconfiguration of the tool rather than a problem with the data. Failing
    fast beats writing thousands of identical error rows."""

    code = "IMAGE_FORBIDDEN"
