"""Shared HTTP: browser User-Agent, global rate limit, backoff on 429/5xx.

The imagery endpoint rejects requests carrying a non-browser User-Agent with
403 PERMISSION_DENIED, so the UA is set explicitly rather than left to whatever
the HTTP library defaults to.
"""

import time
import urllib.error
import urllib.request

from .errors import ImageForbidden, RateLimited

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

MAX_RETRIES = 3
BACKOFF_BASE_S = 1.5

_last_request_at = 0.0
_min_interval_s = 0.25


def configure(rate_limit_ms):
    """Set the floor on spacing between all outbound requests.

    Not configurable below 100 ms: politeness is what keeps this working.
    """
    global _min_interval_s
    _min_interval_s = max(rate_limit_ms, 100) / 1000.0


def _throttle():
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < _min_interval_s:
        time.sleep(_min_interval_s - elapsed)
    _last_request_at = time.monotonic()


def _request(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.geturl()


def fetch(url, timeout=30, retries=MAX_RETRIES):
    """GET a URL. Returns (body_bytes, final_url) after redirects.

    Raises ImageForbidden on 403 (fatal), RateLimited when retries are
    exhausted on 429/5xx, and lets other HTTPErrors propagate.
    """
    attempt = 0
    while True:
        _throttle()
        try:
            return _request(url, timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                raise ImageForbidden(
                    "403 PERMISSION_DENIED - the endpoint rejected the "
                    "User-Agent. This is a tool misconfiguration, not a data "
                    "problem."
                ) from exc
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt >= retries:
                if retryable:
                    raise RateLimited(
                        "HTTP %d after %d retries" % (exc.code, retries)
                    ) from exc
                raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt >= retries:
                raise RateLimited("network error after %d retries: %s" % (retries, exc)) from exc
        attempt += 1
        time.sleep(BACKOFF_BASE_S ** attempt)
