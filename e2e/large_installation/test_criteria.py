import json
import unittest

from e2e.large_installation.criteria import (
    ReportedDevice,
    bus_state_violations,
    log_violations,
    violation_classes,
)
from e2e.large_installation.installation_profile import (
    ExpectedDevice,
    GearModel,
    GearSpec,
)
from e2e.large_installation.log_records import (
    DALI_IDENTIFIER,
    SERIAL_IDENTIFIER,
    LogLevel,
    LogRecord,
    record_from_journal_json,
)

GEAR = (GearSpec(3, 0, GearModel.PLAIN, frozenset({1, 4})),)
DEVICES = [
    ExpectedDevice(1, 3, False, "wb-dali_5_bus_1_3", "DALI 3"),
    ExpectedDevice(1, 4, True, "wb-dali_5_bus_1_dali2_4", "DALI-2 4"),
]


def reported(groups=frozenset({1, 4})) -> list[ReportedDevice]:
    """As `Editor/GetList` lists them: DALI-2 devices report no groups."""
    return [ReportedDevice("DALI 3", groups), ReportedDevice("DALI-2 4", frozenset())]


class DeviceStateTest(unittest.TestCase):
    def test_matching_bus_has_no_violations(self):
        """Gear groups and bus groups (gear groups plus the configured 7) agree."""
        self.assertEqual(bus_state_violations(1, reported(), {1, 4, 7}, GEAR, {7}, DEVICES), [])

    def test_group_mismatch_on_a_gear_and_on_the_bus(self):
        """Gear 3 reported without group 4 gives a gear and a bus group mismatch."""
        violations = bus_state_violations(1, reported(groups=frozenset({1})), {1}, GEAR, set(), DEVICES)
        self.assertEqual(len(violations), 2)
        self.assertIn("gear 3: groups", violations[0])
        self.assertIn("group devices", violations[1])

    def test_unknown_device_is_reported(self):
        extra = [*reported(), ReportedDevice("Kitchen", frozenset())]
        self.assertIn(
            "unexpected device 'Kitchen'", bus_state_violations(1, extra, {1, 4}, GEAR, set(), DEVICES)[0]
        )


def record(identifier: str, message: str, level: LogLevel = LogLevel.WARNING) -> LogRecord:
    return LogRecord(identifier, level, "", message)


class SerialJournalEntryTest(unittest.TestCase):
    """wb-mqtt-serial writes its level into the message and logs everything at journald priority 6."""

    def entry(self, message: str) -> LogRecord:
        return record_from_journal_json(
            json.dumps(
                {
                    "MESSAGE": message,
                    "PRIORITY": "6",
                    "SYSLOG_IDENTIFIER": SERIAL_IDENTIFIER,
                }
            )
        )

    def test_warning_prefix_sets_the_level_and_is_stripped(self):
        """The disconnect warning keeps its text and counts as a warning."""
        parsed = self.entry("WARNING: [serial device] device modbus:234 is disconnected")
        self.assertEqual(
            (parsed.level, parsed.message),
            (LogLevel.WARNING, "[serial device] device modbus:234 is disconnected"),
        )
        self.assertEqual(log_violations([parsed]), [parsed])

    def test_error_prefix(self):
        parsed = self.entry("ERROR: [serial client] Error while running task: x")
        self.assertEqual(
            (parsed.level, parsed.message), (LogLevel.ERROR, "[serial client] Error while running task: x")
        )


class LogViolationsTest(unittest.TestCase):
    def test_every_warning_and_worse_counts(self):
        """INFO does not count; a WARNING of either service and an ERROR do."""
        records = [
            record(
                DALI_IDENTIFIER,
                "Gateway reported reachable; queue resync deferred to next batch",
                level=LogLevel.INFO,
            ),
            record(DALI_IDENTIFIER, "Received response for unknown pointer: 3"),
            record(SERIAL_IDENTIFIER, "[serial device] device modbus:234 is disconnected"),
            record(SERIAL_IDENTIFIER, "[serial client] Error while running task: x", level=LogLevel.ERROR),
        ]
        self.assertEqual(log_violations(records), records[1:])


class ViolationClassesTest(unittest.TestCase):
    def test_records_are_grouped_by_service_and_class(self):
        """Unknown-pointer warnings of two slots fall in one class; the most frequent class comes first."""
        records = [
            record(DALI_IDENTIFIER, "Received response for unknown pointer: 3"),
            record(SERIAL_IDENTIFIER, "[serial device] device modbus:234 is disconnected"),
            record(DALI_IDENTIFIER, "Received response for unknown pointer: 7"),
        ]
        self.assertEqual(
            [tuple(group) for group in violation_classes(records)],
            [
                (DALI_IDENTIFIER, "WARNING:Received response for unknown pointer: N", 2),
                (SERIAL_IDENTIFIER, "WARNING:[serial device] device modbus:N is disconnected", 1),
            ],
        )
