"""Writing a short address through `INITIALISE`: some devices ignore `SET SHORT ADDRESS`.

Answers are read until two agree: a DALI backward frame is one byte with no checksum, so a
corrupted answer decodes into a valid-looking wrong value, and a wrong random address selects
nobody inside `INITIALISE`.
"""

import logging
from typing import Callable, Generator, Optional, Union

from dali.command import Command, Response
from dali.sequences import sleep as seq_sleep

from .dali2_compat import Dali2CommandsCompatibilityLayer
from .dali_compat import DaliCommandsCompatibilityLayer
from .wbdali_utils import (
    FLASH_WRITE_TIME_S,
    MAX_COMMAND_RETRIES,
    check_command_failed,
    has_transmission_error,
)

DaliCommands = Union[DaliCommandsCompatibilityLayer, Dali2CommandsCompatibilityLayer]
Yielded = Union[Command, list[Command], seq_sleep]
Received = Union[Response, list[Response]]

# Two answers have to agree before a value is acted on; the third read breaks a tie.
MAX_READ_ATTEMPTS = 3


def set_short_address_sequence(
    cmds: DaliCommands, current_short: int, new_short: int, logger: logging.Logger
) -> Generator[Yielded, Received, Optional[int]]:
    """Program `new_short` (`MASK` clears it) and return the address the device then reports.

    `None` means nothing usable answered.
    """
    random_address = yield from _read_random_address(cmds, current_short, logger)
    if random_address is None:
        logger.warning(
            "Short address %d: no usable random address, nothing to select the device with; not writing",
            current_short,
        )
        return None

    yield from _send_checked(cmds.Terminate())
    try:
        yield from _send_checked(cmds.Initialise(current_short))
        yield from _send_checked(
            cmds.SetSearchAddrH((random_address >> 16) & 0xFF),
            cmds.SetSearchAddrM((random_address >> 8) & 0xFF),
            cmds.SetSearchAddrL(random_address & 0xFF),
            cmds.ProgramShortAddress(new_short),
        )
        yield seq_sleep(FLASH_WRITE_TIME_S)
        reported = yield from _read_short_address(cmds, logger)
    except Exception:
        yield cmds.Terminate()
        raise

    yield cmds.Terminate()
    return reported


def _read_random_address(
    cmds: DaliCommands, short: int, logger: logging.Logger
) -> Generator[Yielded, Received, Optional[int]]:
    values = yield from _read_agreeing(
        [
            cmds.QueryRandomAddressH(short),
            cmds.QueryRandomAddressM(short),
            cmds.QueryRandomAddressL(short),
        ],
        cmds.QueryRandomAddressResponseValue,
        logger,
    )
    if values is None:
        return None
    return (values[0] << 16) | (values[1] << 8) | values[2]


def _read_short_address(
    cmds: DaliCommands, logger: logging.Logger
) -> Generator[Yielded, Received, Optional[int]]:
    values = yield from _read_agreeing(
        [cmds.QueryShortAddress()], cmds.QueryShortAddressResponseValue, logger
    )
    return None if values is None else values[0]


def _read_agreeing(
    commands: list[Command],
    decode: Callable[[Response], Optional[int]],
    logger: logging.Logger,
) -> Generator[Yielded, Received, Optional[list[int]]]:
    """Read `commands` as one batch until two attempts decode to the same values.

    `None` when no two of `MAX_READ_ATTEMPTS` agreed. An unreadable answer is raised, not reported
    as absence: a device answering with a framing error is present. Frames the gateway itself
    refused are not reads of the device — `_send_checked` retries those.
    """
    seen: list[list[int]] = []
    unreadable: Optional[str] = None
    for _ in range(MAX_READ_ATTEMPTS):
        responses = yield from _send_checked(*commands)
        failures = [check_command_failed(cmd, resp) for cmd, resp in zip(commands, responses)]
        if any(failure is not None for failure in failures):
            for cmd, resp, failure in zip(commands, responses, failures):
                if failure is not None and resp is not None and resp.raw_value is not None:
                    unreadable = f"Unreadable answer to {cmd}: {failure}"
            continue
        values = [decode(resp) for resp in responses]
        if any(value is None for value in values):
            continue
        if values in seen:
            return values
        seen.append(values)

    if unreadable is not None:
        raise RuntimeError(unreadable)
    if seen:
        logger.warning(
            "%s answered %s in %d reads without two answers agreeing",
            [str(cmd) for cmd in commands],
            seen,
            MAX_READ_ATTEMPTS,
        )
    return None


def _send_checked(*commands: Command) -> Generator[list[Command], list[Response], list[Response]]:
    responses = []
    for _ in range(MAX_COMMAND_RETRIES):
        responses = yield list(commands)
        if not has_transmission_error(responses):
            return responses
    raise RuntimeError(
        f"Gateway did not accept {[str(cmd) for cmd in commands]}: {[str(resp) for resp in responses]}"
    )
