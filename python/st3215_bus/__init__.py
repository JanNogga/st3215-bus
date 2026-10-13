"""Python bindings for Feetech ST3215 servo buses, built in Rust on NormaCore's st3215 protocol.

The API lives in the compiled extension ``st3215_bus.st3215_bus`` and is
re-exported here, so ``st3215_bus.Buses`` and ``st3215_bus.COUNTS_PER_TURN``
are the names to use. ``st3215_bus.export_calibration`` is a separate,
pure-Python command-line tool (``st3215-export-calibration``).
"""

from .st3215_bus import COUNTS_PER_TURN, Buses

__all__ = ["Buses", "COUNTS_PER_TURN"]
