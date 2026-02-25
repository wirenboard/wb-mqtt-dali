"""
Commands, responses and events from IEC 62386 part 306: "Input devices —
General purpose sensor"
"""

from __future__ import annotations

from dali import command
from dali.device import general

# "6" corresponds with "Part 306", as per Table 4 of IEC 62386 part 103
instance_type = 6


###############################################################################
# Commands from Part 303 Table 10 start here
###############################################################################


class _GeneralPurposeSensorCommand(general._StandardInstanceCommand):
    """
    An extension of the standard commands, addressed to an absolute input control
    device instance
    """

    _opcode = None


class SetAlarmReportTimer(_GeneralPurposeSensorCommand):
    inputdev = True
    uses_dtr0 = True
    sendtwice = True
    _opcode = 0x51


class SetDeadtimeTimer(_GeneralPurposeSensorCommand):
    inputdev = True
    uses_dtr0 = True
    sendtwice = True
    _opcode = 0x53


class SetHysteresis(_GeneralPurposeSensorCommand):
    inputdev = True
    uses_dtr0 = True
    sendtwice = True
    _opcode = 0x52


class SetHysteresisMin(_GeneralPurposeSensorCommand):
    inputdev = True
    uses_dtr0 = True
    sendtwice = True
    _opcode = 0x54


class SetReportTimer(_GeneralPurposeSensorCommand):
    inputdev = True
    uses_dtr0 = True
    sendtwice = True
    _opcode = 0x50


class QueryAlarmReportTimer(_GeneralPurposeSensorCommand):
    inputdev = True
    response = command.NumericResponse
    _opcode = 0x5D


class QueryDeadtimeTimer(_GeneralPurposeSensorCommand):
    inputdev = True
    response = command.NumericResponse
    _opcode = 0x5B


class QueryHysteresis(_GeneralPurposeSensorCommand):
    inputdev = True
    response = command.NumericResponse
    _opcode = 0x5E


class QueryHysteresisMin(_GeneralPurposeSensorCommand):
    inputdev = True
    response = command.NumericResponse
    _opcode = 0x5A


class QueryReportTimer(_GeneralPurposeSensorCommand):
    inputdev = True
    response = command.NumericResponse
    _opcode = 0x5C
