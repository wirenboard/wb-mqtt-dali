# Type 1 self-contained emergency lighting parameters

from dali.address import GearShort
from dali.gear.emergency import (
    QueryEmergencyFeatures,
    QueryEmergencyLevel,
    QueryEmergencyMaxLevel,
    QueryEmergencyMinLevel,
    StoreDTRAsEmergencyLevel,
)

from .extended_gear_parameters import (
    GearParamBase,
    GearParamName,
    NumberGearParam,
    TypeParameters,
)
from .wbdali import WBDALIDriver, query_request

# TODO: prolong time is write only


class EmergencyLevelParam(NumberGearParam):
    query_command_class = QueryEmergencyLevel
    set_command_class = StoreDTRAsEmergencyLevel

    def __init__(self) -> None:
        super().__init__(
            GearParamName("Emergency level", "Уровень аварийного освещения"), "type_1_emergency_level"
        )

    async def get_schema(self, driver: WBDALIDriver, address: GearShort) -> dict:
        try:
            self.minimum = await query_request(driver, QueryEmergencyMinLevel(address))
        except RuntimeError as e:
            raise RuntimeError(f"Failed to read emergency min level: {e}") from e
        try:
            self.maximum = await query_request(driver, QueryEmergencyMaxLevel(address))
        except RuntimeError as e:
            raise RuntimeError(f"Failed to read emergency max level: {e}") from e
        return await super().get_schema(driver, address)


class Type1Parameters(TypeParameters):
    async def get_parameters(self, driver: WBDALIDriver, address: GearShort) -> list[GearParamBase]:
        try:
            features = await query_request(driver, QueryEmergencyFeatures(address))
        except RuntimeError as e:
            raise RuntimeError(f"Failed to read emergency features: {e}") from e
        if not ((features >> 4) & 1):  # bit 4: type 1 emergency lighting support
            return []
        return [EmergencyLevelParam()]
