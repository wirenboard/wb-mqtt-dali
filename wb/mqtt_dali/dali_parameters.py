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
        self.property_order = 16
        self.description = "dimming_curve_desc"

    async def read(
        self, driver: WBDALIDriver, short_address: Address, logger: Optional[logging.Logger] = None
    ) -> dict:
        if self._is_read_only:
            self.value = DimmingCurveType.LOGARITHMIC
            self._dimming_curve_state.curve_type = DimmingCurveType.LOGARITHMIC
            return {self.property_name: DimmingCurveType.LOGARITHMIC}
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
        if self._is_read_only:
            # Read-only fallback never touches the bus; the curve is fixed at
            # LOGARITHMIC. Swallow any write attempts (including our own key)
            # instead of letting NumberSettingsParam.write hit
            # get_write_commands and raise a misleading RuntimeError.
            return {}
        res = await super().write(driver, short_address, value, logger)
        if self.value is not None:
            self._dimming_curve_state.curve_type = DimmingCurveType(self.value)
        return res

    def get_schema(self, group_and_broadcast: bool) -> dict:
        schema = super().get_schema(group_and_broadcast)
        if not schema:
            return schema
        add_enum(
            schema["properties"][self.property_name],
            [(DimmingCurveType.LOGARITHMIC, "standard"), (DimmingCurveType.LINEAR, "linear")],
        )
        if self._is_read_only:
            ru_desc = (
                "Яркость в интерфейсе задаётся в процентах, но сами DALI-устройства "
                "принимают только условные значения от 0 до 254. Кривая диммирования "
                "задаёт формулу их пересчёта в проценты яркости.\n"
                "Это устройство использует фиксированную стандартную (логарифмическую) "
                "кривую. Она подобрана под восприятие света человеческим глазом и "
                "нелинейна: 50% соответствует примерно 229, а 3% - 127. При этом "
                "пошаговые команды и плавная регулировка ощущаются равномерно — "
                "равные шаги в условных значениях выглядят как равные шаги яркости "
                "на всём диапазоне."
            )
            en_desc = (
                "The UI sets brightness as a percentage, but DALI devices themselves "
                "accept only raw levels from 0 to 254. The dimming curve defines the "
                "formula that converts them into brightness percent.\n"
                "This device uses a fixed standard (logarithmic) curve. It is matched "
                "to how the human eye perceives light and is non-linear: 50% "
                "corresponds to about 229, and 3% to 127. At the same time, step "
                "commands and smooth dimming feel uniform — equal steps in raw levels "
                "look like equal steps in brightness across the whole range."
            )
        else:
            ru_desc = (
                "Яркость в интерфейсе задаётся в процентах, но сами DALI-устройства "
                "принимают только условные значения от 0 до 254. Кривая диммирования "
                "задаёт формулу их пересчёта в проценты яркости.\n"
                "Стандартная (логарифмическая) кривая подобрана под восприятие света "
                "человеческим глазом. Она нелинейная: 50% соответствует примерно 229, "
                "а 3% - 127. При этом пошаговые команды и плавная регулировка "
                "ощущаются равномерно — равные шаги в условных значениях выглядят как "
                "равные шаги яркости на всём диапазоне.\n"
                "Линейная кривая сопоставляет проценты и условные значения напрямую "
                "(50% = 127). Это упрощает работу с внешними системами, которые "
                "ожидают линейную шкалу, но глаз воспринимает изменение неравномерно."
            )
            en_desc = (
                "The UI sets brightness as a percentage, but DALI devices themselves "
                "accept only raw levels from 0 to 254. The dimming curve defines the "
                "formula that converts them into brightness percent.\n"
                "The standard (logarithmic) curve is matched to how the human eye "
                "perceives light. It is non-linear: 50% corresponds to about 229, and "
                "3% to 127. At the same time, step commands and smooth dimming feel "
                "uniform — equal steps in raw levels look like equal steps in "
                "brightness across the whole range.\n"
                "The linear curve maps percent and raw levels directly (50% = 127). "
                "That is convenient for external systems that expect a linear scale, "
                "but the eye perceives the change unevenly."
            )
        add_translations(
            schema,
            "ru",
            {"standard": "стандартная", "linear": "линейная", "dimming_curve_desc": ru_desc},
        )
        add_translations(schema, "en", {"dimming_curve_desc": en_desc})
        return schema
