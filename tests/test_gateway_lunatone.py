"""Tests for gateway-level Lunatone IoT emulator wiring (S2..S5).

Exercises the `Gateway` RPC handlers, config save/load, and `WbDaliGateway`
multi-bus emulator orchestration without starting any real network listener.
"""

import asyncio
import json
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wb.mqtt_dali.application_controller import ApplicationController
from wb.mqtt_dali.gateway import (
    DEFAULT_WEBSOCKET_PORT,
    Gateway,
    WbDaliGateway,
    bus_from_json,
    save_configuration,
)

# pylint: disable=protected-access


def _bare_bus(uid: str) -> ApplicationController:
    """Return a minimally-functional ApplicationController-shaped mock.

    Uses ``spec=ApplicationController`` so renaming or removing any of the
    accessed attributes on the real class makes these tests fail at access
    time instead of silently passing.
    """
    bus = MagicMock(spec=ApplicationController)
    bus.uid = uid
    bus.bus_name = "Bus 1"
    bus.dali_devices = []
    bus.dali2_devices = []
    bus.polling_interval = 5.0
    bus.bus_monitor_enabled = False
    bus.load_bus_info.return_value = {"type": "object", "properties": {}}
    return bus


def _make_gateway_object(uid: str, bus_count: int = 2) -> WbDaliGateway:
    buses = [_bare_bus(f"{uid}_bus_{i}") for i in range(1, bus_count + 1)]
    return WbDaliGateway(uid=uid, buses=buses)


def _make_gateway_service(wb_dali_gateways) -> Gateway:
    """Build a `Gateway` skeleton bypassing __init__."""
    svc = Gateway.__new__(Gateway)
    svc.wb_dali_gateways = wb_dali_gateways
    svc._mqtt_dispatcher = MagicMock()
    svc._config_lock = asyncio.Lock()
    svc._config_path = "/tmp/_unused.conf"
    svc._debug = False
    svc._gtin_db = MagicMock()
    svc._save_configuration = AsyncMock()
    return svc


# -------- bus_from_json: legacy keys are ignored --------


def test_legacy_config_loads_and_ignores_per_bus_websocket_keys():
    """Legacy bus-level `websocket_*` keys must not raise nor surface anywhere."""
    bus_data = {
        "devices": [],
        "websocket_enabled": True,
        "websocket_port": 12345,
        "polling_interval": 7,
        "bus_monitor_enabled": True,
    }
    mqtt_dispatcher = MagicMock()
    gtin_db = MagicMock()

    bus = bus_from_json("gw1", 1, bus_data, mqtt_dispatcher, gtin_db)

    # Bus has no `websocket_config` attribute anymore; only the gateway has settings.
    assert not hasattr(bus, "websocket_config")
    assert bus.polling_interval == 7
    assert bus.bus_monitor_enabled is True


def test_gateway_constructor_reads_gateway_level_websocket_keys():
    config = {
        "gateways": [
            {
                "device_id": "gw1",
                "websocket_enabled": True,
                "websocket_port": 9090,
                "buses": [{"devices": []}],
            },
            {
                "device_id": "gw2",
                "buses": [{"devices": []}],
            },
        ]
    }
    with patch("wb.mqtt_dali.gateway.MQTTRPCServer"):
        svc = Gateway(config, MagicMock(), "/tmp/cfg.json", MagicMock())

    assert len(svc.wb_dali_gateways) == 2
    assert svc.wb_dali_gateways[0].uid == "gw1"
    assert svc.wb_dali_gateways[0].websocket_enabled is True
    assert svc.wb_dali_gateways[0].websocket_port == 9090
    assert svc.wb_dali_gateways[1].uid == "gw2"
    assert svc.wb_dali_gateways[1].websocket_enabled is False
    assert svc.wb_dali_gateways[1].websocket_port == DEFAULT_WEBSOCKET_PORT


def test_gateway_ignores_legacy_per_bus_websocket_keys_in_config():
    """Whole pipeline: gateway with legacy bus-level keys loads cleanly with defaults."""
    config = {
        "gateways": [
            {
                "device_id": "gw-legacy",
                "buses": [
                    {
                        "devices": [],
                        "websocket_enabled": True,
                        "websocket_port": 4242,
                    }
                ],
            }
        ]
    }
    with patch("wb.mqtt_dali.gateway.MQTTRPCServer"):
        svc = Gateway(config, MagicMock(), "/tmp/cfg.json", MagicMock())

    gw = svc.wb_dali_gateways[0]
    assert gw.websocket_enabled is False
    assert gw.websocket_port == DEFAULT_WEBSOCKET_PORT


# -------- Editor.GetGateway --------


@pytest.mark.asyncio
async def test_get_gateway_rpc_returns_config_and_schema():
    gw = _make_gateway_object("gw1")
    gw.websocket_enabled = True
    gw.websocket_port = 1234
    svc = _make_gateway_service([gw])

    result = await svc.get_gateway_rpc_handler({"gatewayId": "gw1"})

    assert result["config"] == {"websocket_enabled": True, "websocket_port": 1234}
    assert result["schema"] is not None


@pytest.mark.asyncio
async def test_get_gateway_rpc_unknown_id_raises():
    gw = _make_gateway_object("gw1")
    svc = _make_gateway_service([gw])

    with pytest.raises(ValueError, match="not found"):
        await svc.get_gateway_rpc_handler({"gatewayId": "missing"})


# -------- Editor.SetGateway --------


@pytest.mark.asyncio
async def test_set_gateway_rpc_starts_emulator():
    gw = _make_gateway_object("gw1")
    svc = _make_gateway_service([gw])

    async def _fake_run_websocket(*_args, **_kwargs):
        await asyncio.sleep(3600)

    with patch("wb.mqtt_dali.gateway.run_websocket", new=_fake_run_websocket), patch(
        "wb.mqtt_dali.gateway.save_configuration"
    ) as mock_save:
        result = await svc.set_gateway_rpc_handler(
            {"gatewayId": "gw1", "config": {"websocket_enabled": True, "websocket_port": 8080}}
        )

        assert result == {"websocket_enabled": True, "websocket_port": 8080}
        assert gw.websocket_enabled is True
        assert gw.websocket_port == 8080
        assert gw._websocket_task is not None
        mock_save.assert_called_once()
        # cleanup
        await gw._stop_websocket()


@pytest.mark.asyncio
async def test_set_gateway_rpc_changes_port():
    gw = _make_gateway_object("gw1")
    svc = _make_gateway_service([gw])

    started_ports: list[int] = []

    async def _fake_run_websocket(_drivers, _name, _host, port, _logger):
        started_ports.append(port)
        await asyncio.sleep(3600)

    # Start initial emulator on port 8080.
    with patch("wb.mqtt_dali.gateway.run_websocket", new=_fake_run_websocket), patch(
        "wb.mqtt_dali.gateway.save_configuration"
    ):
        await svc.set_gateway_rpc_handler(
            {"gatewayId": "gw1", "config": {"websocket_enabled": True, "websocket_port": 8080}}
        )
        first_task = gw._websocket_task
        assert first_task is not None
        await asyncio.sleep(0)  # let _fake_run_websocket start
        # Now change the port.
        result = await svc.set_gateway_rpc_handler(
            {"gatewayId": "gw1", "config": {"websocket_enabled": True, "websocket_port": 9999}}
        )
        await asyncio.sleep(0)  # let the new task record its port

        assert result["websocket_port"] == 9999
        assert started_ports == [8080, 9999]
        assert first_task.cancelled() or first_task.done()
        assert gw._websocket_task is not first_task
        await gw._stop_websocket()


@pytest.mark.asyncio
async def test_set_gateway_rpc_disables_emulator_releases_quiescent():
    """When emulator is disabled, all buses of the gateway exit quiescent mode."""
    gw = _make_gateway_object("gw1", bus_count=2)
    svc = _make_gateway_service([gw])

    async def _fake_run_websocket(*_args, **_kwargs):
        await asyncio.sleep(3600)

    with patch("wb.mqtt_dali.gateway.run_websocket", new=_fake_run_websocket), patch(
        "wb.mqtt_dali.gateway.save_configuration"
    ):
        await svc.set_gateway_rpc_handler(
            {"gatewayId": "gw1", "config": {"websocket_enabled": True, "websocket_port": 8080}}
        )

        result = await svc.set_gateway_rpc_handler(
            {"gatewayId": "gw1", "config": {"websocket_enabled": False}}
        )

        assert result["websocket_enabled"] is False
        for bus in gw.buses:
            bus.release_quiescent_mode.assert_called_once()
        assert gw._websocket_task is None


@pytest.mark.asyncio
async def test_set_gateway_rpc_rejects_duplicate_port():
    gw1 = _make_gateway_object("gw1")
    gw1.websocket_enabled = True
    gw1.websocket_port = 7777
    gw2 = _make_gateway_object("gw2")
    svc = _make_gateway_service([gw1, gw2])

    with pytest.raises(ValueError, match="already in use"):
        await svc.set_gateway_rpc_handler(
            {"gatewayId": "gw2", "config": {"websocket_enabled": True, "websocket_port": 7777}}
        )


@pytest.mark.asyncio
async def test_set_gateway_rpc_unknown_id_raises():
    gw = _make_gateway_object("gw1")
    svc = _make_gateway_service([gw])

    with pytest.raises(ValueError, match="not found"):
        await svc.set_gateway_rpc_handler({"gatewayId": "missing", "config": {"websocket_enabled": True}})


@pytest.mark.asyncio
async def test_set_gateway_rpc_rejects_out_of_range_port():
    gw = _make_gateway_object("gw1")
    svc = _make_gateway_service([gw])

    with pytest.raises(ValueError, match="out of range"):
        await svc.set_gateway_rpc_handler(
            {"gatewayId": "gw1", "config": {"websocket_enabled": True, "websocket_port": 0}}
        )

    with pytest.raises(ValueError, match="out of range"):
        await svc.set_gateway_rpc_handler(
            {"gatewayId": "gw1", "config": {"websocket_enabled": True, "websocket_port": 70000}}
        )


@pytest.mark.asyncio
async def test_set_gateway_rpc_same_port_for_different_gateways_ok_when_disabled():
    """A duplicate port is fine if the other gateway has the emulator disabled."""
    gw1 = _make_gateway_object("gw1")
    gw1.websocket_enabled = False
    gw1.websocket_port = 7777
    gw2 = _make_gateway_object("gw2")
    svc = _make_gateway_service([gw1, gw2])

    async def _fake_run_websocket(*_args, **_kwargs):
        await asyncio.sleep(3600)

    with patch("wb.mqtt_dali.gateway.run_websocket", new=_fake_run_websocket), patch(
        "wb.mqtt_dali.gateway.save_configuration"
    ):
        result = await svc.set_gateway_rpc_handler(
            {"gatewayId": "gw2", "config": {"websocket_enabled": True, "websocket_port": 7777}}
        )

        assert result["websocket_enabled"] is True
        assert result["websocket_port"] == 7777
        await gw2._stop_websocket()


# -------- Editor.GetBus / Editor.SetBus --------


@pytest.mark.asyncio
async def test_get_bus_rpc_omits_websocket_keys():
    gw = _make_gateway_object("gw1")
    svc = _make_gateway_service([gw])

    result = await svc.get_bus_rpc_handler({"busId": "gw1_bus_1"})

    assert "websocket_enabled" not in result["config"]
    assert "websocket_port" not in result["config"]
    assert "polling_interval" in result["config"]
    assert "bus_monitor_enabled" in result["config"]


@pytest.mark.asyncio
async def test_set_bus_rpc_ignores_legacy_websocket_keys():
    gw = _make_gateway_object("gw1")
    svc = _make_gateway_service([gw])

    result = await svc.set_bus_rpc_handler(
        {
            "busId": "gw1_bus_1",
            "config": {
                "websocket_enabled": True,
                "websocket_port": 9999,
                "polling_interval": 12,
                "bus_monitor_enabled": True,
            },
        }
    )

    assert "websocket_enabled" not in result
    assert "websocket_port" not in result
    # gw remained untouched by these keys
    assert gw.websocket_enabled is False
    assert gw.websocket_port == DEFAULT_WEBSOCKET_PORT
    bus = gw.buses[0]
    bus.set_polling_interval.assert_called_with(12)
    bus.set_bus_monitor_enabled.assert_called_with(True)


# -------- save_configuration: file shape --------


def _bus_with_state(uid: str) -> SimpleNamespace:
    bus = SimpleNamespace()
    bus.uid = uid
    bus.polling_interval = 5
    bus.bus_monitor_enabled = False
    bus.dali_devices = []
    bus.dali2_devices = []
    return bus


def test_legacy_config_save_rewrites_to_new_shape():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "wb-mqtt-dali.conf")
        # Pre-populate with a legacy-shape file.
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(
                {
                    "gateways": [
                        {
                            "device_id": "gw1",
                            "buses": [
                                {
                                    "websocket_enabled": True,
                                    "websocket_port": 12345,
                                    "polling_interval": 5,
                                    "devices": [],
                                    "bus_monitor_enabled": False,
                                }
                            ],
                        }
                    ]
                },
                fp,
            )

        gw = WbDaliGateway(
            uid="gw1",
            buses=[_bus_with_state("gw1_bus_1")],
            websocket_enabled=False,
            websocket_port=DEFAULT_WEBSOCKET_PORT,
        )
        save_configuration(path, debug=False, gateways=[gw])

        with open(path, "r", encoding="utf-8") as fp:
            written = json.load(fp)

        assert "gateways" in written
        gw_entry = written["gateways"][0]
        # Per-gateway keys present, per-bus keys absent.
        assert "websocket_enabled" in gw_entry
        assert "websocket_port" in gw_entry
        for bus_entry in gw_entry["buses"]:
            assert "websocket_enabled" not in bus_entry
            assert "websocket_port" not in bus_entry


# -------- Auto-discovery default --------


@pytest.mark.asyncio
async def test_auto_discovered_gateway_emulator_disabled_by_default():
    """A new gateway added by `_update_gateways` starts with the emulator disabled."""
    svc = _make_gateway_service([])
    svc._mqtt_dispatcher = MagicMock()
    svc._gtin_db = MagicMock()

    # Simulate one new wb-mqtt-serial device that should become a new gateway.
    fake_serial = {
        "config": {
            "ports": [
                {
                    "enabled": True,
                    "devices": [
                        {
                            "device_type": "WB-DALI",
                            "id": "wb-dali-new",
                            "enabled": True,
                        }
                    ],
                }
            ]
        }
    }

    with patch(
        "wb.mqtt_dali.gateway.rpc_call",
        new=AsyncMock(return_value=fake_serial),
    ), patch.object(WbDaliGateway, "start", new=AsyncMock()):
        await svc._update_gateways()

    assert len(svc.wb_dali_gateways) == 1
    gw = svc.wb_dali_gateways[0]
    assert gw.uid == "wb-dali-new"
    assert gw.websocket_enabled is False
    assert gw.websocket_port == DEFAULT_WEBSOCKET_PORT


# -------- WbDaliGateway behavior --------


@pytest.mark.asyncio
async def test_wb_dali_gateway_apply_no_op_when_unchanged():
    gw = _make_gateway_object("gw1")
    gw.websocket_enabled = False
    gw.websocket_port = 8080

    with patch("wb.mqtt_dali.gateway.run_websocket") as mock_run:
        await gw.apply_websocket_config(False, 8080)
    mock_run.assert_not_called()
    assert gw._websocket_task is None


@pytest.mark.asyncio
async def test_wb_dali_gateway_disable_releases_quiescent_only_if_was_enabled():
    """If emulator was already disabled, disabling does not call release_quiescent_mode."""
    gw = _make_gateway_object("gw1", bus_count=2)
    gw.websocket_enabled = False
    gw.websocket_port = 8080

    # Change the port while disabled -> still disabled, no release.
    await gw.apply_websocket_config(False, 9090)

    for bus in gw.buses:
        bus.release_quiescent_mode.assert_not_called()
