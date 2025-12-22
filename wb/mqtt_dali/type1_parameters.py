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
from .wbdali import WBDALIDriver, send_extended_command

# TODO: prolong time is write only


class EmergencyLevelParam(GearParam):
    name = "Emergency level"
    property_name = "type_1_emergency_level"
    query_command_class = QueryEmergencyLevel
    set_command_class = StoreDTRAsEmergencyLevel

    async def get_schema(self, driver: WBDALIDriver, addr: GearShort) -> dict:
        min_level_response = await driver.send(QueryEmergencyMinLevel(addr))
        if min_level_response is None:
            min_level = 0
        else:
            min_level = min_level_response.raw_value.as_integer
        max_level_response = await driver.send(QueryEmergencyMaxLevel(addr))
        if max_level_response is None:
            max_level = 254
        else:
            max_level = max_level_response.raw_value.as_integer
        value = await driver.send(QueryEmergencyLevel(addr))
        if value is None:
            return {}
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
        features = await send_extended_command(driver, QueryEmergencyFeatures(addr))
        if not features or not features.bits[4]:
            return []
        return [EmergencyLevelParam()]
