# Type 7 Switching function

import logging
from typing import Optional

from dali.address import GearShort
from dali.command import Response

from .common_dali_device import MqttControl, MqttControlBase
from .dali_parameters import NumberGearParam, TypeParameters
from .device_publisher import ControlInfo, ControlMeta, TranslatedTitle
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
from .wbdali_utils import WBDALIDriver, query_response


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


class ErrorHoldOffTimeParam(NumberGearParam):
    query_command_class = QueryErrorHoldOffTime
    set_command_class = StoreDTRAsErrorHoldOffTime

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Error holdoff time, s", "Время задержки ошибки, с"),
            "type_7_error_holdoff_time",
        )


class Type7Parameters(TypeParameters):
    async def read_mandatory_info(
        self,
        driver: WBDALIDriver,
        short_address: GearShort,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        try:
            features = await query_response(driver, QueryFeatures(short_address), logger)
        except RuntimeError as e:
            raise RuntimeError(f"Failed to read switching function features: {e}") from e
        res = []
        if getattr(features, "adjustable_thresholds") is True:
            res.append(UpSwitchOnThresholdParam())
            res.append(UpSwitchOffThresholdParam())
            res.append(DownSwitchOnThresholdParam())
            res.append(DownSwitchOffThresholdParam())
        if getattr(features, "adjustable_holdoff_time") is True:
            res.append(ErrorHoldOffTimeParam())
        self._parameters = res

    def get_mqtt_controls(self) -> list[MqttControlBase]:

        def format_last_acted(value: Response) -> str:
            if isinstance(value, SwitchingFunctionSwitchStatusResponse):
                if value.last_acted_up_switch_on:
                    return "1"
                if value.last_acted_up_switch_off:
                    return "2"
                if value.last_acted_down_switch_on:
                    return "3"
                if value.last_acted_down_switch_off:
                    return "4"
            return "0"

        return [
            MqttControl(
                control_info=ControlInfo(
                    "last_acted",
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
                query_builder=QuerySwitchStatus,
                value_formatter=format_last_acted,
            ),
        ]
