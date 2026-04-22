import unittest

from wb.mqtt_dali.overheat_rate_limiter import (
    OVERHEAT_RECOVERY_FIRST_INTERVAL_S,
    OverheatRateLimiter,
)


class FakeClock:  # pylint: disable=too-few-public-methods
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class TestOverheatRateLimiter(unittest.TestCase):
    def test_initial_state(self):
        clock = FakeClock()
        limiter = OverheatRateLimiter(clock=clock)

        self.assertEqual(limiter.recovery_step, -1)
        self.assertEqual(limiter.current_interval_s(), 0.0)
        self.assertEqual(limiter.required_delay_s(), 0.0)

    def test_on_overheat_sets_cooldown_and_recovery(self):
        clock = FakeClock(100.0)
        limiter = OverheatRateLimiter(clock=clock)

        limiter.on_overheat()

        self.assertEqual(limiter.recovery_step, 0)
        self.assertAlmostEqual(limiter.required_delay_s(), 10.0)

    def test_recovery_has_six_steps(self):
        clock = FakeClock(0.0)
        limiter = OverheatRateLimiter(clock=clock)
        limiter.on_overheat()

        expected = [1.0, 0.8, 0.6, 0.4, 0.2, 0.0]
        for ratio in expected:
            limiter.on_non_overheat_response()
            self.assertAlmostEqual(
                limiter.current_interval_s(),
                OVERHEAT_RECOVERY_FIRST_INTERVAL_S * ratio,
            )

    def test_non_overheat_response_without_overheat_does_not_change_state(self):
        clock = FakeClock(0.0)
        limiter = OverheatRateLimiter(clock=clock)

        limiter.on_non_overheat_response()

        self.assertEqual(limiter.recovery_step, -1)
        self.assertEqual(limiter.current_interval_s(), 0.0)

    def test_repeated_overheat_extends_cooldown(self):
        clock = FakeClock(0.0)
        limiter = OverheatRateLimiter(clock=clock)

        limiter.on_overheat()
        clock.now = 5.0
        limiter.on_overheat()

        self.assertAlmostEqual(limiter.required_delay_s(), 10.0)
