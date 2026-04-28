# Type 50 Memory bank 1 extension (luminaire information)

import logging
from typing import Optional

from dali.address import Address
from dali.exceptions import MemoryLocationNotImplemented, ResponseError
from dali.memory import oem
from dali.memory.location import FlagValue

from .common_dali_device import PropertyStartOrder, read_memory_bank
from .dali_compat import DaliCommandsCompatibilityLayer
from .dali_parameters import TypeParameters
from .settings import SettingsParamBase, SettingsParamName
from .wbdali import WBDALIDriver

# Each entry: (oem memory value class, json key, English title, Russian title, json schema type)
_FIELD_SPECS = [
    (
        oem.YearOfManufacture,
        "year_of_manufacture",
        "Year of manufacture",
        "Год изготовления",
        "integer",
    ),
    (
        oem.WeekOfManufacture,
        "week_of_manufacture",
        "Week of manufacture",
        "Неделя изготовления",
        "integer",
    ),
    (
        oem.InputPowerNominal,
        "nominal_input_power",
        "Nominal input power, W",
        "Номинальная мощность, Вт",
        "integer",
    ),
    (
        oem.InputPowerMinimumDim,
        "power_at_min_dim",
        "Power at minimum dim level, W",
        "Мощность при минимальной яркости, Вт",
        "integer",
    ),
    (
        oem.MainsVoltageMinimum,
        "min_mains_voltage",
        "Nominal minimum AC mains voltage, V",
        "Номинальное минимальное напряжение, В",
        "integer",
    ),
    (
        oem.MainsVoltageMaximum,
        "max_mains_voltage",
        "Nominal maximum AC mains voltage, V",
        "Номинальное максимальное напряжение, В",
        "integer",
    ),
    (
        oem.LightOutputNominal,
        "nominal_light_output",
        "Nominal light output, lm",
        "Номинальный световой поток, лм",
        "integer",
    ),
    (oem.CRI, "cri", "CRI", "CRI", "integer"),
    (oem.CCT, "cct", "CCT, K", "ЦВТ, К", "integer"),
    (
        oem.LightDistributionType,
        "light_distribution_type",
        "Light distribution type",
        "Тип светораспределения",
        "string",
    ),
    (
        oem.LuminaireColor,
        "luminaire_color",
        "Luminaire body colour",
        "Цвет корпуса светильника",
        "string",
    ),
    (
        oem.LuminaireIdentification,
        "luminaire_identification",
        "Luminaire identification",
        "Идентификация светильника",
        "string",
    ),
]


def _is_all_ff(value: int, n_bytes: int) -> bool:
    return value == (1 << (n_bytes * 8)) - 1


class Type50MemoryBankParam(SettingsParamBase):
    def __init__(self) -> None:
        super().__init__(SettingsParamName("Luminaire information", "Информация о светильнике"))
        self._values: dict = {}
        self._compat = DaliCommandsCompatibilityLayer()

    async def read(
        self, driver: WBDALIDriver, short_address: Address, logger: Optional[logging.Logger] = None
    ) -> dict:
        try:
            raw = await driver.run_sequence(read_memory_bank(oem.BANK_1, short_address, self._compat))
        except (MemoryLocationNotImplemented, ResponseError) as e:
            raise RuntimeError(f"Failed to read DT50 memory bank: {e}") from e

        self._values = {}
        for oem_class, key, _, _, json_type in _FIELD_SPECS:
            value = raw.get(oem_class)
            if value is None or isinstance(value, FlagValue):
                continue
            if json_type == "integer":
                if not isinstance(value, int):
                    continue
                if _is_all_ff(value, len(oem_class.locations)):
                    continue
            elif json_type == "string":
                if not isinstance(value, str) or not value.strip("\x00"):
                    continue
            self._values[key] = value

        if not self._values:
            return {}
        return {"luminaire_info": self._values}

    def get_schema(self, group_and_broadcast: bool) -> dict:
        if group_and_broadcast:
            return {}

        properties = {}
        translations_ru = {self.name.en: self.name.ru}
        for order, (_, key, title_en, title_ru, json_type) in enumerate(_FIELD_SPECS, start=1):
            translations_ru[title_en] = title_ru
            properties[key] = {
                "type": json_type,
                "title": title_en,
                "propertyOrder": order,
                "options": {"wb": {"read_only": True}},
            }

        return {
            "properties": {
                "luminaire_info": {
                    "title": self.name.en,
                    "type": "object",
                    "format": "card",
                    "properties": properties,
                    "propertyOrder": PropertyStartOrder.DT50.value,
                },
            },
            "translations": {
                "ru": translations_ru,
            },
        }


class Type50Parameters(TypeParameters):
    def __init__(self) -> None:
        super().__init__()
        self._parameters = [Type50MemoryBankParam()]
