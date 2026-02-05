import asyncio
from typing import Callable, Optional, Union

from dali.address import DeviceShort, GearShort
from dali.command import Command
from dali.gear.general import DTR0

from .settings import InstanceAddress, NumberSettingsParam, SettingsParamName
from .utils import add_enum, add_translations, merge_json_schemas
from .wbdali import WBDALIDriver


class NumberGearParam(NumberSettingsParam):
    query_command_class: Optional[Callable[[GearShort], Command]] = None
    set_command_class: Optional[Callable[[GearShort], Command]] = None

    def __init__(self, name: SettingsParamName, property_name: str = "") -> None:
        super().__init__(name, property_name)
        self._is_read_only = self.set_command_class is None

    def get_read_command(self, address: Union[DeviceShort, GearShort, InstanceAddress]) -> Command:
        if not isinstance(address, GearShort):
            raise ValueError("Address must be a GearShort")
        if self.query_command_class is not None:
            return self.query_command_class(address)
        raise RuntimeError(f"Query command class for {self.name.en} is not defined")

    def get_write_commands(
        self, address: Union[DeviceShort, GearShort, InstanceAddress], value_to_set: int
    ) -> list[Command]:
        if not isinstance(address, GearShort):
            raise ValueError("Address must be a GearShort")
        if self.set_command_class is not None:
            if self.query_command_class is not None:
                return [
                    DTR0(value_to_set),
                    self.set_command_class(address),
                ]
            raise RuntimeError(f"Set command class for {self.name.en} is not defined")
        raise RuntimeError(f"Query command class for {self.name.en} is not defined")


class TypeParameters:

    def __init__(self) -> None:
        # Must be filled by subclasses
        self._parameters = []

    async def read(self, driver: WBDALIDriver, address: GearShort) -> dict:
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
