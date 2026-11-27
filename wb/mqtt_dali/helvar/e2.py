"""E2 memory of Helvar Digidim 311/321 sensors: where each value lives and how its bytes decode,
and reading and writing it over FF17."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum, unique
from typing import Any, ClassVar, Iterable, Optional, TypeVar, Union

from dali.address import GearShort
from dali.command import Command
from dali.gear.general import DTR0

from ..bus_traffic import BusTrafficSource
from ..dali_common_parameters import SCENES_TOTAL
from ..on_off_control import FADE_TIME_CODE_MAX
from ..wbdali import FramePriority, WBDALIDriver
from ..wbdali_utils import (
    MASK,
    MAX_COMMAND_RETRIES,
    check_command_failed,
    query_responses_retry_from_first_failed,
)
from .digidim import EnableWriteE2, ExtendedDtrMsb, ReadE2, WriteE2

T = TypeVar("T")

E2_SIZE = 0x100
_MODE_FADE_TIME_SET = 0x80
# Modes of the commands acting on a press and a hold.
_BUTTON_MODES = (0x02, 0x03, 0x05, 0x06)
# Bit 0 of the target byte of an event slot; cleared, the command byte is a Direct Level.
_DIRECT_LEVEL_CLEARED_BIT = 0x01
DURATION_STEP_S = 20
# Gaps up to this many bytes are read through instead of starting a new run.
_READ_GAP_MAX = 2
DURATION_MAX_S = 0xFF * DURATION_STEP_S


@dataclass(frozen=True)
class E2Value:
    address: int
    size: int = 1

    @property
    def addresses(self) -> range:
        return range(self.address, self.address + self.size)

    def decode(self, data: bytes) -> Any:
        raise NotImplementedError

    def encode(self, value: Any, old: bytes) -> bytes:
        """The new bytes; `old` keeps the bits of other values sharing them."""
        raise NotImplementedError


@dataclass(frozen=True)
class Number(E2Value):
    """Unsigned, big-endian."""

    def decode(self, data: bytes) -> int:
        return int.from_bytes(data, "big")

    def encode(self, value: int, old: bytes) -> bytes:
        return value.to_bytes(self.size, "big")


@dataclass(frozen=True)
class Bcd(E2Value):
    """Decimal digits, two per byte; the nibbles after the last digit are 0."""

    digits: int = 0

    def decode(self, data: bytes) -> str:
        return data.hex()[: self.digits]

    def encode(self, value: str, old: bytes) -> bytes:
        return bytes.fromhex(value.ljust(2 * self.size, "0"))


@dataclass(frozen=True)
class Bits(E2Value):
    """An unsigned number in the bits of `mask` within one byte."""

    mask: int = 0xFF

    def decode(self, data: bytes) -> int:
        return (data[0] & self.mask) >> self._shift

    def encode(self, value: int, old: bytes) -> bytes:
        return bytes((((old[0] & ~self.mask) | (value << self._shift)) & 0xFF,))

    # --- Private ---

    @property
    def _shift(self) -> int:
        return (self.mask & -self.mask).bit_length() - 1


@dataclass(frozen=True)
class Flag(E2Value):
    """One bit; `inverted` when a cleared bit means on."""

    mask: int = 0x01
    inverted: bool = False

    def decode(self, data: bytes) -> bool:
        return bool(data[0] & self.mask) != self.inverted

    def encode(self, value: bool, old: bytes) -> bytes:
        return bytes((((old[0] & ~self.mask) | (self.mask if value != self.inverted else 0)) & 0xFF,))


@dataclass(frozen=True)
class Switch(E2Value):
    """A byte written whole as `on` or `off`; bit 0 tells them apart."""

    on: int = 0x01
    off: int = 0x00

    def decode(self, data: bytes) -> bool:
        return data[0] & 0x01 == self.on & 0x01

    def encode(self, value: bool, old: bytes) -> bytes:
        return bytes((self.on if value else self.off,))


@dataclass(frozen=True)
class Duration(E2Value):
    """Seconds, stored in steps of DURATION_STEP_S; a value between steps goes to the nearest."""

    def decode(self, data: bytes) -> int:
        return data[0] * DURATION_STEP_S

    def encode(self, value: int, old: bytes) -> bytes:
        return bytes(((value + DURATION_STEP_S // 2) // DURATION_STEP_S,))


@dataclass(frozen=True)
class ShortAddress:
    address: int

    @classmethod
    def decode(cls, byte: int) -> Optional[ShortAddress]:
        return cls(byte >> 1) if byte & 0x81 == 0x01 else None

    def encode(self) -> int:
        return (self.address << 1) | 1


@dataclass(frozen=True)
class Group:
    group: int

    @classmethod
    def decode(cls, byte: int) -> Optional[Group]:
        return cls((byte >> 1) & 0x0F) if byte & 0xE1 == 0x81 else None

    def encode(self) -> int:
        return 0x80 | (self.group << 1) | 1


@dataclass(frozen=True)
class Broadcast:
    @classmethod
    def decode(cls, byte: int) -> Optional[Broadcast]:
        return cls() if byte == 0xFF else None

    def encode(self) -> int:
        return 0xFF


Target = Union[ShortAddress, Group, Broadcast]


@dataclass(frozen=True)
class SendNothing:
    @classmethod
    def decode(cls, mode: int, fade_time: int, command: int) -> Optional[SendNothing]:
        return cls() if (mode, fade_time, command) == (0x07, 0x00, 0xFF) else None

    def encode(self) -> tuple[int, int, int]:
        return 0x07, 0x00, 0xFF


@dataclass(frozen=True)
class _FadedAction:
    """A DALI command; `fade_time` None leaves the fade time of the target as it is."""

    _MODE: ClassVar[int] = 0x00
    _FADE_TIME_NOT_SET: ClassVar[int] = 0x10
    _COMMAND: ClassVar[int] = 0

    fade_time: Optional[int] = None

    @classmethod
    def decode(cls, mode: int, fade_time: int, command: int) -> Optional[_FadedAction]:
        if not cls._is_command(command):
            return None
        if mode == cls._MODE | _MODE_FADE_TIME_SET and fade_time <= FADE_TIME_CODE_MAX:
            return cls._from_command(command, fade_time)
        # _FADE_TIME_NOT_SET is written, but any byte 1 with mode bit 7 cleared reads so.
        if mode == cls._MODE:
            return cls._from_command(command, None)
        return None

    def encode(self) -> tuple[int, int, int]:
        if self.fade_time is None:
            return self._MODE, self._FADE_TIME_NOT_SET, self._command_byte()
        return self._MODE | _MODE_FADE_TIME_SET, self.fade_time, self._command_byte()

    # --- Hooks for subclasses ---

    @classmethod
    def _is_command(cls, command: int) -> bool:
        return command == cls._COMMAND

    @classmethod
    def _from_command(cls, command: int, fade_time: Optional[int]) -> _FadedAction:
        del command
        return cls(fade_time=fade_time)

    def _command_byte(self) -> int:
        return self._COMMAND


@dataclass(frozen=True)
class Off(_FadedAction):
    _COMMAND: ClassVar[int] = 0x00


@dataclass(frozen=True)
class RecallMax(_FadedAction):
    _COMMAND: ClassVar[int] = 0x05


@dataclass(frozen=True)
class RecallMin(_FadedAction):
    _COMMAND: ClassVar[int] = 0x06


@dataclass(frozen=True)
class Up(_FadedAction):
    _COMMAND: ClassVar[int] = 0x01


@dataclass(frozen=True)
class Down(_FadedAction):
    _COMMAND: ClassVar[int] = 0x02


@dataclass(frozen=True)
class StepUp(_FadedAction):
    _COMMAND: ClassVar[int] = 0x03


@dataclass(frozen=True)
class StepDown(_FadedAction):
    _COMMAND: ClassVar[int] = 0x04


@dataclass(frozen=True)
class StepDownAndOff(_FadedAction):
    _COMMAND: ClassVar[int] = 0x07


@dataclass(frozen=True)
class OnAndStepUp(_FadedAction):
    _COMMAND: ClassVar[int] = 0x08


@dataclass(frozen=True)
class GoToScene(_FadedAction):
    _COMMAND: ClassVar[int] = 0x10

    scene: int = 0

    @classmethod
    def _is_command(cls, command: int) -> bool:
        return cls._COMMAND <= command < cls._COMMAND + SCENES_TOTAL

    @classmethod
    def _from_command(cls, command: int, fade_time: Optional[int]) -> GoToScene:
        return cls(fade_time=fade_time, scene=command - cls._COMMAND)

    def _command_byte(self) -> int:
        return self._COMMAND + self.scene


@dataclass(frozen=True)
class SceneRecallStore(GoToScene):
    """A press recalls the scene, a hold longer than the scene store delay stores the level in it."""

    _MODE: ClassVar[int] = 0x01


@dataclass(frozen=True)
class DirectLevel(_FadedAction):
    """DAPC: a slot whose target byte has bit 0 cleared, the level in the command byte."""

    level: int = 0

    @classmethod
    def _is_command(cls, command: int) -> bool:
        return command < MASK

    @classmethod
    def _from_command(cls, command: int, fade_time: Optional[int]) -> DirectLevel:
        return cls(fade_time=fade_time, level=command)

    def _command_byte(self) -> int:
        return self.level


@unique
class FixedCommand(Enum):
    """Commands without a parameter, by (mode, parameter, command) bytes; mode 0x08 sends them as FF17,
    modes 2-6 act on a press and a hold."""

    OFF_STORE_LAST_LEVEL = (0x20, 0, 0x00)
    MIN_STORE_LAST_LEVEL = (0x20, 0, 0x06)
    DISABLE_PIR_OFF = (0x08, 0, 0x30)
    ENABLE_PIR_OFF = (0x08, 0, 0x31)
    DISABLE_PIR_ON = (0x08, 0, 0x32)
    ENABLE_PIR_ON = (0x08, 0, 0x33)
    DISABLE_PROGRAMMING = (0x08, 0, 0x86)
    DISABLE_MANUAL_CONTROLS = (0x08, 0, 0x8A)
    ENABLE_MANUAL_CONTROLS = (0x08, 0, 0x8B)
    DISABLE_INFRA_RED = (0x08, 0, 0x8C)
    ENABLE_INFRA_RED = (0x08, 0, 0x8D)
    DISABLE_PANEL = (0x08, 0, 0x8E)
    ENABLE_PANEL = (0x08, 0, 0x8F)
    TOUCH_LAST_LEVEL = (0x02, 0, 0x1F)
    TOUCH_MAX_LEVEL = (0x02, 0, 0x05)
    TOUCH_SCENE_0 = (0x02, 0, 0x10)
    TOUCH_SCENE_1 = (0x02, 0, 0x11)
    LAST_LEVEL_OFF = (0x03, 0, 0x1F)
    MAX_LEVEL_OFF = (0x03, 0, 0x05)
    UP_STEP_UP = (0x05, 0, 0x01)
    UP_STEP_UP_AND_ON = (0x05, 8, 0x01)
    DOWN_STEP_DOWN = (0x05, 0, 0x02)
    DOWN_STEP_DOWN_AND_OFF = (0x05, 7, 0x02)
    COLOUR_TEMPERATURE_COOLER = (0x05, 0, 0xE8)
    COLOUR_TEMPERATURE_WARMER = (0x05, 0, 0xE9)
    MAX_LEVEL_UP = (0x06, 1, 0x05)
    MAX_LEVEL_DOWN = (0x06, 2, 0x05)
    LAST_LEVEL_UP = (0x06, 1, 0x1F)
    SCENE_0_UP = (0x06, 1, 0x10)
    SCENE_1_UP = (0x06, 1, 0x11)
    OFF_UP = (0x06, 1, 0x00)
    OFF_DOWN = (0x06, 2, 0x00)

    @property
    def takes_fade_time(self) -> bool:
        """With mode bit 7 set, byte 1 holds a fade time instead of the parameter 0."""
        mode, parameter, _ = self.value
        return mode in _BUTTON_MODES and parameter == 0


@dataclass(frozen=True)
class FixedAction:
    command: FixedCommand
    fade_time: Optional[int] = None

    @classmethod
    def decode(cls, mode: int, parameter: int, command: int) -> Optional[FixedAction]:
        fade_time = None
        if mode & _MODE_FADE_TIME_SET:
            mode, parameter, fade_time = mode & ~_MODE_FADE_TIME_SET, 0, parameter
        try:
            fixed_command = FixedCommand((mode, parameter, command))
        except ValueError:
            return None
        if fade_time is not None and not (fixed_command.takes_fade_time and fade_time <= FADE_TIME_CODE_MAX):
            return None
        return cls(fixed_command, fade_time)

    def encode(self) -> tuple[int, int, int]:
        mode, parameter, command = self.command.value
        if self.fade_time is None:
            return mode, parameter, command
        return mode | _MODE_FADE_TIME_SET, self.fade_time, command


@dataclass(frozen=True)
class SceneToggle(GoToScene):
    """Switches between the scene and Off on each press."""

    _MODE: ClassVar[int] = 0x03
    _FADE_TIME_NOT_SET: ClassVar[int] = 0x00
    SCENES: ClassVar[range] = range(12)

    @classmethod
    def _is_command(cls, command: int) -> bool:
        return command - cls._COMMAND in cls.SCENES


@dataclass(frozen=True)
class Ramp:
    """Mode type 4; what the mode, parameter and command bytes mean is unknown. The defaults are the
    factory Constant Light Out slot of a live 311 and are written whenever the slot is, replacing the
    bytes read."""

    _MODE_TYPE_MASK: ClassVar[int] = 0x07
    _MODE_TYPE: ClassVar[int] = 0x04

    mode: int = 0x84
    parameter: int = 0x00
    command: int = 0xFE

    @classmethod
    def decode(cls, mode: int, parameter: int, command: int) -> Optional[Ramp]:
        return cls(mode, parameter, command) if mode & cls._MODE_TYPE_MASK == cls._MODE_TYPE else None

    def encode(self) -> tuple[int, int, int]:
        return self.mode, self.parameter, self.command


_ACTION_CLASSES = (
    SendNothing,
    Off,
    Up,
    Down,
    StepUp,
    StepDown,
    RecallMax,
    RecallMin,
    StepDownAndOff,
    OnAndStepUp,
    GoToScene,
    SceneRecallStore,
    FixedAction,
    SceneToggle,
    Ramp,
)

Action = Union[
    SendNothing,
    Off,
    Up,
    Down,
    StepUp,
    StepDown,
    RecallMax,
    RecallMin,
    StepDownAndOff,
    OnAndStepUp,
    GoToScene,
    SceneRecallStore,
    DirectLevel,
    FixedAction,
    SceneToggle,
    Ramp,
]


@dataclass(frozen=True)
class Event:
    """What a sensor sends on an event; `action` None: a slot this map does not describe."""

    target: Target = Broadcast()
    action: Optional[Action] = None
    responds_to_groups: int = 0


@dataclass(frozen=True)
class EventSlot(E2Value):
    """Bytes: mode, fade time, target, command, Responds to Groups 8-15 and 0-7."""

    size: int = 6

    def decode(self, data: bytes) -> Event:
        mode, fade_time, target_byte, command = data[:4]
        target = _first(
            target_class.decode(target_byte | _DIRECT_LEVEL_CLEARED_BIT)
            for target_class in (ShortAddress, Group, Broadcast)
        )
        if target is None:
            return Event()
        if target_byte & _DIRECT_LEVEL_CLEARED_BIT:
            action = _first(action_class.decode(mode, fade_time, command) for action_class in _ACTION_CLASSES)
        else:
            action = DirectLevel.decode(mode, fade_time, command)
        return Event(target, action, int.from_bytes(data[4:], "big"))

    def encode(self, value: Event, old: bytes) -> bytes:
        if value.action is None:
            return old
        mode, fade_time, command = value.action.encode()
        target_byte = value.target.encode()
        if isinstance(value.action, DirectLevel):
            target_byte &= ~_DIRECT_LEVEL_CLEARED_BIT
        return bytes((mode, fade_time, target_byte, command)) + value.responds_to_groups.to_bytes(2, "big")


@unique
class DigidimE2(Enum):
    """E2 map of Digidim 311/321."""

    GTIN = Bcd(0x00, size=7, digits=13)
    SERIAL_NUMBER = Number(0x07, size=3)
    LOCK = Flag(0x0B, mask=0x80)
    SCENE_STORE_DELAY_S = Bits(0x0B, mask=0x7F)
    IR_ADDRESS_GROUP = Number(0x0C)
    IR_DISABLED = Switch(0x0E, on=0xF2, off=0xFF)
    SENSITIVITY = Number(0x20)
    ON_TIME_S = Duration(0x21)
    EXIT_DELAY_S = Duration(0x22)
    TRANSITION_S = Duration(0x24)
    CONSTANT_LIGHT = Flag(0x26, mask=0x01, inverted=True)
    IR_RECEIVER = Flag(0x26, mask=0x02, inverted=True)
    PRESENCE_SENSOR = Flag(0x26, mask=0x04, inverted=True)
    PRESENCE_TEST_MODE = Flag(0x26, mask=0x20, inverted=True)
    TOUCH_SWITCH_EVENT = EventSlot(0x30)
    PRESENCE_ON_EVENT = EventSlot(0x36)
    PRESENCE_MIN_EVENT = EventSlot(0x3C)
    PRESENCE_OFF_EVENT = EventSlot(0x42)
    CONSTANT_LIGHT_EVENT = EventSlot(0x48)
    IR_SELECTION_1_EVENT = EventSlot(0x54)
    IR_SELECTION_2_EVENT = EventSlot(0x5A)
    IR_SELECTION_3_EVENT = EventSlot(0x60)
    IR_SELECTION_4_EVENT = EventSlot(0x66)
    IR_SELECTION_5_EVENT = EventSlot(0x6C)
    IR_SELECTION_6_EVENT = EventSlot(0x72)
    IR_SELECTION_7_EVENT = EventSlot(0x78)
    IR_SELECTION_8_EVENT = EventSlot(0x7E)
    IR_WALL_PANEL_RIGHT_EVENT = EventSlot(0x84)
    IR_WALL_PANEL_LEFT_EVENT = EventSlot(0x8A)
    IR_ON_OFF_EVENT = EventSlot(0x90)
    IR_UP_EVENT = EventSlot(0x96)
    IR_DOWN_EVENT = EventSlot(0x9C)


class DigidimImage:
    """E2 of one sensor: the bytes read from it or written to it, and changes not yet written."""

    def __init__(self) -> None:
        self._data = bytearray(E2_SIZE)
        self._known: set[int] = set()
        self._changes: dict[int, int] = {}

    def __getitem__(self, field: DigidimE2) -> Any:
        return field.value.decode(self._bytes_of(field))

    def __setitem__(self, field: DigidimE2, value: Any) -> None:
        """Only remembered; `write` sends it."""
        new = field.value.encode(value, self._bytes_of(field))
        for e2_address, byte in zip(field.value.addresses, new):
            if byte == self._data[e2_address]:
                self._changes.pop(e2_address, None)
            else:
                self._changes[e2_address] = byte

    async def read(
        self, driver: WBDALIDriver, address: GearShort, fields: Iterable[DigidimE2], logger: logging.Logger
    ) -> None:
        """Read the bytes of the fields in one batch of runs; a retry starts from the first byte not read."""
        unread = {e2_address for field in fields for e2_address in field.value.addresses}
        failure: Optional[RuntimeError] = None
        for _ in range(MAX_COMMAND_RETRIES):
            runs = [
                (run, [DTR0(run[0]), ExtendedDtrMsb(0), *(ReadE2(address) for _ in run)])
                for run in _runs(unread, _READ_GAP_MAX)
            ]
            responses = iter(
                await driver.send_commands(
                    [cmd for _, commands in runs for cmd in commands],
                    BusTrafficSource.WB,
                    FramePriority.CONFIGURATION,
                    logger,
                )
            )
            for run, commands in runs:
                failure = self._take_run(run, commands, [next(responses) for _ in commands]) or failure
            unread -= self._known
            if not unread:
                return
            logger.debug("E2 bytes %s not read (%s), retrying", sorted(unread), failure)
        raise RuntimeError(
            f"E2 bytes {sorted(unread)} not read after {MAX_COMMAND_RETRIES} attempts: {failure}"
        )

    def drop_changes(self) -> None:
        self._changes = {}

    async def write(self, driver: WBDALIDriver, address: GearShort, logger: logging.Logger) -> None:
        """Write the changed bytes with the presence sensor off; on a failure the rest is dropped."""
        changes, self._changes = self._changes, {}
        if not changes:
            return
        presence_sensor = DigidimE2.PRESENCE_SENSOR.value
        dip = changes.pop(presence_sensor.address, self._data[presence_sensor.address])
        if self[DigidimE2.PRESENCE_SENSOR]:
            await self._write_bytes(
                driver, address, {presence_sensor.address: dip | presence_sensor.mask}, logger
            )
        try:
            await self._write_bytes(driver, address, changes, logger)
        finally:
            # Writing 0x26 restarts the presence logic of the sensor.
            if dip != self._data[presence_sensor.address]:
                await self._write_bytes(driver, address, {presence_sensor.address: dip}, logger)

    # --- Private ---

    async def _write_bytes(
        self, driver: WBDALIDriver, address: GearShort, changes: dict[int, int], logger: logging.Logger
    ) -> None:
        for run in _runs(changes):
            commands: list[Command] = [DTR0(run[0]), ExtendedDtrMsb(0)]
            for e2_address in run:
                commands.append(EnableWriteE2(address))
                commands.append(WriteE2(changes[e2_address]))
            await query_responses_retry_from_first_failed(
                driver, commands, len(commands), logger, priority=FramePriority.CONFIGURATION
            )
            self._data[run[0] : run[-1] + 1] = bytes(changes[e2_address] for e2_address in run)

    def _take_run(self, run: range, commands: list[Command], responses: list) -> Optional[RuntimeError]:
        """Take the bytes up to the first failed frame; after it the E2 address of the rest is unknown."""
        for e2_address, cmd, response in zip([None, None, *run], commands, responses):
            failure = check_command_failed(cmd, response)
            if failure is not None:
                return failure
            if e2_address is not None:
                self._data[e2_address] = response.raw_value.as_integer
                self._known.add(e2_address)
        return None

    def _bytes_of(self, field: DigidimE2) -> bytes:
        """With the changes; a byte never read is an error, since its other bits are unknown."""
        if not self._known.issuperset(field.value.addresses):
            raise RuntimeError(f"E2 field {field.name} has not been read from the sensor")
        return bytes(
            self._changes.get(e2_address, self._data[e2_address]) for e2_address in field.value.addresses
        )


def _runs(e2_addresses: Iterable[int], gap_max: int = 0) -> list[range]:
    """Runs of ascending addresses; a gap up to `gap_max` bytes stays inside a run."""
    runs: list[range] = []
    for e2_address in sorted(e2_addresses):
        if runs and e2_address - runs[-1].stop <= gap_max:
            runs[-1] = range(runs[-1].start, e2_address + 1)
        else:
            runs.append(range(e2_address, e2_address + 1))
    return runs


def _first(candidates: Iterable[Optional[T]]) -> Optional[T]:
    return next((candidate for candidate in candidates if candidate is not None), None)
