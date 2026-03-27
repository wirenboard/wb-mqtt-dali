from dataclasses import dataclass
from typing import Optional

INIT_RETRY_INITIAL_DELAY = 5.0
INIT_RETRY_MULTIPLIER = 2.0
INIT_RETRY_MAX_DELAY = 60.0


@dataclass
class DeviceInitState:
    next_retry_time: float = 0.0
    retry_count: int = 0


class DeviceInitScheduler:
    def __init__(
        self,
        initial_delay: float = INIT_RETRY_INITIAL_DELAY,
        multiplier: float = INIT_RETRY_MULTIPLIER,
        max_delay: float = INIT_RETRY_MAX_DELAY,
    ) -> None:
        self._pending: dict[str, DeviceInitState] = {}
        self._initial_delay = initial_delay
        self._multiplier = multiplier
        self._max_delay = max_delay

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    def schedule(self, mqtt_id: str, current_time: float, delay: float = 0.0) -> None:
        if mqtt_id not in self._pending:
            self._pending[mqtt_id] = DeviceInitState(next_retry_time=current_time + delay)

    def remove(self, mqtt_id: str) -> None:
        self._pending.pop(mqtt_id, None)

    def get_first_attempt_ready(self, current_time: float) -> list[str]:
        return [
            mqtt_id
            for mqtt_id, state in self._pending.items()
            if state.retry_count == 0 and state.next_retry_time <= current_time
        ]

    def get_one_retry_ready(self, current_time: float) -> Optional[str]:
        for mqtt_id, state in self._pending.items():
            if state.retry_count > 0 and state.next_retry_time <= current_time:
                return mqtt_id
        return None

    def record_success(self, mqtt_id: str) -> None:
        self._pending.pop(mqtt_id, None)

    def record_failure(self, mqtt_id: str, current_time: float) -> float:
        state = self._pending.get(mqtt_id)
        if state is None:
            return 0.0
        state.retry_count += 1
        delay = min(
            self._initial_delay * (self._multiplier ** (state.retry_count - 1)),
            self._max_delay,
        )
        state.next_retry_time = current_time + delay
        return delay

    def get_retry_count(self, mqtt_id: str) -> int:
        state = self._pending.get(mqtt_id)
        return state.retry_count if state is not None else 0

    def clear(self) -> None:
        self._pending.clear()
