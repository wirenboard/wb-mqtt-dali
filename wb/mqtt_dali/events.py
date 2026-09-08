"""Bus-derived quantities: what the coordinator turns commands into and what the poll path
decodes its answers into.

Dispatched to every control of a device via ``notify``; each control decides for
itself whether the event concerns it, and updates its own ``control_info.state``.

Every read event carries the bus answer as is, ``ActiveEnergyRead`` excepted -- it is assembled
first: an empty payload means the read brought no value and comes with ``failed`` and only with
it, while MASK travels as the raw 255 it is.
Two invariants nothing enforces: ``settle_at`` (when the bus should have reached the commanded
state) comes with an ``OBSERVED`` event alone, ``failed`` with a ``READ`` alone.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from dali.command import Response
from dali.device.general import _Event

from .dali_type8_common import ColourComponent


class EventSource(Enum):
    """Where a level/colour event came from."""

    READ = "read"  # the outcome of a poll, successful or failed
    OBSERVED = "observed"  # predicted from a sniffed or own bus command


class BusEvent:  # pylint: disable=R0903
    """Base of everything dispatched through ``notify``."""


@dataclass(frozen=True)
class LevelChanged(BusEvent):
    """A device's raw actual level as the bus reports it, read or predicted.

    A control that reads itself schedules its confirming poll on ``settle_at``.
    """

    raw_level: Optional[int]
    source: EventSource
    settle_at: Optional[float] = None
    failed: bool = False


@dataclass(frozen=True)
class ColourChanged(BusEvent):
    """Raw DT8 colour components, as the handler's picture of the active colour type has them.

    A component absent from ``components`` keeps its control's last value; an observed command
    omits the ones nobody has named yet. A failure is always whole -- a failed subbatch ends
    the read cycle -- so ``failed`` comes with every component of the type carried empty.
    """

    components: dict[ColourComponent, Optional[int]]
    source: EventSource
    settle_at: Optional[float] = None
    failed: bool = False


@dataclass(frozen=True)
class Dali2InputEvent(BusEvent):
    """A DALI-2 input-device event, carried as the native ``python-dali`` object.

    Nothing here parses it; the instance it came from is its own ``instance_number``.
    """

    event: _Event


@dataclass(frozen=True)
class QuantityRead(BusEvent):
    """The read of a quantity with a single MQTT representation, successful or failed.

    The response is carried undecoded, and the quantity is the event's class -- so the two
    thermal protections of one device are told apart despite reading the same command.
    """

    response: Optional[Response]
    failed: bool


@dataclass(frozen=True)
class StatusRead(QuantityRead):
    """Gear status (``error_status``)."""


@dataclass(frozen=True)
class SwitchStatusRead(QuantityRead):
    """DT7 switching-function status (``last_acted``)."""


@dataclass(frozen=True)
class ThermalGearProtectionRead(QuantityRead):
    """DT16 thermal gear protection."""


@dataclass(frozen=True)
class ThermalLampProtectionRead(QuantityRead):
    """DT21 thermal lamp protection."""


@dataclass(frozen=True)
class LoadSheddingRead(QuantityRead):
    """DT20 demand-response load-shedding condition."""


@dataclass(frozen=True)
class PowerSupplyRead(QuantityRead):
    """DT49 integrated power supply."""


@dataclass(frozen=True)
class ActiveEnergyRead(BusEvent):
    """DT51 active-energy totalizer, already assembled from its memory reads into kWh."""

    kwh: Optional[float]
    failed: bool
