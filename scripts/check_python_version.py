#!/usr/bin/env python3
"""Fail unless the invoking interpreter is exactly Python 3.12.8."""

from __future__ import annotations

import platform
import sys


EXPECTED_VERSION = (3, 12, 8)


def main() -> int:
    """Check the interpreter running this process and return a shell status."""
    version = sys.version_info
    if version[:3] != EXPECTED_VERSION or version.releaselevel != "final":
        actual = platform.python_version()
        print(
            "Python 3.12.8 is required; "
            f"found Python {actual} at {sys.executable}",
            file=sys.stderr,
        )
        return 1

    print(f"Python 3.12.8 preflight passed ({sys.executable})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
