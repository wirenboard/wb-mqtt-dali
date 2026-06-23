# Type 3 occupancy sensor

from typing import List

from dali.address import InstanceNumber
from dali.device.occupancy import (
    QueryDeadtimeTimer,
    QueryHoldTimer,
    QueryReportTimer,
    SetDeadtimeTimer,
    SetHoldTimer,
    SetReportTimer,
)

from .dali2_parameters import InstanceParam
from .settings import SettingsParamName
from .wbmqtt import TranslatedTitle


class DeadtimeTimerParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Deadtime timer, ms", "Таймер задержки, мс"),
            "deadtime_timer",
            instance_number,
            QueryDeadtimeTimer,
            SetDeadtimeTimer,
        )
        self.grid_columns = 4
        self.property_order = 10
        self.multiplier = 50  # IEC 62386-303 Table 4: T_incr = 50 ms
        self.maximum = 255 * 50
        self.description = TranslatedTitle(
            en=(
                "Sets the minimum interval between consecutive events the sensor puts on the "
                "bus. When triggers arrive in quick succession it keeps the sensor from flooding "
                "the bus: after sending an event the sensor waits out this interval before it "
                "may send the next one."
            ),
            ru=(
                "Задаёт минимальный интервал между событиями, которые датчик отправляет на шину. "
                "Когда срабатывания идут одно за другим, этот интервал не даёт датчику перегружать "
                "шину: отправив событие, датчик выжидает заданное время, прежде чем отправить "
                "следующее."
            ),
        )


class HoldTimerParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Hold timer, s", "Таймер удержания, с"),
            "hold_timer",
            instance_number,
            QueryHoldTimer,
            SetHoldTimer,
        )
        self.grid_columns = 4
        self.property_order = 11
        self.multiplier = 10  # IEC 62386-303 Table 4: T_incr = 10 s
        self.maximum = 255 * 10
        self.description = TranslatedTitle(
            en=(
                "Applies to movement sensors. After motion is last detected, the area is kept "
                '"Occupied" for this length of time, and every new movement starts the interval '
                'over again; once it elapses without movement, the state switches to "Vacant". '
                "Presence sensors ignore this setting, since they determine occupancy directly."
            ),
            ru=(
                "Применимо только к датчикам движения, но не датчикам присутствия. После последнего "
                "обнаруженного движения состояние «Занято» удерживается в течение этого времени, и "
                "каждое новое движение запускает отсчёт заново. Когда время без движения истекает, "
                "состояние сменяется на «Вакантно»."
            ),
        )


class ReportTimerParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Report timer, s", "Таймер отчёта, с"),
            "report_timer",
            instance_number,
            QueryReportTimer,
            SetReportTimer,
        )
        self.grid_columns = 4
        self.property_order = 12
        # IEC 62386-303 Table 4: T_incr = 1 s, raw value = seconds directly
        self.description = TranslatedTitle(
            en=(
                'Sets how often the sensor repeats its current state — "still occupied" or '
                '"still vacant" even when nothing has changed. These periodic messages only '
                "confirm that the sensor is alive, they have no effect on the "
                "occupied/vacant transitions themselves or on the hold time."
            ),
            ru=(
                "Задаёт, как часто датчик повторяет своё текущее состояние — «всё ещё занято» или "
                "«всё ещё вакантно» даже когда ничего не изменилось. Эти периодические сообщения "
                "лишь подтверждают, что датчик на связи, на сами переходы "
                "«занято/вакантно» и на время удержания они не влияют."
            ),
        )


def build_type3_occupancy_sensor_parameters(instance_number: InstanceNumber) -> List[InstanceParam]:
    return [
        DeadtimeTimerParam(instance_number),
        HoldTimerParam(instance_number),
        ReportTimerParam(instance_number),
    ]
