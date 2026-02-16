from typing import Optional

from dali.address import DeviceShort, InstanceNumber
from dali.command import Command
from dali.device import light, occupancy, pushbutton
from dali.device.general import (
    DTR0,
    DisableApplicationController,
    DisableInstance,
    DisablePowerCycleNotification,
    EnableApplicationController,
    EnableInstance,
    EnablePowerCycleNotification,
    QueryApplicationControlEnabled,
    QueryEventPriority,
    QueryEventScheme,
    QueryInstanceEnabled,
    QueryPowerCycleNotification,
    SetEventPriority,
    SetEventScheme,
)

from .common_dali_device import DaliDeviceBase
from .dali2_compat import Dali2CommandsCompatibilityLayer
from .dali2_type1_parameters import build_type1_push_button_parameters
from .dali2_type3_parameters import build_type3_occupancy_sensor_parameters
from .dali2_type4_parameters import build_type4_light_sensor_parameters
from .dali_device import DaliDeviceAddress
from .gtin_db import DaliDatabase
from .settings import (
    BooleanSettingsParam,
    NumberSettingsParam,
    SettingsParamBase,
    SettingsParamGroup,
    SettingsParamName,
)
from .wbdali import WBDALIDriver


class ApplicationActiveParam(BooleanSettingsParam):
    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Application controller"),
            "application_active",
            lambda short_address: QueryApplicationControlEnabled(DeviceShort(short_address)),
            lambda short_address: EnableApplicationController(DeviceShort(short_address)),
            lambda short_address: DisableApplicationController(DeviceShort(short_address)),
        )


class PowerCycleNotificationParam(BooleanSettingsParam):
    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Power cycle notification"),
            "power_cycle_notification",
            lambda short_address: QueryPowerCycleNotification(DeviceShort(short_address)),
            lambda short_address: EnablePowerCycleNotification(DeviceShort(short_address)),
            lambda short_address: DisablePowerCycleNotification(DeviceShort(short_address)),
        )


class InstanceParameters(SettingsParamGroup):
    def __init__(self, instance_number: InstanceNumber, instance_type: int) -> None:
        super().__init__(
            SettingsParamName(f"Instance {instance_number.value}"), f"instance{instance_number.value}"
        )
        self._parameters = [
            InstanceActiveParam(instance_number),
            InstanceTypeParam(instance_type),
            EventPriorityParam(instance_number),
            EventSchemeParam(instance_number),
        ]
        if instance_type == pushbutton.instance_type:
            self._parameters.extend(build_type1_push_button_parameters(instance_number))
        elif instance_type == occupancy.instance_type:
            self._parameters.extend(build_type3_occupancy_sensor_parameters(instance_number))
        elif instance_type == light.instance_type:
            self._parameters.extend(build_type4_light_sensor_parameters(instance_number))
        self.instance_number = instance_number
        self.instance_type = instance_type


class EventSchemeParam(NumberSettingsParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(SettingsParamName("Event addressing scheme"), "event_scheme")
        self._instance_number = instance_number

    def get_write_commands(self, short_address: int, value_to_set: int) -> list[Command]:
        return [
            DTR0(value_to_set),
            SetEventScheme(DeviceShort(short_address), self._instance_number),
        ]

    def get_read_command(self, short_address: int) -> Command:
        return QueryEventScheme(DeviceShort(short_address), self._instance_number)

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


class EventPriorityParam(NumberSettingsParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(SettingsParamName("Event priority"), "event_priority")
        self._instance_number = instance_number

    def get_write_commands(self, short_address: int, value_to_set: int) -> list[Command]:
        return [
            DTR0(value_to_set),
            SetEventPriority(DeviceShort(short_address), self._instance_number),
        ]

    def get_read_command(self, short_address: int) -> Command:
        return QueryEventPriority(DeviceShort(short_address), self._instance_number)

    def get_schema(self) -> dict:
        schema = super().get_schema()
        schema["properties"][self.property_name]["enum"] = [2, 3, 4, 5]
        if "options" not in schema["properties"][self.property_name]:
            schema["properties"][self.property_name]["options"] = {}
        return schema


class InstanceActiveParam(BooleanSettingsParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Enable Event Messages"),
            "instance_active",
            lambda short_address, inst=instance_number: QueryInstanceEnabled(
                DeviceShort(short_address), inst
            ),
            lambda short_address, inst=instance_number: EnableInstance(DeviceShort(short_address), inst),
            lambda short_address, inst=instance_number: DisableInstance(DeviceShort(short_address), inst),
        )


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

    async def read(self, driver: WBDALIDriver, short_address: int) -> dict:
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


class Dali2Device(DaliDeviceBase):
    def __init__(
        self,
        address: DaliDeviceAddress,
        bus_id: str,
        gtin_db: DaliDatabase,
        mqtt_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> None:
        super().__init__(
            address, bus_id, "DALI 2.0", "dali2_", Dali2CommandsCompatibilityLayer(), gtin_db, mqtt_id, name
        )
        self.instances: dict[int, InstanceParameters] = {}
        self._gtin_db = gtin_db

    async def _get_parameter_handlers(self, driver: WBDALIDriver) -> list[SettingsParamBase]:
        handlers: list[SettingsParamBase] = [
            ApplicationActiveParam(),
            PowerCycleNotificationParam(),
        ]
        handlers.extend(self.instances.values())
        return handlers

    def add_instance(self, index: int, instance_type: int) -> None:
        self.instances[index] = InstanceParameters(InstanceNumber(index), instance_type)
