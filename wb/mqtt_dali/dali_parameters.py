import asyncio
from typing import Callable, Optional

from dali.address import GearShort
from dali.command import Command
from dali.gear.general import DTR0

from .settings import (
    NumberSettingsParam,
    SettingsParamBase,
    SettingsParamName,
)
from .utils import add_enum, add_translations, merge_json_schemas
from .wbdali import WBDALIDriver


class NumberGearParam(NumberSettingsParam):
    query_command_class: Optional[Callable[[GearShort], Command]] = None
    set_command_class: Optional[Callable[[GearShort], Command]] = None

    def __init__(self, name: SettingsParamName, property_name: str = "") -> None:
        super().__init__(name, property_name)
        self._is_read_only = self.set_command_class is None

    def get_read_command(self, short_address: int) -> Command:
        if self.query_command_class is not None:
            return self.query_command_class(GearShort(short_address))
        raise RuntimeError(f"Query command class for {self.name.en} is not defined")

    def get_write_commands(self, short_address: int, value_to_set: int) -> list[Command]:
        if self.set_command_class is not None:
            if self.query_command_class is not None:
                return [
                    DTR0(value_to_set),
                    self.set_command_class(GearShort(short_address)),
                ]
            raise RuntimeError(f"Set command class for {self.name.en} is not defined")
        raise RuntimeError(f"Query command class for {self.name.en} is not defined")


class TypeParameters(SettingsParamBase):

    def __init__(self) -> None:
        super().__init__(SettingsParamName("Type parameters"))
        # Must be filled by subclasses
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
        return res

    async def write(self, driver: WBDALIDriver, short_address: int, value: dict) -> dict:
        awaitables = [param.write(driver, short_address, value) for param in self._parameters]
        results = await asyncio.gather(*awaitables, return_exceptions=True)
        res = {}
        for param, result in zip(self._parameters, results):
            if isinstance(result, BaseException):
                raise RuntimeError(f'Error writing "{param.name.en}": {result}') from result
            if result is not None:
                res.update(result)
        return res

    def get_schema(self) -> dict:
        res = {}
        for param in self._parameters:
            merge_json_schemas(res, param.get_schema())
        return res


class DimmingCurveParam(NumberGearParam):

    def __init__(self, property_name: str) -> None:
        super().__init__(SettingsParamName("Dimming curve", "Кривая диммирования"), property_name)

    def get_schema(self) -> dict:
        schema = super().get_schema()
        add_enum(schema["properties"][self.property_name], [(0, "standard"), (1, "linear")])
        add_translations(
            schema,
            "ru",
            {
                "standard": "стандартная",
                "linear": "линейная",
            },
        )
        return schema
