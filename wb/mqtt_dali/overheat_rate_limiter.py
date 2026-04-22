import asyncio
from typing import Callable, Optional

OVERHEAT_COOLDOWN_S = 10.0
OVERHEAT_RECOVERY_FIRST_INTERVAL_S = 1.0
OVERHEAT_RECOVERY_STEPS = 6


class OverheatRateLimiter:
    def __init__(
        self,
        cooldown_s: float = OVERHEAT_COOLDOWN_S,
        first_interval_s: float = OVERHEAT_RECOVERY_FIRST_INTERVAL_S,
        recovery_steps: int = OVERHEAT_RECOVERY_STEPS,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._cooldown_s = cooldown_s
        self._first_interval_s = first_interval_s
        self._recovery_steps = recovery_steps
        self._clock = clock

        self._cooldown_until = 0.0
        self._recovery_step = -1
        self._next_send_allowed = 0.0

    @property
    def recovery_step(self) -> int:
        return self._recovery_step

    def on_overheat(self, now: Optional[float] = None) -> None:
        current = self._now() if now is None else now
        self._cooldown_until = max(self._cooldown_until, current + self._cooldown_s)
        self._recovery_step = 0
        self._next_send_allowed = max(self._next_send_allowed, self._cooldown_until)

    def on_non_overheat_response(self) -> None:
        if self._recovery_step < 0:
            return
        if self._recovery_step < self._recovery_steps:
            self._recovery_step += 1

    def current_interval_s(self) -> float:
        if self._recovery_step <= 0:
            return 0.0
        if self._recovery_step >= self._recovery_steps:
            return 0.0
        ratio = (self._recovery_steps - self._recovery_step) / (self._recovery_steps - 1)
        return self._first_interval_s * ratio

    def required_delay_s(self, now: Optional[float] = None) -> float:
        current = self._now() if now is None else now
        earliest_allowed = max(self._next_send_allowed, self._cooldown_until)
        if current >= earliest_allowed:
            return 0.0
        return earliest_allowed - current

    async def wait_before_send(self) -> None:
        delay = self.required_delay_s()
        if delay > 0:
            await asyncio.sleep(delay)
        current = self._now()
        self._next_send_allowed = current + self.current_interval_s()

    def _now(self) -> float:
        if self._clock is not None:
            return self._clock()
        return asyncio.get_running_loop().time()
