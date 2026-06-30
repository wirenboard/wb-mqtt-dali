import asyncio
import logging
import re
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dali.address import DeviceShort, InstanceNumber
from dali.command import Response, from_frame
from dali.device.general import EnableInstance
from dali.frame import BackwardFrame, BackwardFrameError, ForwardFrame

from wb.mqtt_dali.application_controller import (
    MIN_POLLING_INTERVAL,
    ApplicationController,
    ApplicationControllerConfig,
    ApplicationControllerState,
    ApplicationControllerTask,
    ApplicationControllerTaskType,
    CommissioningDeviceSummary,
    CommissioningStartResult,
    CommissioningState,
    CommissioningStatus,
    format_command,
    format_frame_hex,
    format_response,
)
from wb.mqtt_dali.bus_traffic import BusTrafficSource
from wb.mqtt_dali.commissioning import (
    ChangedDevice,
    CommissioningResult,
    CommissioningStage,
)
from wb.mqtt_dali.common_dali_device import DaliDeviceAddress, DaliDeviceBase
from wb.mqtt_dali.dali_compat import DaliCommandsCompatibilityLayer
from wb.mqtt_dali.device_registry import DeviceRegistry
from wb.mqtt_dali.fetch_scheduler import SettingsFetchScheduler
from wb.mqtt_dali.gateway import Gateway, WbDaliGateway, bus_from_json
from wb.mqtt_dali.wbdali_error_response import WbGatewayTransmissionError

from ._app_controller_helpers import make_loop_controller, stop_loop

# pylint: disable=duplicate-code

# Prevent file system access inside DaliDeviceBase.__init__
DaliDeviceBase._common_schema = {"title": "test-schema"}  # pylint: disable=protected-access


class TestApplicationControllerVirtualGroups:  # pylint: disable=too-few-public-methods
    def test_get_active_group_numbers(self):
        controller = ApplicationController.__new__(ApplicationController)
        controller.dali_devices = [
            SimpleNamespace(groups=set([0, 2])),
            SimpleNamespace(groups=set([1])),
            SimpleNamespace(groups=set([2])),
        ]

        assert getattr(controller, "_get_active_group_numbers")() == [0, 1, 2]


def _make_bare_controller():
    # pylint: disable=protected-access
    controller = ApplicationController.__new__(ApplicationController)
    controller.uid = "gw_bus_1"
    controller.logger = logging.getLogger("test")
    controller._dev = AsyncMock()
    controller._gtin_db = MagicMock()
    controller._devices_by_mqtt_id = {}
    controller._init_scheduler = MagicMock()
    controller._init_scheduler.remove = MagicMock()
    controller._init_scheduler.schedule = MagicMock()
    controller._device_publisher = AsyncMock()
    controller._try_init_new_device = AsyncMock()
    controller._refresh_group_virtual_devices = AsyncMock()
    controller._refresh_broadcast_device = AsyncMock()
    controller.dali_devices = []
    controller.dali2_devices = []
    controller._device_registry = DeviceRegistry()
    controller._fetch_scheduler = SettingsFetchScheduler()
    return controller


@pytest.mark.asyncio
async def test_resolve_initial_names_formats_known_product():
    # pylint: disable=protected-access
    controller = _make_bare_controller()
    compat = DaliCommandsCompatibilityLayer()
    addresses = [DaliDeviceAddress(short=3, random=0x1234)]

    with patch(
        "wb.mqtt_dali.application_controller.read_product_name",
        new=AsyncMock(return_value="LED Driver"),
    ):
        names = await controller._resolve_initial_names(addresses, compat)

    assert names == ["LED Driver 3"]


@pytest.mark.asyncio
async def test_resolve_initial_names_returns_none_for_unknown_product():
    # pylint: disable=protected-access
    controller = _make_bare_controller()
    compat = DaliCommandsCompatibilityLayer()
    addresses = [DaliDeviceAddress(short=7, random=0x22)]

    with patch(
        "wb.mqtt_dali.application_controller.read_product_name",
        new=AsyncMock(return_value=None),
    ):
        names = await controller._resolve_initial_names(addresses, compat)

    assert names == [None]


@pytest.mark.asyncio
async def test_resolve_initial_names_returns_none_on_exception():
    # pylint: disable=protected-access
    controller = _make_bare_controller()
    compat = DaliCommandsCompatibilityLayer()
    addresses = [DaliDeviceAddress(short=2, random=0x00)]

    with patch(
        "wb.mqtt_dali.application_controller.read_product_name",
        new=AsyncMock(side_effect=RuntimeError("bus down")),
    ):
        names = await controller._resolve_initial_names(addresses, compat)

    assert names == [None]


@pytest.mark.asyncio
async def test_resolve_initial_names_handles_empty_input():
    # pylint: disable=protected-access
    controller = _make_bare_controller()
    compat = DaliCommandsCompatibilityLayer()

    with patch("wb.mqtt_dali.application_controller.read_product_name") as mock_rpn:
        names = await controller._resolve_initial_names([], compat)
        mock_rpn.assert_not_called()

    assert names == []


@pytest.mark.asyncio
async def test_update_dali_devices_sets_custom_name_for_new_with_known_gtin():
    # pylint: disable=protected-access
    controller = _make_bare_controller()

    new_addr = DaliDeviceAddress(short=5, random=0xABCD)
    commissioning_result = CommissioningResult(new=[new_addr])

    with patch(
        "wb.mqtt_dali.application_controller.read_product_name",
        new=AsyncMock(return_value="Acme Lamp"),
    ):
        await controller._update_dali_devices(commissioning_result)

    assert len(controller.dali_devices) == 1
    device = controller.dali_devices[0]
    assert device.address.short == 5
    assert device.name == "Acme Lamp 5"
    assert device.has_custom_name is True


@pytest.mark.asyncio
async def test_update_dali_devices_uses_default_name_for_unknown_gtin():
    # pylint: disable=protected-access
    controller = _make_bare_controller()

    new_addr = DaliDeviceAddress(short=8, random=0x01)
    commissioning_result = CommissioningResult(new=[new_addr])

    with patch(
        "wb.mqtt_dali.application_controller.read_product_name",
        new=AsyncMock(return_value=None),
    ):
        await controller._update_dali_devices(commissioning_result)

    assert len(controller.dali_devices) == 1
    device = controller.dali_devices[0]
    assert device.name == "DALI 8"
    assert device.has_custom_name is False


@pytest.mark.asyncio
async def test_update_dali_devices_sets_custom_name_for_changed_device():
    # pylint: disable=protected-access
    controller = _make_bare_controller()

    # Simulate a previously-known device at short 2 that is now a replacement
    old_device = SimpleNamespace(
        address=DaliDeviceAddress(short=2, random=0xAA),
        mqtt_id="gw_bus_1_2",
    )
    controller.dali_devices = [old_device]

    changed = ChangedDevice(
        new=DaliDeviceAddress(short=2, random=0xBB),
        old_short=2,
    )
    commissioning_result = CommissioningResult(changed=[changed])

    with patch(
        "wb.mqtt_dali.application_controller.read_product_name",
        new=AsyncMock(return_value="Replacement Driver"),
    ):
        await controller._update_dali_devices(commissioning_result)

    # Old device is replaced by a new one carrying the resolved name
    assert len(controller.dali_devices) == 1
    device = controller.dali_devices[0]
    assert device.address.short == 2
    assert device.address.random == 0xBB
    assert device.name == "Replacement Driver 2"
    assert device.has_custom_name is True


@pytest.mark.asyncio
async def test_update_dali2_devices_sets_custom_name_for_new_with_known_gtin():
    # pylint: disable=protected-access
    controller = _make_bare_controller()

    new_addr = DaliDeviceAddress(short=4, random=0x55)
    commissioning_result = CommissioningResult(new=[new_addr])

    with patch(
        "wb.mqtt_dali.application_controller.read_product_name",
        new=AsyncMock(return_value="Sensor Pro"),
    ):
        await controller._update_dali2_devices(commissioning_result)

    assert len(controller.dali2_devices) == 1
    device = controller.dali2_devices[0]
    assert device.address.short == 4
    assert device.name == "Sensor Pro 4"
    assert device.has_custom_name is True


@pytest.mark.asyncio
async def test_update_dali2_devices_sets_custom_name_for_changed_device():
    # pylint: disable=protected-access
    controller = _make_bare_controller()

    # Simulate a previously-known DALI-2 device at short 6 that is now a replacement
    old_device = SimpleNamespace(
        address=DaliDeviceAddress(short=6, random=0x11),
        mqtt_id="gw_bus_1_d2_6",
    )
    controller.dali2_devices = [old_device]

    changed = ChangedDevice(
        new=DaliDeviceAddress(short=6, random=0x22),
        old_short=6,
    )
    commissioning_result = CommissioningResult(changed=[changed])

    with patch(
        "wb.mqtt_dali.application_controller.read_product_name",
        new=AsyncMock(return_value="Replacement Sensor"),
    ):
        await controller._update_dali2_devices(commissioning_result)

    # Old device is replaced by a new one carrying the resolved name
    assert len(controller.dali2_devices) == 1
    device = controller.dali2_devices[0]
    assert device.address.short == 6
    assert device.address.random == 0x22
    assert device.name == "Replacement Sensor 6"
    assert device.has_custom_name is True


# --- Commissioning lifecycle ---------------------------------------------------


def _make_commissioning_controller():
    # pylint: disable=protected-access
    controller = _make_bare_controller()
    controller._state = ApplicationControllerState.READY
    controller._state_lock = asyncio.Lock()
    controller._tasks_queue = asyncio.Queue()
    controller._commissioning_state = CommissioningState()
    controller._current_commissioning_task = None
    controller._commissioning_state_cb = None
    controller._one_shot_tasks = MagicMock()
    controller._one_shot_tasks.add = MagicMock()
    return controller


_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class TestCommissioningStateMarkQueued:  # pylint: disable=too-few-public-methods
    def test_sets_running_queued_fields(self):
        # Seed the state with a terminal result to make sure queued clears it.
        state = CommissioningState(
            status=CommissioningStatus.FAILED,
            progress=42,
            error="boom",
            devices=[CommissioningDeviceSummary("1", "2", [])],
            finished_at="2001-01-01T00:00:00Z",
        )
        state.mark_queued()
        assert state.status == CommissioningStatus.QUEUED
        assert state.progress == 3
        assert state.error is None
        assert not state.devices
        assert state.finished_at is None


class TestCommissioningStateReportProgress:
    def test_updates_only_stage_and_progress(self):
        # Seed an arbitrary baseline we want to keep untouched.
        state = CommissioningState(
            status=CommissioningStatus.QUERY_SHORT_ADDRESSES,
            progress=0,
            error=None,
            devices=[],
            finished_at=None,
        )
        state.report_progress(CommissioningStage.BINARY_SEARCH, 37, True)
        assert state.progress == 21
        # Status / error / devices / finished_at are left alone.
        assert state.status == CommissioningStatus.BINARY_SEARCH
        assert state.error is None
        assert not state.devices
        assert state.finished_at is None

    def test_does_not_touch_finished_at_after_terminal(self):
        """After a terminal mark has set finished_at, a progress report must
        not clear it. Progress reports only happen inside smart_extend (i.e.
        while RUNNING), but this guards the invariant regardless.
        """
        state = CommissioningState()
        state.mark_completed([])
        baseline_finished_at = state.finished_at
        assert baseline_finished_at is not None
        state.report_progress(CommissioningStage.BINARY_SEARCH, 50, True)
        assert state.finished_at == baseline_finished_at


class TestCommissioningStateMarkCompleted:  # pylint: disable=too-few-public-methods
    def test_sets_terminal_happy_path_fields(self):
        state = CommissioningState()
        state.mark_completed([])
        assert state.status == CommissioningStatus.COMPLETED
        assert state.progress == 100
        assert state.error is None
        assert state.finished_at is not None
        assert _TIMESTAMP_RE.match(state.finished_at)


class TestCommissioningStateMarkFailed:  # pylint: disable=too-few-public-methods
    def test_preserves_progress_and_stage(self):
        """FAILED is a diagnostic signal — progress and stage are not reset."""
        # Seed with mid-run progress / stage.
        state = CommissioningState(
            status=CommissioningStatus.BINARY_SEARCH,
            progress=63,
            error=None,
            devices=[],
            finished_at=None,
        )
        state.mark_failed("DALI-2 phase failed", [])
        assert state.status == CommissioningStatus.FAILED
        assert state.error == "DALI-2 phase failed"
        # Diagnostic signal — progress and stage intentionally left intact.
        assert state.progress == 63
        assert state.finished_at is not None
        assert _TIMESTAMP_RE.match(state.finished_at)


class TestCommissioningStateMarkCancelled:  # pylint: disable=too-few-public-methods
    def test_preserves_progress(self):
        """CANCELLED is a diagnostic signal — progress is not reset."""
        state = CommissioningState(
            status=CommissioningStatus.QUERY_SHORT_ADDRESSES,
            progress=29,
            error=None,
            devices=[],
            finished_at=None,
        )
        state.mark_cancelled()
        assert state.status == CommissioningStatus.CANCELLED
        assert state.error is None
        assert not state.devices
        # Diagnostic signal — progress intentionally left intact.
        assert state.progress == 29
        assert state.finished_at is not None
        assert _TIMESTAMP_RE.match(state.finished_at)


class TestCommissioningStateSnapshot:
    def test_returns_independent_copy(self):
        original = CommissioningState(
            status=CommissioningStatus.QUERY_SHORT_ADDRESSES,
            progress=15,
            error=None,
            devices=[
                CommissioningDeviceSummary("1", "0x1", []),
                CommissioningDeviceSummary("2", "0x2", []),
            ],
            finished_at=None,
        )
        snapshot = original.snapshot()
        # Different dataclass instance, different list object, different summaries.
        assert snapshot is not original
        assert snapshot.devices is not original.devices
        assert snapshot.devices is not None and original.devices is not None
        assert snapshot.devices[0] is not original.devices[0]
        # But values match.
        assert snapshot == original

        # Mutation of the snapshot does not affect the original.
        snapshot.progress = 99
        snapshot.devices.append(CommissioningDeviceSummary("9", "0x9", []))
        assert original.progress == 15
        assert len(original.devices) == 2

        # Mutation of the original does not affect the earlier snapshot.
        original.progress = 50
        original.devices.append(CommissioningDeviceSummary("7", "0x7", []))
        assert snapshot.progress == 99
        assert len(snapshot.devices) == 3  # unchanged by ``original`` mutation

    def test_empty_devices_stays_empty(self):
        state = CommissioningState(devices=[])
        snapshot = state.snapshot()
        assert not snapshot.devices


class TestPublishCommissioningState:
    def test_sync_callback_invoked_with_snapshot(self):
        # pylint: disable=protected-access
        controller = _make_commissioning_controller()
        received: list[CommissioningState] = []
        controller._commissioning_state_cb = received.append
        controller._commissioning_state = CommissioningState(
            status=CommissioningStatus.QUERY_SHORT_ADDRESSES, progress=10
        )
        controller._publish_commissioning_state()
        assert len(received) == 1
        # Callback got an independent snapshot, not the internal state object.
        assert received[0] is not controller._commissioning_state
        assert received[0].status == CommissioningStatus.QUERY_SHORT_ADDRESSES
        assert received[0].progress == 10
        # Sync path does not schedule a one-shot task.
        cast(MagicMock, controller._one_shot_tasks.add).assert_not_called()

    def test_async_callback_is_scheduled_via_one_shot_tasks(self):
        # pylint: disable=protected-access
        controller = _make_commissioning_controller()

        async def _async_cb(_state):
            return None

        controller._commissioning_state_cb = _async_cb
        controller._publish_commissioning_state()
        add_mock = cast(MagicMock, controller._one_shot_tasks.add)
        add_mock.assert_called_once()
        coro = add_mock.call_args.args[0]
        assert asyncio.iscoroutine(coro)
        # Close to avoid "coroutine was never awaited" warnings.
        coro.close()

    def test_callback_exception_is_logged_and_swallowed(self, caplog):
        # pylint: disable=protected-access
        controller = _make_commissioning_controller()

        def _bad_cb(_state):
            raise RuntimeError("cb exploded")

        controller._commissioning_state_cb = _bad_cb
        with caplog.at_level(logging.ERROR, logger="test"):
            # Must not raise.
            controller._publish_commissioning_state()
        assert any("cb exploded" in rec.message for rec in caplog.records)

    def test_no_callback_is_noop(self):
        # pylint: disable=protected-access
        controller = _make_commissioning_controller()
        controller._commissioning_state_cb = None
        # No exception, no scheduling.
        controller._publish_commissioning_state()
        cast(MagicMock, controller._one_shot_tasks.add).assert_not_called()


class TestStartCommissioning:
    @pytest.mark.asyncio
    async def test_first_call_returns_started_and_marks_running(self):
        # pylint: disable=protected-access
        controller = _make_commissioning_controller()
        result = await controller.start_commissioning(on_state_changed=lambda _s: None)
        assert result is CommissioningStartResult.STARTED
        assert controller._commissioning_state.status == CommissioningStatus.QUEUED
        # One task enqueued
        assert controller._tasks_queue.qsize() == 1
        task = controller._tasks_queue.get_nowait()
        assert task.task_type == ApplicationControllerTaskType.COMMISSIONING

    @pytest.mark.asyncio
    async def test_second_call_while_running_returns_already_running(self):
        # pylint: disable=protected-access
        controller = _make_commissioning_controller()
        await controller.start_commissioning()
        result = await controller.start_commissioning()
        assert result is CommissioningStartResult.ALREADY_RUNNING
        # Only one task remains enqueued.
        assert controller._tasks_queue.qsize() == 1


class TestCancelCommissioning:
    @pytest.mark.asyncio
    async def test_returns_false_when_not_running(self):
        controller = _make_commissioning_controller()
        assert await controller.cancel_commissioning() is False

    @pytest.mark.asyncio
    async def test_removes_queued_task_and_sets_cancelled(self):
        # pylint: disable=protected-access
        controller = _make_commissioning_controller()
        # Seed the bus with devices to make sure they are NOT mutated when
        # a queued commissioning task is cancelled before it starts.
        original_dali = [SimpleNamespace(address=DaliDeviceAddress(short=11, random=0xB))]
        original_dali2 = [SimpleNamespace(address=DaliDeviceAddress(short=12, random=0xC))]
        controller.dali_devices = cast(Any, list(original_dali))
        controller.dali2_devices = cast(Any, list(original_dali2))
        controller._apply_commissioning_results = AsyncMock()

        await controller.start_commissioning()
        # Task is queued but worker hasn't promoted it to _current_commissioning_task yet.
        cancelled = await controller.cancel_commissioning()
        assert cancelled is True
        assert controller._commissioning_state.status == CommissioningStatus.CANCELLED
        # CANCELLED semantics: devices=[], no apply, bus lists untouched.
        assert not controller._commissioning_state.devices
        controller._apply_commissioning_results.assert_not_awaited()
        assert controller.dali_devices == original_dali
        assert controller.dali2_devices == original_dali2
        assert controller._tasks_queue.qsize() == 0

    @pytest.mark.asyncio
    async def test_cancels_running_task(self):
        # pylint: disable=protected-access
        controller = _make_commissioning_controller()
        await controller.start_commissioning()
        # Simulate the worker promoting the queued task.
        controller._tasks_queue.get_nowait()
        running_task = asyncio.create_task(asyncio.sleep(10))
        controller._current_commissioning_task = running_task
        try:
            cancelled = await controller.cancel_commissioning()
            assert cancelled is True
            # Yield control to let cancellation propagate.
            try:
                await running_task
            except asyncio.CancelledError:
                pass
            assert running_task.cancelled()
        finally:
            if not running_task.done():
                running_task.cancel()
                try:
                    await running_task
                except asyncio.CancelledError:
                    pass

    @pytest.mark.asyncio
    async def test_idempotent_during_cancel(self):
        # pylint: disable=protected-access
        controller = _make_commissioning_controller()
        await controller.start_commissioning()
        controller._tasks_queue.get_nowait()
        running_task = asyncio.create_task(asyncio.sleep(10))
        controller._current_commissioning_task = running_task
        try:
            assert await controller.cancel_commissioning() is True
            # Task has been cancelled but hasn't completed yet; second call also stops.
            assert await controller.cancel_commissioning() is True
        finally:
            running_task.cancel()
            try:
                await running_task
            except asyncio.CancelledError:
                pass


class TestCommissioningTaskTerminalBranches:
    @pytest.mark.asyncio
    async def test_happy_path_sets_completed(self):
        # pylint: disable=protected-access
        controller = _make_commissioning_controller()

        async def _fake_send_with_retry(*_args, **_kwargs):
            return None

        dali1_device = CommissioningDeviceSummary("0", "0xA1", [])
        dali2_device = CommissioningDeviceSummary("4", "0xB2", [])

        async def _fake_apply(_res_dali, _res_dali2):
            controller.dali_devices = cast(Any, [SimpleNamespace(uid="0", name="0xA1", groups=[])])
            controller.dali2_devices = cast(Any, [SimpleNamespace(uid="4", name="0xB2", groups=[])])

        controller._apply_commissioning_results = AsyncMock(side_effect=_fake_apply)

        def _make_commissioning(_driver, _old_devices, _dali2, _progress_cb):
            async def _fake_smart_extend():
                return CommissioningResult()

            return SimpleNamespace(smart_extend=_fake_smart_extend)

        with patch(
            "wb.mqtt_dali.application_controller.send_with_retry",
            new=AsyncMock(side_effect=_fake_send_with_retry),
        ), patch("wb.mqtt_dali.application_controller.Commissioning") as mock_cls, patch(
            "wb.mqtt_dali.application_controller.asyncio.sleep",
            new=AsyncMock(return_value=None),
        ):
            mock_cls.side_effect = _make_commissioning
            await controller._commissioning_task()

        assert controller._commissioning_state.status == CommissioningStatus.COMPLETED
        assert controller._commissioning_state.progress == 100
        assert controller._commissioning_state.finished_at is not None
        assert controller._commissioning_state.devices == [dali1_device, dali2_device]
        controller._apply_commissioning_results.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_failure_before_dali1_leaves_bus_untouched(self):
        # pylint: disable=protected-access
        controller = _make_commissioning_controller()
        original_dali = [
            SimpleNamespace(uid="7", name="0x7", groups=[], address=DaliDeviceAddress(short=7, random=0x7))
        ]
        controller.dali_devices = cast(Any, list(original_dali))

        controller._apply_commissioning_results = AsyncMock()

        async def _send_start_failure(_dev, cmd, *_args, **_kwargs):
            # Fail at the very first send_with_retry (StartQuiescentMode).
            if type(cmd).__name__ == "StartQuiescentMode":
                raise RuntimeError("bus offline")
            return None

        with patch(
            "wb.mqtt_dali.application_controller.send_with_retry",
            new=AsyncMock(side_effect=_send_start_failure),
        ), patch(
            "wb.mqtt_dali.application_controller.asyncio.sleep",
            new=AsyncMock(return_value=None),
        ):
            with pytest.raises(RuntimeError):
                await controller._commissioning_task()

        assert controller._commissioning_state.status == CommissioningStatus.FAILED
        assert controller._commissioning_state.error == "bus offline"
        # Neither phase started, so _apply_commissioning_results not called and devices untouched.
        controller._apply_commissioning_results.assert_not_awaited()
        # Bus device lists are untouched.
        assert controller.dali_devices == original_dali

    @pytest.mark.asyncio
    async def test_failure_after_dali1_aborts_without_applying_and_marks_failed(self):
        # pylint: disable=protected-access
        controller = _make_commissioning_controller()
        controller.dali_devices = cast(
            Any,
            [SimpleNamespace(uid="1", name="0x1", groups=[], address=DaliDeviceAddress(short=1, random=0x1))],
        )

        controller._apply_commissioning_results = AsyncMock()

        dali1_device = CommissioningDeviceSummary("1", "0x1", [])

        call_count = {"n": 0}

        def _make_commissioning(_driver, _old_devices, _dali2, progress_cb):
            call_count["n"] += 1
            my_call = call_count["n"]

            async def _fake_smart_extend():
                if my_call == 1:
                    # DALI1 phase emits a found-device event, then succeeds.
                    if progress_cb is not None:
                        progress_cb(CommissioningStage.BINARY_SEARCH, 100, dali1_device)
                    return CommissioningResult()
                # DALI-2 phase raises before emitting anything.
                raise RuntimeError("DALI-2 phase failed")

            return SimpleNamespace(smart_extend=_fake_smart_extend)

        with patch(
            "wb.mqtt_dali.application_controller.send_with_retry",
            new=AsyncMock(return_value=None),
        ), patch("wb.mqtt_dali.application_controller.Commissioning") as mock_cls, patch(
            "wb.mqtt_dali.application_controller.asyncio.sleep",
            new=AsyncMock(return_value=None),
        ):
            mock_cls.side_effect = _make_commissioning
            with pytest.raises(RuntimeError):
                await controller._commissioning_task()

        assert controller._commissioning_state.status == CommissioningStatus.FAILED
        assert controller._commissioning_state.error == "DALI-2 phase failed"
        assert controller._commissioning_state.finished_at is not None
        # devices summary on FAILED reflects the untouched device list.
        assert controller._commissioning_state.devices == [dali1_device]
        # Atomic: a sub-scan failure aborts before any result (including the
        # successful DALI1 scan) is applied.
        controller._apply_commissioning_results.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_apply_failure_after_scan_still_completes(self):
        """Both scans succeed, then applying the results raises. Reading device info is
        best-effort: state was already being mutated, so the run must still finish
        COMPLETED (reporting FAILED would leave the frontend showing the pre-scan tree),
        the error is swallowed (logged, not re-raised), and no exception escapes.
        """
        # pylint: disable=protected-access
        controller = _make_commissioning_controller()
        controller.dali_devices = cast(
            Any,
            [SimpleNamespace(uid="1", name="0x1", groups=[], address=DaliDeviceAddress(short=1, random=0x1))],
        )

        controller._apply_commissioning_results = AsyncMock(side_effect=RuntimeError("publish failed"))

        def _make_commissioning(_driver, _old_devices, _dali2, _progress_cb):
            async def _fake_smart_extend():
                return CommissioningResult()

            return SimpleNamespace(smart_extend=_fake_smart_extend)

        with patch(
            "wb.mqtt_dali.application_controller.send_with_retry",
            new=AsyncMock(return_value=None),
        ), patch("wb.mqtt_dali.application_controller.Commissioning") as mock_cls, patch(
            "wb.mqtt_dali.application_controller.asyncio.sleep",
            new=AsyncMock(return_value=None),
        ):
            mock_cls.side_effect = _make_commissioning
            await controller._commissioning_task()

        assert controller._commissioning_state.status == CommissioningStatus.COMPLETED
        assert controller._commissioning_state.progress == 100
        assert controller._commissioning_state.error is None
        controller._apply_commissioning_results.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancel_sets_cancelled_and_reraises(self):
        # pylint: disable=protected-access
        controller = _make_commissioning_controller()
        original_dali = [
            SimpleNamespace(uid="9", name="0x9", groups=[], address=DaliDeviceAddress(short=9, random=0x9))
        ]
        original_dali2 = [
            SimpleNamespace(uid="10", name="0xA", groups=[], address=DaliDeviceAddress(short=10, random=0xA))
        ]
        controller.dali_devices = cast(Any, list(original_dali))
        controller.dali2_devices = cast(Any, list(original_dali2))

        controller._apply_commissioning_results = AsyncMock()

        stop_calls = []

        async def _fake_send_with_retry(_dev, cmd, *_args, **_kwargs):
            stop_calls.append(type(cmd).__name__)
            return None

        def _make_commissioning(*_args, progress_cb=None, **_kwargs):
            async def _fake_smart_extend():
                # Simulate a device being found right before cancellation arrives —
                # CANCELLED semantics must still null out the devices list.
                if progress_cb is not None:
                    progress_cb(
                        CommissioningStage.BINARY_SEARCH,
                        50,
                        CommissioningDeviceSummary("9", "0x9", []),
                    )
                raise asyncio.CancelledError()

            return SimpleNamespace(smart_extend=_fake_smart_extend)

        with patch(
            "wb.mqtt_dali.application_controller.send_with_retry",
            new=AsyncMock(side_effect=_fake_send_with_retry),
        ), patch("wb.mqtt_dali.application_controller.Commissioning") as mock_cls, patch(
            "wb.mqtt_dali.application_controller.asyncio.sleep",
            new=AsyncMock(return_value=None),
        ):
            mock_cls.side_effect = _make_commissioning
            with pytest.raises(asyncio.CancelledError):
                await controller._commissioning_task()

        assert controller._commissioning_state.status == CommissioningStatus.CANCELLED
        assert controller._commissioning_state.finished_at is not None
        # CANCELLED semantics: devices=None (published as null), no apply,
        # and bus device lists are left untouched.
        assert not controller._commissioning_state.devices
        controller._apply_commissioning_results.assert_not_awaited()
        assert controller.dali_devices == original_dali
        assert controller.dali2_devices == original_dali2
        # StopQuiescentMode still called via finally.
        assert "StopQuiescentMode" in stop_calls


class TestReadDeviceInfoProgress:
    """Per-device progress reporting on the READ_DEVICE_INFO stage (81..99)."""

    @staticmethod
    def _make_commissioning_factory(res_dali, res_dali2):
        def _make_commissioning(_driver, _old_devices, dali2, _progress_cb):
            async def _fake_smart_extend():
                return res_dali2 if dali2 else res_dali

            return SimpleNamespace(smart_extend=_fake_smart_extend)

        return _make_commissioning

    @pytest.mark.asyncio
    async def test_read_progress_advances_per_device(self):
        # pylint: disable=protected-access
        """N>=2 new DALI 1 devices produce N monotonic READ_DEVICE_INFO snapshots in [81, 99],
        followed by a final COMPLETED snapshot with progress=100.
        """
        controller = _make_commissioning_controller()

        snapshots: list[tuple[CommissioningStatus, int]] = []

        def _cb(state):
            snapshots.append((state.status, state.progress))

        controller._commissioning_state_cb = _cb

        new_addresses = [DaliDeviceAddress(short=i, random=i) for i in range(3)]
        res_dali = CommissioningResult(new=new_addresses)
        res_dali2 = CommissioningResult()

        with patch(
            "wb.mqtt_dali.application_controller.send_with_retry",
            new=AsyncMock(return_value=None),
        ), patch("wb.mqtt_dali.application_controller.Commissioning") as mock_cls, patch(
            "wb.mqtt_dali.application_controller.asyncio.sleep",
            new=AsyncMock(return_value=None),
        ), patch(
            "wb.mqtt_dali.application_controller.read_product_name",
            new=AsyncMock(return_value=None),
        ):
            mock_cls.side_effect = self._make_commissioning_factory(res_dali, res_dali2)
            await controller._commissioning_task()

        read_progress = [p for status, p in snapshots if status == CommissioningStatus.READ_DEVICE_INFO]
        # mark_reading_info publishes 81, then one snapshot per device.
        assert read_progress[0] == 81
        per_device = read_progress[1:]
        assert len(per_device) == 3
        # Monotonic, bounded by [81, 99], ends at 99.
        assert all(81 <= p <= 99 for p in per_device)
        assert per_device == sorted(per_device)
        assert per_device[-1] == 99
        # Final snapshot is COMPLETED at 100.
        assert snapshots[-1] == (CommissioningStatus.COMPLETED, 100)

    @pytest.mark.asyncio
    async def test_read_progress_no_changes(self):
        # pylint: disable=protected-access
        """When both buses report only unchanged devices, no intermediate READ_DEVICE_INFO
        snapshots are emitted — progress jumps 81 → 100 with nothing in between.
        """
        controller = _make_commissioning_controller()

        snapshots: list[tuple[CommissioningStatus, int]] = []

        def _cb(state):
            snapshots.append((state.status, state.progress))

        controller._commissioning_state_cb = _cb

        res_dali = CommissioningResult(unchanged=[DaliDeviceAddress(short=0, random=1)])
        res_dali2 = CommissioningResult(unchanged=[DaliDeviceAddress(short=1, random=2)])

        with patch(
            "wb.mqtt_dali.application_controller.send_with_retry",
            new=AsyncMock(return_value=None),
        ), patch("wb.mqtt_dali.application_controller.Commissioning") as mock_cls, patch(
            "wb.mqtt_dali.application_controller.asyncio.sleep",
            new=AsyncMock(return_value=None),
        ), patch(
            "wb.mqtt_dali.application_controller.read_product_name",
            new=AsyncMock(return_value=None),
        ):
            mock_cls.side_effect = self._make_commissioning_factory(res_dali, res_dali2)
            await controller._commissioning_task()

        read_progress = [p for status, p in snapshots if status == CommissioningStatus.READ_DEVICE_INFO]
        # Only the mark_reading_info() entry snapshot — no per-device snapshots.
        assert read_progress == [81]
        assert snapshots[-1] == (CommissioningStatus.COMPLETED, 100)

    @pytest.mark.asyncio
    async def test_read_progress_counts_failed_init(self):
        # pylint: disable=protected-access
        """The read-progress counter advances once per device even when
        ``try_initialize_device`` returns False for some of them: the for-loop in
        ``_update_dali_devices`` calls the progress callback after every
        ``_try_init_new_device`` call regardless of its outcome. Progress reaches
        99 by the end of the stage and the run terminates COMPLETED.
        """
        controller = _make_commissioning_controller()
        # Use the real wrapper so the for-loop in _update_dali_devices actually
        # iterates and calls our progress callback after each device.
        del controller._try_init_new_device

        snapshots: list[tuple[CommissioningStatus, int]] = []

        def _cb(state):
            snapshots.append((state.status, state.progress))

        controller._commissioning_state_cb = _cb

        new_addresses = [DaliDeviceAddress(short=i, random=i) for i in range(3)]
        res_dali = CommissioningResult(new=new_addresses)
        res_dali2 = CommissioningResult()

        # Middle device's init returns False; we don't model the underlying
        # exception — the production wrapper turns the exception into a False
        # return, and that's the only contract the progress loop cares about.
        try_init = AsyncMock(side_effect=[True, False, True])

        with patch(
            "wb.mqtt_dali.application_controller.send_with_retry",
            new=AsyncMock(return_value=None),
        ), patch("wb.mqtt_dali.application_controller.Commissioning") as mock_cls, patch(
            "wb.mqtt_dali.application_controller.asyncio.sleep",
            new=AsyncMock(return_value=None),
        ), patch(
            "wb.mqtt_dali.application_controller.read_product_name",
            new=AsyncMock(return_value=None),
        ), patch(
            "wb.mqtt_dali.application_controller.try_initialize_device",
            new=try_init,
        ):
            mock_cls.side_effect = self._make_commissioning_factory(res_dali, res_dali2)
            await controller._commissioning_task()

        # All three devices reached try_initialize_device regardless of the
        # middle one's False return — proving the progress loop did not abort
        # on a failed init.
        assert try_init.await_count == 3
        read_progress = [p for status, p in snapshots if status == CommissioningStatus.READ_DEVICE_INFO]
        per_device = read_progress[1:]
        assert len(per_device) == 3
        assert per_device[-1] == 99
        assert snapshots[-1] == (CommissioningStatus.COMPLETED, 100)

    @pytest.mark.asyncio
    async def test_read_progress_counts_both_buses(self):
        # pylint: disable=protected-access
        """Devices from DALI 1 and DALI 2 commissioning results both contribute to the
        total; the last READ_DEVICE_INFO snapshot before COMPLETED is at progress=99.
        """
        controller = _make_commissioning_controller()

        snapshots: list[tuple[CommissioningStatus, int]] = []

        def _cb(state):
            snapshots.append((state.status, state.progress))

        controller._commissioning_state_cb = _cb

        # DALI-1: 2 new + 1 changed; DALI-2: 1 new + 1 changed -> total = 5.
        controller.dali_devices = cast(
            Any,
            [SimpleNamespace(address=DaliDeviceAddress(short=10, random=0xAA), mqtt_id="gw_bus_1_10")],
        )
        controller.dali2_devices = cast(
            Any,
            [SimpleNamespace(address=DaliDeviceAddress(short=20, random=0xBB), mqtt_id="gw_bus_1_d2_20")],
        )
        res_dali = CommissioningResult(
            new=[DaliDeviceAddress(short=1, random=1), DaliDeviceAddress(short=2, random=2)],
            changed=[ChangedDevice(new=DaliDeviceAddress(short=10, random=0xCC), old_short=10)],
        )
        res_dali2 = CommissioningResult(
            new=[DaliDeviceAddress(short=21, random=0x21)],
            changed=[ChangedDevice(new=DaliDeviceAddress(short=20, random=0xDD), old_short=20)],
        )

        with patch(
            "wb.mqtt_dali.application_controller.send_with_retry",
            new=AsyncMock(return_value=None),
        ), patch("wb.mqtt_dali.application_controller.Commissioning") as mock_cls, patch(
            "wb.mqtt_dali.application_controller.asyncio.sleep",
            new=AsyncMock(return_value=None),
        ), patch(
            "wb.mqtt_dali.application_controller.read_product_name",
            new=AsyncMock(return_value=None),
        ):
            mock_cls.side_effect = self._make_commissioning_factory(res_dali, res_dali2)
            await controller._commissioning_task()

        # 5 _try_init_new_device calls total across both buses.
        assert controller._try_init_new_device.await_count == 5
        read_progress = [p for status, p in snapshots if status == CommissioningStatus.READ_DEVICE_INFO]
        per_device = read_progress[1:]
        assert len(per_device) == 5
        assert per_device[-1] == 99
        assert snapshots[-1] == (CommissioningStatus.COMPLETED, 100)


class TestFinishedAtLifecycle:
    def test_queued_after_completed_clears_finished_at(self):
        state = CommissioningState()
        state.mark_queued()
        assert state.finished_at is None
        state.mark_completed([])
        finished_at = state.finished_at
        assert finished_at is not None
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", finished_at)
        # Next queued transition resets finished_at back to None.
        state.mark_queued()
        assert state.finished_at is None

    def test_queued_after_failed_clears_finished_at(self):
        state = CommissioningState()
        state.mark_failed("boom", [])
        assert state.finished_at is not None
        state.mark_queued()
        assert state.finished_at is None


class TestRunCommissioningInChildTask:
    """Worker-level wrapper must survive user-initiated cancel of the child."""

    @pytest.mark.asyncio
    async def test_user_cancel_of_child_is_consumed_and_handle_cleared(self):
        # pylint: disable=protected-access
        controller = _make_commissioning_controller()

        commissioning_started = asyncio.Event()

        async def _fake_commissioning_task():
            commissioning_started.set()
            # Sleep propagates CancelledError, mimicking _commissioning_task
            # re-raising after publishing CANCELLED in the except branch.
            await asyncio.sleep(10)

        controller._commissioning_task = _fake_commissioning_task

        wrapper = asyncio.create_task(controller._run_commissioning_in_child_task())
        await commissioning_started.wait()
        assert controller._current_commissioning_task is not None

        # User-initiated cancel of the child only:
        controller._current_commissioning_task.cancel()

        # Wrapper must complete normally — its job is to isolate the worker
        # from child CancelledError. A propagated CancelledError here would
        # kill the worker, which is exactly the regression we guard against.
        await asyncio.wait_for(wrapper, timeout=1.0)
        assert wrapper.done()
        assert not wrapper.cancelled()
        assert wrapper.exception() is None
        # Handle must be cleared so a subsequent cancel_commissioning sees no
        # running task.
        assert controller._current_commissioning_task is None

    @pytest.mark.asyncio
    async def test_worker_survives_user_cancel_and_processes_next_task(self):
        # pylint: disable=protected-access
        """End-to-end: after user-cancel of commissioning, a subsequent queued
        task is still processed by the same wrapper coroutine on the next
        invocation. This exercises the contract that the wrapper is a
        well-behaved async unit that can be called repeatedly.
        """
        controller = _make_commissioning_controller()

        async def _fake_commissioning_task():
            # CancelledError from sleep propagates up; mimics _commissioning_task
            # re-raising after publishing CANCELLED.
            await asyncio.sleep(10)

        controller._commissioning_task = _fake_commissioning_task

        # First run — cancelled by user.
        wrapper = asyncio.create_task(controller._run_commissioning_in_child_task())
        await asyncio.sleep(0)  # let wrapper schedule child
        assert controller._current_commissioning_task is not None
        controller._current_commissioning_task.cancel()
        await asyncio.wait_for(wrapper, timeout=1.0)

        # Second run — succeeds normally, proving the wrapper is reusable and
        # the internal bookkeeping (handle field) recovered after user-cancel.
        completed = asyncio.Event()

        async def _second_task():
            completed.set()

        controller._commissioning_task = _second_task
        await asyncio.wait_for(controller._run_commissioning_in_child_task(), timeout=1.0)
        assert completed.is_set()
        assert controller._current_commissioning_task is None


class TestStopDuringRunningScan:
    """stop() must cancel a running commissioning child before the worker.

    The plan (section "Остановка сканирования") mandates the order:
      1. cancel the commissioning child so its ``except CancelledError``
         gets a chance to publish CANCELLED;
      2. then cancel the polling task so the worker unwinds.
    Skipping step 1 would leave the child orphaned and skip CANCELLED
    publication + inner StopQuiescentMode cleanup.
    """

    def _prepare_for_stop(self, controller):
        # pylint: disable=protected-access
        """Fill in the fields stop() touches so we can call it directly."""
        controller._state = ApplicationControllerState.READY
        controller._bus_traffic_cleanup = MagicMock()
        one_shot = MagicMock()
        one_shot.stop = AsyncMock()
        controller._one_shot_tasks = one_shot
        controller._quiescent_mode_timer = None
        controller._init_scheduler = MagicMock()
        controller._websocket_lock = asyncio.Lock()
        controller._stop_websocket = AsyncMock()
        controller._device_publisher = AsyncMock()
        controller._dev = AsyncMock()

    @pytest.mark.asyncio
    async def test_cancels_child_before_polling_task(self):
        # pylint: disable=protected-access
        controller = _make_commissioning_controller()
        self._prepare_for_stop(controller)

        statuses: list[CommissioningStatus] = []

        def _cb(state):
            statuses.append(state.status)

        controller._commissioning_state_cb = _cb
        controller._commissioning_state.status = CommissioningStatus.QUERY_SHORT_ADDRESSES

        child_entered = asyncio.Event()

        async def _fake_commissioning_task():
            # pylint: disable=protected-access
            # Behaves like the real _commissioning_task: on CancelledError it
            # publishes CANCELLED from inside the except branch, then re-raises.
            child_entered.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                controller._commissioning_state.mark_cancelled()
                controller._publish_commissioning_state()
                raise

        controller._commissioning_task = _fake_commissioning_task

        # Start the wrapper directly as the "polling task" — in production the
        # wrapper is invoked from _polling_loop, but for this test exercising
        # stop()'s teardown we only need an awaitable task that finishes when
        # the wrapper does.
        controller._polling_task = asyncio.create_task(controller._run_commissioning_in_child_task())
        await child_entered.wait()
        child = controller._current_commissioning_task
        assert child is not None and not child.done()

        await controller.stop()

        # Child must have been cancelled; CANCELLED must have been published
        # from the child's except branch before the worker was torn down.
        assert child.cancelled() or child.done()
        assert CommissioningStatus.CANCELLED in statuses
        # stop() cleared the polling task handle.
        assert controller._polling_task is None

    @pytest.mark.asyncio
    async def test_stop_without_running_scan_does_not_crash(self):
        # pylint: disable=protected-access
        controller = _make_commissioning_controller()
        self._prepare_for_stop(controller)
        controller._polling_task = None
        controller._current_commissioning_task = None
        # Must not raise even when there is no scan in flight.
        await controller.stop()


class TestRetainedLifecycleIntegration:
    """End-to-end sequence of state callback invocations across a full run."""

    @pytest.mark.asyncio
    async def test_full_cycle_publishes_running_then_completed(self):
        # pylint: disable=protected-access
        controller = _make_commissioning_controller()
        statuses: list[CommissioningStatus] = []

        def _cb(state):
            statuses.append(state.status)

        # Gateway publishes the lifecycle IDLE from its own start() hook,
        # not via the controller's callback — here we only assert the
        # transitions produced by the controller itself.
        controller._commissioning_state_cb = _cb

        # Start commissioning → QUEUED.
        await controller.start_commissioning(on_state_changed=_cb)

        # Simulate the commissioning task reaching the happy-path terminal branch.
        controller._apply_commissioning_results = AsyncMock()

        async def _fake_smart_extend():
            return CommissioningResult()

        with patch(
            "wb.mqtt_dali.application_controller.send_with_retry",
            new=AsyncMock(return_value=None),
        ), patch("wb.mqtt_dali.application_controller.Commissioning") as mock_cls, patch(
            "wb.mqtt_dali.application_controller.asyncio.sleep",
            new=AsyncMock(return_value=None),
        ):
            mock_cls.return_value = SimpleNamespace(smart_extend=_fake_smart_extend)
            await controller._commissioning_task()

        # Sequence: QUEUED (start_commissioning) → COMPLETED.
        assert statuses[0] == CommissioningStatus.QUEUED
        assert statuses[-1] == CommissioningStatus.COMPLETED
        # No FAILED/CANCELLED leaked in.
        assert CommissioningStatus.FAILED not in statuses
        assert CommissioningStatus.CANCELLED not in statuses

    @pytest.mark.asyncio
    async def test_cancel_cycle_publishes_running_then_cancelled(self):
        # pylint: disable=protected-access
        controller = _make_commissioning_controller()
        statuses: list[CommissioningStatus] = []

        def _cb(state):
            statuses.append(state.status)

        controller._commissioning_state_cb = _cb
        await controller.start_commissioning(on_state_changed=_cb)

        controller._apply_commissioning_results = AsyncMock()

        async def _fake_smart_extend():
            raise asyncio.CancelledError()

        with patch(
            "wb.mqtt_dali.application_controller.send_with_retry",
            new=AsyncMock(return_value=None),
        ), patch("wb.mqtt_dali.application_controller.Commissioning") as mock_cls, patch(
            "wb.mqtt_dali.application_controller.asyncio.sleep",
            new=AsyncMock(return_value=None),
        ):
            mock_cls.return_value = SimpleNamespace(smart_extend=_fake_smart_extend)
            with pytest.raises(asyncio.CancelledError):
                await controller._commissioning_task()

        assert statuses[0] == CommissioningStatus.QUEUED
        assert statuses[-1] == CommissioningStatus.CANCELLED
        assert CommissioningStatus.COMPLETED not in statuses


# --- polling_interval clamp (S1) ---------------------------------------------


def _make_polling_controller():
    """Return a bare controller with just enough state to exercise set_polling_interval."""
    controller = ApplicationController.__new__(ApplicationController)
    controller.logger = logging.getLogger("test.polling")
    return controller


class TestPollingIntervalClamp:
    def test_set_polling_interval_clamped(self, caplog):
        """set_polling_interval(<1) is clamped to MIN_POLLING_INTERVAL with a warning."""
        controller = _make_polling_controller()

        with caplog.at_level(logging.WARNING, logger="test.polling"):
            controller.set_polling_interval(0)
            assert controller.polling_interval == MIN_POLLING_INTERVAL
            controller.set_polling_interval(0.5)
            assert controller.polling_interval == MIN_POLLING_INTERVAL
            controller.set_polling_interval(-1)
            assert controller.polling_interval == MIN_POLLING_INTERVAL

        # Every offending call must log, no dedup.
        clamp_warnings = [r for r in caplog.records if "clamping" in r.getMessage()]
        assert len(clamp_warnings) == 3

    def test_polling_interval_one_or_more_passes_through(self, caplog):
        """Values >= MIN_POLLING_INTERVAL are accepted unchanged and emit no warning."""
        controller = _make_polling_controller()

        with caplog.at_level(logging.WARNING, logger="test.polling"):
            for value in (1, 5, 10):
                controller.set_polling_interval(value)
                assert controller.polling_interval == value

        assert not any("clamping" in r.getMessage() for r in caplog.records)

    def test_polling_interval_clamped_on_load(self, caplog):
        """bus_from_json with sub-1s polling_interval routes through set_polling_interval and clamps."""
        for bad_value in (0, -1, 0.5):
            with caplog.at_level(logging.WARNING):
                bus = bus_from_json(
                    "gw1",
                    1,
                    {"devices": [], "polling_interval": bad_value},
                    MagicMock(),
                    MagicMock(),
                )
            assert bus.polling_interval == MIN_POLLING_INTERVAL
            assert any("clamping" in r.getMessage() for r in caplog.records)
            caplog.clear()


@pytest.mark.asyncio
async def test_rpc_config_update_clamps_polling_interval():
    # pylint: disable=protected-access
    """SetBus RPC with polling_interval=0 reaches the controller as MIN_POLLING_INTERVAL."""
    bus = bus_from_json("gw1", 1, {"devices": [], "polling_interval": 5.0}, MagicMock(), MagicMock())
    svc = Gateway.__new__(Gateway)
    svc.wb_dali_gateways = [WbDaliGateway(uid="gw1", buses=[bus])]
    svc._save_configuration = AsyncMock()

    result = await svc.set_bus_rpc_handler({"busId": "gw1_bus_1", "config": {"polling_interval": 0}})

    assert result["polling_interval"] == MIN_POLLING_INTERVAL
    assert bus.polling_interval == MIN_POLLING_INTERVAL


# --- polling-loop fallback under sustained EXECUTE_CONTROL load ---------------


_make_polling_loop_controller = make_loop_controller


async def _cancel_loop(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


_stop_polling_loop = stop_loop


async def _run_loop_briefly(controller: ApplicationController, duration: float) -> None:
    # pylint: disable=protected-access
    """Run _polling_loop for `duration` seconds, then cancel cleanly."""
    task = asyncio.create_task(controller._polling_loop())
    try:
        await asyncio.sleep(duration)
    finally:
        await _cancel_loop(task)


def _make_execute_control(control_id: str = "ctrl", value_to_set: str = "payload") -> MagicMock:
    """Build a stand-in for MqttControlBase carrying the fields the loop touches."""
    control = MagicMock()
    control.control_info.id = control_id
    control.control_info.value = None
    control.value_to_set = value_to_set
    return control


def _make_execute_control_task(device, control=None, control_id="ctrl") -> ApplicationControllerTask:
    if control is None:
        control = _make_execute_control(control_id=control_id)
    return ApplicationControllerTask(
        task_type=ApplicationControllerTaskType.EXECUTE_CONTROL,
        data=(device, control),
    )


class TestPollingLoopFallback:
    @pytest.mark.asyncio
    async def test_poll_runs_when_queue_empties_after_interval(self):
        # pylint: disable=protected-access
        """After EXECUTE_CONTROL drains, _poll_step fires immediately, not after queue_timeout."""
        controller = _make_polling_loop_controller(polling_interval=1.0)
        controller._poll_step = AsyncMock(return_value=10.0)
        device = MagicMock()
        device.execute_control = AsyncMock(return_value=None)

        task = _make_execute_control_task(device)
        controller._tasks_queue.put_nowait(task)

        # Poll step returns a long timeout so any fallback fire would be visible
        # only via the inline empty-queue check, not via TimeoutError.
        await _run_loop_briefly(controller, 0.05)

        controller._poll_step.assert_awaited()
        device.execute_control.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_command_stream_does_not_starve_polling(self):
        # pylint: disable=protected-access
        """Bursts of EXECUTE_CONTROL with sub-interval gaps still let polling slip in."""
        controller = _make_polling_loop_controller(polling_interval=0.05)
        device = MagicMock()
        device.is_initialized = True
        device.execute_control = AsyncMock(return_value=None)
        device.time_until_next_poll = MagicMock(return_value=0.05)
        controller.dali_devices = [device]
        controller._poll_devices = AsyncMock()

        controller._tasks_queue.put_nowait(_make_execute_control_task(device, control_id="ctrl0"))

        loop_task = asyncio.create_task(controller._polling_loop())
        try:
            for i in range(1, 4):
                await asyncio.sleep(0.08)
                cid = f"ctrl{i}"
                controller._tasks_queue.put_nowait(_make_execute_control_task(device, control_id=cid))
            await asyncio.sleep(0.08)
        finally:
            await _cancel_loop(loop_task)

        # poll_turn alternates each tick, so at most one poll per inter-command gap.
        assert device.execute_control.await_count == 4
        assert controller._poll_devices.await_count >= 2

    @pytest.mark.asyncio
    async def test_polling_waits_for_queue_to_drain(self):
        # pylint: disable=protected-access
        """While the queue stays non-empty, polling does not fire (commands have priority)."""
        controller = _make_polling_loop_controller(polling_interval=0.05)
        device = MagicMock()
        device.is_initialized = True
        device.time_until_next_poll = MagicMock(return_value=0.05)

        async def _slow_execute(*_args, **_kwargs):
            await asyncio.sleep(0)

        device.execute_control = AsyncMock(side_effect=_slow_execute)
        controller.dali_devices = [device]
        controller._poll_devices = AsyncMock()

        counter = 0

        def _enqueue_pair():
            # pylint: disable=protected-access
            nonlocal counter
            for _ in range(2):
                cid = f"c{counter}"
                counter += 1
                controller._tasks_queue.put_nowait(_make_execute_control_task(device, control_id=cid))

        async def _execute_and_refill(*_args, **_kwargs):
            # Refill synchronously so the polling loop's inline empty() check always sees the queue non-empty.
            # pylint: disable=protected-access
            if controller._tasks_queue.qsize() < 4:
                _enqueue_pair()

        device.execute_control = AsyncMock(side_effect=_execute_and_refill)
        controller.dali_devices = [device]

        _enqueue_pair()

        loop_task = asyncio.create_task(controller._polling_loop())
        try:
            await asyncio.sleep(0.3)
        finally:
            await _stop_polling_loop(controller, loop_task)

        assert device.execute_control.await_count >= 10
        assert controller._poll_devices.await_count == 0

    @pytest.mark.asyncio
    async def test_poll_runs_after_non_execute_control_task(self):
        # pylint: disable=protected-access
        """Inline poll check fires after a single non-EXECUTE_CONTROL task too."""
        controller = _make_polling_loop_controller(polling_interval=1.0)
        controller._poll_step = AsyncMock(return_value=10.0)
        device = MagicMock()
        device.load_info = AsyncMock(return_value=None)

        load_info_task = ApplicationControllerTask(
            task_type=ApplicationControllerTaskType.LOAD_INFO,
            data=(device, False),
        )
        controller._tasks_queue.put_nowait(load_info_task)

        await _run_loop_briefly(controller, 0.05)

        device.load_info.assert_awaited_once()
        controller._poll_step.assert_awaited()

    @pytest.mark.asyncio
    async def test_polling_interval_change_applies_within_one_second(self):
        # pylint: disable=protected-access
        """Lowering polling_interval at runtime takes effect within ~1s."""
        controller = _make_polling_loop_controller(polling_interval=10.0)
        device = MagicMock()
        device.is_initialized = True
        device.time_until_next_poll = MagicMock(side_effect=lambda _t, default: default)
        controller.dali_devices = [device]

        async def _drain(scheduler, current_time):
            del current_time
            scheduler._current_device_index = len(scheduler._devices)

        controller._poll_devices = AsyncMock(side_effect=_drain)
        controller._poll_scheduler.poll_turn = True

        loop_task = asyncio.create_task(controller._polling_loop())
        try:
            # With interval=10 capped to 1.0 by min() in _poll_step, no extra poll within 0.2s.
            await asyncio.sleep(0.2)
            polls_before = controller._poll_devices.await_count
            assert polls_before == 1, "expected exactly one initial poll"

            controller._polling_interval = 1.0
            await asyncio.sleep(1.2)
            polls_after = controller._poll_devices.await_count
        finally:
            await _cancel_loop(loop_task)

        assert polls_after > polls_before

    @pytest.mark.asyncio
    async def test_execute_control_batch_failure_resolves_all_futures(self):
        # pylint: disable=protected-access
        """An unexpected error while dispatching the EXECUTE_CONTROL batch must
        resolve every pulled-off future (with the exception) so no one-shot
        on-topic write hangs, and the loop keeps running."""
        controller = _make_polling_loop_controller(polling_interval=1.0)
        controller._poll_step = AsyncMock(return_value=10.0)
        device = MagicMock()
        # Raise synchronously while building the gather call, escaping the
        # gather(return_exceptions=True) net and hitting the recovery branch.
        device.execute_control = MagicMock(side_effect=RuntimeError("boom"))

        task1 = _make_execute_control_task(device, control_id="ctrl1")
        task2 = _make_execute_control_task(device, control_id="ctrl2")
        controller._tasks_queue.put_nowait(task1)
        controller._tasks_queue.put_nowait(task2)

        await _run_loop_briefly(controller, 0.05)

        for task in (task1, task2):
            assert task.future.done()
            assert isinstance(task.future.exception(), RuntimeError)


@pytest.mark.asyncio
async def test_polling_loop_command_stream_does_not_starve_polling():
    # pylint: disable=protected-access
    """EXECUTE_CONTROL arriving faster than the previous wait_for budget must not starve polling."""
    controller = _make_polling_loop_controller(polling_interval=0.05)
    device = MagicMock()
    device.is_initialized = True
    device.execute_control = AsyncMock(return_value=None)
    device.time_until_next_poll = MagicMock(return_value=0.05)
    controller.dali_devices = [device]

    poll_count = 0

    async def _fake_poll_devices(poll_scheduler, current_time):
        nonlocal poll_count
        del current_time
        poll_count += 1
        # Drain the round so the next iteration rebuilds via set_devices.
        poll_scheduler._current_device_index = len(poll_scheduler._devices)

    controller._poll_devices = _fake_poll_devices

    controller._tasks_queue.put_nowait(_make_execute_control_task(device, control_id="ctrl0"))

    loop_task = asyncio.create_task(controller._polling_loop())
    try:
        # 80 ms gaps > polling_interval (50 ms) but < the stale 1.0 s queue_timeout that produced the bug.
        for i in range(1, 6):
            await asyncio.sleep(0.08)
            controller._tasks_queue.put_nowait(_make_execute_control_task(device, control_id=f"ctrl{i}"))
        await asyncio.sleep(0.08)
    finally:
        await _cancel_loop(loop_task)

    assert device.execute_control.await_count == 6
    assert poll_count >= 2


@pytest.mark.asyncio
async def test_start_rolls_back_on_publisher_bringup_failure():
    """A bringup failure after the driver is up must leave a clean slate.

    The controller is built with its normal constructor (no I/O) but with the
    driver and publisher classes patched to mocks. The failure is injected at the
    virtual-device publication step (register_control_handler) — the dangerous
    case where the publisher is already initialized and has registered device
    state/subscriptions. start() must roll back symmetrically: clean up the
    publisher (so its state/subscriptions are unwound and a re-publish won't hit
    "Handler already registered"), deinitialize the driver, and re-raise. The
    rollback is observed publicly: the publisher cleanup and driver teardown were
    awaited, and a subsequent start() succeeds — only possible if the controller
    fell back to UNINITIALIZED, since start() rejects any other state.
    """
    with patch("wb.mqtt_dali.application_controller.WBDALIDriver") as driver_cls, patch(
        "wb.mqtt_dali.application_controller.DevicePublisher"
    ) as publisher_cls:
        driver = driver_cls.return_value
        driver.initialize = AsyncMock()
        driver.deinitialize = AsyncMock()
        publisher = publisher_cls.return_value
        publisher.initialize = AsyncMock()
        publisher.add_device = AsyncMock()
        publisher.register_control_handler = AsyncMock(side_effect=[RuntimeError("broker down"), None])
        publisher.cleanup = AsyncMock()

        config = ApplicationControllerConfig(
            gateway_mqtt_device_id="gw",
            bus=1,
            dali_devices=[],
            dali2_devices=[],
            polling_interval=1.0,
        )
        controller = ApplicationController(config, MagicMock(), MagicMock())

        with pytest.raises(RuntimeError, match="broker down"):
            await controller.start()

        publisher.cleanup.assert_awaited_once()
        driver.deinitialize.assert_awaited_once()
        assert controller.driver is driver

        # A clean re-start is only reachable from UNINITIALIZED, and it must
        # re-publish the broadcast device without a duplicate-handler error.
        await controller.start()
        await controller.stop()


# --- bus-monitor syslog mirroring --------------------------------------------


class _LogCapture:
    """Captures records emitted on a named logger (the per-bus controller logger)."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._prev_level = logging.NOTSET
        self.handler = logging.Handler()
        self.messages: list[str] = []
        self.levels: list[int] = []
        self.handler.emit = self._capture

    def _capture(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())
        self.levels.append(record.levelno)

    def __enter__(self) -> "_LogCapture":
        logger = logging.getLogger(self._name)
        self._prev_level = logger.level
        logger.addHandler(self.handler)
        logger.setLevel(logging.INFO)
        return self

    def __exit__(self, *exc) -> None:
        logger = logging.getLogger(self._name)
        logger.removeHandler(self.handler)
        logger.setLevel(self._prev_level)


def _monitor_bus(*, monitor: bool, syslog: bool, uid_bus: int = 1) -> tuple[ApplicationController, MagicMock]:
    """Build a real controller wired for the bus-monitor path.

    Returns the controller and its MQTT dispatcher mock. The publish is an
    AsyncMock so the one-shot publish task is awaitable.
    """
    dispatcher = MagicMock()
    dispatcher.client.publish = AsyncMock()
    bus = bus_from_json(
        "gw1",
        uid_bus,
        {"devices": [], "bus_monitor_enabled": monitor, "bus_monitor_syslog_enabled": syslog},
        dispatcher,
        MagicMock(),
    )
    return bus, dispatcher


@pytest.mark.asyncio
async def test_monitor_lines_logged_when_enabled():
    """With both the monitor and the syslog flag on, handling a command frame and
    its response emits a single combined monitor line: one MQTT publish and one
    log record. The log record is the MQTT line stripped of its leading
    `{HH:MM:SS.mmm} ` timestamp (journald stamps log records itself). Captured via
    a handler on the bus-uid logger."""
    bus, dispatcher = _monitor_bus(monitor=True, syslog=True)
    command = EnableInstance(DeviceShort(3), InstanceNumber(2))
    with _LogCapture(bus.uid) as log:
        bus.driver.bus_traffic.notify_command(
            command.frame, Response(BackwardFrame(42)), BusTrafficSource.WB, 0
        )
        await asyncio.sleep(0)

    dispatcher.client.publish.assert_called_once()
    mqtt_payload = dispatcher.client.publish.call_args.args[1]
    timestamp, _, body = mqtt_payload.partition(" ")
    assert re.fullmatch(r"\d{2}:\d{2}:\d{2}\.\d{3}", timestamp)
    assert " - " in body
    assert log.messages == [body]
    assert not log.messages[0].startswith(timestamp)


@pytest.mark.asyncio
async def test_monitor_request_with_response_is_one_combined_line():
    """A command frame paired with a response is published as exactly one MQTT
    line of the form `{request} - {format_response(response)}` — the request part
    (with its `>>` prefix) joined to the response by ` - `, with the response part
    carrying no leading `<<`. The combined line is timestamped once."""
    bus, dispatcher = _monitor_bus(monitor=True, syslog=False)
    command = EnableInstance(DeviceShort(3), InstanceNumber(2))
    response = Response(BackwardFrame(42))
    bus.driver.bus_traffic.notify_command(command.frame, response, BusTrafficSource.WB, 0)
    await asyncio.sleep(0)

    dispatcher.client.publish.assert_called_once()
    payload = dispatcher.client.publish.call_args.args[1]
    timestamp, _, body = payload.partition(" ")
    assert re.fullmatch(r"\d{2}:\d{2}:\d{2}\.\d{3}", timestamp)

    request_part, sep, response_part = body.partition(" - ")
    assert sep == " - "
    assert request_part.startswith(">>")
    assert response_part == format_response(response)
    assert not response_part.startswith("<<")


@pytest.mark.asyncio
async def test_monitor_fire_and_forget_request_has_no_arrow():
    """A fire-and-forget bus frame (no response) is published as a single MQTT
    line containing only the request, with no ` - ` separator and no response
    part appended."""
    bus, dispatcher = _monitor_bus(monitor=True, syslog=False)
    bus.driver.bus_traffic.notify_bus_frame(ForwardFrame(16, 0xFF93), 7)
    await asyncio.sleep(0)

    dispatcher.client.publish.assert_called_once()
    payload = dispatcher.client.publish.call_args.args[1]
    _, _, body = payload.partition(" ")
    assert " - " not in body
    assert body.startswith("<<")


@pytest.mark.asyncio
async def test_monitor_not_logged_when_flag_off():
    """The syslog flag is an extra sink layered on the MQTT monitor: with the
    monitor on but the flag off, the frame is still published to MQTT but nothing
    is written to the controller logger."""
    bus, dispatcher = _monitor_bus(monitor=True, syslog=False)
    with _LogCapture(bus.uid) as log:
        bus.driver.bus_traffic.notify_bus_frame(ForwardFrame(16, 0xFF93), 7)
        await asyncio.sleep(0)

    assert not log.messages
    dispatcher.client.publish.assert_called_once()


@pytest.mark.asyncio
async def test_monitor_not_logged_when_monitor_disabled():
    """The log sink is gated on the MQTT monitor: with the monitor off, a frame
    produces neither an MQTT publish nor a log record, even with the flag on."""
    bus, dispatcher = _monitor_bus(monitor=False, syslog=True)
    with _LogCapture(bus.uid) as log:
        bus.driver.bus_traffic.notify_bus_frame(ForwardFrame(16, 0xFF93), 7)
        await asyncio.sleep(0)

    assert not log.messages
    dispatcher.client.publish.assert_not_called()


@pytest.mark.asyncio
async def test_monitor_problems_logged_at_warning_normal_at_info():
    """The log mirror carries a level so problems can be filtered out of the
    firehose: a transmission error, a framing-error response, and a framing error
    on a received frame are logged at WARNING, while a normal value response stays
    at INFO. Sequence ids are contiguous so the items dispatch in order."""
    bus, _ = _monitor_bus(monitor=True, syslog=True)
    command = EnableInstance(DeviceShort(3), InstanceNumber(2))
    with _LogCapture(bus.uid) as log:
        bus.driver.bus_traffic.notify_command(
            command.frame, WbGatewayTransmissionError(), BusTrafficSource.WB, 0
        )
        bus.driver.bus_traffic.notify_command(
            command.frame, Response(BackwardFrameError(8)), BusTrafficSource.WB, 1
        )
        bus.driver.bus_traffic.notify_command(
            command.frame, Response(BackwardFrame(42)), BusTrafficSource.WB, 2
        )
        bus.driver.bus_traffic.notify_bus_frame(BackwardFrameError(8), 45)
        await asyncio.sleep(0)

    assert log.levels == [logging.WARNING, logging.WARNING, logging.INFO, logging.WARNING]


# --- monitor line formatting -------------------------------------------------


def test_format_command_decoded_drops_frame_type_token():
    """A decoded device command renders as `{hex} {command}` with no `FF{len}`
    descriptor token: the command name already carries the type (`FF24.…`). The
    `FF24` substring must appear only as the command-name prefix, never as a
    standalone frame descriptor between the hex and the command."""
    command = EnableInstance(DeviceShort(3), InstanceNumber(2))
    frame = command.frame
    rendered = format_command(frame, from_frame(frame))

    assert rendered == f"{format_frame_hex(frame)} FF24.EnableInstance(A3, I2)"
    # The raw hex is still present, and FF24 occurs exactly once — as the prefix.
    assert format_frame_hex(frame).strip() in rendered
    assert rendered.count("FF24") == 1
    assert " FF24 " not in rendered


def test_format_command_undecoded_forward_keeps_frame_type_token():
    """An undecoded forward frame has no command name, so the `FF{len}` token is
    the only type indicator and must be retained."""
    command = EnableInstance(DeviceShort(3), InstanceNumber(2))
    frame = command.frame
    assert format_command(frame, None) == f"{format_frame_hex(frame)} FF24"

    gear_frame = ForwardFrame(16, 0xFF93)
    assert format_command(gear_frame, None) == f"{format_frame_hex(gear_frame)} FF16"


def test_format_command_error_frame_keeps_frame_type_token():
    """A frame received with a framing error keeps the `FF{len} framing error`
    suffix: there is no decoded command name, so the frame descriptor still
    carries the only type signal, and `frame.error` is rendered as `framing
    error` (the meaning of the python-dali frame error flag)."""
    frame = ForwardFrame(16, 0xFF93)
    frame._error = True  # pylint: disable=protected-access
    assert format_command(frame, None) == f"{format_frame_hex(frame)} FF16 framing error"


def test_format_response_backward_renders_hex_and_value_without_bf_token():
    """The response half of `request - response` renders as `{hex} {value}` with
    no `BF{len}` descriptor: this is always our request's reply, so the position
    after ` - ` already marks it as a backward frame and the token is just noise
    (backward frames are invariably 8 bits anyway). Hex and decimal value stay."""
    response = Response(BackwardFrame(42))
    rendered = format_response(response)
    assert "BF" not in rendered
    assert rendered == f"{format_frame_hex(response.raw_value)} 42"


def test_format_command_undecoded_backward_keeps_frame_type_token():
    """An unexpected backward frame observed on the bus (not a reply to one of our
    requests — no decoded command, no leading ` - `) keeps its `BF{len}` token:
    here it is the only signal that the standalone packet is a backward frame
    rather than a forward one. This is the mirror of the response case above."""
    frame = BackwardFrame(42)
    rendered = format_command(frame, None)
    assert rendered.startswith(format_frame_hex(frame))
    assert "BF8" in rendered
