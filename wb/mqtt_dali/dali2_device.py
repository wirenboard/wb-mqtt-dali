from typing import Optional, Type

from dali.address import Address, DeviceShort, InstanceNumber
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
    QueryInstanceType,
    QueryNumberOfInstances,
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
from dali.device.helpers import check_bad_rsp

from .common_dali_device import DaliDeviceBase, MqttControlBase
from .dali2_compat import Dali2CommandsCompatibilityLayer
from .dali2_controls import (
    get_absolute_input_device_controls,
    get_button_controls,
    get_feedback_controls,
    get_general_purpose_sensor_controls,
    get_light_controls,
    get_occupancy_controls,
)
from .dali2_type1_parameters import build_type1_push_button_parameters
from .dali2_type2_parameters import build_type2_absolute_input_device_parameters
from .dali2_type3_parameters import build_type3_occupancy_sensor_parameters
from .dali2_type4_parameters import build_type4_light_sensor_parameters
from .dali2_type6_parameters import build_type6_general_purpose_sensor_parameters
from .dali2_type32_parameters import build_type32_feedback_parameters
from .dali_device import DaliDeviceAddress
from .device import absolute_input_device, feedback, general_purpose_sensor
from .gtin_db import DaliDatabase
from .settings import (
    BooleanSettingsParam,
    NumberSettingsParam,
    SettingsParamBase,
    SettingsParamGroup,
    SettingsParamName,
)
from .wbdali_utils import WBDALIDriver, check_query_response


class ApplicationActiveParam(BooleanSettingsParam):
    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Application controller", "Контроллер приложения"),
            "application_active",
            QueryApplicationControlEnabled,
            EnableApplicationController,
            DisableApplicationController,
        )


class PowerCycleNotificationParam(BooleanSettingsParam):
    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Power cycle notification", "Уведомление о цикле питания"),
            "power_cycle_notification",
            QueryPowerCycleNotification,
            EnablePowerCycleNotification,
            DisablePowerCycleNotification,
        )


class InstanceParameters(SettingsParamGroup):
    def __init__(self, instance_number: InstanceNumber, instance_type: int) -> None:
        super().__init__(
            SettingsParamName(
                f"Instance {instance_number.value}",
                f"Экземпляр {instance_number.value}",
            ),
            f"instance{instance_number.value}",
        )
        self.property_order = instance_number.value + 100
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
        elif instance_type == absolute_input_device.instance_type:
            self._parameters.extend(build_type2_absolute_input_device_parameters(instance_number))
        elif instance_type == occupancy.instance_type:
            self._parameters.extend(build_type3_occupancy_sensor_parameters(instance_number))
        elif instance_type == light.instance_type:
            self._parameters.extend(build_type4_light_sensor_parameters(instance_number))
        elif instance_type == general_purpose_sensor.instance_type:
            self._parameters.extend(build_type6_general_purpose_sensor_parameters(instance_number))
        elif instance_type == feedback.instance_type:
            self._parameters.extend(build_type32_feedback_parameters(instance_number))
        self.instance_number = instance_number
        self.instance_type = instance_type


class EventSchemeParam(NumberSettingsParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Event addressing scheme", "Схема адресации событий"), "event_scheme"
        )
        self._instance_number = instance_number
        self.property_order = 2
        self.grid_columns = 6

    def get_write_commands(self, short_address: Address, value_to_set: int) -> list[Command]:
        return [
            DTR0(value_to_set),
            SetEventScheme(short_address, self._instance_number),
        ]

    def get_read_command(self, short_address: Address) -> Command:
        return QueryEventScheme(short_address, self._instance_number)

    def get_schema(self, group_and_broadcast: bool) -> dict:
        schema = super().get_schema(group_and_broadcast)
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
        super().__init__(SettingsParamName("Event priority", "Приоритет события"), "event_priority")
        self._instance_number = instance_number
        self.property_order = 3
        self.grid_columns = 6

    def get_write_commands(self, short_address: Address, value_to_set: int) -> list[Command]:
        return [
            DTR0(value_to_set),
            SetEventPriority(short_address, self._instance_number),
        ]

    def get_read_command(self, short_address: Address) -> Command:
        return QueryEventPriority(short_address, self._instance_number)

    def get_schema(self, group_and_broadcast: bool) -> dict:
        schema = super().get_schema(group_and_broadcast)
        schema["properties"][self.property_name]["enum"] = [2, 3, 4, 5]
        if "options" not in schema["properties"][self.property_name]:
            schema["properties"][self.property_name]["options"] = {}
        return schema


class InstanceGroupParamBase(NumberSettingsParam):
    def __init__(self, name: SettingsParamName, property_name: str, instance_number: InstanceNumber) -> None:
        super().__init__(name, property_name)
        self._instance_number = instance_number
        self.grid_columns = 4

    def get_schema(self, group_and_broadcast: bool) -> dict:
        schema = super().get_schema(group_and_broadcast)
        schema["properties"][self.property_name]["enum"] = list(range(32)) + [255]
        return schema


class InstanceGroup0Param(InstanceGroupParamBase):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Primary instance group", "Основная группа экземпляра"),
            "instance_group_0",
            instance_number,
        )
        self.property_order = 4

    def get_write_commands(self, short_address: Address, value_to_set: int) -> list[Command]:
        return [
            DTR0(value_to_set),
            SetPrimaryInstanceGroup(short_address, self._instance_number),
        ]

    def get_read_command(self, short_address: Address) -> Command:
        return QueryPrimaryInstanceGroup(short_address, self._instance_number)


class InstanceGroup1Param(InstanceGroupParamBase):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Instance group 1", "Группа экземпляра 1"),
            "instance_group_1",
            instance_number,
        )
        self.property_order = 5

    def get_write_commands(self, short_address: Address, value_to_set: int) -> list[Command]:
        return [
            DTR0(value_to_set),
            SetInstanceGroup1(short_address, self._instance_number),
        ]

    def get_read_command(self, short_address: Address) -> Command:
        return QueryInstanceGroup1(short_address, self._instance_number)


class InstanceGroup2Param(InstanceGroupParamBase):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Instance group 2", "Группа экземпляра 2"),
            "instance_group_2",
            instance_number,
        )
        self.property_order = 6

    def get_write_commands(self, short_address: Address, value_to_set: int) -> list[Command]:
        return [
            DTR0(value_to_set),
            SetInstanceGroup2(short_address, self._instance_number),
        ]

    def get_read_command(self, short_address: Address) -> Command:
        return QueryInstanceGroup2(short_address, self._instance_number)


class InstanceActiveParam(BooleanSettingsParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Enable Event Messages", "Включить сообщения о событиях"),
            "instance_active",
            lambda short_address, inst=instance_number: QueryInstanceEnabled(short_address, inst),
            lambda short_address, inst=instance_number: EnableInstance(short_address, inst),
            lambda short_address, inst=instance_number: DisableInstance(short_address, inst),
        )
        self.property_order = 1


class InstanceTypeParam(SettingsParamBase):
    INSTANCE_TYPE_NAMES = {
        0: "Generic (0)",
        1: "Push button (1)",
        2: "Absolute input device (2)",
        3: "Occupancy sensor (3)",
        4: "Light sensor (4)",
        6: "General purpose sensor (6)",
        32: "Feedback (32)",
    }

    def __init__(self, instance_type: int) -> None:
        super().__init__(SettingsParamName("Instance type", "Тип экземпляра"))
        self.instance_type_name = self.INSTANCE_TYPE_NAMES.get(instance_type, f"Unknown ({instance_type})")
        self.property_name = "instance_type"

    async def read(self, driver: WBDALIDriver, short_address: Address) -> dict:
        return {self.property_name: self.instance_type_name}

    def get_schema(self, group_and_broadcast: bool) -> dict:
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
        super().__init__(SettingsParamName("Device groups", "Группы устройств"))
        self.property_name = "device_groups"
        self._groups = [False] * self.TOTAL_GROUPS
        self._group_indexes: set[int] = set()

    async def read(self, driver: WBDALIDriver, short_address: Address) -> dict:
        updated_groups = await self._query_all_groups(driver, short_address)
        self._groups = updated_groups
        self._group_indexes = {i for i, is_member in enumerate(updated_groups) if is_member}
        return {self.property_name: updated_groups}

    async def write(self, driver: WBDALIDriver, short_address: Address, value: dict) -> dict:
        groups_to_set = value.get(self.property_name)
        if groups_to_set is None:
            return {}
        desired_groups = [bool(item) for item in groups_to_set]
        if len(desired_groups) != self.TOTAL_GROUPS:
            raise ValueError(f"{self.property_name} must contain {self.TOTAL_GROUPS} items")
        if desired_groups == self._groups:
            return {}

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
                self._build_group_command_sequence(
                    short_address, remove_lower, RemoveFromDeviceGroupsZeroToFifteen
                )
            )
        if remove_upper:
            commands.extend(
                self._build_group_command_sequence(
                    short_address, remove_upper, RemoveFromDeviceGroupsSixteenToThirtyOne
                )
            )
        if add_lower:
            commands.extend(
                self._build_group_command_sequence(short_address, add_lower, AddToDeviceGroupsZeroToFifteen)
            )
        if add_upper:
            commands.extend(
                self._build_group_command_sequence(
                    short_address, add_upper, AddToDeviceGroupsSixteenToThirtyOne
                )
            )
        if not commands:
            return {}

        query_commands = self._build_query_commands(short_address)
        responses = await driver.send_commands(commands + query_commands)
        updated_groups = self._parse_group_responses(responses[-len(query_commands) :])
        self._groups = updated_groups
        self._group_indexes = {i for i, is_member in enumerate(updated_groups) if is_member}
        return {self.property_name: updated_groups}

    def get_schema(self, group_and_broadcast: bool) -> dict:
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

    @property
    def groups(self) -> set[int]:
        return self._group_indexes

    async def _query_all_groups(self, driver: WBDALIDriver, short_address: Address) -> list[bool]:
        responses = await driver.send_commands(self._build_query_commands(short_address))
        return self._parse_group_responses(responses)

    def _build_query_commands(self, address: Address) -> list[Command]:
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
        address: Address,
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
        self._groups_parameter = DeviceGroupsParam()

    @property
    def groups(self) -> set[int]:
        return set()

    def add_instance(self, index: int, instance_type: int) -> None:
        self.instances[index] = InstanceParameters(InstanceNumber(index), instance_type)

    async def _initialize_impl(
        self, driver: WBDALIDriver
    ) -> tuple[list[SettingsParamBase], list[MqttControlBase], list[SettingsParamBase]]:
        addr = DeviceShort(self.address.short)
        await self._groups_parameter.read(driver, addr)

        # Per-device instance discovery
        self.instances.clear()
        num_instances_rsp = await driver.send(QueryNumberOfInstances(device=addr))
        if not check_bad_rsp(num_instances_rsp):
            num_instances = num_instances_rsp.value
            for inst_int in range(num_instances):
                inst = InstanceNumber(inst_int)
                enabled_rsp = await driver.send(QueryInstanceEnabled(device=addr, instance=inst))
                if check_bad_rsp(enabled_rsp) or not enabled_rsp.value:
                    continue
                type_rsp = await driver.send(QueryInstanceType(device=addr, instance=inst))
                if check_bad_rsp(type_rsp):
                    continue
                self.add_instance(inst_int, type_rsp.value)

        parameter_handlers: list[SettingsParamBase] = [
            self._groups_parameter,
            PowerCycleNotificationParam(),
        ]
        parameter_handlers.extend(self.instances.values())

        mqtt_controls: list[MqttControlBase] = []
        for instance in self.instances.values():
            if instance.instance_type == occupancy.instance_type:
                mqtt_controls.extend(get_occupancy_controls(instance.instance_number.value))
            elif instance.instance_type == light.instance_type:
                mqtt_controls.extend(get_light_controls(instance.instance_number.value))
            elif instance.instance_type == pushbutton.instance_type:
                mqtt_controls.extend(get_button_controls(instance.instance_number.value))
            elif instance.instance_type == absolute_input_device.instance_type:
                mqtt_controls.extend(get_absolute_input_device_controls(instance.instance_number.value))
            elif instance.instance_type == general_purpose_sensor.instance_type:
                mqtt_controls.extend(get_general_purpose_sensor_controls(instance.instance_number.value))
            elif instance.instance_type == feedback.instance_type:
                mqtt_controls.extend(get_feedback_controls(instance.instance_number.value))

        return (parameter_handlers, mqtt_controls, [])
