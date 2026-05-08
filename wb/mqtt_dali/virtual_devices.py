import logging
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional, Union

from dali.address import GearBroadcast, GearGroup

from .common_dali_device import MqttControlBase
from .dali_controls import WantedLevelControl, make_controls
from .dali_device import DaliDevice
from .dali_dimming_curve import DimmingCurveState, DimmingCurveType
from .dali_type8_parameters import ColourType
from .dali_type8_rgbwaf import get_mqtt_controls as rgbwaf_mqtt_controls
from .dali_type8_tc import get_wanted_mqtt_controls as tc_mqtt_controls
from .device_publisher import ControlInfo, TranslatedTitle
from .wbdali import WBDALIDriver
from .wbdali_utils import send_commands_with_retry

ControlId = str
# Stable per-bus identity for a candidate device. ``device.uid`` (UUID4) survives
# both SetDevice renames (which only touch mqtt_id) and SetDevice short-address
# changes; the alternatives — mqtt_id or short_address — drift on either of
# those edits and would silently lose the pinned source.
CandidateUid = str


@dataclass(frozen=True)
class StateControlCandidates:
    control_id: ControlId
    candidate_uids: tuple[CandidateUid, ...]


@dataclass(frozen=True)
class GroupStateConfig:
    """Snapshot of the group's state-control composition; equality drives rebuild."""

    entries: tuple[StateControlCandidates, ...] = ()


def make_group_state_config(
    state_candidates: Optional[dict[ControlId, list[CandidateUid]]],
) -> GroupStateConfig:
    return GroupStateConfig(
        entries=tuple(
            StateControlCandidates(control_id=cid, candidate_uids=tuple(uids))
            for cid, uids in sorted((state_candidates or {}).items())
        )
    )


def collect_group_state_controls(
    devices: Iterable[DaliDevice],
) -> tuple[dict[ControlId, MqttControlBase], dict[ControlId, list[CandidateUid]]]:
    templates: dict[ControlId, MqttControlBase] = {}
    candidates: dict[ControlId, list[CandidateUid]] = {}
    for device in devices:
        if not device.is_initialized:
            continue
        for control in device.get_group_state_controls():
            control_id = control.control_info.id
            if control_id not in templates:
                # Deep-copy: meta.order is reassigned for the group's layout
                # and must not bleed back into the source device.
                templates[control_id] = MqttControlBase(deepcopy(control.control_info))
                candidates[control_id] = []
            candidates[control_id].append(device.uid)
    return templates, candidates


class GroupStateUpdateKind(Enum):
    VALUE = "value"
    ERROR = "error"


@dataclass(frozen=True)
class GroupStateUpdate:
    kind: GroupStateUpdateKind
    control_id: ControlId
    payload: str


class CandidatePollStatus(Enum):
    SUCCESS = "success"
    ERROR = "error"


@dataclass
class PerControlState:
    candidates: tuple[CandidateUid, ...]
    candidate_statuses: dict[CandidateUid, Optional[CandidatePollStatus]] = field(default_factory=dict)
    pinned_source: Optional[CandidateUid] = None
    err_published: bool = False


class GroupStateSource:
    """Pins one candidate per control_id as the value source mirrored to the group.

    Pinned source: its successful polls drive the group; its errored poll
    unpins it but keeps the last published value. ``err=r`` is emitted only
    once every candidate's last poll is an error. Any candidate's next
    successful poll clears the error and re-pins.
    """

    def __init__(self, candidates_by_control_id: dict[ControlId, list[CandidateUid]]) -> None:
        self._state: dict[ControlId, PerControlState] = {
            cid: PerControlState(
                candidates=tuple(uids),
                candidate_statuses={uid: None for uid in uids},
            )
            for cid, uids in candidates_by_control_id.items()
        }

    @property
    def control_ids(self) -> set[ControlId]:
        return set(self._state)

    def candidates_for(self, control_id: ControlId) -> tuple[CandidateUid, ...]:
        book = self._state.get(control_id)
        return book.candidates if book is not None else ()

    def pinned_source(self, control_id: ControlId) -> Optional[CandidateUid]:
        book = self._state.get(control_id)
        return book.pinned_source if book is not None else None

    def is_err_set(self, control_id: ControlId) -> bool:
        book = self._state.get(control_id)
        return book.err_published if book is not None else False

    def record_poll(
        self,
        candidate_uid: CandidateUid,
        control_id: ControlId,
        success: bool,
        value: Optional[str],
    ) -> Optional[GroupStateUpdate]:
        book = self._state.get(control_id)
        if book is None or candidate_uid not in book.candidate_statuses:
            return None

        book.candidate_statuses[candidate_uid] = (
            CandidatePollStatus.SUCCESS if success else CandidatePollStatus.ERROR
        )
        if book.pinned_source is not None and book.pinned_source != candidate_uid:
            return None

        if success:
            book.pinned_source = candidate_uid
            book.err_published = False
            return GroupStateUpdate(
                kind=GroupStateUpdateKind.VALUE, control_id=control_id, payload=value or ""
            )

        book.pinned_source = None
        if self._all_errored(book) and not book.err_published:
            book.err_published = True
            return GroupStateUpdate(kind=GroupStateUpdateKind.ERROR, control_id=control_id, payload="r")
        return None

    def update_candidates(self, new_candidates_by_control_id: dict[ControlId, list[CandidateUid]]) -> None:
        """Replace candidate set in place; preserve last_status / pin for surviving candidates.

        Caller guarantees the set of ``control_id``s is unchanged. Only the
        candidate lists per control_id may differ.
        """
        for control_id, book in self._state.items():
            new_uids = tuple(new_candidates_by_control_id.get(control_id, ()))
            book.candidates = new_uids
            new_uid_set = set(new_uids)
            statuses = book.candidate_statuses
            for gone in [uid for uid in statuses if uid not in new_uid_set]:
                statuses.pop(gone)
            for added in new_uids:
                statuses.setdefault(added, None)
            if book.pinned_source not in new_uid_set:
                book.pinned_source = None

    @staticmethod
    def _all_errored(book: PerControlState) -> bool:
        statuses = book.candidate_statuses
        if not statuses:
            return False
        return all(status == CandidatePollStatus.ERROR for status in statuses.values())


@dataclass(frozen=True)
class AggregatedCapabilities:
    has_dt8_rgbwaf: bool = False
    has_dt8_tc: bool = False
    tc_min_mirek: int = 0
    tc_max_mirek: int = 0
    dimming_curve_type: DimmingCurveType = DimmingCurveType.LOGARITHMIC


# Each state control sits immediately before its anchor on the group card.
_GROUP_STATE_ANCHOR: dict[ControlId, ControlId] = {
    "actual_level": "wanted_level",
    "current_rgb": "set_rgb",
    "current_white": "set_white",
    "current_colour_temperature": "set_colour_temperature",
}


def build_virtual_device_controls(
    capabilities: AggregatedCapabilities,
    state_controls: Optional[Iterable[MqttControlBase]] = None,
) -> dict[ControlId, MqttControlBase]:
    dimming_state = DimmingCurveState()
    dimming_state.curve_type = capabilities.dimming_curve_type

    setup_controls: list[MqttControlBase] = [WantedLevelControl(dimming_state), *make_controls()]
    if capabilities.has_dt8_rgbwaf:
        setup_controls.extend(rgbwaf_mqtt_controls(only_setup_controls=True))
    if capabilities.has_dt8_tc:
        setup_controls.extend(tc_mqtt_controls(capabilities.tc_min_mirek, capabilities.tc_max_mirek))

    state_by_id = {c.control_info.id: c for c in (state_controls or [])}
    state_at_anchor: dict[ControlId, MqttControlBase] = {
        anchor: state_by_id.pop(sid) for sid, anchor in _GROUP_STATE_ANCHOR.items() if sid in state_by_id
    }

    controls: list[MqttControlBase] = []
    for setup in setup_controls:
        anchored = state_at_anchor.pop(setup.control_info.id, None)
        if anchored is not None:
            controls.append(anchored)
        controls.append(setup)
    controls.extend(state_by_id.values())

    for i, control in enumerate(controls, start=1):
        control.control_info.meta.order = i

    return {c.control_info.id: c for c in controls}


def aggregate_capabilities(devices: Iterable[DaliDevice]) -> AggregatedCapabilities:
    has_rgbwaf = False
    has_tc = False
    tc_min_values: list[int] = []
    tc_max_values: list[int] = []
    curve_types: set[DimmingCurveType] = set()
    for device in devices:
        if not device.is_initialized:
            continue
        colour_type = device.dt8_colour_type
        if colour_type == ColourType.RGBWAF:
            has_rgbwaf = True
        elif colour_type == ColourType.COLOUR_TEMPERATURE:
            has_tc = True
            limits = device.dt8_tc_limits
            if limits is not None:
                tc_min_values.append(limits.tc_min_mirek)
                tc_max_values.append(limits.tc_max_mirek)
        curve_types.add(device.dimming_curve_type)
    dimming_curve_type = next(iter(curve_types)) if len(curve_types) == 1 else DimmingCurveType.LOGARITHMIC
    return AggregatedCapabilities(
        has_dt8_rgbwaf=has_rgbwaf,
        has_dt8_tc=has_tc,
        tc_min_mirek=min(tc_min_values) if tc_min_values else 0,
        tc_max_mirek=max(tc_max_values) if tc_max_values else 0,
        dimming_curve_type=dimming_curve_type,
    )


@dataclass(frozen=True)
class GroupSpec:
    """Single-pass snapshot of everything needed to build a group's virtual device.

    ``capabilities`` and ``state_config`` jointly define the MQTT topic layout;
    ``state_config`` additionally encodes the per-control candidate lists, so
    two snapshots with equal ``capabilities`` and equal control_id sets but
    different candidate lists differ in ``state_config`` only.
    """

    capabilities: AggregatedCapabilities
    templates: dict[ControlId, MqttControlBase]
    state_candidates: dict[ControlId, list[CandidateUid]]
    state_config: GroupStateConfig

    @classmethod
    def from_devices(cls, devices: Iterable[DaliDevice]) -> "GroupSpec":
        device_list = list(devices)
        capabilities = aggregate_capabilities(device_list)
        templates, state_candidates = collect_group_state_controls(device_list)
        state_config = make_group_state_config(state_candidates)
        return cls(
            capabilities=capabilities,
            templates=templates,
            state_candidates=state_candidates,
            state_config=state_config,
        )


class GroupVirtualDevice:  # pylint: disable=too-many-instance-attributes
    """Virtual device that aggregates DALI gear in a single group.

    Owns a per-control ``GroupStateSource`` mirroring one member's polled value
    onto the group topic. The source is always present; with no group-eligible
    state controls it carries an empty per-control map.
    """

    def __init__(  # pylint: disable=too-many-arguments, R0917
        self,
        mqtt_id: str,
        name: Union[str, TranslatedTitle],
        capabilities: AggregatedCapabilities,
        group_number: int,
        state_control_templates: dict[ControlId, MqttControlBase],
        state_candidates: dict[ControlId, list[CandidateUid]],
    ) -> None:
        self.mqtt_id = mqtt_id
        self.name = name
        self.capabilities = capabilities
        self.logger = logging.getLogger()

        self._controls = build_virtual_device_controls(
            capabilities,
            state_controls=state_control_templates.values(),
        )
        self._address = GearGroup(group_number)
        self._state_config = make_group_state_config(state_candidates)
        self._state_source = GroupStateSource(state_candidates)

    @classmethod
    def for_group(
        cls,
        group_number: int,
        spec: GroupSpec,
        mqtt_id_prefix: str,
        bus_name: str,
    ) -> "GroupVirtualDevice":
        return cls(
            mqtt_id=f"{mqtt_id_prefix}_group_{group_number:02d}",
            name=TranslatedTitle(
                f"{bus_name} Group {group_number}",
                f"{bus_name} группа {group_number}",
            ),
            capabilities=spec.capabilities,
            group_number=group_number,
            state_control_templates=spec.templates,
            state_candidates=spec.state_candidates,
        )

    @property
    def state_config(self) -> GroupStateConfig:
        return self._state_config

    @property
    def state_source(self) -> GroupStateSource:
        return self._state_source

    def update_in_place(self, spec: GroupSpec) -> bool:
        """Reconcile this device with ``spec`` without republishing if possible.

        Returns ``True`` when the device already matches the snapshot or when
        only the candidate lists differ (in which case ``state_config`` and
        the ``state_source`` are updated in place). Returns ``False`` when the
        MQTT topic layout differs — capabilities mismatch or the set of
        state-control ids changed — and the caller must rebuild the device.
        """
        if self.capabilities != spec.capabilities:
            return False
        if self._state_config == spec.state_config:
            return True
        old_ids = {entry.control_id for entry in self._state_config.entries}
        new_ids = {entry.control_id for entry in spec.state_config.entries}
        if old_ids != new_ids:
            return False
        self._state_config = spec.state_config
        self._state_source.update_candidates(spec.state_candidates)
        return True

    def get_mqtt_controls(self) -> list[ControlInfo]:
        return [control.control_info for control in self._controls.values()]

    def get_mqtt_control(self, control_id: str) -> Optional[MqttControlBase]:
        return self._controls.get(control_id)

    async def execute_control(
        self,
        driver: WBDALIDriver,
        control_id: ControlId,
        value: str,
    ) -> None:
        control = self._controls.get(control_id)
        if control is not None and control.is_writable():
            await send_commands_with_retry(
                driver,
                control.get_setup_commands(self._address, value),
                self.logger,
            )

    def set_logger(self, logger: logging.Logger) -> None:
        self.logger = logger


class BroadcastVirtualDevice:
    """Virtual device targeting the bus broadcast address (no state mirroring)."""

    def __init__(
        self,
        capabilities: AggregatedCapabilities,
        mqtt_id_prefix: str,
        bus_name: str,
    ) -> None:
        self.mqtt_id = f"{mqtt_id_prefix}_broadcast"
        self.name = TranslatedTitle(
            f"{bus_name} Broadcast",
            f"{bus_name} широковещательный",
        )
        self.capabilities = capabilities
        self.logger = logging.getLogger()

        self._controls = build_virtual_device_controls(capabilities)
        self._address = GearBroadcast()

    def get_mqtt_controls(self) -> list[ControlInfo]:
        return [control.control_info for control in self._controls.values()]

    def get_mqtt_control(self, control_id: str) -> Optional[MqttControlBase]:
        return self._controls.get(control_id)

    async def execute_control(
        self,
        driver: WBDALIDriver,
        control_id: ControlId,
        value: str,
    ) -> None:
        control = self._controls.get(control_id)
        if control is not None and control.is_writable():
            await send_commands_with_retry(
                driver,
                control.get_setup_commands(self._address, value),
                self.logger,
            )

    def set_logger(self, logger: logging.Logger) -> None:
        self.logger = logger
