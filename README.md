# st3215-bus
Python bindings for Feetech ST3215 servo buses, built in Rust on NormaCore's st3215 protocol.

`st3215-bus` is a thin Python layer on top of the ST3215 wire protocol from
[NormaCore](https://github.com/norma-core/norma-core). Every packet is encoded
and decoded by NormaCore's protocol code; this package decides which requests to
send and returns the results as plain Python dicts.

One call reads or writes every motor on every bus. The buses are driven
concurrently on a tokio runtime and the GIL is released while they are, so a
control loop pays for one Python call per cycle, not one per motor. That matters
because NormaCore's protocol module offers a sync-write request (`SyncWrite`,
0x83) but no sync-read request, so reads are one request/response round trip per
motor. On real hardware, reading 4 buses x 8 motors takes about 3 ms.

It was developed and tested on
[ElRobot](https://github.com/norma-core/norma-core/tree/main/hardware/elrobot),
NormaCore's open-source, fully 3D-printed robot arm.

## Install

```sh
pip install st3215-bus
```

Wheels are abi3, so one wheel per platform covers CPython 3.8 and newer.

Building from a checkout (`pip install .`) needs a Rust toolchain; to produce
distributable wheels use `docker/build.sh`. Either way, clone with
`--recurse-submodules` (or run `git submodule update --init`): the protocol is
compiled from NormaCore's repository, which is a git submodule.

### Building wheels with Docker

`docker/build.sh` needs nothing but Docker: it builds Linux wheels for every
supported target, from an x86_64 or an aarch64 host, into `dist/`.

```sh
docker/build.sh                          # x86_64 + aarch64
docker/build.sh --targets all --sdist    # every target, plus the sdist
docker/build.sh --help                   # targets, glibc baseline, tests
```

## Usage

```python
import st3215_bus

with st3215_bus.Buses({"arm": "/dev/ttyUSB0"}) as buses:
    print(buses.ping())                    # {"arm": [1, 2, 3, 4, 5, 6, 7, 8]}
    pos = buses.read_positions()           # {"arm": {1: 2047, 2: 1985, ...}}
    buses.write_goals({"arm": {1: pos["arm"][1] + 50}})
    buses.set_torque(True)
```

Positions are raw encoder counts; `st3215_bus.COUNTS_PER_TURN` is 4096.

## API

Results are keyed by bus role name, then by motor id. Values are raw register
contents as unsigned integers (1- or 2-byte, little-endian, width taken from the
register).

**`Buses(ports: dict[str, str], timeout_ms: int = 20)`**
Opens one serial port per role (`{"arm": "/dev/ttyUSB0", ...}`) at 1 Mbaud.
`timeout_ms` bounds each request, i.e. how long a motor that does not answer
costs. Raises `OSError` if a port cannot be opened. Usable as a context manager.

**`ping(highest_id=8) -> dict[str, list[int]]`**
Ids `1..highest_id` that answer, per bus. Each id is tried up to 3 times, 60 ms
apart, because a freshly powered bus can miss the first ping.

**`read_positions(motors=[1..8]) -> dict[str, dict[int, int]]`**
Present position of each motor in encoder counts. A motor that does not answer
(after one retry) is **omitted** from its bus's dict, never reported as 0, since
0 is a legal position.

**`read_register(register: str, motors) -> dict[str, dict[int, int]]`**
Same as `read_positions` for any RAM register (names below). Unknown names raise
`RuntimeError`.

**`write_goals(goals: dict[str, dict[int, int]]) -> None`**
Goal positions, one `SyncWrite` packet per bus, buses in parallel. Buses not
named are left alone. Writes are not acknowledged by the servos. An unknown bus
name or a failed write raises `RuntimeError`.

**`write_register(register: str, values: dict[str, dict[int, int]]) -> None`**
`SyncWrite` of any RAM register, same shape and errors as `write_goals`.

**`read_eeprom(register: str, motors) -> dict[str, dict[int, int]]`**
Same as `read_register` for EEPROM registers (names below).

**`write_eeprom(register: str, values: dict[str, dict[int, int]]) -> dict[str, list[int]]`**
Writes EEPROM one motor at a time, with verification: unlock, `RegWrite` +
`Action`, read back, retried up to 5 times until the read-back matches, then
re-lock whatever happened. Returns, **for every bus held**, the sorted motor ids
whose value could not be verified, so all lists empty means every write took.
EEPROM is locked in normal operation and an unverified write can be silently
discarded, so check the result.

**`set_torque(enable: bool, motors=[1..8]) -> None`**
Torque on or off for the listed motors **on every bus held**. To change a
subset of buses, use `write_register("TorqueEnable", {...})`.

**`names -> list[str]`**
Role names of the open buses, sorted.

**`close() -> None`**
Releases the serial ports. After this, calls see no buses. Call it (or use
`with`) rather than relying on garbage collection: a port that is still held
makes the next process fail with "Device or resource busy".

### Register names

RAM (`read_register`, `write_register`): `TorqueEnable`, `Acc`,
`GoalPosition`, `GoalTime`, `GoalSpeed`, `TorqueLimit`, `Lock`,
`PresentPosition`, `PresentSpeed`, `PresentLoad`, `PresentVoltage`,
`PresentTemperature`, `Status`, `Moving`, `PresentCurrent`.

EEPROM (`read_eeprom`, `write_eeprom`): `ModelNumber`, `FirmwareVersion`,
`MaxTorque`, `MaxAngleLimit`, `MinAngleLimit`, `ProtectionCurrent`,
`ProtectionTorque`, `OverloadTorque`, `AngularResolution`, `MaxVoltage`,
`MinVoltage`, `UnloadCondition`, `LedAlarm`, `PCoef`, `ICoef`, `DCoef`,
`Offset`, `Mode`, `ID`.

Type stubs ship with the package (`py.typed`), so mypy and IDEs see the full API.

## Exporting calibration from a NormaCore station

NormaCore's station has a good calibration workflow for ST3215 arms, and it
keeps each motor's calibrated range (`range_min` / `range_max`, in encoder
counts) in its own state. `st3215-export-calibration`, installed with this
package, copies those ranges out of a running station into plain JSON files, so
code that drives the arms through `st3215_bus.Buses` can use them.

Prerequisites:

1. Run NormaCore's station yourself, following
   [NormaCore's instructions](https://github.com/norma-core/norma-core/tree/main/software/station/bin/station),
   with its TCP interface enabled: `station --web --tcp 127.0.0.1:8888`
   (NormaCore's desktop app enables it on 127.0.0.1:8888 for you).
2. Connect the arms and power them.
3. Complete the calibration in NormaCore's UI and save it ("freeze" it).

Then, with the station still running:

```sh
st3215-export-calibration --station 127.0.0.1:8888 --out calib/
# name the files by role instead of by adapter serial:
st3215-export-calibration --out calib/ --name 5A68001234=right_arm --name 5A68005678=left_arm
# just look, write nothing:
st3215-export-calibration --list
```

`python -m st3215_bus.export_calibration` works the same. Other options:
`--max-age SECONDS` (default 5), `--timeout SECONDS` (default 5), `--force`
(overwrite existing files, which are otherwise left alone). Exit codes: 0
exported, 1 refused (nothing written), 2 connection or protocol error.

Each bus is written to `<serial>.json` (or the `--name` given for it), keyed by
the USB serial number of its adapter, which is how NormaCore identifies a bus:

```json
{
  "format": "st3215-bus-calibration",
  "version": 1,
  "serial": "5A68001234",
  "port_name": "/dev/ttyACM0",
  "exported_at": "2026-10-13T14:00:21+00:00",
  "station": {"address": "127.0.0.1:8888", "instance_id": "..."},
  "motors": {
    "1": {"range_min": 1041, "range_max": 3055},
    "2": {"range_min": 909, "range_max": 3187}
  }
}
```

`st3215_bus.export_calibration.load_calibration(path)` reads such a file back,
with the motor ids as ints.

**Read-only.** The tool only sends the station `setup`, `ping` and `read`
requests (the latest entry of the `st3215/inference` queue), never a `write`,
so it cannot move a motor or change the station's state. It is pure Python
standard library and does not use this package's compiled extension: the
station holds the serial ports while it runs. The station's TCP interface has
no authentication, so bind it to localhost (`--tcp 127.0.0.1:8888`; a bare
`--tcp` listens on all interfaces) unless you need it reachable from elsewhere.

**Refusals.** Nothing is written, and the reason is printed, if any bus:

- has a state older than `--max-age` on the station's own clock (the station
  keeps its last state on disk across restarts, so an old snapshot is not
  mistaken for the arm in front of you): is the arm powered and connected?
- has a motor whose range is not saved, or whose saved range is invalid
  (`range_min` >= `range_max`, e.g. 0..0): finish and save the calibration in
  NormaCore's UI;
- reports no motors or no adapter serial number;
- would overwrite an existing file without `--force`.

**Compatibility.** NormaCore's station protocol is still in beta and may
change. The tool was tested against NormaCore revision
[`78f4bc96`](https://github.com/norma-core/norma-core/tree/78f4bc9678a1db3bc3c63f2ebc1ac175b98c9206)
(station v0.1.0-beta.9), the one pinned in `third_party/norma-core`. If the
station's replies stop decoding, it says so and exits with code 2 rather than
guessing.

## Supported platforms

Linux on x86_64 and aarch64 (manylinux wheels), CPython 3.8+. Ports are opened
by path, so no libudev is needed at runtime.

Those two are the wheels CI builds and publishes to PyPI. The Docker builder
above can also produce wheels for 32-bit ARM (armv7, manylinux) and for musl
distributions such as Alpine (x86_64 and aarch64, musllinux); these are not
published, so build them yourself if you need them.

## Credits and licensing

The ST3215 wire protocol implementation is NormaCore's
([norma-core/norma-core](https://github.com/norma-core/norma-core), Copyright (c)
2026 NormaCore). This repository does not copy it: NormaCore's repository is a
git submodule at `third_party/norma-core`, pinned to revision
`78f4bc9678a1db3bc3c63f2ebc1ac175b98c9206`, and its
[`software/drivers/st3215/src/protocol`](https://github.com/norma-core/norma-core/tree/78f4bc9678a1db3bc3c63f2ebc1ac175b98c9206/software/drivers/st3215/src/protocol)
module is compiled unchanged into the wheel. That code is redistributed under
NormaCore's MIT license, in compiled form in the wheels and as the protocol
source files in the sdist, and NormaCore's license notice ships with both. Thanks to the NormaCore team for the protocol
and for ElRobot.

This package is released under the [MIT license](LICENSE).

If you use, modify or redistribute this package, you must always adhere to
NormaCore's licensing terms for the NormaCore-derived parts (the protocol
module from `third_party/norma-core`), including keeping its copyright and
license notice.
