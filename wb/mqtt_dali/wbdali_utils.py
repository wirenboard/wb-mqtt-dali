from __future__ import annotations

import asyncio
import logging
from typing import Iterable, Optional, Sequence, Union

from dali.address import (
    Address,
    DeviceBroadcast,
    DeviceGroup,
    DeviceShort,
    GearBroadcast,
    GearGroup,
    InstanceNumber,
)
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

from .bus_traffic import BusTrafficSource
from .wbdali import WBDALIConfig as WBDALIDriverNewConfig
from .wbdali import WBDALIDriver as WBDALIDriverNew
from .wbdali_error_response import WbGatewayTransmissionError
from .wbmdali import WBDALIConfig as WBDALIDriverOldConfig
from .wbmdali import WBDALIDriver as WBDALIDriverOld

MASK = 0xFF

WBDALIDriver = Union[WBDALIDriverNew, WBDALIDriverOld]
WBDALIConfig = Union[WBDALIDriverNewConfig, WBDALIDriverOldConfig]

MAX_COMMAND_RETRIES = 3


class AsyncDeviceInstanceTypeMapper(DeviceInstanceTypeMapper):
    # pylint: disable=too-many-locals, too-many-branches
    """A version of DeviceInstanceTypeMapper taking advantage of
    sending of multiple DALI commands in parallel
    """

    async def async_autodiscover(
        self,
        driver,
        addresses: int | tuple[int, int] | Iterable[int] = (0, 63),
        logger: Optional[logging.Logger] = None,
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
        await send_with_retry(driver, StartQuiescentMode(DeviceBroadcast()), logger)
        responses = await asyncio.gather(
            *[
                send_with_retry(
                    driver,
                    QueryDeviceStatus(device=DeviceShort(addr_int)),
                    logger,
                )
                for addr_int in addresses
            ],
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

        responses = await asyncio.gather(*[send_with_retry(driver, q, logger) for q in queries])
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
            *[send_with_retry(driver, q, logger) for q in enabled_queries],
            *[send_with_retry(driver, q, logger) for q in type_queries],
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
        await send_with_retry(driver, StopQuiescentMode(DeviceBroadcast()), logger)

    def update_mapping(self, short_address: int, new_short_address: int) -> None:
        """Update the mapping for a device that has changed short address."""
        if short_address in self._mapping:
            self._mapping[new_short_address] = self._mapping.pop(short_address)


async def query_int(driver: WBDALIDriver, cmd: Command, logger: Optional[logging.Logger] = None) -> int:
    return (await query_response(driver, cmd, logger)).raw_value.as_integer


async def query_response(
    driver: WBDALIDriver,
    cmd: Command,
    logger: Optional[logging.Logger] = None,
) -> Response:
    last_error: Optional[str] = None
    for attempt in range(1, MAX_COMMAND_RETRIES + 1):
        resp = await driver.send(cmd)
        last_error = check_command_failed(cmd, resp)
        if last_error is not None:
            if logger is not None:
                logger.warning(
                    "DALI send retry %d/%d for %s: %s",
                    attempt,
                    MAX_COMMAND_RETRIES,
                    cmd,
                    resp,
                )
            continue
        return resp
    raise RuntimeError(f"Error in response for {cmd}: {last_error}")


async def query_responses(
    driver: WBDALIDriver,
    cmds: list[Command],
    logger: Optional[logging.Logger] = None,
) -> list[Response]:
    last_error: Optional[str] = None
    for attempt in range(1, MAX_COMMAND_RETRIES + 1):
        responses = await driver.send_commands(cmds, BusTrafficSource.WB)
        for command, resp in zip(cmds, responses):
            last_error = check_command_failed(command, resp)
            if last_error is not None:
                if logger is not None:
                    logger.warning(
                        "DALI send retry %d/%d for %s: %s",
                        attempt,
                        MAX_COMMAND_RETRIES,
                        cmds,
                        resp,
                    )
                break
        if last_error is None:
            return responses
    raise RuntimeError(f"Error in response for {cmds}: {last_error}")


def check_query_response(resp: Optional[Response]) -> None:
    if resp is None:
        raise RuntimeError("no response")
    raw_value = resp.raw_value
    error_acceptable = getattr(resp, "_error_acceptable", False)
    if raw_value is None and not error_acceptable:
        raise RuntimeError("no response")
    if raw_value is not None and raw_value.error and not error_acceptable:
        raise RuntimeError("framing error")


def is_broadcast_or_group_address(addr: Address) -> bool:
    return isinstance(addr, (DeviceBroadcast, GearGroup, GearBroadcast, DeviceGroup))


def is_transmission_error_response(resp: Optional[Response]) -> bool:
    return isinstance(resp, WbGatewayTransmissionError)


def has_transmission_error(responses: Sequence[Optional[Response]]) -> bool:
    return any(is_transmission_error_response(resp) for resp in responses)


def check_command_failed(cmd: Command, resp: Optional[Response]) -> Optional[str]:
    if is_transmission_error_response(resp):
        return str(resp)
    if getattr(cmd, "response", None) is not None:
        try:
            check_query_response(resp)
            return None
        except RuntimeError as exc:
            return str(exc)
    return None


async def query_responses_retry_from_first_failed(  # pylint: disable=too-many-locals
    driver: WBDALIDriver,
    commands: Sequence[Command],
    batch_size: int = 1,
    logger: Optional[logging.Logger] = None,
    source=BusTrafficSource.WB,
) -> list[Optional[Response]]:
    if not commands:
        return []

    if batch_size <= 0:
        raise ValueError("query_responses_retry_from_first_failed batch_size must be a positive integer")

    total_count = len(commands)
    full_responses: list[Optional[Response]] = [None] * total_count
    pending_commands = list(commands)
    pending_indexes = list(range(total_count))
    last_failed_index = -1
    last_failed_command: Optional[Command] = None
    last_failed_reason = "unknown error"

    for attempt in range(1, MAX_COMMAND_RETRIES + 1):
        responses = await driver.send_commands(pending_commands, source)

        first_failed_pos: Optional[int] = None
        for pos, (index, cmd, resp) in enumerate(zip(pending_indexes, pending_commands, responses)):
            fail_reason = check_command_failed(cmd, resp)
            if fail_reason is None:
                full_responses[index] = resp
                continue

            first_failed_pos = pos
            last_failed_index = index
            last_failed_command = cmd
            last_failed_reason = fail_reason
            break

        if first_failed_pos is None:
            return list(full_responses)

        first_failed_pos = first_failed_pos - (first_failed_pos % batch_size)

        pending_commands = pending_commands[first_failed_pos:]
        pending_indexes = pending_indexes[first_failed_pos:]

        if logger is not None:
            logger.warning(
                "DALI batch retry-from-first %d/%d: failed at index %d (%s): %s",
                attempt,
                MAX_COMMAND_RETRIES,
                last_failed_index,
                last_failed_command,
                last_failed_reason,
            )

    raise RuntimeError(
        "DALI batch retry-from-first failed after "
        f"{MAX_COMMAND_RETRIES} attempts at index {last_failed_index} "
        f"({last_failed_command}): {last_failed_reason}"
    )


async def query_responses_retry_only_failed(  # pylint: disable=too-many-locals
    driver: WBDALIDriver,
    commands: Sequence[Command],
    logger: Optional[logging.Logger] = None,
    source=BusTrafficSource.WB,
) -> list[Optional[Response]]:
    if not commands:
        return []

    total_count = len(commands)
    full_responses: list[Optional[Response]] = [None] * total_count
    pending_indexes = list(range(total_count))
    pending_commands = list(commands)
    last_failed_reasons: dict[int, str] = {}

    for attempt in range(1, MAX_COMMAND_RETRIES + 1):
        responses = await driver.send_commands(pending_commands, source)

        next_pending_indexes: list[int] = []
        next_pending_commands: list[Command] = []
        failed_info: list[tuple[int, str]] = []

        for index, cmd, resp in zip(pending_indexes, pending_commands, responses):
            fail_reason = check_command_failed(cmd, resp)
            if fail_reason is None:
                full_responses[index] = resp
                continue
            next_pending_indexes.append(index)
            next_pending_commands.append(cmd)
            failed_info.append((index, fail_reason))

        if not next_pending_indexes:
            return list(full_responses)

        last_failed_reasons = dict(failed_info)
        pending_indexes = next_pending_indexes
        pending_commands = next_pending_commands

        if logger is not None:
            logger.warning(
                "DALI batch retry-only-failed %d/%d: pending indexes %s",
                attempt,
                MAX_COMMAND_RETRIES,
                pending_indexes,
            )

    raise RuntimeError(
        "DALI batch retry-only-failed failed after "
        f"{MAX_COMMAND_RETRIES} attempts for indexes {pending_indexes}: {last_failed_reasons}"
    )


async def send_with_retry(
    driver: WBDALIDriver,
    cmd: Command,
    logger: Optional[logging.Logger] = None,
    source=BusTrafficSource.WB,
) -> Response:
    response: Response = Response(None)
    for attempt in range(1, MAX_COMMAND_RETRIES + 1):
        response = await driver.send(cmd, source)
        if not is_transmission_error_response(response):
            return response
        if logger is not None:
            logger.warning("DALI send retry %d/%d for %s: %s", attempt, MAX_COMMAND_RETRIES, cmd, response)
    return response


async def send_commands_with_retry(
    driver: WBDALIDriver,
    commands: Sequence[Command],
    logger: Optional[logging.Logger] = None,
    source=BusTrafficSource.WB,
) -> list[Response]:
    responses: list[Response] = []
    for attempt in range(1, MAX_COMMAND_RETRIES + 1):
        responses = await driver.send_commands(commands, source)
        if not has_transmission_error(responses):
            return responses
        if logger is not None:
            logger.warning(
                "DALI send_commands retry %d/%d failed",
                attempt,
                MAX_COMMAND_RETRIES,
            )
    return responses
