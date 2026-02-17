from enum import IntEnum
from typing import Optional

from dali.address import GearShort
from dali.sequences import QueryDeviceTypes

from .common_dali_device import (
    ControlPollResult,
    DaliDeviceAddress,
    DaliDeviceBase,
    MqttControl,
)
from .dali_common_parameters import (
    FadeTimeFadeRateParam,
    GroupsParam,
    MaxLevelParam,
    MinLevelParam,
    PowerOnLevelParam,
    ScenesParam,
    SystemFailureLevelParam,
)
from .dali_compat import DaliCommandsCompatibilityLayer
from .dali_controls import ACTION_CONTROLS, POLLING_CONTROLS
from .dali_type1_parameters import Type1Parameters
from .dali_type4_parameters import Type4Parameters
from .dali_type5_parameters import Type5Parameters
from .dali_type6_parameters import Type6Parameters
from .dali_type7_parameters import Type7Parameters
from .dali_type8_parameters import Type8Parameters
from .dali_type17_parameters import Type17Parameters
from .dali_type20_parameters import Type20Parameters
from .gtin_db import DaliDatabase
from .settings import SettingsParamBase
from .wbdali import WBDALIDriver


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
    NON_REPLACEABLE_LAMP_SOURCE = 22
    INTEGRATED_POWER_SUPPLY = 49
    ENERGY_REPORTING_DEVICE = 51


class DaliDevice(DaliDeviceBase):

    def __init__(
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

    async def poll_controls(self, driver: WBDALIDriver) -> list[ControlPollResult]:
        res = await super().poll_controls(driver)
        if self._type8_handler is not None:
            res.extend(
                ControlPollResult(self.mqtt_id, control_id, value)
                for control_id, value in (
                    await self._type8_handler.poll_controls(driver, self.address.short)
                ).items()
            )
        return res

    async def _get_mqtt_controls(self, driver: WBDALIDriver) -> list[MqttControl]:
        await self._read_types(driver)
        return POLLING_CONTROLS + ACTION_CONTROLS

    async def _get_parameter_handlers(self, driver: WBDALIDriver) -> list[SettingsParamBase]:
        await self._read_types(driver)
        res: list[SettingsParamBase] = [
            GroupsParam(),
            MaxLevelParam(),
            MinLevelParam(),
            FadeTimeFadeRateParam(),
        ]
        # Colour control has own scenes, power on level and system failure level parameters
        if DaliDeviceType.COLOUR_CONTROL.value not in self.types:
            res.extend([ScenesParam(), PowerOnLevelParam(), SystemFailureLevelParam()])
        gear_type_params = {
            DaliDeviceType.SELF_CONTAINED_EMERGENCY_LIGHTING: Type1Parameters(),
            DaliDeviceType.SUPPLY_VOLTAGE_CONTROLLER_FOR_INCANDESCENT_LAMPS: Type4Parameters(),
            DaliDeviceType.CONVERSION_FROM_DIGITAL_SIGNAL_INTO_DC_VOLTAGE: Type5Parameters(),
            DaliDeviceType.LED_MODULES: Type6Parameters(),
            DaliDeviceType.SWITCHING_FUNCTION: Type7Parameters(),
            DaliDeviceType.DIMMING_CURVE_SELECTION: Type17Parameters(),
            DaliDeviceType.DEMAND_RESPONSE: Type20Parameters(),
        }
        for gear_type in self.types:
            try:
                dali_device_type = DaliDeviceType(gear_type)
                if dali_device_type == DaliDeviceType.COLOUR_CONTROL:
                    self._type8_handler = Type8Parameters()
                    res.append(self._type8_handler)
                else:
                    res.append(gear_type_params[dali_device_type])
            except (ValueError, KeyError):
                continue
        return res

    async def _read_types(self, driver: WBDALIDriver) -> None:
        if self.types:
            return
        types = await driver.run_sequence(QueryDeviceTypes(GearShort(self.address.short)))
        if types is None:
            raise RuntimeError(
                f"Device at short address {self.address.short} did not respond to QueryDeviceTypes"
            )
        self.types = types
