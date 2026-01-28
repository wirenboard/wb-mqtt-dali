import asyncio
from dataclasses import dataclass
from typing import Callable, Optional

from dali.address import GearShort
from dali.command import Command
from dali.gear.general import DTR0

from .utils import merge_json_schemas
from .wbdali import WBDALIDriver, check_query_response, query_request


@dataclass
class GearParamName:
    en: str
    ru: Optional[str] = None


class GearParamBase:
    def __init__(self, name: GearParamName) -> None:
        self.name = name

    async def read(self, driver: WBDALIDriver, address: GearShort) -> dict:
        return {}

    async def write(self, driver: WBDALIDriver, address: GearShort, value: dict) -> dict:
        """
        Write extended gear parameters to a DALI device.

        Args:
            driver (WBDALIDriver): The DALI driver instance used to communicate with devices.
            address (GearShort): The short address of the DALI gear device to write to.
            value (dict): A dictionary containing the parameter values to write to the device.

        Returns:
            dict: An empty dictionary if nothing was changed, or a dictionary with the updated parameter values.
        """
        return {}

    async def get_schema(self, driver: WBDALIDriver, address: GearShort) -> dict:
        return {}


class NumberGearParam(GearParamBase):
    query_command_class: Optional[Callable[[GearShort], Command]] = None
    set_command_class: Optional[Callable[[GearShort], Command]] = None

    def __init__(self, name: GearParamName, property_name: str = "") -> None:
        super().__init__(name)
        self.property_name = property_name
        self.minimum = 0
        self.maximum = 255
        self.grid_columns = None
        self.property_order = None
        self.default: Optional[int] = None
        if self.query_command_class is None:
            raise RuntimeError(f"Query command class for {self.name.en} is not defined")
        self._last_value = None

    async def read(self, driver: WBDALIDriver, address: GearShort) -> dict:
        self._last_value = await query_request(driver, self.query_command_class(address))
        return {self.property_name: self._last_value}

    async def write(self, driver: WBDALIDriver, address: GearShort, value: dict) -> dict:
        if self.set_command_class is None:
            raise RuntimeError(f"Set command class for {self.name.en} is not defined")
        if self.query_command_class is None:
            raise RuntimeError(f"Query command class for {self.name.en} is not defined")
        if self.property_name not in value:
            return {}
        value_to_set = value[self.property_name]
        if self._last_value == value_to_set:
            return {}
        commands = [
            DTR0(value_to_set),
            self.set_command_class(address),
            self.query_command_class(address),
        ]
        res = (await driver.send_commands(commands))[-1]
        check_query_response(res)
        self._last_value = res.raw_value.as_integer
        return {self.property_name: self._last_value}

    async def get_schema(self, driver: WBDALIDriver, address: GearShort) -> dict:
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
        if self.set_command_class is None:
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


class TypeParameters:

    def __init__(self) -> None:
        self._parameters = []

    async def read(self, driver: WBDALIDriver, address: GearShort) -> dict:
        self._parameters = await self.get_parameters(driver, address)
        res = {}
        awaitables = [param.read(driver, address) for param in self._parameters]
        results = await asyncio.gather(*awaitables, return_exceptions=True)
        for param, result in zip(self._parameters, results):
            if isinstance(result, BaseException):
                raise RuntimeError(f'Error reading "{param.name.en}": {result}') from result
            if result is not None:
                res.update(result)
        return res

    async def write(self, driver: WBDALIDriver, address: GearShort, value: dict) -> dict:
        awaitables = [param.write(driver, address, value) for param in self._parameters]
        results = await asyncio.gather(*awaitables, return_exceptions=True)
        res = {}
        for param, result in zip(self._parameters, results):
            if isinstance(result, BaseException):
                raise RuntimeError(f'Error writing "{param.name.en}": {result}') from result
            if result is not None:
                res.update(result)
        return res

    async def get_schema(self, driver: WBDALIDriver, address: GearShort) -> dict:
        awaitables = [param.get_schema(driver, address) for param in self._parameters]
        results = await asyncio.gather(*awaitables, return_exceptions=True)
        res = {}
        for param, result in zip(self._parameters, results):
            if isinstance(result, BaseException):
                raise RuntimeError(f'Error getting schema for "{param.name.en}": {result}') from result
            if result is not None:
                merge_json_schemas(res, result)
        return res

    async def get_parameters(self, driver: WBDALIDriver, address: GearShort) -> list[GearParamBase]:
        return []
