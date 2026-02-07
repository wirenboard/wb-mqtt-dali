from typing import Callable

from dali.address import DeviceShort, InstanceNumber
from dali.command import Command
from dali.device.general import DTR0

from .settings import (
    InstanceAddress,
    NumberSettingsParam,
    SettingsParamAddress,
    SettingsParamName,
)


class InstanceParam(NumberSettingsParam):
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

    @staticmethod
    def _ensure_instance_address(address: SettingsParamAddress) -> InstanceAddress:
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
