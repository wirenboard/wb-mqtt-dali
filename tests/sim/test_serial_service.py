"""The unmodified WBDALIDriver against the simulator, through the fake wb-mqtt-serial.

What the daemon does on a controller — write the queue through port/Load, read
answers and bus traffic from the controls wb-mqtt-serial publishes — happens here
against simulated modules over the in-process broker.
"""

import asyncio
import logging

import pytest
from dali.address import GearShort
from dali.gear.general import DAPC, QueryActualLevel

from wb.mqtt_dali.bus_traffic import BusTrafficSource
from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher
from wb.mqtt_dali.sim import (
    Broker,
    Client,
    FakeWbMqttSerial,
    build_network,
    default_scenario,
    serial_config,
)
from wb.mqtt_dali.wbdali import WBDALIConfig, WBDALIDriver

MODULE = "wb-mdali_1"


class _Stack:
    def __init__(self) -> None:
        self.scenario = default_scenario()
        # An addressed wall switch: an unaddressed one has no short address to
        # put into its event frames yet.
        self.scenario["gateways"][0]["buses"]["1"]["devices"][0]["shortAddress"] = 0
        self.network = build_network(self.scenario)
        self.broker = Broker()
        self.serial = FakeWbMqttSerial(
            self.broker, self.network, serial_config(self.scenario), poll_interval_s=0.01
        )
        self.client = Client(self.broker, "wb-mqtt-dali")
        self.dispatcher = MQTTDispatcher(self.client)
        self.dispatcher_task = None
        self.driver = WBDALIDriver(
            WBDALIConfig(device_name=MODULE, bus=1), self.dispatcher, logging.getLogger("test.sim")
        )

    async def __aenter__(self) -> "_Stack":
        await self.serial.start()
        await self.client.__aenter__()
        self.dispatcher_task = asyncio.create_task(self.dispatcher.run())
        await self.driver.initialize()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.driver.deinitialize()
        self.dispatcher_task.cancel()
        try:
            await self.dispatcher_task
        except asyncio.CancelledError:
            pass
        await self.client.__aexit__(None, None, None)
        await self.serial.stop()
        self.network.stop()


@pytest.mark.asyncio
async def test_the_daemons_driver_gets_answers_from_simulated_gear():
    async with _Stack() as stack:
        await asyncio.wait_for(stack.driver.send(DAPC(GearShort(0), 128)), 5.0)
        response = await asyncio.wait_for(stack.driver.send(QueryActualLevel(GearShort(0))), 5.0)
        assert response.value == 128


@pytest.mark.asyncio
async def test_a_batch_of_queries_is_answered_slot_by_slot():
    async with _Stack() as stack:
        commands = [
            QueryActualLevel(GearShort(0)),
            QueryActualLevel(GearShort(1)),
            QueryActualLevel(GearShort(9)),
        ]
        responses = await asyncio.wait_for(stack.driver.send_commands(commands), 5.0)
        assert [r.value for r in responses[:2]] == [0, 0]
        # Nothing lives at short address 9 in the default scenario.
        assert responses[2].raw_value is None


@pytest.mark.asyncio
async def test_a_button_press_on_the_bus_reaches_the_driver_as_foreign_traffic():
    async with _Stack() as stack:
        seen = []
        stack.driver.bus_traffic.register(seen.append)
        assert stack.network.press_button(MODULE, 1, 0)
        for _ in range(50):
            await asyncio.sleep(0.02)
            if seen:
                break
        assert seen and seen[0].request_source is BusTrafficSource.BUS
