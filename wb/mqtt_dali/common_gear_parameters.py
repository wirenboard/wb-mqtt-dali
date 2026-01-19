import json
from pathlib import Path

from dali.address import GearShort
from dali.exceptions import MemoryLocationNotImplemented, ResponseError
from dali.gear.general import (
    DTR0,
    AddToGroup,
    QueryFadeTimeFadeRate,
    QueryGroupsEightToFifteen,
    QueryGroupsZeroToSeven,
    QueryMaxLevel,
    QueryMinLevel,
    QueryPowerOnLevel,
    QuerySystemFailureLevel,
    QueryVersionNumber,
    ReadMemoryLocation,
    RemoveFromGroup,
    SetFadeRate,
    SetFadeTime,
    SetMaxLevel,
    SetMinLevel,
    SetPowerOnLevel,
    SetSystemFailureLevel,
)
from dali.memory import info, location, oem

from .extended_gear_parameters import GearParam, TypeParameters
from .wbdali import WBDALIDriver, check_query_response


def read_memory_bank(bank: info.MemoryBank, address: GearShort):
    last_address = yield from bank.LastAddress.read(address)
    if isinstance(last_address, location.FlagValue):
        raise ResponseError(
            f"Cannot read memory bank {bank.address}: last address location is {last_address.value}"
        )
    # Reading the last address also sets DTR1 appropriately

    # Bank 0 has a useful value at address 0x02; all other banks
    # use this for the lock/latch byte
    start_address = 0x02 if bank.address == 0 else 0x03
    yield DTR0(start_address)
    raw_data = [None] * start_address
    commands_count = last_address - start_address + 1
    r = yield [ReadMemoryLocation(address) for _ in range(commands_count)]
    for i in range(commands_count):
        if r[i].raw_value is not None:
            if r[i].raw_value.error:
                raise ResponseError(
                    f"Framing error while reading memory bank " f"{address} location {i + start_address}"
                )
            raw_data.append(r[i].raw_value.as_integer)
        else:
            raw_data.append(None)
    result = {}
    for memory_value in bank.values:
        try:
            r = memory_value.from_list(raw_data)
        except MemoryLocationNotImplemented:
            pass
        else:
            result[memory_value] = r
    return result


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

    def __init__(self) -> None:
        self._fade_time = None
        self._fade_rate = None

    async def read(self, driver: WBDALIDriver, address: GearShort):
        value = await driver.send(QueryFadeTimeFadeRate(address))
        check_query_response(value)
        self._fade_time = value.fade_time
        self._fade_rate = value.fade_rate
        return {
            "fade_time": value.fade_time,
            "fade_rate": value.fade_rate,
        }

    async def write(self, driver: WBDALIDriver, address: GearShort, value: dict) -> dict:
        fade_rate_to_set = value.get("fade_rate")
        fade_time_to_set = value.get("fade_time")

        if fade_rate_to_set is None and fade_time_to_set is None:
            return {}
        if self._fade_time == fade_time_to_set and self._fade_rate == fade_rate_to_set:
            return {}

        commands = []
        if fade_time_to_set is not None:
            commands.append(DTR0(fade_time_to_set))
            commands.append(SetFadeTime(address))
        if fade_rate_to_set is not None:
            commands.append(DTR0(fade_rate_to_set))
            commands.append(SetFadeRate(address))
        commands.append(QueryFadeTimeFadeRate(address))

        last_response = (await driver.send_commands(commands))[-1]
        check_query_response(last_response)
        self._fade_time = last_response.fade_time
        self._fade_rate = last_response.fade_rate
        return {
            "fade_time": self._fade_time,
            "fade_rate": self._fade_rate,
        }


class GroupsParam:
    name = "Groups"

    def __init__(self) -> None:
        self._groups = [False for _ in range(16)]

    async def read(self, driver: WBDALIDriver, address: GearShort):
        groups = []
        commands = [QueryGroupsZeroToSeven(address), QueryGroupsEightToFifteen(address)]
        responses = await driver.send_commands(commands)
        for response in responses:
            check_query_response(response)
            groups.extend([((response.raw_value.as_integer >> i) & 1) == 1 for i in range(8)])
        self._groups = groups
        return {"groups": groups}

    async def write(self, driver: WBDALIDriver, address: GearShort, value: dict) -> dict:
        groups_to_set = value.get("groups")
        if groups_to_set is None:
            return {}
        if self._groups == groups_to_set:
            return {}

        commands = []
        for i in range(16):
            if groups_to_set[i] != self._groups[i]:
                if groups_to_set[i]:
                    commands.append(AddToGroup(address, i))
                else:
                    commands.append(RemoveFromGroup(address, i))
        if not commands:
            return {}
        commands.append(QueryGroupsZeroToSeven(address))
        commands.append(QueryGroupsEightToFifteen(address))
        responses = await driver.send_commands(commands)
        groups = []
        for response in responses[-2:]:
            check_query_response(response)
            groups.extend([((response.raw_value.as_integer >> i) & 1) == 1 for i in range(8)])
        self._groups = groups
        return {"groups": groups}


class CommonParameters(TypeParameters):
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

    async def read(self, driver: WBDALIDriver, address: GearShort) -> dict:
        res = {}
        try:
            v = await driver.send(QueryVersionNumber(address))
            if v is None or v.raw_value is None or v.value == 1:
                bank0 = info.BANK_0_legacy
            else:
                bank0 = info.BANK_0
            self._update_info(res, await driver.run_sequence(read_memory_bank(bank0, address)))
        except MemoryLocationNotImplemented:
            pass
        try:
            self._update_info(res, await driver.run_sequence(read_memory_bank(oem.BANK_1, address)))
        except MemoryLocationNotImplemented:
            pass

        res.update(await super().read(driver, address))
        return res

    async def get_schema(self, driver: WBDALIDriver, address: GearShort) -> dict:
        schema_path = Path("/usr/share/wb-mqtt-dali/schemas/control_gear.schema.json")
        with open(schema_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _update_info(self, dst: dict, values) -> None:
        for field, param in self.memory_fields_to_json_params.items():
            value = values.get(field)
            if value is not None:
                dst[param] = value

    async def get_parameters(self, driver: WBDALIDriver, address: GearShort) -> list:
        return [
            GroupsParam(),
            MaxLevelParam(),
            MinLevelParam(),
            PowerOnLevelParam(),
            SystemFailureLevelParam(),
            FadeTimeFadeRateParam(),
        ]
