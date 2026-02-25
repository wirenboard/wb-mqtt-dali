"""
Commands, responses and events from IEC 62386 part 332: "Input devices —
Feedback"
"""

from __future__ import annotations

from dali import command
from dali.device import general

# "32" corresponds with "Part 332", as per Table 4 of IEC 62386 part 103
instance_type = 32


###############################################################################
# Commands from Part 303 Table 10 start here
###############################################################################


class _FeedbackCommand(general._StandardInstanceCommand):
    """
    An extension of the standard commands, addressed to a feedback control
    device instance
    """

    _opcode = None


class SetFeedbackTiming(_FeedbackCommand):
    inputdev = True
    uses_dtr0 = True
    sendtwice = True
    _opcode = 0x12


class SetActiveFeedbackBrightness(_FeedbackCommand):
    inputdev = True
    uses_dtr0 = True
    sendtwice = True
    _opcode = 0x13


class SetActiveFeedbackColour(_FeedbackCommand):
    inputdev = True
    uses_dtr0 = True
    sendtwice = True
    _opcode = 0x14


class SetInactiveFeedbackBrightness(_FeedbackCommand):
    inputdev = True
    uses_dtr0 = True
    sendtwice = True
    _opcode = 0x15


class SetInactiveFeedbackColour(_FeedbackCommand):
    inputdev = True
    uses_dtr0 = True
    sendtwice = True
    _opcode = 0x16


class SetActiveFeedbackVolume(_FeedbackCommand):
    inputdev = True
    uses_dtr0 = True
    sendtwice = True
    _opcode = 0x17


class SetActiveFeedbackPitch(_FeedbackCommand):
    inputdev = True
    uses_dtr0 = True
    sendtwice = True
    _opcode = 0x18


class QueryFeedbackTiming(_FeedbackCommand):
    inputdev = True
    response = command.NumericResponse
    _opcode = 0x2D


class QueryActiveFeedbackBrightness(_FeedbackCommand):
    inputdev = True
    response = command.NumericResponse
    _opcode = 0x2C


class QueryActiveFeedbackColour(_FeedbackCommand):
    inputdev = True
    response = command.NumericResponse
    _opcode = 0x2B


class QueryInactiveFeedbackBrightness(_FeedbackCommand):
    inputdev = True
    response = command.NumericResponse
    _opcode = 0x2A


class QueryInactiveFeedbackColour(_FeedbackCommand):
    inputdev = True
    response = command.NumericResponse
    _opcode = 0x29


class QueryActiveFeedbackVolume(_FeedbackCommand):
    inputdev = True
    response = command.NumericResponse
    _opcode = 0x28


class QueryActiveFeedbackPitch(_FeedbackCommand):
    inputdev = True
    response = command.NumericResponse
    _opcode = 0x27
