from typing import Callable, Generator, Optional

from dali import command
from dali.address import GearShort
from dali.gear.general import DTR0

from .utils import merge_json_schemas
from .wbdali import WBDALIDriver, query_request


class GearParam:
    name: str = ""
    property_name: str = ""
    query_command_class: Optional[Callable] = None
    set_command_class: Optional[Callable] = None

    def __init__(self) -> None:
        if self.query_command_class is None:
            raise RuntimeError(f"Query command class for {self.name} is not defined")
        if self.set_command_class is None:
            raise RuntimeError(f"Set command class for {self.name} is not defined")
        self._last_value = None

    async def read(self, driver: WBDALIDriver, addr: GearShort) -> dict:
        self._last_value = await query_request(driver, self.query_command_class(addr))
        return {self.property_name: self._last_value}

    async def write(self, driver: WBDALIDriver, addr: GearShort, value: dict) -> dict:
        if self.property_name not in value:
            return {}
        value_to_set = value[self.property_name]
        if self._last_value == value_to_set:
            return {}

        def set_sequence() -> Generator[command.Command, Optional[command.Response], None]:
            rsp = yield DTR0(value_to_set)
            if rsp is not None:
                raise RuntimeError(f"Failed to write DTR0 got unexpected response {rsp}")

            rsp = yield self.set_command_class(addr)
            if rsp is not None:
                raise RuntimeError(f"Got unexpected response {rsp}")

        await driver.run_sequence(set_sequence())
        await self.read(driver, addr)
        return {self.property_name: self._last_value}

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
                raise RuntimeError(f'Error reading "{param.name}": {e}') from e
        return res

    async def write(self, driver: WBDALIDriver, address: GearShort, value: dict) -> dict:
        res = {}
        for param in self._parameters:
            try:
                updated_value = await param.write(driver, address, value)
                if updated_value is not None:
                    res.update(updated_value)
            except Exception as e:
                raise RuntimeError(f'Error writing "{param.name}": {e}') from e
        return res

    async def get_schema(self, driver: WBDALIDriver, addr: GearShort) -> dict:
        res = {}
        for param in self._parameters:
            try:
                schema = await param.get_schema(driver, addr)
            except Exception as e:
                raise RuntimeError(f'Error getting schema for "{param.name}": {e}') from e
            if schema:
                merge_json_schemas(res, schema)
        return res

    async def get_parameters(self, driver: WBDALIDriver, addr: GearShort) -> list:
        return []
