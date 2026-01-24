import asyncio
import logging
import uuid
from typing import Awaitable, Callable, Union

import paho.mqtt.client as mqtt
from jsonrpc.exceptions import (
    JSONRPCDispatchException,
    JSONRPCInvalidRequest,
    JSONRPCMethodNotFound,
    JSONRPCServerError,
)
from mqttrpc.protocol import MQTTRPC10Request, MQTTRPC10Response

from .mqtt_dispatcher import MQTTDispatcher

RpcHandlerFunction = Callable[[dict], Awaitable[Union[dict, list, str, int, float, bool, None]]]


def get_topic_path(driver: str, service: str, method: str) -> str:
    return f"/rpc/v1/{driver}/{service}/{method}"


def get_request_topic_path(driver: str) -> str:
    return get_topic_path(driver, "+", "+") + "/+"


class MQTTRPCServer:

    logger = logging.getLogger("MQTTRPCServer")

    def __init__(self, driver_name: str, mqtt_dispatcher: MQTTDispatcher) -> None:
        self.driver_name = driver_name

        self._endpoints: dict[str, RpcHandlerFunction] = {}
        self._mqtt_dispatcher = mqtt_dispatcher

    async def start(self) -> None:
        self.logger.debug("Starting MQTT RPC Server for driver: %s", self.driver_name)
        await self._mqtt_dispatcher.subscribe(get_request_topic_path(self.driver_name), self._on_request)

    async def add_endpoint(self, service: str, method: str, callback: RpcHandlerFunction) -> None:
        self.logger.debug("Add RPC: %s/%s", service, method)
        topic_str = get_topic_path(self.driver_name, service, method)
        await self._mqtt_dispatcher.client.publish(topic_str, "1", retain=True, qos=1)
        self._endpoints[topic_str] = callback

    async def remove_endpoint(self, service: str, method: str) -> None:
        self.logger.debug("Remove RPC: %s/%s", service, method)
        topic_str = get_topic_path(self.driver_name, service, method)
        try:
            if self._mqtt_dispatcher.is_running:
                await self._mqtt_dispatcher.client.publish(topic_str, payload=None, retain=True, qos=1)
        except Exception as e:  # pylint: disable=broad-exception-caught
            self.logger.error("Failed to delete RPC topic %s: %s", topic_str, e)
        finally:
            self._endpoints.pop(topic_str, None)

    async def stop(self) -> None:
        self.logger.debug("Clearing all endpoints")
        items = list(self._endpoints.items())
        for topic_str, _ in items:
            service, method = topic_str.split("/")[-2:]
            await self.remove_endpoint(service, method)
        if self._mqtt_dispatcher.is_running:
            try:
                await self._mqtt_dispatcher.unsubscribe(get_request_topic_path(self.driver_name))
            except Exception as e:  # pylint: disable=broad-exception-caught
                self.logger.error("Failed to unsubscribe from RPC requests: %s", e)

    async def _on_request(self, mqtt_message: mqtt.MQTTMessage) -> None:
        asyncio.create_task(self._process_callback(mqtt_message))

    async def _handle_request(self, mqtt_message: mqtt.MQTTMessage) -> str:
        try:
            request_string = mqtt_message.payload.decode()
            self.logger.debug("Request %s: %s", mqtt_message.topic, request_string)
            request = MQTTRPC10Request.from_json(request_string)
        except Exception as e:  # pylint: disable=broad-exception-caught
            self.logger.error("Invalid JSON-RPC request: %s", e)
            return MQTTRPC10Response(
                error=JSONRPCInvalidRequest()._data  # pylint: disable=protected-access
            ).json
        try:
            topic_base, _ = mqtt_message.topic.rsplit("/", 1)
            handler = self._endpoints.get(topic_base)
            if handler is None:
                self.logger.error("No RPC endpoint for topic: %s", mqtt_message.topic)
                return MQTTRPC10Response(
                    _id=request._id, error=JSONRPCMethodNotFound()._data  # pylint: disable=protected-access
                ).json
            result = await handler(request.params)
            return MQTTRPC10Response(_id=request._id, result=result).json  # pylint: disable=protected-access
        except JSONRPCDispatchException as e:
            self.logger.error("Error processing RPC request: %s", e)
            return MQTTRPC10Response(
                _id=request._id, error=e.error._data  # pylint: disable=protected-access
            ).json
        except Exception as e:  # pylint: disable=broad-exception-caught
            self.logger.error("Error processing RPC request: %s", e)
            return MQTTRPC10Response(
                _id=request._id,  # pylint: disable=protected-access
                error=JSONRPCServerError(data=str(e))._data,  # pylint: disable=protected-access
            ).json

    async def _process_callback(self, mqtt_message: mqtt.MQTTMessage) -> None:
        response = await self._handle_request(mqtt_message)
        try:
            reply_topic = mqtt_message.topic + "/reply"
            self.logger.debug("Response %s: %s", reply_topic, response)
            await self._mqtt_dispatcher.client.publish(reply_topic, response, qos=2, retain=False)
        except Exception as e:  # pylint: disable=broad-exception-caught
            self.logger.error("Failed to publish RPC response: %s", e)


async def rpc_call(
    driver: str,
    service: str,
    method: str,
    params: dict,
    mqtt_dispatcher: MQTTDispatcher,
    timeout: float = 10.0,
) -> dict:
    """
    Execute a remote procedure call over MQTT using JSON-RPC

    Args:
        driver: The driver name identifying the target service provider.
        service: The service name within the driver.
        method: The RPC method name to invoke.
        params: Dictionary of parameters to pass to the remote method.
        mqtt_dispatcher: MQTT dispatcher client for publishing and subscribing to messages.
        timeout: Maximum time in seconds to wait for a response (default: 10.0).

    Returns:
        dict: The result from the remote procedure call response.

    Raises:
        JSONRPCDispatchException: If the RPC response contains an error.
        asyncio.TimeoutError: If the response is not received within the timeout period.
        Exception: If message parsing or processing fails.
    """
    topic_str = f"{get_topic_path(driver, service, method)}/{uuid.uuid4()}"
    fut = asyncio.get_running_loop().create_future()

    async def on_response(mqtt_message: mqtt.MQTTMessage) -> None:
        try:
            response = MQTTRPC10Response.from_json(mqtt_message.payload.decode())
            if response.error:
                fut.set_exception(JSONRPCDispatchException(response.error))
            else:
                fut.set_result(response.result)
        except Exception as e:  # pylint: disable=broad-exception-caught
            fut.set_exception(e)

    reply_topic = topic_str + "/reply"
    await mqtt_dispatcher.subscribe(reply_topic, on_response)
    try:
        request = MQTTRPC10Request(params=params, _id="1")
        await mqtt_dispatcher.client.publish(topic_str, request.json, qos=2, retain=False)
        return await asyncio.wait_for(fut, timeout)
    finally:
        await mqtt_dispatcher.unsubscribe(reply_topic, on_response)
