import asyncio
import logging
import uuid

import paho.mqtt.client as mqtt
from jsonrpc.exceptions import JSONRPCDispatchException
from mqttrpc.protocol import MQTTRPC10Request, MQTTRPC10Response

from .mqtt_dispatcher import MQTTDispatcher
from .mqtt_rpc_server import get_topic_path


async def wait_for_rpc_endpoint(
    driver: str,
    service: str,
    method: str,
    mqtt_dispatcher: MQTTDispatcher,
    timeout: float = 5.0,
) -> None:
    fut = asyncio.get_running_loop().create_future()

    async def on_response(_mqtt_message) -> None:
        if not fut.done():
            fut.set_result(None)

    def timeout_callback():
        if not fut.done():
            fut.set_exception(TimeoutError("Timeout waiting for RPC endpoint"))

    timeout_handler = asyncio.get_running_loop().call_later(
        timeout,
        timeout_callback,
    )

    topic = get_topic_path(driver, service, method)
    await mqtt_dispatcher.subscribe(topic, on_response)
    try:
        await fut
    finally:
        timeout_handler.cancel()
        await mqtt_dispatcher.unsubscribe(topic, on_response)


async def rpc_call(
    driver: str,
    service: str,
    method: str,
    params: dict,
    mqtt_dispatcher: MQTTDispatcher,
    timeout: float = 2.0,
) -> dict:
    """
    Execute a remote procedure call over MQTT using JSON-RPC.
    It is a relatively slow implementation suitable for infrequent calls.

    Args:
        driver: The driver name identifying the target service provider.
        service: The service name within the driver.
        method: The RPC method name to invoke.
        params: Dictionary of parameters to pass to the remote method.
        mqtt_dispatcher: MQTT dispatcher client for publishing and subscribing to messages.
        timeout: Maximum time in seconds to wait for a response (default: 2.0).

    Returns:
        dict: The result from the remote procedure call response.

    Raises:
        JSONRPCDispatchException: If the RPC response contains an error.
        asyncio.TimeoutError: If the response is not received within the timeout period.
        Exception: If message parsing or processing fails.
    """
    topic_str = f"{get_topic_path(driver, service, method)}/wb-mqtt-dali-{uuid.uuid4()}"
    fut = asyncio.get_running_loop().create_future()

    async def on_response(mqtt_message: mqtt.MQTTMessage) -> None:
        if not fut.done():
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
    logger = logging.getLogger("MQTTRPCClient")
    try:
        request = MQTTRPC10Request(params=params, _id="1")
        logger.debug("RPC call %s: %s", topic_str, request.json)
        await mqtt_dispatcher.client.publish(topic_str, request.json, qos=2, retain=False)
        res = await asyncio.wait_for(fut, timeout)
        logger.debug("RPC response %s: %s", reply_topic, res)
        return res
    except Exception as e:  # pylint: disable=broad-exception-caught
        exception_message = str(e)
        if not exception_message:
            exception_message = type(e).__name__
        logger.error("RPC call to %s failed: %s", topic_str, exception_message)
        raise
    finally:
        await mqtt_dispatcher.unsubscribe(reply_topic, on_response)
