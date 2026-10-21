"""Gateway.run() when the polling loop of one of its buses dies."""

import asyncio
import unittest
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

from wb.mqtt_dali.common_dali_device import DaliDeviceBase
from wb.mqtt_dali.gateway import BusPollingError, Gateway
from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher

from ._app_controller_helpers import LoopKiller
from ._on_off_helpers import BusScript, ScriptedGatewayClient

DaliDeviceBase._common_schema = {"title": "test-schema"}  # pylint: disable=protected-access


class GatewayRunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # A configured device is absent from the bus, and publishing its error state raises
        # past every handler of the polling loop.
        self.client = ScriptedGatewayClient(BusScript(present_shorts=frozenset()), "gw", 1)
        self.client.fail("publish", "/devices/gw_bus_1_5/meta", LoopKiller())
        self.dispatcher = MQTTDispatcher(self.client)
        config = {"gateways": [{"device_id": "gw", "buses": [{"devices": [{"short": 5, "random": 123}]}]}]}
        self.gateway = Gateway(config, self.dispatcher, "/nonexistent.conf", MagicMock(), command_registry={})
        self.dispatch_task: Optional[asyncio.Task] = None

    async def asyncSetUp(self):
        self.dispatch_task = asyncio.create_task(self.dispatcher.run())

    async def asyncTearDown(self):
        self.dispatch_task.cancel()

    async def test_run_raises_when_a_bus_polling_loop_dies(self):
        """The polling loop of the only bus dies: run() raises BusPollingError."""
        with self.assertRaises(BusPollingError):
            await self._run()
        await self.gateway.stop()

    async def test_stop_after_a_bus_polling_loop_died_removes_the_bus_topics(self):
        """run() has raised BusPollingError: stop() completes and clears the retained topics of the bus."""
        with self.assertRaises(BusPollingError):
            await self._run()
        self.client.failures.clear()
        self.client.clear()

        await self.gateway.stop()

        self.assertIn(
            ("/devices/gw_bus_1_broadcast/meta", None), [(p.topic, p.payload) for p in self.client.publishes]
        )

    # --- Private ---

    async def _run(self) -> None:
        with patch("wb.mqtt_dali.gateway.remove_topics_by_driver", AsyncMock()), patch(
            "wb.mqtt_dali.gateway.wait_for_rpc_endpoint", AsyncMock()
        ), patch("wb.mqtt_dali.gateway.rpc_call", AsyncMock(side_effect=asyncio.TimeoutError)):
            await asyncio.wait_for(self.gateway.run(), timeout=5.0)
