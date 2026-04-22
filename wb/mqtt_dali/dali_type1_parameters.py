# Type 1 self-contained emergency lighting parameters

import logging
from typing import Optional

from dali.address import Address, GearShort
from dali.gear.emergency import (
    QueryEmergencyFeatures,
    QueryEmergencyLevel,
    QueryEmergencyMaxLevel,
    QueryEmergencyMinLevel,
    StoreDTRAsEmergencyLevel,
)

from .dali_parameters import NumberGearParam, TypeParameters
from .settings import SettingsParamName
from .wbdali import WBDALIDriver
from .wbdali_utils import query_int, query_response


class EmergencyLevelParam(NumberGearParam):
    query_command_class = QueryEmergencyLevel
    set_command_class = StoreDTRAsEmergencyLevel

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Emergency level", "Яркость аварийного освещения"), "type_1_emergency_level"
        )
        self.format = "dali-level"

    async def read(
        self, driver: WBDALIDriver, short_address: Address, logger: Optional[logging.Logger] = None
    ) -> dict:
        res = await super().read(driver, short_address, logger)
        try:
            self.minimum = await query_int(driver, QueryEmergencyMinLevel(short_address), logger=logger)
        except RuntimeError as e:
            raise RuntimeError(f"Failed to read emergency min level: {e}") from e
        try:
            self.maximum = await query_int(driver, QueryEmergencyMaxLevel(short_address), logger=logger)
        except RuntimeError as e:
            raise RuntimeError(f"Failed to read emergency max level: {e}") from e
        return res


class Type1Parameters(TypeParameters):
    async def read_mandatory_info(
        self,
        driver: WBDALIDriver,
        short_address: GearShort,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        try:
            features = await query_response(driver, QueryEmergencyFeatures(short_address), logger)
        except RuntimeError as e:
            raise RuntimeError(f"Failed to read emergency features: {e}") from e
        if getattr(features, "adjustable_emergency_level") is True:
            self._parameters = [EmergencyLevelParam()]
