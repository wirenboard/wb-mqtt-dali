"""
Commands and responses from IEC 62386 part 208, Device Type 7
Switching function
"""

from dali import command
from dali.gear.general import _StandardCommand


class _SwitchingFunctionCommand(_StandardCommand):
    devicetype = 7


class _SwitchingFunctionConfigCommand(_SwitchingFunctionCommand):
    """A switching function configuration command as defined in
    section 11.2 of IEC 62386-208:2009.
    """

    sendtwice = True


class SwitchingFunctionFeaturesResponse(command.BitmapResponse):
    bits = [
        "load error can be queried",
        None,
        None,
        "adjustable thresholds",
        "adjustable hold-off time",
        None,
        "Reference system power supported",
        "physical selection supported",
    ]


class QueryFeatures(_SwitchingFunctionCommand):
    """Query the supported features of the switching function."""

    response = SwitchingFunctionFeaturesResponse
    _cmdval = 0xF0


class SwitchingFunctionSwitchStatusResponse(command.NumericResponse):
    @property
    def load_error_detected(self) -> bool:
        if self._value:
            return self._value.as_integer & 0x01 == 0x01
        return False

    @property
    def error_detection_in_hold_off(self) -> bool:
        if self._value:
            return (self._value.as_integer >> 1) & 0x01 == 0x01
        return False

    @property
    def last_acted_up_switch_on(self) -> bool:
        if self._value:
            return (self._value.as_integer >> 2) & 0x03 == 0x00
        return False

    @property
    def last_acted_up_switch_off(self) -> bool:
        if self._value:
            return (self._value.as_integer >> 2) & 0x03 == 0x01
        return False

    @property
    def last_acted_down_switch_on(self) -> bool:
        if self._value:
            return (self._value.as_integer >> 2) & 0x03 == 0x02
        return False

    @property
    def last_acted_down_switch_off(self) -> bool:
        if self._value:
            return (self._value.as_integer >> 2) & 0x03 == 0x03
        return False

    @property
    def reference_measurement_failed(self) -> bool:
        if self._value:
            return (self._value.as_integer >> 7) & 0x01 == 0x01
        return False


class QuerySwitchStatus(_SwitchingFunctionCommand):
    """Query the current status of the switching function."""

    response = SwitchingFunctionSwitchStatusResponse
    _cmdval = 0xF1


class QueryUpSwitchOnThreshold(_SwitchingFunctionCommand):
    """Query the up switch on threshold."""

    response = command.NumericResponseMask
    _cmdval = 0xF2


class QueryUpSwitchOffThreshold(_SwitchingFunctionCommand):
    """Query the up switch off threshold."""

    response = command.NumericResponseMask
    _cmdval = 0xF3


class QueryDownSwitchOnThreshold(_SwitchingFunctionCommand):
    """Query the down switch on threshold."""

    response = command.NumericResponseMask
    _cmdval = 0xF4


class QueryDownSwitchOffThreshold(_SwitchingFunctionCommand):
    """Query the down switch off threshold."""

    response = command.NumericResponseMask
    _cmdval = 0xF5


class QueryErrorHoldOffTime(_SwitchingFunctionCommand):
    """Query the error holdoff time."""

    response = command.NumericResponseMask
    _cmdval = 0xF6


class StoreDTRAsUpSwitchOnThreshold(_SwitchingFunctionConfigCommand):
    """Store DTR0 as up switch on threshold

    If 255 (MASK) is stored, the threshold shall not be used for comparison.
    """

    uses_dtr0 = True
    _cmdval = 0xE1


class StoreDTRAsUpSwitchOffThreshold(_SwitchingFunctionConfigCommand):
    """Store DTR0 as up switch off threshold

    If 255 (MASK) is stored, the threshold shall not be used for comparison.
    """

    uses_dtr0 = True
    _cmdval = 0xE2


class StoreDTRAsDownSwitchOnThreshold(_SwitchingFunctionConfigCommand):
    """Store DTR0 as down switch on threshold

    If 255 (MASK) is stored, the threshold shall not be used for comparison.
    """

    uses_dtr0 = True
    _cmdval = 0xE3


class StoreDTRAsDownSwitchOffThreshold(_SwitchingFunctionConfigCommand):
    """Store DTR0 as down switch off threshold

    If 255 (MASK) is stored, the threshold shall not be used for comparison.
    """

    uses_dtr0 = True
    _cmdval = 0xE4


class StoreDTRAsErrorHoldOffTime(_SwitchingFunctionConfigCommand):
    """Store DTR0 as error holdoff time

    If 0 is stored, a load error shall be indicated immediately.
    If 255 (MASK) is stored, a load error shall not be indicated.
    """

    uses_dtr0 = True
    _cmdval = 0xE5
