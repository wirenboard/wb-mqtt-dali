# Type 8 Colour Temperature

import logging
from dataclasses import dataclass
from typing import List, Optional

from dali import command
from dali.address import Address
from dali.gear.colour import (
    Activate,
    ColourTemperatureTcStepCooler,
    ColourTemperatureTcStepWarmer,
    QueryColourValue,
    QueryColourValueDTR,
    SetTemporaryColourTemperature,
    StoreColourTemperatureTcLimit,
    StoreColourTemperatureTcLimitDTR2,
    tc_kelvin_mirek,
)
from dali.gear.general import DTR0, DTR1, DTR2, QueryActualLevel, QueryContentDTR0

from .common_dali_device import (
    ControlPollResult,
    MqttControl,
    MqttControlBase,
    PropertyStartOrder,
)
from .dali_type8_common import ColourComponent
from .device_publisher import ControlInfo, ControlMeta
from .settings import SettingsParamBase, SettingsParamName
from .wbdali import WBDALIDriver
from .wbdali_utils import (
    MASK_2BYTES,
    is_broadcast_or_group_address,
    send_commands_with_retry,
)
from .wbmqtt import TranslatedTitle

MAX_TC_MIREK = MASK_2BYTES - 1
MIN_TC_MIREK = 1

UI_MIN_TC_K = 1000
UI_MAX_TC_K = 10000

# The limits for UI if physical limits are not set
UI_MIN_TC_MIREK = tc_kelvin_mirek(UI_MAX_TC_K)
UI_MAX_TC_MIREK = tc_kelvin_mirek(UI_MIN_TC_K)

COLOR_TEMPERATURE_COLOUR_COMPONENTS = [
    ColourComponent.COLOUR_TEMPERATURE,
]


@dataclass
class Type8TcLimits:
    tc_min_mirek: int
    tc_max_mirek: int
    tc_phys_min_mirek: int
    tc_phys_max_mirek: int

    def __init__(
        self,
        tc_min_mirek: int = MIN_TC_MIREK,
        tc_max_mirek: int = MAX_TC_MIREK,
        tc_phys_min_mirek: int = MIN_TC_MIREK,
        tc_phys_max_mirek: int = MAX_TC_MIREK,
    ) -> None:
        self.tc_min_mirek = tc_min_mirek
        self.tc_max_mirek = tc_max_mirek
        self.tc_phys_min_mirek = tc_phys_min_mirek
        self.tc_phys_max_mirek = tc_phys_max_mirek

    def update_from(self, other: "Type8TcLimits") -> None:
        self.tc_min_mirek = other.tc_min_mirek
        self.tc_max_mirek = other.tc_max_mirek
        self.tc_phys_min_mirek = other.tc_phys_min_mirek
        self.tc_phys_max_mirek = other.tc_phys_max_mirek


def set_colour_temperature_commands_builder(address: Address, value: int) -> list[command.Command]:
    return [
        DTR0((value & 0xFF)),
        DTR1((value >> 8) & 0xFF),
        SetTemporaryColourTemperature(address),
    ]


@dataclass
class ColourTemperatureValue:
    tc: int = MASK_2BYTES
    components = COLOR_TEMPERATURE_COLOUR_COMPONENTS

    def get_write_commands(self, address: Address) -> List[command.Command]:
        return set_colour_temperature_commands_builder(address, self.tc)

    def to_json(self) -> dict:
        return {
            "tc": self.tc,
        }

    def from_json(self, value: dict) -> None:
        self.tc = value.get("tc", self.tc)

    def get_schema(self, limits: Type8TcLimits) -> dict:
        return {
            "properties": {
                "tc": {
                    "type": "integer",
                    "title": "Colour temperature",
                    "default": MASK_2BYTES,
                    "format": "dali-tc",
                    "propertyOrder": 2,
                    "options": {
                        "grid_columns": 2,
                        "wb": {
                            "dali_tc": {
                                "minimum": (
                                    limits.tc_min_mirek
                                    if limits.tc_min_mirek != MASK_2BYTES
                                    else UI_MIN_TC_MIREK
                                ),
                                "maximum": (
                                    limits.tc_max_mirek
                                    if limits.tc_max_mirek != MASK_2BYTES
                                    else UI_MAX_TC_MIREK
                                ),
                            },
                        },
                    },
                },
            },
            "required": ["tc"],
            "translations": {"ru": {"Colour temperature": "Цветовая температура"}},
        }


def get_wanted_mqtt_controls(
    tc_min_mirek: int,
    tc_max_mirek: int,
) -> list[MqttControlBase]:
    def _set_colour_temperature_commands_builder(
        short_address: Address, value_k: str
    ) -> list[command.Command]:
        try:
            tc_k = max(int(value_k), 1)
            tc_mirek = tc_kelvin_mirek(tc_k)
            tc_mirek = min(tc_mirek, MAX_TC_MIREK)
            tc_mirek = max(tc_mirek, MIN_TC_MIREK)
        except ValueError as e:
            raise ValueError("colour temperature must be integer") from e
        return set_colour_temperature_commands_builder(short_address, tc_mirek) + [Activate(short_address)]

    if tc_min_mirek == MASK_2BYTES:
        tc_min_mirek = UI_MIN_TC_MIREK
    if tc_max_mirek == MASK_2BYTES:
        tc_max_mirek = UI_MAX_TC_MIREK

    min_k = tc_kelvin_mirek(tc_max_mirek)
    max_k = tc_kelvin_mirek(tc_min_mirek)
    default_k = 4000
    if not min_k < default_k < max_k:
        default_k = min_k

    return [
        MqttControl(
            ControlInfo(
                "set_colour_temperature",
                ControlMeta(
                    "range",
                    TranslatedTitle("Wanted Colour Temperature", "Желаемая цветовая температура"),
                    minimum=min_k,
                    maximum=max_k,
                    units="K",
                ),
                str(default_k),
            ),
            commands_builder=_set_colour_temperature_commands_builder,
        ),
    ]


def get_mqtt_controls(tc_min_mirek: int, tc_max_mirek: int) -> list[MqttControlBase]:

    return [
        MqttControl(
            ControlInfo(
                "current_colour_temperature",
                ControlMeta(
                    title=TranslatedTitle("Colour Temperature", "Цветовая температура"),
                    read_only=True,
                    units="K",
                ),
                "4000",
            ),
            is_group_state_control=True,
        ),
        *get_wanted_mqtt_controls(tc_min_mirek, tc_max_mirek),
        MqttControl(
            ControlInfo(
                "colour_temperature_step_warmer",
                ControlMeta("pushbutton", TranslatedTitle("Colour Temperature Step Warmer", "Теплее")),
                "0",
            ),
            commands_builder=lambda short_address, _: [ColourTemperatureTcStepWarmer(short_address)],
        ),
        MqttControl(
            ControlInfo(
                "colour_temperature_step_cooler",
                ControlMeta("pushbutton", TranslatedTitle("Colour Temperature Step Cooler", "Холоднее")),
                "0",
            ),
            commands_builder=lambda short_address, _: [ColourTemperatureTcStepCooler(short_address)],
        ),
    ]


def handle_poll_controls_result(new_colour: Optional[ColourTemperatureValue]) -> list[ControlPollResult]:
    if new_colour is None or new_colour.tc == MASK_2BYTES:
        value = None
        error = "r"
    else:
        value = str(tc_kelvin_mirek(new_colour.tc))
        error = None
    return [
        ControlPollResult(
            "current_colour_temperature",
            value,
            error=error,
        ),
    ]


async def read_colour_temperature_limits_mirek(
    driver: WBDALIDriver,
    short_address: Address,
    logger: Optional[logging.Logger] = None,
) -> Type8TcLimits:
    cmds = [
        QueryActualLevel(short_address),
        DTR0(QueryColourValueDTR.ColourTemperatureTcWarmest),
        QueryColourValue(short_address),
        QueryContentDTR0(short_address),
        DTR0(QueryColourValueDTR.ColourTemperatureTcCoolest),
        QueryColourValue(short_address),
        QueryContentDTR0(short_address),
        DTR0(QueryColourValueDTR.ColourTemperatureTcPhysicalWarmest),
        QueryColourValue(short_address),
        QueryContentDTR0(short_address),
        DTR0(QueryColourValueDTR.ColourTemperatureTcPhysicalCoolest),
        QueryColourValue(short_address),
        QueryContentDTR0(short_address),
    ]
    resp = await send_commands_with_retry(driver, cmds, logger)
    warmest = MAX_TC_MIREK
    msb_item = resp[2]
    lsb_item = resp[3]
    if msb_item.raw_value is not None and lsb_item.raw_value is not None:
        warmest = (msb_item.raw_value.as_integer << 8) | lsb_item.raw_value.as_integer
    coolest = MIN_TC_MIREK
    msb_item = resp[5]
    lsb_item = resp[6]
    if msb_item.raw_value is not None and lsb_item.raw_value is not None:
        coolest = (msb_item.raw_value.as_integer << 8) | lsb_item.raw_value.as_integer
    physical_warmest = MAX_TC_MIREK
    msb_item = resp[8]
    lsb_item = resp[9]
    if msb_item.raw_value is not None and lsb_item.raw_value is not None:
        physical_warmest = (msb_item.raw_value.as_integer << 8) | lsb_item.raw_value.as_integer
    physical_coolest = MIN_TC_MIREK
    msb_item = resp[11]
    lsb_item = resp[12]
    if msb_item.raw_value is not None and lsb_item.raw_value is not None:
        physical_coolest = (msb_item.raw_value.as_integer << 8) | lsb_item.raw_value.as_integer
    return Type8TcLimits(
        tc_min_mirek=coolest,
        tc_max_mirek=warmest,
        tc_phys_min_mirek=physical_coolest,
        tc_phys_max_mirek=physical_warmest,
    )


class TcLimitsSettings(SettingsParamBase):
    requires_mqtt_controls_refresh = True

    def __init__(self, limits: Type8TcLimits) -> None:
        super().__init__(SettingsParamName("Colour Temperature Limits", "Границы цветовой температуры"))
        self.property_name = "tc_limits"
        self._limits = limits

    def _current_values(self) -> dict:
        return {
            "tc_coolest": self._limits.tc_min_mirek,
            "tc_warmest": self._limits.tc_max_mirek,
            "tc_physical_coolest": self._limits.tc_phys_min_mirek,
            "tc_physical_warmest": self._limits.tc_phys_max_mirek,
        }

    async def read(
        self, driver: WBDALIDriver, short_address: Address, logger: Optional[logging.Logger] = None
    ) -> dict:
        result = await read_colour_temperature_limits_mirek(driver, short_address, logger)
        self._limits.update_from(result)
        return {self.property_name: self._current_values()}

    async def write(  # pylint: disable=too-many-branches, too-many-locals
        self,
        driver: WBDALIDriver,
        short_address: Address,
        value: dict,
        logger: Optional[logging.Logger] = None,
    ) -> dict:
        if self.property_name not in value:
            return {}
        new_vals = value[self.property_name]
        current = self._current_values()
        is_for_single_device = not is_broadcast_or_group_address(short_address)
        if is_for_single_device and new_vals == current:
            return {}

        phys_cool = new_vals.get("tc_physical_coolest", current["tc_physical_coolest"])
        cool = new_vals.get("tc_coolest", current["tc_coolest"])
        warm = new_vals.get("tc_warmest", current["tc_warmest"])
        phys_warm = new_vals.get("tc_physical_warmest", current["tc_physical_warmest"])

        # Send physical limits first to avoid cascading shifts of user limits
        for field_name, mirek_val, selector in [
            ("tc_physical_coolest", phys_cool, StoreColourTemperatureTcLimitDTR2.TcPhysicalCoolest),
            ("tc_physical_warmest", phys_warm, StoreColourTemperatureTcLimitDTR2.TcPhysicalWarmest),
            ("tc_coolest", cool, StoreColourTemperatureTcLimitDTR2.TcCoolest),
            ("tc_warmest", warm, StoreColourTemperatureTcLimitDTR2.TcWarmest),
        ]:
            if is_for_single_device and new_vals.get(field_name, current[field_name]) == current[field_name]:
                continue
            cmds = [
                DTR0(mirek_val & 0xFF),
                DTR1((mirek_val >> 8) & 0xFF),
                DTR2(selector),
                StoreColourTemperatureTcLimit(short_address),
            ]
            await send_commands_with_retry(driver, cmds, logger)

        if not is_for_single_device:
            return {}

        # Re-read all 4 limits (ECG may cascade)
        result = await read_colour_temperature_limits_mirek(driver, short_address, logger)
        self._limits.update_from(result)
        return {self.property_name: self._current_values()}

    def has_changes(self, new_params: dict) -> bool:
        if self.property_name not in new_params:
            return False
        return new_params[self.property_name] != self._current_values()

    def get_schema(self, group_and_broadcast: bool) -> dict:
        return {
            "properties": {
                self.property_name: {
                    "type": "object",
                    "title": self.name.en,
                    "format": "card",
                    "propertyOrder": PropertyStartOrder.TC_LIMITS.value,
                    "properties": {
                        "tc_physical_warmest": {
                            "type": "integer",
                            "title": "Physical Warmest",
                            "format": "dali-tc",
                            "propertyOrder": 1,
                            "options": {
                                "grid_columns": 6,
                                "wb": {
                                    "dali_tc": {
                                        "minimum": UI_MIN_TC_MIREK,
                                        "maximum": UI_MAX_TC_MIREK,
                                        "mode": "limit",
                                    },
                                },
                            },
                        },
                        "tc_physical_coolest": {
                            "type": "integer",
                            "title": "Physical Coolest",
                            "format": "dali-tc",
                            "propertyOrder": 2,
                            "options": {
                                "grid_columns": 6,
                                "wb": {
                                    "dali_tc": {
                                        "minimum": UI_MIN_TC_MIREK,
                                        "maximum": UI_MAX_TC_MIREK,
                                        "mode": "limit",
                                    },
                                },
                            },
                        },
                        "tc_warmest": {
                            "type": "integer",
                            "title": "UI Warmest",
                            "format": "dali-tc",
                            "propertyOrder": 3,
                            "options": {
                                "grid_columns": 6,
                                "wb": {
                                    "dali_tc": {
                                        "minimum": (
                                            self._limits.tc_phys_min_mirek
                                            if self._limits.tc_phys_min_mirek != MASK_2BYTES
                                            else UI_MIN_TC_MIREK
                                        ),
                                        "maximum": (
                                            self._limits.tc_phys_max_mirek
                                            if self._limits.tc_phys_max_mirek != MASK_2BYTES
                                            else UI_MAX_TC_MIREK
                                        ),
                                        "mode": "limit",
                                    },
                                },
                            },
                        },
                        "tc_coolest": {
                            "type": "integer",
                            "title": "UI Coolest",
                            "format": "dali-tc",
                            "propertyOrder": 4,
                            "options": {
                                "grid_columns": 6,
                                "wb": {
                                    "dali_tc": {
                                        "minimum": (
                                            self._limits.tc_phys_min_mirek
                                            if self._limits.tc_phys_min_mirek != MASK_2BYTES
                                            else UI_MIN_TC_MIREK
                                        ),
                                        "maximum": (
                                            self._limits.tc_phys_max_mirek
                                            if self._limits.tc_phys_max_mirek != MASK_2BYTES
                                            else UI_MAX_TC_MIREK
                                        ),
                                        "mode": "limit",
                                    },
                                },
                            },
                        },
                    },
                    "required": [
                        "tc_coolest",
                        "tc_warmest",
                        "tc_physical_coolest",
                        "tc_physical_warmest",
                    ],
                },
            },
            "translations": {
                "ru": {
                    self.name.en: self.name.ru,
                    "UI Coolest": "Максимальная цветовая температура",
                    "UI Warmest": "Минимальная цветовая температура",
                    "Physical Coolest": "Физическая максимальная цветовая температура",
                    "Physical Warmest": "Физическая минимальная цветовая температура",
                },
            },
        }
