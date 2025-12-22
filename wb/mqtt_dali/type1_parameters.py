# Type 1 self-contained emergency lighting parameters

from dali.address import GearShort
from dali.gear.emergency import (
    QueryEmergencyFeatures,
    QueryEmergencyLevel,
    QueryEmergencyMaxLevel,
    QueryEmergencyMinLevel,
    StoreDTRAsEmergencyLevel,
)

from .extended_gear_parameters import GearParam, TypeParameters
from .wbdali import WBDALIDriver, query_request

# TODO: prolong time is write only


class EmergencyLevelParam(GearParam):
    name = "Emergency level"
    property_name = "type_1_emergency_level"
    query_command_class = QueryEmergencyLevel
    set_command_class = StoreDTRAsEmergencyLevel

    async def get_schema(self, driver: WBDALIDriver, addr: GearShort) -> dict:
        try:
            min_level = await query_request(driver, QueryEmergencyMinLevel(addr))
        except RuntimeError as e:
            raise RuntimeError(f"Failed to read emergency min level: {e}") from e
        try:
            max_level = await query_request(driver, QueryEmergencyMaxLevel(addr))
        except RuntimeError as e:
            raise RuntimeError(f"Failed to read emergency max level: {e}") from e
        return {
            "properties": {
                self.property_name: {
                    "title": self.name,
                    "type": "integer",
                    "minimum": min_level,
                    "maximum": max_level,
                }
            },
            "translations": {"ru": {self.name: "Уровень аварийного освещения"}},
        }


class Type1Parameters(TypeParameters):
    async def get_parameters(self, driver: WBDALIDriver, addr: GearShort) -> list:
        try:
            features = await query_request(driver, QueryEmergencyFeatures(addr))
        except RuntimeError as e:
            raise RuntimeError(f"Failed to read emergency features: {e}") from e
        if ((features >> 4) & 1) != 1:  # bit 4: type 1 emergency lighting support
            return []
        return [EmergencyLevelParam()]
