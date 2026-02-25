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
    pass  # TODO


class SetDeadtimeTimer(_GeneralPurposeSensorCommand):
    pass  # TODO


class SetHysteresis(_GeneralPurposeSensorCommand):
    pass  # TODO


class SetHysteresisMin(_GeneralPurposeSensorCommand):
    pass  # TODO


class SetReportTimer(_GeneralPurposeSensorCommand):
    pass  # TODO


class QueryAlarmReportTimer(_GeneralPurposeSensorCommand):
    pass  # TODO


class QueryDeadtimeTimer(_GeneralPurposeSensorCommand):
    pass  # TODO


class QueryHysteresis(_GeneralPurposeSensorCommand):
    pass  # TODO


class QueryHysteresisMin(_GeneralPurposeSensorCommand):
    pass  # TODO


class QueryReportTimer(_GeneralPurposeSensorCommand):
    pass  # TODO
