import logging
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional, Sequence

from dali.address import GearBroadcast, GearGroup

from .common_dali_device import ApplyResult, MqttControlBase, NotifyResult
from .control_ids import (
    ACTUAL_LEVEL,
    CURRENT_COLOUR_TEMPERATURE,
    CURRENT_PRIMARY_N,
    CURRENT_RGB,
    CURRENT_WHITE,
    CURRENT_X_COORDINATE,
    CURRENT_Y_COORDINATE,
)
from .control_ids import DAPC as DAPC_ID
from .control_ids import (
    ON_OFF,
    PRIMARY_N_MAX,
    SET_COLOUR_TEMPERATURE,
    SET_PRIMARY_N,
    SET_RGB,
    SET_WHITE,
    SET_X_COORDINATE,
    SET_Y_COORDINATE,
    WANTED_LEVEL,
)
from .dali_controls import WantedLevelControl, make_controls
from .dali_device import DaliDevice
from .dali_dimming_curve import DimmingCurveState, DimmingCurveType
from .dali_type8_parameters import ColourType
from .dali_type8_rgbwaf import get_mqtt_controls as rgbwaf_mqtt_controls
from .dali_type8_tc import Type8TcLimits
from .dali_type8_tc import get_wanted_mqtt_controls as tc_mqtt_controls
from .device_publisher import ControlInfo, TranslatedTitle
from .device_registry import DeviceRegistry
from .events import BusEvent
from .on_off_control import (
    OnOffConfig,
    OnOffControl,
    OnOffSettingsParam,
    gear_params_only,
    on_off_config_to_editor_json,
)
from .settings import SettingsParamBase
from .utils import merge_json_schemas
from .wbdali import WBDALIDriver
from .wbdali_utils import MASK_2BYTES, send_commands_with_retry
from .wbmqtt import ControlError

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


class MemberAnswer(Enum):
    """What the group does with the answer a member control gave."""

    TAKE = "take"  # copy the member's value onto the group
    HOLD = "hold"  # another member leads, or this failure is not the last one
    SHOW_ERROR = "show_error"  # every candidate's last read failed


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

    def record_answer(
        self,
        candidate_uid: CandidateUid,
        control_id: ControlId,
        success: bool,
    ) -> MemberAnswer:
        book = self._state.get(control_id)
        if book is None or candidate_uid not in book.candidate_statuses:
            return MemberAnswer.HOLD

        book.candidate_statuses[candidate_uid] = (
            CandidatePollStatus.SUCCESS if success else CandidatePollStatus.ERROR
        )
        if book.pinned_source is not None and book.pinned_source != candidate_uid:
            return MemberAnswer.HOLD

        if success:
            book.pinned_source = candidate_uid
            book.err_published = False
            return MemberAnswer.TAKE

        book.pinned_source = None
        if self._all_errored(book) and not book.err_published:
            book.err_published = True
            return MemberAnswer.SHOW_ERROR
        return MemberAnswer.HOLD

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


# The state control whose pinned member drives each mirrored setpoint, so a group card never
# shows a value and a setpoint that came from two different members.
_SETPOINT_STATE: dict[ControlId, ControlId] = {
    WANTED_LEVEL: ACTUAL_LEVEL,
    DAPC_ID: ACTUAL_LEVEL,
    SET_RGB: CURRENT_RGB,
    SET_WHITE: CURRENT_WHITE,
    SET_COLOUR_TEMPERATURE: CURRENT_COLOUR_TEMPERATURE,
    SET_X_COORDINATE: CURRENT_X_COORDINATE,
    SET_Y_COORDINATE: CURRENT_Y_COORDINATE,
    **{SET_PRIMARY_N.format(i): CURRENT_PRIMARY_N.format(i) for i in range(PRIMARY_N_MAX)},
}

# The setpoints each state control drives, the reverse of _SETPOINT_STATE.
_STATE_SETPOINTS: dict[ControlId, tuple[ControlId, ...]] = {
    state_id: tuple(sp for sp, st in _SETPOINT_STATE.items() if st == state_id)
    for state_id in set(_SETPOINT_STATE.values())
}


@dataclass(frozen=True)
class AggregatedCapabilities:
    has_dt8_rgbwaf: bool = False
    has_dt8_tc: bool = False
    tc_min_mirek: int = 0
    tc_max_mirek: int = 0
    dimming_curve_type: DimmingCurveType = DimmingCurveType.LOGARITHMIC


# Each state control sits immediately before its anchor on the group card.
_GROUP_STATE_ANCHOR: dict[ControlId, ControlId] = {
    ACTUAL_LEVEL: WANTED_LEVEL,
    CURRENT_RGB: SET_RGB,
    CURRENT_WHITE: SET_WHITE,
    CURRENT_COLOUR_TEMPERATURE: SET_COLOUR_TEMPERATURE,
}


def _dimming_state(capabilities: AggregatedCapabilities) -> DimmingCurveState:
    state = DimmingCurveState()
    state.curve_type = capabilities.dimming_curve_type
    return state


def build_virtual_device_controls(
    capabilities: AggregatedCapabilities,
    state_controls: Optional[Iterable[MqttControlBase]] = None,
) -> dict[ControlId, MqttControlBase]:
    setup_controls: list[MqttControlBase] = [
        WantedLevelControl(_dimming_state(capabilities)),
        *make_controls(),
    ]
    if capabilities.has_dt8_rgbwaf:
        setup_controls.extend(rgbwaf_mqtt_controls(only_setup_controls=True))
    if capabilities.has_dt8_tc:
        setup_controls.extend(
            tc_mqtt_controls(Type8TcLimits(capabilities.tc_min_mirek, capabilities.tc_max_mirek))
        )

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
        control.control_info.state.meta.order = i

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
        # A member with no limit on a side leaves the group unbounded there. Its marker is the
        # largest mirek there is, so max() keeps it but min() would drop it.
        tc_min_mirek=MASK_2BYTES if MASK_2BYTES in tc_min_values else min(tc_min_values, default=0),
        tc_max_mirek=max(tc_max_values) if tc_max_values else 0,
        dimming_curve_type=dimming_curve_type,
    )


_ON_OFF_CONTROL_ORDER = 0


class GroupVirtualDevice:  # pylint: disable=too-many-instance-attributes
    """Virtual device that aggregates DALI gear in a single group.

    Owns a per-control ``GroupStateSource`` picking whose answer the group takes.
    The source is always present; with no group-eligible state controls it
    carries an empty per-control map.
    """

    def __init__(  # pylint: disable=too-many-arguments, R0917
        self,
        group_number: int,
        device_registry: DeviceRegistry,
        mqtt_id_prefix: str,
        bus_name: str,
        on_off_config: Optional[OnOffConfig] = None,
    ) -> None:
        self._registry = device_registry
        self._address = GearGroup(group_number)
        members = device_registry.resolve(self._address)
        templates, state_candidates = collect_group_state_controls(members)
        # The Editor-RPC identity of the group
        self.uid = f"{mqtt_id_prefix}_g{group_number}"
        self.mqtt_id = f"{mqtt_id_prefix}_group_{group_number:02d}"
        self.name = TranslatedTitle(
            f"{bus_name} Group {group_number}",
            f"{bus_name} группа {group_number}",
        )
        self.capabilities = aggregate_capabilities(members)
        self.logger = logging.getLogger()

        self._controls = build_virtual_device_controls(
            self.capabilities,
            state_controls=templates.values(),
        )
        self._state_config = make_group_state_config(state_candidates)
        self._state_source = GroupStateSource(state_candidates)
        self._on_off_param = OnOffSettingsParam(on_off_config)
        if on_off_config is not None:
            on_off = OnOffControl(self._on_off_param, _dimming_state(self.capabilities))
            on_off.control_info.state.meta.order = _ON_OFF_CONTROL_ORDER
            self._controls = {ON_OFF: on_off, **self._controls}
        self._seed_state_from_members(members)

    @property
    def state_config(self) -> GroupStateConfig:
        return self._state_config

    @property
    def state_source(self) -> GroupStateSource:
        return self._state_source

    @property
    def on_off_config(self) -> Optional[OnOffConfig]:
        """``None`` when the group has no on/off block, so no on_off control."""
        return self._on_off_param.config

    @property
    def params(self) -> dict:
        """Editor config: the group has no setting of its own beyond the on/off block."""
        return {"on_off": on_off_config_to_editor_json(self.on_off_config)}

    @property
    def schema(self) -> dict:
        """Group parameters of the current members, merged, plus the group's own on/off block."""
        schema: dict = {}
        for member in self._registry.resolve(self._address):
            for handler in member.get_group_parameter_handlers():
                merge_json_schemas(schema, handler.get_schema(group_and_broadcast=True))
        merge_json_schemas(schema, self._on_off_param.get_schema(group_and_broadcast=True))
        return schema

    def update_in_place(self) -> bool:
        """Reconcile this device with its current members without republishing if possible.

        Returns ``True`` when the device already matches them or when only the
        candidate lists differ (in which case ``state_config`` and the
        ``state_source`` are updated in place). Returns ``False`` when the
        MQTT topic layout differs — capabilities mismatch or the set of
        state-control ids changed — and the caller must rebuild the device.
        """
        members = self._registry.resolve(self._address)
        if self.capabilities != aggregate_capabilities(members):
            return False
        _, state_candidates = collect_group_state_controls(members)
        state_config = make_group_state_config(state_candidates)
        if self._state_config == state_config:
            return True
        old_ids = {entry.control_id for entry in self._state_config.entries}
        new_ids = {entry.control_id for entry in state_config.entries}
        if old_ids != new_ids:
            return False
        self._state_config = state_config
        self._state_source.update_candidates(state_candidates)
        return True

    def notify_all(self, event: BusEvent, member: DaliDevice) -> list[MqttControlBase]:
        """Take what ``member``'s own controls decided about ``event``, for the controls it leads.

        The member control is asked again with the same event, so its ``notify`` has to answer a
        repeat the same way -- the group holds no rule of its own to fall back on.
        """
        to_publish: list[MqttControlBase] = []
        # The state controls go first: they move the pin the setpoints then follow.
        for control_id in self._state_source.control_ids:
            control = self._controls.get(control_id)
            if control is not None and self._take_state(member, event, control_id, control):
                to_publish.append(control)
                if control_id == ACTUAL_LEVEL:
                    to_publish.extend(self._take_on_off(event))
        for control_id, state_id in _SETPOINT_STATE.items():
            control = self._controls.get(control_id)
            if control is not None and self._take_setpoint(member, event, control_id, control, state_id):
                to_publish.append(control)
        return to_publish

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

    async def apply_parameters(self, driver: WBDALIDriver, new_values: dict) -> ApplyResult:
        """Writes the members' group parameters, then this group's own on/off setting."""
        # The setting goes last on purpose: a failed parameter write must leave it untouched.
        gear_params = gear_params_only(new_values)
        for handler in self._member_group_handlers(self._registry.resolve(self._address)):
            await handler.write(driver, self._address, gear_params)
        written = await self._on_off_param.write(driver, self._address, new_values, self.logger)
        return ApplyResult(
            needs_mqtt_controls_refresh=bool(written) and self._on_off_param.requires_mqtt_controls_refresh
        )

    def set_logger(self, logger: logging.Logger) -> None:
        self.logger = logger

    # --- Private ---

    def _take_state(
        self, member: DaliDevice, event: BusEvent, control_id: ControlId, control: MqttControlBase
    ) -> bool:
        answered = self._ask_member(member, event, control_id)
        if answered is None:
            return False
        answer = self._state_source.record_answer(
            member.uid, control_id, not answered.control_info.state.error
        )
        if answer is MemberAnswer.TAKE:
            control.control_info.state.value = answered.control_info.state.value
            control.control_info.state.error = ControlError.NONE
            return True
        if answer is MemberAnswer.SHOW_ERROR:
            # Only the error goes out: the last value the group showed stays.
            control.control_info.state.error = ControlError.READ
            return True
        return False

    def _take_setpoint(  # pylint: disable=too-many-arguments, R0917
        self,
        member: DaliDevice,
        event: BusEvent,
        control_id: ControlId,
        control: MqttControlBase,
        state_id: ControlId,
    ) -> bool:
        if self._state_source.pinned_source(state_id) != member.uid:
            return False
        answered = self._ask_member(member, event, control_id)
        if answered is None or answered.control_info.state.value is None:
            return False
        control.control_info.state.value = answered.control_info.state.value
        return True

    def _ask_member(
        self, member: DaliDevice, event: BusEvent, control_id: ControlId
    ) -> Optional[MqttControlBase]:
        control = member.get_mqtt_control(control_id)
        if control is None:
            return None
        try:
            if control.notify(event) is not NotifyResult.PUBLISH_STATE:
                return None
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.logger.warning(
                "Control %s of group member %s failed to handle %s: %s",
                control_id,
                member.name,
                type(event).__name__,
                exc,
                exc_info=True,
            )
            return None
        return control

    def _seed_state_from_members(self, members: Iterable[DaliDevice]) -> None:
        """Take each state control's value, and its setpoints', from one member — no bus I/O."""
        members_by_uid = {d.uid: d for d in members if d.is_initialized}
        for control_id in self._state_source.control_ids:
            group_control = self._controls.get(control_id)
            if group_control is None:
                continue
            source = self._seed_source(members_by_uid, control_id)
            if source is None:
                group_control.control_info.state.error = ControlError.READ
                continue
            member, seeded_value = source
            group_control.control_info.state.value = seeded_value
            # The control was cloned from the first candidate, error included.
            group_control.control_info.state.error = ControlError.NONE
            for setpoint_id in _STATE_SETPOINTS.get(control_id, ()):
                self._seed_setpoint(member, setpoint_id)
            if control_id == ACTUAL_LEVEL:
                self._seed_on_off(seeded_value)

    def _take_on_off(self, event: BusEvent) -> list[MqttControlBase]:
        """The group's on_off follows the level the group has just taken from a member."""
        control = self._controls.get(ON_OFF)
        if not isinstance(control, OnOffControl):
            return []
        if control.notify(event) is not NotifyResult.PUBLISH_STATE:
            return []
        return [control]

    def _seed_on_off(self, level_percent: str) -> None:
        control = self._controls.get(ON_OFF)
        if isinstance(control, OnOffControl):
            control.update_from_percent(level_percent)

    def _seed_setpoint(self, member: DaliDevice, setpoint_id: ControlId) -> None:
        """Mirror the member's own setpoint, which already holds the read in the setpoint's terms."""
        group_control = self._controls.get(setpoint_id)
        member_control = member.get_mqtt_control(setpoint_id)
        if group_control is None or member_control is None:
            return
        if member_control.control_info.state.value is not None:
            group_control.control_info.state.value = member_control.control_info.state.value

    def _seed_source(
        self, members_by_uid: dict[CandidateUid, DaliDevice], control_id: ControlId
    ) -> Optional[tuple[DaliDevice, str]]:
        """The first candidate whose value is actually known, and that value."""
        for candidate_uid in self._state_source.candidates_for(control_id):
            member = members_by_uid.get(candidate_uid)
            if member is None:
                continue
            control = member.get_mqtt_control(control_id)
            if control is None or control.control_info.state.error:
                continue
            if control.control_info.state.value is not None:
                return member, control.control_info.state.value
        return None

    @staticmethod
    def _member_group_handlers(members: Sequence[DaliDevice]) -> list[SettingsParamBase]:
        """One handler per parameter: members of the same type would write the same group address."""
        handlers: list[SettingsParamBase] = []
        seen: set[tuple[str, str]] = set()
        for member in members:
            if not member.is_initialized:
                continue
            for handler in member.get_group_parameter_handlers():
                key = (type(handler).__name__, getattr(handler, "property_name", ""))
                if key not in seen:
                    seen.add(key)
                    handlers.append(handler)
        return handlers


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
