import asyncio
import json
import logging
import random
import string
from enum import Enum
from typing import Optional

import aiomqtt
from mqttrpc.client import MQTTRPCError

from .mqtt_dispatcher import MQTTDispatcher, get_str_payload
from .mqtt_rpc_server import RpcResult, get_topic_path


async def wait_for_rpc_endpoint(
    driver: str,
    service: str,
    method: str,
    mqtt_dispatcher: MQTTDispatcher,
    timeout: Optional[float] = 5.0,
) -> None:
    """Wait for the endpoint to be published; a `timeout` of None waits indefinitely."""
    fut = asyncio.get_running_loop().create_future()

    def on_response(_mqtt_message) -> None:
        if not fut.done():
            fut.set_result(None)

    def timeout_callback():
        if not fut.done():
            fut.set_exception(TimeoutError("Timeout waiting for RPC endpoint"))

    timeout_handler = (
        None if timeout is None else asyncio.get_running_loop().call_later(timeout, timeout_callback)
    )

    topic = get_topic_path(driver, service, method)
    await mqtt_dispatcher.subscribe(topic, on_response)
    try:
        await fut
    finally:
        if timeout_handler is not None:
            timeout_handler.cancel()
        await mqtt_dispatcher.unsubscribe(topic, on_response)


class MQTTRPCErrorCode(Enum):
    # Server failed to parse the request JSON.
    PARSE_ERROR = -32700
    # libwbmqtt1 / json-rpc: any server-side failure, reason in `data`.
    SERVER_ERROR = -32000
    # libwbmqtt1: request timed out (wb-mqtt-serial also for a silent device); json-rpc: Invalid Request.
    REQUEST_TIMEOUT = -32600
    # libwbmqtt0 (before 2018): request timed out.
    LEGACY_REQUEST_TIMEOUT = -32100
    # json-rpc servers only.
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603


REQUEST_TIMEOUT_CODES = (
    MQTTRPCErrorCode.REQUEST_TIMEOUT.value,
    MQTTRPCErrorCode.LEGACY_REQUEST_TIMEOUT.value,
)


class MQTTRPCClientError(MQTTRPCError):
    """The client failed the call itself, with no error from the server."""

    def __init__(self, message: str) -> None:
        super().__init__(message, None, None)

    def __str__(self) -> str:
        return self.rpc_message


class MQTTRPCCallTimeoutError(MQTTRPCClientError):
    """No reply within the call's timeout."""


class MQTTRPCInvalidReplyError(MQTTRPCClientError):
    """The reply is not a JSON-RPC response."""


class MQTTRPCRequestTimeoutError(MQTTRPCError):
    """A reply with a request timeout code; `data` says why."""


def make_rpc_error(error: object) -> MQTTRPCError:
    """The exception for a JSON-RPC `error` member of a reply."""
    if not isinstance(error, dict):
        return MQTTRPCError(str(error), None, None)
    code = error.get("code")
    if code in REQUEST_TIMEOUT_CODES:
        return MQTTRPCRequestTimeoutError(error.get("message"), code, error.get("data"))
    return MQTTRPCError(error.get("message"), code, error.get("data"))


class MQTTRPCClient:
    """JSON-RPC client over MQTT; subscribes to its replies once and makes one call at a time."""

    logger = logging.getLogger("MQTTRPCClient")

    def __init__(self) -> None:
        self._mqtt_dispatcher: Optional[MQTTDispatcher] = None
        self.client_id = ""
        self._last_id = 0
        self._call_lock = asyncio.Lock()
        self._reply: Optional[asyncio.Future[RpcResult]] = None
        self._running = False

    async def start(self, mqtt_dispatcher: MQTTDispatcher) -> None:
        self._mqtt_dispatcher = mqtt_dispatcher
        client_id_suffix = "".join(random.sample(string.ascii_letters + string.digits, 8))
        self.client_id = f"{mqtt_dispatcher.client_id.replace('/', '_')}-{client_id_suffix}"
        await mqtt_dispatcher.subscribe(self._reply_topic, self._handle_reply)
        self._running = True

    async def stop(self) -> None:
        self._running = False
        if self._reply is not None and not self._reply.done():
            self._reply.set_exception(MQTTRPCClientError("RPC client stopped"))
        if self._mqtt_dispatcher is not None:
            await self._mqtt_dispatcher.unsubscribe(self._reply_topic, self._handle_reply)

    async def call(  # pylint: disable=too-many-arguments, too-many-positional-arguments
        self, driver: str, service: str, method: str, params: dict, timeout_s: float
    ) -> RpcResult:
        """
        Execute a remote procedure call over MQTT using JSON-RPC.

        Args:
            driver: The driver name identifying the target service provider.
            service: The service name within the driver.
            method: The RPC method name to invoke.
            params: Dictionary of parameters to pass to the remote method.
            timeout_s: Maximum time in seconds for the whole call, waiting for a previous one included.

        Returns:
            The result from the remote procedure call response.

        Raises:
            MQTTRPCRequestTimeoutError: The response has a request timeout code; `data` says why.
            MQTTRPCError: The RPC response contains any other error.
            MQTTRPCInvalidReplyError: The response could not be parsed.
            MQTTRPCCallTimeoutError: No response within `timeout_s`.
            MQTTRPCClientError: The client is not running or was stopped during the call.
            aiomqtt.MqttError: The request could not be published.
            Exception: Any other unexpected failure.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        topic = f"{get_topic_path(driver, service, method)}/{self.client_id}"
        try:
            await asyncio.wait_for(self._call_lock.acquire(), timeout_s)
        except asyncio.TimeoutError as exc:
            raise MQTTRPCCallTimeoutError(
                f"{topic}: previous call still running after {timeout_s:.1f} s"
            ) from exc
        try:
            if not self._running:
                raise MQTTRPCClientError(f"{topic}: RPC client is not running")
            self._last_id += 1
            self._reply = loop.create_future()
            payload = json.dumps({"params": params, "id": self._last_id})
            self.logger.debug("RPC call %s: %s", topic, payload)
            await self._mqtt_dispatcher.publish(topic, payload)
            result = await asyncio.wait_for(self._reply, max(0.0, deadline - loop.time()))
            self.logger.debug("RPC response %s: %s", topic, result)
            return result
        except asyncio.TimeoutError as exc:
            raise MQTTRPCCallTimeoutError(f"no reply to {topic} in {timeout_s:.1f} s") from exc
        finally:
            self._reply = None
            self._call_lock.release()

    # --- Private ---

    @property
    def _reply_topic(self) -> str:
        return f"{get_topic_path('+', '+', '+')}/{self.client_id}/reply"

    def _handle_reply(self, message: aiomqtt.Message) -> None:
        reply = self._reply
        if message.retain or reply is None or reply.done():
            return
        try:
            body = json.loads(get_str_payload(message))
            call_id = body["id"]
            if call_id != self._last_id:
                self.logger.debug("RPC %s answered after its timeout", call_id)
                return
            error = body.get("error")
            if error:
                reply.set_exception(make_rpc_error(error))
            else:
                reply.set_result(body.get("result"))
        except Exception as exc:  # pylint: disable=broad-exception-caught
            reply.set_exception(
                MQTTRPCInvalidReplyError(f"failed to parse RPC reply '{message.payload}': {exc}")
            )


async def rpc_call(  # pylint: disable=too-many-arguments, R0917
    driver: str,
    service: str,
    method: str,
    params: dict,
    mqtt_dispatcher: MQTTDispatcher,
    timeout: float = 2.0,
) -> RpcResult:
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
        The result from the remote procedure call response.

    Raises:
        MQTTRPCRequestTimeoutError: The response has a request timeout code; `data` says why.
        MQTTRPCError: The RPC response contains any other error.
        MQTTRPCInvalidReplyError: The response could not be parsed.
        MQTTRPCCallTimeoutError: No response within `timeout`.
        aiomqtt.MqttError: The request could not be published.
        Exception: Any other unexpected failure.
    """
    client = MQTTRPCClient()
    await client.start(mqtt_dispatcher)
    try:
        return await client.call(driver, service, method, params, timeout)
    except Exception as exc:
        MQTTRPCClient.logger.error("RPC call to %s/%s/%s failed: %s", driver, service, method, exc)
        raise
    finally:
        await client.stop()
