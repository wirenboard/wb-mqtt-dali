"""MQTT controls that own DT8 colour components.

Kept out of ``dali_type8_common`` so that module stays free of the MQTT layer: ``events``
imports it for ``ColourComponent``.
"""

from typing import Callable, Iterable, Optional

from dali.address import Address
from dali.command import Command

from .common_dali_device import MqttControl, NotifyResult
from .dali_type8_common import ColourComponent, is_unset_component_value
from .device_publisher import ControlInfo
from .events import BusEvent, ColourChanged, EventSource


class ColourComponentControl(MqttControl):
    """Owns a subset of DT8 colour components and formats whatever a ``ColourChanged`` carries.

    The colour picture belongs to the DT8 handler, so nothing accumulates here: an event that
    does not carry all of this control's components leaves it alone.

    A component carried empty (the read cycle failed) or carrying no colour value (the gear
    would not name it) is not a measurement: the reading control puts ``ControlError.READ`` on
    itself, while its writable ``set_*`` mirror means "requested", not "measured": it keeps its
    value, and a successful read only settles its errors.
    """

    def __init__(
        self,
        control_info: ControlInfo,
        components: Iterable[ColourComponent],
        commands_builder: Optional[Callable[[Address, str], list[Command]]] = None,
        is_group_state_control: bool = False,
    ) -> None:
        super().__init__(
            control_info,
            commands_builder=commands_builder,
            is_group_state_control=is_group_state_control,
        )
        self._components = tuple(components)

    def notify(self, event: BusEvent) -> NotifyResult:  # pylint: disable=too-many-return-statements
        if not isinstance(event, ColourChanged):
            return NotifyResult.NOTHING_TO_PUBLISH
        mine = {c: event.components[c] for c in self._components if c in event.components}
        if len(mine) < len(self._components):
            return NotifyResult.NOTHING_TO_PUBLISH
        if event.source is EventSource.OBSERVED and self.control_info.state.has_read_error():
            return NotifyResult.NOTHING_TO_PUBLISH
        if any(raw is None or is_unset_component_value(c, raw) for c, raw in mine.items()):
            if self.is_writable():
                if self.control_info.state.error and event.source is EventSource.READ and not event.failed:
                    self.control_info.state.clear_errors()
                    return NotifyResult.PUBLISH_STATE
                return NotifyResult.NOTHING_TO_PUBLISH
            self.control_info.state.set_read_error()
            return NotifyResult.PUBLISH_STATE
        self.control_info.state.value = self._format(mine)
        if event.source is EventSource.READ:
            self.control_info.state.clear_errors()
        return NotifyResult.PUBLISH_STATE

    # --- Hooks for subclasses ---

    def _format(self, components: dict[ColourComponent, int]) -> str:
        raise NotImplementedError


class SingleComponentColourControl(ColourComponentControl):
    """A colour control whose value is one raw component, published as-is."""

    def __init__(
        self,
        control_info: ControlInfo,
        component: ColourComponent,
        commands_builder: Optional[Callable[[Address, str], list[Command]]] = None,
        is_group_state_control: bool = False,
    ) -> None:
        super().__init__(
            control_info,
            components=[component],
            commands_builder=commands_builder,
            is_group_state_control=is_group_state_control,
        )
        self._component = component

    # --- Hooks for subclasses ---

    def _format(self, components: dict[ColourComponent, int]) -> str:
        return str(components[self._component])
