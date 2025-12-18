"""
Commands and responses from IEC 62386 part 218, Device Type 17
Dimming curve selection
"""

from dali import command
from dali.gear.general import _StandardCommand


class _DimmingCurveCommand(_StandardCommand):
    devicetype = 17


class _DimmingCurveConfigCommand(_DimmingCurveCommand):
    """A dimming curve configuration command as defined in
    section 11.2 of IEC 62386-218:2018.
    """

    sendtwice = True


class QueryDimmingCurve(_DimmingCurveCommand):
    """Query the dimming curve currently in use.

    0 - standard
    1 - linear
    """

    response = command.Response
    _cmdval = 0xEE


class SelectDimmingCurve(_DimmingCurveConfigCommand):
    """Select Dimming Curve

    If DTR0 is 0 then selects the standard logarithmic curve

    If DTR0 is 1 then selects a linear dimming curve

    Other values of DTR0 are reserved and will not change the dimming
    curve.  The setting is stored in non-volatile memory and is not
    cleared by the Reset command.
    """

    _cmdval = 0xE3
