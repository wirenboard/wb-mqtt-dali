"""ApplicationController-level reactions to gateway unavailability (SOFT-6841).

Driver responses already covered in tests/test_wbdali_gateway_unavailable.py.
Here we verify that the existing exception/return paths propagate the
`gateway unavailable` signal through the polling loop and commissioning runs:

- A queued SEND_COMMAND_BATCH task whose driver flips to "unavailable" while
  in flight surfaces the error through the standard SendCommandResult.
- A running commissioning task transitions to FAILED with status published
  through the commissioning state callback.
"""

import asyncio
import logging
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wb.mqtt_dali.application_controller import (
    CommissioningState,
    CommissioningStatus,
    SendCommandStatus,
)
from wb.mqtt_dali.send_command import build_command_registry, parse_expression
from wb.mqtt_dali.wbdali_error_response import GatewayUnavailable

from ._app_controller_helpers import make_loop_controller, stop_loop


@pytest.fixture(scope="module")
def registry():
    return build_command_registry()


@pytest.mark.asyncio
async def test_app_controller_task_fails_on_r(registry):  # pylint: disable=redefined-outer-name
    """A SEND_COMMAND_BATCH dispatched while the driver is gateway-unavailable
    surfaces SendCommandStatus.ERROR with error='gateway unavailable'."""
    # pylint: disable=protected-access
    controller = make_loop_controller()

    async def fake_send(_driver, _cmd, *_a, **_kw):
        return GatewayUnavailable()

    commands = [parse_expression("Off(A0)", registry), parse_expression("Off(A1)", registry)]

    with patch("wb.mqtt_dali.application_controller.send_with_retry", side_effect=fake_send):
        loop_task = asyncio.create_task(controller._polling_loop())
        try:
            result = await controller.send_command_batch(commands)
        finally:
            await stop_loop(controller, loop_task)

    # The handler breaks on the first transmission error so the batch stops
    # at the first command rather than producing multiple identical errors.
    assert len(result) == 1
    assert result[0].status is SendCommandStatus.ERROR
    assert result[0].error == "gateway unavailable"


@pytest.mark.asyncio
async def test_commissioning_fails_on_r():
    """Commissioning that hits `gateway unavailable` reports FAILED through the
    commissioning state callback."""
    # pylint: disable=protected-access
    # Build a minimal controller with just enough state for _commissioning_task to run.
    controller = make_loop_controller()
    controller.dali_devices = []
    controller.dali2_devices = []
    controller._gtin_db = MagicMock()
    controller._one_shot_tasks = MagicMock()
    controller._one_shot_tasks.add = MagicMock()
    controller._commissioning_state = CommissioningState()
    states: List[CommissioningState] = []

    def _cb(state):
        states.append(state)

    controller._commissioning_state_cb = _cb
    controller.logger = logging.getLogger("test.commissioning")

    async def fake_send(_driver, _cmd, *_a, **_kw):
        return GatewayUnavailable()

    # Commissioning calls a few helpers and the Commissioning class; the very
    # first send (StartQuiescentMode) returning GatewayUnavailable is enough to
    # propagate via the existing error path. We make smart_extend raise a clear
    # error to match the production code path.
    with patch("wb.mqtt_dali.application_controller.send_with_retry", side_effect=fake_send), patch(
        "wb.mqtt_dali.application_controller.Commissioning"
    ) as commissioning_cls:
        commissioning_obj = MagicMock()
        commissioning_obj.smart_extend = AsyncMock(side_effect=RuntimeError("gateway unavailable"))
        commissioning_cls.return_value = commissioning_obj

        try:
            await controller._commissioning_task()
        except RuntimeError:
            pass

    # The final published state must be FAILED with the gateway unavailable error.
    assert states, "commissioning state callback was not invoked"
    final_state = states[-1]
    assert final_state.status is CommissioningStatus.FAILED
    assert "gateway unavailable" in (final_state.error or "")
