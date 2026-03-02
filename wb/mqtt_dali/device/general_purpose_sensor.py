"""
Commands, responses and events from IEC 62386 part 306: "Input devices —
General purpose sensor"
"""

from __future__ import annotations

from dali import command
from dali.device import general

# "6" corresponds with "Part 306", as per Table 4 of IEC 62386 part 103
instance_type = 6  # pylint: disable=C0103


###############################################################################
# Commands from Part 303 Table 10 start here
###############################################################################


class _GeneralPurposeSensorCommand(general._StandardInstanceCommand):  # pylint: disable=W0212
    """
    An extension of the standard commands, addressed to an absolute input control
    device instance
    """

    _opcode = None


class SetReportTimer(_GeneralPurposeSensorCommand):
    inputdev = True
    uses_dtr0 = True
    sendtwice = True
    _opcode = 0x50


class SetAlarmReportTimer(_GeneralPurposeSensorCommand):
    inputdev = True
    uses_dtr0 = True
    sendtwice = True
    _opcode = 0x51


class SetHysteresis(_GeneralPurposeSensorCommand):
    inputdev = True
    uses_dtr0 = True
    sendtwice = True
    _opcode = 0x52


class SetDeadtimeTimer(_GeneralPurposeSensorCommand):
    inputdev = True
    uses_dtr0 = True
    sendtwice = True
    _opcode = 0x53


class SetHysteresisMin(_GeneralPurposeSensorCommand):
    inputdev = True
    uses_dtr0 = True
    sendtwice = True
    _opcode = 0x54


class SetAlarmType(_GeneralPurposeSensorCommand):
    inputdev = True
    uses_dtr0 = True
    sendtwice = True
    _opcode = 0x55


class SetMagnitude(_GeneralPurposeSensorCommand):
    inputdev = True
    uses_dtr0 = True
    sendtwice = True
    _opcode = 0x56


class SetAlarm(_GeneralPurposeSensorCommand):
    inputdev = True
    uses_dtr0 = True
    uses_dtr1 = True
    uses_dtr2 = True
    sendtwice = True
    _opcode = 0x57


class SetAlarmHysteresis(_GeneralPurposeSensorCommand):
    inputdev = True
    uses_dtr0 = True
    uses_dtr1 = True
    uses_dtr2 = True
    sendtwice = True
    _opcode = 0x58


class QueryHysteresisMin(_GeneralPurposeSensorCommand):
    inputdev = True
    response = command.NumericResponse
    _opcode = 0x5A


class QueryDeadtimeTimer(_GeneralPurposeSensorCommand):
    inputdev = True
    response = command.NumericResponse
    _opcode = 0x5B


class QueryReportTimer(_GeneralPurposeSensorCommand):
    inputdev = True
    response = command.NumericResponse
    _opcode = 0x5C


class QueryAlarmReportTimer(_GeneralPurposeSensorCommand):
    inputdev = True
    response = command.NumericResponse
    _opcode = 0x5D


class QueryHysteresis(_GeneralPurposeSensorCommand):
    inputdev = True
    response = command.NumericResponse
    _opcode = 0x5E


class QueryMeasurementVariable(_GeneralPurposeSensorCommand):
    inputdev = True
    uses_dtr0 = True
    response = command.NumericResponse
    _opcode = 0x5F
