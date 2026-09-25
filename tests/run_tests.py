#!/usr/bin/env python3
"""Run the whole test suite without any third-party dependency.

    python3 run_tests.py            # everything
    python3 run_tests.py -v         # verbose
    python3 run_tests.py stats      # only modules matching "stats"

Uses :mod:`unittest` from the standard library and Qt's ``offscreen`` platform,
so it runs on a headless machine.  Tests that need ffmpeg skip themselves when
it is missing.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def main(argv: list[str]) -> int:
    verbosity = 2 if "-v" in argv or "--verbose" in argv else 1
    patterns = [arg for arg in argv if not arg.startswith("-")]

    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(HERE), pattern="test_*.py", top_level_dir=str(HERE))
    if patterns:
        selected = unittest.TestSuite()

        def _matches(test: unittest.Test) -> bool:  # noqa: ANN001
            name = test.id()
            return any(pattern in name for pattern in patterns)

        def _filter(item):  # noqa: ANN001, ANN202
            if isinstance(item, unittest.TestSuite):
                filtered = unittest.TestSuite()
                for child in item:
                    result = _filter(child)
                    if isinstance(result, unittest.TestSuite) and not result.countTestCases():
                        continue
                    filtered.addTest(result)
                return filtered
            return item if _matches(item) else unittest.TestSuite()

        suite = _filter(suite)  # type: ignore[assignment]

    result = unittest.TextTestRunner(verbosity=verbosity, buffer=False).run(suite)
    print()
    print(f"tests: {result.testsRun}  failures: {len(result.failures)}  errors: {len(result.errors)}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
