from dataclasses import dataclass
from typing import Optional

INIT_RETRY_INITIAL_DELAY = 5.0
INIT_RETRY_MULTIPLIER = 2.0
INIT_RETRY_MAX_DELAY = 60.0


@dataclass
class DeviceInitState:
    next_retry_time: float = 0.0
    retry_delay: float = 0.0

    def has_failed(self) -> bool:
        return self.retry_delay > 0.0


class DeviceInitScheduler:
    def __init__(self) -> None:
        self._pending: dict[str, DeviceInitState] = {}

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
            if not state.has_failed() and state.next_retry_time <= current_time
        ]

    def get_one_retry_ready(self, current_time: float) -> Optional[str]:
        for mqtt_id, state in self._pending.items():
            if state.has_failed() and state.next_retry_time <= current_time:
                return mqtt_id
        return None

    def record_success(self, mqtt_id: str) -> None:
        self._pending.pop(mqtt_id, None)

    def record_failure(self, mqtt_id: str, current_time: float) -> None:
        state = self._pending.get(mqtt_id)
        if state is None:
            return
        state.retry_delay = min(
            state.retry_delay * INIT_RETRY_MULTIPLIER if state.has_failed() else INIT_RETRY_INITIAL_DELAY,
            INIT_RETRY_MAX_DELAY,
        )
        state.next_retry_time = current_time + state.retry_delay

    def clear(self) -> None:
        self._pending.clear()
