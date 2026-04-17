import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Optional

from dali.address import Address
from dali.command import Command

from .utils import merge_json_schema_properties, merge_translations
from .wbdali_utils import (
    WBDALIDriver,
    is_broadcast_or_group_address,
    query_int,
    query_response,
    query_responses,
    send_with_retry,
)


@dataclass
class SettingsParamName:
    en: str
    ru: Optional[str] = None


class SettingsParamBase:
    requires_mqtt_controls_refresh: bool = False

    def __init__(self, name: SettingsParamName) -> None:
        self.name = name

    async def read(
        self, driver: WBDALIDriver, short_address: Address, logger: Optional[logging.Logger] = None
    ) -> dict:
        del driver, short_address, logger
        return {}

    async def write(
        self,
        driver: WBDALIDriver,
        short_address: Address,
        value: dict,
        logger: Optional[logging.Logger] = None,
    ) -> dict:
        """
        Write extended gear parameters to a DALI device.

        Args:
            driver (WBDALIDriver): The DALI driver instance used to communicate with devices.
            address (Address): The address of the DALI control device to write to or broadcast or group.
            value (dict): A dictionary containing the parameter values to write to the device.

        Returns:
            dict: An empty dictionary if nothing was changed or if short_address is group or broadcast,
            otherwise a dictionary with the updated parameter values.
        """
        del driver, short_address, value, logger
        return {}

    def has_changes(self, new_params: dict) -> bool:
        del new_params
        return False

    def get_schema(self, group_and_broadcast: bool) -> dict:
        del group_and_broadcast
        return {}


class BooleanSettingsParam(SettingsParamBase):  # pylint: disable=too-many-instance-attributes
    def __init__(  # pylint: disable=too-many-arguments, R0917
        self,
        name: SettingsParamName,
        property_name: str,
        query_command_factory: Callable[[Address], Command],
        enable_command_factory: Callable[[Address], Command],
        disable_command_factory: Callable[[Address], Command],
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

    async def read(
        self, driver: WBDALIDriver, short_address: Address, logger: Optional[logging.Logger] = None
    ) -> dict:
        response = await query_response(driver, self._query_factory(short_address), logger)
        if response.value is None:
            raise RuntimeError(f"Failed to read {self.property_name} state")
        self.value = bool(response.value)
        return {self.property_name: self.value}

    async def write(
        self,
        driver: WBDALIDriver,
        short_address: Address,
        value: dict,
        logger: Optional[logging.Logger] = None,
    ) -> dict:
        if self.property_name not in value:
            return {}

        value_to_set = value[self.property_name]
        is_for_single_device = not is_broadcast_or_group_address(short_address)
        if is_for_single_device and self.value == value_to_set:
            return {}

        command_factory = self._enable_factory if bool(value_to_set) else self._disable_factory
        await send_with_retry(driver, command_factory(short_address), logger)
        if not is_for_single_device:
            return {}
        return await self.read(driver, short_address, logger)

    def has_changes(self, new_params: dict) -> bool:
        return self.property_name in new_params and self.value != new_params[self.property_name]

    def get_schema(self, group_and_broadcast: bool) -> dict:
        del group_and_broadcast
        schema: dict = {
            "properties": {
                self.property_name: {
                    "type": "boolean",
                    "title": self.name.en,
                    "format": "switch",
                }
            },
            "required": [self.property_name],
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


class NumberSettingsParam(SettingsParamBase):  # pylint: disable=too-many-instance-attributes
    def __init__(self, name: SettingsParamName, property_name: str) -> None:
        super().__init__(name)
        self.property_name = property_name
        self.minimum = 0
        self.maximum = 255
        self.multiplier = 1
        self.grid_columns: Optional[int] = None
        self.property_order: Optional[int] = None
        self.default: Optional[int] = None
        self.format: Optional[str] = None
        self.value = None
        self._is_read_only = False

    async def read(
        self, driver: WBDALIDriver, short_address: Address, logger: Optional[logging.Logger] = None
    ) -> dict:
        self.value = await query_int(driver, self.get_read_command(short_address), logger) * self.multiplier
        return {self.property_name: self.value}

    async def write(
        self,
        driver: WBDALIDriver,
        short_address: Address,
        value: dict,
        logger: Optional[logging.Logger] = None,
    ) -> dict:
        if self.property_name not in value:
            return {}
        value_to_set = value[self.property_name]
        is_for_single_device = not is_broadcast_or_group_address(short_address)
        if is_for_single_device and self.value == value_to_set:
            return {}
        raw = (int(value_to_set) + self.multiplier // 2) // self.multiplier
        commands = self.get_write_commands(short_address, raw)
        if is_for_single_device:
            commands.append(self.get_read_command(short_address))
        responses = await query_responses(driver, commands, logger)
        if not is_for_single_device:
            return {}
        res = responses[-1]
        self.value = res.raw_value.as_integer * self.multiplier
        return {self.property_name: self.value}

    def has_changes(self, new_params: dict) -> bool:
        return self.property_name in new_params and self.value != new_params[self.property_name]

    def get_schema(self, group_and_broadcast: bool) -> dict:
        if group_and_broadcast and self._is_read_only:
            return {}

        schema: dict = {
            "properties": {
                self.property_name: {
                    "type": "integer",
                    "title": self.name.en,
                    "minimum": self.minimum,
                    "maximum": self.maximum,
                }
            },
            "required": [self.property_name],
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
        if self.format is not None:
            schema["properties"][self.property_name]["format"] = self.format
        if self.default is not None:
            schema["properties"][self.property_name]["default"] = self.default
        if self.property_order is not None:
            schema["properties"][self.property_name]["propertyOrder"] = self.property_order
        if self.multiplier > 1:
            schema["properties"][self.property_name]["multipleOf"] = self.multiplier
        return schema

    def get_write_commands(self, short_address: Address, value_to_set: int) -> list[Command]:
        raise NotImplementedError(f"Write commands for {self.name.en} are not defined")

    def get_read_command(self, short_address: Address) -> Command:
        raise NotImplementedError(f"Read commands for {self.name.en} are not defined")


class SettingsParamGroup(SettingsParamBase):
    def __init__(self, name: SettingsParamName, property_name: str) -> None:
        super().__init__(name)

        self.property_order = None

        self._property_name = property_name
        self._parameters: list[SettingsParamBase] = []

    async def read(
        self, driver: WBDALIDriver, short_address: Address, logger: Optional[logging.Logger] = None
    ) -> dict:
        res = {}
        awaitables = [param.read(driver, short_address, logger) for param in self._parameters]
        results = await asyncio.gather(*awaitables, return_exceptions=True)
        for param, result in zip(self._parameters, results):
            if isinstance(result, BaseException):
                raise RuntimeError(f'Error reading "{param.name.en}": {result}') from result
            if result is not None:
                res.update(result)
        return {self._property_name: res}

    async def write(
        self,
        driver: WBDALIDriver,
        short_address: Address,
        value: dict,
        logger: Optional[logging.Logger] = None,
    ) -> dict:
        if self._property_name not in value:
            return {}
        instance_value = value[self._property_name]
        awaitables = [
            param.write(driver, short_address, instance_value, logger) for param in self._parameters
        ]
        results = await asyncio.gather(*awaitables, return_exceptions=True)
        res = {}
        for param, result in zip(self._parameters, results):
            if isinstance(result, BaseException):
                raise RuntimeError(f'Error writing "{param.name.en}": {result}') from result
            if result is not None:
                res.update(result)
        if is_broadcast_or_group_address(short_address):
            return {}
        return {self._property_name: res}

    def has_changes(self, new_params: dict) -> bool:
        if self._property_name not in new_params:
            return False
        return any(p.has_changes(new_params[self._property_name]) for p in self._parameters)

    def get_schema(self, group_and_broadcast: bool) -> dict:
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
            merge_json_schema_properties(
                res["properties"][self._property_name], param.get_schema(group_and_broadcast)
            )
            merge_translations(res, param.get_schema(group_and_broadcast))
        if self.property_order is not None:
            res["properties"][self._property_name]["propertyOrder"] = self.property_order
        return res
