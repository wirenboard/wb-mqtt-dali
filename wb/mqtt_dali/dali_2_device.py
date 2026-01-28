import asyncio
from typing import Callable, Optional

import jsonschema
from dali.address import DeviceShort, InstanceNumber
from dali.command import Command
from dali.device.general import DTR0, QueryEventScheme, SetEventScheme

from .dali_device import DaliDeviceAddress
from .extended_gear_parameters import GearParamName
from .utils import merge_json_schema_properties, merge_json_schemas, merge_translations
from .wbdali import WBDALIDriver, check_query_response, query_request


class InstanceParamBase:
    def __init__(self, name: GearParamName) -> None:
        self.name = name

    async def read(self, driver: WBDALIDriver, address: DeviceShort, instance_number: InstanceNumber) -> dict:
        return {}

    async def write(
        self, driver: WBDALIDriver, address: DeviceShort, instance_number: InstanceNumber, value: dict
    ) -> dict:
        """
        Write extended gear parameters to a DALI device.

        Args:
            driver (WBDALIDriver): The DALI driver instance used to communicate with devices.
            address (DeviceShort): The address of the DALI control device to write to.
            instance_number (InstanceNumber): The instance number to write to.
            value (dict): A dictionary containing the parameter values to write to the device.

        Returns:
            dict: An empty dictionary if nothing was changed, or a dictionary with the updated parameter values.
        """
        return {}

    def get_schema(self) -> dict:
        return {}


class NumberInstanceParam(InstanceParamBase):
    query_command_class: Optional[Callable[[DeviceShort, InstanceNumber], Command]] = None
    set_command_class: Optional[Callable[[DeviceShort, InstanceNumber], Command]] = None

    def __init__(self, name: GearParamName, property_name: str) -> None:
        super().__init__(name)
        self.property_name = property_name
        self.minimum = 0
        self.maximum = 255
        self.grid_columns = None
        self.property_order = None
        self.default: Optional[int] = None
        self._last_value = None

    async def read(self, driver: WBDALIDriver, address: DeviceShort, instance_number: InstanceNumber) -> dict:
        if self.query_command_class is not None:
            self._last_value = await query_request(driver, self.query_command_class(address, instance_number))
            return {self.property_name: self._last_value}
        raise RuntimeError(f"Query command class for {self.name.en} is not defined")

    async def write(
        self, driver: WBDALIDriver, address: DeviceShort, instance_number: InstanceNumber, value: dict
    ) -> dict:
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
            self.set_command_class(address, instance_number),
            self.query_command_class(address, instance_number),
        ]
        res = (await driver.send_commands(commands))[-1]
        check_query_response(res)
        self._last_value = res.raw_value.as_integer
        return {self.property_name: self._last_value}

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
        if self.set_command_class is None:
            schema["properties"][self.property_name]["options"] = {"wb": {"readOnly": True}}
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


class InstanceParameters:
    def __init__(self, instance_number: InstanceNumber, instance_type: int) -> None:
        self.instance_number = instance_number
        self.instance_type = instance_type
        self._property_name = f"instance{self.instance_number.value}"
        self._parameters = [
            InstanceTypeParam(self.instance_type),
            EventSchemeParam(),
        ]

    async def read(self, driver: WBDALIDriver, address: DeviceShort) -> dict:
        res = {}
        awaitables = [param.read(driver, address, self.instance_number) for param in self._parameters]
        results = await asyncio.gather(*awaitables, return_exceptions=True)
        for param, result in zip(self._parameters, results):
            if isinstance(result, BaseException):
                raise RuntimeError(f'Error reading "{param.name.en}": {result}') from result
            if result is not None:
                res.update(result)
        return {self._property_name: res}

    async def write(self, driver: WBDALIDriver, address: DeviceShort, value: dict) -> dict:
        if self._property_name not in value:
            return {}
        instance_value = value[self._property_name]
        awaitables = [
            param.write(driver, address, self.instance_number, instance_value) for param in self._parameters
        ]
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
                    "properties": {},
                }
            }
        }
        for param in self._parameters:
            merge_json_schema_properties(res["properties"][self._property_name], param.get_schema())
            merge_translations(res, param.get_schema())
        return res


class EventSchemeParam(NumberInstanceParam):
    query_command_class = QueryEventScheme
    set_command_class = SetEventScheme

    def __init__(self) -> None:
        super().__init__(GearParamName("Event addressing scheme"), "event_scheme")

    def get_schema(self) -> dict:
        schema = super().get_schema()
        schema["properties"][self.property_name]["enum"] = [0, 1, 2, 3, 4]
        if "options" not in schema["properties"][self.property_name]:
            schema["properties"][self.property_name]["options"] = {}
        schema["properties"][self.property_name]["options"]["enum_titles"] = [
            "instance type and number",
            "device short and instance type",
            "device short and instance number",
            "device group and instance type",
            "instance group and type",
        ]
        return schema


class InstanceTypeParam(InstanceParamBase):
    def __init__(self, instance_type: int) -> None:
        super().__init__(GearParamName("Instance type"))
        self.instance_type = instance_type
        self.property_name = "instance_type"

    async def read(self, driver: WBDALIDriver, address: DeviceShort, instance_number: InstanceNumber) -> dict:
        return {self.property_name: self.instance_type}

    def get_schema(self) -> dict:
        return {
            "properties": {
                self.property_name: {
                    "type": "integer",
                    "title": self.name.en,
                    "options": {
                        "wb": {"read_only": True},
                    },
                    "propertyOrder": 0,
                }
            },
        }


class Dali2Device:

    def __init__(self, uid: str, name: str, address: DaliDeviceAddress) -> None:
        self.uid = uid
        self.name = name
        self.address = address
        self.params: Optional[dict] = None
        self.schema: dict = {}
        self.instances: dict[int, InstanceParameters] = {}

    async def load_info(self, driver: WBDALIDriver, force_reload: bool = False) -> None:
        if self.params and not force_reload:
            return
        res = {}
        awaitables = [
            instance.read(driver, DeviceShort(self.address.short)) for instance in self.instances.values()
        ]
        results = await asyncio.gather(*awaitables, return_exceptions=True)
        for instance, result in zip(self.instances.values(), results):
            if isinstance(result, BaseException):
                raise RuntimeError(
                    f"Error reading parameters for instance {instance.instance_number.value}: {result}"
                ) from result
            if result is not None:
                res.update(result)
        self.params = res

    async def apply_parameters(self, driver: WBDALIDriver, new_values: dict) -> None:
        if not self.params:
            await self.load_info(driver)
        jsonschema.validate(
            instance=new_values, schema=self.schema, format_checker=jsonschema.draft4_format_checker
        )
        short_addr = DeviceShort(self.address.short)
        updated_parameters = {}
        for instance in self.instances.values():
            updated_parameters.update(await instance.write(driver, short_addr, new_values))
        self.params.update(updated_parameters)

    def add_instance(self, index: int, instance_type: int) -> None:
        instance_parameters = InstanceParameters(InstanceNumber(index), instance_type)
        self.instances[index] = instance_parameters
        merge_json_schemas(self.schema, instance_parameters.get_schema())


def make_dali2_device(bus_uid: str, address: DaliDeviceAddress) -> Dali2Device:
    return Dali2Device(
        uid=f"{bus_uid}_dali2_{address.short}",
        name=f"DALI-2 {address.short}:{address.random:#x}",
        address=address,
    )
