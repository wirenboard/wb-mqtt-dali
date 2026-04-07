import asyncio
import json
import logging
from unittest.mock import AsyncMock, patch

import pytest

from wb.mqtt_dali.wbmqtt import (
    ControlMeta,
    ControlState,
    Device,
    TranslatedTitle,
    remove_topics_by_driver,
    retain_hack,
)

# pylint: disable=protected-access,redefined-outer-name,too-many-public-methods


class MockMessage:  # pylint: disable=too-few-public-methods
    def __init__(self, topic: str, payload: bytes = b""):
        self.topic = topic
        self.payload = payload


class MockMessageIterator:
    def __init__(self, client):
        self.client = client
        self.local_index = 0

    async def __aenter__(self):
        self.local_index = 0
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.local_index < len(self.client._messages):
            message = self.client._messages[self.local_index]
            self.local_index += 1
            await asyncio.sleep(0.01)
            return message
        raise StopAsyncIteration


class MockMQTTClient:
    def __init__(self):
        self.publish = AsyncMock()
        self.subscribe = AsyncMock()
        self.unsubscribe = AsyncMock()
        self._messages = []
        self._message_index = 0

    def add_message(self, topic: str, payload: bytes = b""):
        self._messages.append(MockMessage(topic, payload))

    def unfiltered_messages(self):
        return MockMessageIterator(self)


@pytest.fixture
def mock_client():
    return MockMQTTClient()


class MockMQTTDispatcher:
    def __init__(self, client):
        self.client = client
        self._subscriptions = {}

    async def subscribe(self, topic, callback):
        if topic not in self._subscriptions:
            self._subscriptions[topic] = []
        self._subscriptions[topic].append(callback)
        await self.client.subscribe(topic)

        if "#" in topic:
            prefix = topic.replace("/#", "")
            for message in self.client._messages:
                if str(message.topic).startswith(prefix):
                    await callback(message)
        else:
            for message in self.client._messages:
                if str(message.topic) == topic:
                    await callback(message)

    async def unsubscribe(self, topic):
        if topic in self._subscriptions:
            del self._subscriptions[topic]
        await self.client.unsubscribe(topic)


@pytest.fixture
def mock_dispatcher(mock_client):
    return MockMQTTDispatcher(mock_client)


class TestControlMeta:
    def test_default_initialization(self):
        meta = ControlMeta()
        assert meta.title is None
        assert meta.control_type == "value"
        assert meta.order is None
        assert meta.read_only is False

    def test_full_initialization(self):
        meta = ControlMeta(title="Test Control", control_type="switch", order=1, read_only=True)
        assert meta.title.en == "Test Control"
        assert meta.control_type == "switch"
        assert meta.order == 1
        assert meta.read_only is True

    def test_partial_initialization(self):
        meta = ControlMeta(title="Partial", order=5)
        assert meta.title.en == "Partial"
        assert meta.control_type == "value"
        assert meta.order == 5
        assert meta.read_only is False


class TestControlState:
    def test_initialization(self):
        meta = ControlMeta(
            title="Test",
            control_type="text",
            order=2,
            read_only=True,
            enum={"100": TranslatedTitle("aaa")},
            minimum=200,
            maximum=100,
        )
        state = ControlState(meta, "test_value")

        assert state.value == "test_value"
        assert state.meta.title.en == "Test"
        assert state.meta.control_type == "text"
        assert state.meta.order == 2
        assert state.meta.read_only is True
        assert state.meta.enum == {"100": TranslatedTitle("aaa")}
        assert state.meta.minimum == 200
        assert state.meta.maximum == 100

    def test_meta_is_copied(self):
        meta = ControlMeta(title="Original")
        state = ControlState(meta, "value")

        meta.title.en = "Modified"

        assert state.meta.title.en == "Original"


class TestDevice:
    @pytest.mark.asyncio
    async def test_initialization(self, mock_client):
        device = Device(mock_client, "test_device", "test_driver", "Test Device")
        await device.initialize()

        assert device._base_topic == "/devices/test_device"
        assert device._device_title.en == "Test Device"
        assert device._driver_name == "test_driver"
        assert len(device._controls) == 0

        assert mock_client.publish.call_count >= 1
        meta_calls = [
            c
            for c in mock_client.publish.call_args_list
            if "/devices/test_device/meta" in str(c[0][0]) and "/controls" not in str(c[0][0])
        ]
        assert len(meta_calls) == 1
        meta_json = json.loads(meta_calls[0][0][1])
        assert meta_json["driver"] == "test_driver"
        assert meta_json["title"]["en"] == "Test Device"

    @pytest.mark.asyncio
    async def test_create_control(self, mock_client):
        device = Device(mock_client, "test_device", "test_driver", "Test Device")
        await device.initialize()
        mock_client.publish.reset_mock()

        meta = ControlMeta(title="Test Control", control_type="switch", order=1)
        await device.create_control("ctrl1", meta, "1")

        assert "ctrl1" in device._controls
        assert device._controls["ctrl1"].value == "1"
        assert device._controls["ctrl1"].meta.title.en == "Test Control"
        assert device._controls["ctrl1"].meta.control_type == "switch"

        assert mock_client.publish.call_count == 2

    @pytest.mark.asyncio
    async def test_set_control_value(self, mock_client):
        device = Device(mock_client, "test_device", "test_driver", "Test Device")
        await device.initialize()
        mock_client.publish.reset_mock()

        meta = ControlMeta(title="Test")
        await device.create_control("ctrl1", meta, "initial")
        mock_client.publish.reset_mock()

        await device.set_control_value("ctrl1", "updated")

        assert device._controls["ctrl1"].value == "updated"
        mock_client.publish.assert_called_once_with(
            "/devices/test_device/controls/ctrl1", "updated", retain=True
        )

    @pytest.mark.asyncio
    async def test_set_control_value_no_change(self, mock_client):
        device = Device(mock_client, "test_device", "test_driver", "Test Device")
        await device.initialize()

        meta = ControlMeta(title="Test")
        await device.create_control("ctrl1", meta, "value")
        mock_client.publish.reset_mock()

        await device.set_control_value("ctrl1", "value")

        mock_client.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_set_control_value_force(self, mock_client):
        device = Device(mock_client, "test_device", "test_driver", "Test Device")
        await device.initialize()

        meta = ControlMeta(title="Test")
        await device.create_control("ctrl1", meta, "value")
        mock_client.publish.reset_mock()

        await device.set_control_value("ctrl1", "value", force=True)

        mock_client.publish.assert_called_once_with(
            "/devices/test_device/controls/ctrl1", "value", retain=True
        )

    @pytest.mark.asyncio
    async def test_set_control_value_undeclared(self, mock_client, caplog):
        device = Device(mock_client, "test_device", "test_driver", "Test Device")
        await device.initialize()
        mock_client.publish.reset_mock()

        with caplog.at_level(logging.DEBUG):
            await device.set_control_value("nonexistent", "value")

        mock_client.publish.assert_not_called()
        assert "Can't set value of undeclared control" in caplog.text

    @pytest.mark.asyncio
    async def test_set_control_read_only(self, mock_client):
        device = Device(mock_client, "test_device", "test_driver", "Test Device")
        await device.initialize()

        meta = ControlMeta(title="Test", read_only=False)
        await device.create_control("ctrl1", meta, "value")
        mock_client.publish.reset_mock()

        await device.set_control_read_only("ctrl1", True)

        assert device._controls["ctrl1"].meta.read_only is True
        assert mock_client.publish.call_count == 1

    @pytest.mark.asyncio
    async def test_set_control_read_only_no_change(self, mock_client):
        device = Device(mock_client, "test_device", "test_driver", "Test Device")
        await device.initialize()

        meta = ControlMeta(title="Test", read_only=True)
        await device.create_control("ctrl1", meta, "value")
        mock_client.publish.reset_mock()

        await device.set_control_read_only("ctrl1", True)

        mock_client.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_set_control_title(self, mock_client):
        device = Device(mock_client, "test_device", "test_driver", "Test Device")
        await device.initialize()

        meta = ControlMeta(title="Old Title")
        await device.create_control("ctrl1", meta, "value")
        mock_client.publish.reset_mock()

        await device.set_control_title("ctrl1", "New Title")

        assert device._controls["ctrl1"].meta.title.en == "New Title"
        assert mock_client.publish.call_count == 1

    @pytest.mark.asyncio
    async def test_set_control_title_no_change(self, mock_client):
        device = Device(mock_client, "test_device", "test_driver", "Test Device")
        await device.initialize()

        meta = ControlMeta(title="Same Title")
        await device.create_control("ctrl1", meta, "value")
        mock_client.publish.reset_mock()

        await device.set_control_title("ctrl1", "Same Title")

        mock_client.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_set_control_error(self, mock_client):
        device = Device(mock_client, "test_device", "test_driver", "Test Device")
        await device.initialize()

        meta = ControlMeta(title="Test")
        await device.create_control("ctrl1", meta, "value")
        mock_client.publish.reset_mock()

        await device.set_control_error("ctrl1", "r")

        error_calls = [
            c
            for c in mock_client.publish.call_args_list
            if len(c[0]) > 0 and c[0][0] == "/devices/test_device/controls/ctrl1/meta/error"
        ]
        assert len(error_calls) == 1
        assert error_calls[0][0][1] == "r"
        assert device._controls["ctrl1"].error == "r"

    @pytest.mark.asyncio
    async def test_set_control_error_clears(self, mock_client):
        device = Device(mock_client, "test_device", "test_driver", "Test Device")
        await device.initialize()

        meta = ControlMeta(title="Test")
        await device.create_control("ctrl1", meta, "value")
        await device.set_control_error("ctrl1", "r")
        mock_client.publish.reset_mock()

        await device.set_control_value("ctrl1", "new_value")

        error_calls = [
            c
            for c in mock_client.publish.call_args_list
            if len(c[0]) > 0 and c[0][0] == "/devices/test_device/controls/ctrl1/meta/error"
        ]
        assert len(error_calls) == 1
        assert error_calls[0][0][1] is None
        assert device._controls["ctrl1"].error is None

    @pytest.mark.asyncio
    async def test_set_control_error_nonexistent(self, mock_client):
        device = Device(mock_client, "test_device", "test_driver", "Test Device")
        await device.initialize()
        mock_client.publish.reset_mock()

        await device.set_control_error("nonexistent", "r")

        mock_client.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_remove_control(self, mock_client):
        device = Device(mock_client, "test_device", "test_driver", "Test Device")
        await device.initialize()

        meta = ControlMeta(title="Test")
        await device.create_control("ctrl1", meta, "value")
        mock_client.publish.reset_mock()

        await device.remove_control("ctrl1")

        assert "ctrl1" not in device._controls
        assert mock_client.publish.call_count == 3
        mock_client.publish.assert_any_call("/devices/test_device/controls/ctrl1", None, retain=True)
        mock_client.publish.assert_any_call(
            "/devices/test_device/controls/ctrl1/meta/error", None, retain=True
        )
        mock_client.publish.assert_any_call("/devices/test_device/controls/ctrl1/meta", None, retain=True)

    @pytest.mark.asyncio
    async def test_remove_control_nonexistent(self, mock_client):
        device = Device(mock_client, "test_device", "test_driver", "Test Device")
        await device.initialize()
        mock_client.publish.reset_mock()

        await device.remove_control("nonexistent")

        mock_client.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_republish_control(self, mock_client):
        device = Device(mock_client, "test_device", "test_driver", "Test Device")
        await device.initialize()

        meta = ControlMeta(title="Test", control_type="switch")
        await device.create_control("ctrl1", meta, "1")
        mock_client.publish.reset_mock()

        await device.republish_control("ctrl1")

        assert mock_client.publish.call_count == 2

    @pytest.mark.asyncio
    async def test_republish_control_nonexistent(self, mock_client):
        device = Device(mock_client, "test_device", "test_driver", "Test Device")
        await device.initialize()
        mock_client.publish.reset_mock()

        await device.republish_control("nonexistent")

        mock_client.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_republish_device(self, mock_client):
        device = Device(mock_client, "test_device", "test_driver", "Test Device")
        await device.initialize()

        meta1 = ControlMeta(title="Control 1")
        meta2 = ControlMeta(title="Control 2")
        await device.create_control("ctrl1", meta1, "val1")
        await device.create_control("ctrl2", meta2, "val2")
        mock_client.publish.reset_mock()

        await device.republish_device()

        assert mock_client.publish.call_count >= 5

    @pytest.mark.asyncio
    async def test_remove_device(self, mock_client):
        device = Device(mock_client, "test_device", "test_driver", "Test Device")
        await device.initialize()

        meta1 = ControlMeta(title="Control 1")
        meta2 = ControlMeta(title="Control 2")
        await device.create_control("ctrl1", meta1, "val1")
        await device.create_control("ctrl2", meta2, "val2")
        mock_client.publish.reset_mock()

        await device.remove_device()

        assert len(device._controls) == 0
        assert mock_client.publish.call_count >= 5

    @pytest.mark.asyncio
    async def test_publish_control_meta_full(self, mock_client):
        device = Device(mock_client, "test_device", "test_driver", "Test Device")
        await device.initialize()

        meta = ControlMeta(
            title="Full Control",
            control_type="temperature",
            order=3,
            read_only=True,
            enum={"100": TranslatedTitle("aaa")},
            minimum=200,
            maximum=100,
        )
        await device.create_control("ctrl1", meta, "25")

        meta_calls = [
            c
            for c in mock_client.publish.call_args_list
            if "/meta" in str(c[0][0]) and "controls" in str(c[0][0])
        ]
        assert len(meta_calls) > 0

        meta_json = json.loads(meta_calls[0][0][1])
        assert meta_json["type"] == "temperature"
        assert meta_json["readonly"] is True
        assert meta_json["title"]["en"] == "Full Control"
        assert meta_json["order"] == 3
        assert meta_json["enum"] == {"100": {"en": "aaa"}}
        assert meta_json["min"] == 200
        assert meta_json["max"] == 100

    @pytest.mark.asyncio
    async def test_publish_control_meta_empty_enum(self, mock_client):
        device = Device(mock_client, "test_device", "test_driver", "Test Device")
        await device.initialize()

        meta = ControlMeta(
            title="Full Control",
            enum={
                "100": TranslatedTitle(),
                "200": None,
            },
        )
        await device.create_control("ctrl1", meta, "25")

        meta_calls = [
            c
            for c in mock_client.publish.call_args_list
            if "/meta" in str(c[0][0]) and "controls" in str(c[0][0])
        ]
        assert len(meta_calls) > 0

        meta_json = json.loads(meta_calls[0][0][1])
        assert meta_json["title"]["en"] == "Full Control"
        assert meta_json["enum"] == {"100": {"en": "100"}, "200": {"en": "200"}}

    @pytest.mark.asyncio
    async def test_publish_control_meta_minimal(self, mock_client):
        device = Device(mock_client, "test_device", "test_driver", "Test Device")
        await device.initialize()

        meta = ControlMeta()
        await device.create_control("ctrl1", meta, "value")

        meta_calls = [
            c
            for c in mock_client.publish.call_args_list
            if "/meta" in str(c[0][0]) and "controls" in str(c[0][0])
        ]
        assert len(meta_calls) > 0

        meta_json = json.loads(meta_calls[0][0][1])
        assert meta_json["type"] == "value"
        assert meta_json["readonly"] is False
        assert "title" not in meta_json
        assert "order" not in meta_json

    def test_get_control_base_topic(self, mock_client):
        device = Device(mock_client, "test_device", "test_driver", "Test Device")
        topic = device._get_control_base_topic("my_control")
        assert topic == "/devices/test_device/controls/my_control"

    @pytest.mark.asyncio
    async def test_device_meta_format(self, mock_client):
        device = Device(mock_client, "test_device", "test_driver", "Test Device")
        await device.initialize()

        meta_calls = [c for c in mock_client.publish.call_args_list if c[0][0] == "/devices/test_device/meta"]
        assert len(meta_calls) == 1

        meta_json = json.loads(meta_calls[0][0][1])
        assert "driver" in meta_json
        assert "title" in meta_json
        assert meta_json["driver"] == "test_driver"
        assert meta_json["title"]["en"] == "Test Device"
        assert meta_calls[0][1]["retain"] is True

    @pytest.mark.asyncio
    async def test_device_meta_without_title(self, mock_client):
        device = Device(mock_client, "test_device", "test_driver")
        await device.initialize()

        meta_calls = [c for c in mock_client.publish.call_args_list if c[0][0] == "/devices/test_device/meta"]
        assert len(meta_calls) == 1

        meta_json = json.loads(meta_calls[0][0][1])
        assert "driver" in meta_json
        assert "title" not in meta_json
        assert meta_json["driver"] == "test_driver"

    @pytest.mark.asyncio
    async def test_remove_device_clears_meta(self, mock_client):
        device = Device(mock_client, "test_device", "test_driver", "Test Device")
        await device.initialize()
        mock_client.publish.reset_mock()

        await device.remove_device()

        clear_calls = [
            c
            for c in mock_client.publish.call_args_list
            if c[0][0] == "/devices/test_device/meta" and c[0][1] is None
        ]
        assert len(clear_calls) == 1


class TestRetainHack:
    @pytest.mark.asyncio
    async def test_retain_hack_success(self, mock_dispatcher):
        with patch("wb.mqtt_dali.wbmqtt.random.random", return_value=0.12345):
            mock_dispatcher.client.add_message("/wbretainhack/1234500", b"2")

            await retain_hack(mock_dispatcher)

            mock_dispatcher.client.subscribe.assert_called()
            mock_dispatcher.client.publish.assert_called()
            mock_dispatcher.client.unsubscribe.assert_called()

    @pytest.mark.asyncio
    async def test_retain_hack_timeout(self, mock_dispatcher, caplog):
        with patch("wb.mqtt_dali.wbmqtt.random.random", return_value=0.54321):
            with caplog.at_level(logging.WARNING):
                await retain_hack(mock_dispatcher, timeout=1)

            assert "Retain hack timeout" in caplog.text


class TestRemoveTopicsByDriver:
    @pytest.mark.asyncio
    async def test_remove_topics_by_driver(self, mock_dispatcher):
        with patch("wb.mqtt_dali.wbmqtt.random.random", return_value=0.99999):
            device1_meta = json.dumps({"driver": "wb-mqtt-dali", "title": {"en": "DALI Device 1"}})
            mock_dispatcher.client.add_message("/devices/dali_device_1/meta", device1_meta.encode())
            mock_dispatcher.client.add_message("/devices/dali_device_1/controls/ch1", b"100")
            mock_dispatcher.client.add_message(
                "/devices/dali_device_1/controls/ch1/meta", b'{"type":"value"}'
            )

            device2_meta = json.dumps({"driver": "wb-mqtt-dali"})
            mock_dispatcher.client.add_message("/devices/dali_device_2/meta", device2_meta.encode())
            mock_dispatcher.client.add_message("/devices/dali_device_2/controls/level", b"50")

            other_device_meta = json.dumps({"driver": "other-driver"})
            mock_dispatcher.client.add_message("/devices/other_device/meta", other_device_meta.encode())
            mock_dispatcher.client.add_message("/devices/other_device/controls/ch1", b"value")

            mock_dispatcher.client.add_message("/wbretainhack/9999900", b"2")

            await remove_topics_by_driver(mock_dispatcher, "wb-mqtt-dali", timeout=1)

            mock_dispatcher.client.subscribe.assert_any_call("/devices/#")
            mock_dispatcher.client.unsubscribe.assert_called()

            clear_calls = [c for c in mock_dispatcher.client.publish.call_args_list if c[0][1] is None]

            cleared_topics = [c[0][0] for c in clear_calls]

            assert "/devices/dali_device_1/meta" in cleared_topics
            assert "/devices/dali_device_1/controls/ch1" in cleared_topics
            assert "/devices/dali_device_1/controls/ch1/meta" in cleared_topics
            assert "/devices/dali_device_2/meta" in cleared_topics
            assert "/devices/dali_device_2/controls/level" in cleared_topics

            assert "/devices/other_device/meta" not in cleared_topics
            assert "/devices/other_device/controls/ch1" not in cleared_topics

    @pytest.mark.asyncio
    async def test_remove_topics_no_matching(self, mock_dispatcher):
        with patch("wb.mqtt_dali.wbmqtt.random.random", return_value=0.11111):
            mock_dispatcher.client.add_message("/devices/other_device/controls/ch1", b"value")
            device_meta = json.dumps({"driver": "other-driver"})
            mock_dispatcher.client.add_message("/devices/other_device/meta", device_meta.encode())
            mock_dispatcher.client.add_message("/wbretainhack/1111100", b"2")

            await remove_topics_by_driver(mock_dispatcher, "wb-mqtt-dali", timeout=1)

            mock_dispatcher.client.subscribe.assert_called()
            mock_dispatcher.client.unsubscribe.assert_called()

            clear_calls = [
                c
                for c in mock_dispatcher.client.publish.call_args_list
                if c[0][1] is None and c[0][0].startswith("/devices/")
            ]
            assert len(clear_calls) == 0


class TestIntegration:  # pylint: disable=too-few-public-methods
    @pytest.mark.asyncio
    async def test_device_lifecycle(self, mock_client):
        device = Device(mock_client, "test_dev", "test_driver", "Test Device")
        await device.initialize()

        meta1 = ControlMeta(title="Switch", control_type="switch")
        meta2 = ControlMeta(title="Brightness", control_type="range")
        await device.create_control("switch", meta1, "0")
        await device.create_control("brightness", meta2, "50")

        assert len(device._controls) == 2

        await device.set_control_value("switch", "1")
        await device.set_control_value("brightness", "75")

        assert device._controls["switch"].value == "1"
        assert device._controls["brightness"].value == "75"

        publish_count_before = mock_client.publish.call_count
        await device.republish_device()
        assert mock_client.publish.call_count > publish_count_before

        await device.remove_control("brightness")
        assert len(device._controls) == 1
        assert "brightness" not in device._controls

        await device.remove_device()
        assert len(device._controls) == 0
