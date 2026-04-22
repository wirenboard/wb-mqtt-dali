import asyncio
import logging
from enum import IntEnum
from typing import Optional

from dali.address import Address, GearShort
from dali.gear.general import (
    IdentifyDevice,
    Initialise,
    QueryDeviceType,
    QueryNextDeviceType,
    RecallMaxLevel,
    RecallMinLevel,
    Terminate,
)

from .common_dali_device import (
    ControlPollResult,
    DaliDeviceAddress,
    DaliDeviceBase,
    MqttControlBase,
)
from .dali_common_parameters import (
    FadeTimeFadeRateParam,
    GroupScenesSettings,
    GroupsParam,
    MaxLevelParam,
    MinLevelParam,
    PowerOnLevelParam,
    ScenesParam,
    SystemFailureLevelParam,
)
from .dali_compat import DaliCommandsCompatibilityLayer
from .dali_controls import CONTROLS, ActualLevelControl, WantedLevelControl
from .dali_dimming_curve import DimmingCurveState, DimmingCurveType
from .dali_parameters import DimmingCurveParam, TypeParameters
from .dali_type1_parameters import Type1Parameters
from .dali_type4_parameters import Type4Parameters
from .dali_type5_parameters import Type5Parameters
from .dali_type6_parameters import Type6Parameters
from .dali_type7_parameters import Type7Parameters
from .dali_type8_parameters import ColourType, Type8Parameters
from .dali_type8_rgbwaf import MAX_COLOUR_VALUE, set_rgb_commands_builder
from .dali_type8_tc import Type8TcLimits
from .dali_type16_parameters import Type16Parameters
from .dali_type17_parameters import Type17Parameters
from .dali_type20_parameters import Type20Parameters
from .dali_type21_parameters import Type21Parameters
from .dali_type49_parameters import Type49Parameters
from .dali_type50_parameters import Type50Parameters
from .dali_type52_parameters import Type52Parameters
from .gtin_db import DaliDatabase
from .settings import SettingsParamBase
from .wbdali import WBDALIDriver
from .wbdali_utils import (
    check_query_response,
    query_response,
    send_commands_with_retry,
    send_with_retry,
)


class DaliDeviceType(IntEnum):
    FLUORESCENT_LAMP_BALLAST = 0
    SELF_CONTAINED_EMERGENCY_LIGHTING = 1
    DISCHARGE_LAMPS = 2
    LOW_VOLTAGE_HALOGEN_LAMPS = 3
    SUPPLY_VOLTAGE_CONTROLLER_FOR_INCANDESCENT_LAMPS = 4
    CONVERSION_FROM_DIGITAL_SIGNAL_INTO_DC_VOLTAGE = 5
    LED_MODULES = 6
    SWITCHING_FUNCTION = 7
    COLOUR_CONTROL = 8
    SEQUENCER = 9
    LOAD_REFERENCING = 15
    THERMAL_GEAR_PROTECTION = 16
    DIMMING_CURVE_SELECTION = 17
    DEMAND_RESPONSE = 20
    THERMAL_LAMP_PROTECTION = 21
    NON_REPLACEABLE_LAMP_SOURCE = 23
    INTEGRATED_POWER_SUPPLY = 49
    MEMORY_BANK_1_EXTENSION = 50
    ENERGY_REPORTING_DEVICE = 51
    DIAGNOSTICS_AND_MAINTENANCE = 52


def request_with_retry_sequence(cmd):
    """Helper function to perform a sequence with retries on failure"""
    last_error = "unknown error"
    for _ in range(3):
        result = yield cmd
        try:
            check_query_response(result)
            return result
        except RuntimeError as e:
            last_error = str(e)
    raise RuntimeError(f"No response to {cmd} after 3 attempts; last error: {last_error}")


def query_device_types_sequence(addr: Address):
    """Obtain a list of part 2xx device types supported by control gear"""
    r = yield from request_with_retry_sequence(QueryDeviceType(addr))
    if r.raw_value.as_integer < 254:
        return [r.raw_value.as_integer]
    if r.raw_value.as_integer == 254:
        return []
    assert r.raw_value.as_integer == 255
    last_seen = 0
    result = []
    while True:
        r = yield from request_with_retry_sequence(QueryNextDeviceType(addr))
        if r.raw_value.as_integer == 254:
            if len(result) == 0:
                raise RuntimeError("No device types returned by QueryNextDeviceType")
            return result
        if r.raw_value.as_integer <= last_seen:
            # The gear is required to return device types in
            # ascending order, without repeats
            raise RuntimeError("Device type received out of order")
        result.append(r.raw_value.as_integer)


class DaliDevice(DaliDeviceBase):

    def __init__(  # pylint: disable=too-many-arguments, R0917
        self,
        address: DaliDeviceAddress,
        bus_id: str,
        gtin_db: DaliDatabase,
        mqtt_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> None:
        super().__init__(
            address, bus_id, "DALI", "", DaliCommandsCompatibilityLayer(), gtin_db, mqtt_id, name
        )
        self.types: list[int] = []

        self._type8_handler: Optional[Type8Parameters] = None
        self._type_handlers: list[TypeParameters] = []
        self._dimming_curve_state = DimmingCurveState()
        self._groups_parameter = GroupsParam()

    async def identify(self, driver: WBDALIDriver) -> None:
        address = GearShort(self.address.short)
        # Old gear that does not support QUERY VERSION NUMBER returns 1.
        # If the device does not respond or returns 1, assume old gear and fall back to
        # the deprecated blink method (INITIALISE → RECALL MAX/MIN alternating → TERMINATE).
        use_identify_cmd = False
        for _ in range(2):
            try:
                v = await query_response(driver, self._compat.QueryVersionNumber(address), self.logger)
            except Exception:  # pylint: disable=broad-exception-caught
                continue
            use_identify_cmd = v.value != 1
            break
        if use_identify_cmd:
            await send_with_retry(driver, IdentifyDevice(address), self.logger)
        else:
            setup_rgb = (
                self._type8_handler is not None
                and self._type8_handler.default_colour_type == ColourType.RGBWAF
            )
            await legacy_identify_sequence(driver, address, setup_rgb, self.logger)

    async def load_info(self, driver: WBDALIDriver, force_reload: bool = False) -> None:
        await super().load_info(driver, force_reload)
        self.params["types"] = self.types

    async def poll_controls(self, driver: WBDALIDriver) -> list[ControlPollResult]:
        res = await super().poll_controls(driver)
        if self._type8_handler is not None:
            res.extend(await self._type8_handler.poll_controls(driver, self.address.short))
        return res

    @property
    def groups(self) -> set[int]:
        return self._groups_parameter.groups

    @property
    def dt8_colour_type(self) -> Optional[ColourType]:
        return self._type8_handler.default_colour_type if self._type8_handler is not None else None

    @property
    def dimming_curve_type(self) -> DimmingCurveType:
        return self._dimming_curve_state.curve_type

    @property
    def dt8_tc_limits(self) -> Optional[Type8TcLimits]:
        if (
            self._type8_handler is not None
            and self._type8_handler.default_colour_type == ColourType.COLOUR_TEMPERATURE
        ):
            return self._type8_handler.tc_limits
        return None

    def get_common_mqtt_controls(self) -> list[MqttControlBase]:
        return [
            ActualLevelControl(self._dimming_curve_state),
            WantedLevelControl(self._dimming_curve_state),
            *CONTROLS,
        ]

    def _build_mqtt_controls(self) -> list[MqttControlBase]:
        mqtt_controls: list[MqttControlBase] = [
            ActualLevelControl(self._dimming_curve_state),
            WantedLevelControl(self._dimming_curve_state),
        ]
        mqtt_controls.extend(CONTROLS)
        for type_handler in self._type_handlers:
            mqtt_controls.extend(type_handler.get_mqtt_controls())
        return mqtt_controls

    async def _initialize_impl(  # pylint: disable=too-many-branches
        self, driver: WBDALIDriver
    ) -> tuple[list[SettingsParamBase], list[SettingsParamBase]]:
        address = GearShort(self.address.short)

        types = await driver.run_sequence(query_device_types_sequence(address))
        if types is None:
            raise RuntimeError(
                f"Device at short address {self.address.short} did not respond to QueryDeviceTypes"
            )
        self.types = types

        gear_type_params = {
            DaliDeviceType.SELF_CONTAINED_EMERGENCY_LIGHTING: Type1Parameters(),
            DaliDeviceType.SUPPLY_VOLTAGE_CONTROLLER_FOR_INCANDESCENT_LAMPS: Type4Parameters(
                self._dimming_curve_state
            ),
            DaliDeviceType.CONVERSION_FROM_DIGITAL_SIGNAL_INTO_DC_VOLTAGE: Type5Parameters(
                self._dimming_curve_state
            ),
            DaliDeviceType.LED_MODULES: Type6Parameters(self._dimming_curve_state),
            DaliDeviceType.SWITCHING_FUNCTION: Type7Parameters(),
            DaliDeviceType.THERMAL_GEAR_PROTECTION: Type16Parameters(),
            DaliDeviceType.DIMMING_CURVE_SELECTION: Type17Parameters(self._dimming_curve_state),
            DaliDeviceType.DEMAND_RESPONSE: Type20Parameters(),
            DaliDeviceType.THERMAL_LAMP_PROTECTION: Type21Parameters(),
            DaliDeviceType.INTEGRATED_POWER_SUPPLY: Type49Parameters(),
            DaliDeviceType.MEMORY_BANK_1_EXTENSION: Type50Parameters(),
            DaliDeviceType.DIAGNOSTICS_AND_MAINTENANCE: Type52Parameters(),
        }
        self._type_handlers = []
        for gear_type in types:
            try:
                dali_device_type = DaliDeviceType(gear_type)
                if dali_device_type == DaliDeviceType.COLOUR_CONTROL:
                    self._type8_handler = Type8Parameters()
                    type_handler = self._type8_handler
                else:
                    type_handler = gear_type_params[dali_device_type]
                self._type_handlers.append(type_handler)
            except (ValueError, KeyError):
                continue

        for handler in self._type_handlers:
            await handler.read_mandatory_info(driver, address, self.logger)

        await self._groups_parameter.read(driver, address, self.logger)

        # Parameter handlers for settings page in UI
        parameter_handlers: list[SettingsParamBase] = [
            self._groups_parameter,
            MaxLevelParam(),
            MinLevelParam(),
            FadeTimeFadeRateParam(),
        ]

        # If none of the type handlers contributed a dimming curve parameter, fall back
        # to a read-only one fixed at the standard (logarithmic) curve so the UI always
        # exposes the field for single devices.
        if not any(
            isinstance(param, DimmingCurveParam)
            for handler in self._type_handlers
            for param in handler._parameters  # pylint: disable=protected-access
        ):
            fallback = DimmingCurveParam(self._dimming_curve_state)
            parameter_handlers.append(fallback)
        # Colour control has own scenes, power on level and system failure level parameters
        if DaliDeviceType.COLOUR_CONTROL.value not in self.types:
            parameter_handlers.extend([ScenesParam(), PowerOnLevelParam(), SystemFailureLevelParam()])
        for type_handler in self._type_handlers:
            parameter_handlers.extend(type_handler._parameters)  # pylint: disable=protected-access

        # Group parameter handlers for group settings page in UI
        group_parameter_handlers: list[SettingsParamBase] = [
            MaxLevelParam(),
            MinLevelParam(),
            FadeTimeFadeRateParam(),
        ]
        # Colour control has own scenes, power on level and system failure level parameters
        if DaliDeviceType.COLOUR_CONTROL.value not in self.types:
            group_parameter_handlers.extend(
                [GroupScenesSettings(), PowerOnLevelParam(), SystemFailureLevelParam()]
            )
        for type_handler in self._type_handlers:
            if type_handler != self._type8_handler:
                group_parameter_handlers.extend(type_handler._parameters)  # pylint: disable=protected-access
            else:
                if self._type8_handler is not None:
                    group_parameter_handlers.extend(self._type8_handler.get_group_parameters())

        return (parameter_handlers, group_parameter_handlers)


async def legacy_identify_sequence(
    driver: WBDALIDriver,
    address: GearShort,
    setup_rgb: bool,
    logger: Optional[logging.Logger] = None,
) -> None:

    await send_with_retry(driver, Initialise(address=address.address), logger)
    if setup_rgb:
        await send_commands_with_retry(
            driver,
            set_rgb_commands_builder(address, MAX_COLOUR_VALUE, MAX_COLOUR_VALUE, MAX_COLOUR_VALUE),
            logger=logger,
        )
    try:
        for _ in range(5):
            await send_with_retry(driver, RecallMaxLevel(address), logger)
            await asyncio.sleep(0.5)
            await send_with_retry(driver, RecallMinLevel(address), logger)
            await asyncio.sleep(0.5)
    finally:
        await send_with_retry(driver, Terminate(), logger)
