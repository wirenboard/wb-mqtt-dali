import json
from pathlib import Path

from dali.address import GearShort
from dali.exceptions import MemoryLocationNotImplemented
from dali.gear.general import (
    QueryFadeTimeFadeRate,
    QueryGroupsEightToFifteen,
    QueryGroupsZeroToSeven,
    QueryMaxLevel,
    QueryMinLevel,
    QueryPowerOnLevel,
    QuerySystemFailureLevel,
    QueryVersionNumber,
    SetMaxLevel,
    SetMinLevel,
    SetPowerOnLevel,
    SetSystemFailureLevel,
)
from dali.memory import info, oem

from .extended_gear_parameters import GearParam
from .wbdali import WBDALIDriver


class MaxLevelParam(GearParam):
    name = "Max level"
    property_name = "max_level"
    query_command_class = QueryMaxLevel
    set_command_class = SetMaxLevel


class MinLevelParam(GearParam):
    name = "Min level"
    property_name = "min_level"
    query_command_class = QueryMinLevel
    set_command_class = SetMinLevel


class PowerOnLevelParam(GearParam):
    name = "Power on level"
    property_name = "power_on_level"
    query_command_class = QueryPowerOnLevel
    set_command_class = SetPowerOnLevel


class SystemFailureLevelParam(GearParam):
    name = "System failure level"
    property_name = "system_failure_level"
    query_command_class = QuerySystemFailureLevel
    set_command_class = SetSystemFailureLevel


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

    async def write(self, driver: WBDALIDriver, address: GearShort, value: dict) -> None:
        pass


class GroupsParam:
    name = "Groups"

    async def read(self, driver: WBDALIDriver, addr: GearShort):
        groups = []

        groups_value = await driver.send(QueryGroupsZeroToSeven(addr))
        if groups_value is None:
            groups = [False for _ in range(8)]
        else:
            groups.extend([groups_value.raw_value[i] == 1 for i in range(8)])
        groups_value = await driver.send(QueryGroupsEightToFifteen(addr))
        if groups_value is None:
            groups.extend([False for _ in range(8)])
        else:
            groups.extend([groups_value.raw_value[i] == 1 for i in range(8)])
        return {"groups": groups}

    async def write(self, driver: WBDALIDriver, address: GearShort, value: dict) -> None:
        pass


class CommonParameters:
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

    params = [
        GroupsParam(),
        MaxLevelParam(),
        MinLevelParam(),
        PowerOnLevelParam(),
        SystemFailureLevelParam(),
        FadeTimeFadeRateParam(),
    ]

    async def read(self, driver: WBDALIDriver, addr: GearShort) -> dict:
        res = {}
        try:
            v = await driver.send(QueryVersionNumber(addr))
            bank0 = info.BANK_0_legacy if v is None or v.value == 1 else info.BANK_0
            self._update_info(res, await driver.run_sequence(bank0.read_all(addr)))
        except MemoryLocationNotImplemented:
            pass
        try:
            self._update_info(res, await driver.run_sequence(oem.BANK_1.read_all(addr)))
        except MemoryLocationNotImplemented:
            pass

        for param in self.params:
            try:
                value = await param.read(driver, addr)
                if value is not None:
                    res.update(value)
            except Exception as e:
                raise RuntimeError(f'Error reading "{param.name}"') from e
        return res

    async def write(self, driver: WBDALIDriver, address: GearShort, value: str) -> None:
        pass

    async def get_schema(self) -> dict:
        schema_path = Path("/usr/share/wb-mqtt-dali/schemas/control_gear.schema.json")
        with open(schema_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _update_info(self, dst: dict, values) -> None:
        for field, param in self.memory_fields_to_json_params.items():
            value = values.get(field)
            if value is not None:
                dst[param] = value
