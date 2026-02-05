import asyncio
from typing import Optional

import jsonschema
from dali.address import DeviceShort, InstanceNumber
from dali.command import Command
from dali.device.general import DTR0, QueryEventScheme, SetEventScheme

from .dali_device import DaliDeviceAddress
from .settings import (
    InstanceAddress,
    NumberSettingsParam,
    SettingsParamAddress,
    SettingsParamBase,
    SettingsParamGroup,
    SettingsParamName,
)
from .utils import merge_json_schemas
from .wbdali import WBDALIDriver


class InstanceParameters(SettingsParamGroup):
    def __init__(self, instance_number: InstanceNumber, instance_type: int) -> None:
        super().__init__(
            SettingsParamName(f"Instance {instance_number.value}"), f"instance{instance_number.value}"
        )
        self._parameters = [
            InstanceTypeParam(instance_type),
            EventSchemeParam(),
        ]
        self.instance_number = instance_number
        self.instance_type = instance_type


class EventSchemeParam(NumberSettingsParam):

    def __init__(self) -> None:
        super().__init__(SettingsParamName("Event addressing scheme"), "event_scheme")

    def get_write_commands(self, address: SettingsParamAddress, value_to_set: int) -> list[Command]:
        if not isinstance(address, InstanceAddress):
            raise ValueError("Address must be an InstanceAddress")
        return [
            DTR0(value_to_set),
            SetEventScheme(address.device_short, address.instance_number),
        ]

    def get_read_command(self, address: SettingsParamAddress) -> Command:
        if not isinstance(address, InstanceAddress):
            raise ValueError("Address must be an InstanceAddress")
        return QueryEventScheme(address.device_short, address.instance_number)

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


class InstanceTypeParam(SettingsParamBase):
    INSTANCE_TYPE_NAMES = {
        0: "Generic (0)",
        1: "Push button (1)",
        2: "Absolute input device (2)",
        3: "Occupancy sensor (3)",
        4: "Light sensor (4)",
        6: "General purpose sensor (6)",
    }

    def __init__(self, instance_type: int) -> None:
        super().__init__(SettingsParamName("Instance type"))
        self.instance_type_name = self.INSTANCE_TYPE_NAMES.get(instance_type, f"Unknown ({instance_type})")
        self.property_name = "instance_type"

    async def read(self, driver: WBDALIDriver, address: SettingsParamAddress) -> dict:
        return {self.property_name: self.instance_type_name}

    def get_schema(self) -> dict:
        return {
            "properties": {
                self.property_name: {
                    "type": "string",
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
            instance.read(driver, InstanceAddress(DeviceShort(self.address.short), instance.instance_number))
            for instance in self.instances.values()
        ]
        results = await asyncio.gather(*awaitables, return_exceptions=True)
        for instance, result in zip(self.instances.values(), results):
            if isinstance(result, BaseException):
                raise RuntimeError(f"Error reading parameters for {instance.name.en}: {result}") from result
            if result is not None:
                res.update(result)
        self.params = res
        new_schema = {}
        for instance in self.instances.values():
            merge_json_schemas(new_schema, instance.get_schema())
        self.schema = new_schema

    async def apply_parameters(self, driver: WBDALIDriver, new_values: dict) -> None:
        if not self.params:
            await self.load_info(driver)
        jsonschema.validate(
            instance=new_values, schema=self.schema, format_checker=jsonschema.draft4_format_checker
        )
        short_addr = DeviceShort(self.address.short)
        updated_parameters = {}
        for instance in self.instances.values():
            updated_parameters.update(
                await instance.write(
                    driver, InstanceAddress(short_addr, instance.instance_number), new_values
                )
            )
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
