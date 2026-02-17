# Type 7 Switching function

from dali.address import GearShort
from dali.command import Response

from .common_dali_device import MqttControl
from .dali_parameters import NumberGearParam, TypeParameters
from .device_publisher import ControlInfo
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
from .wbdali import WBDALIDriver, query_request
from .wbmqtt import ControlMeta, TranslatedTitle


class UpSwitchOnThresholdParam(NumberGearParam):
    query_command_class = QueryUpSwitchOnThreshold
    set_command_class = StoreDTRAsUpSwitchOnThreshold

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Up switch on threshold", "Верхний порог включения переключателя"),
            "type_7_up_switch_on_threshold",
        )
        self.minimum = 1


class UpSwitchOffThresholdParam(NumberGearParam):
    query_command_class = QueryUpSwitchOffThreshold
    set_command_class = StoreDTRAsUpSwitchOffThreshold

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Up switch off threshold", "Верхний порог выключения переключателя"),
            "type_7_up_switch_off_threshold",
        )
        self.minimum = 1


class DownSwitchOnThresholdParam(NumberGearParam):
    query_command_class = QueryDownSwitchOnThreshold
    set_command_class = StoreDTRAsDownSwitchOnThreshold

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Down switch on threshold", "Нижний порог включения переключателя"),
            "type_7_down_switch_on_threshold",
        )


class DownSwitchOffThresholdParam(NumberGearParam):
    query_command_class = QueryDownSwitchOffThreshold
    set_command_class = StoreDTRAsDownSwitchOffThreshold

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Down switch off threshold", "Нижний порог выключения переключателя"),
            "type_7_down_switch_off_threshold",
        )


class ErrorHoldOffTimeParam(NumberGearParam):
    query_command_class = QueryErrorHoldOffTime
    set_command_class = StoreDTRAsErrorHoldOffTime

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Error holdoff time", "Время задержки ошибки"), "type_7_error_holdoff_time"
        )


class Type7Parameters(TypeParameters):
    async def read(self, driver: WBDALIDriver, short_address: int) -> dict:
        try:
            features = await query_request(driver, QueryFeatures(GearShort(short_address)))
        except RuntimeError as e:
            raise RuntimeError(f"Failed to read switching function features: {e}") from e
        res = []
        if getattr(features, "adjustable thresholds") is True:
            res.append(UpSwitchOnThresholdParam())
            res.append(UpSwitchOffThresholdParam())
            res.append(DownSwitchOnThresholdParam())
            res.append(DownSwitchOffThresholdParam())
        if getattr(features, "adjustable hold-off time") is True:
            res.append(ErrorHoldOffTimeParam())
        self._parameters = res
        return await super().read(driver, short_address)

    async def get_mqtt_controls(self, driver: WBDALIDriver, short_address: int) -> list[MqttControl]:

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
                        title="Last Acted",
                        read_only=True,
                        enum={
                            "0": TranslatedTitle("unknown"),
                            "1": TranslatedTitle("up switch on"),
                            "2": TranslatedTitle("up switch off"),
                            "3": TranslatedTitle("down switch on"),
                            "4": TranslatedTitle("down switch off"),
                        },
                    ),
                    "0",
                ),
                query_builder=lambda short_address: QuerySwitchStatus(GearShort(short_address)),
                value_formatter=format_last_acted,
            ),
        ]
