import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dali.address import GearShort
from dali.exceptions import MemoryLocationNotImplemented
from dali.gear.converter import QueryDimmingCurve as ConverterQueryDimmingCurve
from dali.gear.emergency import QueryEmergencyLevel
from dali.gear.general import (
    QueryFadeTimeFadeRate,
    QueryGroupsEightToFifteen,
    QueryGroupsZeroToSeven,
    QueryMaxLevel,
    QueryMinLevel,
    QueryPowerOnLevel,
    QuerySystemFailureLevel,
    QueryVersionNumber,
)
from dali.gear.incandescent import QueryDimmingCurve as IncandescentQueryDimmingCurve
from dali.gear.led import QueryDimmingCurve as LEDQueryDimmingCurve
from dali.gear.led import QueryFastFadeTime
from dali.memory import info, oem
from dali.sequences import QueryDeviceTypes

from .gear.dimming_curve import QueryDimmingCurve as DimmingCurveQueryDimmingCurve
from .wbdali import WBDALIDriver


@dataclass
class DaliDeviceAddress:
    short: int
    random: int


memory_fields_to_json_params = {
    info.GTIN: "gtin",
    info.FirmwareVersion: "firmware_version",
    info.IdentificationNumber: "identification_number",
    info.IdentifictionNumber_legacy: "identification_number",
    info.HardwareVersion: "hardware_version",
    info.Part101Version: "part101_version",
    info.Part102Version: "part102_version",
    info.Part103Version: "part103_version",
    oem.ManufacturerGTIN: "oem_gtin",
    oem.LuminaireID: "oem_identification_number",
}


class MaxLevelParam:
    name = "Max level"

    async def read(self, driver: WBDALIDriver, addr: GearShort):
        value = await driver.send(QueryMaxLevel(addr))
        if value is None:
            return None
        return {"max_level": value}

    async def write(self, driver: WBDALIDriver, address: DaliDeviceAddress, value: str) -> None:
        pass


class MinLevelParam:
    name = "Min level"

    async def read(self, driver: WBDALIDriver, addr: GearShort):
        value = await driver.send(QueryMinLevel(addr))
        if value is None:
            return None
        return {"min_level": value}

    async def write(self, driver: WBDALIDriver, address: DaliDeviceAddress, value: str) -> None:
        pass


class PowerOnLevelParam:
    name = "Power on level"

    async def read(self, driver: WBDALIDriver, addr: GearShort):
        value = await driver.send(QueryPowerOnLevel(addr))
        if value is None:
            return None
        return {"power_on_level": value}

    async def write(self, driver: WBDALIDriver, address: DaliDeviceAddress, value: str) -> None:
        pass


class SystemFailureLevelParam:
    name = "System failure level"

    async def read(self, driver: WBDALIDriver, addr: GearShort):
        value = await driver.send(QuerySystemFailureLevel(addr))
        if value is None:
            return None
        return {"system_failure_level": value}

    async def write(self, driver: WBDALIDriver, address: DaliDeviceAddress, value: dict) -> None:
        pass


class FadeTimeFadeRateParam:
    name = "Fade time and fade rate"

    async def read(self, driver: WBDALIDriver, addr: GearShort):
        value = await driver.send(QueryFadeTimeFadeRate(addr))
        if value is None:
            return None
        return {
            "fade_time": value.fade_time,
            "fade_rate": value.fade_rate,
        }

    async def write(self, driver: WBDALIDriver, address: DaliDeviceAddress, value: dict) -> None:
        pass


class GroupsParam:
    name = "Groups"

    async def read(self, driver: WBDALIDriver, addr: GearShort):
        groups = []

        groups_value = await driver.send(QueryGroupsZeroToSeven(addr))
        if groups_value is None:
            groups = [False for _ in range(8)]
        else:
            groups.append([((groups_value.value >> i) & 1 == 1) for i in range(8)])
        groups_value = await driver.send(QueryGroupsEightToFifteen(addr))
        if groups_value is None:
            groups.extend([False for _ in range(8)])
        else:
            groups.extend([((groups_value.value >> i) & 1 == 1) for i in range(8)])
        return {"groups": groups}

    async def write(self, driver: WBDALIDriver, address: DaliDeviceAddress, value: dict) -> None:
        pass


COMMON_DEVICE_PARAMS = [
    GroupsParam(),
    MaxLevelParam(),
    MinLevelParam(),
    PowerOnLevelParam(),
    SystemFailureLevelParam(),
    FadeTimeFadeRateParam(),
]

# Type 0 fluorescent lamp ballast has no custom parameters

# Type 1 self-contained emergency lighting parameters


class Type1EmergencyLevelParam:
    name = "Emergency level"

    async def read(self, driver: WBDALIDriver, addr: GearShort):
        value = await driver.send(QueryEmergencyLevel(addr))
        if value is None:
            return None
        return {"type_1_emergency_level": value}

    async def write(self, driver: WBDALIDriver, address: DaliDeviceAddress, value: str) -> None:
        pass


# Type 2 Discharge lamps has no custom parameters
# Type 3 Low voltage halogen lamps has no custom parameters

# Type 4 Supply voltage controller for incandescent lamps


class Type4DimmingCurveParam:
    name = "Dimming curve"

    async def read(self, driver: WBDALIDriver, addr: GearShort):
        value = await driver.send(IncandescentQueryDimmingCurve(addr))
        if value is None:
            return None
        return {"type_4_dimming_curve": value}

    async def write(self, driver: WBDALIDriver, address: DaliDeviceAddress, value: str) -> None:
        pass


# Type 5 Conversion from digital signal into d. c. voltage

# Output range is write only


class Type5DimmingCurveParam:
    name = "Dimming curve"

    async def read(self, driver: WBDALIDriver, addr: GearShort):
        value = await driver.send(ConverterQueryDimmingCurve(addr))
        if value is None:
            return None
        return {"type_5_dimming_curve": value}

    async def write(self, driver: WBDALIDriver, address: DaliDeviceAddress, value: str) -> None:
        pass


# Type 6 LED modules


class Type6DimmingCurveParam:
    name = "Dimming curve"

    async def read(self, driver: WBDALIDriver, addr: GearShort):
        value = await driver.send(LEDQueryDimmingCurve(addr))
        if value is None:
            return None
        return {"type_6_dimming_curve": value}

    async def write(self, driver: WBDALIDriver, address: DaliDeviceAddress, value: str) -> None:
        pass


class Type6FastFadeTimeParam:
    name = "Fast fade time"

    async def read(self, driver: WBDALIDriver, addr: GearShort):
        value = await driver.send(QueryFastFadeTime(addr))
        if value is None:
            return None
        return {"type_6_fast_fade_time": value}

    async def write(self, driver: WBDALIDriver, address: DaliDeviceAddress, value: str) -> None:
        pass


# Type 7 Switching function
# TODO: implement

# Type 8 Colour control gear
# TODO: implement

# Type 9 Sequencer
# TODO: implement

# Type 15 Load referencing has no custom parameters
# Type 16 Thermal gear protection has no custom parameters, only reset command


# Type 17 Dimming curve selection
class Type17DimmingCurveParam:
    name = "Dimming curve"

    async def read(self, driver: WBDALIDriver, addr: GearShort):
        value = await driver.send(DimmingCurveQueryDimmingCurve(addr))
        if value is None:
            return None
        return {"type_17_dimming_curve": value}

    async def write(self, driver: WBDALIDriver, address: DaliDeviceAddress, value: str) -> None:
        pass


class DaliDevice:
    def __init__(self, uid: str, name: str, address: DaliDeviceAddress) -> None:
        self.uid = uid
        self.name = name
        self.address = address
        self.types: list[int] = []
        self.groups: list[str] = []
        self.params: Optional[dict] = {}

    async def load_info(self, driver: WBDALIDriver, force_reload: bool = False) -> None:
        if self.params and not force_reload:
            return
        self.params = {}
        short_addr = GearShort(self.address.short)
        try:
            v = await driver.send(QueryVersionNumber(short_addr))
            bank0 = info.BANK_0_legacy if v.value == 1 else info.BANK_0
            self._update_info(await driver.run_sequence(bank0.read_all(short_addr)))
        except MemoryLocationNotImplemented:
            pass
        try:
            self._update_info(await driver.run_sequence(oem.BANK_1.read_all(short_addr)))
        except MemoryLocationNotImplemented:
            pass

        for param in COMMON_DEVICE_PARAMS:
            try:
                value = await param.read(driver, short_addr)
                if value is not None:
                    self.params.update(value)
            except Exception as e:
                raise RuntimeError(f'Error reading "{param.name}" for device {self.name}') from e

        types = await driver.run_sequence(QueryDeviceTypes(short_addr))
        if types is None:
            raise RuntimeError(
                f"Device at short address {short_addr.address} did not respond to QueryDeviceTypes"
            )
        self.types = types

    def get_json_config(self) -> dict:
        res: dict = {
            "groups": self.groups,
        }
        if self.params:
            res.update(self.params)
        return res

    def get_config_schema(self) -> dict:
        schema_path = Path("/usr/share/wb-mqtt-dali/schemas/control_gear.schema.json")
        with open(schema_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _update_info(self, values) -> None:
        for field, param in memory_fields_to_json_params.items():
            value = values.get(field)
            if value is not None:
                self.params[param] = value


def make_device(bus_uid: str, address: DaliDeviceAddress) -> DaliDevice:
    return DaliDevice(
        uid=f"{bus_uid}_{address.short}",
        name=f"Dev {address.short}:{address.random:#x}",
        address=address,
    )
