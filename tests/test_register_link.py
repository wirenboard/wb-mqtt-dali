"""WBDALIDriver over the register link, against the simulated module.

The load-bearing test of the split: the same driver that runs on a controller
through wb-mqtt-serial, here writing the module's registers itself and polling
for the answers — one rewind, one batch write, one poll, one bulk read.
"""

# pylint: disable=redefined-outer-name

import asyncio
import logging

import pytest
import pytest_asyncio
from dali.address import GearBroadcast, GearShort
from dali.gear.general import DAPC, Off, QueryActualLevel, QueryControlGearPresent

from wb.mqtt_dali.gateway_link import RegisterLink
from wb.mqtt_dali.sim.control_gear import SimulatedControlGear
from wb.mqtt_dali.sim.dali_bus import SimulatedDaliBus
from wb.mqtt_dali.sim.network import SimulatedModbusNetwork
from wb.mqtt_dali.wbdali import WBDALIConfig, WBDALIDriver
from wb.mqtt_dali.wbdali_error_response import NoResponseFromGateway
from wb.mqtt_dali.wbdali_registers import (
    MONITOR_BASE,
    TransmissionStatus,
    encode_frame,
    queue_slot_address,
    to_registers,
)

MODULE = "wb-mdali_1"


class Stack:  # pylint: disable=too-few-public-methods
    """A simulated module and a register-link driver talking to it."""

    def __init__(self, gear=(), frame_delay_s: float = 0.0) -> None:
        self.network = SimulatedModbusNetwork(frame_delay_s=frame_delay_s)
        self.buses = {index: SimulatedDaliBus() for index in (1, 2, 3)}
        for unit in gear:
            self.buses[1].add_gear(unit)
        self.gateway = self.network.add_module(MODULE, self.buses)

    def driver(self, bus: int = 1, transport=None) -> WBDALIDriver:
        config = WBDALIConfig(device_name=MODULE, bus=bus)
        logger = logging.getLogger("test.register_link")
        link = RegisterLink(config, transport or self.network, logger)
        return WBDALIDriver(config, None, logger, link=link)


@pytest.fixture
def stack():
    return Stack(
        gear=[
            SimulatedControlGear(shortaddr=0, random_address=0x000010),
            SimulatedControlGear(shortaddr=1, random_address=0x400000),
        ]
    )


@pytest_asyncio.fixture
async def driver(stack):
    instance = stack.driver()
    await instance.initialize()
    try:
        yield instance
    finally:
        await instance.deinitialize()


@pytest.mark.asyncio
async def test_query_reaches_the_gear_and_the_answer_comes_back(stack, driver):
    await driver.send(DAPC(GearShort(0), 200))
    response = await driver.send(QueryActualLevel(GearShort(0)))
    assert response.value == 200
    assert stack.buses[1].gear[0].level == 200


@pytest.mark.asyncio
async def test_command_without_an_answer_completes(stack, driver):
    response = await driver.send(Off(GearBroadcast()))
    assert response.raw_value is None
    assert stack.buses[1].gear[0].level == 0


@pytest.mark.asyncio
async def test_each_answer_in_a_batch_lands_on_its_own_command(driver):
    await driver.send_commands([DAPC(GearShort(0), 10), DAPC(GearShort(1), 20)])
    responses = await driver.send_commands([QueryActualLevel(GearShort(0)), QueryActualLevel(GearShort(1))])
    assert [response.value for response in responses] == [10, 20]


@pytest.mark.asyncio
async def test_query_to_an_empty_address_reports_no_answer(driver):
    response = await driver.send(QueryControlGearPresent(GearShort(40)))
    assert response.raw_value is None


@pytest.mark.asyncio
async def test_an_unwired_bus_is_silent(stack):
    """Buses 2 and 3 of the module exist but have no gear on them."""
    instance = stack.driver(bus=2)
    await instance.initialize()
    try:
        response = await instance.send(QueryControlGearPresent(GearShort(0)))
        assert response.raw_value is None
    finally:
        await instance.deinitialize()


@pytest.mark.asyncio
async def test_a_long_run_of_commands_keeps_answering():
    """Twenty answers, more than the module's queue holds, arriving as two batches."""
    stack = Stack(gear=[SimulatedControlGear(shortaddr=i, random_address=0x1000 + i) for i in range(20)])
    instance = stack.driver()
    await instance.initialize()
    try:
        responses = await instance.send_commands([QueryControlGearPresent(GearShort(i)) for i in range(20)])
        assert len(responses) == 20
        assert all(response.value for response in responses)
    finally:
        await instance.deinitialize()


@pytest.mark.asyncio
async def test_a_module_that_stops_answering_modbus_does_not_hang_the_caller(stack, driver):
    """The Modbus request itself fails, before any reply polling."""
    stack.gateway.reachable = False
    response = await asyncio.wait_for(driver.send(QueryActualLevel(GearShort(0))), timeout=3.0)
    assert isinstance(response, NoResponseFromGateway)


@pytest.mark.asyncio
async def test_a_gateway_that_never_transmits_gives_up_on_the_frame(stack, driver):
    """A reply register stuck at "no transmission" must not be polled forever."""
    driver.response_timeout = 0.2
    # Accept the write but never transmit, which is what a wedged queue looks like.
    stack.gateway.buses[1].dali_bus.send_frame = lambda *_: (TransmissionStatus.NO_TRANSMISSION, 0)
    started = asyncio.get_running_loop().time()
    response = await asyncio.wait_for(driver.send(QueryActualLevel(GearShort(0))), timeout=5.0)
    elapsed = asyncio.get_running_loop().time() - started
    assert isinstance(response, NoResponseFromGateway)
    assert 0.2 <= elapsed < 2.0


@pytest.mark.asyncio
async def test_a_module_that_comes_back_answers_again(stack, driver):
    driver.response_timeout = 0.2
    stack.gateway.reachable = False
    assert isinstance(await driver.send(QueryActualLevel(GearShort(0))), NoResponseFromGateway)
    stack.gateway.reachable = True
    assert (await driver.send(QueryActualLevel(GearShort(0)))).value == 0


@pytest.mark.asyncio
async def test_repeating_a_query_gets_a_fresh_answer_each_time(driver):
    """Consecutive identical answers must not look like a stale reply register:
    the module clears a reply register when its slot is written, and the link
    relies on that rather than on comparing values."""
    await driver.send(DAPC(GearShort(0), 42))
    for _ in range(4):
        assert (await driver.send(QueryActualLevel(GearShort(0)))).value == 42


@pytest.mark.asyncio
async def test_the_gateway_will_not_reach_past_its_pointer(stack, driver):  # pylint: disable=unused-argument
    """Measured on a real WB-DALI: with the pointer at 0, a frame written into
    slot 5 sat there unsent and the pointer never moved."""
    bus = stack.gateway.buses[1]
    assert bus.pointer == 0
    frame = encode_frame(QueryControlGearPresent(GearBroadcast()).frame.as_integer, 16, False, 2)
    stack.gateway.write_holding(queue_slot_address(1, 5), to_registers(frame))
    assert bus.pointer == 0, "the gateway must wait at its pointer"
    assert bus.replies[5] == 0, "and report nothing for the slot it has not reached"


@pytest.mark.asyncio
async def test_a_frame_goes_out_whatever_the_pointer_was(stack, driver):
    """The link rewinds before every batch, so no earlier state can leave it stalled —
    the property the round-robin discipline lacked on hardware."""
    for stale_pointer in (0, 5, 15):
        stack.gateway.buses[1].pointer = stale_pointer
        response = await driver.send(QueryControlGearPresent(GearShort(0)))
        assert response.value is True, f"stalled with the pointer at {stale_pointer}"


class _CountingTransport:
    def __init__(self, inner):
        self.inner = inner
        self.writes = []
        self.reads = []

    async def read_input(self, device_id, address, count):
        self.reads.append((address, count))
        return await self.inner.read_input(device_id, address, count)

    async def write_holding(self, device_id, address, values):
        self.writes.append((address, len(values)))
        await self.inner.write_holding(device_id, address, values)


@pytest.mark.asyncio
async def test_a_batch_is_one_write_and_one_bulk_read(stack):
    """Ten commands must not cost ten writes and ten polled reads — one rewind,
    one fc16 carrying every frame, a poll on the last slot, one bulk read."""
    transport = _CountingTransport(stack.network)
    instance = stack.driver(transport=transport)
    await instance.initialize()
    try:
        transport.writes.clear()
        transport.reads.clear()
        responses = await instance.send_commands([QueryControlGearPresent(GearShort(0)) for _ in range(10)])
        assert all(response.value for response in responses)
        # One pointer rewind (1 register) plus one batch write (20 registers).
        assert transport.writes == [(1432, 1), (1400, 20)]
        # The ring poll is its own traffic; only the batch reads are counted.
        reads = [read for read in transport.reads if read[0] < MONITOR_BASE]
        # Polls of the last slot's reply, then one 10-register bulk read.
        assert reads[-1] == (1500, 10)
        assert all(read == (1509, 1) for read in reads[:-1])
    finally:
        await instance.deinitialize()
