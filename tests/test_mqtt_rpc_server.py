import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from jsonrpc.exceptions import (
    JSONRPCDispatchException,
    JSONRPCInvalidRequest,
    JSONRPCMethodNotFound,
)
from mqttrpc.protocol import MQTTRPC10Request

from wb.mqtt_dali.mqtt_rpc_server import (
    MQTTRPCServer,
    get_request_topic_path,
    get_topic_path,
)


@pytest.fixture
def mock_mqtt_dispatcher():
    dispatcher = MagicMock()
    dispatcher.client = AsyncMock()
    dispatcher.subscribe = AsyncMock()
    dispatcher.unsubscribe = AsyncMock()
    return dispatcher


@pytest.fixture
def rpc_server(mock_mqtt_dispatcher):
    return MQTTRPCServer("test_driver", mock_mqtt_dispatcher)


class TestTopicPaths:
    def test_get_topic_path(self):
        result = get_topic_path("driver1", "service1", "method1")
        assert result == "/rpc/v1/driver1/service1/method1"

    def test_get_request_topic_path(self):
        result = get_request_topic_path("driver1")
        assert result == "/rpc/v1/driver1/+/+/+"


class TestMQTTRPCServer:
    @pytest.mark.asyncio
    async def test_start(self, rpc_server, mock_mqtt_dispatcher):
        await rpc_server.start()
        mock_mqtt_dispatcher.subscribe.assert_called_once()
        call_args = mock_mqtt_dispatcher.subscribe.call_args
        assert call_args[0][0] == "/rpc/v1/test_driver/+/+/+"

    @pytest.mark.asyncio
    async def test_add_endpoint(self, rpc_server, mock_mqtt_dispatcher):
        async def test_handler(params):
            return {"result": "test"}

        await rpc_server.add_endpoint("service1", "method1", test_handler)

        mock_mqtt_dispatcher.client.publish.assert_called_once_with(
            "/rpc/v1/test_driver/service1/method1", "1", retain=True, qos=1
        )
        assert "/rpc/v1/test_driver/service1/method1" in rpc_server._endpoints

    @pytest.mark.asyncio
    async def test_remove_endpoint(self, rpc_server, mock_mqtt_dispatcher):
        async def test_handler(params):
            return {"result": "test"}

        await rpc_server.add_endpoint("service1", "method1", test_handler)
        mock_mqtt_dispatcher.client.publish.reset_mock()

        await rpc_server.remove_endpoint("service1", "method1")

        mock_mqtt_dispatcher.client.publish.assert_called_once_with(
            "/rpc/v1/test_driver/service1/method1", payload=None, retain=True, qos=1
        )
        assert "/rpc/v1/test_driver/service1/method1" not in rpc_server._endpoints

    @pytest.mark.asyncio
    async def test_remove_endpoint_error(self, rpc_server, mock_mqtt_dispatcher):
        async def test_handler(params):
            return {"result": "test"}

        await rpc_server.add_endpoint("service1", "method1", test_handler)
        mock_mqtt_dispatcher.client.publish.side_effect = [None, Exception("Publish failed")]

        await rpc_server.remove_endpoint("service1", "method1")

        assert "/rpc/v1/test_driver/service1/method1" not in rpc_server._endpoints

    @pytest.mark.asyncio
    async def test_stop(self, rpc_server, mock_mqtt_dispatcher):
        async def test_handler(params):
            return {"result": "test"}

        await rpc_server.add_endpoint("service1", "method1", test_handler)
        await rpc_server.add_endpoint("service2", "method2", test_handler)
        mock_mqtt_dispatcher.client.publish.reset_mock()

        await rpc_server.stop()

        assert len(rpc_server._endpoints) == 0
        assert mock_mqtt_dispatcher.client.publish.call_count == 2
        mock_mqtt_dispatcher.client.publish.assert_has_calls(
            [
                call("/rpc/v1/test_driver/service1/method1", payload=None, retain=True, qos=1),
                call("/rpc/v1/test_driver/service2/method2", payload=None, retain=True, qos=1),
            ],
            any_order=True,
        )
        mock_mqtt_dispatcher.unsubscribe.assert_called_once_with("/rpc/v1/test_driver/+/+/+")

    @pytest.mark.asyncio
    async def test_stop_with_unsubscribe_error(self, rpc_server, mock_mqtt_dispatcher):
        mock_mqtt_dispatcher.unsubscribe.side_effect = Exception("Unsubscribe failed")
        await rpc_server.stop()
        mock_mqtt_dispatcher.unsubscribe.assert_called_once()

    def test_clear(self, rpc_server):
        rpc_server._endpoints = {"test": "value"}
        rpc_server.clear()
        assert len(rpc_server._endpoints) == 0

    @pytest.mark.asyncio
    async def test_on_request_creates_task(self, rpc_server):
        mqtt_message = MagicMock()
        original_create_task = asyncio.create_task
        created_tasks = []

        def track_create_task(coro, **kwargs):
            task = original_create_task(coro, **kwargs)
            created_tasks.append(task)
            return task

        with patch.object(asyncio, "create_task", side_effect=track_create_task):
            await rpc_server._on_request(mqtt_message)

        assert len(created_tasks) == 1

    @pytest.mark.asyncio
    async def test_handle_request_success(self, rpc_server):
        async def test_handler(params):
            return {"result": "success", "value": params.get("test")}

        await rpc_server.add_endpoint("service1", "method1", test_handler)

        mqtt_message = MagicMock()
        mqtt_message.topic = "/rpc/v1/test_driver/service1/method1/123"
        request = MQTTRPC10Request(params={"test": "data"}, _id="req1")
        mqtt_message.payload.decode.return_value = request.json

        response = await rpc_server._handle_request(mqtt_message)

        assert response.data["id"] == "req1"
        assert response.data["result"]["result"] == "success"
        assert response.data["result"]["value"] == "data"

    @pytest.mark.asyncio
    async def test_handle_request_invalid_json(self, rpc_server):
        mqtt_message = MagicMock()
        mqtt_message.topic = "/rpc/v1/test_driver/service1/method1/123"
        mqtt_message.payload.decode.return_value = "invalid json"

        response = await rpc_server._handle_request(mqtt_message)

        assert "error" in response.data
        assert response.data["error"]["code"] == JSONRPCInvalidRequest()._data["code"]

    @pytest.mark.asyncio
    async def test_handle_request_method_not_found(self, rpc_server):
        mqtt_message = MagicMock()
        mqtt_message.topic = "/rpc/v1/test_driver/service1/method1/123"
        request = MQTTRPC10Request(params={}, _id="req1")
        mqtt_message.payload.decode.return_value = request.json

        response = await rpc_server._handle_request(mqtt_message)

        assert "error" in response.data
        assert response.data["error"]["code"] == JSONRPCMethodNotFound()._data["code"]

    @pytest.mark.asyncio
    async def test_handle_request_handler_exception(self, rpc_server):

        async def failing_handler(params):
            if params.get("first_call"):
                raise ValueError("Test error")
            raise JSONRPCDispatchException(code=-32123, message="Dispatch error", data="Test error2")

        await rpc_server.add_endpoint("service1", "method1", failing_handler)

        mqtt_message = MagicMock()
        mqtt_message.topic = "/rpc/v1/test_driver/service1/method1/123"
        request = MQTTRPC10Request(params={"first_call": True}, _id="req1")
        mqtt_message.payload.decode.return_value = request.json

        response = await rpc_server._handle_request(mqtt_message)

        assert response.data["id"] == "req1"
        assert "error" in response.data
        assert response.data["error"]["code"] == -32000
        assert response.data["error"]["message"] == "Server error"
        assert response.data["error"]["data"] == "Test error"

        mqtt_message2 = MagicMock()
        mqtt_message2.topic = "/rpc/v1/test_driver/service1/method1/222"
        request = MQTTRPC10Request(params={}, _id="req2")
        mqtt_message2.payload.decode.return_value = request.json

        response = await rpc_server._handle_request(mqtt_message2)

        assert response.data["id"] == "req2"
        assert "error" in response.data
        assert response.data["error"]["code"] == -32123
        assert response.data["error"]["message"] == "Dispatch error"
        assert response.data["error"]["data"] == "Test error2"

    @pytest.mark.asyncio
    async def test_process_callback(self, rpc_server, mock_mqtt_dispatcher):
        async def test_handler(params):
            return {"result": "success"}

        await rpc_server.add_endpoint("service1", "method1", test_handler)
        mock_mqtt_dispatcher.client.publish.reset_mock()

        mqtt_message = MagicMock()
        mqtt_message.topic = "/rpc/v1/test_driver/service1/method1/123"
        request = MQTTRPC10Request(params={}, _id="req1")
        mqtt_message.payload.decode.return_value = request.json

        await rpc_server._process_callback(mqtt_message)

        mock_mqtt_dispatcher.client.publish.assert_called_once_with(
            "/rpc/v1/test_driver/service1/method1/123/reply",
            '{"result": {"result": "success"}, "error": null, "id": "req1"}',
            qos=2,
            retain=False,
        )

    @pytest.mark.asyncio
    async def test_process_callback_publish_error(self, rpc_server, mock_mqtt_dispatcher):
        async def test_handler(params):
            return {"result": "success"}

        await rpc_server.add_endpoint("service1", "method1", test_handler)
        mock_mqtt_dispatcher.client.publish.reset_mock()

        mock_mqtt_dispatcher.client.publish.side_effect = [None, Exception("Publish failed")]
        mqtt_message = MagicMock()
        mqtt_message.topic = "/rpc/v1/test_driver/service1/method1/123"
        request = MQTTRPC10Request(params={}, _id="req1")
        mqtt_message.payload.decode.return_value = request.json

        await rpc_server._process_callback(mqtt_message)

        mock_mqtt_dispatcher.client.publish.assert_called_once_with(
            "/rpc/v1/test_driver/service1/method1/123/reply",
            '{"result": {"result": "success"}, "error": null, "id": "req1"}',
            qos=2,
            retain=False,
        )
