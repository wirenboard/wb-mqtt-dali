"""
Commands and responses from IEC 62386 part 250, Device Type 49
Integrated power supply
"""

from dali import command
from dali.gear.general import _StandardCommand


class _IntegratedPowerSupplyCommand(_StandardCommand):
    devicetype = 49


class QueryActivePowerSupply(_IntegratedPowerSupplyCommand):
    """Query integrated power supply active status."""

    response = command.YesNoResponse
    _cmdval = 0xFE
