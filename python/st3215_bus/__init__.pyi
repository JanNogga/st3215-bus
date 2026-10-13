"""Python bindings for Feetech ST3215 servo buses, built in Rust on NormaCore's st3215 protocol.

Positions are raw encoder counts (``COUNTS_PER_TURN`` per revolution). Bus
results are keyed by the role names given to :class:`Buses`, motor results by
motor id.
"""

from collections.abc import Sequence
from types import TracebackType
from typing import Literal, final

from typing_extensions import Self

__all__ = ["Buses", "COUNTS_PER_TURN"]

COUNTS_PER_TURN: int
"""Encoder counts per revolution of an ST3215 (4096)."""

@final
class Buses:
    """Serial buses to a set of ST3215 adapters, addressed by role name.

    Every call that talks to the servos drives all buses concurrently on an
    internal tokio runtime and releases the GIL while it waits. Motors on one
    bus are addressed one after another, each request bounded by
    ``timeout_ms``.
    """

    def __new__(cls, ports: dict[str, str], timeout_ms: int = 20) -> Self:
        """Open one serial port per role at 1 Mbaud.

        :param ports: role name -> device path, e.g. ``{"arm": "/dev/ttyUSB0"}``.
        :param timeout_ms: per-request timeout in milliseconds. It bounds how
            long a motor that does not answer costs.
        :raises OSError: a port could not be opened.
        """

    def ping(self, highest_id: int = 8) -> dict[str, list[int]]:
        """Motor ids ``1..=highest_id`` that answer a ping, per bus.

        Each id is tried up to 3 times, 60 ms apart, because a bus whose
        supply has just come up commonly misses the first ping.
        """

    def read_positions(self, motors: Sequence[int] = ...) -> dict[str, dict[int, int]]:
        """Present position of each motor, per bus, in encoder counts.

        ``motors`` defaults to ids 1-8. A motor that does not answer (after one
        retry) is omitted from its bus's dict rather than reported as zero,
        since zero is a legal position.
        """

    def read_register(self, register: str, motors: Sequence[int]) -> dict[str, dict[int, int]]:
        """As :meth:`read_positions`, for any named RAM register.

        Accepted names: TorqueEnable, Acc, GoalPosition, GoalTime, GoalSpeed,
        TorqueLimit, Lock, PresentPosition, PresentSpeed, PresentLoad,
        PresentVoltage, PresentTemperature, Status, Moving, PresentCurrent.
        Values are the raw register contents (1- or 2-byte, little-endian).

        :raises RuntimeError: unknown register name.
        """

    def write_goals(self, goals: dict[str, dict[int, int]]) -> None:
        """Command goal positions: ``{bus: {motor: counts}}``.

        One SyncWrite packet per bus, buses in parallel. Buses not named are
        left alone. Writes are not acknowledged by the servos.

        :raises RuntimeError: unknown bus name, or the write failed.
        """

    def write_register(self, register: str, values: dict[str, dict[int, int]]) -> None:
        """Write any named RAM register (see :meth:`read_register`) by SyncWrite.

        The byte width comes from the register itself.

        :raises RuntimeError: unknown register or bus name, or the write failed.
        """

    def read_eeprom(self, register: str, motors: Sequence[int]) -> dict[str, dict[int, int]]:
        """As :meth:`read_register`, for a named EEPROM register.

        Accepted names: ModelNumber, FirmwareVersion, MaxTorque, MaxAngleLimit,
        MinAngleLimit, ProtectionCurrent, ProtectionTorque, OverloadTorque,
        AngularResolution, MaxVoltage, MinVoltage, UnloadCondition, LedAlarm,
        PCoef, ICoef, DCoef, Offset, Mode, ID.

        :raises RuntimeError: unknown register name.
        """

    def write_eeprom(self, register: str, values: dict[str, dict[int, int]]) -> dict[str, list[int]]:
        """Write a named EEPROM register and verify it, one motor at a time.

        Per motor: unlock EEPROM, RegWrite + Action, read back, retried up to
        5 times until the read-back matches, then re-lock EEPROM whatever the
        outcome.

        :returns: per bus held, the sorted motor ids whose value could NOT be
            verified. Every list empty means every write took.
        :raises RuntimeError: unknown register or bus name.
        """

    def set_torque(self, enable: bool, motors: Sequence[int] = ...) -> None:
        """Torque on or off for ``motors`` (default ids 1-8) on every bus held.

        :raises RuntimeError: the write failed.
        """

    @property
    def names(self) -> list[str]:
        """Role names of the open buses, sorted."""

    def close(self) -> None:
        """Release the serial ports. Later calls see no buses.

        Worth calling explicitly: a port still held makes the next process
        fail with "Device or resource busy".
        """

    def __enter__(self) -> Self: ...
    def __exit__(
        self,
        t: type[BaseException] | None,
        v: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        """Close the ports; never suppresses an exception."""
