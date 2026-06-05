import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import asyncio_mqtt as aiomqtt

from wb.mqtt_dali.main import default_service

_real_sleep = asyncio.sleep


async def _no_backoff_sleep(delay):
    # Patches asyncio.sleep module-wide: collapse the 1s reconnect backoff to a
    # bare yield, while keeping real cooperative yields (sleep(0)) working so
    # the test's own polling loops still advance.
    await _real_sleep(0)
    del delay


class _FakeClient:
    """Async context manager standing in for the MQTT client.

    Entering always succeeds (a connected session); exiting is a no-op. A broker
    drop is modelled by a child task raising MqttError rather than by the
    context manager, mirroring how asyncio_mqtt surfaces a lost connection
    through the gathered tasks. The same client instance is reused across
    reconnect iterations, as in production.
    """

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeDispatcher:  # pylint: disable=too-few-public-methods
    """Stand-in for MQTTDispatcher; run() is scriptable per reconnect iteration.

    A single instance is reused across reconnect iterations (the service builds
    one dispatcher up front). `run_behaviours[i]` drives the i-th run() call:
      "drop"  -> raise MqttError after letting the session settle, modelling a
                 broker loss surfacing through the dispatcher during normal
                 operation;
      "block" -> a healthy session: await until the loop cancels it.
    """

    def __init__(self, run_behaviours):
        self._run_behaviours = list(run_behaviours)
        self.run_calls = 0
        self.cancel_count = 0

    async def run(self):
        behaviour = self._run_behaviours[self.run_calls]
        self.run_calls += 1
        if behaviour == "drop":
            # Yield once so the rest of the session (gateway start) is in flight,
            # then surface the broker loss like a real dropped connection.
            await asyncio.sleep(0)
            raise aiomqtt.MqttError("broker connection lost")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancel_count += 1
            raise


class _FakeGateway:
    """Stand-in for Gateway with a scriptable start() per reconnect iteration.

    `start_behaviours[i]` drives the i-th start() call:
      "ok"           -> start succeeds, signal `healthy_session`;
      "init_fail"    -> flag the bus as still INITIALIZING and raise (a startup
                        that never finished), so the following stop() raises
                        RuntimeError like a real bus torn down mid-start.
    stop() raises RuntimeError while the gateway is flagged as initializing.
    """

    def __init__(self, start_behaviours):
        self._start_behaviours = list(start_behaviours)
        self.start_calls = 0
        self.stop_calls = 0
        self._initializing = False
        self.reconnected = asyncio.Event()

    async def start(self):
        behaviour = self._start_behaviours[self.start_calls]
        self.start_calls += 1
        if behaviour == "init_fail":
            self._initializing = True
            raise aiomqtt.MqttError("broker lost while bus INITIALIZING")
        # A successful start fully initializes the bus, so a later stop() no
        # longer raises — mirroring a real reconnect recovering from a prior
        # mid-start teardown.
        self._initializing = False
        if self.start_calls >= 2:
            # The healthy reconnect (second start) has begun.
            self.reconnected.set()

    async def stop(self):
        self.stop_calls += 1
        if self._initializing:
            raise RuntimeError("ApplicationController must be initialized to stop")


def _cancel_wait_for_cancel():
    """Deliver the same cancellation the SIGTERM handler would, ending the loop."""
    for task in asyncio.all_tasks():
        coro = task.get_coro()
        if getattr(coro, "__name__", None) == "wait_for_cancel":
            task.cancel()


class TestDefaultServiceReconnect(unittest.IsolatedAsyncioTestCase):
    async def _run_loop(self, gateway, dispatcher):
        async def reach_healthy_then_stop():
            # Once the reconnect has reached a healthy second session, signal a
            # graceful shutdown so the loop terminates and the test can assert.
            await gateway.reconnected.wait()
            _cancel_wait_for_cancel()

        with patch("wb.mqtt_dali.main.load_config", return_value={}), patch(
            "wb.mqtt_dali.main.DaliDatabase", return_value=object()
        ), patch("wb.mqtt_dali.main.MQTTDispatcher", return_value=dispatcher), patch(
            "wb.mqtt_dali.main.asyncio.sleep", new=_no_backoff_sleep
        ):
            helper = asyncio.create_task(reach_healthy_then_stop())
            result = await default_service(
                SimpleNamespace(config="x", broker_url="y"),
                client_factory=lambda _url: _FakeClient(),
                gateway_factory=lambda *_a: gateway,
            )
            await helper
        return result

    async def test_reconnect_cancels_children_on_broker_drop(self):
        """First session drops with MqttError mid-operation; the loop must tear
        down the child tasks (dispatcher cancelled and awaited, gateway folded
        via stop()) and run a second iteration that reconnects to a healthy
        session, which is then cancelled gracefully so the loop terminates."""
        gateway = _FakeGateway(["ok", "ok"])
        dispatcher = _FakeDispatcher(["drop", "block"])

        result = await self._run_loop(gateway, dispatcher)

        # Two start attempts: the dropped one and the healthy reconnect.
        self.assertEqual(gateway.start_calls, 2)
        # Each session folded the gateway exactly once — no old gateway left
        # running in parallel with the reconnect.
        self.assertEqual(gateway.stop_calls, 2)
        # The dispatcher ran in both sessions; the healthy second session's run
        # was cancelled during graceful teardown.
        self.assertEqual(dispatcher.run_calls, 2)
        self.assertGreaterEqual(dispatcher.cancel_count, 1)
        self.assertEqual(result, 0)

    async def test_reconnect_survives_bus_initializing_teardown_error(self):
        """The session drops with MqttError while the bus is still INITIALIZING,
        so gateway.stop() raises RuntimeError during teardown. The loop must
        swallow that teardown error, not exit, and proceed to a healthy
        reconnect that is then cancelled gracefully."""
        gateway = _FakeGateway(["init_fail", "ok"])
        dispatcher = _FakeDispatcher(["block", "block"])

        result = await self._run_loop(gateway, dispatcher)

        # The RuntimeError from stop() did not break the loop: it reconnected.
        self.assertEqual(gateway.start_calls, 2)
        self.assertEqual(result, 0)
