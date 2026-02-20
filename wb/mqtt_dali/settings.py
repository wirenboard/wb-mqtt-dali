import asyncio
from dataclasses import dataclass
from typing import Callable, Optional

from dali.command import Command

from .utils import merge_json_schema_properties, merge_translations
from .wbdali_utils import WBDALIDriver, check_query_response, query_int


@dataclass
class SettingsParamName:
    en: str
    ru: Optional[str] = None


class SettingsParamBase:
    def __init__(self, name: SettingsParamName) -> None:
        self.name = name

    async def read(self, driver: WBDALIDriver, short_address: int) -> dict:
        return {}

    async def write(self, driver: WBDALIDriver, short_address: int, value: dict) -> dict:
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


class BooleanSettingsParam(SettingsParamBase):
    def __init__(
        self,
        name: SettingsParamName,
        property_name: str,
        query_command_factory: Callable[[int], Command],
        enable_command_factory: Callable[[int], Command],
        disable_command_factory: Callable[[int], Command],
    ) -> None:
        super().__init__(name)
        self.property_name = property_name
        self._query_factory = query_command_factory
        self._enable_factory = enable_command_factory
        self._disable_factory = disable_command_factory
        self.grid_columns = None
        self.property_order = None
        self.default: Optional[bool] = None
        self.value = None
        self._is_read_only = False

    async def read(self, driver: WBDALIDriver, short_address: int) -> dict:
        response = await driver.send(self._query_factory(short_address))
        if response is None or response.value is None:
            raise RuntimeError(f"Failed to read {self.property_name} state")
        self.value = bool(response.value)
        return {self.property_name: self.value}

    async def write(self, driver: WBDALIDriver, short_address: int, value: dict) -> dict:
        if self.property_name not in value:
            return {}

        value_to_set = value[self.property_name]
        if self.value == value_to_set:
            return {}

        command_factory = self._enable_factory if bool(value_to_set) else self._disable_factory
        await driver.send(command_factory(short_address))
        return await self.read(driver, short_address)

    def get_schema(self) -> dict:
        schema: dict = {
            "properties": {
                self.property_name: {
                    "type": "boolean",
                    "title": self.name.en,
                    "format": "switch",
                }
            }
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

    async def read(self, driver: WBDALIDriver, short_address: int) -> dict:
        self.value = await query_int(driver, self.get_read_command(short_address))
        return {self.property_name: self.value}

    async def write(self, driver: WBDALIDriver, short_address: int, value: dict) -> dict:
        if self.property_name not in value:
            return {}
        value_to_set = value[self.property_name]
        if self.value == value_to_set:
            return {}
        commands = self.get_write_commands(short_address, value_to_set)
        commands.append(self.get_read_command(short_address))
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

    def get_write_commands(self, short_address: int, value_to_set: int) -> list[Command]:
        raise NotImplementedError(f"Write commands for {self.name.en} are not defined")

    def get_read_command(self, short_address: int) -> Command:
        raise NotImplementedError(f"Read commands for {self.name.en} are not defined")


class SettingsParamGroup(SettingsParamBase):
    def __init__(self, name: SettingsParamName, property_name: str) -> None:
        super().__init__(name)

        self.property_order = None

        self._property_name = property_name
        self._parameters: list[SettingsParamBase] = []

    async def read(self, driver: WBDALIDriver, short_address: int) -> dict:
        res = {}
        awaitables = [param.read(driver, short_address) for param in self._parameters]
        results = await asyncio.gather(*awaitables, return_exceptions=True)
        for param, result in zip(self._parameters, results):
            if isinstance(result, BaseException):
                raise RuntimeError(f'Error reading "{param.name.en}": {result}') from result
            if result is not None:
                res.update(result)
        return {self._property_name: res}

    async def write(self, driver: WBDALIDriver, short_address: int, value: dict) -> dict:
        if self._property_name not in value:
            return {}
        instance_value = value[self._property_name]
        awaitables = [param.write(driver, short_address, instance_value) for param in self._parameters]
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
                    "format": "card",
                }
            },
        }
        if self.name.ru is not None:
            res["translations"] = {"ru": {self.name.en: self.name.ru}}
        for param in self._parameters:
            merge_json_schema_properties(res["properties"][self._property_name], param.get_schema())
            merge_translations(res, param.get_schema())
        if self.property_order is not None:
            res["properties"][self._property_name]["propertyOrder"] = self.property_order
        return res
