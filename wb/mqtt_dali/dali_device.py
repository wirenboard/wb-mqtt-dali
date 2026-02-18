from enum import IntEnum
from typing import Optional

from dali.address import GearShort
from dali.sequences import QueryDeviceTypes

from .common_dali_device import DaliDeviceAddress, DaliDeviceBase
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
from .wbdali_utils import WBDALIDriver


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

    async def _get_parameter_handlers(self, driver: WBDALIDriver) -> list[SettingsParamBase]:
        self.types = await driver.run_sequence(QueryDeviceTypes(GearShort(self.address.short)))
        if self.types is None:
            raise RuntimeError(
                f"Device at short address {self.address.short} did not respond to QueryDeviceTypes"
            )
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
            DaliDeviceType.COLOUR_CONTROL: Type8Parameters(),
            DaliDeviceType.DIMMING_CURVE_SELECTION: Type17Parameters(),
            DaliDeviceType.DEMAND_RESPONSE: Type20Parameters(),
        }
        for gear_type in self.types:
            try:
                res.append(gear_type_params[DaliDeviceType(gear_type)])
            except (ValueError, KeyError):
                continue
        return res
