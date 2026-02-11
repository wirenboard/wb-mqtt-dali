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
            self.logger.exception("Error processing RPC request: %s", e)
            return MQTTRPC10Response(
                _id=request._id, error=e.error._data  # pylint: disable=protected-access
            ).json
        except Exception as e:  # pylint: disable=broad-exception-caught
            exception_message = str(e)
            if not exception_message:
                exception_message = type(e).__name__
            self.logger.exception("Error processing RPC request: %s", exception_message)
            return MQTTRPC10Response(
                _id=request._id,  # pylint: disable=protected-access
                error=JSONRPCServerError(data=exception_message)._data,  # pylint: disable=protected-access
            ).json

    async def _process_callback(self, mqtt_message: mqtt.MQTTMessage) -> None:
        response = await self._handle_request(mqtt_message)
        try:
            reply_topic = mqtt_message.topic + "/reply"
            self.logger.debug("Response %s: %s", reply_topic, response)
            await self._mqtt_dispatcher.client.publish(reply_topic, response, qos=2, retain=False)
        except Exception as e:  # pylint: disable=broad-exception-caught
            self.logger.error("Failed to publish RPC response: %s", e)
