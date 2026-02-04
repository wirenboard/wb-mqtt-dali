import asyncio
from dataclasses import dataclass
from typing import Optional, Union

from dali.address import DeviceShort, GearShort, InstanceNumber
from dali.command import Command

from .utils import merge_json_schema_properties, merge_translations
from .wbdali import WBDALIDriver, check_query_response, query_request


@dataclass
class SettingsParamName:
    en: str
    ru: Optional[str] = None


@dataclass
class InstanceAddress:
    device_short: DeviceShort
    instance_number: InstanceNumber


SettingsParamAddress = Union[GearShort, DeviceShort, InstanceAddress]


class SettingsParamBase:
    def __init__(self, name: SettingsParamName) -> None:
        self.name = name

    async def read(self, driver: WBDALIDriver, address: SettingsParamAddress) -> dict:
        return {}

    async def write(self, driver: WBDALIDriver, address: SettingsParamAddress, value: dict) -> dict:
        """
        Write extended gear parameters to a DALI device.

        Args:
            driver (WBDALIDriver): The DALI driver instance used to communicate with devices.
            address (SettingsParamAddress): The address of the DALI control device to write to.
            value (dict): A dictionary containing the parameter values to write to the device.

        Returns:
            dict: An empty dictionary if nothing was changed, or a dictionary with the updated parameter values.
        """
        return {}

    def get_schema(self) -> dict:
        return {}


class NumberSettingsParam(SettingsParamBase):
    def __init__(self, name: SettingsParamName, property_name: str) -> None:
        super().__init__(name)
        self.property_name = property_name
        self.minimum = 0
        self.maximum = 255
        self.grid_columns = None
        self.property_order = None
        self.default: Optional[int] = None
        self.value = None
        self._is_read_only = False

    async def read(self, driver: WBDALIDriver, address: SettingsParamAddress) -> dict:
        self.value = await query_request(driver, self.get_read_command(address))
        return {self.property_name: self.value}

    async def write(self, driver: WBDALIDriver, address: SettingsParamAddress, value: dict) -> dict:
        if self.property_name not in value:
            return {}
        value_to_set = value[self.property_name]
        if self.value == value_to_set:
            return {}
        commands = self.get_write_commands(address, value_to_set)
        if not commands:
            raise RuntimeError(f"Set commands for {self.name.en} are not defined")
        commands.append(self.get_read_command(address))
        res = (await driver.send_commands(commands))[-1]
        check_query_response(res)
        self.value = res.raw_value.as_integer
        return {self.property_name: self.value}

    def get_schema(self) -> dict:
        schema: dict = {
            "properties": {
                self.property_name: {
                    "type": "integer",
                    "title": self.name.en,
                    "minimum": self.minimum,
                    "maximum": self.maximum,
                }
            },
        }
        has_options = False
        if self._is_read_only:
            schema["properties"][self.property_name]["options"] = {"wb": {"read_only": True}}
            has_options = True
        if self.grid_columns is not None:
            if not has_options:
                schema["properties"][self.property_name]["options"] = {}
            schema["properties"][self.property_name]["options"]["grid_columns"] = self.grid_columns
        if self.name.ru is not None:
            schema["translations"] = {"ru": {self.name.en: self.name.ru}}
        if self.default is not None:
            schema["properties"][self.property_name]["default"] = self.default
        if self.property_order is not None:
            schema["properties"][self.property_name]["propertyOrder"] = self.property_order
        return schema

    def get_write_commands(self, address: SettingsParamAddress, value_to_set: int) -> list[Command]:
        return []

    def get_read_command(self, address: SettingsParamAddress) -> Command:
        raise NotImplementedError()


class SettingsParamGroup(SettingsParamBase):
    def __init__(self, name: SettingsParamName, property_name: str) -> None:
        super().__init__(name)
        self._property_name = property_name
        self._parameters: list[SettingsParamBase] = []

    async def read(self, driver: WBDALIDriver, address: SettingsParamAddress) -> dict:
        res = {}
        awaitables = [param.read(driver, address) for param in self._parameters]
        results = await asyncio.gather(*awaitables, return_exceptions=True)
        for param, result in zip(self._parameters, results):
            if isinstance(result, BaseException):
                raise RuntimeError(f'Error reading "{param.name.en}": {result}') from result
            if result is not None:
                res.update(result)
        return {self._property_name: res}

    async def write(self, driver: WBDALIDriver, address: SettingsParamAddress, value: dict) -> dict:
        if self._property_name not in value:
            return {}
        instance_value = value[self._property_name]
        awaitables = [param.write(driver, address, instance_value) for param in self._parameters]
        results = await asyncio.gather(*awaitables, return_exceptions=True)
        res = {}
        for param, result in zip(self._parameters, results):
            if isinstance(result, BaseException):
                raise RuntimeError(f'Error writing "{param.name.en}": {result}') from result
            if result is not None:
                res.update(result)
        return {self._property_name: res}

    def get_schema(self) -> dict:
        res = {
            "properties": {
                self._property_name: {
                    "title": self.name.en,
                    "properties": {},
                }
            },
            "translations": {
                "ru": {
                    self.name.en: self.name.ru,
                }
            },
        }
        for param in self._parameters:
            merge_json_schema_properties(res["properties"][self._property_name], param.get_schema())
            merge_translations(res, param.get_schema())
        return res
