import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from wb.mqtt_dali.device_publisher import (
    ControlHandler,
    ControlInfo,
    DeviceChange,
    DeviceInfo,
    DevicePublisher,
)
from wb.mqtt_dali.wbmqtt import ControlMeta


class MockMessage:  # pylint: disable=too-few-public-methods
    def __init__(self, topic: str, payload: bytes = b""):
        self.topic = topic
        self.payload = payload


class MockMQTTClient:  # pylint: disable=too-few-public-methods
    def __init__(self):
        self.publish = AsyncMock()
        self.subscribe = AsyncMock()
        self.unsubscribe = AsyncMock()


class MockMQTTDispatcher:
    def __init__(self, client):
        self.client = client
        self.subscribe = AsyncMock()
        self.unsubscribe = AsyncMock()
        self._subscriptions = {}

    async def mock_subscribe(self, topic, callback):
        if topic not in self._subscriptions:
            self._subscriptions[topic] = []
        self._subscriptions[topic].append(callback)

    async def mock_unsubscribe(self, topic, callback=None):
        if topic in self._subscriptions:
            if callback is None:
                del self._subscriptions[topic]
            else:
                if callback in self._subscriptions[topic]:
                    self._subscriptions[topic].remove(callback)
                if not self._subscriptions[topic]:
                    del self._subscriptions[topic]


@pytest.fixture
def mock_client():
    return MockMQTTClient()


@pytest.fixture
def mock_dispatcher(mock_client):
    dispatcher = MockMQTTDispatcher(mock_client)
    dispatcher.subscribe.side_effect = dispatcher.mock_subscribe
    dispatcher.unsubscribe.side_effect = dispatcher.mock_unsubscribe
    return dispatcher


@pytest.fixture
def publisher(mock_dispatcher):
    logger_mock = MagicMock()
    logger_mock.getChild.return_value = MagicMock()
    return DevicePublisher(mock_dispatcher, logger_mock)


class TestDeviceChange:
    def test_default_initialization(self):
        change = DeviceChange()
        assert change.added == []
        assert change.removed == []

    def test_with_parameters(self):
        added = [DeviceInfo("dev1", "Device 1")]
        removed = ["dev2"]

        change = DeviceChange(added=added, removed=removed)

        assert change.added == added
        assert change.removed == removed


class TestControlHandler:  # pylint: disable=too-few-public-methods
    def test_initialization(self):
        def callback(_msg):
            pass

        handler = ControlHandler("device1", "control1", callback)

        assert handler.device_id == "device1"
        assert handler.control_id == "control1"
        assert handler.callback == callback


class TestDevicePublisher:
    @pytest.mark.asyncio
    async def test_initialization(self, publisher):
        assert len(publisher._devices) == 0
        assert len(publisher._control_handlers) == 0
        assert publisher._initialized is False

    @pytest.mark.asyncio
    async def test_initialize(self, publisher: DevicePublisher, mock_client):
        device_info = DeviceInfo("dev1", "Device 1")
        await publisher.add_device(device_info)
        await publisher.initialize()
        assert publisher._initialized is True
        assert mock_client.publish.call_count > 0

    @pytest.mark.asyncio
    async def test_add_device(self, publisher):
        device_info = DeviceInfo(
            "dev1",
            "Device 1",
            [
                ControlInfo("ctrl1", ControlMeta("switch", "Control 1"), "0"),
            ],
        )

        await publisher.add_device(device_info)

        assert "dev1" in publisher._devices
        assert publisher.has_device("dev1")

    @pytest.mark.asyncio
    async def test_add_device_with_no_title(self, publisher):
        device_info = DeviceInfo("dev1")
        await publisher.add_device(device_info)

        assert "dev1" in publisher._devices
        device = publisher._devices["dev1"]
        assert device._device_title is None

    @pytest.mark.asyncio
    async def test_add_device_duplicate(self, publisher):
        device_info = DeviceInfo("dev1", "Device 1")
        await publisher.add_device(device_info)
        with pytest.raises(RuntimeError) as e:
            await publisher.add_device(device_info)
        assert str(e.value) == "Device dev1 already exists"

    @pytest.mark.asyncio
    async def test_remove_device(self, publisher):
        device_info = DeviceInfo("dev1", "Device 1")

        await publisher.add_device(device_info)
        assert "dev1" in publisher._devices

        await publisher.remove_device("dev1")
        assert "dev1" not in publisher._devices

    @pytest.mark.asyncio
    async def test_remove_nonexistent_device(self, publisher):
        await publisher.remove_device("nonexistent")
        publisher.logger.warning.assert_called_with("Device %s not found for removal", "nonexistent")

    @pytest.mark.asyncio
    async def test_rebuild_with_changes(self, publisher):
        initial_devices = [
            DeviceInfo("dev1", "Device 1"),
            DeviceInfo("dev2", "Device 2"),
        ]

        for device_info in initial_devices:
            await publisher.add_device(device_info)

        changes = DeviceChange(
            added=[DeviceInfo("dev3", "Device 3")],
            removed=["dev1"],
        )

        await publisher.rebuild(changes)

        assert "dev1" not in publisher._devices
        assert "dev2" in publisher._devices
        assert "dev3" in publisher._devices

    @pytest.mark.asyncio
    async def test_set_control_value(self, publisher, mock_client):
        device_info = DeviceInfo(
            "dev1",
            "Device 1",
            [
                ControlInfo("ctrl1", ControlMeta("switch", "Control 1"), "0"),
            ],
        )

        await publisher.add_device(device_info)
        await publisher.initialize()
        mock_client.publish.reset_mock()

        await publisher.set_control_value("dev1", "ctrl1", "1")

        device = publisher._devices["dev1"]
        assert device._controls["ctrl1"].value == "1"
        mock_client.publish.assert_called()

    @pytest.mark.asyncio
    async def test_set_control_value_nonexistent_device(self, publisher):
        await publisher.set_control_value("nonexistent", "ctrl1", "1")
        publisher.logger.warning.assert_called_with("Device %s not found", "nonexistent")

    @pytest.mark.asyncio
    async def test_set_control_title(self, publisher, mock_client):
        device_info = DeviceInfo(
            "dev1",
            "Device 1",
            [
                ControlInfo("ctrl1", ControlMeta("switch", "Control 1"), "0"),
            ],
        )

        await publisher.add_device(device_info)
        await publisher.initialize()
        mock_client.publish.reset_mock()

        await publisher.set_control_title("dev1", "ctrl1", "New Title")

        device = publisher._devices["dev1"]
        assert device._controls["ctrl1"].meta.title.en == "New Title"
        mock_client.publish.assert_called()

    @pytest.mark.asyncio
    async def test_set_control_title_nonexistent_device(self, publisher):
        await publisher.set_control_title("nonexistent", "ctrl1", "New Title")
        publisher.logger.warning.assert_called_with("Device %s not found", "nonexistent")

    @pytest.mark.asyncio
    async def test_register_control_handler(self, publisher, mock_dispatcher):
        device_info = DeviceInfo(
            "dev1",
            "Device 1",
            [
                ControlInfo("ctrl1", ControlMeta("switch", "Control 1"), "0"),
            ],
        )

        await publisher.add_device(device_info)

        callback = AsyncMock()
        await publisher.register_control_handler("dev1", "ctrl1", callback)

        assert "dev1/ctrl1" in publisher._control_handlers
        mock_dispatcher.subscribe.assert_called()

    @pytest.mark.asyncio
    async def test_register_duplicate_handler(self, publisher):
        device_info = DeviceInfo(
            "dev1",
            "Device 1",
            [
                ControlInfo("ctrl1", ControlMeta("switch", "Control 1"), "0"),
            ],
        )

        await publisher.add_device(device_info)

        callback = AsyncMock()
        await publisher.register_control_handler("dev1", "ctrl1", callback)
        with pytest.raises(RuntimeError) as e:
            await publisher.register_control_handler("dev1", "ctrl1", callback)
        assert str(e.value) == "Handler already registered for dev1/ctrl1"

    @pytest.mark.asyncio
    async def test_unregister_control_handler(self, publisher):
        device_info = DeviceInfo(
            "dev1",
            "Device 1",
            [
                ControlInfo("ctrl1", ControlMeta("switch", "Control 1"), "0"),
            ],
        )

        await publisher.add_device(device_info)

        callback = AsyncMock()
        await publisher.register_control_handler("dev1", "ctrl1", callback)
        assert "dev1/ctrl1" in publisher._control_handlers

        await publisher.unregister_control_handler("dev1", "ctrl1")
        assert "dev1/ctrl1" not in publisher._control_handlers

    @pytest.mark.asyncio
    async def test_handle_on_message(self, publisher):
        device_info = DeviceInfo(
            "dev1",
            "Device 1",
            [
                ControlInfo("ctrl1", ControlMeta("switch", "Control 1"), "0"),
            ],
        )

        await publisher.add_device(device_info)

        callback = AsyncMock()
        await publisher.register_control_handler("dev1", "ctrl1", callback)

        message = MockMessage("/devices/test_bus_dev1/controls/ctrl1/on", b"1")
        await publisher._handle_on_message("dev1/ctrl1", message)

        callback.assert_called_once_with(message)

    @pytest.mark.asyncio
    async def test_handle_on_message_with_error(self, publisher):
        device_info = DeviceInfo(
            "dev1",
            "Device 1",
            [
                ControlInfo("ctrl1", ControlMeta("switch", "Control 1"), "0"),
            ],
        )

        await publisher.add_device(device_info)

        callback = AsyncMock(side_effect=ValueError("Test error"))
        await publisher.register_control_handler("dev1", "ctrl1", callback)

        message = MockMessage("/devices/test_bus_dev1/controls/ctrl1/on", b"1")
        await publisher._handle_on_message("dev1/ctrl1", message)
        if len(publisher._on_topic_running_handlers._tasks) > 0:
            await asyncio.gather(*publisher._on_topic_running_handlers._tasks, return_exceptions=True)
        assert len(publisher._on_topic_running_handlers._tasks) == 0

        publisher.logger.error.assert_called_once()
        assert publisher.logger.error.call_args[0][0] == "%s raised an exception: %s"

    @pytest.mark.asyncio
    async def test_get_device_ids(self, publisher):
        device_info1 = DeviceInfo(
            "dev1",
            "Device 1",
            [
                ControlInfo("ctrl1", ControlMeta("switch", "Control 1"), "0"),
            ],
        )
        device_info2 = DeviceInfo("dev2", "Device 2")

        await publisher.add_device(device_info1)
        await publisher.add_device(device_info2)

        device_ids = publisher.get_device_ids()
        assert device_ids == {"dev1", "dev2"}

    @pytest.mark.asyncio
    async def test_has_device(self, publisher):
        device_info = DeviceInfo(
            "dev1",
            "Device 1",
            [
                ControlInfo("ctrl1", ControlMeta("switch", "Control 1"), "0"),
            ],
        )

        await publisher.add_device(device_info)

        assert publisher.has_device("dev1") is True
        assert publisher.has_device("dev2") is False

    @pytest.mark.asyncio
    async def test_cleanup(self, publisher):
        device_info = DeviceInfo(
            "dev1",
            "Device 1",
            [
                ControlInfo("ctrl1", ControlMeta("switch", "Control 1"), "0"),
            ],
        )
        await publisher.add_device(device_info)
        await publisher.initialize()

        callback = AsyncMock()
        await publisher.register_control_handler("dev1", "ctrl1", callback)
        await publisher.cleanup()

        assert len(publisher._devices) == 0
        assert len(publisher._control_handlers) == 0
        assert publisher._initialized is False

    @pytest.mark.asyncio
    async def test_get_control_on_topic(self, publisher):
        topic = publisher._get_control_on_topic("test_bus_dev1", "ctrl1")
        assert topic == "/devices/test_bus_dev1/controls/ctrl1/on"

    @pytest.mark.asyncio
    async def test_concurrent_operations(self, publisher):
        device_infos = [
            DeviceInfo(
                f"dev{i}",
                f"Device {i}",
                [
                    ControlInfo("ctrl1", ControlMeta("switch", "Control 1"), "0"),
                ],
            )
            for i in range(10)
        ]

        await asyncio.gather(*[publisher.add_device(info) for info in device_infos])

        assert len(publisher._devices) == 10

        await asyncio.gather(*[publisher.remove_device(f"dev{i}") for i in range(10)])

        assert len(publisher._devices) == 0

    @pytest.mark.asyncio
    async def test_remove_device_removes_handlers(self, publisher):
        device_info = DeviceInfo(
            "dev1",
            "Device 1",
            [
                ControlInfo("ctrl1", ControlMeta("switch", "Control 1"), "0"),
                ControlInfo("ctrl2", ControlMeta("text", "Control 2"), "test"),
            ],
        )

        await publisher.add_device(device_info)

        callback1 = AsyncMock()
        callback2 = AsyncMock()
        await publisher.register_control_handler("dev1", "ctrl1", callback1)
        await publisher.register_control_handler("dev1", "ctrl2", callback2)

        assert "dev1/ctrl1" in publisher._control_handlers
        assert "dev1/ctrl2" in publisher._control_handlers

        await publisher.remove_device("dev1")

        assert "dev1/ctrl1" not in publisher._control_handlers
        assert "dev1/ctrl2" not in publisher._control_handlers

    @pytest.mark.asyncio
    async def test_add_control_with_all_fields(self, publisher):
        device_info = DeviceInfo(
            "dev1",
            "Device 1",
            [ControlInfo("ctrl1", ControlMeta("temperature", "Full Control", True, 1), "23.5")],
        )

        await publisher.add_device(device_info)
        await publisher.initialize()

        device = publisher._devices["dev1"]
        control = device._controls["ctrl1"]

        assert control.meta.title.en == "Full Control"
        assert control.meta.control_type == "temperature"
        assert control.meta.order == 1
        assert control.meta.read_only is True
        assert control.value == "23.5"
