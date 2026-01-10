import asyncio
from typing import Callable, Optional

from dali.address import GearShort
from dali.command import Command
from dali.gear.general import DTR0, EnableDeviceType

from .utils import merge_json_schemas
from .wbdali import WBDALIDriver, check_query_response, query_request


class GearParam:
    name: str = ""
    property_name: str = ""
    query_command_class: Optional[Callable[[GearShort], Command]] = None
    set_command_class: Optional[Callable[[GearShort], Command]] = None

    def __init__(self) -> None:
        if self.query_command_class is None:
            raise RuntimeError(f"Query command class for {self.name} is not defined")
        self._last_value = None

    async def read(self, driver: WBDALIDriver, address: GearShort) -> dict:
        self._last_value = await query_request(driver, self.query_command_class(address))
        return {self.property_name: self._last_value}

    async def write(self, driver: WBDALIDriver, address: GearShort, value: dict) -> dict:
        if self.set_command_class is None:
            raise RuntimeError(f"Set command class for {self.name} is not defined")
        if self.property_name not in value:
            return {}
        value_to_set = value[self.property_name]
        if self._last_value == value_to_set:
            return {}
        commands: list[Command] = [DTR0(value_to_set)]
        set_command = self.set_command_class(address)
        if set_command.devicetype != 0:
            commands.append(EnableDeviceType(set_command.devicetype))
        commands.append(set_command)
        query_command = self.query_command_class(address)
        if query_command.devicetype != 0:
            commands.append(EnableDeviceType(query_command.devicetype))
        commands.append(query_command)
        res = (await driver.send_commands(commands))[-1]
        check_query_response(res)
        self._last_value = res.raw_value.as_integer
        return {self.property_name: self._last_value}

    async def get_schema(self, driver: WBDALIDriver, address: GearShort) -> dict:
        return {}


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
                raise RuntimeError(f'Error reading "{param.name}": {result}') from result
            if result is not None:
                res.update(result)
        return res

    async def write(self, driver: WBDALIDriver, address: GearShort, value: dict) -> dict:
        if not self._parameters:
            self._parameters = await self.get_parameters(driver, address)
        awaitables = [param.write(driver, address, value) for param in self._parameters]
        results = await asyncio.gather(*awaitables, return_exceptions=True)
        res = {}
        for param, result in zip(self._parameters, results):
            if isinstance(result, BaseException):
                raise RuntimeError(f'Error writing "{param.name}": {result}') from result
            if result is not None:
                res.update(result)
        return res

    async def get_schema(self, driver: WBDALIDriver, address: GearShort) -> dict:
        if not self._parameters:
            self._parameters = await self.get_parameters(driver, address)
        awaitables = [param.get_schema(driver, address) for param in self._parameters]
        results = await asyncio.gather(*awaitables, return_exceptions=True)
        res = {}
        for param, result in zip(self._parameters, results):
            if isinstance(result, BaseException):
                raise RuntimeError(f'Error getting schema for "{param.name}": {result}') from result
            if result is not None:
                merge_json_schemas(res, result)
        return res

    async def get_parameters(self, driver: WBDALIDriver, address: GearShort) -> list:
        return []
