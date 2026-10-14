"""Return types of st3215_bus.Buses, checked against a fake servo on a pty.

Standard library only; no hardware. A pseudo-terminal pair stands in for the
serial adapter: the extension opens the slave side as its port, a thread on the
master side answers ST3215 ping requests for a fixed set of motor ids.

Needs the compiled extension, so it is skipped when only the sources are
available (run it against an installed wheel, as docker/build.sh does).
"""

import os
import select
import threading
import unittest

try:
    from st3215_bus import Buses
except ImportError:  # no wheel installed: nothing to test
    Buses = None

ANSWERING = (1, 3)


def _checksum(body):
    return (~sum(body)) & 0xFF


class _FakeServos(threading.Thread):
    """Answers a ping for each id in ANSWERING; ignores every other request."""

    def __init__(self, master):
        super().__init__(daemon=True)
        self.master = master
        self.stop = threading.Event()

    def run(self):
        buf = b""
        while not self.stop.is_set():
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if not ready:
                continue
            try:
                buf += os.read(self.master, 256)
            except OSError:
                return
            # Request: FF FF id len instruction ... checksum
            while len(buf) >= 4:
                if buf[:2] != b"\xff\xff":
                    buf = buf[1:]
                    continue
                size = 4 + buf[3]
                if len(buf) < size:
                    break
                motor, instruction = buf[2], buf[4]
                buf = buf[size:]
                if instruction == 0x01 and motor in ANSWERING:
                    body = bytes([motor, 2, 0])  # id, length, no error
                    os.write(self.master, b"\xff\xff" + body + bytes([_checksum(body)]))


@unittest.skipIf(Buses is None, "compiled st3215_bus extension not installed")
class BusesReturnTypes(unittest.TestCase):
    def setUp(self):
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        self.servos = _FakeServos(master)
        self.servos.start()
        self.addCleanup(self.servos.join)
        self.addCleanup(self.servos.stop.set)
        try:
            self.buses = Buses({"bus": os.ttyname(slave)}, 30)
        except OSError as e:  # a pty that refuses serial setup in this sandbox
            self.skipTest(f"cannot open a pty as a serial port: {e}")
        self.addCleanup(self.buses.close)

    def test_ping_returns_lists_of_int(self):
        result = self.buses.ping(4)
        self.assertEqual(result, {"bus": list(ANSWERING)})
        ids = result["bus"]
        self.assertIs(type(ids), list)  # PyO3 turns Vec<u8> into bytes
        for motor in ids:
            self.assertIs(type(motor), int)

    def test_write_eeprom_unverified_ids_are_lists_of_int(self):
        # The fake servos never acknowledge a write, so no value is verified.
        result = self.buses.write_eeprom("ID", {"bus": {5: 5, 2: 2}})
        self.assertEqual(result, {"bus": [2, 5]})
        ids = result["bus"]
        self.assertIs(type(ids), list)
        for motor in ids:
            self.assertIs(type(motor), int)


if __name__ == "__main__":
    unittest.main()
