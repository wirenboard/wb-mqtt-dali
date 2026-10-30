"""What the driver logs about the bus state and failed exchanges, and into which logger."""

import asyncio
import contextlib
import logging
import unittest
from typing import List, Optional
from unittest.mock import patch

import aiomqtt
from dali.command import Command
from dali.frame import ForwardFrame

from wb.mqtt_dali.bus_traffic import BusTrafficSource
from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher
from wb.mqtt_dali.overheat_rate_limiter import OverheatRateLimiter
from wb.mqtt_dali.wbdali import FramePriority, WBDALIConfig, WBDALIDriver

from ._broker_link_helpers import RecordingClient

BUS_LOGGER_NAME = "wb-dali_1_bus_1"
DEVICE_LOGGER_NAME = f"{BUS_LOGGER_NAME}.dali_5"

STATUS_NO_TRANSMISSION = 0x0000
STATUS_WITHOUT_RESPONSE = 0x0200
STATUS_BROKEN_RESPONSE = 0x0312
STATUS_NO_POWER = 0x0400
STATUS_OVERHEAT = 0x0500
STATUS_UNKNOWN = 0x0700


class _MockCommand(Command):
    def __init__(self):
        super().__init__(ForwardFrame(16, [0x12, 0x34]))
        self.sendtwice = False
        self.response = None


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _above_debug(records: List[logging.LogRecord]) -> List[tuple]:
    return [(r.name, r.levelno, r.getMessage()) for r in records if r.levelno > logging.DEBUG]


class TestBusStateLogging(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = RecordingClient()
        self.dispatcher = MQTTDispatcher(self.client)
        self.bus_logger = logging.getLogger(BUS_LOGGER_NAME)
        self.device_logger = self.bus_logger.getChild("dali_5")
        # No cooldown after an overheat reply, so the tests need not wait it out.
        no_cooldown = patch("wb.mqtt_dali.wbdali.OverheatRateLimiter", lambda: OverheatRateLimiter(0.0, 0.0))
        no_cooldown.start()
        self.addCleanup(no_cooldown.stop)
        self.driver = WBDALIDriver(WBDALIConfig(), self.dispatcher, self.bus_logger)
        self.driver.response_timeout = 30.0
        self._next_slot = 0

    async def asyncSetUp(self):
        dispatch_task = asyncio.create_task(self.dispatcher.run())
        self.addAsyncCleanup(_cancel, dispatch_task)
        await self.driver.initialize()
        self.addAsyncCleanup(self.driver.deinitialize)
        self.client.drain_publishes()

    async def test_no_power_is_logged_once_when_it_starts_and_once_when_it_ends(self):
        """Status 4 thrice, a normal reply, status 4 again: WARNING, INFO, WARNING in the bus logger."""
        await self._check_bus_state_transitions(
            STATUS_NO_POWER, "No power on DALI bus", "DALI bus power restored", "status 4"
        )

    async def test_overheat_is_logged_once_when_it_starts_and_once_when_it_ends(self):
        """Status 5 thrice, a normal reply, status 5 again: WARNING, INFO, WARNING in the bus logger."""
        await self._check_bus_state_transitions(
            STATUS_OVERHEAT, "DALI gateway overheat", "DALI gateway overheat is over", "status 5"
        )

    async def test_no_transmission_status_keeps_the_no_power_state(self):
        """Status 4, status 0, status 4: one WARNING, no INFO about power coming back."""
        await self._check_no_power_kept(STATUS_NO_TRANSMISSION)

    async def test_unknown_status_keeps_the_no_power_state(self):
        """Status 4, an unknown status, status 4: one WARNING, no INFO about power coming back."""
        await self._check_no_power_kept(STATUS_UNKNOWN)

    async def test_broken_and_unknown_status_and_timeout_are_logged_at_debug_into_the_sender_logger(self):
        """Status 3, an unknown status and a reply timeout: nothing above DEBUG, the command
        errors in the sender's logger."""
        with self.assertLogs(BUS_LOGGER_NAME, logging.DEBUG) as logs:
            await self._exchange(STATUS_BROKEN_RESPONSE, self.device_logger)
            await self._exchange(STATUS_UNKNOWN, self.device_logger)
            self.driver.response_timeout = 0.05
            await self._exchange(None, self.device_logger)

        self.assertEqual(_above_debug(logs.records), [])
        self.assertEqual(self._device_debug(logs.records, "status 3"), 1)
        self.assertEqual(self._device_debug(logs.records, "unknown status 7"), 1)
        self.assertEqual(self._device_debug(logs.records, "Timeout waiting for response"), 1)

    async def test_command_sent_with_the_bus_logger_is_logged_into_it(self):
        """A failed command sent with the bus logger, as commands with no device behind them are:
        its error in the bus logger itself, the bus name not repeated in a child logger."""
        with self.assertLogs(BUS_LOGGER_NAME, logging.DEBUG) as logs:
            await self._exchange(STATUS_BROKEN_RESPONSE, self.bus_logger)

        self.assertEqual({record.name for record in logs.records}, {BUS_LOGGER_NAME})
        self.assertTrue(any("status 3" in record.getMessage() for record in logs.records))

    async def _check_bus_state_transitions(
        self, bad_status: int, entered_message: str, left_message: str, command_error: str
    ) -> None:
        """Each command's error is expected only at DEBUG, in the sender's logger."""
        with self.assertLogs(BUS_LOGGER_NAME, logging.DEBUG) as logs:
            for status in (bad_status,) * 3 + (STATUS_WITHOUT_RESPONSE, bad_status):
                await self._exchange(status, self.device_logger)

        self.assertEqual(
            _above_debug(logs.records),
            [
                (BUS_LOGGER_NAME, logging.WARNING, entered_message),
                (BUS_LOGGER_NAME, logging.INFO, left_message),
                (BUS_LOGGER_NAME, logging.WARNING, entered_message),
            ],
        )
        self.assertEqual(self._device_debug(logs.records, command_error), 4)

    async def _check_no_power_kept(self, neutral_status: int) -> None:
        with self.assertLogs(BUS_LOGGER_NAME, logging.DEBUG) as logs:
            for status in (STATUS_NO_POWER, neutral_status, STATUS_NO_POWER):
                await self._exchange(status, self.device_logger)

        self.assertEqual(
            _above_debug(logs.records), [(BUS_LOGGER_NAME, logging.WARNING, "No power on DALI bus")]
        )

    async def _exchange(self, status_word: Optional[int], logger: logging.Logger) -> None:
        """One command answered with ``status_word`` in its slot, or left unanswered if None."""
        task = asyncio.create_task(
            self.driver.send(_MockCommand(), BusTrafficSource.WB, FramePriority.USER_ACTION, logger)
        )
        await self.client.wait_for_publish(f"/rpc/v1/wb-mqtt-serial/port/Load/{self.driver.rpc_client_id}")
        if status_word is not None:
            topic = (
                f"/devices/{self.driver.config.device_name}/controls/bus_1_bulk_send_reply_{self._next_slot}"
            )
            self.client.messages.deliver(aiomqtt.Message(topic, str(status_word), 0, False, 0, None))
        self._next_slot += 1
        await asyncio.wait_for(task, 1.0)

    @staticmethod
    def _device_debug(records: List[logging.LogRecord], text: str) -> int:
        return sum(
            record.name == DEVICE_LOGGER_NAME
            and record.levelno == logging.DEBUG
            and text in record.getMessage()
            for record in records
        )
