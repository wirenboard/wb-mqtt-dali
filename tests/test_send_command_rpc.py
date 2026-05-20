# pylint: disable=duplicate-code  # shared gateway.start() boilerplate with tests/test_reset_device.py
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dali.command import Response
from dali.frame import BackwardFrame, BackwardFrameError
from dali.gear.general import Off

from wb.mqtt_dali.application_controller import (
    ApplicationControllerState,
    ApplicationControllerTask,
    ApplicationControllerTaskType,
    SendCommandResponse,
    SendCommandResult,
    SendCommandStatus,
)
from wb.mqtt_dali.send_command import build_command_registry, parse_expression
from wb.mqtt_dali.wbdali_error_response import NoResponseFromGateway

from ._app_controller_helpers import make_bare_gateway, make_loop_controller, stop_loop

# `registry` is a module-scope pytest fixture; tests receive it as a parameter,
# which pylint sees as redefining the fixture's outer name.
# pylint: disable=redefined-outer-name


@pytest.fixture(scope="module")
def registry():
    return build_command_registry()


# ---------------------------------------------------------------------------
# Bus/ListCommands
# ---------------------------------------------------------------------------


class TestListCommandsRpc:
    @pytest.mark.asyncio
    async def test_list_commands_rpc_contains_known_commands(self, registry):
        """Catalog is 1-to-1 with the registry: feature commands are registered
        once with `instance_mode=OPTIONAL`, and each registry entry produces
        exactly one catalog row with only `name` / `category` / `snippet`.
        """
        gateway = _make_bare_gateway(registry=registry)

        catalog = await gateway.list_commands_rpc_handler({})

        names = [entry["name"] for entry in catalog]
        assert len(names) == len(set(names)), "catalog has duplicate command names"

        by_name = {entry["name"]: entry for entry in catalog}
        expected_names = [
            "Off",
            "DAPC",
            "DT8.Activate",
            "FF24.QueryDeviceStatus",
            "FF24.EnableInstance",
            "FF24.F32.QueryFeedbackCapability",
            "FF24.F32.QueryFeedbackActive",
            "DTR0",
            "Terminate",
            "FF24.Terminate",
        ]
        for name in expected_names:
            assert name in by_name, f"missing {name} in catalog"
            entry = by_name[name]
            assert set(entry.keys()) == {"name", "category", "snippet"}

        # EnableInstance lives under "FF24 Device General" (it's a general
        # device command, not feature- or instance-type-specific).
        assert by_name["FF24.EnableInstance"]["category"] == "FF24 Device General"
        assert by_name["DT8.Activate"]["category"] == "DT8 Colour Control"

        # Regression guard: if anyone ever reintroduces an `Ix.`-shaped key in
        # the registry, this test catches it.
        assert not any(".Ix." in name for name in names)

    @pytest.mark.asyncio
    async def test_list_commands_rpc_snippet_shapes(self, registry):
        """Snippets match the canonical form from the plan:
        `FF24.EnableInstance(${1:A0}, ${2:I0})` etc. Feature commands
        (`instance_mode=OPTIONAL`) expose the `I<n>` placeholder in the
        snippet; the parser accepts both with and without it.
        """
        gateway = _make_bare_gateway(registry=registry)

        catalog = await gateway.list_commands_rpc_handler({})
        by_name = {entry["name"]: entry for entry in catalog}

        assert by_name["Off"]["snippet"] == "Off(${1:A0})"
        assert by_name["DAPC"]["snippet"] == "DAPC(${1:A0}, ${2:level})"
        assert by_name["Terminate"]["snippet"] == "Terminate"
        assert by_name["DTR0"]["snippet"] == "DTR0(${1:data})"
        assert by_name["FF24.EnableInstance"]["snippet"] == "FF24.EnableInstance(${1:A0}, ${2:I0})"
        assert (
            by_name["FF24.F32.QueryFeedbackActive"]["snippet"]
            == "FF24.F32.QueryFeedbackActive(${1:A0}, ${2:I0})"
        )

    @pytest.mark.asyncio
    async def test_list_commands_sorts_dt_numerically(self, registry):
        """DT-gear categories must be ordered numerically (DT4 before DT16), not
        lexicographically — the CLI and RPC share the same ordering.
        """
        gateway = _make_bare_gateway(registry=registry)
        catalog = await gateway.list_commands_rpc_handler({})

        # Collect the order in which DT-gear categories first appear. The
        # `DT<digits> <label>` shape filters out `FF24.DT<n>` instance-type
        # categories and `Gear General`/`Gear Special`.
        seen: list = []
        for entry in catalog:
            cat = entry["category"]
            head = cat.split(" ", 1)[0]
            if head.startswith("DT") and head[2:].isdigit() and cat not in seen:
                seen.append(cat)

        dt_numbers = [int(c.split(" ", 1)[0][2:]) for c in seen]
        assert dt_numbers == sorted(dt_numbers), f"DT categories not in numeric order: {seen}"
        # Concrete trip-wire against lexicographic regression (which would put DT16 before DT4).
        dt4_idx = next(i for i, c in enumerate(seen) if c.startswith("DT4 "))
        dt16_idx = next(i for i, c in enumerate(seen) if c.startswith("DT16 "))
        assert dt4_idx < dt16_idx


# ---------------------------------------------------------------------------
# ApplicationController.send_command_batch (queue + handler)
# ---------------------------------------------------------------------------


def patch_send_with_retry(fake):
    """Return a context manager that replaces application_controller.send_with_retry."""
    return patch("wb.mqtt_dali.application_controller.send_with_retry", side_effect=fake)


# ---------------------------------------------------------------------------
# Polling-loop integration: batch atomicity, queue ordering, dispatch
# ---------------------------------------------------------------------------


class TestSendCommandBatchPollingLoop:
    @pytest.mark.asyncio
    async def test_batch_atomic_against_polling(self, registry):
        """No poll step is taken between commands of a batch (only between bursts)."""
        controller = make_loop_controller()
        controller._polling_interval = 0.01  # pylint: disable=protected-access

        sent = []

        async def fake_send(_drv, command, *_a, **_kw):
            await asyncio.sleep(0.01)
            sent.append(command)
            return Response(BackwardFrame(0))

        commands = [parse_expression(f"DAPC(A{i % 64}, 1)", registry) for i in range(4)]

        with patch_send_with_retry(fake_send):
            loop_task = asyncio.create_task(controller._polling_loop())  # pylint: disable=protected-access
            try:
                result = await controller.send_command_batch(commands)
            finally:
                await stop_loop(controller, loop_task)

        assert len(result) == 4
        # The sent order matches the requested order; no other DALI command
        # (e.g. a poll) appears between them.
        assert sent == commands

    @pytest.mark.asyncio
    async def test_batch_atomic_against_control_topic(self, registry):
        """EXECUTE_CONTROL queued during a running batch waits for the batch."""
        controller = make_loop_controller()
        controller._polling_interval = 100.0  # pylint: disable=protected-access

        commands = [parse_expression(f"DAPC(A{i}, 1)", registry) for i in range(2)]
        sent_during_batch: list = []
        order: list = []
        send_release = asyncio.Event()

        async def fake_send(_drv, command, *_a, **_kw):
            sent_during_batch.append(command)
            # First send blocks until the test releases it; this lets us
            # queue EXECUTE_CONTROL while the batch holds the loop.
            if len(sent_during_batch) == 1:
                await send_release.wait()
            # Record batch completion at the last command, inside the polling
            # loop's processing of SEND_COMMAND_BATCH — observing it from the
            # test coroutine after `await batch_task` races with the loop
            # picking up the next queued task.
            if len(sent_during_batch) == len(commands):
                order.append("batch")
            return Response(BackwardFrame(0))

        device = MagicMock()

        async def fake_execute_control(*_a, **_kw):
            order.append("control")
            return None

        device.execute_control = AsyncMock(side_effect=fake_execute_control)

        with patch_send_with_retry(fake_send):
            loop_task = asyncio.create_task(controller._polling_loop())  # pylint: disable=protected-access
            try:
                batch_task = asyncio.create_task(controller.send_command_batch(commands))
                # Wait until the first send is mid-flight before queueing EXECUTE_CONTROL.
                for _ in range(50):
                    if sent_during_batch:
                        break
                    await asyncio.sleep(0.01)

                control = _make_execute_control_obj()
                exec_task = ApplicationControllerTask(
                    task_type=ApplicationControllerTaskType.EXECUTE_CONTROL,
                    data=(device, control),
                )
                controller._tasks_queue.put_nowait(exec_task)  # pylint: disable=protected-access

                # Give the loop a moment to (incorrectly) try to interleave.
                await asyncio.sleep(0.02)
                assert not exec_task.future.done()

                send_release.set()
                await batch_task
                await asyncio.wait_for(exec_task.future, timeout=1.0)
            finally:
                await stop_loop(controller, loop_task)

        # The batch completed before the EXECUTE_CONTROL handler ran.
        assert order == ["batch", "control"]

    @pytest.mark.asyncio
    async def test_parallel_calls_serialized(self, registry):
        """Two concurrent send_command_batch calls execute sequentially in arrival order."""
        controller = make_loop_controller()
        sent: list = []

        async def fake_send(_drv, command, *_a, **_kw):
            await asyncio.sleep(0.005)
            sent.append(command)
            return Response(BackwardFrame(0))

        cmd_a = [parse_expression("DAPC(A0, 1)", registry), parse_expression("DAPC(A0, 2)", registry)]
        cmd_b = [parse_expression("DAPC(A1, 1)", registry), parse_expression("DAPC(A1, 2)", registry)]

        with patch_send_with_retry(fake_send):
            loop_task = asyncio.create_task(controller._polling_loop())  # pylint: disable=protected-access
            try:
                task_a = asyncio.create_task(controller.send_command_batch(cmd_a))
                # Ensure task_a is queued before task_b.
                await asyncio.sleep(0)
                task_b = asyncio.create_task(controller.send_command_batch(cmd_b))
                await asyncio.gather(task_a, task_b)
            finally:
                await stop_loop(controller, loop_task)

        # All commands sent. Crucially, the two batches do not interleave:
        # cmd_a entries appear contiguously before any cmd_b entry.
        idx_a = [sent.index(c) for c in cmd_a]
        idx_b = [sent.index(c) for c in cmd_b]
        assert max(idx_a) < min(idx_b)

    @pytest.mark.asyncio
    async def test_query_response_surfaced(self, registry):
        """Query commands route the backward frame into result.response."""
        controller = make_loop_controller()
        cmd = parse_expression("QueryActualLevel(A7)", registry)

        async def fake_send(_drv, command, *_a, **_kw):
            return command.response(BackwardFrame(123))

        with patch_send_with_retry(fake_send):
            loop_task = asyncio.create_task(controller._polling_loop())  # pylint: disable=protected-access
            try:
                result = await controller.send_command_batch([cmd])
            finally:
                await stop_loop(controller, loop_task)

        assert len(result) == 1
        assert result[0].status is SendCommandStatus.OK
        assert result[0].response == SendCommandResponse(raw=123, value="123")

    @pytest.mark.asyncio
    async def test_query_timeout_is_error(self, registry):
        """A query without a backward frame is reported as an error, not as a null
        response: no response means the device did not answer, which python-dali
        surfaces as MissingResponse when decoding."""
        controller = make_loop_controller()
        cmd = parse_expression("QueryActualLevel(A7)", registry)

        async def fake_send(_drv, command, *_a, **_kw):
            return command.response(None)

        with patch_send_with_retry(fake_send):
            loop_task = asyncio.create_task(controller._polling_loop())  # pylint: disable=protected-access
            try:
                result = await controller.send_command_batch([cmd])
            finally:
                await stop_loop(controller, loop_task)

        assert len(result) == 1
        assert result[0].status is SendCommandStatus.ERROR
        assert result[0].response is None
        assert result[0].error

    @pytest.mark.asyncio
    async def test_query_framing_error_is_error(self, registry):
        """A backward frame with framing error is rejected by `check_query_response`
        and surfaces as `status="error"`; the batch continues with the next element.
        """
        controller = make_loop_controller()
        commands = [
            parse_expression("QueryActualLevel(A7)", registry),
            parse_expression("Off(A5)", registry),
        ]

        async def fake_send(_drv, command, *_a, **_kw):
            if command is commands[0]:
                return command.response(BackwardFrameError(0))
            return Response(BackwardFrame(0))

        with patch_send_with_retry(fake_send):
            loop_task = asyncio.create_task(controller._polling_loop())  # pylint: disable=protected-access
            try:
                result = await controller.send_command_batch(commands)
            finally:
                await stop_loop(controller, loop_task)

        assert len(result) == 2
        assert result[0].status is SendCommandStatus.ERROR
        assert result[0].response is None
        assert "framing" in result[0].error.lower()
        assert result[1].status is SendCommandStatus.OK

    @pytest.mark.asyncio
    async def test_batch_dt8_setup_and_activate(self, registry):
        """Typical DT8 setup: DTR0/1/2 → DT8.Activate. Commands reach the bus in
        order and the result list is all-ok with the same length.
        """
        controller = make_loop_controller()
        commands = [
            parse_expression("DTR0(0xFF)", registry),
            parse_expression("DTR1(0x00)", registry),
            parse_expression("DTR2(0x00)", registry),
            parse_expression("DT8.Activate", registry),
        ]
        sent: list = []

        async def fake_send(_drv, command, *_a, **_kw):
            sent.append(command)
            return Response(BackwardFrame(0))

        with patch_send_with_retry(fake_send):
            loop_task = asyncio.create_task(controller._polling_loop())  # pylint: disable=protected-access
            try:
                result = await controller.send_command_batch(commands)
            finally:
                await stop_loop(controller, loop_task)

        assert [r.status for r in result] == [SendCommandStatus.OK] * 4
        assert sent == commands

    @pytest.mark.asyncio
    async def test_batch_stops_on_transport_error(self, registry):
        """Transport error truncates the batch and surfaces a partial result list."""
        controller = make_loop_controller()
        commands = [
            parse_expression("DTR0(0xFF)", registry),
            parse_expression("DTR1(0x00)", registry),
            parse_expression("DTR2(0x00)", registry),
            parse_expression("DT8.Activate", registry),
        ]
        call_count = {"n": 0}

        async def fake_send(_drv, _cmd, *_a, **_kw):
            call_count["n"] += 1
            if call_count["n"] == 2:
                return NoResponseFromGateway()
            return Response(BackwardFrame(0))

        with patch_send_with_retry(fake_send):
            loop_task = asyncio.create_task(controller._polling_loop())  # pylint: disable=protected-access
            try:
                result = await controller.send_command_batch(commands)
            finally:
                await stop_loop(controller, loop_task)

        assert len(result) == 2
        assert result[0].status is SendCommandStatus.OK
        assert result[1].status is SendCommandStatus.ERROR
        assert "no response" in result[1].error.lower()


def _make_execute_control_obj():
    control = MagicMock()
    control.control_info.id = "ctrl"
    control.control_info.value = None
    control.value_to_set = "1"
    return control


# ---------------------------------------------------------------------------
# Bus/SendCommand RPC handler
# ---------------------------------------------------------------------------


def _make_bare_gateway(buses=None, registry=None):
    """Construct a Gateway via the helper, then overwrite `wb_dali_gateways`
    with a single stub gateway carrying the given buses.
    """
    gateway = make_bare_gateway(command_registry=registry)
    gw = SimpleNamespace(uid="gw1", buses=buses or [])
    gateway.wb_dali_gateways = [gw]
    return gateway


class TestSendCommandRpcHandler:
    @pytest.mark.asyncio
    async def test_single_ok(self):
        bus = SimpleNamespace(
            uid="gw1_bus_1",
            send_command_batch=AsyncMock(return_value=[SendCommandResult(status=SendCommandStatus.OK)]),
        )
        gateway = _make_bare_gateway([bus])

        result = await gateway.send_command_rpc_handler({"busId": "gw1_bus_1", "commands": ["Off(A5)"]})

        bus.send_command_batch.assert_awaited_once()
        sent_commands = bus.send_command_batch.await_args.args[0]
        assert isinstance(sent_commands[0], Off)
        assert result == [{"status": "ok"}]

    @pytest.mark.asyncio
    async def test_query_returns_response(self):
        bus = SimpleNamespace(
            uid="gw1_bus_1",
            send_command_batch=AsyncMock(
                return_value=[
                    SendCommandResult(
                        status=SendCommandStatus.OK,
                        response=SendCommandResponse(raw=42, value="42"),
                    )
                ]
            ),
        )
        gateway = _make_bare_gateway([bus])

        result = await gateway.send_command_rpc_handler(
            {"busId": "gw1_bus_1", "commands": ["QueryActualLevel(A7)"]}
        )

        assert result == [{"status": "ok", "response": {"raw": 42, "value": "42"}}]

    @pytest.mark.asyncio
    async def test_query_timeout_is_error(self):
        bus = SimpleNamespace(
            uid="gw1_bus_1",
            send_command_batch=AsyncMock(
                return_value=[
                    SendCommandResult(
                        status=SendCommandStatus.ERROR,
                        error="no response",
                    )
                ]
            ),
        )
        gateway = _make_bare_gateway([bus])

        result = await gateway.send_command_rpc_handler(
            {"busId": "gw1_bus_1", "commands": ["QueryActualLevel(A7)"]}
        )

        assert result == [{"status": "error", "error": "no response"}]

    @pytest.mark.asyncio
    async def test_batch_truncated_on_transport_error(self):
        bus = SimpleNamespace(
            uid="gw1_bus_1",
            send_command_batch=AsyncMock(
                return_value=[
                    SendCommandResult(status=SendCommandStatus.OK),
                    SendCommandResult(status=SendCommandStatus.ERROR, error="no response from gateway"),
                ]
            ),
        )
        gateway = _make_bare_gateway([bus])

        result = await gateway.send_command_rpc_handler(
            {"busId": "gw1_bus_1", "commands": ["DTR0(1)", "DTR0(2)", "DTR0(3)", "DT8.Activate"]}
        )

        assert len(result) == 2
        assert result[0] == {"status": "ok"}
        assert result[1]["status"] == "error"
        assert result[1]["error"] == "no response from gateway"

    @pytest.mark.asyncio
    async def test_empty_commands(self):
        gateway = _make_bare_gateway()
        with pytest.raises(ValueError, match="non-empty list"):
            await gateway.send_command_rpc_handler({"busId": "gw1_bus_1", "commands": []})

    @pytest.mark.asyncio
    async def test_bus_not_found(self):
        gateway = _make_bare_gateway()
        with pytest.raises(ValueError, match="not found"):
            await gateway.send_command_rpc_handler({"busId": "missing", "commands": ["Off(A5)"]})

    @pytest.mark.asyncio
    async def test_bus_not_initialized(self):
        """ApplicationController in non-READY state surfaces RuntimeError via send_command_batch."""

        async def bad_batch(_commands):
            raise RuntimeError("ApplicationController must be initialized")

        bus = SimpleNamespace(uid="gw1_bus_1", send_command_batch=bad_batch)
        gateway = _make_bare_gateway([bus])

        with pytest.raises(RuntimeError, match="initialized"):
            await gateway.send_command_rpc_handler({"busId": "gw1_bus_1", "commands": ["Off(A5)"]})

    @pytest.mark.asyncio
    async def test_bus_quiescent_mode_rejects(self):
        """External quiescent mode rejects the task at dequeue via the
        polling-loop contract (RPC handler surfaces the RuntimeError)."""

        async def quiescent_batch(_commands):
            raise RuntimeError("Cannot execute tasks while in quiescent mode")

        bus = SimpleNamespace(uid="gw1_bus_1", send_command_batch=quiescent_batch)
        gateway = _make_bare_gateway([bus])

        with pytest.raises(RuntimeError, match="quiescent"):
            await gateway.send_command_rpc_handler({"busId": "gw1_bus_1", "commands": ["Off(A5)"]})

    @pytest.mark.asyncio
    async def test_invalid_expression_rejects_batch(self):
        """Validation errors on any one expression abort the whole batch."""
        bus = SimpleNamespace(uid="gw1_bus_1", send_command_batch=AsyncMock())
        gateway = _make_bare_gateway([bus])

        with pytest.raises(ValueError):
            await gateway.send_command_rpc_handler(
                {"busId": "gw1_bus_1", "commands": ["Off(A5)", "WhatIsThis(A5)"]}
            )
        bus.send_command_batch.assert_not_called()


# ---------------------------------------------------------------------------
# Endpoints registered on Gateway.start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bus_endpoints_registered_on_start():
    """Bus/SendCommand and Bus/ListCommands are wired up by Gateway.start()."""
    gateway = _make_bare_gateway()
    gateway.rpc_server = MagicMock()
    gateway.rpc_server.start = AsyncMock()
    gateway.rpc_server.add_endpoint = AsyncMock()
    gateway.wb_dali_gateways = []
    gateway._update_gateways = AsyncMock()  # pylint: disable=protected-access

    with patch("wb.mqtt_dali.gateway.remove_topics_by_driver", new=AsyncMock()), patch(
        "wb.mqtt_dali.gateway.wait_for_rpc_endpoint", new=AsyncMock()
    ):
        await gateway.start()

    registered = {
        (call_args.args[0], call_args.args[1])
        for call_args in gateway.rpc_server.add_endpoint.await_args_list
    }
    assert ("Bus", "SendCommand") in registered
    assert ("Bus", "ListCommands") in registered


# ---------------------------------------------------------------------------
# Public API state-check parity with existing tasks (READY required)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_command_batch_raises_when_not_ready(registry):
    # pylint: disable=protected-access
    controller = make_loop_controller()
    controller._state = ApplicationControllerState.UNINITIALIZED

    with pytest.raises(RuntimeError):
        await controller.send_command_batch([parse_expression("Off(A5)", registry)])

    # Nothing was queued.
    assert controller._tasks_queue.qsize() == 0


@pytest.mark.asyncio
async def test_send_command_batch_rejected_in_quiescent_mode(registry):
    """Quiescent mode rejects the task at dequeue (symmetric with other task types)."""
    # pylint: disable=protected-access
    controller = make_loop_controller()
    controller._in_quiescent_mode = True

    async def fake_send(*_a, **_kw):
        return Response(BackwardFrame(0))

    with patch_send_with_retry(fake_send):
        loop_task = asyncio.create_task(controller._polling_loop())
        try:
            with pytest.raises(RuntimeError, match="quiescent"):
                await controller.send_command_batch([parse_expression("Off(A5)", registry)])
        finally:
            await stop_loop(controller, loop_task)


@pytest.mark.asyncio
async def test_send_command_batch_rejected_when_commissioning_running(registry):
    """Client commissioning (Bus/ScanBus) is queued or running → upfront reject,
    nothing is put on the task queue.
    """
    # pylint: disable=protected-access
    controller = make_loop_controller()
    controller._commissioning_state.mark_queued()

    with pytest.raises(RuntimeError, match="commissioning"):
        await controller.send_command_batch([parse_expression("Off(A5)", registry)])

    # No task was enqueued; the polling loop never sees the batch.
    assert controller._tasks_queue.qsize() == 0
