"""A gateway fault must degrade a memoized batch, never abort it.

The gateway's error responses (NoResponseFromGateway, NoPowerOnBus, Overheat)
implement `raw_value` as a property that raises — the memo must treat them as
"nothing learned", not let the exception escape the driver's send path.
"""

import logging

import pytest
from dali.address import GearShort
from dali.gear.general import DTR0, DTR1, QuerySceneLevel, ReadMemoryLocation

from wb.mqtt_dali.bus_traffic import BusTrafficSource
from wb.mqtt_dali.gateway_link import RegisterLink
from wb.mqtt_dali.memory_cache import MemoryCache
from wb.mqtt_dali.wbdali import WBDALIConfig, WBDALIDriver
from wb.mqtt_dali.wbdali_error_response import NoPowerOnBus, NoResponseFromGateway


def test_observing_a_gateway_error_learns_nothing_and_does_not_raise():
    cache = MemoryCache()
    query = QuerySceneLevel(GearShort(0), 7)

    cache.observe(query, NoResponseFromGateway(), delivered=False)
    cache.observe(query, NoPowerOnBus(), delivered=True)
    assert cache.plan([query]) is None

    cache.observe(DTR1(0), None)
    cache.observe(DTR0(3), None)
    cache.observe(ReadMemoryLocation(GearShort(0)), NoResponseFromGateway(), delivered=False)
    # The undelivered frame advanced nothing; the next delivered read still
    # lands on offset 3.
    assert cache.plan([DTR1(0), DTR0(3), ReadMemoryLocation(GearShort(0))]) is None


class _DeadGateway:
    """A transport whose module never reports a transmission."""

    async def read_input(self, _device_id, _address, count):
        return [0] * count

    async def write_holding(self, _device_id, _address, _values):
        return None


@pytest.mark.asyncio
async def test_a_batch_of_memoizable_reads_survives_a_dead_gateway():
    config = WBDALIConfig(device_name="wb-mdali_1", bus=1)
    logger = logging.getLogger("test.memo")
    driver = WBDALIDriver(
        config, None, logger, link=RegisterLink(config, _DeadGateway(), logger), memory=MemoryCache()
    )
    driver.response_timeout = 0.05
    await driver.initialize()
    try:
        responses = await driver.send_commands(
            [DTR0(7), QuerySceneLevel(GearShort(0), 7)], source=BusTrafficSource.WB
        )
        # Every command gets an error *response* — the batch degrades, it does
        # not raise out of send_commands.
        assert all(isinstance(r, NoResponseFromGateway) for r in responses)
    finally:
        await driver.deinitialize()
