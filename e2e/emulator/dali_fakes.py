# DALI devices of the emulated buses: python-dali's fake Gear with the queries wb-mqtt-dali
# polls added, an energy-reporting (bank 202) gear model, Helvar Digidim sensors that ignore DALI
# commands but take Helvar 17-bit ones, and a DALI-2 control device fake.

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

from e2e.emulator.installation_profile import (
    HELVAR_312_GTIN,
    ControlDeviceSpec,
    GearModel,
    GearSpec,
)
from wb.mqtt_dali.device import absolute_input_device, general_purpose_sensor
from wb.mqtt_dali.helvar.digidim import (
    EnableWriteE2,
    QueryMajorVersion,
    QueryMinorVersion,
    ReadE2,
    WriteE2,
)
from wb.mqtt_dali.helvar.e2 import E2_SIZE
from wb.mqtt_dali.helvar_device import HELVAR_321_GTIN, HelvarFirmware
from wb.mqtt_dali.wbdali import FF17_BIT_LENGTH

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
HELVAR_E2_GTIN = slice(0x00, 0x07)
HELVAR_E2_SERIAL = slice(0x07, 0x0A)
HELVAR_E2_SETTINGS = slice(0x10, 0x30)
# E2 of a live 311 with firmware 6.08.
HELVAR_311_E2 = bytes.fromhex(
    """
    50 30 79 70 03 11 80 15 db b4 01 0a ff 81 ff 06
    ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff
    07 03 06 82 01 00 f9 00 08 04 08 00 00 00 ff ff
    02 00 ff 1f 00 00 80 04 ff 1f 00 00 80 04 ff 1d
    00 00 80 04 ff 1e 00 00 84 00 ff fe 00 00 00 04
    ff 06 00 00 81 04 ff 10 00 00 81 04 ff 11 00 00
    81 04 ff 12 00 00 81 04 ff 13 00 00 81 04 ff 14
    00 00 81 04 ff 15 00 00 81 04 ff 16 00 00 81 04
    ff 17 00 00 00 04 ff 00 00 00 00 04 ff 10 00 00
    03 00 ff 1f 00 00 85 00 ff 01 00 00 85 00 ff 02
    00 00
    """
    + " ff" * 0x5C
    + " 02 06"
)
# Factory defaults of E2 0x10-0x2F for 320, 321, 322, 330.
HELVAR_32X_SETTINGS = bytes.fromhex(
    """
    ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff
    09 3c 06 82 01 fb fb 00 08 04 08 00 00 00 ff ff
    """
)
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
        if isinstance(cmd, gear_general.Randomise) and not self.initialising:
            return None
        answer = super().send(cmd)
        if isinstance(cmd, gear_general.QueryShortAddress):
            return self._query_short_address_answer()
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

    # --- Private ---

    def _query_short_address_answer(self) -> Optional[int]:
        # Withdrawn gear still answers: WITHDRAW mutes COMPARE only.
        if not self.initialising or self.randomaddr != self.searchaddr:
            return None
        return MASK if self.shortaddr is None else (self.shortaddr << 1) | 1


def _helvar_word(cmd: Command) -> int:
    """The first 16 bits of an FF17 frame, as `send_helvar` gets them."""
    return cmd.frame.as_integer >> 1


def helvar_gtin_bcd(gtin: int) -> bytes:
    """E2 bytes 0-6: the 13 GTIN digits as BCD, the low nibble of byte 6 zero."""
    return bytes.fromhex(f"{gtin:013d}0")


class Helvar311Gear(EmulatedGear):
    """Takes part in the search; of the addressed 16-bit commands answers QUERY CONTROL GEAR only.
    Helvar 17-bit commands: version, E2 read and write at DTR0, which advances by one."""

    default_firmware = HelvarFirmware(6, 8)
    factory_e2 = HELVAR_311_E2

    def __init__(self, spec: GearSpec) -> None:
        super().__init__(spec)
        self.firmware = spec.helvar_firmware or self.default_firmware
        self._e2 = bytearray(self.factory_e2)
        self._e2[HELVAR_E2_SERIAL] = spec.random_address.to_bytes(3, "big")
        self._write_e2_enabled = False

    def send(self, cmd: Command) -> Optional[int]:
        self._write_e2_enabled = False
        if hasattr(cmd, "destination") and not isinstance(cmd, gear_general.QueryControlGearPresent):
            return None
        return super().send(cmd)

    @property
    def e2(self) -> bytes:
        return bytes(self._e2)

    def send_helvar(self, word: int) -> Optional[int]:
        """A 17-bit frame; `word` is its first 16 bits. Enable Write E2 holds for the next frame only."""
        write_enabled, self._write_e2_enabled = self._write_e2_enabled, False
        if word >> 8 == _helvar_word(WriteE2(0)) >> 8:
            if write_enabled:
                self._e2[self.dtr0] = word & 0xFF
                self._advance_e2_address()
            return None
        if self.shortaddr is None:
            return None
        short = address.GearShort(self.shortaddr)
        if word == _helvar_word(EnableWriteE2(short)):
            self._write_e2_enabled = True
            return None
        if word == _helvar_word(ReadE2(short)):
            value = self._e2[self.dtr0]
            self._advance_e2_address()
            return value
        versions = {
            _helvar_word(QueryMajorVersion(short)): self.firmware.major,
            _helvar_word(QueryMinorVersion(short)): self.firmware.minor,
        }
        return versions.get(word)

    # --- Private ---

    def _advance_e2_address(self) -> None:
        self.dtr0 = (self.dtr0 + 1) % E2_SIZE


class Helvar321Gear(Helvar311Gear):
    """The 321 in the control gear space: the 311 image with its own GTIN and factory settings."""

    default_firmware = HelvarFirmware(10, 41)
    factory_e2 = (
        helvar_gtin_bcd(HELVAR_321_GTIN)
        + HELVAR_311_E2[HELVAR_E2_GTIN.stop : HELVAR_E2_SETTINGS.start]
        + HELVAR_32X_SETTINGS
        + HELVAR_311_E2[HELVAR_E2_SETTINGS.stop :]
    )


class Helvar312Gear(Helvar311Gear):
    """A Digidim model wb-mqtt-dali does not support: the 311 image with the 312 GTIN."""

    default_firmware = HelvarFirmware(6, 47)
    factory_e2 = helvar_gtin_bcd(HELVAR_312_GTIN) + HELVAR_311_E2[HELVAR_E2_GTIN.stop :]


HELVAR_GEAR_CLASSES: dict[GearModel, type[Helvar311Gear]] = {
    GearModel.HELVAR_311: Helvar311Gear,
    GearModel.HELVAR_321: Helvar321Gear,
    GearModel.HELVAR_312: Helvar312Gear,
}


def make_gear(spec: GearSpec) -> EmulatedGear:
    return HELVAR_GEAR_CLASSES.get(spec.model, EmulatedGear)(spec)


@dataclass
class _Search:
    initialising: bool = False
    withdrawn: bool = False
    search_address: int = 0


class ControlDevice(fakes.Device):
    """DALI-2 input device: addressing, commissioning, instances with types, instance queries."""

    def __init__(self, spec: ControlDeviceSpec) -> None:
        super().__init__(shortaddr=address.DeviceShort(spec.address))
        if spec.gtin is not None:
            self.memory_banks[0].contents[GTIN_ADDRESS : GTIN_ADDRESS + GTIN_LENGTH] = list(
                spec.gtin.to_bytes(GTIN_LENGTH, "big")
            )
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
    """Delivers 16- and 24-bit forward frames; `EnableDeviceType` applies to the next 16-bit frame.
    17-bit frames reach Helvar models only and leave everything else untouched."""

    def __init__(self, devices: list[Union[EmulatedGear, ControlDevice]]) -> None:
        super().__init__(devices)
        self._device_type = 0

    def transact(self, forward: frame.ForwardFrame) -> BusReply:
        if len(forward) == FF17_BIT_LENGTH:
            # The 17th bit is the last one sent, the least significant.
            word = forward.as_integer >> 1
            replies = [d.send_helvar(word) for d in self.gear if isinstance(d, Helvar311Gear)]
        else:
            device_type = self._device_type if len(forward) == 16 else 0
            cmd = Command.from_frame(forward, devicetype=device_type)
            if len(forward) == 16:
                self._device_type = cmd.param if isinstance(cmd, gear_general.EnableDeviceType) else 0
            replies = [device.send(cmd) for device in self.gear]
        answers = [a for a in replies if a is not None]
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
