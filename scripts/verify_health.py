#!/usr/bin/env python3
"""Verify that the local OmniImmune target health endpoint is ready."""

from __future__ import annotations

import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


HEALTH_URL = "http://127.0.0.1:8000/health"
TIMEOUT_SECONDS = 5


def verify_health(url: str = HEALTH_URL, timeout: float = TIMEOUT_SECONDS) -> int:
    """Return zero only when the health endpoint responds with HTTP 200."""
    request = Request(url, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            status = response.status
    except HTTPError as exc:
        print(f"Health check failed: {url} returned HTTP {exc.code}", file=sys.stderr)
        return 1
    except (URLError, TimeoutError, OSError) as exc:
        print(f"Health check failed: unable to reach {url}: {exc}", file=sys.stderr)
        return 1

    if status != 200:
        print(f"Health check failed: {url} returned HTTP {status}", file=sys.stderr)
        return 1

    print(f"Health check passed: {url} returned 200 OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(verify_health())
