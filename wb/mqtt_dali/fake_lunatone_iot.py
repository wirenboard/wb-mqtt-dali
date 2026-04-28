"""
Emulate the bare minimum of Lunatone's DALI-2 IoT Gateway over Websocket

Sometimes it's useful to fire up Lunatone's DALI Cockpit, the proprietary
Windows GUI application for addressing, configuring, managing, etc. a DALI
installation. This code implements the bare minimum of their Websocket
protocol, effectively emulating the "Lunatone DALI-2 IoT Gateway". If you
run this code (or integrate the relevant two lines into your existing
building automation system which uses python-dali internally), you can let
the DALI Cockpit access the DALI bus without having to mess with, say,
usb-ip and restarting services.

In the DALI Cockpit, select DALI Bus -> Bus Interface, pick the "Network"
option and in there the "DALI-2 Display/DALI-2 IoT/DALI-2 WLAN", and enter
your device's IP address and port, e.g., 192.0.2.3:8080.

A single emulator instance maps multiple DALI buses to "lines" 0..N-1 of
one Lunatone device: incoming `daliFrame` with `line=k` is routed to the
k-th driver, and bus traffic from each driver is published with its own
line index.
"""

import asyncio
import json
import logging
from copy import deepcopy
from enum import Enum
from http import HTTPStatus
from typing import Any, Callable, Optional, Sequence

import dali.command
import dali.frame
import websockets.exceptions

try:
    from websockets.http import Headers
    from websockets.server import HTTPResponse, WebSocketServerProtocol, serve
except ImportError:
    from websockets.legacy.http import Headers
    from websockets.legacy.server import HTTPResponse, WebSocketServerProtocol, serve

from websockets.typing import Data

from .asyncio_utils import OneShotTasks
from .bus_traffic import BusTrafficItem, BusTrafficSource
from .wbdali import WBDALIDriver
from .wbdali_utils import is_transmission_error_response, send_with_retry


class LunatoneIotProtocolError(RuntimeError):
    pass


class SendingResult(Enum):
    SENT = 0
    ERROR_BUS_VOLTAGE = 1
    ERROR_INITIALIZE = 2
    ERROR_QUIESCENT = 3
    BUFFER_FULL = 4
    NO_SUCH_LINE = 5
    SYNTAX_ERROR = 6
    MACRO_IS_ACTIVE = 7
    COLLISION = 61
    BUS_ERROR = 62
    TIMEOUT = 63
    NO_ANSWER = 100


class AnswerResult(Enum):
    NO_ANSWER = 0
    VALUE_8BIT = 8
    FRAMING_ERROR = 63


def _msg_dali_monitor(line: int, bits: int, data: list[int], framing_error: bool) -> dict[str, Any]:
    if bits == 25:
        # eDALI actually has 24 meaningful bits and DALI Cockpit expects 24 bits here as well
        data = data[1:]
    return {
        "type": "daliMonitor",
        "data": {
            "bits": bits,
            "data": data,
            "line": line,
            "framingError": framing_error,
        },
    }


# The docs mention a ton of other fields, but I'm still getting this thing displayed as a 'DALI-2 Display 7"',
# despite using the IoT-gateway's GTIN. I can live with that :).
_INITIAL_GREET = {
    "type": "info",
    "data": {
        "name": "wb-lunatone-iot",
        "errors": {},
        "descriptor": {
            "lines": 1,
            "protocolVersion": "1.0",
        },
        "device": {
            "gtin": 9010342013607,  # "Lunatone DALI-2 IoT"
        },
    },
}


def make_initial_greet(name: str, lines: int) -> dict[str, Any]:
    greet = deepcopy(_INITIAL_GREET)
    greet["data"]["name"] = name
    greet["data"]["descriptor"]["lines"] = lines
    return greet


def _unbreak_jsonish(blob: Data) -> str:
    # Lunatone's DALI-Cockpit sends malformed JSON,
    # with `True` and `False` instead of JSON's own `true` and `false`.
    # This is a huge hack which will corrupt unrelated data, but hey,
    # I *hope* I won't be getting any strings here.
    string_blob = ""
    if isinstance(blob, bytes):
        string_blob = blob.decode("utf-8")
    if isinstance(blob, str):
        string_blob = blob
    return string_blob.replace("True", "true").replace("False", "false")


async def frame_result(websocket, line, result: SendingResult, logger: logging.Logger):
    logger.debug("WS >> daliFrame line=%s result=%s", line, result)
    await websocket.send(json.dumps({"type": "daliFrame", "data": {"line": line, "result": result.value}}))


async def dali_answer(websocket, line, result, dali_data, logger: logging.Logger):
    if dali_data is None:
        logger.debug("WS >> daliAnswer line=%s result=%s dali_data=%s", line, result, dali_data)
    else:
        logger.debug("WS >> daliAnswer line=%s result=%s dali_data=%02x", line, result, dali_data)
    await websocket.send(
        json.dumps(
            {"type": "daliAnswer", "data": {"line": line, "result": result.value, "daliData": dali_data}}
        )
    )


async def emulate(  # pylint: disable=too-many-locals, too-many-branches, too-many-statements
    websocket: WebSocketServerProtocol,
    drivers: Sequence[WBDALIDriver],
    name: str,
    logger: logging.Logger,
):
    one_shot_tasks = OneShotTasks(logger)
    cleanup_callbacks: list[Callable[[], None]] = []
    for line_index, driver in enumerate(drivers):
        cleanup_callbacks.append(
            driver.bus_traffic.register(publish_traffic(websocket, line_index, logger, one_shot_tasks))
        )
    try:
        await websocket.send(json.dumps(make_initial_greet(name, len(drivers))))
        async for raw_message in websocket:
            line = 0
            try:
                try:
                    message = json.loads(_unbreak_jsonish(raw_message))
                except json.JSONDecodeError as e:
                    raise LunatoneIotProtocolError(f"Cannot parse JSON: {e}: {raw_message=}") from e
                if "type" not in message:
                    raise LunatoneIotProtocolError(f'No "type" field in this JSON packet: {message}')
                if message["type"] == "filtering":
                    logger.debug("WS << NOOP filtering: %s", message)
                    # Filtering is intentionally ignored by this emulator.
                    pass  # pylint: disable=W0107
                elif message["type"] == "daliFrame":
                    try:
                        bits = message["data"]["numberOfBits"]
                        payload = message["data"]["daliData"]
                        line = message["data"]["line"]
                        send_twice = message["data"]["mode"]["sendTwice"]
                        # priority = message["data"]["mode"]["priority"]
                        wait_for_answer = message["data"]["mode"]["waitForAnswer"]
                    except KeyError as e:
                        raise KeyError(f"Missing {e} for DALI frame: {message}") from e
                    logger.debug(
                        "WS << daliFrame (bits=%s line=%s sendTwice=%s waitForAnswer=%s) %s",
                        bits,
                        line,
                        send_twice,
                        wait_for_answer,
                        " ".join(f"{b:02x}" for b in payload),
                    )
                    if not 0 <= line < len(drivers):
                        await frame_result(websocket, line, SendingResult.NO_SUCH_LINE, logger)
                        continue

                    driver = drivers[line]
                    if bits not in (16, 24, 25):
                        logger.error("bits=%s not supported yet, faking a no-reply", bits)
                        await frame_result(websocket, line, SendingResult.SENT, logger)
                        if wait_for_answer:
                            await dali_answer(websocket, line, AnswerResult.NO_ANSWER, None, logger)
                        continue

                    frame = dali.frame.ForwardFrame(bits, payload)
                    command = dali.command.from_frame(frame)
                    if wait_for_answer and command.response is None:
                        # If we are waiting for an answer, we need to set the response type
                        # otherwise the driver won't wait for answer
                        # this is useful for commands unknown to python-dali, like eDALI
                        command.response = dali.command.Response

                    resp = await send_with_retry(
                        driver,
                        command,
                        logger,
                        BusTrafficSource.LUNATONE,
                    )
                    await frame_result(websocket, line, SendingResult.SENT, logger)
                    if send_twice:
                        # As per docs, just send the confirmation twice.
                        # I am lazy, and therefore I ignore the `sendTwice`
                        # because the frame parser within python-dali already does that for me.
                        # This might be a bug from the DALI Cockpit's point of view.
                        await frame_result(websocket, line, SendingResult.SENT, logger)
                    if wait_for_answer:
                        if resp is None or resp.raw_value is None:
                            await dali_answer(websocket, line, AnswerResult.NO_ANSWER, None, logger)
                        elif isinstance(resp.raw_value, dali.frame.BackwardFrameError):
                            await dali_answer(websocket, line, AnswerResult.FRAMING_ERROR, None, logger)
                        else:
                            await dali_answer(
                                websocket, line, AnswerResult.VALUE_8BIT, resp.raw_value.as_integer, logger
                            )
                else:
                    raise LunatoneIotProtocolError(f'Unknown "type" field in this JSON packet: {message}')
            except LunatoneIotProtocolError as e:
                logger.error("Error: %s", e)
                await frame_result(websocket, line, SendingResult.SYNTAX_ERROR, logger)
    except websockets.exceptions.ConnectionClosed as e:
        logger.info("WS closed: %s", e)
    finally:
        for cleanup in cleanup_callbacks:
            cleanup()
        await one_shot_tasks.stop()


def publish_traffic(
    websocket,
    line: int,
    logger: logging.Logger,
    one_shot_tasks: OneShotTasks,
) -> Callable[[BusTrafficItem], None]:
    def _traffic_filter(bus_traffic_item: BusTrafficItem) -> None:
        logger.debug(
            "WS >> daliMonitor: line=%d %sbits=%d %s",
            line,
            "FRAMING ERROR " if bus_traffic_item.request.error else "",
            len(bus_traffic_item.request),
            " ".join(f"{b:02x}" for b in bus_traffic_item.request.as_byte_sequence),
        )
        one_shot_tasks.add(
            websocket.send(
                json.dumps(
                    _msg_dali_monitor(
                        line,
                        len(bus_traffic_item.request),
                        bus_traffic_item.request.as_byte_sequence,
                        bus_traffic_item.request.error is True,
                    )
                )
            ),
            "Publish DALI bus traffic to websocket",
        )
        if (
            not is_transmission_error_response(bus_traffic_item.response)
            and bus_traffic_item.response is not None
            and bus_traffic_item.response.raw_value is not None
        ):
            logger.debug(
                "WS >> daliMonitor (response): line=%d %sbits=%d %s",
                line,
                "FRAMING ERROR " if bus_traffic_item.response.raw_value.error else "",
                len(bus_traffic_item.response.raw_value),
                " ".join(f"{b:02x}" for b in bus_traffic_item.response.raw_value.as_byte_sequence),
            )
            one_shot_tasks.add(
                websocket.send(
                    json.dumps(
                        _msg_dali_monitor(
                            line,
                            len(bus_traffic_item.response.raw_value),
                            bus_traffic_item.response.raw_value.as_byte_sequence,
                            bus_traffic_item.response.raw_value.error is True,
                        )
                    )
                ),
                "Publish DALI bus traffic to websocket",
            )

    return _traffic_filter


async def process_request(path: str, _request_headers: Headers) -> Optional[HTTPResponse]:
    if path != "/":
        return (HTTPStatus.NOT_FOUND, [], "Not found".encode("utf-8"))
    return None


async def run_websocket(
    drivers: Sequence[WBDALIDriver],
    name: str,
    host: str,
    port: int,
    logger: logging.Logger,
) -> None:
    _log = logger.getChild("lunatone-iot-emulator")
    _log.info("Starting Lunatone IoT Gateway emulator on %s:%d", host, port)

    async def _handler(websocket: WebSocketServerProtocol, _path: str = "") -> None:
        await emulate(websocket, drivers, name, _log)

    try:
        async with serve(
            _handler,
            host,
            port,
            process_request=process_request,
        ):
            await asyncio.get_running_loop().create_future()
    except asyncio.CancelledError:
        _log.info("Lunatone IoT Gateway emulator stopped")
        raise
    except Exception as e:
        _log.error("Lunatone IoT Gateway emulator failed: %s", e)
        raise
