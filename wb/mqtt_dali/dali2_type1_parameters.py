# Type 1 push buttons

import logging
from typing import List, Optional, Tuple

from dali.address import Address, InstanceNumber
from dali.device.general import DTR0, QueryEventFilterZeroToSeven, SetEventFilter
from dali.device.pushbutton import (
    InstanceEventFilter,
    QueryDoubleTimer,
    QueryRepeatTimer,
    QueryShortTimer,
    QueryStuckTimer,
    SetDoubleTimer,
    SetRepeatTimer,
    SetShortTimer,
    SetStuckTimer,
)

from .dali2_parameters import InstanceParam
from .settings import SettingsParamBase, SettingsParamName
from .wbdali import WBDALIDriver
from .wbdali_utils import is_broadcast_or_group_address, query_int, query_responses


class DoubleTimerParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Double timer, ms", "Таймер двойного нажатия, мс"),
            "double_timer",
            instance_number,
            QueryDoubleTimer,
            SetDoubleTimer,
        )
        self.property_order = 11
        self.grid_columns = 3
        self.multiplier = 20  # IEC 62386-301 Table 4: T_incr = 20 ms
        self.maximum = 100 * self.multiplier


class RepeatTimerParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Repeat timer, ms", "Таймер повтора, мс"),
            "repeat_timer",
            instance_number,
            QueryRepeatTimer,
            SetRepeatTimer,
        )
        self.property_order = 12
        self.grid_columns = 3
        self.multiplier = 20  # IEC 62386-301 Table 4: T_incr = 20 ms
        self.minimum = 5 * self.multiplier
        self.maximum = 100 * self.multiplier


class ShortTimerParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Short timer, ms", "Таймер короткого нажатия, мс"),
            "short_timer",
            instance_number,
            QueryShortTimer,
            SetShortTimer,
        )
        self.property_order = 10
        self.grid_columns = 3
        self.multiplier = 20  # IEC 62386-301 Table 4: T_incr = 20 ms
        self.minimum = 10 * self.multiplier  # IEC 62386-301 Table 9
        self.maximum = 255 * self.multiplier


class StuckTimerParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Stuck timer, s", "Таймер залипания, с"),
            "stuck_timer",
            instance_number,
            QueryStuckTimer,
            SetStuckTimer,
        )
        self.property_order = 13
        self.grid_columns = 3
        self.minimum = 5
        # IEC 62386-301 Table 4: T_incr = 1 s, raw value = seconds directly


class EventFilterParam(SettingsParamBase):
    PROPERTY_NAME = "event_filter"

    # Bit name in dali.device.pushbutton.InstanceEventFilter -> (en title, ru title, default)
    BIT_DEFINITIONS: List[Tuple[str, str, str, bool]] = [
        ("button_released", "Button released", "Кнопка отпущена", False),
        ("button_pressed", "Button pressed", "Кнопка нажата", False),
        ("short_press", "Short press", "Короткое нажатие", True),
        ("double_press", "Double press", "Двойное нажатие", False),
        ("long_press_start", "Long press start", "Начало длинного нажатия", True),
        ("long_press_repeat", "Long press repeat", "Повтор длинного нажатия", True),
        ("long_press_stop", "Long press stop", "Конец длинного нажатия", True),
        ("button_stuck_free", "Button stuck/free", "Кнопка залипла/освободилась", True),
    ]

    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(SettingsParamName("Event filter", "Фильтр событий"))
        self._instance_number = instance_number
        self.property_order = 7
        self.value: Optional[int] = None

    async def read(
        self, driver: WBDALIDriver, short_address: Address, logger: Optional[logging.Logger] = None
    ) -> dict:
        if is_broadcast_or_group_address(short_address):
            return {}
        raw = await query_int(
            driver,
            QueryEventFilterZeroToSeven(short_address, self._instance_number),
            logger,
        )
        mask = raw & 0xFF
        self.value = mask
        return {self.PROPERTY_NAME: self._mask_to_dict(mask)}

    async def write(
        self,
        driver: WBDALIDriver,
        short_address: Address,
        value: dict,
        logger: Optional[logging.Logger] = None,
    ) -> dict:
        if self.PROPERTY_NAME not in value:
            return {}
        if is_broadcast_or_group_address(short_address):
            return {}
        new_mask = self._dict_to_mask(value[self.PROPERTY_NAME])
        if self.value == new_mask:
            return {}
        commands = [
            DTR0(new_mask),
            SetEventFilter(short_address, self._instance_number),
            QueryEventFilterZeroToSeven(short_address, self._instance_number),
        ]
        responses = await query_responses(driver, commands, logger)
        actual_mask = responses[-1].raw_value.as_integer & 0xFF
        self.value = actual_mask
        return {self.PROPERTY_NAME: self._mask_to_dict(actual_mask)}

    def has_changes(self, new_params: dict) -> bool:
        if self.PROPERTY_NAME not in new_params:
            return False
        new_mask = self._dict_to_mask(new_params[self.PROPERTY_NAME])
        return self.value != new_mask

    def get_schema(self, group_and_broadcast: bool) -> dict:
        del group_and_broadcast
        bit_properties: dict = {}
        translations: dict = {self.name.en: self.name.ru} if self.name.ru else {}
        for bit_name, en_title, ru_title, default in self.BIT_DEFINITIONS:
            bit_properties[bit_name] = {
                "type": "boolean",
                "title": en_title,
                "format": "switch",
                "default": default,
            }
            translations[en_title] = ru_title
        card: dict = {
            "title": self.name.en,
            "type": "object",
            "format": "card",
            "options": {"collapsed": True},
            "properties": bit_properties,
        }
        if self.property_order is not None:
            card["propertyOrder"] = self.property_order
        return {
            "properties": {self.PROPERTY_NAME: card},
            "translations": {"ru": translations},
        }

    @classmethod
    def _mask_to_dict(cls, mask: int) -> dict:
        return {
            bit_name: bool(mask & InstanceEventFilter[bit_name].value) for bit_name, *_ in cls.BIT_DEFINITIONS
        }

    @classmethod
    def _dict_to_mask(cls, value: dict) -> int:
        mask = 0
        for bit_name, *_ in cls.BIT_DEFINITIONS:
            if value.get(bit_name, False):
                mask |= InstanceEventFilter[bit_name].value
        return mask


def build_type1_push_button_parameters(instance_number: InstanceNumber) -> List[SettingsParamBase]:
    return [
        DoubleTimerParam(instance_number),
        RepeatTimerParam(instance_number),
        ShortTimerParam(instance_number),
        StuckTimerParam(instance_number),
        EventFilterParam(instance_number),
    ]
