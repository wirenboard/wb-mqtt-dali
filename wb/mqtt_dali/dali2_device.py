import logging
from dataclasses import dataclass
from typing import Optional, Type

from dali.address import (
    Address,
    Device,
    DeviceShort,
    FeatureDevice,
    FeatureInstanceNumber,
    Instance,
    InstanceNumber,
)
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
    IdentifyDevice,
    QueryApplicationControlEnabled,
    QueryDeviceGroupsEightToFifteen,
    QueryDeviceGroupsSixteenToTwentyThree,
    QueryDeviceGroupsTwentyFourToThirtyOne,
    QueryDeviceGroupsZeroToSeven,
    QueryEventPriority,
    QueryEventScheme,
    QueryFeatureType,
    QueryInstanceEnabled,
    QueryInstanceGroup1,
    QueryInstanceGroup2,
    QueryInstanceType,
    QueryNextFeatureType,
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

from .common_dali_device import DaliDeviceBase, MqttControlBase, PropertyStartOrder
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
from .utils import add_enum, add_translations
from .wbdali import WBDALIDriver
from .wbdali_utils import (
    is_broadcast_or_group_address,
    query_response,
    query_responses,
    query_responses_retry_only_failed,
    send_commands_with_retry,
    send_with_retry,
)

# IEC 62386-103 §11.9.14–15: QueryFeatureType / QueryNextFeatureType.
_FEATURE_TYPE_NONE = 254
_FEATURE_TYPE_MASK = 255
_FEATURE_TYPE_MIN = 32
_FEATURE_TYPE_MAX = 96
_FEATURE_TYPE_ITERATION_LIMIT = _FEATURE_TYPE_MAX - _FEATURE_TYPE_MIN + 1


@dataclass(frozen=True)
class _FeedbackScope:
    query_address: Instance
    feature_address: Instance
    label: str


async def _query_feature_types(
    driver: WBDALIDriver,
    short_address: Address,
    scope: _FeedbackScope,
    logger: Optional[logging.Logger],
) -> list[int]:
    try:
        first_value = (
            await query_response(driver, QueryFeatureType(short_address, scope.query_address), logger)
        ).raw_value.as_integer
    except RuntimeError:
        return []
    if first_value == _FEATURE_TYPE_NONE:
        return []
    if _FEATURE_TYPE_MIN <= first_value <= _FEATURE_TYPE_MAX:
        return [first_value]
    if first_value != _FEATURE_TYPE_MASK:
        if logger is not None:
            logger.info(
                "%s: unexpected QueryFeatureType response %d, ignoring",
                scope.label,
                first_value,
            )
        return []
    feature_types: list[int] = []
    for _ in range(_FEATURE_TYPE_ITERATION_LIMIT):
        try:
            value = (
                await query_response(driver, QueryNextFeatureType(short_address, scope.query_address), logger)
            ).raw_value.as_integer
        except RuntimeError:
            break
        if value == _FEATURE_TYPE_NONE:
            break
        if _FEATURE_TYPE_MIN <= value <= _FEATURE_TYPE_MAX:
            feature_types.append(value)
        elif logger is not None:
            logger.info(
                "%s: unexpected QueryNextFeatureType response %d, ignoring",
                scope.label,
                value,
            )
    return feature_types


async def _query_feedback_capability(
    driver: WBDALIDriver,
    short_address: Address,
    feature_address: Instance,
    logger: Optional[logging.Logger],
) -> Optional[int]:
    try:
        return (
            await query_response(
                driver, feedback.QueryFeedbackCapability(short_address, feature_address), logger
            )
        ).raw_value.as_integer
    except RuntimeError:
        return None


async def _discover_feedback_capability(
    driver: WBDALIDriver,
    short_address: Address,
    scope: _FeedbackScope,
    logger: Optional[logging.Logger],
) -> Optional[int]:
    feature_types = await _query_feature_types(driver, short_address, scope, logger)
    has_feedback = feedback.feature_type in feature_types
    other_features = [ft for ft in feature_types if ft != feedback.feature_type]
    if other_features and logger is not None:
        for other in other_features:
            logger.info("%s: feature type %d is not implemented", scope.label, other)
    if has_feedback:
        capability = await _query_feedback_capability(driver, short_address, scope.feature_address, logger)
        if capability is None and logger is not None:
            logger.debug(
                "%s: feedback feature present but capability query had no answer",
                scope.label,
            )
        return capability
    if feature_types:
        return None
    # Some firmware doesn't advertise feature 32 via QueryFeatureType but still
    # implements Part 332. Probe capability directly before giving up.
    capability = await _query_feedback_capability(driver, short_address, scope.feature_address, logger)
    if capability is not None and logger is not None:
        logger.debug("%s: feedback discovered via heuristic fallback", scope.label)
    return capability


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
            SettingsParamName("Power cycle notification", "Уведомление о перезагрузке по питанию"),
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
                f"Компонент {instance_number.value}",
            ),
            f"instance{instance_number.value}",
        )
        self.property_order = instance_number.value + 100
        self._base_parameters: list[SettingsParamBase] = [
            InstanceActiveParam(instance_number),
            InstanceTypeParam(instance_type),
            EventPriorityParam(instance_number),
            EventSchemeParam(instance_number),
            InstanceGroup0Param(instance_number),
            InstanceGroup1Param(instance_number),
            InstanceGroup2Param(instance_number),
        ]
        if instance_type == pushbutton.instance_type:
            self._base_parameters.extend(build_type1_push_button_parameters(instance_number))
        elif instance_type == absolute_input_device.instance_type:
            self._base_parameters.extend(build_type2_absolute_input_device_parameters(instance_number))
        elif instance_type == occupancy.instance_type:
            self._base_parameters.extend(build_type3_occupancy_sensor_parameters(instance_number))
        elif instance_type == light.instance_type:
            self._base_parameters.extend(build_type4_light_sensor_parameters(instance_number))
        elif instance_type == general_purpose_sensor.instance_type:
            self._base_parameters.extend(build_type6_general_purpose_sensor_parameters(instance_number))
        self.instance_number = instance_number
        self.instance_type = instance_type
        self.feature_address = FeatureInstanceNumber(instance_number.value)
        self.feedback_capability: Optional[int] = None
        self._parameters = list(self._base_parameters)

    async def discover_feedback(
        self, driver: WBDALIDriver, short_address: Address, logger: Optional[logging.Logger] = None
    ) -> None:
        scope = _FeedbackScope(
            query_address=self.instance_number,
            feature_address=self.feature_address,
            label=f"Instance {self.instance_number.value}",
        )
        self.feedback_capability = await _discover_feedback_capability(driver, short_address, scope, logger)

    async def read(
        self, driver: WBDALIDriver, short_address: Address, logger: Optional[logging.Logger] = None
    ) -> dict:
        self._parameters = list(self._base_parameters)
        if self.feedback_capability is not None:
            self._parameters.extend(
                build_type32_feedback_parameters(self.feature_address, self.feedback_capability)
            )
        return await super().read(driver, short_address, logger)


class DeviceFeedbackParameters(SettingsParamGroup):
    PROPERTY_NAME = "feedback"

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Feedback", "Обратная связь"),
            self.PROPERTY_NAME,
        )
        self.feedback_capability: Optional[int] = None

    async def discover_feedback(
        self, driver: WBDALIDriver, short_address: Address, logger: Optional[logging.Logger] = None
    ) -> None:
        scope = _FeedbackScope(
            query_address=Device(),
            feature_address=FeatureDevice(),
            label="Device-level",
        )
        self.feedback_capability = await _discover_feedback_capability(driver, short_address, scope, logger)

    async def read(
        self, driver: WBDALIDriver, short_address: Address, logger: Optional[logging.Logger] = None
    ) -> dict:
        if self.feedback_capability is None:
            return {}
        self._parameters = build_type32_feedback_parameters(FeatureDevice(), self.feedback_capability)
        return await super().read(driver, short_address, logger)

    def get_schema(self, group_and_broadcast: bool) -> dict:
        if self.feedback_capability is None:
            return {}
        return super().get_schema(group_and_broadcast)


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
        add_enum(
            schema["properties"][self.property_name],
            [
                (0, "instance type and number"),
                (1, "device short and instance type"),
                (2, "device short and instance number"),
                (3, "device group and instance type"),
                (4, "instance group and type"),
            ],
        )
        add_translations(
            schema,
            "ru",
            {
                "instance type and number": "тип и номер компонента",
                "device short and instance type": "короткий адрес устройства и тип компонента",
                "device short and instance number": "короткий адрес устройства и номер компонента",
                "device group and instance type": "группа устройства и тип компонента",
                "instance group and type": "группа компонента и тип",
            },
        )
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


class InstanceGroupParamBase(NumberSettingsParam):  # pylint: disable=abstract-method
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
            SettingsParamName("Primary instance group", "Основная группа"),
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
            SettingsParamName("Instance group 1", "Первая группа"),
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
            SettingsParamName("Instance group 2", "Вторая группа"),
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
        0: "Generic",
        1: "Push button",
        2: "Absolute input device",
        3: "Occupancy sensor",
        4: "Light sensor",
        6: "General purpose sensor",
    }

    def __init__(self, instance_type: int) -> None:
        super().__init__(SettingsParamName("Instance type", "Тип компонента"))
        self.property_name = "instance_type"
        self.instance_type = instance_type

    async def read(
        self, driver: WBDALIDriver, short_address: Address, logger: Optional[logging.Logger] = None
    ) -> dict:
        return {self.property_name: self.instance_type}

    def get_schema(self, group_and_broadcast: bool) -> dict:
        return {
            "properties": {
                self.property_name: {
                    "type": "number",
                    "enum": list(self.INSTANCE_TYPE_NAMES.keys()),
                    "title": self.name.en,
                    "options": {
                        "enum_titles": list(self.INSTANCE_TYPE_NAMES.values()),
                        "wb": {"read_only": True},
                    },
                    "propertyOrder": 0,
                }
            },
            "translations": {
                "ru": {
                    self.name.en: self.name.ru,
                    "Generic": "Универсальный",
                    "Push button": "Кнопка",
                    "Absolute input device": "Устройство ввода",
                    "Occupancy sensor": "Датчик присутствия",
                    "Light sensor": "Датчик освещённости",
                    "General purpose sensor": "Датчик общего назначения",
                },
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

    async def read(
        self, driver: WBDALIDriver, short_address: Address, logger: Optional[logging.Logger] = None
    ) -> dict:
        updated_groups = await self._query_all_groups(driver, short_address, logger)
        self._groups = updated_groups
        self._group_indexes = {i for i, is_member in enumerate(updated_groups) if is_member}
        return {self.property_name: updated_groups}

    async def write(  # pylint: disable=too-many-locals
        self,
        driver: WBDALIDriver,
        short_address: Address,
        value: dict,
        logger: Optional[logging.Logger] = None,
    ) -> dict:
        groups_to_set = value.get(self.property_name)
        if groups_to_set is None:
            return {}
        desired_groups = [bool(item) for item in groups_to_set]
        if len(desired_groups) != self.TOTAL_GROUPS:
            raise ValueError(f"{self.property_name} must contain {self.TOTAL_GROUPS} items")
        is_for_single_device = not is_broadcast_or_group_address(short_address)
        if is_for_single_device and desired_groups == self._groups:
            return {}

        desired_lower = self._mask_for_slice(desired_groups[: self.HALF_RANGE])
        desired_upper = self._mask_for_slice(desired_groups[self.HALF_RANGE :])

        if is_for_single_device:
            current_lower = self._mask_for_slice(self._groups[: self.HALF_RANGE])
            current_upper = self._mask_for_slice(self._groups[self.HALF_RANGE :])
            remove_lower = current_lower & ~desired_lower
            remove_upper = current_upper & ~desired_upper
            add_lower = desired_lower & ~current_lower
            add_upper = desired_upper & ~current_upper
        else:
            remove_lower = ~desired_lower & 0xFFFF
            remove_upper = ~desired_upper & 0xFFFF
            add_lower = desired_lower
            add_upper = desired_upper

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

        if not is_for_single_device:
            await send_commands_with_retry(driver, commands, logger)
            return {}

        query_commands = self._build_query_commands(short_address)
        responses = await query_responses(driver, commands + query_commands, logger)
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
                    "propertyOrder": PropertyStartOrder.GROUPS.value,
                    "items": {"type": "boolean", "format": "button"},
                    "minItems": self.TOTAL_GROUPS,
                    "maxItems": self.TOTAL_GROUPS,
                }
            }
        }

    @property
    def groups(self) -> set[int]:
        return self._group_indexes

    async def _query_all_groups(
        self,
        driver: WBDALIDriver,
        short_address: Address,
        logger: Optional[logging.Logger] = None,
    ) -> list[bool]:
        responses = await query_responses_retry_only_failed(
            driver,
            self._build_query_commands(short_address),
            logger,
        )
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
    def __init__(  # pylint: disable=too-many-arguments, R0917
        self,
        address: DaliDeviceAddress,
        bus_id: str,
        gtin_db: DaliDatabase,
        mqtt_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> None:
        super().__init__(
            address, bus_id, "DALI2", "dali2_", Dali2CommandsCompatibilityLayer(), gtin_db, mqtt_id, name
        )
        self.instances: dict[int, InstanceParameters] = {}
        self._gtin_db = gtin_db
        self._groups_parameter = DeviceGroupsParam()
        self._device_feedback = DeviceFeedbackParameters()

    @property
    def groups(self) -> set[int]:
        return set()

    def add_instance(self, index: int, instance_type: int) -> None:
        self.instances[index] = InstanceParameters(InstanceNumber(index), instance_type)

    async def identify(self, driver: WBDALIDriver) -> None:
        await send_with_retry(driver, IdentifyDevice(DeviceShort(self.address.short)), self.logger)

    def _build_mqtt_controls(self) -> list[MqttControlBase]:
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
            if instance.feedback_capability:
                mqtt_controls.extend(
                    get_feedback_controls(
                        instance.feature_address,
                        suffix=str(instance.instance_number.value),
                        order_base=instance.instance_number.value * 10 + 8,
                    )
                )
        if self._device_feedback.feedback_capability:
            mqtt_controls.extend(
                get_feedback_controls(FeatureDevice(), suffix="", order_base=900),
            )
        return mqtt_controls

    async def _initialize_impl(
        self, driver: WBDALIDriver
    ) -> tuple[list[SettingsParamBase], list[SettingsParamBase]]:
        addr = DeviceShort(self.address.short)
        await self._groups_parameter.read(driver, addr, self.logger)

        # Per-device instance discovery
        self.instances.clear()
        num_instances_rsp = await query_response(
            driver,
            QueryNumberOfInstances(device=addr),
            self.logger,
        )
        num_instances = num_instances_rsp.value
        instance_types = await query_responses_retry_only_failed(
            driver,
            [QueryInstanceType(device=addr, instance=InstanceNumber(i)) for i in range(num_instances)],
            self.logger,
        )
        for i, instance_type in enumerate(instance_types):
            self.add_instance(i, instance_type.value)

        await self._device_feedback.discover_feedback(driver, addr, self.logger)
        for instance in self.instances.values():
            await instance.discover_feedback(driver, addr, self.logger)

        parameter_handlers: list[SettingsParamBase] = [
            self._groups_parameter,
            PowerCycleNotificationParam(),
            self._device_feedback,
        ]
        parameter_handlers.extend(self.instances.values())

        return (parameter_handlers, [])
