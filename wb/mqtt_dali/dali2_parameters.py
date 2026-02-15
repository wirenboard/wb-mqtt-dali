from typing import Callable, Sequence

from dali.address import DeviceShort, InstanceNumber
from dali.command import Command
from dali.device.general import DTR0

from .settings import (
    CommandWriteItem,
    DelayHint,
    NumberSettingsParam,
    SettingsParamName,
)


class InstanceParam(NumberSettingsParam):
    def __init__(
        self,
        name: SettingsParamName,
        property_name: str,
        instance_number: InstanceNumber,
        query_command: Callable[[DeviceShort, InstanceNumber], Command],
        set_command: Callable[[DeviceShort, InstanceNumber], Command],
    ) -> None:
        super().__init__(name, property_name)
        self._query_command = query_command
        self._set_command = set_command
        self._instance_number = instance_number

    def get_read_command(self, short_address: int) -> Command:
        return self._query_command(DeviceShort(short_address), self._instance_number)

    def get_write_commands(self, short_address: int, value_to_set: int) -> Sequence[CommandWriteItem]:
        return [
            DTR0(value_to_set),
            DelayHint(0.3),
            self._set_command(DeviceShort(short_address), self._instance_number),
            DelayHint(0.3),
        ]
