from typing import Callable, Generator, Optional

from dali import command
from dali.address import GearShort
from dali.gear.general import DTR0

from .utils import merge_json_schemas
from .wbdali import WBDALIDriver, send_extended_command


class GearParam:
    name: str = ""
    property_name: str = ""
    query_command_class: Optional[Callable] = None
    set_command_class: Optional[Callable] = None

    async def read(self, driver: WBDALIDriver, addr: GearShort):
        if self.query_command_class is None:
            return None
        value = await send_extended_command(driver, self.query_command_class(addr))
        if value is None:
            return None
        return {self.property_name: value.raw_value.as_integer}

    async def write(self, driver: WBDALIDriver, addr: GearShort, value: dict) -> dict:
        if (
            self.property_name not in value
            or self.query_command_class is None
            or self.set_command_class is None
        ):
            return {}
        value_to_set = value[self.property_name]

        def set_sequence() -> (
            Generator[command.Command, Optional[command.Response], Optional[command.Response]]
        ):
            rsp = yield DTR0(value_to_set)
            if rsp is not None:
                return None

            rsp = yield self.set_command_class(addr)
            if rsp is not None:
                return None

            rsp = yield self.query_command_class(addr)
            return rsp

        value_after_write = await driver.run_sequence(set_sequence())
        if value_after_write is None:
            return {}
        return {self.property_name: value_after_write.raw_value.as_integer}

    async def get_schema(self, driver: WBDALIDriver, addr: GearShort) -> dict:
        return {}


class TypeParameters:

    def __init__(self) -> None:
        self._parameters = []

    async def read(self, driver: WBDALIDriver, addr: GearShort) -> dict:
        self._parameters = await self.get_parameters(driver, addr)
        res = {}
        for param in self._parameters:
            try:
                value = await param.read(driver, addr)
                if value is not None:
                    res.update(value)
            except Exception as e:
                raise RuntimeError(f'Error reading "{param.name}"') from e
        return res

    async def write(self, driver: WBDALIDriver, address: GearShort, value: dict) -> dict:
        res = {}
        for param in self._parameters:
            try:
                updated_value = await param.write(driver, address, value)
                if updated_value is not None:
                    res.update(updated_value)
            except Exception as e:
                raise RuntimeError(f'Error writing "{param.name}"') from e
        return res

    async def get_schema(self, driver: WBDALIDriver, addr: GearShort) -> dict:
        res = {}
        for param in self._parameters:
            schema = await param.get_schema(driver, addr)
            if schema:
                merge_json_schemas(res, schema)
        return res

    async def get_parameters(self, driver: WBDALIDriver, addr: GearShort) -> list:
        return []
