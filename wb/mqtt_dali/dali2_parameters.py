import logging
from typing import Callable, Optional

from dali.address import Address, InstanceNumber
from dali.command import Command
from dali.device.general import DTR0

from .settings import NumberSettingsParam, SettingsParamName
from .wbdali import WBDALIDriver


class InstanceParam(NumberSettingsParam):
    def __init__(  # pylint: disable=too-many-arguments, R0917
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

    async def read(
        self, driver: WBDALIDriver, short_address: Address, logger: Optional[logging.Logger] = None
    ) -> dict:
        try:
            return await super().read(driver, short_address, logger)
        except RuntimeError:
            # If the device doesn't support this parameter,
            # return default value instead of raising an error.
            return {self.property_name: self._get_default_value()}

    async def write(
        self,
        driver: WBDALIDriver,
        short_address: Address,
        value: dict,
        logger: Optional[logging.Logger] = None,
    ) -> dict:
        try:
            return await super().write(driver, short_address, value, logger)
        except RuntimeError:
            # If the device doesn't support this parameter,
            # return default value instead of raising an error.
            return {self.property_name: self._get_default_value()}

    def get_read_command(self, short_address: Address) -> Command:
        return self._query_command(short_address, self._instance_number)

    def get_write_commands(self, short_address: Address, value_to_set: int) -> list[Command]:
        return [
            DTR0(value_to_set),
            self._set_command(short_address, self._instance_number),
        ]

    def _get_default_value(self) -> int:
        if self.default is not None:
            return self.default
        if self.minimum is not None:
            return self.minimum
        return 0
