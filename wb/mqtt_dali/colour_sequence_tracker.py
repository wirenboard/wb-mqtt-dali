"""Accumulates a Type-8 colour sequence sniffed/sent on the bus.

A colour change is a sequence: write DTR0/1/2 -> ``SetTemporary…`` (moves the DTR
word into a temporary register) -> ``Activate`` (applies it, fading). This tracker
copies the snapshot DTR word into a per-device pending capture on each
``SetTemporary…`` and hands the captured components to the colour control on
``Activate``. A component whose required DTR registers were not seen written since
the last Activate (sequence started mid-stream / dropped frame), or a
``SetTemporaryRGBWAFControl`` (a channel-control byte, not a colour value), marks
the capture unpredictable -> poll only.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from dali.command import Command
from dali.gear.colour import (
    SetTemporaryColourTemperature,
    SetTemporaryPrimaryNDimLevel,
    SetTemporaryRGBDimLevel,
    SetTemporaryRGBWAFControl,
    SetTemporaryWAFDimLevel,
    SetTemporaryXCoordinate,
    SetTemporaryYCoordinate,
)

from .dali_type8_common import PRIMARY_N_BY_INDEX, ColourComponent
from .dtr_snapshot import DtrSnapshot

CapturedComponents = dict[ColourComponent, int]


@dataclass
class ColourCapture:
    components: CapturedComponents = field(default_factory=dict)
    # False once a non-predictable control byte or an incomplete DTR sequence is seen.
    predictable: bool = True


def _capture_xy_x(capture: ColourCapture, snapshot: DtrSnapshot) -> None:
    capture.components[ColourComponent.X_COORDINATE] = snapshot.word


def _capture_xy_y(capture: ColourCapture, snapshot: DtrSnapshot) -> None:
    capture.components[ColourComponent.Y_COORDINATE] = snapshot.word


def _capture_tc(capture: ColourCapture, snapshot: DtrSnapshot) -> None:
    capture.components[ColourComponent.COLOUR_TEMPERATURE] = snapshot.word


def _capture_rgb(capture: ColourCapture, snapshot: DtrSnapshot) -> None:
    capture.components[ColourComponent.RED] = snapshot.dtr0
    capture.components[ColourComponent.GREEN] = snapshot.dtr1
    capture.components[ColourComponent.BLUE] = snapshot.dtr2


def _capture_waf(capture: ColourCapture, snapshot: DtrSnapshot) -> None:
    capture.components[ColourComponent.WHITE] = snapshot.dtr0
    capture.components[ColourComponent.AMBER] = snapshot.dtr1
    capture.components[ColourComponent.FREE_COLOUR] = snapshot.dtr2


def _capture_primary_n(capture: ColourCapture, snapshot: DtrSnapshot) -> None:
    # DTR2 is a raw byte off a shared bus: a foreign controller or a mis-decoded frame can
    # leave anything there.
    component = PRIMARY_N_BY_INDEX.get(snapshot.dtr2)
    if component is None:
        capture.predictable = False
        return
    capture.components[component] = snapshot.word


# SetTemporary class -> (required DTR registers, capture builder).
_SET_TEMPORARY_SPECS: dict[type, tuple[frozenset[int], Callable[[ColourCapture, DtrSnapshot], None]]] = {
    SetTemporaryXCoordinate: (frozenset({0, 1}), _capture_xy_x),
    SetTemporaryYCoordinate: (frozenset({0, 1}), _capture_xy_y),
    SetTemporaryColourTemperature: (frozenset({0, 1}), _capture_tc),
    SetTemporaryRGBDimLevel: (frozenset({0, 1, 2}), _capture_rgb),
    SetTemporaryWAFDimLevel: (frozenset({0, 1, 2}), _capture_waf),
    SetTemporaryPrimaryNDimLevel: (frozenset({0, 1, 2}), _capture_primary_n),
}


class ColourSequenceTracker:
    def __init__(self) -> None:
        self._pending: dict[str, ColourCapture] = {}  # device uid -> capture
        self._fresh_dtr: set[int] = set()  # registers written since the last Activate

    def note_dtr(self, register: int) -> None:
        if not self._fresh_dtr:
            # First DTR write of a new transaction (the previous one ended at its
            # Activate, which clears _fresh_dtr): drop captures abandoned by a prior
            # transaction whose Activate was missed, so a stale or poisoned
            # (predictable=False) capture can't bleed into this fresh sequence.
            self._pending.clear()
        self._fresh_dtr.add(register)

    def record(self, device_uid: str, command: Command, snapshot: DtrSnapshot) -> None:
        capture = self._pending.setdefault(device_uid, ColourCapture())
        if isinstance(command, SetTemporaryRGBWAFControl):
            capture.predictable = False
            return
        spec = _SET_TEMPORARY_SPECS.get(type(command))
        if spec is None:
            return
        required, builder = spec
        if not required.issubset(self._fresh_dtr):
            capture.predictable = False
            return
        builder(capture, snapshot)

    def take(self, device_uid: str) -> Optional[ColourCapture]:
        return self._pending.pop(device_uid, None)

    def end_activate(self) -> None:
        """The DTR word that fed this transaction is consumed once Activate is handled."""
        self._fresh_dtr.clear()
