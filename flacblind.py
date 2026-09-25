#!/usr/bin/env python3
"""flacblind — blind ABX test for lossless vs. lossy audio (GUI).

This module is the entry point that keeps the original ``python3 flacblind.py
--mp3path … --flacpath …`` invocation working; it simply launches the PyQt6
window of the refactored application.  See README.md for the GUI usage.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flacblind.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
