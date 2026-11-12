# DALI devices of the emulated buses: python-dali's fake Gear with the queries wb-mqtt-dali
# polls added, an energy-reporting (bank 202) gear model, and a DALI-2 control device fake.

from __future__ import annotations

import functools
import inspect
from dataclasses import dataclass
from types import ModuleType
from typing import Optional, Union

from dali import address, frame
from dali.command import Command
from dali.device import general as device_general
from dali.device import light, occupancy, pushbutton
from dali.gear import general as gear_general
from dali.gear import led
from dali.memory.diagnostics import BANK_205, BANK_206
from dali.memory.energy import BANK_202
from dali.memory.maintenance import BANK_207
from dali.tests import fakes

from e2e.large_installation.installation_profile import (
    ControlDeviceSpec,
    GearModel,
    GearSpec,
)
from wb.mqtt_dali.device import absolute_input_device, general_purpose_sensor

YES = 0xFF
MASK = 0xFF
NO_FEATURE_TYPE = 254
INITIALISE_ALL = 0xFF
INITIALISE_UNADDRESSED = 0x7F
# OTI DALI 15/220-240/1A0 NFC: parts 207, 251, 252, 253 of its datasheet.
ENERGY_GEAR_DEVICE_TYPES = [6, 50, 51, 52]
STANDARD_DIMMING_CURVE = 0
LED_DEVICE_TYPE = 6
GTIN_ADDRESS = 0x03
GTIN_LENGTH = 6
PART_103_VERSION_2_0 = 0x08
ENERGY_WH = 12345
STATUS_LAMP_ARC_POWER_ON = 0x04
STATUS_MISSING_SHORT_ADDRESS = 0x40
DEFAULT_FADE_TIME_FADE_RATE = 0x07
DEFAULT_TIMER = 20
DEFAULT_HYSTERESIS = 5
DEFAULT_HYSTERESIS_MIN = 10
PUSHBUTTON_EVENT_PRIORITY = 3
DEFAULT_EVENT_PRIORITY = 4
INSTANCE_TYPE_MODULES: dict[int, ModuleType] = {
    pushbutton.instance_type: pushbutton,
    absolute_input_device.instance_type: absolute_input_device,
    occupancy.instance_type: occupancy,
    light.instance_type: light,
    general_purpose_sensor.instance_type: general_purpose_sensor,
}


SEARCH_ADDRESS_SHIFTS = {
    device_general.SearchAddrH: 16,
    device_general.SearchAddrM: 8,
    device_general.SearchAddrL: 0,
}
SEARCH_QUERIES = (device_general.Compare, device_general.VerifyShortAddress, device_general.QueryShortAddress)


class EnergyBank(fakes.FakeMemoryBank):
    bank = BANK_202
    initial_contents = [
        0x0F, None, 0xFF, 0x01,  # last address, checksum, lock, bank version
        0x00, *ENERGY_WH.to_bytes(6, "big"),  # active energy: scale 10^0, Wh
        0x00, 0x00, 0x00, 0x00, 0x0A,  # active power: scale 10^0, W
    ]  # fmt: skip


class DiagnosticsBank205(fakes.FakeMemoryBank):
    bank = BANK_205


class DiagnosticsBank206(fakes.FakeMemoryBank):
    bank = BANK_206


class MaintenanceBank207(fakes.FakeMemoryBank):
    bank = BANK_207


PLAIN_GEAR_BANKS = (fakes.FakeBank0, fakes.FakeBank1)
ENERGY_GEAR_BANKS = (
    *PLAIN_GEAR_BANKS,
    EnergyBank,
    DiagnosticsBank205,
    DiagnosticsBank206,
    MaintenanceBank207,
)


class EmulatedGear(fakes.Gear):
    def __init__(self, spec: GearSpec) -> None:
        energy = spec.model == GearModel.ENERGY
        super().__init__(
            shortaddr=spec.address,
            groups=set(spec.groups),
            devicetypes=list(ENERGY_GEAR_DEVICE_TYPES) if energy else [],
            memory_banks=ENERGY_GEAR_BANKS if energy else PLAIN_GEAR_BANKS,
        )
        self.randomaddr = frame.Frame(24, spec.random_address)
        gtin = b"\xff" * GTIN_LENGTH if spec.gtin is None else spec.gtin.to_bytes(GTIN_LENGTH, "big")
        self.memory_banks[0].contents[GTIN_ADDRESS : GTIN_ADDRESS + GTIN_LENGTH] = list(gtin)
        self.fade_time_fade_rate = DEFAULT_FADE_TIME_FADE_RATE
        self.power_on_level = 254
        self.system_failure_level = 254

    def send(self, cmd: Command) -> Optional[int]:
        answer = super().send(cmd)
        if answer is not None or not self.valid_address(cmd):
            return answer
        if isinstance(cmd, gear_general.QueryStatus):
            status = STATUS_LAMP_ARC_POWER_ON if self.level else 0
            return status | (STATUS_MISSING_SHORT_ADDRESS if self.shortaddr is None else 0)
        answers: dict[type, int] = {
            gear_general.QueryFadeTimeFadeRate: self.fade_time_fade_rate,
            gear_general.QueryPowerOnLevel: self.power_on_level,
            gear_general.QuerySystemFailureLevel: self.system_failure_level,
        }
        if LED_DEVICE_TYPE in self.devicetypes:
            answers.update({led.QueryDimmingCurve: STANDARD_DIMMING_CURVE, led.QueryFastFadeTime: 0})
        return answers.get(type(cmd))


@dataclass
class _Search:
    initialising: bool = False
    withdrawn: bool = False
    search_address: int = 0


class ControlDevice(fakes.Device):
    """DALI-2 input device: addressing, commissioning, instances with types, instance queries."""

    def __init__(self, spec: ControlDeviceSpec) -> None:
        super().__init__(shortaddr=address.DeviceShort(spec.address))
        self._instances = [fakes.Device.Instance(inst_type=t, scheme=0) for t in spec.instance_types]
        self.random_address = spec.random_address
        self.device_groups = 0
        self._search = _Search()

    @property
    def short_address(self) -> Optional[int]:
        return None if self.shortaddr is None else self.shortaddr.address

    def send(self, cmd: Command) -> Optional[int]:
        if len(cmd.frame) != 24 or self._search_setup(cmd):
            return None
        if isinstance(cmd, SEARCH_QUERIES):
            return self._search_answer(cmd)
        if not self.valid_address(cmd):
            return None
        instance = getattr(cmd, "instance", None)
        if isinstance(instance, address.InstanceNumber):
            return self._instance_answer(instance.value, cmd.frame.as_integer & 0xFF)
        device_answer = self._device_answer(cmd)
        return device_answer if device_answer is not None else super().send(cmd)

    # --- Private ---

    def _search_setup(self, cmd: Command) -> bool:
        """Apply a commissioning command that never answers; False if `cmd` is not one."""
        search = self._search
        if isinstance(cmd, device_general.Initialise):
            own = INITIALISE_UNADDRESSED if self.short_address is None else self.short_address
            if cmd.param in (INITIALISE_ALL, own):
                search.initialising = True
                search.withdrawn = False
        elif isinstance(cmd, device_general.Terminate):
            self._search = _Search()
        elif type(cmd) in SEARCH_ADDRESS_SHIFTS:
            shift = SEARCH_ADDRESS_SHIFTS[type(cmd)]
            search.search_address = (search.search_address & ~(0xFF << shift)) | (cmd.param << shift)
        elif isinstance(cmd, device_general.Withdraw):
            if search.initialising and self.random_address == search.search_address:
                search.withdrawn = True
        elif isinstance(cmd, device_general.ProgramShortAddress):
            if search.initialising and self.random_address == search.search_address:
                self.shortaddr = None if cmd.param == MASK else address.DeviceShort(cmd.param)
        else:
            return False
        return True

    def _search_answer(self, cmd: Command) -> Optional[int]:
        search = self._search
        if not search.initialising:
            return None
        if isinstance(cmd, device_general.Compare):
            return YES if not search.withdrawn and self.random_address <= search.search_address else None
        if isinstance(cmd, device_general.VerifyShortAddress):
            return YES if self.short_address == cmd.param else None
        if self.random_address != search.search_address:
            return None
        return MASK if self.short_address is None else self.short_address

    def _device_answer(self, cmd: Command) -> Optional[int]:
        random_bytes = self.random_address.to_bytes(3, "big")
        group_queries = (
            device_general.QueryDeviceGroupsZeroToSeven,
            device_general.QueryDeviceGroupsEightToFifteen,
            device_general.QueryDeviceGroupsSixteenToTwentyThree,
            device_general.QueryDeviceGroupsTwentyFourToThirtyOne,
        )
        for index, query in enumerate(group_queries):
            if isinstance(cmd, query):
                return (self.device_groups >> (8 * index)) & 0xFF
        answers: dict[type, int] = {
            device_general.QueryRandomAddressH: random_bytes[0],
            device_general.QueryRandomAddressM: random_bytes[1],
            device_general.QueryRandomAddressL: random_bytes[2],
            device_general.QueryVersionNumber: PART_103_VERSION_2_0,
            device_general.QueryOperatingMode: 0,
            device_general.QueryDeviceCapabilities: 0,
        }
        return answers.get(type(cmd))

    def _instance_answer(self, number: int, opcode: int) -> Optional[int]:
        if number >= len(self._instances):
            return None
        return instance_query_answers(self._instances[number].inst_type).get(opcode)


@dataclass(frozen=True)
class BusReply:
    backward: Optional[int]
    collision: bool


class DaliBus(fakes.Bus):
    """Delivers 16- and 24-bit forward frames; `EnableDeviceType` applies to the next 16-bit frame."""

    def __init__(self, devices: list[Union[EmulatedGear, ControlDevice]]) -> None:
        super().__init__(devices)
        self._device_type = 0

    def transact(self, forward: frame.ForwardFrame) -> BusReply:
        device_type = self._device_type if len(forward) == 16 else 0
        cmd = Command.from_frame(forward, devicetype=device_type)
        if len(forward) == 16:
            self._device_type = cmd.param if isinstance(cmd, gear_general.EnableDeviceType) else 0
        answers = [a for a in (device.send(cmd) for device in self.gear) if a is not None]
        if not answers:
            return BusReply(None, False)
        return BusReply(answers[0] & 0xFF, len(answers) > 1)


@functools.cache
def instance_query_answers(instance_type: int) -> dict[int, int]:
    """Opcode -> answer of the instance queries for an instance type."""
    answers: dict[int, int] = {}
    modules = [device_general]
    if instance_type in INSTANCE_TYPE_MODULES:
        modules.append(INSTANCE_TYPE_MODULES[instance_type])
    for module in modules:
        for name, cls in _instance_queries(module):
            value = _default_instance_answer(name, instance_type)
            opcode = cls(address.DeviceShort(0), address.InstanceNumber(0)).frame.as_integer & 0xFF
            if value is None:
                answers.pop(opcode, None)
            else:
                answers[opcode] = value
    return answers


def _instance_queries(module: ModuleType) -> list[tuple[str, type]]:
    result = []
    for name, cls in inspect.getmembers(module, inspect.isclass):
        if not name.startswith("Query") or not issubclass(cls, Command) or cls.__module__ != module.__name__:
            continue
        if cls.response is None:
            continue
        try:
            cmd = cls(address.DeviceShort(0), address.InstanceNumber(0))
        except (TypeError, ValueError, NotImplementedError):
            continue
        if isinstance(getattr(cmd, "instance", None), address.InstanceNumber):
            result.append((name, cls))
    return result


def _default_instance_answer(  # pylint: disable=too-many-return-statements
    name: str, instance_type: int
) -> Optional[int]:
    if name == "QueryInstanceType":
        return instance_type
    if name in ("QueryInstanceEnabled", "QueryEventFilterZeroToSeven"):
        return YES
    if name in ("QueryPrimaryInstanceGroup", "QueryInstanceGroup1", "QueryInstanceGroup2"):
        return MASK
    if name in ("QueryFeatureType", "QueryNextFeatureType"):
        return NO_FEATURE_TYPE
    if name == "QueryEventPriority":
        return (
            PUSHBUTTON_EVENT_PRIORITY if instance_type == pushbutton.instance_type else DEFAULT_EVENT_PRIORITY
        )
    if "HysteresisMin" in name:
        return DEFAULT_HYSTERESIS_MIN
    if "Hysteresis" in name:
        return DEFAULT_HYSTERESIS
    if "Timer" in name:
        return DEFAULT_TIMER
    return 0
