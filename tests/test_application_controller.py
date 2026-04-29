import asyncio
import logging
import re
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wb.mqtt_dali.application_controller import (
    ApplicationController,
    ApplicationControllerState,
    ApplicationControllerTaskType,
    CommissioningDeviceSummary,
    CommissioningStartResult,
    CommissioningState,
    CommissioningStatus,
)
from wb.mqtt_dali.commissioning import (
    ChangedDevice,
    CommissioningResult,
    CommissioningStage,
)
from wb.mqtt_dali.common_dali_device import DaliDeviceAddress, DaliDeviceBase
from wb.mqtt_dali.dali_compat import DaliCommandsCompatibilityLayer

# pylint: disable=protected-access

# Prevent file system access inside DaliDeviceBase.__init__
DaliDeviceBase._common_schema = {"title": "test-schema"}


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
    controller._dali2_devices_by_addr = {}
    return controller


@pytest.mark.asyncio
async def test_resolve_initial_names_formats_known_product():
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
    controller = _make_bare_controller()
    compat = DaliCommandsCompatibilityLayer()

    with patch("wb.mqtt_dali.application_controller.read_product_name") as mock_rpn:
        names = await controller._resolve_initial_names([], compat)
        mock_rpn.assert_not_called()

    assert names == []


@pytest.mark.asyncio
async def test_update_dali_devices_sets_custom_name_for_new_with_known_gtin():
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
    controller = _make_bare_controller()

    # Simulate a previously-known DALI2 device at short 6 that is now a replacement
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
        state.mark_failed("DALI2 phase failed", [])
        assert state.status == CommissioningStatus.FAILED
        assert state.error == "DALI2 phase failed"
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
        controller = _make_commissioning_controller()

        def _bad_cb(_state):
            raise RuntimeError("cb exploded")

        controller._commissioning_state_cb = _bad_cb
        with caplog.at_level(logging.ERROR, logger="test"):
            # Must not raise.
            controller._publish_commissioning_state()
        assert any("cb exploded" in rec.message for rec in caplog.records)

    def test_no_callback_is_noop(self):
        controller = _make_commissioning_controller()
        controller._commissioning_state_cb = None
        # No exception, no scheduling.
        controller._publish_commissioning_state()
        cast(MagicMock, controller._one_shot_tasks.add).assert_not_called()


class TestStartCommissioning:
    @pytest.mark.asyncio
    async def test_first_call_returns_started_and_marks_running(self):
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
    async def test_failure_after_dali1_applies_partial_and_marks_failed(self):
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
                # DALI2 phase raises before emitting anything.
                raise RuntimeError("DALI2 phase failed")

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

        assert controller._commissioning_state.status == CommissioningStatus.FAILED
        assert controller._commissioning_state.error == "DALI2 phase failed"
        assert controller._commissioning_state.finished_at is not None
        # Only DALI1-phase devices remain on FAILED.
        assert controller._commissioning_state.devices == [dali1_device]
        # Partial result (DALI1) was applied.
        controller._apply_commissioning_results.assert_awaited_once()
        applied_args = controller._apply_commissioning_results.await_args
        assert applied_args is not None
        assert applied_args.args[0] is not None  # res_dali
        assert applied_args.args[1] is None  # res_dali2

    @pytest.mark.asyncio
    async def test_cancel_sets_cancelled_and_reraises(self):
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
        """Fill in the fields stop() touches so we can call it directly."""
        controller._state = ApplicationControllerState.READY
        controller._bus_traffic_cleanup = MagicMock()
        one_shot = MagicMock()
        one_shot.stop = AsyncMock()
        controller._one_shot_tasks = one_shot
        controller._quiescent_mode_timer = None
        controller._controls_to_execute = {}
        controller._init_scheduler = MagicMock()
        controller._websocket_lock = asyncio.Lock()
        controller._stop_websocket = AsyncMock()
        controller._device_publisher = AsyncMock()
        controller._dev = AsyncMock()

    @pytest.mark.asyncio
    async def test_cancels_child_before_polling_task(self):
        controller = _make_commissioning_controller()
        self._prepare_for_stop(controller)

        statuses: list[CommissioningStatus] = []

        def _cb(state):
            statuses.append(state.status)

        controller._commissioning_state_cb = _cb
        controller._commissioning_state.status = CommissioningStatus.QUERY_SHORT_ADDRESSES

        child_entered = asyncio.Event()

        async def _fake_commissioning_task():
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
