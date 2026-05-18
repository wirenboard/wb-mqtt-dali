"""Shared helpers for ApplicationController-loop and Gateway tests.

Bypasses `__init__` for `ApplicationController` to avoid bringing up MQTT/driver,
then wires up the minimal real state that `_polling_loop` and friends touch.
Centralized so the existing grandfathered private-attribute setup is not
duplicated across test modules. `make_bare_gateway` uses the normal `Gateway`
constructor since it does no I/O.
"""

import asyncio
import logging
from typing import Dict, Optional
from unittest.mock import AsyncMock, MagicMock

from wb.mqtt_dali.application_controller import (
    ApplicationController,
    ApplicationControllerState,
    CommissioningState,
    PollScheduler,
)
from wb.mqtt_dali.gateway import Gateway
from wb.mqtt_dali.send_command import CommandInfo, build_command_registry


def make_loop_controller(polling_interval: float = 1.0) -> ApplicationController:
    # pylint: disable=protected-access
    controller = ApplicationController.__new__(ApplicationController)
    controller.uid = "gw_bus_1"
    controller.logger = logging.getLogger("test.loop")
    controller._dev = AsyncMock()
    controller._state = ApplicationControllerState.READY
    controller._state_lock = asyncio.Lock()
    controller._tasks_queue = asyncio.Queue()
    controller._in_quiescent_mode = False
    controller._polling_interval = polling_interval
    controller._stop_requested = False
    controller.dali_devices = []
    controller.dali2_devices = []
    controller._init_scheduler = MagicMock(
        get_first_attempt_ready=MagicMock(return_value=[]),
        get_one_retry_ready=MagicMock(return_value=None),
    )
    controller._poll_scheduler = PollScheduler()
    controller._current_commissioning_task = None
    controller._commissioning_state = CommissioningState()
    controller._commissioning_state_cb = None
    return controller

def make_bare_gateway(
    command_registry: Optional[Dict[str, CommandInfo]] = None,
    config: Optional[dict] = None,
) -> Gateway:
    """Construct a Gateway via its normal constructor with mocked collaborators.

    The constructor does no I/O, so this is the preferred path to obtain a
    `Gateway` for handler-level tests; pass a custom `command_registry` to
    avoid the (non-trivial) reflective enumeration cost when the registry
    contents are not under test.
    """
    return Gateway(
        config=config if config is not None else {},
        mqtt_dispatcher=MagicMock(),
        config_path="",
        gtin_db=MagicMock(),
        command_registry=command_registry if command_registry is not None else build_command_registry(),
    )


async def stop_loop(controller: ApplicationController, task: asyncio.Task) -> None:
    # pylint: disable=protected-access
    """Mirror ApplicationController.stop()'s polling-task shutdown.

    Cancel alone is unreliable when the loop sits inside
    `gather(..., return_exceptions=True)` on Python 3.9 (see
    https://github.com/python/cpython/issues/76865), so production sets
    `_stop_requested` first and the loop exits at the next iteration even
    if the cancel is swallowed.
    """
    controller._stop_requested = True
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
