"""Simulated DALI units, built on python-dali's test fakes.

``dali.tests.fakes`` models most of IEC 62386-102 but was written for
python-dali's own tests, which never run a full commissioning cycle and never
drive a configuration UI. The gaps that matter to us are filled here rather
than by patching the vendored copy:

* ``QUERY SHORT ADDRESS`` (0xBB) — the query that reads a selected device's
  short address during the binary search. Without it every device the search
  isolates looks silent, and commissioning readdresses the whole bus.
* A factory random address. ``fakes.Gear`` starts at random address 0 and only
  picks one when it is sent RANDOMISE, so an untouched bus looks like a bus
  where every unit has the same address.
* ``QUERY COLOUR STATUS`` (248) and the colour-type half of ``QUERY COLOUR
  VALUE`` — the DT8 queries wb-mqtt-dali uses to decide which colour features a
  unit has and which colour type is active. The fake answers only the colour
  temperature values, so opening a DT8 device in the editor failed outright.
* The standard gear variables of IEC 62386-102 §9.10 — power-on level, system
  failure level, fade time and rate, fast fade time — and the DT6 dimming curve.
  Real gear always answers these, the editor shows all of them, and a single
  unanswered query in a batch fails the whole parameter read.
* The whole DALI-2 commissioning state machine of IEC 62386-103 §11. `fakes.Device`
  models instances, DTRs and memory banks but not addressing, so a bus scan could
  never find an input device — half of what the editor exists to configure.
"""

from __future__ import annotations

import random
from typing import Optional

from dali.address import DeviceShort, InstanceNumber
from dali.device import general as control_device
from dali.device import pushbutton
from dali.frame import ForwardFrame, Frame
from dali.gear import colour
from dali.gear import general as control_gear
from dali.gear import led
from dali.gear.colour import QueryColourValueDTR
from dali.tests import fakes

MASK = 0xFF

# A backward frame of 0xFF is the DALI "yes".
_YES = 0xFF

# IEC 62386-209 §11.3.4.3, QUERY COLOUR STATUS.
COLOUR_STATUS_TC_OUT_OF_RANGE = 1 << 1
COLOUR_STATUS_TC_ACTIVE = 1 << 5

# IEC 62386-209 §9.11, colour type codes.
COLOUR_TYPE_COLOUR_TEMPERATURE = 0x20

DEVICE_TYPE_COLOUR = 8
DEVICE_TYPE_LED = 6

# IEC 62386-102 Table 9: the reset value of each gear variable.
DEFAULT_POWER_ON_LEVEL = 254
DEFAULT_SYSTEM_FAILURE_LEVEL = 254
DEFAULT_FADE_TIME = 0
DEFAULT_FADE_RATE = 7
DEFAULT_FAST_FADE_TIME = 0


class SimulatedControlGear(fakes.Gear):
    """One DALI control gear unit — a ballast or LED driver.

    :param shortaddr: short address 0..63, or ``None`` for an unaddressed unit
    :param random_address: the 24-bit random address the unit left the factory with
    """

    def __init__(
        self,
        shortaddr: Optional[int] = None,
        random_address: Optional[int] = None,
        colour_temperature: Optional[int] = None,
        **kwargs,
    ):
        super().__init__(shortaddr=shortaddr, **kwargs)
        if random_address is not None:
            self.randomaddr = Frame(24, random_address)
        if DEVICE_TYPE_COLOUR in self.devicetypes:
            # `fakes.Gear` powers up reporting colour temperature 0, which is not
            # a colour: the editor converts mireds to kelvin by dividing into
            # them, so a zero makes the daemon's poll loop raise on every pass —
            # and it retries a failing poll every millisecond, which is enough to
            # wedge the browser tab it runs in. Real gear powers up at a colour.
            self.actual_ct = colour_temperature or self.ct_mired_max
            self.temp_ct = self.actual_ct
        self.power_on_level = DEFAULT_POWER_ON_LEVEL
        self.system_failure_level = DEFAULT_SYSTEM_FAILURE_LEVEL
        self.fade_time = DEFAULT_FADE_TIME
        self.fade_rate = DEFAULT_FADE_RATE
        self.fast_fade_time = DEFAULT_FAST_FADE_TIME
        self.dimming_curve = 0  # 0 standard (logarithmic), 1 linear

    def send(self, cmd):
        # QUERY COLOUR VALUE selects what to read through DTR0, and the fake
        # overwrites DTR0 with the value it returns — so the selector has to be
        # read before the fake gets the command.
        requested_colour_value = self.dtr0

        answer = super().send(cmd)
        if answer is not None:
            return answer
        # `fakes.Gear.send` has already applied the address filter by this point;
        # re-checking is what keeps this override from answering for other units.
        if not self.valid_address(cmd):
            return None
        if isinstance(cmd, control_gear.QueryShortAddress):
            return self._query_short_address()

        stored = self._store_gear_variable(cmd)
        if stored:
            return None
        answer = self._query_gear_variable(cmd)
        if answer is not None:
            return answer

        if DEVICE_TYPE_LED in self.devicetypes:
            if isinstance(cmd, led.SelectDimmingCurve):
                self.dimming_curve = self.dtr0
                return None
            if isinstance(cmd, led.QueryDimmingCurve):
                return self.dimming_curve
            if isinstance(cmd, led.StoreDTRAsFastFadeTime):
                self.fast_fade_time = min(self.dtr0, 27)
                return None
            if isinstance(cmd, led.QueryFastFadeTime):
                return self.fast_fade_time

        if DEVICE_TYPE_COLOUR not in self.devicetypes:
            return None
        if isinstance(cmd, colour.QueryColourStatus):
            return self._query_colour_status()
        if isinstance(cmd, colour.QueryColourValue):
            return self._query_colour_value(requested_colour_value)
        return None

    def _store_gear_variable(self, cmd) -> bool:
        """The DTR0-carried setters of IEC 62386-102 §11.4."""
        if isinstance(cmd, control_gear.SetPowerOnLevel):
            self.power_on_level = self.dtr0
        elif isinstance(cmd, control_gear.SetSystemFailureLevel):
            self.system_failure_level = self.dtr0
        elif isinstance(cmd, control_gear.SetFadeTime):
            self.fade_time = self.dtr0
        elif isinstance(cmd, control_gear.SetFadeRate):
            self.fade_rate = self.dtr0
        else:
            return False
        return True

    def _query_gear_variable(self, cmd) -> Optional[int]:
        if isinstance(cmd, control_gear.QueryPowerOnLevel):
            return self.power_on_level
        if isinstance(cmd, control_gear.QuerySystemFailureLevel):
            return self.system_failure_level
        if isinstance(cmd, control_gear.QueryFadeTimeFadeRate):
            # One byte carries both: fade time in the high nibble, rate in the low.
            return ((self.fade_time & 0x0F) << 4) | (self.fade_rate & 0x0F)
        return None

    def _query_short_address(self) -> Optional[int]:
        """IEC 62386-102 §11.9: answer only while selected by the search address."""
        if not self.initialising or self.randomaddr != self.searchaddr:
            return None
        if self.shortaddr is None:
            return MASK
        return (self.shortaddr << 1) | 1

    def _query_colour_status(self) -> int:
        """The fake models colour temperature only, so Tc is the active colour type.

        A unit that has never been given a colour reports Tc out of range, which
        is what real gear does before its first ACTIVATE.
        """
        status = COLOUR_STATUS_TC_ACTIVE
        if not self.ct_mired_min <= self.actual_ct <= self.ct_mired_max:
            status |= COLOUR_STATUS_TC_OUT_OF_RANGE
        return status

    def _query_colour_value(self, selector: int) -> Optional[int]:
        """The colour-type selectors the fake does not answer.

        It covers the colour temperature values; these two report *which* colour
        type is in use, which is what the editor reads before deciding which
        colour controls to show.
        """
        if selector in (
            QueryColourValueDTR.ReportColourType.value,
            QueryColourValueDTR.TemporaryColourType.value,
        ):
            return COLOUR_TYPE_COLOUR_TEMPERATURE
        return None


# IEC 62386-103 §11.4.2, INITIALISE parameter. Note that these are the reverse
# of the control-gear meanings: for a control device MASK selects every device,
# and 0x7F selects the unaddressed ones.
INITIALISE_ALL = 0xFF
INITIALISE_UNADDRESSED = 0x7F

# Instance types, IEC 62386-3xx.
INSTANCE_TYPE_PUSHBUTTON = 1

# Event priorities: pushbuttons are a user action, other instance types are not
# (IEC 62386-301 §9.4.1, IEC 62386-103 §9.14.2).
EVENT_PRIORITY_PUSHBUTTON = 3
EVENT_PRIORITY_DEFAULT = 4

# The factory state of a simulated pushbutton's timers, in the raw units the
# commands carry: 20 ms per step for the three press timers, 1 s for the stuck
# timer. All are inside the ranges the editor accepts.
DEFAULT_SHORT_TIMER = 20
DEFAULT_DOUBLE_TIMER = 20
DEFAULT_REPEAT_TIMER = 20
DEFAULT_STUCK_TIMER = 20

# A device with no group assigned answers MASK, not 0 — group 0 is a real group.
NO_GROUP = 0xFF


class _InstanceSettings:
    """The configurable state of one instance of a control device."""

    def __init__(self, inst_type: int) -> None:
        self.inst_type = inst_type
        self.event_priority = (
            EVENT_PRIORITY_PUSHBUTTON if inst_type == INSTANCE_TYPE_PUSHBUTTON else EVENT_PRIORITY_DEFAULT
        )
        self.primary_group = NO_GROUP
        self.group1 = NO_GROUP
        self.group2 = NO_GROUP
        self.short_timer = DEFAULT_SHORT_TIMER
        self.short_timer_min = DEFAULT_SHORT_TIMER
        self.double_timer = DEFAULT_DOUBLE_TIMER
        self.double_timer_min = DEFAULT_DOUBLE_TIMER
        self.repeat_timer = DEFAULT_REPEAT_TIMER
        self.stuck_timer = DEFAULT_STUCK_TIMER


class SimulatedControlDevice(fakes.Device):
    """One DALI-2 control device — a wall switch, presence or light sensor.

    :param shortaddr: short address 0..63, or ``None`` for an unaddressed device
    :param random_address: the 24-bit random address the device left the factory with
    """

    def __init__(self, shortaddr: Optional[int] = None, random_address: Optional[int] = None, **kwargs):
        if isinstance(shortaddr, int):
            shortaddr = DeviceShort(shortaddr)
        super().__init__(shortaddr=shortaddr, **kwargs)
        self.randomaddr = Frame(24, random_address or 0)
        self.searchaddr = Frame(24)
        self.initialising = False
        self.withdrawn = False
        # `fakes.Device._instances` is a class attribute, so every fake device
        # shares one list: configuring one would configure all of them.
        self._instances = [
            fakes.Device.Instance(inst_type=inst.inst_type, scheme=inst.scheme, filter=inst.filter)
            for inst in type(self)._instances  # pylint: disable=protected-access
        ]
        self._instance_settings = [_InstanceSettings(inst.inst_type) for inst in self._instances]
        self.version_number = 2  # IEC 62386-103 edition 2

    @property
    def short_address(self) -> Optional[int]:
        """The short address as a plain number, or None when unaddressed."""
        return None if self.shortaddr is None else self.shortaddr.address

    def press(self, instance_number: int = 0) -> Optional[ForwardFrame]:
        """The frame this device puts on the bus when a button is pressed.

        A control device is a bus master in its own right: nobody asked for
        this frame, and it does not answer anything. It reaches the daemon only
        through the gateway's bus monitor ring, which is why the ring is worth
        polling at all.
        """
        if self.shortaddr is None or instance_number >= len(self._instances):
            return None
        if self._instances[instance_number].inst_type != INSTANCE_TYPE_PUSHBUTTON:
            return None
        event = pushbutton.ShortPress(short_address=self.shortaddr, instance_number=instance_number)
        return event.frame

    def send(self, cmd):
        answer = super().send(cmd)
        if answer is not None:
            return answer
        if not self.valid_address(cmd):
            return None
        answer = self._instance_command(cmd)
        if answer is not None:
            return answer
        return self._commission(cmd)

    def _instance_command(self, cmd):  # pylint: disable=too-many-return-statements
        """Per-instance settings of IEC 62386-103 §9 and -301 §9.

        `fakes.Device` answers the instance type, scheme and event filter but
        none of these, and `Editor/GetDevice` reads every one of them — a single
        unanswered query fails the whole batch and empties the form.
        """
        instance = self._settings_for(cmd)
        if instance is None:
            return None

        if isinstance(cmd, control_device.QueryEventPriority):
            return instance.event_priority
        if isinstance(cmd, control_device.SetEventPriority):
            instance.event_priority = self.dtr0
            return None
        if isinstance(cmd, control_device.QueryPrimaryInstanceGroup):
            return instance.primary_group
        if isinstance(cmd, control_device.SetPrimaryInstanceGroup):
            instance.primary_group = self.dtr0
            return None
        if isinstance(cmd, control_device.QueryInstanceGroup1):
            return instance.group1
        if isinstance(cmd, control_device.SetInstanceGroup1):
            instance.group1 = self.dtr0
            return None
        if isinstance(cmd, control_device.QueryInstanceGroup2):
            return instance.group2
        if isinstance(cmd, control_device.SetInstanceGroup2):
            instance.group2 = self.dtr0
            return None
        if isinstance(cmd, control_device.QueryFeatureType):
            return instance.inst_type

        if isinstance(cmd, pushbutton.QueryShortTimer):
            return instance.short_timer
        if isinstance(cmd, pushbutton.SetShortTimer):
            instance.short_timer = self.dtr0
            return None
        if isinstance(cmd, pushbutton.QueryShortTimerMin):
            return instance.short_timer_min
        if isinstance(cmd, pushbutton.QueryDoubleTimer):
            return instance.double_timer
        if isinstance(cmd, pushbutton.SetDoubleTimer):
            instance.double_timer = self.dtr0
            return None
        if isinstance(cmd, pushbutton.QueryDoubleTimerMin):
            return instance.double_timer_min
        if isinstance(cmd, pushbutton.QueryRepeatTimer):
            return instance.repeat_timer
        if isinstance(cmd, pushbutton.SetRepeatTimer):
            instance.repeat_timer = self.dtr0
            return None
        if isinstance(cmd, pushbutton.QueryStuckTimer):
            return instance.stuck_timer
        if isinstance(cmd, pushbutton.SetStuckTimer):
            instance.stuck_timer = self.dtr0
            return None
        return None

    def _settings_for(self, cmd) -> Optional["_InstanceSettings"]:
        instance = getattr(cmd, "instance", None)
        if not isinstance(instance, InstanceNumber):
            return None
        number = instance.value
        if number >= len(self._instance_settings):
            return None
        return self._instance_settings[number]

    def _commission(self, cmd):  # pylint: disable=too-many-return-statements
        """The addressing commands of IEC 62386-103 §11.

        Deliberately mirrors `fakes.Gear`'s control-gear equivalent, including
        the parts that make a binary search work: COMPARE answers while the
        random address is at or below the search address, and WITHDRAW only
        takes effect on the device the search has isolated.
        """
        if isinstance(cmd, control_device.Terminate):
            self.initialising = False
            self.withdrawn = False
        elif isinstance(cmd, control_device.Initialise):
            if cmd.param in (INITIALISE_ALL, self.short_address) or (
                cmd.param == INITIALISE_UNADDRESSED and self.shortaddr is None
            ):
                self.initialising = True
                self.withdrawn = False
        elif isinstance(cmd, control_device.Randomise):
            self.randomaddr = Frame(24, self._next_random_address())
        elif isinstance(cmd, control_device.Compare):
            if self._selectable() and self.randomaddr.as_integer <= self.searchaddr.as_integer:
                return _YES
        elif isinstance(cmd, control_device.Withdraw):
            if self._selected():
                self.withdrawn = True
        elif isinstance(cmd, control_device.SearchAddrH):
            self.searchaddr[23:16] = cmd.param
        elif isinstance(cmd, control_device.SearchAddrM):
            self.searchaddr[15:8] = cmd.param
        elif isinstance(cmd, control_device.SearchAddrL):
            self.searchaddr[7:0] = cmd.param
        elif isinstance(cmd, control_device.ProgramShortAddress):
            if self._selected():
                self.shortaddr = None if cmd.param == MASK else DeviceShort(cmd.param)
        elif isinstance(cmd, control_device.VerifyShortAddress):
            if self.initialising and self.short_address == cmd.param:
                return _YES
        elif isinstance(cmd, control_device.QueryShortAddress):
            if self._selected():
                return MASK if self.shortaddr is None else self.short_address
        elif isinstance(cmd, control_device.QueryRandomAddressH):
            return self.randomaddr[23:16]
        elif isinstance(cmd, control_device.QueryRandomAddressM):
            return self.randomaddr[15:8]
        elif isinstance(cmd, control_device.QueryRandomAddressL):
            return self.randomaddr[7:0]
        elif isinstance(cmd, control_device.QueryDeviceGroupsZeroToSeven):
            return self._group_byte(0)
        elif isinstance(cmd, control_device.QueryDeviceGroupsEightToFifteen):
            return self._group_byte(1)
        elif isinstance(cmd, control_device.QueryDeviceGroupsSixteenToTwentyThree):
            return self._group_byte(2)
        elif isinstance(cmd, control_device.QueryDeviceGroupsTwentyFourToThirtyOne):
            return self._group_byte(3)
        elif isinstance(cmd, control_device.QueryVersionNumber):
            return self.version_number
        return None

    def _group_byte(self, index: int) -> int:
        """Group membership, eight groups per answer.

        A control device has 32 groups (IEC 62386-103 §9.7), read back as four
        bytes; control gear has 16. `fakes.Device` tracks the set but never
        reports it, and an unanswered query fails the whole parameter batch.
        """
        first = index * 8
        bits = 0
        for group in self.groups:
            number = group.group if hasattr(group, "group") else int(group)
            if first <= number < first + 8:
                bits |= 1 << (number - first)
        return bits

    def _selectable(self) -> bool:
        return self.initialising and not self.withdrawn

    def _selected(self) -> bool:
        """True for the one device the search address currently isolates."""
        return self.initialising and self.randomaddr == self.searchaddr

    def _next_random_address(self) -> int:
        return random.randrange(0, 0x1000000)
