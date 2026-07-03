"""Time-to-settle for a confirming poll after a state-changing command.

The fade-code -> seconds table used to live only in the JSON schema; it is brought
into code here so settle can be computed from a device's tracked ``fade_time``.
"""

import enum
from typing import Optional

# IEC 62386-102 fade-time code -> seconds (matches the schema's enum_titles; code 0 = "no fade").
_FADE_TIME_SECONDS: dict[int, float] = {
    0: 0.0,
    1: 0.7,
    2: 1.0,
    3: 1.4,
    4: 2.0,
    5: 2.8,
    6: 4.0,
    7: 5.7,
    8: 8.0,
    9: 11.3,
    10: 16.0,
    11: 22.6,
    12: 32.0,
    13: 45.3,
    14: 64.0,
    15: 90.5,
}

# Slack added after a fade/step so the confirming read lands on the settled value,
# not on the trailing edge of the transition.
_SETTLE_MARGIN_S = 0.3
# Up/Down run a fixed ~200 ms ramp (magnitude unknown, so the level is not predicted).
_STEP_WINDOW_S = 0.2
# Used for a fading command whose fade_time is unknown.
_DEFAULT_FADE_DELAY_S = 6.0


class SettleBasis(enum.Enum):
    """How long a command takes to reach its settled value."""

    IMMEDIATE = enum.auto()  # ~0: no fade, only the margin
    STEP_WINDOW = enum.auto()  # Up/Down ~200 ms ramp
    FADE = enum.auto()  # device fade_time + margin


class SettleClock:
    """Maps a command's settle basis (+ the device fade code) to seconds of slack."""

    def settle_for(self, basis: SettleBasis, fade_time_code: Optional[int] = None) -> float:
        if basis is SettleBasis.IMMEDIATE:
            return _SETTLE_MARGIN_S
        if basis is SettleBasis.STEP_WINDOW:
            return _STEP_WINDOW_S + _SETTLE_MARGIN_S
        seconds = self.fade_seconds(fade_time_code)
        if seconds is None:
            return _DEFAULT_FADE_DELAY_S
        return seconds + _SETTLE_MARGIN_S

    @staticmethod
    def fade_seconds(fade_time_code: Optional[int]) -> Optional[float]:
        if fade_time_code is None:
            return None
        return _FADE_TIME_SECONDS.get(fade_time_code)
