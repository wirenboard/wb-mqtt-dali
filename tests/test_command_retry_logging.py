"""What a retried DALI command leaves in the sender's logger."""

import logging
import unittest
from typing import List
from unittest.mock import MagicMock

from dali.address import GearBroadcast, GearShort
from dali.command import Response
from dali.frame import BackwardFrame
from dali.gear.general import Off, QueryActualLevel, RecallMaxLevel

from wb.mqtt_dali.common_dali_device import read_product_name
from wb.mqtt_dali.dali_compat import DaliCommandsCompatibilityLayer
from wb.mqtt_dali.settings import BooleanSettingsParam, SettingsParamName
from wb.mqtt_dali.wbdali_error_response import NoPowerOnBus
from wb.mqtt_dali.wbdali_utils import (
    query_response,
    query_responses_retry_from_first_failed,
    query_responses_retry_only_failed,
    send_commands_with_retry,
    send_with_retry,
)

SENDER_LOGGER = logging.getLogger("wb-dali_1_bus_1.dali_5")


class _ScriptedDriver:
    """Answers each send with the next scripted response, whatever the command."""

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.sent_count = 0

    async def send(self, cmd, source=None, priority=None, logger=None):
        return (await self.send_commands([cmd], source, priority, logger))[0]

    async def send_commands(self, cmds, source=None, priority=None, logger=None):
        del source, priority, logger
        self.sent_count += len(cmds)
        return [self._responses.pop(0) for _ in cmds]


def _level(value: int):
    return QueryActualLevel.response(BackwardFrame(value))


def _above_debug(records: List[logging.LogRecord]) -> List[str]:
    return [record.getMessage() for record in records if record.levelno > logging.DEBUG]


class TestCommandRetryLogging(unittest.IsolatedAsyncioTestCase):
    async def test_success_on_a_later_attempt_is_logged_only_at_debug(self):
        """A query and a command each fail once and then go through: only DEBUG records."""
        query = QueryActualLevel(GearShort(5))
        driver = _ScriptedDriver([NoPowerOnBus(), _level(10), NoPowerOnBus(), _level(0)])

        with self.assertLogs(SENDER_LOGGER, logging.DEBUG) as logs:
            self.assertEqual((await query_response(driver, query, SENDER_LOGGER)).raw_value.as_integer, 10)
            await send_with_retry(driver, Off(GearShort(5)), SENDER_LOGGER)

        self.assertEqual(_above_debug(logs.records), [])

    async def test_raising_helpers_leave_the_failure_to_the_operation(self):
        """Every attempt fails in the helpers that raise: they raise and log nothing above DEBUG."""
        query = QueryActualLevel(GearShort(5))
        driver = _ScriptedDriver([NoPowerOnBus()] * 12)

        with self.assertLogs(SENDER_LOGGER, logging.DEBUG) as logs:
            with self.assertRaises(RuntimeError):
                await query_response(driver, query, SENDER_LOGGER)
            with self.assertRaises(RuntimeError):
                await query_responses_retry_only_failed(driver, [query], SENDER_LOGGER)
            with self.assertRaises(RuntimeError):
                await query_responses_retry_from_first_failed(driver, [query], 1, SENDER_LOGGER)

        self.assertEqual(_above_debug(logs.records), [])

    async def test_command_failing_every_attempt_leaves_one_warning(self):
        """A command fails all three attempts and the caller goes on: one WARNING with the command
        and the last error."""
        driver = _ScriptedDriver([NoPowerOnBus()] * 3)

        with self.assertLogs(SENDER_LOGGER, logging.DEBUG) as logs:
            response = await send_with_retry(driver, Off(GearShort(5)), SENDER_LOGGER)

        self.assertIsInstance(response, NoPowerOnBus)
        warnings = _above_debug(logs.records)
        self.assertEqual(len(warnings), 1)
        self.assertIn(str(Off(GearShort(5))), warnings[0])
        self.assertIn("no power on bus", warnings[0])

    async def test_batch_failing_every_attempt_leaves_one_warning(self):
        """A batch of two fails all three attempts and the caller goes on: one WARNING naming both
        commands and the last error."""
        commands = [Off(GearShort(5)), RecallMaxLevel(GearShort(5))]
        driver = _ScriptedDriver([NoPowerOnBus()] * 6)

        with self.assertLogs(SENDER_LOGGER, logging.DEBUG) as logs:
            await send_commands_with_retry(driver, commands, SENDER_LOGGER)

        warnings = _above_debug(logs.records)
        self.assertEqual(len(warnings), 1)
        for command in commands:
            self.assertIn(str(command), warnings[0])
        self.assertIn("no power on bus", warnings[0])

    async def test_gtin_read_failing_every_attempt_leaves_the_retry_and_the_operation_messages(self):
        """The GTIN batch gets no power on every attempt: the retry WARNING, then read_product_name's."""
        driver = _ScriptedDriver([NoPowerOnBus()] * 24)

        with self.assertLogs(SENDER_LOGGER, logging.DEBUG) as logs:
            name = await read_product_name(
                driver, GearShort(5), DaliCommandsCompatibilityLayer(), MagicMock(), SENDER_LOGGER
            )

        self.assertIsNone(name)
        warnings = _above_debug(logs.records)
        self.assertEqual(len(warnings), 2)
        self.assertIn("failed after 3 attempts", warnings[0])
        self.assertIn("looking up product name", warnings[1])
        self.assertIn("no power on bus", warnings[1].lower())

    async def test_silent_gtin_bank_is_read_once(self):
        """The gear answers no GTIN read: the batch is not retried, and read_product_name gives up
        with its own WARNING only."""
        driver = _ScriptedDriver([Response(None)] * 24)

        with self.assertLogs(SENDER_LOGGER, logging.DEBUG) as logs:
            name = await read_product_name(
                driver, GearShort(5), DaliCommandsCompatibilityLayer(), MagicMock(), SENDER_LOGGER
            )

        self.assertIsNone(name)
        self.assertEqual(driver.sent_count, 8)
        self.assertEqual(len(_above_debug(logs.records)), 1)

    async def test_parameter_write_failing_every_attempt_leaves_the_failure_to_the_operation(self):
        """A single-device write gets no power on every attempt: the write raises with the error
        and logs nothing above DEBUG."""
        driver = _ScriptedDriver([NoPowerOnBus()] * 3)

        with self.assertLogs(SENDER_LOGGER, logging.DEBUG) as logs:
            with self.assertRaisesRegex(RuntimeError, "no power on bus"):
                await _boolean_param().write(driver, GearShort(5), {"flag": True}, SENDER_LOGGER)

        self.assertEqual(_above_debug(logs.records), [])

    async def test_broadcast_parameter_write_failing_every_attempt_leaves_one_warning(self):
        """A broadcast write, which reads nothing back, gets no power on every attempt: it goes on
        and leaves one WARNING."""
        driver = _ScriptedDriver([NoPowerOnBus()] * 3)

        with self.assertLogs(SENDER_LOGGER, logging.DEBUG) as logs:
            self.assertEqual(
                await _boolean_param().write(driver, GearBroadcast(), {"flag": True}, SENDER_LOGGER), {}
            )

        self.assertEqual(len(_above_debug(logs.records)), 1)


def _boolean_param() -> BooleanSettingsParam:
    return BooleanSettingsParam(SettingsParamName("Flag"), "flag", QueryActualLevel, RecallMaxLevel, Off)
