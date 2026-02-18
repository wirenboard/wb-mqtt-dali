from __future__ import annotations

import asyncio
import logging
from typing import Iterable, Optional, Union

from dali.address import DeviceBroadcast, DeviceShort, InstanceNumber
from dali.command import Command, Response
from dali.device.general import (
    QueryDeviceStatus,
    QueryDeviceStatusResponse,
    QueryInstanceEnabled,
    QueryInstanceType,
    QueryNumberOfInstances,
    StartQuiescentMode,
    StopQuiescentMode,
)
from dali.device.helpers import DeviceInstanceTypeMapper, check_bad_rsp

from .wbdali import WBDALIConfig as WBDALIDriverNewConfig
from .wbdali import WBDALIDriver as WBDALIDriverNew
from .wbmdali import WBDALIConfig as WBDALIDriverOldConfig
from .wbmdali import WBDALIDriver as WBDALIDriverOld

MASK = 0xFF

WBDALIDriver = Union[WBDALIDriverNew, WBDALIDriverOld]
WBDALIConfig = Union[WBDALIDriverNewConfig, WBDALIDriverOldConfig]


class AsyncDeviceInstanceTypeMapper(DeviceInstanceTypeMapper):
    """A version of DeviceInstanceTypeMapper taking advantage of
    sending of multiple DALI commands in parallel
    """

    async def async_autodiscover(
        self, driver, addresses: int | tuple[int, int] | Iterable[int] = (0, 63)
    ) -> None:
        """
        An async function to scan a DALI bus for control device instances,
        and query their types. Internally it uses asyncio.gather to wait for
        completion of multiple DALI commands in parallel.
        This information is stored within this AsyncDeviceInstanceTypeMapper,
        for use in decoding "Device/Instance" event messages.

        :param driver: The async DALI driver to use for sending commands.
        :param addresses: Optional specifier of which addresses to scan. Can
        either be a single int, in which case all addresses from zero to that
        value will be scanned; or can be a tuple in the form (start, end), in
        which case all addresses between the provided values will be scanned;
        or finally can be an iterable of ints in which case each address, in
        the iterator will be scanned.

        Needs to be used through an appropriate driver for example:
        ```
        dev_inst_map = AsyncDeviceInstanceTypeMapper()
        await dev_inst_map.async_autodiscover(driver)
        ```
        instead of
        ```
        await driver.run_sequence(dev_inst_map.autodiscover())
        ```

        """
        logging.debug("Starting autodiscover with addresses: %s", addresses)

        if isinstance(addresses, int):
            addresses = list(range(0, addresses + 1))
        elif isinstance(addresses, tuple) and len(addresses) == 2:
            addresses = list(range(addresses[0], addresses[1] + 1))

        # Use quiescent mode to reduce bus contention from input devices
        await driver.send(StartQuiescentMode(DeviceBroadcast()))
        responses = await asyncio.gather(
            *[driver.send(QueryDeviceStatus(device=DeviceShort(addr_int))) for addr_int in addresses],
        )

        queries = []
        logging.debug("QueryDeviceStatus responses: %s", zip(addresses, responses))
        for addr_int, rsp in zip(addresses, responses):
            addr = DeviceShort(addr_int)
            if check_bad_rsp(rsp):
                continue
            if isinstance(rsp, QueryDeviceStatusResponse):
                # Make sure the status is OK
                if rsp.short_address_is_mask or rsp.reset_state:
                    continue
            else:
                # If the response isn't QueryDeviceStatusResponse then
                # something is wrong
                continue

            # Find out how many instances the device has
            queries.append(QueryNumberOfInstances(device=addr))

        responses = await asyncio.gather(*[driver.send(q) for q in queries])
        enabled_queries = []
        type_queries = []
        for query, rsp in zip(queries, responses):
            addr = query.destination

            if check_bad_rsp(rsp):
                continue
            num_inst = rsp.value

            # For each instance, check it is enabled and then query the type
            for inst_int in range(num_inst):
                inst = InstanceNumber(inst_int)

                enabled_queries.append(QueryInstanceEnabled(device=addr, instance=inst))
                type_queries.append(QueryInstanceType(device=addr, instance=inst))

        responses = await asyncio.gather(
            *[driver.send(q) for q in enabled_queries],
            *[driver.send(q) for q in type_queries],
        )

        enabled_responses = responses[: len(enabled_queries)]
        type_responses = responses[len(enabled_queries) :]

        for query, enabled_rsp, type_rsp in zip(enabled_queries, enabled_responses, type_responses):
            addr = query.destination
            inst = query.instance
            if check_bad_rsp(enabled_rsp):
                continue
            if not enabled_rsp.value:
                # Skip if not enabled
                continue

            if check_bad_rsp(type_rsp):
                continue

            logging.debug("message=A²%d I%d type: %s", addr.address, inst.value, type_rsp.value)

            # Add the type to the device/instance map
            self.add_type(
                short_address=addr,
                instance_number=inst,
                instance_type=type_rsp.value,
            )
        await driver.send(StopQuiescentMode(DeviceBroadcast()))

    def update_mapping(self, short_address: int, new_short_address: int) -> None:
        """Update the mapping for a device that has changed short address."""
        if short_address in self._mapping:
            self._mapping[new_short_address] = self._mapping.pop(short_address)


async def query_request(driver: WBDALIDriver, cmd: Command) -> int:
    resp = await driver.send(cmd)
    try:
        check_query_response(resp)
    except Exception as e:
        raise RuntimeError(f"Error in response for {cmd}: {e}") from e
    return resp.raw_value.as_integer


def check_query_response(resp: Optional[Response]) -> None:
    if resp is None:
        raise RuntimeError("no response")
    raw_value = resp.raw_value
    if raw_value is None:
        raise RuntimeError("no response")
    if raw_value.error:
        raise RuntimeError("framing error")
