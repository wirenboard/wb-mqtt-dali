from typing import Optional, Sequence, Type

from dali.address import DeviceShort, InstanceNumber
from dali.command import Command
from dali.device import light, occupancy, pushbutton
from dali.device.general import (
    DTR0,
    DTR1,
    DTR2,
    AddToDeviceGroupsSixteenToThirtyOne,
    AddToDeviceGroupsZeroToFifteen,
    DisableApplicationController,
    DisableInstance,
    DisablePowerCycleNotification,
    EnableApplicationController,
    EnableInstance,
    EnablePowerCycleNotification,
    QueryApplicationControlEnabled,
    QueryDeviceGroupsEightToFifteen,
    QueryDeviceGroupsSixteenToTwentyThree,
    QueryDeviceGroupsTwentyFourToThirtyOne,
    QueryDeviceGroupsZeroToSeven,
    QueryEventPriority,
    QueryEventScheme,
    QueryInstanceEnabled,
    QueryInstanceGroup1,
    QueryInstanceGroup2,
    QueryPowerCycleNotification,
    QueryPrimaryInstanceGroup,
    RemoveFromDeviceGroupsSixteenToThirtyOne,
    RemoveFromDeviceGroupsZeroToFifteen,
    SetEventPriority,
    SetEventScheme,
    SetInstanceGroup1,
    SetInstanceGroup2,
    SetPrimaryInstanceGroup,
)

from .common_dali_device import DaliDeviceBase, MqttControl
from .dali2_compat import Dali2CommandsCompatibilityLayer
from .dali2_controls import (
    get_button_controls,
    get_light_controls,
    get_occupancy_controls,
)
from .dali2_type1_parameters import build_type1_push_button_parameters
from .dali2_type3_parameters import build_type3_occupancy_sensor_parameters
from .dali2_type4_parameters import build_type4_light_sensor_parameters
from .dali_device import DaliDeviceAddress
from .gtin_db import DaliDatabase
from .settings import (
    BooleanSettingsParam,
    CommandWriteItem,
    DelayHint,
    NumberSettingsParam,
    SettingsParamBase,
    SettingsParamGroup,
    SettingsParamName,
)
from .wbdali_utils import WBDALIDriver, check_query_response


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
            InstanceGroup0Param(instance_number),
            InstanceGroup1Param(instance_number),
            InstanceGroup2Param(instance_number),
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

    def get_write_commands(self, short_address: int, value_to_set: int) -> Sequence[CommandWriteItem]:
        return [
            DTR0(value_to_set),
            DelayHint(0.3),
            SetEventScheme(DeviceShort(short_address), self._instance_number),
            DelayHint(0.3),
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

    def get_write_commands(self, short_address: int, value_to_set: int) -> Sequence[CommandWriteItem]:
        return [
            DTR0(value_to_set),
            DelayHint(0.3),
            SetEventPriority(DeviceShort(short_address), self._instance_number),
            DelayHint(0.3),
        ]

    def get_read_command(self, short_address: int) -> Command:
        return QueryEventPriority(DeviceShort(short_address), self._instance_number)

    def get_schema(self) -> dict:
        schema = super().get_schema()
        schema["properties"][self.property_name]["enum"] = [2, 3, 4, 5]
        if "options" not in schema["properties"][self.property_name]:
            schema["properties"][self.property_name]["options"] = {}
        return schema


class InstanceGroupParamBase(NumberSettingsParam):
    def __init__(self, name: str, property_name: str, instance_number: InstanceNumber) -> None:
        super().__init__(SettingsParamName(name), property_name)
        self._instance_number = instance_number

    def get_schema(self) -> dict:
        schema = super().get_schema()
        schema["properties"][self.property_name]["enum"] = list(range(32)) + [255]
        return schema


class InstanceGroup0Param(InstanceGroupParamBase):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__("Instance group 0", "instance_group_0", instance_number)

    def get_write_commands(self, short_address: int, value_to_set: int) -> Sequence[CommandWriteItem]:
        return [
            DTR0(value_to_set),
            DelayHint(0.3),
            SetPrimaryInstanceGroup(DeviceShort(short_address), self._instance_number),
            DelayHint(0.3),
        ]

    def get_read_command(self, short_address: int) -> Command:
        return QueryPrimaryInstanceGroup(DeviceShort(short_address), self._instance_number)


class InstanceGroup1Param(InstanceGroupParamBase):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__("Instance group 1", "instance_group_1", instance_number)

    def get_write_commands(self, short_address: int, value_to_set: int) -> Sequence[CommandWriteItem]:
        return [
            DTR0(value_to_set),
            DelayHint(0.3),
            SetInstanceGroup1(DeviceShort(short_address), self._instance_number),
            DelayHint(0.3),
        ]

    def get_read_command(self, short_address: int) -> Command:
        return QueryInstanceGroup1(DeviceShort(short_address), self._instance_number)


class InstanceGroup2Param(InstanceGroupParamBase):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__("Instance group 2", "instance_group_2", instance_number)

    def get_write_commands(self, short_address: int, value_to_set: int) -> Sequence[CommandWriteItem]:
        return [
            DTR0(value_to_set),
            DelayHint(0.3),
            SetInstanceGroup2(DeviceShort(short_address), self._instance_number),
            DelayHint(0.3),
        ]

    def get_read_command(self, short_address: int) -> Command:
        return QueryInstanceGroup2(DeviceShort(short_address), self._instance_number)


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


class DeviceGroupsParam(SettingsParamBase):
    TOTAL_GROUPS = 32
    HALF_RANGE = 16

    def __init__(self) -> None:
        super().__init__(SettingsParamName("Device groups"))
        self.property_name = "device_groups"
        self._groups = [False] * self.TOTAL_GROUPS

    async def read(self, driver: WBDALIDriver, short_address: int) -> dict:
        updated_groups = await self._query_all_groups(driver, short_address)
        self._groups = updated_groups
        return {self.property_name: updated_groups}

    async def write(self, driver: WBDALIDriver, short_address: int, value: dict) -> dict:
        groups_to_set = value.get(self.property_name)
        if groups_to_set is None:
            return {}
        desired_groups = [bool(item) for item in groups_to_set]
        if len(desired_groups) != self.TOTAL_GROUPS:
            raise ValueError(f"{self.property_name} must contain {self.TOTAL_GROUPS} items")
        if desired_groups == self._groups:
            return {}

        address = DeviceShort(short_address)
        current_lower = self._mask_for_slice(self._groups[: self.HALF_RANGE])
        current_upper = self._mask_for_slice(self._groups[self.HALF_RANGE :])
        desired_lower = self._mask_for_slice(desired_groups[: self.HALF_RANGE])
        desired_upper = self._mask_for_slice(desired_groups[self.HALF_RANGE :])

        remove_lower = current_lower & ~desired_lower
        remove_upper = current_upper & ~desired_upper
        add_lower = desired_lower & ~current_lower
        add_upper = desired_upper & ~current_upper

        commands: list[Command] = []
        if remove_lower:
            commands.extend(
                self._build_group_command_sequence(address, remove_lower, RemoveFromDeviceGroupsZeroToFifteen)
            )
        if remove_upper:
            commands.extend(
                self._build_group_command_sequence(
                    address, remove_upper, RemoveFromDeviceGroupsSixteenToThirtyOne
                )
            )
        if add_lower:
            commands.extend(
                self._build_group_command_sequence(address, add_lower, AddToDeviceGroupsZeroToFifteen)
            )
        if add_upper:
            commands.extend(
                self._build_group_command_sequence(address, add_upper, AddToDeviceGroupsSixteenToThirtyOne)
            )
        if not commands:
            return {}

        query_commands = self._build_query_commands(address)
        responses = await driver.send_commands(commands + query_commands)
        updated_groups = self._parse_group_responses(responses[-len(query_commands) :])
        self._groups = updated_groups
        return {self.property_name: updated_groups}

    def get_schema(self) -> dict:
        return {
            "properties": {
                self.property_name: {
                    "type": "array",
                    "title": self.name.en,
                    "items": {"type": "boolean", "format": "button"},
                    "minItems": self.TOTAL_GROUPS,
                    "maxItems": self.TOTAL_GROUPS,
                }
            }
        }

    async def _query_all_groups(self, driver: WBDALIDriver, short_address: int) -> list[bool]:
        responses = await driver.send_commands(self._build_query_commands(DeviceShort(short_address)))
        return self._parse_group_responses(responses)

    def _build_query_commands(self, address: DeviceShort) -> list[Command]:
        return [
            QueryDeviceGroupsZeroToSeven(address),
            QueryDeviceGroupsEightToFifteen(address),
            QueryDeviceGroupsSixteenToTwentyThree(address),
            QueryDeviceGroupsTwentyFourToThirtyOne(address),
        ]

    def _parse_group_responses(self, responses: list) -> list[bool]:
        groups: list[bool] = []
        for response in responses:
            check_query_response(response)
            raw_value = response.raw_value.as_integer
            groups.extend(((raw_value >> bit) & 1) == 1 for bit in range(8))
        return groups[: self.TOTAL_GROUPS]

    def _build_group_command_sequence(
        self,
        address: DeviceShort,
        mask: int,
        command_factory: Type[Command],
    ) -> list[Command]:
        return [
            DTR1(mask & 0xFF),
            DTR2((mask >> 8) & 0xFF),
            command_factory(address),
        ]

    @staticmethod
    def _mask_for_slice(values: list[bool]) -> int:
        mask = 0
        for index, enabled in enumerate(values):
            if enabled:
                mask |= 1 << index
        return mask


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

    def add_instance(self, index: int, instance_type: int) -> None:
        self.instances[index] = InstanceParameters(InstanceNumber(index), instance_type)

    async def _get_parameter_handlers(self, driver: WBDALIDriver) -> list[SettingsParamBase]:
        handlers: list[SettingsParamBase] = [
            DeviceGroupsParam(),
            PowerCycleNotificationParam(),
        ]
        handlers.extend(self.instances.values())
        return handlers

    async def _get_mqtt_controls(self, driver: WBDALIDriver) -> list[MqttControl]:
        return_controls: list[MqttControl] = []
        for instance in self.instances.values():
            if instance.instance_type == occupancy.instance_type:
                return_controls.extend(get_occupancy_controls(instance.instance_number.value))
            elif instance.instance_type == light.instance_type:
                return_controls.extend(get_light_controls(instance.instance_number.value))
            elif instance.instance_type == pushbutton.instance_type:
                return_controls.extend(get_button_controls(instance.instance_number.value))
        return return_controls
