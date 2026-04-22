from dataclasses import dataclass, field
from typing import Any, Optional

import pytest

from wb.mqtt_dali.bus_traffic import BusTrafficSource
from wb.mqtt_dali.wbdali_error_response import WbGatewayTransmissionError
from wb.mqtt_dali.wbdali_utils import (
    query_response,
    query_responses_retry_from_first_failed,
    query_responses_retry_only_failed,
    send_commands_with_retry,
    send_with_retry,
)


@dataclass
class FakeCommand:
    name: str = "_FakeCommand"

    def __str__(self) -> str:
        return self.name


@dataclass
class FakeQueryCommand(FakeCommand):
    response: object = field(default_factory=object)


@dataclass
class FakeLogger:
    messages: list[str] = field(default_factory=list)

    def warning(self, message: str, *args: object) -> None:
        self.messages.append(message % args)


LOGGER = FakeLogger()


@dataclass
class FakeDriver:
    send_results: list[Any] = field(default_factory=list)
    send_commands_results: list[list[Any]] = field(default_factory=list)
    send_calls: int = 0
    send_commands_calls: int = 0
    last_send_cmd: Optional[FakeCommand] = None
    last_send_commands: Optional[list[FakeCommand]] = None
    send_commands_history: list[list[FakeCommand]] = field(default_factory=list)

    async def send(self, cmd, source=BusTrafficSource.WB):  # pylint: disable=unused-argument
        self.send_calls += 1
        self.last_send_cmd = cmd
        result = self.send_results[self.send_calls - 1]
        return result

    async def send_commands(self, commands, source=BusTrafficSource.WB):  # pylint: disable=unused-argument
        self.send_commands_calls += 1
        self.last_send_commands = commands
        self.send_commands_history.append(list(commands))
        return self.send_commands_results[self.send_commands_calls - 1]


@dataclass
class FakeRawValue:
    error: bool = False
    as_integer: int = 0


@dataclass
class FakeResponse:
    raw_value: FakeRawValue = field(default_factory=FakeRawValue)
    value: Any = 1
    _expected: bool = True
    _error_acceptable: bool = False


@pytest.mark.asyncio
async def test_send_with_retry_success_first_attempt():
    cmd = FakeCommand()
    driver = FakeDriver(send_results=["ok"])

    result = await send_with_retry(driver, cmd, LOGGER)

    assert result == "ok"
    assert driver.send_calls == 1
    assert driver.last_send_cmd is cmd


@pytest.mark.asyncio
async def test_send_with_retry_retries_then_succeeds():
    cmd = FakeCommand()
    driver = FakeDriver(send_results=[WbGatewayTransmissionError(), WbGatewayTransmissionError(), "ok"])
    logger = FakeLogger()

    result = await send_with_retry(driver, cmd, logger)

    assert result == "ok"
    assert driver.send_calls == 3
    assert len(logger.messages) == 2


@pytest.mark.asyncio
async def test_send_with_retry_returns_last_error_after_retries():
    cmd = FakeCommand()
    driver = FakeDriver(
        send_results=[
            WbGatewayTransmissionError(),
            WbGatewayTransmissionError(),
            WbGatewayTransmissionError(),
        ]
    )

    result = await send_with_retry(driver, cmd, LOGGER)

    assert isinstance(result, WbGatewayTransmissionError)
    assert driver.send_calls == 3


@pytest.mark.asyncio
async def test_send_commands_with_retry_success_first_attempt():
    commands = [FakeCommand("c1"), FakeCommand("c2")]
    driver = FakeDriver(send_commands_results=[["a", "b"]])

    result = await send_commands_with_retry(driver, commands, LOGGER)

    assert result == ["a", "b"]
    assert driver.send_commands_calls == 1
    assert driver.last_send_commands is commands


@pytest.mark.asyncio
async def test_send_commands_with_retry_retries_whole_batch():
    commands = [FakeCommand("c1"), FakeCommand("c2"), FakeCommand("c3")]
    driver = FakeDriver(
        send_commands_results=[["ok", WbGatewayTransmissionError(), "ok"], ["ok", "ok", "ok"]]
    )
    logger = FakeLogger()

    result = await send_commands_with_retry(driver, commands, logger)

    assert result == ["ok", "ok", "ok"]
    assert driver.send_commands_calls == 2
    assert len(logger.messages) == 1


@pytest.mark.asyncio
async def test_send_commands_with_retry_returns_last_responses_after_retries():
    commands = [FakeCommand("c1")]
    driver = FakeDriver(
        send_commands_results=[
            [WbGatewayTransmissionError()],
            [WbGatewayTransmissionError()],
            [WbGatewayTransmissionError()],
        ]
    )

    result = await send_commands_with_retry(driver, commands, LOGGER)

    assert len(result) == 1
    assert isinstance(result[0], WbGatewayTransmissionError)
    assert driver.send_commands_calls == 3


@pytest.mark.asyncio
async def test_query_response_retries_after_transmission_error():
    cmd = FakeCommand("query")
    response = FakeResponse(raw_value=FakeRawValue(error=False, as_integer=10), value=10)
    driver = FakeDriver(send_results=[WbGatewayTransmissionError(), WbGatewayTransmissionError(), response])
    logger = FakeLogger()

    result = await query_response(driver, cmd, logger)

    assert result is response
    assert driver.send_calls == 3
    assert len(logger.messages) == 2


@pytest.mark.asyncio
async def test_query_response_retries_after_check_query_error():
    cmd = FakeQueryCommand("query")
    response = FakeResponse(raw_value=FakeRawValue(error=False, as_integer=11), value=11)
    driver = FakeDriver(send_results=[None, None, response])
    logger = FakeLogger()

    result = await query_response(driver, cmd, logger)

    assert result is response
    assert driver.send_calls == 3
    assert len(logger.messages) == 2


@pytest.mark.asyncio
async def test_query_response_raises_after_retries_exhausted():
    cmd = FakeQueryCommand("query")
    driver = FakeDriver(send_results=[None, None, None])

    with pytest.raises(RuntimeError, match="Error in response"):
        await query_response(driver, cmd, LOGGER)
    assert driver.send_calls == 3


@pytest.mark.asyncio
async def test_query_responses_retry_from_first_failed_retries_tail_only():
    commands = [FakeCommand("c1"), FakeCommand("c2"), FakeCommand("c3")]
    driver = FakeDriver(
        send_commands_results=[
            ["ok1", WbGatewayTransmissionError(), "ok3_should_not_be_used"],
            ["ok2", "ok3"],
        ]
    )

    result = await query_responses_retry_from_first_failed(driver, commands, logger=LOGGER)

    assert result == ["ok1", "ok2", "ok3"]
    assert driver.send_commands_history == [commands, commands[1:]]


@pytest.mark.asyncio
async def test_query_responses_retry_from_first_failed_raises_after_exhausted():
    commands = [FakeCommand("c1"), FakeCommand("c2")]
    driver = FakeDriver(
        send_commands_results=[
            ["ok1", WbGatewayTransmissionError()],
            [WbGatewayTransmissionError()],
            [WbGatewayTransmissionError()],
        ]
    )

    with pytest.raises(RuntimeError, match="retry-from-first failed"):
        await query_responses_retry_from_first_failed(driver, commands, logger=LOGGER)


@pytest.mark.asyncio
async def test_query_responses_retry_from_first_failed_empty_commands_returns_empty():
    driver = FakeDriver(send_commands_results=[])

    result = await query_responses_retry_from_first_failed(driver, [], logger=LOGGER)

    assert result == []
    assert driver.send_commands_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_size", [0, -1])
async def test_query_responses_retry_from_first_failed_invalid_batch_size_raises(batch_size):
    commands = [FakeCommand("c1")]
    driver = FakeDriver(send_commands_results=[["ok1"]])

    with pytest.raises(ValueError, match="positive integer"):
        await query_responses_retry_from_first_failed(driver, commands, batch_size=batch_size, logger=LOGGER)


@pytest.mark.asyncio
async def test_query_responses_retry_from_first_failed_batch_rounding_retries_from_group_start():
    commands = [FakeCommand("c1"), FakeCommand("c2"), FakeCommand("c3"), FakeCommand("c4")]
    driver = FakeDriver(
        send_commands_results=[
            ["ok1", "ok2", "ok3", WbGatewayTransmissionError()],
            ["ok3", "ok4"],
        ]
    )

    result = await query_responses_retry_from_first_failed(driver, commands, batch_size=2, logger=LOGGER)

    assert result == ["ok1", "ok2", "ok3", "ok4"]
    assert driver.send_commands_history == [commands, commands[2:]]
    retry_command_names = [str(cmd) for cmd in driver.send_commands_history[1]]
    assert retry_command_names == ["c3", "c4"]


@pytest.mark.asyncio
async def test_query_responses_retry_only_failed_retries_sparse_indexes():
    commands = [FakeCommand("c1"), FakeCommand("c2"), FakeCommand("c3"), FakeCommand("c4")]
    driver = FakeDriver(
        send_commands_results=[
            ["ok1", WbGatewayTransmissionError(), "ok3", WbGatewayTransmissionError()],
            ["ok2", "ok4"],
        ]
    )

    result = await query_responses_retry_only_failed(driver, commands, LOGGER)

    assert result == ["ok1", "ok2", "ok3", "ok4"]
    assert driver.send_commands_history == [commands, [commands[1], commands[3]]]


@pytest.mark.asyncio
async def test_query_responses_retry_only_failed_empty_commands_returns_empty():
    driver = FakeDriver(send_commands_results=[])

    result = await query_responses_retry_only_failed(driver, [], LOGGER)

    assert result == []
    assert driver.send_commands_calls == 0


@pytest.mark.asyncio
async def test_query_responses_retry_only_failed_uses_check_query_response_for_query_commands():
    commands = [FakeQueryCommand("q1"), FakeCommand("c2")]
    bad_response = FakeResponse(raw_value=FakeRawValue(error=True, as_integer=0), value=0)
    ok_response = FakeResponse(raw_value=FakeRawValue(error=False, as_integer=5), value=5)
    driver = FakeDriver(send_commands_results=[[bad_response, "ok2"], [ok_response]])

    result = await query_responses_retry_only_failed(driver, commands, LOGGER)

    assert result == [ok_response, "ok2"]
    assert driver.send_commands_history == [commands, [commands[0]]]


@pytest.mark.asyncio
async def test_query_responses_retry_only_failed_raises_after_exhausted():
    commands = [FakeQueryCommand("q1")]
    bad_response = FakeResponse(raw_value=FakeRawValue(error=True, as_integer=0), value=0)
    driver = FakeDriver(send_commands_results=[[bad_response], [bad_response], [bad_response]])

    with pytest.raises(RuntimeError, match="retry-only-failed failed"):
        await query_responses_retry_only_failed(driver, commands, LOGGER)
