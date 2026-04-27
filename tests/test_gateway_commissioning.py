"""Tests covering gateway-side plumbing for the commissioning RPC/topic layer."""

import json
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from wb.mqtt_dali.application_controller import (
    CommissioningDeviceSummary,
    CommissioningStartResult,
    CommissioningState,
    CommissioningStatus,
)
from wb.mqtt_dali.gateway import (
    Gateway,
    WbDaliGateway,
    bus_to_json,
    commissioning_topic,
)

# pylint: disable=protected-access


def _make_gateway_shell():
    gw = Gateway.__new__(Gateway)
    gw.wb_dali_gateways = []
    gw._mqtt_dispatcher = MagicMock()
    gw._mqtt_dispatcher.client = MagicMock()
    gw._mqtt_dispatcher.client.publish = AsyncMock()
    gw._save_configuration = AsyncMock()
    return gw


def _make_fake_bus(uid: str):
    bus = MagicMock()
    bus.uid = uid
    bus.start_commissioning = AsyncMock(return_value=CommissioningStartResult.STARTED)
    bus.cancel_commissioning = AsyncMock(return_value=True)
    return bus


def test_commissioning_topic_format():
    assert commissioning_topic("wb-mdali_1_bus_2") == "/wb-dali/wb-mdali_1_bus_2/commissioning"


@pytest.mark.asyncio
async def test_rescan_bus_handler_returns_status_and_topic():
    gw = _make_gateway_shell()
    bus = _make_fake_bus("wb-mdali_1_bus_1")
    gw.wb_dali_gateways = [WbDaliGateway(uid="wb-mdali_1", buses=[bus])]

    result = await gw.rescan_bus_rpc_handler({"busId": "wb-mdali_1_bus_1"})

    assert result == {
        "status": "started",
        "progressTopic": "/wb-dali/wb-mdali_1_bus_1/commissioning",
    }
    bus.start_commissioning.assert_awaited_once()


@pytest.mark.asyncio
async def test_rescan_bus_handler_already_running():
    gw = _make_gateway_shell()
    bus = _make_fake_bus("wb-mdali_1_bus_1")
    bus.start_commissioning = AsyncMock(return_value=CommissioningStartResult.ALREADY_RUNNING)
    gw.wb_dali_gateways = [WbDaliGateway(uid="wb-mdali_1", buses=[bus])]

    result = await gw.rescan_bus_rpc_handler({"busId": "wb-mdali_1_bus_1"})

    assert result["status"] == "already_running"
    assert result["progressTopic"] == "/wb-dali/wb-mdali_1_bus_1/commissioning"


@pytest.mark.asyncio
async def test_rescan_bus_handler_unknown_bus_raises():
    gw = _make_gateway_shell()
    with pytest.raises(ValueError):
        await gw.rescan_bus_rpc_handler({"busId": "unknown"})


@pytest.mark.asyncio
async def test_stop_scan_bus_handler_stopped():
    gw = _make_gateway_shell()
    bus = _make_fake_bus("wb-mdali_1_bus_1")
    bus.cancel_commissioning = AsyncMock(return_value=True)
    gw.wb_dali_gateways = [WbDaliGateway(uid="wb-mdali_1", buses=[bus])]

    result = await gw.stop_scan_bus_rpc_handler({"busId": "wb-mdali_1_bus_1"})
    assert result == {"status": "stopped"}


@pytest.mark.asyncio
async def test_stop_scan_bus_handler_not_running():
    gw = _make_gateway_shell()
    bus = _make_fake_bus("wb-mdali_1_bus_1")
    bus.cancel_commissioning = AsyncMock(return_value=False)
    gw.wb_dali_gateways = [WbDaliGateway(uid="wb-mdali_1", buses=[bus])]

    result = await gw.stop_scan_bus_rpc_handler({"busId": "wb-mdali_1_bus_1"})
    assert result == {"status": "not_running"}


@pytest.mark.asyncio
async def test_stop_scan_bus_handler_unknown_bus_raises():
    gw = _make_gateway_shell()
    with pytest.raises(ValueError):
        await gw.stop_scan_bus_rpc_handler({"busId": "unknown"})


@pytest.mark.asyncio
async def test_commissioning_state_cb_saves_on_completed():
    gw = _make_gateway_shell()
    cb = gw._make_commissioning_state_cb("wb-mdali_1_bus_1")

    state = CommissioningState(
        status=CommissioningStatus.COMPLETED,
        progress=100,
        devices=[CommissioningDeviceSummary("0", "0x1", [])],
        finished_at="2026-04-23T14:32:15Z",
    )
    await cb(state)

    publish_mock = cast(AsyncMock, gw._mqtt_dispatcher.client.publish)
    publish_mock.assert_awaited_once()
    await_args = publish_mock.await_args
    assert await_args is not None
    (topic, payload), kwargs = await_args
    assert topic == "/wb-dali/wb-mdali_1_bus_1/commissioning"
    assert '"status": "completed"' in payload
    assert kwargs.get("retain") is True
    cast(AsyncMock, gw._save_configuration).assert_awaited_once()


@pytest.mark.asyncio
async def test_commissioning_state_cb_skips_save_on_cancelled():
    """CANCELLED means scan did not happen; config must NOT be saved.

    The state callback still publishes to MQTT, but ``_save_configuration``
    is intentionally skipped — see "Обработка терминальных состояний" in
    doc/commissioning_progress_plan.md.
    """
    gw = _make_gateway_shell()
    cb = gw._make_commissioning_state_cb("bus")

    state = CommissioningState(
        status=CommissioningStatus.CANCELLED,
        progress=42,
        devices=[],
        finished_at="2026-04-23T14:32:15Z",
    )
    await cb(state)

    # Still published so the retained topic converges to the cancelled state.
    cast(AsyncMock, gw._mqtt_dispatcher.client.publish).assert_awaited_once()
    # But the config is NOT saved.
    cast(AsyncMock, gw._save_configuration).assert_not_awaited()


@pytest.mark.asyncio
async def test_commissioning_state_cb_skips_save_on_running():
    gw = _make_gateway_shell()
    cb = gw._make_commissioning_state_cb("bus")
    state = CommissioningState(
        status=CommissioningStatus.QUERY_SHORT_ADDRESSES,
        progress=10,
    )
    await cb(state)
    cast(AsyncMock, gw._save_configuration).assert_not_awaited()


@pytest.mark.asyncio
async def test_commissioning_state_cb_skips_save_on_idle():
    gw = _make_gateway_shell()
    cb = gw._make_commissioning_state_cb("bus")
    state = CommissioningState(status=CommissioningStatus.IDLE)
    await cb(state)
    cast(AsyncMock, gw._save_configuration).assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_idle_emits_retained_for_every_bus():
    gw = _make_gateway_shell()
    bus1 = _make_fake_bus("bus_1")
    bus2 = _make_fake_bus("bus_2")
    gw.wb_dali_gateways = [WbDaliGateway(uid="gw", buses=[bus1, bus2])]

    await gw._publish_idle_commissioning_state_for_all_buses()

    calls = gw._mqtt_dispatcher.client.publish.await_args_list
    assert len(calls) == 2
    topics = [c.args[0] for c in calls]
    assert "/wb-dali/bus_1/commissioning" in topics
    assert "/wb-dali/bus_2/commissioning" in topics
    for c in calls:
        assert '"status": "idle"' in c.args[1]
        assert c.kwargs.get("retain") is True


@pytest.mark.asyncio
async def test_publish_idle_payload_matches_default_state_constructor():
    """The idle payload must be generated from ``CommissioningState()`` — the
    constructor is the single source of truth for the payload shape.
    """
    gw = _make_gateway_shell()
    bus = _make_fake_bus("bus_1")
    gw.wb_dali_gateways = [WbDaliGateway(uid="gw", buses=[bus])]

    await gw._publish_idle_commissioning_state_for_all_buses()

    call = gw._mqtt_dispatcher.client.publish.await_args
    assert call is not None
    _topic, payload = call.args
    assert payload == json.dumps(CommissioningState().to_dict())


@pytest.mark.asyncio
async def test_clear_retained_state_on_stop():
    gw = _make_gateway_shell()
    bus = _make_fake_bus("bus_1")
    gw.wb_dali_gateways = [WbDaliGateway(uid="gw", buses=[bus])]

    await gw._clear_commissioning_state_for_all_buses()

    call = gw._mqtt_dispatcher.client.publish.await_args
    assert call.args[0] == "/wb-dali/bus_1/commissioning"
    # payload None → retained deletion.
    assert call.kwargs.get("payload") is None
    assert call.kwargs.get("retain") is True


def test_bus_to_json_includes_commissioning():
    bus = MagicMock()
    bus.uid = "bus_1"
    bus.bus_name = "Bus 1"
    bus.dali_devices = []
    bus.dali2_devices = []
    bus.commissioning_state = CommissioningState(status=CommissioningStatus.IDLE, progress=0)

    result = bus_to_json(bus)
    assert "commissioning" in result
    assert result["commissioning"]["status"] == "idle"
    assert result["commissioning"]["finished_at"] is None
