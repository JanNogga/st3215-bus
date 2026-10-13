"""Tests for st3215_bus.export_calibration. Standard library only; no hardware.

Run from the repository root with ``python -m unittest discover -s tests``.
They test the installed st3215_bus package if there is one, else the module in
python/st3215_bus/ directly (it uses nothing from the compiled extension).
"""

import contextlib
import importlib.util
import io
import json
import os
import socket
import socketserver
import struct
import tempfile
import threading
import unittest

try:
    from st3215_bus import export_calibration as ec
except ImportError:  # no wheel installed: test the sources
    _path = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir,
                         "python", "st3215_bus", "export_calibration.py")
    _spec = importlib.util.spec_from_file_location("export_calibration", _path)
    ec = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(ec)


# Independent of the module's encoder, so fixtures do not just mirror it.
def varint(n):
    out = b""
    while True:
        b, n = n & 0x7F, n >> 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def f_varint(num, value):
    return varint(num << 3) + varint(value) if value else b""  # proto3: omit zero


def f_bytes(num, value):
    if isinstance(value, str):
        value = value.encode()
    return varint(num << 3 | 2) + varint(len(value)) + value if value else b""


def motor(mid, lo, hi, frozen=True, extra=b""):
    return (f_varint(1, mid) + f_bytes(5, b"\x01\x02\x03") + f_varint(10, lo)
            + f_varint(11, hi) + f_varint(14, int(frozen)) + extra)


def bus(serial, port, stamp_ns, motors):
    st3215_bus = f_bytes(1, port) + f_varint(2, 0x1A86) + f_varint(3, 0x55D3) + f_bytes(4, serial)
    return (f_bytes(1, st3215_bus) + f_varint(2, 123) + f_varint(3, stamp_ns)
            + f_varint(4, 9) + b"".join(f_bytes(10, m) for m in motors))


def inference_state(buses):
    return f_bytes(1, b"\x00" * 8) + b"".join(f_bytes(10, b) for b in buses)


NOW = 1_760_000_000_000_000_000  # station wall clock in the fake, ns
GOOD = [motor(1, 1041, 3055), motor(2, 0, 4095), motor(3, 909, 3187)]


class DecoderTest(unittest.TestCase):
    def test_handcrafted_message(self):
        # field 1 varint 150, field 2 string "hi", field 3 fixed64, field 4 fixed32
        buf = bytes.fromhex("089601" "12026869" "19" "0100000000000000" "25" "02000000")
        self.assertEqual(ec.decode(buf), {1: [150], 2: [b"hi"], 3: [1], 4: [2]})

    def test_repeated_and_empty(self):
        self.assertEqual(ec.decode(b""), {})
        self.assertEqual(ec.decode(bytes.fromhex("0801" "0802")), {1: [1, 2]})

    def test_absent_fields_take_defaults(self):
        # A motor with only its id: proto3 dropped range_min, range_max and
        # range_freezed because they are 0 / false.
        state = inference_state([bus("S", "/dev/x", 5, [bytes.fromhex("0807")])])
        (b,) = ec.parse_inference_state(state)
        self.assertEqual(b["motors"], [{"id": 7, "range_min": 0, "range_max": 0, "frozen": False}])
        # A bus with nothing set at all.
        (b,) = ec.parse_inference_state(bytes.fromhex("5200"))
        self.assertEqual(b, {"serial": "", "port_name": "", "system_stamp_ns": 0, "motors": []})

    def test_unknown_fields_skipped(self):
        unknown = (f_varint(99, 5) + bytes.fromhex("fd0f" "11223344")  # field 255 fixed32
                   + bytes.fromhex("f907" "0102030405060708")          # field 127 fixed64
                   + f_bytes(500, b"whatever"))
        state = inference_state([bus("S1", "/dev/a", 5, [motor(2, 10, 20, extra=unknown)])
                                 + unknown]) + unknown
        (b,) = ec.parse_inference_state(state)
        self.assertEqual(b["serial"], "S1")
        self.assertEqual(b["motors"], [{"id": 2, "range_min": 10, "range_max": 20, "frozen": True}])

    def test_rejects_groups_and_garbage(self):
        for bad in ("0b", "0c", "0e", "0f",       # groups (3, 4) and wire types 6, 7
                    "08", "0880",                 # truncated varints
                    "1205ab", "19000000",          # truncated bytes / fixed64
                    "00",                          # field number 0
                    "08ffffffffffffffffffff01"):   # varint longer than 64 bits
            with self.subTest(bad=bad), self.assertRaises(ec.StationError) as cm:
                ec.decode(bytes.fromhex(bad))
            self.assertIn("NormaCore protocol changed?", str(cm.exception))

    def test_wrong_wire_type_for_known_field(self):
        # serial_number (field 4 of ST3215Bus) sent as a varint
        with self.assertRaises(ec.StationError):
            ec.parse_inference_state(f_bytes(10, f_bytes(1, bytes.fromhex("2001"))))


class BusProblemsTest(unittest.TestCase):
    def problems(self, *motors):
        b = {"serial": "S1", "port_name": "/dev/a", "system_stamp_ns": NOW,
             "motors": [{"id": i, "range_min": lo, "range_max": hi, "frozen": True}
                        for i, lo, hi in motors]}
        return ec.bus_problems(b, NOW, 5.0)

    def test_valid_ranges(self):
        self.assertEqual(self.problems((1, 1041, 3055), (2, 0, 4095), (3, 5, 6)), [])

    def test_invalid_ranges(self):
        for lo, hi in ((0, 0), (3000, 1000), (2048, 2048), (4095, 0)):
            with self.subTest(range=(lo, hi)):
                self.assertEqual(self.problems((1, 100, 200), (2, lo, hi)), [
                    "motor(s) 2 have an invalid saved range (range_min >= range_max): "
                    "redo and save the calibration in NormaCore's UI"])


class EncodingTest(unittest.TestCase):
    def test_handshake(self):
        self.assertEqual(ec.setup_request(), bytes.fromhex("0a020801"))

    def test_read_request(self):
        # Derived by hand from normfs.proto:
        #   ClientRequest.read (11, LEN) = 5a 28
        #     ReadRequest.read_id (1) = 08 07
        #     ReadRequest.queue_id (2) = 12 10 "st3215/inference"
        #     ReadRequest.step (3) = 18 01
        #     ReadRequest.offset (10, LEN) = 52 0e
        #       Offset.id (1, LEN) = 0a 0a
        #         Id.raw (1, LEN) = 0a 08 0100000000000000   (u64 LE 1)
        #       Offset.type (2) = 10 01                      (OT_SHIFT_FROM_TAIL)
        #     ReadRequest.limit (11) = 58 01
        expected = (bytes.fromhex("5a28" "0807" "1210") + b"st3215/inference"
                    + bytes.fromhex("1801" "520e" "0a0a" "0a080100000000000000" "1001" "5801"))
        self.assertEqual(ec.read_last_request(7), expected)

    def test_ping_request(self):
        self.assertEqual(ec.ping_request(1, 300), bytes.fromhex("1205" "0801" "10ac02"))

    def test_no_write_request(self):
        # Every request this module can build is setup (1), ping (2) or read (11).
        for req in (ec.setup_request(), ec.ping_request(1, 2), ec.read_last_request(1)):
            self.assertLessEqual(set(ec.decode(req)), {1, 2, 11})

    def test_parse_address(self):
        self.assertEqual(ec.parse_address("127.0.0.1:8888"), ("127.0.0.1", 8888))
        self.assertEqual(ec.parse_address("station.local"), ("station.local", 8888))
        self.assertEqual(ec.parse_address("[::1]:9000"), ("::1", 9000))
        for bad in ("", ":8888", "host:0", "host:x", "[::1"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ec.parse_address(bad)


class FakeStation(socketserver.ThreadingTCPServer):
    """A normfs TCP endpoint replaying handcrafted ServerResponse frames."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, state=None, version=1, read_result=None):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.state = state                # InferenceState bytes for RR_ENTRY
        self.version = version
        self.read_result = read_result    # e.g. RR_QUEUE_NOT_FOUND instead of an entry
        self.seen = []                    # top-level field numbers of each request
        self.address = "127.0.0.1:%d" % self.server_address[1]
        threading.Thread(target=self.serve_forever, args=(0.05,), daemon=True).start()

    def stop(self):
        self.shutdown()
        self.server_close()


class _Handler(socketserver.BaseRequestHandler):
    def recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.request.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def send(self, payload):
        self.request.sendall(struct.pack("<Q", len(payload)) + payload)

    def handle(self):
        srv = self.server
        while True:
            head = self.recv_exact(8)
            if head is None:
                return
            req = ec.decode(self.recv_exact(struct.unpack("<Q", head)[0]))
            srv.seen.append(sorted(req))
            if 1 in req:
                self.send(f_bytes(1, f_varint(1, srv.version) + f_bytes(2, b"\x01" * 16)
                                  + f_bytes(3, "fake-instance")))
            elif 2 in req:
                self.request.sendall(struct.pack("<Q", 0))   # empty frame: ignored
                self.send(f_bytes(11, f_varint(1, 999) + f_varint(2, 2)))  # someone else's read
                self.send(f_bytes(2, f_varint(2, NOW) + f_varint(3, 42) + f_bytes(4, req[2][0])))
            elif 11 in req:
                read_id = ec.decode(req[11][0])[1][0]
                self.send(f_bytes(11, f_varint(1, read_id)))  # RR_START: result absent
                if srv.read_result is not None:
                    self.send(f_bytes(11, f_varint(1, read_id) + f_varint(2, srv.read_result)))
                    continue
                if srv.state is not None:
                    self.send(f_bytes(11, f_varint(1, read_id) + f_varint(2, 1)
                                      + f_bytes(10, f_bytes(1, b"\x05" + b"\x00" * 7))
                                      + f_bytes(11, srv.state) + f_varint(12, 4)))
                self.send(f_bytes(11, f_varint(1, read_id) + f_varint(2, 2)))  # RR_END


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "calib")
        self.station = None

    def tearDown(self):
        if self.station:
            self.station.stop()
            # Read-only: nothing but setup, ping and read was ever sent.
            for fields in self.station.seen:
                self.assertIn(fields, ([1], [2], [11]))
        self.tmp.cleanup()

    def start(self, **kw):
        self.station = FakeStation(**kw)
        return self.station.address

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ec.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def fresh_state(self):
        return inference_state([
            bus("5A68001234", "/dev/ttyACM0", NOW - 100_000_000, GOOD),
            bus("5A68005678", "/dev/ttyACM2", NOW - 200_000_000, [motor(8, 2000, 3000), motor(1, 5, 6)]),
        ])

    def test_success(self):
        addr = self.start(state=self.fresh_state())
        code, out, err = self.run_main("--station", addr, "--out", self.out,
                                       "--name", "5A68005678=left_follower")
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(os.listdir(self.out)), ["5A68001234.json", "left_follower.json"])
        with open(os.path.join(self.out, "5A68001234.json")) as f:
            doc = json.load(f)
        self.assertEqual(doc["format"], "st3215-bus-calibration")
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["serial"], "5A68001234")
        self.assertEqual(doc["port_name"], "/dev/ttyACM0")
        self.assertEqual(doc["station"], {"address": addr, "instance_id": "fake-instance"})
        self.assertTrue(doc["exported_at"].endswith("+00:00"))
        self.assertEqual(list(doc["motors"]), ["1", "2", "3"])
        self.assertEqual(doc["motors"]["1"], {"range_min": 1041, "range_max": 3055})
        self.assertEqual(doc["motors"]["2"], {"range_min": 0, "range_max": 4095})
        left = ec.load_calibration(os.path.join(self.out, "left_follower.json"))
        self.assertEqual(list(left["motors"]), [1, 8])  # sorted by id, int keys
        self.assertEqual(left["motors"][8], {"range_min": 2000, "range_max": 3000})
        self.assertIn("exported 2 bus(es)", out)

        # Existing files are not overwritten without --force.
        code, _, err = self.run_main("--station", addr, "--out", self.out)
        self.assertEqual(code, 1)
        self.assertIn("exists (use --force", err)
        code, _, err = self.run_main("--station", addr, "--out", self.out, "--force")
        self.assertEqual(code, 0, err)

    def test_list_writes_nothing(self):
        addr = self.start(state=inference_state([
            bus("5A68001234", "/dev/ttyACM0", NOW - 100_000_000, GOOD + [motor(4, 0, 0, frozen=False)])]))
        code, out, err = self.run_main("--station", addr, "--out", self.out, "--list")
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(self.out))
        self.assertIn("5A68001234", out)
        self.assertIn("/dev/ttyACM0", out)
        self.assertIn("1041 .. 3055", out)
        self.assertIn("NOT SAVED", out)

    def test_stale(self):
        addr = self.start(state=inference_state([bus("S1", "/dev/a", NOW - 3600 * 10**9, GOOD)]))
        code, _, err = self.run_main("--station", addr, "--out", self.out)
        self.assertEqual(code, 1)
        self.assertIn("3600 s old", err)
        self.assertIn("is the arm powered and connected", err)
        self.assertFalse(os.path.exists(self.out))
        code, _, err = self.run_main("--station", addr, "--out", self.out, "--max-age", "7200")
        self.assertEqual(code, 0, err)

    def test_unfrozen_or_invalid_range(self):
        # 3: empty (0..0), 4: wrapped (3000..1000), 6: zero width. 5 and 7 are
        # not saved, so they are reported as that, not as invalid.
        addr = self.start(state=inference_state([bus("S1", "/dev/a", NOW, [
            motor(1, 100, 200), motor(2, 100, 200, frozen=False), motor(3, 0, 0),
            motor(4, 3000, 1000), motor(5, 0, 0, frozen=False), motor(6, 500, 500),
            motor(7, 3000, 1000, frozen=False)])]))
        code, _, err = self.run_main("--station", addr, "--out", self.out)
        self.assertEqual(code, 1)
        self.assertIn("motor(s) 2, 5, 7 have no saved range", err)
        self.assertIn("motor(s) 3, 4, 6 have an invalid saved range (range_min >= range_max)", err)
        self.assertIn("NormaCore's UI", err)
        self.assertFalse(os.path.exists(self.out))

    def test_one_bad_bus_blocks_all(self):
        addr = self.start(state=inference_state([
            bus("S1", "/dev/a", NOW, GOOD), bus("S2", "/dev/b", NOW, [])]))
        code, _, err = self.run_main("--station", addr, "--out", self.out)
        self.assertEqual(code, 1)
        self.assertIn("bus S2 (/dev/b): no motors reported", err)
        self.assertFalse(os.path.exists(self.out))

    def test_unknown_name_serial(self):
        addr = self.start(state=self.fresh_state())
        code, _, err = self.run_main("--station", addr, "--out", self.out, "--name", "NOPE=x")
        self.assertEqual(code, 1)
        self.assertIn("no bus with that serial", err)
        code, _, err = self.run_main("--station", addr, "--out", self.out, "--name", "S=../x")
        self.assertEqual(code, 1)
        self.assertIn("plain file name", err)

    def test_queue_not_found(self):
        addr = self.start(read_result=ec.RR_QUEUE_NOT_FOUND)
        code, _, err = self.run_main("--station", addr, "--out", self.out)
        self.assertEqual(code, 1)
        self.assertIn("has no st3215/inference queue", err)

    def test_empty_queue(self):
        addr = self.start(state=None)
        code, _, err = self.run_main("--station", addr, "--out", self.out)
        self.assertEqual(code, 1)
        self.assertIn("queue is empty", err)

    def test_server_error(self):
        addr = self.start(read_result=ec.RR_SERVER_ERROR)
        code, _, err = self.run_main("--station", addr)
        self.assertEqual(code, 2)
        self.assertIn("server error", err)

    def test_undecodable_entry(self):
        addr = self.start(state=b"\x0b\x00\x00")
        code, _, err = self.run_main("--station", addr)
        self.assertEqual(code, 2)
        self.assertIn("NormaCore protocol changed?", err)
        self.assertIn("78f4bc96", err)

    def test_version_mismatch(self):
        addr = self.start(version=2)
        code, _, err = self.run_main("--station", addr, "--out", self.out)
        self.assertEqual(code, 2)
        self.assertIn("protocol version 2", err)
        self.assertIn("78f4bc96", err)
        self.assertEqual(self.station.seen, [[1]])  # stopped right after setup

    def test_connection_refused(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()  # nothing listens there now
        code, _, err = self.run_main("--station", "127.0.0.1:%d" % port)
        self.assertEqual(code, 2)
        self.assertIn("refused", err)
        self.assertIn("--tcp", err)

    def test_silent_station_times_out(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        try:
            code, _, err = self.run_main("--station", "127.0.0.1:%d" % s.getsockname()[1],
                                         "--timeout", "0.3")
        finally:
            s.close()
        self.assertEqual(code, 2)
        self.assertIn("did not answer", err)


if __name__ == "__main__":
    unittest.main()
