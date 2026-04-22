import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from dali.address import GearBroadcast

from wb.mqtt_dali.application_controller import (
    AggregatedCapabilities,
    AggregatedVirtualDevice,
    ApplicationController,
    PollingState,
    publish_device,
    try_initialize_device,
)

# pylint: disable=protected-access,too-many-public-methods
from wb.mqtt_dali.dali_device import DaliDevice
from wb.mqtt_dali.device_init_scheduler import (
    INIT_RETRY_INITIAL_DELAY,
    INIT_RETRY_MAX_DELAY,
    INIT_RETRY_MULTIPLIER,
    DeviceInitScheduler,
)


class TestDeviceInitScheduler:
    def test_schedule_adds_device(self):
        """Scheduled device appears in pending list and is ready for first attempt."""
        scheduler = DeviceInitScheduler()
        scheduler.schedule("dev_1", current_time=100.0)

        assert scheduler.has_pending
        assert scheduler.get_first_attempt_ready(100.0) == ["dev_1"]

    def test_schedule_does_not_overwrite(self):
        """Re-scheduling an existing device preserves its retry state."""
        scheduler = DeviceInitScheduler()
        scheduler.schedule("dev_1", current_time=100.0)
        scheduler.record_failure("dev_1", current_time=100.0)

        scheduler.schedule("dev_1", current_time=100.0)

        assert scheduler.get_retry_count("dev_1") == 1

    def test_schedule_with_delay(self):
        """Device scheduled with delay is not ready until the delay elapses."""
        scheduler = DeviceInitScheduler()
        scheduler.schedule("dev_1", current_time=100.0, delay=10.0)

        assert scheduler.get_first_attempt_ready(105.0) == []
        assert scheduler.get_first_attempt_ready(110.0) == ["dev_1"]

    def test_remove(self):
        """Removed device is no longer pending."""
        scheduler = DeviceInitScheduler()
        scheduler.schedule("dev_1", current_time=100.0)

        scheduler.remove("dev_1")

        assert not scheduler.has_pending

    def test_remove_nonexistent(self):
        """Removing a non-existent device does not raise."""
        scheduler = DeviceInitScheduler()
        scheduler.remove("nonexistent")

    def test_get_first_attempt_ready_filters_retries(self):
        """Only devices with retry_count == 0 are returned as first attempts."""
        scheduler = DeviceInitScheduler()
        scheduler.schedule("dev_1", current_time=100.0)
        scheduler.schedule("dev_2", current_time=100.0)
        scheduler.record_failure("dev_1", current_time=100.0)

        ready = scheduler.get_first_attempt_ready(200.0)

        assert ready == ["dev_2"]

    def test_get_first_attempt_ready_respects_time(self):
        """First attempt devices with future next_retry_time are not returned."""
        scheduler = DeviceInitScheduler()
        scheduler.schedule("dev_1", current_time=100.0, delay=50.0)

        assert scheduler.get_first_attempt_ready(120.0) == []
        assert scheduler.get_first_attempt_ready(150.0) == ["dev_1"]

    def test_get_one_retry_ready_skips_first_attempt(self):
        """Devices that never failed are not returned as retry candidates."""
        scheduler = DeviceInitScheduler()
        scheduler.schedule("dev_1", current_time=100.0)

        assert scheduler.get_one_retry_ready(200.0) is None

    def test_get_one_retry_ready_returns_one(self):
        """Only one retry device is returned even when multiple are ready."""
        scheduler = DeviceInitScheduler()
        scheduler.schedule("dev_1", current_time=100.0)
        scheduler.schedule("dev_2", current_time=100.0)
        scheduler.record_failure("dev_1", current_time=100.0)
        scheduler.record_failure("dev_2", current_time=100.0)

        result = scheduler.get_one_retry_ready(200.0)

        assert result in ("dev_1", "dev_2")

    def test_get_one_retry_ready_respects_time(self):
        """Retry device is not returned before its backoff delay elapses."""
        scheduler = DeviceInitScheduler()
        scheduler.schedule("dev_1", current_time=100.0)
        scheduler.record_failure("dev_1", current_time=100.0)

        assert scheduler.get_one_retry_ready(101.0) is None
        assert scheduler.get_one_retry_ready(200.0) == "dev_1"

    def test_record_success_removes(self):
        """Successful device is removed from pending list."""
        scheduler = DeviceInitScheduler()
        scheduler.schedule("dev_1", current_time=100.0)

        scheduler.record_success("dev_1")

        assert not scheduler.has_pending

    def test_record_failure_increments_count(self):
        """Each failure increments the retry counter."""
        scheduler = DeviceInitScheduler()
        scheduler.schedule("dev_1", current_time=100.0)

        scheduler.record_failure("dev_1", current_time=100.0)

        assert scheduler.get_retry_count("dev_1") == 1

    def test_record_failure_returns_delay(self):
        """First failure returns the initial delay value."""
        scheduler = DeviceInitScheduler()
        scheduler.schedule("dev_1", current_time=100.0)

        delay = scheduler.record_failure("dev_1", current_time=100.0)

        assert delay == INIT_RETRY_INITIAL_DELAY

    def test_record_failure_nonexistent(self):
        """Recording failure for unknown device returns 0 and does not raise."""
        scheduler = DeviceInitScheduler()
        delay = scheduler.record_failure("nonexistent", current_time=100.0)
        assert delay == 0.0

    def test_exponential_backoff(self):
        """Each consecutive failure doubles the delay: 5 -> 10 -> 20 -> 40."""
        scheduler = DeviceInitScheduler()
        scheduler.schedule("dev_1", current_time=0.0)

        expected_delays = [
            INIT_RETRY_INITIAL_DELAY,
            INIT_RETRY_INITIAL_DELAY * INIT_RETRY_MULTIPLIER,
            INIT_RETRY_INITIAL_DELAY * INIT_RETRY_MULTIPLIER**2,
            INIT_RETRY_INITIAL_DELAY * INIT_RETRY_MULTIPLIER**3,
        ]

        for expected in expected_delays:
            delay = scheduler.record_failure("dev_1", current_time=0.0)
            assert delay == expected

    def test_backoff_caps_at_max(self):
        """Delay never exceeds INIT_RETRY_MAX_DELAY regardless of failure count."""
        scheduler = DeviceInitScheduler()
        scheduler.schedule("dev_1", current_time=0.0)

        for _ in range(20):
            delay = scheduler.record_failure("dev_1", current_time=0.0)

        assert delay == INIT_RETRY_MAX_DELAY

    def test_clear(self):
        """clear() removes all pending devices."""
        scheduler = DeviceInitScheduler()
        scheduler.schedule("dev_1", current_time=100.0)
        scheduler.schedule("dev_2", current_time=100.0)

        scheduler.clear()

        assert not scheduler.has_pending

    def test_get_retry_count_nonexistent(self):
        """Retry count for unknown device is 0."""
        scheduler = DeviceInitScheduler()
        assert scheduler.get_retry_count("nonexistent") == 0

    def test_custom_parameters(self):
        """Scheduler respects custom initial_delay, multiplier, max_delay."""
        scheduler = DeviceInitScheduler(initial_delay=1.0, multiplier=3.0, max_delay=10.0)
        scheduler.schedule("dev_1", current_time=0.0)

        d1 = scheduler.record_failure("dev_1", current_time=0.0)
        d2 = scheduler.record_failure("dev_1", current_time=0.0)
        d3 = scheduler.record_failure("dev_1", current_time=0.0)

        assert d1 == 1.0
        assert d2 == 3.0
        assert d3 == 9.0

    def test_custom_parameters_cap(self):
        """Custom max_delay caps the backoff correctly."""
        scheduler = DeviceInitScheduler(initial_delay=1.0, multiplier=3.0, max_delay=10.0)
        scheduler.schedule("dev_1", current_time=0.0)

        for _ in range(10):
            delay = scheduler.record_failure("dev_1", current_time=0.0)

        assert delay == 10.0

    def test_batch_first_attempt(self):
        """All 10 devices scheduled at once are returned together for first attempt."""
        scheduler = DeviceInitScheduler()
        for i in range(10):
            scheduler.schedule(f"dev_{i}", current_time=0.0)

        ready = scheduler.get_first_attempt_ready(0.0)
        assert len(ready) == 10


# --- DeviceInitializer tests (public API, injected dependencies) ---


def _make_publisher():
    publisher = MagicMock()
    publisher.add_device = AsyncMock()
    publisher.remove_device = AsyncMock()
    publisher.register_control_handler = AsyncMock()
    publisher.set_control_error = AsyncMock()
    publisher.has_device = MagicMock(return_value=False)
    return publisher


def _make_mock_device(mqtt_id="dev_1", name="DALI 1", is_initialized=False):
    device = MagicMock(spec=DaliDevice)
    device.mqtt_id = mqtt_id
    device.name = name
    device.is_initialized = is_initialized
    device.groups = set()
    device.dt8_colour_type = None
    device.dt8_tc_limits = None
    device.address = SimpleNamespace(short=1)
    device.instances = {}
    device.initialize = AsyncMock()
    device.get_mqtt_controls = MagicMock(return_value=[])
    device.get_common_mqtt_controls = MagicMock(return_value=[])
    device.set_logger = MagicMock()
    return device


def _make_init_deps(publisher=None, scheduler=None):
    publisher = publisher or _make_publisher()
    scheduler = scheduler or DeviceInitScheduler()
    driver = AsyncMock()
    control_handler = MagicMock()
    logger = logging.getLogger("test")
    return driver, publisher, scheduler, control_handler, logger


class TestTryInitializeDevice:
    @pytest.mark.asyncio
    async def test_success_publishes_and_records(self):
        """Successful init publishes device to MQTT and removes it from scheduler."""
        driver, publisher, scheduler, handler, logger = _make_init_deps()
        device = _make_mock_device()
        scheduler.schedule(device.mqtt_id, 0.0)

        result = await try_initialize_device(device, driver, publisher, scheduler, handler, logger, 100.0)

        assert result is True
        device.initialize.assert_awaited_once()
        publisher.add_device.assert_awaited_once()
        assert not scheduler.has_pending

    @pytest.mark.asyncio
    async def test_success_removes_old_if_published(self):
        """If device was previously published with error, it is removed before republishing."""
        publisher = _make_publisher()
        publisher.has_device = MagicMock(return_value=True)
        driver, _, scheduler, handler, logger = _make_init_deps(publisher=publisher)
        device = _make_mock_device()
        scheduler.schedule(device.mqtt_id, 0.0)

        await try_initialize_device(device, driver, publisher, scheduler, handler, logger, 100.0)

        publisher.remove_device.assert_awaited_once_with(device.mqtt_id)

    @pytest.mark.asyncio
    async def test_failure_stays_in_scheduler(self):
        """Failed init keeps device in scheduler with incremented retry count."""
        driver, publisher, scheduler, handler, logger = _make_init_deps()
        device = _make_mock_device()
        device.initialize = AsyncMock(side_effect=RuntimeError("no response"))
        scheduler.schedule(device.mqtt_id, 0.0)

        result = await try_initialize_device(device, driver, publisher, scheduler, handler, logger, 100.0)

        assert result is False
        assert scheduler.has_pending
        assert scheduler.get_retry_count(device.mqtt_id) == 1

    @pytest.mark.asyncio
    async def test_failure_publishes_error_controls(self):
        """First failure publishes device with common controls marked as error 'r'."""
        driver, publisher, scheduler, handler, logger = _make_init_deps()
        device = _make_mock_device()
        device.initialize = AsyncMock(side_effect=RuntimeError("no response"))
        mock_control = MagicMock()
        mock_control.control_info.id = "brightness"
        mock_control.is_readable = MagicMock(return_value=True)
        device.get_common_mqtt_controls = MagicMock(return_value=[mock_control])
        scheduler.schedule(device.mqtt_id, 0.0)

        await try_initialize_device(device, driver, publisher, scheduler, handler, logger, 100.0)

        publisher.set_control_error.assert_awaited_once_with(device.mqtt_id, "brightness", "r")

    @pytest.mark.asyncio
    async def test_failure_does_not_republish_if_already_published(self):
        """Repeated failures do not republish device if it was already published with error."""
        publisher = _make_publisher()
        publisher.has_device = MagicMock(return_value=True)
        driver, _, scheduler, handler, logger = _make_init_deps(publisher=publisher)
        device = _make_mock_device()
        device.initialize = AsyncMock(side_effect=RuntimeError("no response"))
        scheduler.schedule(device.mqtt_id, 0.0)

        await try_initialize_device(device, driver, publisher, scheduler, handler, logger, 100.0)

        publisher.add_device.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_publish_error_skips_non_readable(self):
        """Error publishing does not set error on write-only controls."""
        driver, publisher, scheduler, handler, logger = _make_init_deps()
        device = _make_mock_device()
        device.initialize = AsyncMock(side_effect=RuntimeError("no response"))
        writable_control = MagicMock()
        writable_control.control_info.id = "button"
        writable_control.is_readable = MagicMock(return_value=False)
        device.get_common_mqtt_controls = MagicMock(return_value=[writable_control])
        scheduler.schedule(device.mqtt_id, 0.0)

        await try_initialize_device(device, driver, publisher, scheduler, handler, logger, 100.0)

        publisher.set_control_error.assert_not_awaited()


class TestPublishDevice:  # pylint: disable=too-few-public-methods
    @pytest.mark.asyncio
    async def test_publish_normal(self):
        """publish_device with error=False uses get_mqtt_controls."""
        publisher = _make_publisher()
        handler = MagicMock()
        device = _make_mock_device()
        device.get_mqtt_controls = MagicMock(return_value=["c1", "c2"])

        await publish_device(device, publisher, handler)

        publisher.add_device.assert_awaited_once()
        publisher.register_control_handler.assert_awaited_once()
        publisher.set_control_error.assert_not_awaited()


# --- ApplicationController._poll_step tests ---


def _make_controller():
    ctrl = ApplicationController.__new__(ApplicationController)
    ctrl.logger = logging.getLogger("test")
    ctrl._init_scheduler = DeviceInitScheduler()
    ctrl._device_publisher = _make_publisher()
    ctrl._dev = AsyncMock()
    ctrl._handle_on_topic = MagicMock()
    ctrl.dali_devices = []
    ctrl.dali2_devices = []
    ctrl._dali2_devices_by_addr = {}
    ctrl._dev_inst_map = MagicMock()
    ctrl._group_devices_by_number = {}
    ctrl._devices_by_mqtt_id = {}
    ctrl._broadcast_device = AggregatedVirtualDevice(
        mqtt_id="test_broadcast",
        name="Test Broadcast",
        capabilities=AggregatedCapabilities(),
        address=GearBroadcast(),
    )
    return ctrl


class TestPollStep:
    @pytest.mark.asyncio
    async def test_alternates_poll_and_retry(self):
        """With one pollable and one retry device, steps alternate: poll, retry, poll."""
        ctrl = _make_controller()
        poll_dev = _make_mock_device(mqtt_id="p1", is_initialized=True)
        retry_dev = _make_mock_device(mqtt_id="r1", is_initialized=False)
        retry_dev.initialize = AsyncMock(side_effect=RuntimeError("fail"))
        ctrl.dali_devices = [poll_dev]
        ctrl._devices_by_mqtt_id = {"p1": poll_dev, "r1": retry_dev}
        ctrl._init_scheduler.schedule("r1", 0.0)
        ctrl._init_scheduler.record_failure("r1", 0.0)
        ctrl._poll_device = AsyncMock()
        ctrl._polling_interval = 0.0
        state = PollingState(last_poll_time=0.0)

        t = 100.0
        await ctrl._poll_step(state, t)
        assert state.poll_turn is False
        ctrl._poll_device.assert_awaited_once()

        await ctrl._poll_step(state, t)
        assert state.poll_turn is True
        assert ctrl._init_scheduler.get_retry_count("r1") == 2

        ctrl._poll_device.reset_mock()
        await ctrl._poll_step(state, t)
        assert state.poll_turn is False
        ctrl._poll_device.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_poll_only_when_no_retries(self):
        """Without pending retries, poll runs then idle (poll_turn resets)."""
        ctrl = _make_controller()
        dev = _make_mock_device(mqtt_id="d1", is_initialized=True)
        ctrl.dali_devices = [dev]
        ctrl._devices_by_mqtt_id = {"d1": dev}
        ctrl._poll_device = AsyncMock()
        ctrl._polling_interval = 0.0
        state = PollingState(last_poll_time=0.0)

        timeout = await ctrl._poll_step(state, 100.0)
        assert state.poll_turn is False
        ctrl._poll_device.assert_awaited_once()

        timeout = await ctrl._poll_step(state, 100.0)
        assert state.poll_turn is True
        assert timeout == 1.0

    @pytest.mark.asyncio
    async def test_retry_only_when_no_poll_devices(self):
        """Without initialized devices, only retry-init runs, polling is skipped."""
        ctrl = _make_controller()
        retry_dev = _make_mock_device(mqtt_id="r1", is_initialized=False)
        retry_dev.initialize = AsyncMock(side_effect=RuntimeError("fail"))
        ctrl.dali_devices = []
        ctrl._devices_by_mqtt_id = {"r1": retry_dev}
        ctrl._init_scheduler.schedule("r1", 0.0)
        ctrl._init_scheduler.record_failure("r1", 0.0)
        ctrl._poll_device = AsyncMock()
        ctrl._polling_interval = 0.0
        state = PollingState(last_poll_time=0.0)

        timeout = await ctrl._poll_step(state, 100.0)
        assert timeout == 0.001
        assert state.poll_turn is True
        ctrl._poll_device.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_first_attempt_has_priority(self):
        """First-attempt batch runs before polling or retry."""
        ctrl = _make_controller()
        dev = _make_mock_device(mqtt_id="new1", is_initialized=False)
        ctrl._devices_by_mqtt_id = {"new1": dev}
        ctrl._init_scheduler.schedule("new1", 0.0)
        ctrl._poll_device = AsyncMock()
        ctrl._polling_interval = 0.0
        state = PollingState(last_poll_time=0.0)

        timeout = await ctrl._poll_step(state, 100.0)
        assert timeout == 0.001
        dev.initialize.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_retry_not_due_yields_idle(self):
        """When retry backoff has not elapsed, step returns idle timeout."""
        ctrl = _make_controller()
        retry_dev = _make_mock_device(mqtt_id="r1", is_initialized=False)
        ctrl.dali_devices = []
        ctrl._devices_by_mqtt_id = {"r1": retry_dev}
        ctrl._init_scheduler.schedule("r1", 0.0)
        ctrl._init_scheduler.record_failure("r1", 0.0)
        ctrl._poll_device = AsyncMock()
        ctrl._polling_interval = 0.0
        state = PollingState(last_poll_time=0.0)

        timeout = await ctrl._poll_step(state, 2.0)
        assert timeout == 1.0
