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

from wb.mqtt_dali.gateway_link import RegisterLink, _frame_counter_order
from wb.mqtt_dali.sim.control_gear import SimulatedControlGear
from wb.mqtt_dali.sim.dali_bus import SimulatedDaliBus
from wb.mqtt_dali.sim.network import SimulatedModbusNetwork
from wb.mqtt_dali.wbdali import WBDALIConfig, WBDALIDriver
from wb.mqtt_dali.wbdali_error_response import NoResponseFromGateway
from wb.mqtt_dali.wbdali_registers import (
    FRAME_COUNTER_MODULO,
    MONITOR_BASE,
    MONITOR_RING_SIZE,
    TransmissionStatus,
    encode_frame,
    encode_monitor_slot,
    monitor_address,
    queue_slot_address,
    to_monitor_registers,
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


class _SlowTransport:
    """Every register exchange costs a USB round trip — a WebSerial port, not a controller."""

    def __init__(self, inner, delay_s: float):
        self.inner = inner
        self.delay_s = delay_s

    async def read_input(self, device_id, address, count):
        await asyncio.sleep(self.delay_s)
        return await self.inner.read_input(device_id, address, count)

    async def write_holding(self, device_id, address, values):
        await asyncio.sleep(self.delay_s)
        await self.inner.write_holding(device_id, address, values)


@pytest.mark.asyncio
async def test_the_reply_clock_covers_the_register_exchanges_of_a_slow_port(stack):
    """Seen on hardware: a 3-frame batch needs five exchanges of ~280 ms each
    over WebSerial, and the driver — its clock armed before the first of them,
    from the bus time alone — reported every frame unanswered."""
    transport = _SlowTransport(stack.network, delay_s=0.05)
    instance = stack.driver(transport=transport)
    instance.response_timeout = 0.1  # The bus time alone: less than the exchanges cost.
    await instance.initialize()
    try:
        for _ in range(3):
            response = await instance.send(QueryActualLevel(GearShort(0)))
            assert not isinstance(response, NoResponseFromGateway)
            assert response.value == 0
        link = instance._link  # pylint: disable=protected-access
        assert link.round_trip_s >= 0.04
        assert link.reply_timeout(1, 0.1) > 0.1 + 5 * 0.04
    finally:
        await instance.deinitialize()


@pytest.mark.asyncio
async def test_deinitialize_returns_while_a_batch_is_in_flight_on_a_slow_port(stack):
    """Shutting down mid-batch used to hang: the sender's cancellation was lost in
    a `finally: continue` and deinitialize() waited on a task that kept running."""
    transport = _SlowTransport(stack.network, delay_s=0.3)
    instance = stack.driver(transport=transport)
    await instance.initialize()
    pending = asyncio.ensure_future(instance.send(QueryActualLevel(GearShort(0))))
    await asyncio.sleep(0.4)  # The batch is inside its register exchanges now.
    await asyncio.wait_for(instance.deinitialize(), timeout=2.0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending


class _RingTransport:
    """A module whose ring holds whatever the test puts there; nothing else answers."""

    def __init__(self, ring):
        self.ring = ring

    async def read_input(self, device_id, address, count):
        del device_id
        registers = []
        for slot in self.ring:
            registers.extend(to_monitor_registers(slot))
        offset = address - monitor_address(1, 0)
        return registers[offset : offset + count]

    async def write_holding(self, device_id, address, values):
        pass


class _Listener:
    def __init__(self):
        self.slots = []

    def on_reply(self, index, value):
        pass

    def on_monitor_slot(self, raw):
        self.slots.append((raw >> 48) & 0xFFFF)

    def on_gateway_error_payload(self, payload):
        pass


@pytest.mark.asyncio
async def test_frames_found_in_one_poll_arrive_in_bus_order():
    """The ring is circular: after a wrap the newest frame sits in slot 0 and the
    older ones follow. Delivered by slot, the handler saw the counter go
    backwards and dropped three of four frames."""
    counters_by_slot = [3657, 3654, 3655, 3656]
    ring = [0] * MONITOR_RING_SIZE
    transport = _RingTransport(ring)
    link = RegisterLink(WBDALIConfig(device_name=MODULE, bus=1), transport, logging.getLogger("test.ring"))
    listener = _Listener()
    await link.start(listener)
    link.set_bus_monitor_enabled(True)
    try:
        for slot, counter in enumerate(counters_by_slot):
            ring[slot] = encode_monitor_slot(counter, 16, 0xFF00, False, False)
        await asyncio.sleep(0.3)
        assert listener.slots == [3654, 3655, 3656, 3657]
    finally:
        await link.stop()


def test_frame_counter_order_survives_the_counter_wrapping():
    raws = [counter << 48 for counter in (1, FRAME_COUNTER_MODULO - 2, 0, FRAME_COUNTER_MODULO - 1)]
    ordered = [(raw >> 48) & 0xFFFF for raw in sorted(raws, key=_frame_counter_order(raws))]
    assert ordered == [FRAME_COUNTER_MODULO - 2, FRAME_COUNTER_MODULO - 1, 0, 1]
