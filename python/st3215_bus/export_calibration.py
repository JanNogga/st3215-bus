"""Export per-motor calibration ranges from a running NormaCore station.

    st3215-export-calibration --station 127.0.0.1:8888 --out calib/
    python -m st3215_bus.export_calibration --list

NormaCore's station (https://github.com/norma-core/norma-core) owns the serial
ports while it runs and keeps each motor's calibrated range (``range_min`` /
``range_max``) in its state. This tool connects to the station's TCP interface
(``station --tcp``), reads the latest entry of the ``st3215/inference`` queue
and writes one JSON file per bus, named after the adapter's USB serial number.

It is READ-ONLY: the only requests it ever sends are ``setup``, ``ping`` and
``read``. It never sends a ``write`` request, so it cannot command a motor or
change the station's state. It uses only the Python standard library: the few
protobuf messages involved are encoded and decoded by hand below, and the
compiled ``st3215_bus`` extension is not used (it would need the serial ports,
which the station holds).

Tested against NormaCore revision 78f4bc96 (station v0.1.0-beta.9). NormaCore's
protocol is beta; if it changes, decoding fails with a message saying so rather
than producing wrong numbers.

Exit codes: 0 exported (or listed), 1 refused (nothing written), 2 connection
or protocol error.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["load_calibration", "main"]

TESTED_AGAINST = "NormaCore 78f4bc96 (station v0.1.0-beta.9)"
FORMAT = "st3215-bus-calibration"
FORMAT_VERSION = 1
DEFAULT_STATION = "127.0.0.1:8888"
DEFAULT_PORT = 8888
QUEUE_ID = "st3215/inference"
NORMFS_VERSION = 1  # SetupRequest.version that NormaCore's own clients send
MAX_FRAME = 64 << 20  # an InferenceState is a few KiB; anything huge is garbage

# normfs.ReadResponse.Result
RR_START, RR_ENTRY, RR_END, RR_QUEUE_NOT_FOUND, RR_NOT_FOUND, RR_SERVER_ERROR = range(6)
OT_SHIFT_FROM_TAIL = 1  # normfs.OffsetType

EXIT_OK, EXIT_REFUSED, EXIT_ERROR = 0, 1, 2

Fields = Dict[int, List[Any]]


class Refused(Exception):
    """The station answered, but there is nothing safe to export (exit 1)."""


class StationError(Exception):
    """Could not talk to the station, or did not understand it (exit 2)."""


def _protocol_error(what: str) -> StationError:
    return StationError(
        f"could not decode the station's reply ({what}). NormaCore protocol "
        f"changed? This tool was tested against {TESTED_AGAINST}."
    )


# --------------------------------------------------------------------------- #
# Protobuf wire format: just enough of it for the messages used here.

def _read_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    value = shift = 0
    while True:
        if pos >= len(buf) or shift > 63:
            raise _protocol_error("truncated or overlong varint")
        b = buf[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return value, pos


def decode(buf: bytes) -> Fields:
    """Decode one protobuf message into ``{field number: [values]}``.

    Varint and fixed-width fields become ints, length-delimited fields bytes
    (nested messages are decoded by calling this again). Unknown fields are
    kept and simply never looked at; groups (deprecated) are rejected.
    """
    fields: Fields = {}
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        number, wire = key >> 3, key & 7
        if number == 0:
            raise _protocol_error("field number 0")
        value: Any
        if wire == 0:
            value, pos = _read_varint(buf, pos)
        elif wire in (1, 5):
            width = 8 if wire == 1 else 4
            if pos + width > len(buf):
                raise _protocol_error("truncated fixed-width field")
            value = int.from_bytes(buf[pos:pos + width], "little")
            pos += width
        elif wire == 2:
            length, pos = _read_varint(buf, pos)
            if pos + length > len(buf):
                raise _protocol_error("truncated length-delimited field")
            value = bytes(buf[pos:pos + length])
            pos += length
        else:
            raise _protocol_error(f"unsupported wire type {wire} in field {number}")
        fields.setdefault(number, []).append(value)
    return fields


def _scalar(fields: Fields, number: int, default: Any = 0) -> Any:
    """Last value of a scalar field (proto3 omits zero/false/empty ones)."""
    values = fields.get(number)
    if not values:
        return default
    value = values[-1]
    if isinstance(value, bytes) != isinstance(default, (bytes, str)):
        raise _protocol_error(f"field {number} has an unexpected wire type")
    return value


def _string(fields: Fields, number: int) -> str:
    try:
        return str(_scalar(fields, number, b"").decode("utf-8"))
    except UnicodeDecodeError:
        raise _protocol_error(f"field {number} is not UTF-8") from None


def _message(fields: Fields, number: int) -> Optional[Fields]:
    values = fields.get(number)
    if not values:
        return None
    if not isinstance(values[-1], bytes):
        raise _protocol_error(f"field {number} is not a message")
    return decode(values[-1])


def _messages(fields: Fields, number: int) -> List[Fields]:
    out = []
    for value in fields.get(number, []):
        if not isinstance(value, bytes):
            raise _protocol_error(f"field {number} is not a message")
        out.append(decode(value))
    return out


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _field_varint(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def _field_bytes(number: int, value: bytes) -> bytes:
    return _varint(number << 3 | 2) + _varint(len(value)) + value


# --------------------------------------------------------------------------- #
# The three requests this tool sends (normfs.ClientRequest). There is no
# encoder for WriteRequest (ClientRequest field 10), on purpose.

def setup_request() -> bytes:
    """ClientRequest{setup: SetupRequest{version: 1}}."""
    return _field_bytes(1, _field_varint(1, NORMFS_VERSION))


def ping_request(sequence: int, client_timestamp_ns: int) -> bytes:
    """ClientRequest{ping: PingRequest{sequence, client_timestamp_ns}}."""
    return _field_bytes(2, _field_varint(1, sequence) + _field_varint(2, client_timestamp_ns))


def read_last_request(read_id: int, queue_id: str = QUEUE_ID) -> bytes:
    """ClientRequest{read: ReadRequest} for the newest entry of ``queue_id``.

    Offset 1 back from the tail, limit 1: the same request NormaCore's station
    viewer sends for its "last entry" reads. Not the streaming follow mode.
    """
    offset = _field_bytes(1, _field_bytes(1, struct.pack("<Q", 1)))  # Id{raw: u64 LE}
    offset += _field_varint(2, OT_SHIFT_FROM_TAIL)
    read = (
        _field_varint(1, read_id)
        + _field_bytes(2, queue_id.encode("utf-8"))
        + _field_varint(3, 1)  # step
        + _field_bytes(10, offset)
        + _field_varint(11, 1)  # limit
    )
    return _field_bytes(11, read)


# --------------------------------------------------------------------------- #
# Talking to the station.

def parse_address(text: str) -> Tuple[str, int]:
    """``HOST:PORT``, ``HOST`` (port 8888) or ``[IPV6]:PORT``."""
    host, port = text, DEFAULT_PORT
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise ValueError(f"bad address {text!r}")
        host, rest = text[1:end], text[end + 1:]
        if rest:
            if not rest.startswith(":"):
                raise ValueError(f"bad address {text!r}")
            port = int(rest[1:])
    elif text.count(":") == 1:
        host, _, port_text = text.partition(":")
        port = int(port_text)
    if not host or not 0 < port < 65536:
        raise ValueError(f"bad address {text!r}")
    return host, port


class Station:
    """A read-only normfs connection: setup, ping and read, nothing else."""

    def __init__(self, address: str, timeout: float) -> None:
        self.address = address
        self.timeout = timeout
        self.instance_id = ""
        self._deadline = time.monotonic() + timeout
        host, port = parse_address(address)
        try:
            self._sock = socket.create_connection((host, port), timeout=timeout)
        except ConnectionRefusedError:
            raise StationError(
                f"connection to {address} refused. Is the NormaCore station "
                f"running with --tcp (e.g. `station --web --tcp 127.0.0.1:8888`)?"
            ) from None
        except socket.timeout:
            raise StationError(f"no answer from {address} within {timeout:g} s") from None
        except OSError as e:
            raise StationError(f"cannot connect to {address}: {e}") from None

    def close(self) -> None:
        self._sock.close()

    def __enter__(self) -> "Station":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _send(self, payload: bytes) -> None:
        try:
            self._sock.sendall(struct.pack("<Q", len(payload)) + payload)
        except OSError as e:
            raise StationError(f"lost the connection to {self.address}: {e}") from None

    def _recv_exact(self, n: int) -> bytes:
        chunks = []
        while n:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout()
            self._sock.settimeout(remaining)
            chunk = self._sock.recv(min(n, 1 << 20))
            if not chunk:
                raise StationError(f"the station at {self.address} closed the connection")
            chunks.append(chunk)
            n -= len(chunk)
        return b"".join(chunks)

    def _recv(self) -> Fields:
        """Next non-empty ServerResponse."""
        try:
            while True:
                (size,) = struct.unpack("<Q", self._recv_exact(8))
                if size == 0:
                    continue  # empty frame: ignored, as NormaCore's client does
                if size > MAX_FRAME:
                    raise _protocol_error(f"frame of {size} bytes")
                return decode(self._recv_exact(size))
        except socket.timeout:
            raise StationError(
                f"the station at {self.address} did not answer within {self.timeout:g} s"
            ) from None
        except OSError as e:
            raise StationError(f"lost the connection to {self.address}: {e}") from None

    def setup(self) -> None:
        self._send(setup_request())
        while True:
            setup = _message(self._recv(), 1)
            if setup is None:
                continue
            version = _scalar(setup, 1)
            if version != NORMFS_VERSION:
                raise StationError(
                    f"the station speaks normfs protocol version {version}, this tool "
                    f"version {NORMFS_VERSION}. NormaCore protocol changed? This tool "
                    f"was tested against {TESTED_AGAINST}."
                )
            self.instance_id = _string(setup, 3)
            return

    def clock_ns(self) -> int:
        """The station's wall clock (ns since the epoch), from a ping."""
        self._send(ping_request(1, time.time_ns()))
        while True:
            ping = _message(self._recv(), 2)
            if ping is None:
                continue
            request = _message(ping, 4)
            if request is not None and _scalar(request, 1) not in (0, 1):
                continue  # someone else's ping
            stamp = _scalar(ping, 2)
            if not stamp:
                raise _protocol_error("ping reply without a timestamp")
            return int(stamp)

    def read_last(self, queue_id: str = QUEUE_ID, read_id: int = 1) -> bytes:
        """Data of the newest entry in ``queue_id``."""
        self._send(read_last_request(read_id, queue_id))
        while True:
            read = _message(self._recv(), 11)
            if read is None or _scalar(read, 1) != read_id:
                continue
            result = _scalar(read, 2)
            if result == RR_START:
                continue
            if result == RR_ENTRY:
                return bytes(_scalar(read, 11, b""))
            if result in (RR_END, RR_NOT_FOUND):
                raise Refused(
                    f"the station's {queue_id} queue is empty: it has not seen an "
                    f"ST3215 bus yet. Connect the arms and complete the calibration "
                    f"in NormaCore's UI."
                )
            if result == RR_QUEUE_NOT_FOUND:
                raise Refused(
                    f"the station has no {queue_id} queue. Is NormaCore's st3215 "
                    f"driver enabled in the station config, and has an arm been "
                    f"connected?"
                )
            if result == RR_SERVER_ERROR:
                raise StationError(f"the station reported a server error reading {queue_id}")
            raise _protocol_error(f"unknown read result {result}")


# --------------------------------------------------------------------------- #
# st3215.InferenceState -> buses.

def parse_inference_state(data: bytes) -> List[Dict[str, Any]]:
    """The calibration-relevant part of an ``st3215.InferenceState``."""
    buses = []
    for bus_state in _messages(decode(data), 10):
        bus = _message(bus_state, 1) or {}
        motors = []
        for m in _messages(bus_state, 10):
            motors.append({
                "id": int(_scalar(m, 1)),
                "range_min": int(_scalar(m, 10)),
                "range_max": int(_scalar(m, 11)),
                "frozen": bool(_scalar(m, 14)),
            })
        motors.sort(key=lambda m: m["id"])
        buses.append({
            "serial": _string(bus, 4),
            "port_name": _string(bus, 1),
            "system_stamp_ns": int(_scalar(bus_state, 3)),
            "motors": motors,
        })
    return buses


def bus_problems(bus: Dict[str, Any], now_ns: int, max_age: float) -> List[str]:
    """Why ``bus`` must not be exported; empty if it is fine."""
    problems = []
    if not bus["serial"]:
        problems.append("the adapter reports no USB serial number")
    if not bus["system_stamp_ns"]:
        problems.append("its state has no timestamp")
    else:
        age = (now_ns - bus["system_stamp_ns"]) / 1e9
        if age > max_age:
            problems.append(
                f"its state is {age:.0f} s old (--max-age {max_age:g}): is the arm "
                f"powered and connected to the running station?"
            )
    if not bus["motors"]:
        problems.append("no motors reported: is the arm powered?")
    unfrozen = [m["id"] for m in bus["motors"] if not m["frozen"]]
    if unfrozen:
        problems.append(
            f"motor(s) {', '.join(map(str, unfrozen))} have no saved range: "
            f"complete and save the calibration in NormaCore's UI"
        )
    # Downstream count<->angle conversion assumes range_min < range_max.
    # NormaCore centres the ranges when it saves them, so this should not
    # happen; an empty 0..0 range or a wrapped one (e.g. 3000..1000) means the
    # calibration is unusable.
    invalid = [m["id"] for m in bus["motors"]
               if m["frozen"] and m["range_min"] >= m["range_max"]]
    if invalid:
        problems.append(
            f"motor(s) {', '.join(map(str, invalid))} have an invalid saved range "
            f"(range_min >= range_max): redo and save the calibration in NormaCore's UI"
        )
    return problems


def calibration_document(bus: Dict[str, Any], station: str, instance_id: str,
                         exported_at: str) -> Dict[str, Any]:
    return {
        "format": FORMAT,
        "version": FORMAT_VERSION,
        "serial": bus["serial"],
        "port_name": bus["port_name"],
        "exported_at": exported_at,
        "station": {"address": station, "instance_id": instance_id},
        "motors": {
            str(m["id"]): {"range_min": m["range_min"], "range_max": m["range_max"]}
            for m in bus["motors"]
        },
    }


def load_calibration(path: "str | os.PathLike[str]") -> Dict[str, Any]:
    """Read a file written by this tool.

    Returns the document with ``motors`` keyed by int motor id, e.g.
    ``doc["motors"][1]["range_min"]``. Raises ``ValueError`` if the file is
    not an st3215-bus calibration of a supported version.
    """
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise ValueError(f"{path}: not an {FORMAT} file")
    if doc.get("version") != FORMAT_VERSION:
        raise ValueError(f"{path}: unsupported {FORMAT} version {doc.get('version')!r}")
    doc["motors"] = {int(k): v for k, v in doc["motors"].items()}
    return doc


# --------------------------------------------------------------------------- #
# Command line.

def _parse_names(pairs: List[str]) -> Dict[str, str]:
    names: Dict[str, str] = {}
    for pair in pairs:
        serial, sep, name = pair.partition("=")
        if not sep or not serial or not name:
            raise Refused(f"--name {pair!r}: expected SERIAL=NAME")
        if name in (".", "..") or "/" in name or "\\" in name or "\0" in name:
            raise Refused(f"--name {pair!r}: NAME must be a plain file name")
        names[serial] = name
    return names


def _write_json(path: str, doc: Dict[str, Any]) -> None:
    """Write atomically, so an interrupted run never leaves half a file."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def _print_list(buses: List[Dict[str, Any]], now_ns: int) -> None:
    for bus in buses:
        age = (now_ns - bus["system_stamp_ns"]) / 1e9 if bus["system_stamp_ns"] else float("nan")
        print(f"bus {bus['serial'] or '(no serial)'}  port {bus['port_name'] or '?'}  "
              f"state {age:.1f} s old  {len(bus['motors'])} motor(s)")
        for m in bus["motors"]:
            print(f"  motor {m['id']:>3}  range {m['range_min']:>5} .. {m['range_max']:<5}  "
                  f"{'saved' if m['frozen'] else 'NOT SAVED'}")


def _export(args: argparse.Namespace) -> int:
    names = _parse_names(args.name)
    with Station(args.station, args.timeout) as station:
        station.setup()
        data = station.read_last()
        now_ns = station.clock_ns()
        buses = parse_inference_state(data)

    if args.list:
        print(f"station {args.station} (instance {station.instance_id or '?'}): "
              f"{len(buses)} bus(es)")
        _print_list(buses, now_ns)
        return EXIT_OK

    if not buses:
        raise Refused("the station reports no ST3215 buses: are the arms connected?")

    refusals = []
    serials = [b["serial"] for b in buses]
    for serial in sorted(set(s for s in serials if s and serials.count(s) > 1)):
        refusals.append(f"serial {serial} appears on more than one bus")
    for serial in sorted(set(names) - set(serials)):
        refusals.append(f"--name {serial}=...: the station has no bus with that serial "
                        f"(it has: {', '.join(s for s in serials if s) or 'none'})")
    for bus in buses:
        for problem in bus_problems(bus, now_ns, args.max_age):
            refusals.append(f"bus {bus['serial'] or '?'} ({bus['port_name'] or '?'}): {problem}")

    targets = []
    for bus in buses:
        name = names.get(bus["serial"], bus["serial"])
        targets.append((bus, name, os.path.join(args.out, name + ".json")))
    paths = [t[2] for t in targets]
    for path in sorted(set(p for p in paths if paths.count(p) > 1)):
        refusals.append(f"two buses would both be written to {path}")
    if not args.force:
        for _, _, path in targets:
            if os.path.exists(path):
                refusals.append(f"{path} exists (use --force to overwrite)")

    if refusals:
        print("Refusing to export; nothing was written:", file=sys.stderr)
        for r in refusals:
            print(f"  - {r}", file=sys.stderr)
        return EXIT_REFUSED

    os.makedirs(args.out, exist_ok=True)
    exported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for bus, _, path in targets:
        _write_json(path, calibration_document(bus, args.station, station.instance_id,
                                               exported_at))
        ranges = ", ".join(f"{m['id']}:{m['range_min']}-{m['range_max']}" for m in bus["motors"])
        print(f"{path}: bus {bus['serial']} ({bus['port_name']}), "
              f"{len(bus['motors'])} motors [{ranges}]")
    print(f"exported {len(targets)} bus(es) from station {args.station} "
          f"(instance {station.instance_id or '?'})")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="st3215-export-calibration",
        description=(
            "Export per-motor calibration ranges (range_min/range_max) from a "
            "running NormaCore station, one JSON file per ST3215 bus. Read-only: "
            "it only sends setup, ping and read requests, never a write."
        ),
        epilog=(
            "Run NormaCore's station with its TCP interface enabled (e.g. "
            "`station --web --tcp 127.0.0.1:8888`), connect the arms, and "
            "complete and save the calibration in NormaCore's UI first. "
            f"Tested against {TESTED_AGAINST}. Exit codes: 0 ok, 1 refused "
            "(nothing written), 2 connection or protocol error."
        ),
    )
    p.add_argument("--station", default=DEFAULT_STATION, metavar="HOST:PORT",
                   help=f"the station's --tcp address (default {DEFAULT_STATION})")
    p.add_argument("--out", default=".", metavar="DIR",
                   help="directory for the JSON files, created if missing (default: .)")
    p.add_argument("--name", action="append", default=[], metavar="SERIAL=NAME",
                   help="write bus SERIAL to NAME.json instead of SERIAL.json (repeatable)")
    p.add_argument("--max-age", type=float, default=5.0, metavar="SECONDS",
                   help="refuse buses whose state is older than this on the station's "
                        "clock (default 5)")
    p.add_argument("--timeout", type=float, default=5.0, metavar="SECONDS",
                   help="give up if the station has not answered within this (default 5)")
    p.add_argument("--list", action="store_true",
                   help="print buses, serials, ports, motors, ranges and whether they are "
                        "saved; write nothing")
    p.add_argument("--force", action="store_true", help="overwrite existing files")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    """Run the command line tool; returns the exit code."""
    args = build_parser().parse_args(argv)
    prog = "st3215-export-calibration"
    try:
        parse_address(args.station)
    except ValueError:
        print(f"{prog}: error: --station must be HOST:PORT, got {args.station!r}",
              file=sys.stderr)
        return EXIT_ERROR
    try:
        return _export(args)
    except Refused as e:
        print(f"{prog}: refused: {e}", file=sys.stderr)
        return EXIT_REFUSED
    except StationError as e:
        print(f"{prog}: error: {e}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
