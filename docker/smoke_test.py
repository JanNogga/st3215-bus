"""Smoke test for an installed st3215-bus wheel. Touches no hardware.

Usage: python -I smoke_test.py [TESTS_DIR]

Checks the extension imports and behaves, that the calibration exporter's
console script and ``python -m`` entry point start, and, given TESTS_DIR, runs
the unit tests there against the installed package.
"""

import os
import subprocess
import sys
import unittest

import st3215_bus
from st3215_bus import export_calibration

assert st3215_bus.COUNTS_PER_TURN == 4096, st3215_bus.COUNTS_PER_TURN
assert st3215_bus.Buses.__module__ == "st3215_bus", st3215_bus.Buses.__module__
try:
    st3215_bus.Buses({"x": "/dev/does-not-exist"})
except OSError:
    pass
else:
    sys.exit("FAIL: Buses() on a missing port did not raise OSError")

# The exporter must come from the installed wheel, next to the extension.
assert os.path.dirname(export_calibration.__file__) == os.path.dirname(st3215_bus.__file__)
script = os.path.join(os.path.dirname(sys.executable), "st3215-export-calibration")
for cmd in ([script, "--help"],
            [sys.executable, "-I", "-m", "st3215_bus.export_calibration", "--help"]):
    out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         universal_newlines=True)
    if out.returncode != 0 or "--station" not in out.stdout:
        sys.exit(f"FAIL: {' '.join(cmd)} exited {out.returncode}:\n{out.stdout}")

if len(sys.argv) > 1:
    suite = unittest.defaultTestLoader.discover(sys.argv[1], top_level_dir=sys.argv[1])
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=0).run(suite)
    if not result.wasSuccessful():
        sys.exit("FAIL: unit tests")

print(f"ok: Python {sys.version.split()[0]}, {st3215_bus.__file__}")
