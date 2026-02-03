#!/usr/bin/env python3
"""Wrapper for flickr_download that patches set_file_time for unknown dates."""

import flickr_download.utils as _u

_orig = _u.set_file_time


def _safe(f: str, t: str) -> None:
    if not t or t.startswith("0000"):
        return
    _orig(f, t)


_u.set_file_time = _safe

from flickr_download.flick_download import main

main()
