import unittest

from e2e.large_installation.e2e_large_installation import (
    DEFAULT_PROFILE,
    IterationResult,
    Scenario,
    StartupTraffic,
    parse_args,
)
from e2e.large_installation.log_records import DALI_IDENTIFIER, LogLevel, LogRecord


def result(**changes) -> IterationResult:
    values = {
        "label": "x",
        "startup_s": 50.0,
        "state_violations": [],
        "log_violations": [],
        "startup_over_limit": False,
        "restarts": [],
        "traffic": StartupTraffic(0.0, []),
    }
    values.update(changes)
    return IterationResult(**values)


class IterationResultTest(unittest.TestCase):
    def test_clean_iteration_passes(self):
        """A full start without violations, restarts or a startup limit passes."""
        self.assertFalse(result().failed)

    def test_each_criterion_fails_the_iteration(self):
        """No full start, a startup over the limit, a state mismatch, a warning or a service restart."""
        warning = LogRecord(DALI_IDENTIFIER, LogLevel.WARNING, "", "x")
        for changes in (
            {"startup_s": None},
            {"restarts": ["wb-mqtt-serial restarted during the iteration (restarts 0 -> 1)"]},
            {"startup_over_limit": True},
            {"state_violations": ["bus 1: x"]},
            {"log_violations": [warning]},
        ):
            with self.subTest(changes=changes):
                self.assertTrue(result(**changes).failed)


class ParseArgsTest(unittest.TestCase):
    def test_defaults_and_overrides(self):
        """--scenario is required; site is the default profile and --profile overrides it."""
        with self.assertRaises(SystemExit):
            parse_args(["x"])
        args = parse_args(["x", "--scenario", "cold-start"])
        self.assertEqual(
            (args.scenario, args.profile, args.max_startup_time), (Scenario.COLD_START, DEFAULT_PROFILE, None)
        )
        args = parse_args(["x", "--scenario", "rpc-flood", "--profile", "mine"])
        self.assertEqual((args.scenario, args.profile), (Scenario.RPC_FLOOD, "mine"))
