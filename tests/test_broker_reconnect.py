"""Recovery after a broker outage above the driver: gateway, bus start, service stop."""

import asyncio
import json
import re
import unittest
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

from dali.command import Command
from dali.frame import ForwardFrame

from wb.mqtt_dali.gateway import Gateway, bus_from_json
from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher
from wb.mqtt_dali.wbdali_error_response import GatewayUnavailable

from ._broker_link_helpers import Publish, RecordingClient


class _MockCommand(Command):
    def __init__(self):
        super().__init__(ForwardFrame(16, [0x12, 0x34]))
        self.sendtwice = False
        self.response = None


def _bus_of_publish(publish: Publish) -> str:
    """Which reconnect step a publish belongs to, so the order can be read as a list."""
    if publish.topic.startswith("/rpc/v1/wb-mqtt-serial/"):
        params = json.loads(publish.payload)["params"]
        kind = "queue reset" if params["function"] == 6 else "batch"
        return f"{kind} bus {params['address'] // 1000}"
    bus_number = re.search(r"gw_bus_(\d+)", publish.topic)
    if publish.topic.startswith("/devices/") and bus_number:
        return f"devices bus {bus_number.group(1)}"
    if publish.topic.startswith("/wb-dali/") and bus_number:
        return f"commissioning bus {bus_number.group(1)}"
    if publish.topic.startswith("/rpc/v1/wb-mqtt-dali/"):
        return "rpc markers"
    raise AssertionError(f"unexpected publish {publish}")


def _steps(publishes: List[Publish]) -> List[str]:
    steps: List[str] = []
    for publish in publishes:
        step = _bus_of_publish(publish)
        if not steps or steps[-1] != step:
            steps.append(step)
    return steps


class GatewayRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """A real Gateway with two buses against a recording client; wb-mqtt-serial is stubbed out."""

    def setUp(self):
        self.client = RecordingClient()
        self.dispatcher = MQTTDispatcher(self.client)
        config = {"gateways": [{"device_id": "gw", "buses": [{"devices": []}, {"devices": []}]}]}
        self.gateway = Gateway(config, self.dispatcher, "/nonexistent.conf", MagicMock(), command_registry={})

    async def asyncSetUp(self):
        with patch("wb.mqtt_dali.gateway.remove_topics_by_driver", AsyncMock()), patch(
            "wb.mqtt_dali.gateway.wait_for_rpc_endpoint", AsyncMock()
        ), patch("wb.mqtt_dali.gateway.rpc_call", AsyncMock(side_effect=asyncio.TimeoutError)):
            await self.gateway.start()
        self.client.clear()

    async def test_reconnect_restores_subscriptions_then_replays_devices_commissioning_and_markers(self):
        """Subscribes first, then the retained state in start order; no queue reset among them,
        the driver publishes it before its first batch."""
        self.dispatcher.connection_lost()
        self.client.clear()

        await self.dispatcher.connection_restored()
        await self.dispatcher.replay_retained()

        first_publish = next(i for i, call in enumerate(self.client.calls) if call[0] == "publish")
        self.assertTrue(first_publish > 0)
        self.assertTrue(all(call[0] == "subscribe" for call in self.client.calls[:first_publish]))
        self.assertTrue(all(call[0] == "publish" for call in self.client.calls[first_publish:]))
        self.assertEqual(
            _steps(self.client.publishes),
            ["devices bus 1", "devices bus 2", "commissioning bus 1", "commissioning bus 2", "rpc markers"],
        )
        self.assertTrue(all(p.retain for p in self.client.publishes))
        await self.gateway.stop()

    async def test_stop_with_the_link_up_removes_devices_and_markers(self):
        """The started gateway is stopped with the link up: devices, commissioning state and RPC
        endpoint markers of both buses are cleared with empty retained publishes."""
        await self.gateway.stop()

        cleared = {p.topic for p in self.client.publishes if p.payload is None and p.retain}
        self.assertIn("/rpc/v1/wb-mqtt-dali/Editor/GetList", cleared)
        self.assertIn("/wb-dali/gw_bus_1/commissioning", cleared)
        self.assertIn("/devices/gw_bus_1_broadcast/meta", cleared)
        self.assertIn("/devices/gw_bus_2_broadcast/controls/wanted_level", cleared)

    async def test_stop_during_outage_publishes_nothing(self):
        """The started gateway is stopped during an outage: it completes and nothing reaches the
        client."""
        self.dispatcher.connection_lost()
        self.client.clear()

        await self.gateway.stop()

        self.assertEqual(self.client.publishes, [])


class BusStartDuringOutageTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_completes_and_the_reconnect_replays_the_devices(self):
        """A bus started in an outage publishes nothing and refuses commands; the reconnect
        replays its devices."""
        client = RecordingClient()
        dispatcher = MQTTDispatcher(client)
        dispatcher.connection_lost()
        bus = bus_from_json("gw", 1, {"devices": []}, dispatcher, MagicMock())

        await bus.start()

        self.assertEqual(client.publishes, [])
        self.assertIsInstance(
            await asyncio.wait_for(bus.driver.send(_MockCommand()), timeout=0.5), GatewayUnavailable
        )
        await dispatcher.connection_restored()
        await dispatcher.replay_retained()
        self.assertEqual(_steps(client.publishes), ["devices bus 1"])
        self.assertIn("/devices/gw_bus_1_broadcast/meta", [p.topic for p in client.publishes])
        await bus.stop()
