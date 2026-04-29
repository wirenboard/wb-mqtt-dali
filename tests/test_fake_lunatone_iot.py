"""Tests for the gateway-level Lunatone IoT emulator (`fake_lunatone_iot`).

These tests cover scenarios S1 (multi-line emulator) by exercising
`emulate()` directly with a stub websocket and stub drivers.
"""

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dali.frame import BackwardFrame, ForwardFrame

from wb.mqtt_dali.asyncio_utils import OneShotTasks
from wb.mqtt_dali.bus_traffic import (
    BusTrafficCallbacks,
    BusTrafficItem,
    BusTrafficSource,
)
from wb.mqtt_dali.fake_lunatone_iot import (
    AnswerResult,
    SendingResult,
    emulate,
    make_initial_greet,
    publish_traffic,
)


class _StubWebSocket:
    """Async iterator over `incoming` plus a `send()` that records outgoing JSON."""

    def __init__(self, incoming):
        self._incoming = list(incoming)
        self.sent: list[dict] = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._incoming:
            raise StopAsyncIteration
        return self._incoming.pop(0)

    async def send(self, data):
        self.sent.append(json.loads(data))


def _make_driver():
    driver = MagicMock()
    driver.bus_traffic = BusTrafficCallbacks(gateway_queue_size=16)
    driver.config = SimpleNamespace(device_name="test", bus=1)
    return driver


def _dali_frame_message(line, payload, bits=16, send_twice=False, wait_for_answer=False):
    return json.dumps(
        {
            "type": "daliFrame",
            "data": {
                "numberOfBits": bits,
                "daliData": payload,
                "line": line,
                "mode": {
                    "sendTwice": send_twice,
                    "priority": 4,
                    "waitForAnswer": wait_for_answer,
                },
            },
        }
    )


# -------- make_initial_greet --------


def test_lunatone_emulator_advertises_lines_count():
    greet = make_initial_greet("wb-test", lines=3)
    assert greet["type"] == "info"
    assert greet["data"]["name"] == "wb-test"
    assert greet["data"]["descriptor"]["lines"] == 3


def test_lunatone_emulator_advertises_single_line_for_single_bus():
    greet = make_initial_greet("wb-only-one", lines=1)
    assert greet["data"]["descriptor"]["lines"] == 1


# -------- emulate(): routing of daliFrame to lines --------


@pytest.mark.asyncio
async def test_lunatone_emulator_routes_frame_to_correct_line():
    drivers = [_make_driver(), _make_driver(), _make_driver()]
    incoming = [
        _dali_frame_message(0, [0xFE, 0x00]),
        _dali_frame_message(1, [0xFE, 0x01]),
        _dali_frame_message(2, [0xFE, 0x02]),
    ]
    ws = _StubWebSocket(incoming)

    captured = []

    async def fake_send_with_retry(driver, _cmd, _logger, _source):
        captured.append((driver, _cmd))
        return MagicMock(raw_value=None)

    with patch(
        "wb.mqtt_dali.fake_lunatone_iot.send_with_retry",
        new=AsyncMock(side_effect=fake_send_with_retry),
    ):
        await emulate(ws, drivers, "wb-multi", logging.getLogger("test"))

    # 3 frames sent, each routed to its driver in order
    assert len(captured) == 3
    assert captured[0][0] is drivers[0]
    assert captured[1][0] is drivers[1]
    assert captured[2][0] is drivers[2]


@pytest.mark.asyncio
async def test_lunatone_emulator_rejects_out_of_range_line_with_no_such_line():
    drivers = [_make_driver(), _make_driver()]
    incoming = [_dali_frame_message(2, [0xFE, 0xAA])]  # line=2 with only 2 lines (0,1)
    ws = _StubWebSocket(incoming)

    with patch(
        "wb.mqtt_dali.fake_lunatone_iot.send_with_retry",
        new=AsyncMock(),
    ) as mock_send:
        await emulate(ws, drivers, "wb-multi", logging.getLogger("test"))

    mock_send.assert_not_awaited()
    # First message is greeting
    assert ws.sent[0]["type"] == "info"
    # Then a daliFrame result with NO_SUCH_LINE
    frame_results = [m for m in ws.sent if m["type"] == "daliFrame"]
    assert frame_results
    assert frame_results[0]["data"]["line"] == 2
    assert frame_results[0]["data"]["result"] == SendingResult.NO_SUCH_LINE.value


@pytest.mark.asyncio
async def test_lunatone_emulator_rejects_negative_line_with_no_such_line():
    drivers = [_make_driver()]
    incoming = [_dali_frame_message(-1, [0xFE, 0xAA])]
    ws = _StubWebSocket(incoming)

    with patch(
        "wb.mqtt_dali.fake_lunatone_iot.send_with_retry",
        new=AsyncMock(),
    ) as mock_send:
        await emulate(ws, drivers, "wb-only", logging.getLogger("test"))

    mock_send.assert_not_awaited()
    frame_results = [m for m in ws.sent if m["type"] == "daliFrame"]
    assert frame_results
    assert frame_results[0]["data"]["result"] == SendingResult.NO_SUCH_LINE.value


@pytest.mark.asyncio
async def test_lunatone_emulator_returns_dali_answer_with_correct_line():
    drivers = [_make_driver(), _make_driver()]
    incoming = [_dali_frame_message(1, [0xA0, 0x00], bits=16, wait_for_answer=True)]
    ws = _StubWebSocket(incoming)

    fake_response = MagicMock()
    fake_response.raw_value = BackwardFrame(0x42)

    with patch(
        "wb.mqtt_dali.fake_lunatone_iot.send_with_retry",
        new=AsyncMock(return_value=fake_response),
    ):
        await emulate(ws, drivers, "wb-multi", logging.getLogger("test"))

    answers = [m for m in ws.sent if m["type"] == "daliAnswer"]
    assert len(answers) == 1
    assert answers[0]["data"]["line"] == 1
    assert answers[0]["data"]["result"] == AnswerResult.VALUE_8BIT.value
    assert answers[0]["data"]["daliData"] == 0x42


# -------- publish_traffic(): line index in daliMonitor messages --------


@pytest.mark.asyncio
async def test_lunatone_emulator_monitor_carries_line_index():
    """Monitor events from line k carry `line=k` in the daliMonitor message."""
    ws = _StubWebSocket([])

    one_shot = OneShotTasks(logging.getLogger("test"))
    handler = publish_traffic(ws, line=1, logger=logging.getLogger("test"), one_shot_tasks=one_shot)

    request = ForwardFrame(16, [0x12, 0x34])
    item = BusTrafficItem(
        request=request,
        response=None,
        request_source=BusTrafficSource.WB,
        frame_counter=1,
    )
    handler(item)
    # Let scheduled one-shot tasks run.
    for _ in range(3):
        await asyncio.sleep(0)
    await one_shot.stop()

    monitors = [m for m in ws.sent if m["type"] == "daliMonitor"]
    assert monitors, "no daliMonitor messages were sent"
    assert all(m["data"]["line"] == 1 for m in monitors)


@pytest.mark.asyncio
async def test_lunatone_emulator_monitor_per_line_separation():
    """When two drivers fire traffic, monitor messages carry the right line per driver."""
    drivers = [_make_driver(), _make_driver(), _make_driver()]

    sent: list[dict] = []
    iterator_started = asyncio.Event()
    end_iteration = asyncio.Event()

    class _ControllableWS:
        async def send(self, data):
            sent.append(json.loads(data))

        def __aiter__(self):
            return self

        async def __anext__(self):
            iterator_started.set()
            await end_iteration.wait()
            raise StopAsyncIteration

    ws = _ControllableWS()

    task = asyncio.create_task(emulate(ws, drivers, "wb-multi", logging.getLogger("test")))
    await iterator_started.wait()

    # Driver 2 fires a frame; it should be reported as line=2.
    request = ForwardFrame(16, [0xCC, 0xDD])
    drivers[2].bus_traffic.notify_command(
        request=request,
        response=None,
        source=BusTrafficSource.LUNATONE,
        sequence_id=0,
    )
    # Let scheduled one-shot tasks run.
    for _ in range(3):
        await asyncio.sleep(0)

    end_iteration.set()
    await task

    monitors = [m for m in sent if m["type"] == "daliMonitor"]
    assert monitors
    assert monitors[0]["data"]["line"] == 2
