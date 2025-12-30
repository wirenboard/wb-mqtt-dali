# Type 7 Switching function

from dali.address import GearShort

from .extended_gear_parameters import GearParam, TypeParameters
from .gear.switching_function import (
    QueryDownSwitchOffThreshold,
    QueryDownSwitchOnThreshold,
    QueryErrorHoldOffTime,
    QueryUpSwitchOffThreshold,
    QueryUpSwitchOnThreshold,
    StoreDTRAsDownSwitchOffThreshold,
    StoreDTRAsDownSwitchOnThreshold,
    StoreDTRAsErrorHoldOffTime,
    StoreDTRAsUpSwitchOffThreshold,
    StoreDTRAsUpSwitchOnThreshold,
)
from .wbdali import WBDALIDriver


class UpSwitchOnThresholdParam(GearParam):
    name = "Up switch on threshold"
    property_name = "type_7_up_switch_on_threshold"
    query_command_class = QueryUpSwitchOnThreshold
    set_command_class = StoreDTRAsUpSwitchOnThreshold

    async def get_schema(self, driver: WBDALIDriver, address: GearShort) -> dict:
        return {
            "properties": {
                self.property_name: {
                    "title": self.name,
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 255,
                }
            },
            "translations": {
                "ru": {
                    self.name: "Верхний порог включения переключателя",
                }
            },
        }


class UpSwitchOffThresholdParam(GearParam):
    name = "Up switch off threshold"
    property_name = "type_7_up_switch_off_threshold"
    query_command_class = QueryUpSwitchOffThreshold
    set_command_class = StoreDTRAsUpSwitchOffThreshold

    async def get_schema(self, driver: WBDALIDriver, address: GearShort) -> dict:
        return {
            "properties": {
                self.property_name: {
                    "title": self.name,
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 255,
                }
            },
            "translations": {
                "ru": {
                    self.name: "Верхний порог выключения переключателя",
                }
            },
        }


class DownSwitchOnThresholdParam(GearParam):
    name = "Down switch on threshold"
    property_name = "type_7_down_switch_on_threshold"
    query_command_class = QueryDownSwitchOnThreshold
    set_command_class = StoreDTRAsDownSwitchOnThreshold

    async def get_schema(self, driver: WBDALIDriver, address: GearShort) -> dict:
        return {
            "properties": {
                self.property_name: {
                    "title": self.name,
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 255,
                }
            },
            "translations": {
                "ru": {
                    self.name: "Нижний порог включения переключателя",
                }
            },
        }


class DownSwitchOffThresholdParam(GearParam):
    name = "Down switch off threshold"
    property_name = "type_7_down_switch_off_threshold"
    query_command_class = QueryDownSwitchOffThreshold
    set_command_class = StoreDTRAsDownSwitchOffThreshold

    async def get_schema(self, driver: WBDALIDriver, address: GearShort) -> dict:
        return {
            "properties": {
                self.property_name: {
                    "title": self.name,
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 255,
                }
            },
            "translations": {
                "ru": {
                    self.name: "Нижний порог выключения переключателя",
                }
            },
        }


class ErrorHoldOffTimeParam(GearParam):
    name = "Error holdoff time"
    property_name = "type_7_error_holdoff_time"
    query_command_class = QueryErrorHoldOffTime
    set_command_class = StoreDTRAsErrorHoldOffTime

    async def get_schema(self, driver: WBDALIDriver, address: GearShort) -> dict:
        return {
            "properties": {
                self.property_name: {
                    "title": self.name,
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 255,
                }
            },
            "translations": {
                "ru": {
                    self.name: "Время задержки ошибки",
                }
            },
        }


class Type7Parameters(TypeParameters):
    async def get_parameters(self, driver: WBDALIDriver, address: GearShort) -> list:
        return [
            UpSwitchOnThresholdParam(),
            UpSwitchOffThresholdParam(),
            DownSwitchOnThresholdParam(),
            DownSwitchOffThresholdParam(),
            ErrorHoldOffTimeParam(),
        ]
