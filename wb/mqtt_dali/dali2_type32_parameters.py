# Feature type 32 feedback, IEC 62386-332

import logging
from typing import List, Optional

from dali.address import Address, Instance
from dali.device.general import DTR0

from .dali2_parameters import InstanceParam
from .device.feedback import (
    QueryActiveFeedbackBrightness,
    QueryActiveFeedbackColour,
    QueryActiveFeedbackPitch,
    QueryActiveFeedbackVolume,
    QueryFeedbackTiming,
    QueryInactiveFeedbackBrightness,
    QueryInactiveFeedbackColour,
    SetActiveFeedbackBrightness,
    SetActiveFeedbackColour,
    SetActiveFeedbackPitch,
    SetActiveFeedbackVolume,
    SetFeedbackTiming,
    SetInactiveFeedbackBrightness,
    SetInactiveFeedbackColour,
)
from .settings import SettingsParamBase, SettingsParamName
from .utils import add_enum
from .wbdali import WBDALIDriver
from .wbdali_utils import (
    is_broadcast_or_group_address,
    query_int,
    query_responses,
    send_with_retry,
)


class _FeedbackBrightnessParamBase(InstanceParam):
    def __init__(  # pylint: disable=too-many-arguments, R0917
        self,
        name: SettingsParamName,
        property_name: str,
        feature_address: Instance,
        query_command,
        set_command,
        property_order: int,
        default: int,
    ) -> None:
        super().__init__(name, property_name, feature_address, query_command, set_command)
        self.minimum = 0
        self.maximum = 255
        self.default = default
        self.grid_columns = 3
        self.property_order = property_order


class ActiveFeedbackBrightnessParam(_FeedbackBrightnessParamBase):
    def __init__(self, feature_address: Instance) -> None:
        super().__init__(
            SettingsParamName("Active feedback brightness", "Яркость активной обратной связи"),
            "active_feedback_brightness",
            feature_address,
            QueryActiveFeedbackBrightness,
            SetActiveFeedbackBrightness,
            property_order=10,
            default=255,
        )


class InactiveFeedbackBrightnessParam(_FeedbackBrightnessParamBase):
    def __init__(self, feature_address: Instance) -> None:
        super().__init__(
            SettingsParamName("Inactive feedback brightness", "Яркость неактивной обратной связи"),
            "inactive_feedback_brightness",
            feature_address,
            QueryInactiveFeedbackBrightness,
            SetInactiveFeedbackBrightness,
            property_order=11,
            default=0,
        )


class _FeedbackColourParamBase(InstanceParam):
    def __init__(  # pylint: disable=too-many-arguments, R0917
        self,
        name: SettingsParamName,
        property_name: str,
        feature_address: Instance,
        query_command,
        set_command,
        property_order: int,
    ) -> None:
        super().__init__(name, property_name, feature_address, query_command, set_command)
        self.minimum = 1
        self.maximum = 63
        self.default = 63
        self.grid_columns = 3
        self.property_order = property_order
        self.description = "Packed 6-bit RGB: bits[1:0]=R, [3:2]=G, [5:4]=B"


class ActiveFeedbackColourParam(_FeedbackColourParamBase):
    def __init__(self, feature_address: Instance) -> None:
        super().__init__(
            SettingsParamName("Active feedback colour", "Цвет активной обратной связи"),
            "active_feedback_colour",
            feature_address,
            QueryActiveFeedbackColour,
            SetActiveFeedbackColour,
            property_order=12,
        )


class InactiveFeedbackColourParam(_FeedbackColourParamBase):
    def __init__(self, feature_address: Instance) -> None:
        super().__init__(
            SettingsParamName("Inactive feedback colour", "Цвет неактивной обратной связи"),
            "inactive_feedback_colour",
            feature_address,
            QueryInactiveFeedbackColour,
            SetInactiveFeedbackColour,
            property_order=13,
        )


class ActiveFeedbackVolumeParam(InstanceParam):
    def __init__(self, feature_address: Instance) -> None:
        super().__init__(
            SettingsParamName("Active feedback volume", "Громкость активной обратной связи"),
            "active_feedback_volume",
            feature_address,
            QueryActiveFeedbackVolume,
            SetActiveFeedbackVolume,
        )
        self.minimum = 0
        self.maximum = 255
        self.default = 255
        self.grid_columns = 3
        self.property_order = 14


class ActiveFeedbackPitchParam(InstanceParam):
    def __init__(self, feature_address: Instance) -> None:
        super().__init__(
            SettingsParamName("Active feedback pitch", "Тон активной обратной связи"),
            "active_feedback_pitch",
            feature_address,
            QueryActiveFeedbackPitch,
            SetActiveFeedbackPitch,
        )
        self.minimum = 0
        self.maximum = 255
        self.default = 128
        self.grid_columns = 3
        self.property_order = 15


# Feedback timing byte layout, IEC 62386-332 Table 2:
# bits[2:0]=duty, bits[5:3]=period, bits[7:6]=cycles.
TIMING_DUTY_CYCLE_MASK = 0b0000_0111
TIMING_PERIOD_SHIFT = 3
TIMING_PERIOD_MASK = 0b0000_0111
TIMING_CYCLES_SHIFT = 6
TIMING_CYCLES_MASK = 0b0000_0011

TIMING_DUTY_CYCLE_TITLES = [
    (0, "1/8 of period"),
    (1, "2/8 of period"),
    (2, "3/8 of period"),
    (3, "4/8 of period"),
    (4, "5/8 of period"),
    (5, "6/8 of period"),
    (6, "7/8 of period"),
    (7, "8/8 of period (continuous)"),
]
TIMING_DUTY_CYCLE_TRANSLATIONS_RU = {
    "1/8 of period": "1/8 периода",
    "2/8 of period": "2/8 периода",
    "3/8 of period": "3/8 периода",
    "4/8 of period": "4/8 периода",
    "5/8 of period": "5/8 периода",
    "6/8 of period": "6/8 периода",
    "7/8 of period": "7/8 периода",
    "8/8 of period (continuous)": "8/8 периода (без мигания)",
}
TIMING_PERIOD_TITLES = [
    (0, "0.5 s"),
    (1, "1.0 s"),
    (2, "1.5 s"),
    (3, "2.0 s"),
    (4, "2.5 s"),
    (5, "3.0 s"),
    (6, "3.5 s"),
    (7, "4.0 s"),
]
TIMING_CYCLES_TITLES = [
    (0, "1 cycle"),
    (1, "2 cycles"),
    (2, "3 cycles"),
    (3, "endless"),
]
TIMING_CYCLES_TRANSLATIONS_RU = {
    "1 cycle": "1 цикл",
    "2 cycles": "2 цикла",
    "3 cycles": "3 цикла",
    "endless": "бесконечно",
}

# 0xFF (duty=7, period=7, cycles=3) — factory default, solid feedback.
TIMING_DEFAULT_DUTY_CYCLE = 7
TIMING_DEFAULT_PERIOD = 7
TIMING_DEFAULT_CYCLES = 3


def pack_feedback_timing(duty_cycle: int, period: int, cycles: int) -> int:
    return (
        (cycles & TIMING_CYCLES_MASK) << TIMING_CYCLES_SHIFT
        | (period & TIMING_PERIOD_MASK) << TIMING_PERIOD_SHIFT
        | (duty_cycle & TIMING_DUTY_CYCLE_MASK)
    )


def unpack_feedback_timing(byte_value: int) -> tuple[int, int, int]:
    duty_cycle = byte_value & TIMING_DUTY_CYCLE_MASK
    period = (byte_value >> TIMING_PERIOD_SHIFT) & TIMING_PERIOD_MASK
    cycles = (byte_value >> TIMING_CYCLES_SHIFT) & TIMING_CYCLES_MASK
    return duty_cycle, period, cycles


class FeedbackTimingParam(SettingsParamBase):
    PROPERTY_NAME = "feedback_timing"
    DUTY_CYCLE_KEY = "feedback_timing_duty_cycle"
    PERIOD_KEY = "feedback_timing_period"
    CYCLES_KEY = "feedback_timing_cycles"

    def __init__(self, feature_address: Instance) -> None:
        super().__init__(SettingsParamName("Feedback timing", "Тайминг обратной связи"))
        self.property_name = self.PROPERTY_NAME
        self._feature_address = feature_address
        self.property_order = 16
        self.value: Optional[dict[str, int]] = None

    async def read(
        self, driver: WBDALIDriver, short_address: Address, logger: Optional[logging.Logger] = None
    ) -> dict:
        try:
            byte_value = await query_int(
                driver, QueryFeedbackTiming(short_address, self._feature_address), logger
            )
        except RuntimeError:
            byte_value = pack_feedback_timing(
                TIMING_DEFAULT_DUTY_CYCLE, TIMING_DEFAULT_PERIOD, TIMING_DEFAULT_CYCLES
            )
        self.value = self._card_from_byte(byte_value)
        return {self.property_name: dict(self.value)}

    async def write(
        self,
        driver: WBDALIDriver,
        short_address: Address,
        value: dict,
        logger: Optional[logging.Logger] = None,
    ) -> dict:
        if self.property_name not in value:
            return {}
        target = self._normalise_card(value[self.property_name])
        is_for_single_device = not is_broadcast_or_group_address(short_address)
        if is_for_single_device and self.value == target:
            return {}
        packed = pack_feedback_timing(
            target[self.DUTY_CYCLE_KEY], target[self.PERIOD_KEY], target[self.CYCLES_KEY]
        )
        commands = [
            DTR0(packed),
            SetFeedbackTiming(short_address, self._feature_address),
        ]
        if not is_for_single_device:
            for cmd in commands:
                await send_with_retry(driver, cmd, logger)
            return {}
        # Read back to record the stored value, not the requested one.
        commands.append(QueryFeedbackTiming(short_address, self._feature_address))
        responses = await query_responses(driver, commands, logger)
        stored = responses[-1].raw_value.as_integer
        self.value = self._card_from_byte(stored)
        return {self.property_name: dict(self.value)}

    def _card_from_byte(self, byte_value: int) -> dict[str, int]:
        duty_cycle, period, cycles = unpack_feedback_timing(byte_value)
        return {
            self.DUTY_CYCLE_KEY: duty_cycle,
            self.PERIOD_KEY: period,
            self.CYCLES_KEY: cycles,
        }

    def _normalise_card(self, raw_card) -> dict[str, int]:
        new_card = raw_card or {}
        return {
            self.DUTY_CYCLE_KEY: self._coerce(new_card.get(self.DUTY_CYCLE_KEY), TIMING_DUTY_CYCLE_MASK),
            self.PERIOD_KEY: self._coerce(new_card.get(self.PERIOD_KEY), TIMING_PERIOD_MASK),
            self.CYCLES_KEY: self._coerce(new_card.get(self.CYCLES_KEY), TIMING_CYCLES_MASK),
        }

    def has_changes(self, new_params: dict) -> bool:
        if self.property_name not in new_params:
            return False
        return self.value != self._normalise_card(new_params[self.property_name])

    def get_schema(self, group_and_broadcast: bool) -> dict:
        del group_and_broadcast
        duty_field: dict = {
            "type": "integer",
            "title": "Duty cycle",
            "default": TIMING_DEFAULT_DUTY_CYCLE,
            "propertyOrder": 1,
            "options": {"grid_columns": 4},
        }
        add_enum(duty_field, TIMING_DUTY_CYCLE_TITLES)
        period_field: dict = {
            "type": "integer",
            "title": "Period",
            "default": TIMING_DEFAULT_PERIOD,
            "propertyOrder": 2,
            "options": {"grid_columns": 4},
        }
        add_enum(period_field, TIMING_PERIOD_TITLES)
        cycles_field: dict = {
            "type": "integer",
            "title": "Cycles",
            "default": TIMING_DEFAULT_CYCLES,
            "propertyOrder": 3,
            "options": {"grid_columns": 4},
        }
        add_enum(cycles_field, TIMING_CYCLES_TITLES)
        card: dict = {
            "format": "card",
            "type": "object",
            "title": self.name.en,
            "propertyOrder": self.property_order,
            "properties": {
                self.DUTY_CYCLE_KEY: duty_field,
                self.PERIOD_KEY: period_field,
                self.CYCLES_KEY: cycles_field,
            },
            "required": [self.DUTY_CYCLE_KEY, self.PERIOD_KEY, self.CYCLES_KEY],
        }
        return {
            "properties": {self.property_name: card},
            "translations": {
                "ru": {
                    self.name.en: self.name.ru,
                    "Duty cycle": "Длительность импульса",
                    "Period": "Период",
                    "Cycles": "Циклы",
                    **TIMING_DUTY_CYCLE_TRANSLATIONS_RU,
                    **TIMING_CYCLES_TRANSLATIONS_RU,
                }
            },
        }

    @staticmethod
    def _coerce(raw, mask: int) -> int:
        if raw is None:
            return 0
        return int(raw) & mask


# Capability bits, IEC 62386-332 Table 1.
CAPABILITY_BIT_VISIBLE = 0
CAPABILITY_BIT_BRIGHTNESS = 1
CAPABILITY_BIT_COLOUR = 2
CAPABILITY_BIT_AUDIBLE = 3
CAPABILITY_BIT_VOLUME = 4
CAPABILITY_BIT_PITCH = 5


def build_type32_feedback_parameters(
    feature_address: Instance, capability_bits: int
) -> List[SettingsParamBase]:
    params: list[SettingsParamBase] = []
    if capability_bits & (1 << CAPABILITY_BIT_BRIGHTNESS):
        params.append(ActiveFeedbackBrightnessParam(feature_address))
        params.append(InactiveFeedbackBrightnessParam(feature_address))
    if capability_bits & (1 << CAPABILITY_BIT_COLOUR):
        params.append(ActiveFeedbackColourParam(feature_address))
        params.append(InactiveFeedbackColourParam(feature_address))
    if capability_bits & (1 << CAPABILITY_BIT_VOLUME):
        params.append(ActiveFeedbackVolumeParam(feature_address))
    if capability_bits & (1 << CAPABILITY_BIT_PITCH):
        params.append(ActiveFeedbackPitchParam(feature_address))
    # Timing applies to every capability bit, so always shown.
    params.append(FeedbackTimingParam(feature_address))
    return params
