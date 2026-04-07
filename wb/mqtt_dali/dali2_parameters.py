from typing import Callable

from dali.address import Address, InstanceNumber
from dali.command import Command
from dali.device.general import DTR0

from .settings import NumberSettingsParam, SettingsParamName


class InstanceParam(NumberSettingsParam):
    def __init__(  # pylint: disable=too-many-arguments, too-many-positional-arguments
        self,
        name: SettingsParamName,
        property_name: str,
        instance_number: InstanceNumber,
        query_command: Callable[[Address, InstanceNumber], Command],
        set_command: Callable[[Address, InstanceNumber], Command],
    ) -> None:
        super().__init__(name, property_name)
        self._query_command = query_command
        self._set_command = set_command
        self._instance_number = instance_number

    def get_read_command(self, short_address: Address) -> Command:
        return self._query_command(short_address, self._instance_number)

    def get_write_commands(self, short_address: Address, value_to_set: int) -> list[Command]:
        return [
            DTR0(value_to_set),
            self._set_command(short_address, self._instance_number),
        ]
