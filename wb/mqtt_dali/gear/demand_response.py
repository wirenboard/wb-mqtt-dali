"""
Commands and responses from IEC 62386 part 221, Device Type 20
Demand response
"""

from dali import command
from dali.gear.general import _StandardCommand


class _DemandResponseCommand(_StandardCommand):
    devicetype = 20


class _DemandResponseReductionConfigCommand(_DemandResponseCommand):
    """A demand response reduction factor configuration command as defined in
    section 11.2 of IEC 62386-221:2018.
    """

    sendtwice = True


class QueryLoadSheddingCondition(_DemandResponseCommand):
    """Query the load shedding condition.

    0 - no load shedding
    1 - load shedding active
    """

    response = command.Response
    _cmdval = 0xF9


class QueryReductionFactor1(_DemandResponseCommand):
    """Query the reduction factor for load shedding level 1."""

    response = command.Response
    _cmdval = 0xFA


class QueryReductionFactor2(_DemandResponseCommand):
    """Query the reduction factor for load shedding level 2."""

    response = command.Response
    _cmdval = 0xFB


class QueryReductionFactor3(_DemandResponseCommand):
    """Query the reduction factor for load shedding level 3."""

    response = command.Response
    _cmdval = 0xFC


class SetLoadSheddingCondition(_DemandResponseCommand):
    """Set the load shedding condition.

    The command is sent once!
    """

    uses_dtr0 = True
    _cmdval = 0xE0


class SetReductionFactor1(_DemandResponseReductionConfigCommand):
    """Set the reduction factor 1."""

    uses_dtr0 = True
    _cmdval = 0xE1


class SetReductionFactor2(_DemandResponseReductionConfigCommand):
    """Set the reduction factor 2."""

    uses_dtr0 = True
    _cmdval = 0xE2


class SetReductionFactor3(_DemandResponseReductionConfigCommand):
    """Set the reduction factor 3."""

    uses_dtr0 = True
    _cmdval = 0xE3
