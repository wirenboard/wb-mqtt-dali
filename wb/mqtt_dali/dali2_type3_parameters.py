# Type 3 occupancy sensor

from typing import Callable, List

from dali.address import DeviceShort, InstanceNumber
from dali.command import Command
from dali.device.general import DTR0
from dali.device.occupancy import (
    QueryDeadtimeTimer,
    QueryHoldTimer,
    QueryReportTimer,
    SetDeadtimeTimer,
    SetHoldTimer,
    SetReportTimer,
)

from .settings import (
    InstanceAddress,
    NumberSettingsParam,
    SettingsParamAddress,
    SettingsParamName,
)


class _OccupancySensorParam(NumberSettingsParam):
    def __init__(
        self,
        name: str,
        property_name: str,
        query_command: Callable[[DeviceShort, InstanceNumber], Command],
        set_command: Callable[[DeviceShort, InstanceNumber], Command],
    ) -> None:
        super().__init__(SettingsParamName(name), property_name)
        self._query_command = query_command
        self._set_command = set_command

    def _ensure_instance_address(self, address: SettingsParamAddress) -> InstanceAddress:
        if not isinstance(address, InstanceAddress):
            raise ValueError("Address must be an InstanceAddress")
        return address

    def get_read_command(self, address: SettingsParamAddress) -> Command:
        instance_address = self._ensure_instance_address(address)
        return self._query_command(instance_address.device_short, instance_address.instance_number)

    def get_write_commands(self, address: SettingsParamAddress, value_to_set: int) -> list[Command]:
        instance_address = self._ensure_instance_address(address)
        return [
            DTR0(value_to_set),
            self._set_command(instance_address.device_short, instance_address.instance_number),
        ]


class DeadtimeTimerParam(_OccupancySensorParam):
    def __init__(self) -> None:
        super().__init__("Deadtime timer", "deadtime_timer", QueryDeadtimeTimer, SetDeadtimeTimer)


class HoldTimerParam(_OccupancySensorParam):
    def __init__(self) -> None:
        super().__init__("Hold timer", "hold_timer", QueryHoldTimer, SetHoldTimer)


class ReportTimerParam(_OccupancySensorParam):
    def __init__(self) -> None:
        super().__init__("Report timer", "report_timer", QueryReportTimer, SetReportTimer)


def build_type3_occupancy_parameters() -> List[NumberSettingsParam]:
    return [
        DeadtimeTimerParam(),
        HoldTimerParam(),
        ReportTimerParam(),
    ]
