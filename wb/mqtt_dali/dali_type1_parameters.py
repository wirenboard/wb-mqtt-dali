# Type 1 self-contained emergency lighting parameters

from dali.address import GearShort
from dali.gear.emergency import (
    QueryEmergencyFeatures,
    QueryEmergencyLevel,
    QueryEmergencyMaxLevel,
    QueryEmergencyMinLevel,
    StoreDTRAsEmergencyLevel,
)

from .dali_parameters import NumberGearParam, TypeParameters
from .settings import SettingsParamName
from .wbdali import WBDALIDriver, query_request


class EmergencyLevelParam(NumberGearParam):
    query_command_class = QueryEmergencyLevel
    set_command_class = StoreDTRAsEmergencyLevel

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Emergency level", "Уровень аварийного освещения"), "type_1_emergency_level"
        )

    async def read(self, driver: WBDALIDriver, short_address: int) -> dict:
        res = await super().read(driver, short_address)
        address = GearShort(short_address)
        try:
            self.minimum = await query_request(driver, QueryEmergencyMinLevel(address))
        except RuntimeError as e:
            raise RuntimeError(f"Failed to read emergency min level: {e}") from e
        try:
            self.maximum = await query_request(driver, QueryEmergencyMaxLevel(address))
        except RuntimeError as e:
            raise RuntimeError(f"Failed to read emergency max level: {e}") from e
        return res


class Type1Parameters(TypeParameters):
    async def read(self, driver: WBDALIDriver, short_address: int) -> dict:
        address = GearShort(short_address)
        try:
            features = await query_request(driver, QueryEmergencyFeatures(address))
        except RuntimeError as e:
            raise RuntimeError(f"Failed to read emergency features: {e}") from e
        if getattr(features, "adjustable emergency level") is True:
            self._parameters = [EmergencyLevelParam()]
            return await super().read(driver, short_address)
        return {}
