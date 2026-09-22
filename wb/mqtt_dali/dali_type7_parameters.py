# Type 7 Switching function

import logging
from typing import Optional

from dali.address import GearShort
from dali.command import Response

from .common_dali_device import (
    EVENT_RESYNC_BASE_INTERVAL,
    MqttControlBase,
    NotifyResult,
    PropertyStartOrder,
    SingleQueryControl,
)
from .control_ids import LAST_ACTED
from .dali_controls import MAX_GEAR_LEVEL
from .dali_parameters import NumberGearParam, TypeParameters
from .device_publisher import ControlInfo
from .events import BusEvent, EventSource, LevelChanged, SwitchStatusRead
from .gear.switching_function import (
    QueryDownSwitchOffThreshold,
    QueryDownSwitchOnThreshold,
    QueryErrorHoldOffTime,
    QueryFeatures,
    QuerySwitchStatus,
    QueryUpSwitchOffThreshold,
    QueryUpSwitchOnThreshold,
    StoreDTRAsDownSwitchOffThreshold,
    StoreDTRAsDownSwitchOnThreshold,
    StoreDTRAsErrorHoldOffTime,
    StoreDTRAsUpSwitchOffThreshold,
    StoreDTRAsUpSwitchOnThreshold,
    SwitchingFunctionSwitchStatusResponse,
)
from .settings import SettingsParamName
from .wbdali import FramePriority, WBDALIDriver
from .wbdali_utils import query_response
from .wbmqtt import ControlMeta, ControlState, TranslatedTitle


class UpSwitchOnThresholdParam(NumberGearParam):
    query_command_class = QueryUpSwitchOnThreshold
    set_command_class = StoreDTRAsUpSwitchOnThreshold

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Up switch on threshold", "Верхний порог включения переключателя"),
            "type_7_up_switch_on_threshold",
        )
        self.minimum = 1
        self.grid_columns = 6
        self.format = "dali-level"
        self.property_order = PropertyStartOrder.SPECIFIC.value


class UpSwitchOffThresholdParam(NumberGearParam):
    query_command_class = QueryUpSwitchOffThreshold
    set_command_class = StoreDTRAsUpSwitchOffThreshold

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Up switch off threshold", "Верхний порог выключения переключателя"),
            "type_7_up_switch_off_threshold",
        )
        self.minimum = 1
        self.grid_columns = 6
        self.format = "dali-level"
        self.property_order = PropertyStartOrder.SPECIFIC.value + 1


class DownSwitchOnThresholdParam(NumberGearParam):
    query_command_class = QueryDownSwitchOnThreshold
    set_command_class = StoreDTRAsDownSwitchOnThreshold

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Down switch on threshold", "Нижний порог включения переключателя"),
            "type_7_down_switch_on_threshold",
        )
        self.grid_columns = 6
        self.format = "dali-level"
        self.property_order = PropertyStartOrder.SPECIFIC.value + 2


class DownSwitchOffThresholdParam(NumberGearParam):
    query_command_class = QueryDownSwitchOffThreshold
    set_command_class = StoreDTRAsDownSwitchOffThreshold

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Down switch off threshold", "Нижний порог выключения переключателя"),
            "type_7_down_switch_off_threshold",
        )
        self.grid_columns = 6
        self.format = "dali-level"
        self.property_order = PropertyStartOrder.SPECIFIC.value + 3


class ErrorHoldOffTimeParam(NumberGearParam):
    query_command_class = QueryErrorHoldOffTime
    set_command_class = StoreDTRAsErrorHoldOffTime

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Error holdoff time, s", "Время задержки ошибки, с"),
            "type_7_error_holdoff_time",
        )
        self.property_order = PropertyStartOrder.SPECIFIC.value + 4


class LastActedControl(SingleQueryControl):
    """Type-7 switch status; an event control predicted from level-threshold crossings.

    Best-effort: a level transition that crosses a switch's on/off threshold sets the
    matching code (up/down × on/off; hysteresis is the separate on/off thresholds).
    No crossing -> value untouched; unread thresholds -> poll only. The exact wiring is
    device-specific, so this is a confirmation-poll hint, not an authoritative value.
    """

    follows_device_fade = True

    def __init__(
        self,
        up_on: Optional[NumberGearParam] = None,
        up_off: Optional[NumberGearParam] = None,
        down_on: Optional[NumberGearParam] = None,
        down_off: Optional[NumberGearParam] = None,
    ) -> None:
        super().__init__(
            ControlInfo(
                LAST_ACTED,
                ControlState(
                    ControlMeta(
                        title=TranslatedTitle("Last Acted", "Последнее действие"),
                        read_only=True,
                        enum={
                            "0": TranslatedTitle("unknown", "неизвестно"),
                            "1": TranslatedTitle("up switch on", "верхний переключатель вкл"),
                            "2": TranslatedTitle("up switch off", "верхний переключатель выкл"),
                            "3": TranslatedTitle("down switch on", "нижний переключатель вкл"),
                            "4": TranslatedTitle("down switch off", "нижний переключатель выкл"),
                        },
                    ),
                    "0",
                ),
            ),
            query_builder=QuerySwitchStatus,
            poll_interval=EVENT_RESYNC_BASE_INTERVAL,
            randomize_poll_interval=True,
        )
        self._up_on = up_on
        self._up_off = up_off
        self._down_on = down_on
        self._down_off = down_off
        # Kept to detect a threshold crossing on the next level.
        self._last_level: Optional[int] = None

    def format_response(self, response: Response) -> str:
        if isinstance(response, SwitchingFunctionSwitchStatusResponse):
            if response.last_acted_up_switch_on:
                return "1"
            if response.last_acted_up_switch_off:
                return "2"
            if response.last_acted_down_switch_on:
                return "3"
            if response.last_acted_down_switch_off:
                return "4"
        return "0"

    def notify(self, event: BusEvent) -> NotifyResult:
        if isinstance(event, SwitchStatusRead):
            return self._apply_quantity_read(event, SwitchStatusRead, self.format_response)
        if not isinstance(event, LevelChanged):
            return super().notify(event)
        if event.settle_at is not None:
            self.schedule_poll_at(event.settle_at)
        # Neither a failed read nor MASK is a side of a crossing; keeping one would drop the
        # last real level the next one is compared against.
        if event.raw_level is None or event.raw_level > MAX_GEAR_LEVEL:
            return NotifyResult.NOTHING_TO_PUBLISH
        prev_level = self._last_level
        self._last_level = event.raw_level
        # Only a sniffed command says something about the switching function: a difference
        # between two polls can come from a missed frame or mid-fade. And a code published
        # while the readback is failing would sit beside a standing /meta/error=r.
        if event.source is not EventSource.OBSERVED or self.control_info.state.error:
            return NotifyResult.NOTHING_TO_PUBLISH
        code = self._crossing_code(prev_level, event.raw_level) if prev_level is not None else None
        if code is None:
            return NotifyResult.NOTHING_TO_PUBLISH
        self.control_info.state.value = str(code)
        return NotifyResult.PUBLISH_STATE

    # --- Hooks for subclasses ---

    def decode_response(self, response: Optional[Response]) -> BusEvent:
        return SwitchStatusRead(response, response is None)

    # --- Private ---

    @staticmethod
    def _threshold(param: Optional[NumberGearParam]) -> Optional[int]:
        return param.value if param is not None else None

    def _crossing_code(self, prev: int, new: int) -> Optional[int]:
        if new > prev:
            up_on = self._threshold(self._up_on)
            if up_on is not None and prev < up_on <= new:
                return 1
            down_on = self._threshold(self._down_on)
            if down_on is not None and prev < down_on <= new:
                return 3
        elif new < prev:
            up_off = self._threshold(self._up_off)
            if up_off is not None and new <= up_off < prev:
                return 2
            down_off = self._threshold(self._down_off)
            if down_off is not None and new <= down_off < prev:
                return 4
        return None


class Type7Parameters(TypeParameters):
    def __init__(self) -> None:
        super().__init__()
        self._up_on: Optional[UpSwitchOnThresholdParam] = None
        self._up_off: Optional[UpSwitchOffThresholdParam] = None
        self._down_on: Optional[DownSwitchOnThresholdParam] = None
        self._down_off: Optional[DownSwitchOffThresholdParam] = None

    async def read_mandatory_info(
        self,
        driver: WBDALIDriver,
        short_address: GearShort,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        try:
            features = await query_response(
                driver, QueryFeatures(short_address), logger, FramePriority.CONFIGURATION
            )
        except RuntimeError as e:
            raise RuntimeError(f"Failed to read switching function features: {e}") from e
        res = []
        if getattr(features, "adjustable_thresholds") is True:
            self._up_on = UpSwitchOnThresholdParam()
            self._up_off = UpSwitchOffThresholdParam()
            self._down_on = DownSwitchOnThresholdParam()
            self._down_off = DownSwitchOffThresholdParam()
            res.extend([self._up_on, self._up_off, self._down_on, self._down_off])
        if getattr(features, "adjustable_holdoff_time") is True:
            res.append(ErrorHoldOffTimeParam())
        self._parameters = res

    def get_mqtt_controls(self) -> list[MqttControlBase]:
        return [
            LastActedControl(
                up_on=self._up_on,
                up_off=self._up_off,
                down_on=self._down_on,
                down_off=self._down_off,
            ),
        ]
