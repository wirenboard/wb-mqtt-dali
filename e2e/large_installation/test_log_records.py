import json
import unittest
from typing import Optional

from e2e.large_installation.log_records import (
    LogLevel,
    message_class,
    record_from_journal_json,
)


def journal_line(message: str, fields: Optional[dict[str, str]] = None) -> str:
    return json.dumps(
        {"MESSAGE": message, "PRIORITY": "6", "SYSLOG_IDENTIFIER": "wb-mqtt-dali", **(fields or {})}
    )


class NormalisationTest(unittest.TestCase):
    def classes(self, messages: list[str]) -> set[str]:
        return {message_class(record_from_journal_json(journal_line(m))) for m in messages}

    def test_numbers_addresses_and_command_lists_become_placeholders(self):
        """Two retry warnings that differ in bus, address, commands and attempts fall into one class."""
        self.assertEqual(
            self.classes(
                [
                    "WARNING:gw_bus_2.dali_5:DALI ['QueryStatus(<address (control gear) 5>)'] failed after 3 "
                    "attempts: no response from gateway",
                    "WARNING:gw_bus_3.dali_7:DALI ['QueryActualLevel(<address (control gear) 7>)', 'X(1)'] "
                    "failed after 2 attempts: no response from gateway",
                ]
            ),
            {"WARNING:DALI <cmds> failed after N attempts: no response from gateway"},
        )

    def test_device_names_and_commands_are_dropped(self):
        """Init failures of differently named gear on different queries fall into one class."""
        self.assertEqual(
            self.classes(
                [
                    "WARNING:gw_bus_2.dali_1:Failed to initialize device Some Lamp 1/2 1: No response to "
                    "QueryDeviceType(<address (control gear) 1>) after 3 attempts",
                    "WARNING:gw_bus_2.dali_9:Failed to initialize device DALI 9: No response to "
                    "QueryNextDeviceType(<address (control gear) 9>) after 3 attempts",
                ]
            ),
            {"WARNING:Failed to initialize device <name>: No response to <cmd> after N attempts"},
        )

    def test_python_prefix_gives_level_and_logger(self):
        """A `LEVEL:logger:message` line yields its level and logger."""
        record = record_from_journal_json(
            journal_line("ERROR:gw_bus_3:Failed to parse reply payload 'b''' x")
        )
        self.assertEqual((record.level, record.logger), (LogLevel.ERROR, "gw_bus_3"))

    def test_journal_json_with_logger_field(self):
        """A journald JSON entry of the JournalHandler gives level and logger from its fields."""
        record = record_from_journal_json(
            journal_line(
                "Received response for unknown pointer: 4",
                {"PRIORITY": "4", "LOGGER": "wb-dali_1_bus_2"},
            )
        )
        self.assertEqual((record.level, record.logger), (LogLevel.WARNING, "wb-dali_1_bus_2"))
        self.assertEqual(message_class(record), "WARNING:Received response for unknown pointer: N")
