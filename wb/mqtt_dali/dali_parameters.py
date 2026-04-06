import logging
from typing import Callable, Optional

from dali.address import Address, GearShort
from dali.command import Command
from dali.gear.general import DTR0

from .common_dali_device import MqttControlBase
from .dali_dimming_curve import DimmingCurveState, DimmingCurveType
from .settings import NumberSettingsParam, SettingsParamBase, SettingsParamName
from .utils import add_enum, add_translations
from .wbdali_utils import WBDALIDriver


class NumberGearParam(NumberSettingsParam):
    query_command_class: Optional[Callable[[Address], Command]] = None
    set_command_class: Optional[Callable[[Address], Command]] = None

    def __init__(self, name: SettingsParamName, property_name: str = "") -> None:
        super().__init__(name, property_name)
        self._is_read_only = self.set_command_class is None

    def get_read_command(self, short_address: Address) -> Command:
        if self.query_command_class is not None:
            return self.query_command_class(short_address)
        raise RuntimeError(f"Query command class for {self.name.en} is not defined")

    def get_write_commands(self, short_address: Address, value_to_set: int) -> list[Command]:
        if self.set_command_class is not None:
            if self.query_command_class is not None:
                return [
                    DTR0(value_to_set),
                    self.set_command_class(short_address),
                ]
            raise RuntimeError(f"Set command class for {self.name.en} is not defined")
        raise RuntimeError(f"Query command class for {self.name.en} is not defined")


class TypeParameters:

    def __init__(self) -> None:
        # Must be filled by subclasses
        self._parameters: list[SettingsParamBase] = []

    def get_mqtt_controls(self) -> list[MqttControlBase]:
        return []

    async def read_mandatory_info(
        self,
        driver: WBDALIDriver,
        short_address: GearShort,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        """
        Must be called before any other read, write or get_schema operations
        to ensure that all mandatory parameters are read and available.
        self._parameters must be filled in this method or in constructor,
        otherwise get_schema for groups or broadcast may not work correctly.
        """


class DimmingCurveParam(NumberGearParam):

    def __init__(self, dimming_curve_state: DimmingCurveState) -> None:
        super().__init__(SettingsParamName("Dimming curve", "Кривая диммирования"), "dimming_curve")
        self._dimming_curve_state = dimming_curve_state

    async def read(
        self, driver: WBDALIDriver, short_address: Address, logger: Optional[logging.Logger] = None
    ) -> dict:
        res = await super().read(driver, short_address, logger)
        self._dimming_curve_state.curve_type = DimmingCurveType(self.value)
        return res

    async def write(
        self,
        driver: WBDALIDriver,
        short_address: Address,
        value: dict,
        logger: Optional[logging.Logger] = None,
    ) -> dict:
        res = await super().write(driver, short_address, value, logger)
        if self.value is not None:
            self._dimming_curve_state.curve_type = DimmingCurveType(self.value)
        return res

    def get_schema(self, group_and_broadcast: bool) -> dict:
        schema = super().get_schema(group_and_broadcast)
        add_enum(
            schema["properties"][self.property_name],
            [(DimmingCurveType.LOGARITHMIC, "standard"), (DimmingCurveType.LINEAR, "linear")],
        )
        add_translations(
            schema,
            "ru",
            {
                "standard": "стандартная",
                "linear": "линейная",
            },
        )
        return schema
