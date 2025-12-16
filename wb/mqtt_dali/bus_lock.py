import asyncio
from typing import Optional

QUIESCENT_MODE_TIMEOUT = 60 * 15  # 15 minutes


class BusLock:
    """
    Manages exclusive access to the DALI bus.
    """

    def __init__(self):
        self._lock: asyncio.Lock = asyncio.Lock()
        self._in_quiescent_mode: bool = False
        self._quiescent_mode_timer: Optional[asyncio.TimerHandle] = None

    async def __aenter__(self):
        await asyncio.wait_for(self._lock.acquire(), timeout=1.0)

    async def __aexit__(self, exc_type, exc, tb):
        self._lock.release()

    async def start_quiescent_mode(self) -> None:
        if not self._in_quiescent_mode:
            await self._lock.acquire()
            self._quiescent_mode_timer = asyncio.get_event_loop().call_later(
                QUIESCENT_MODE_TIMEOUT, self.stop_quiescent_mode
            )
            self._in_quiescent_mode = True
        else:
            if self._quiescent_mode_timer:
                self._quiescent_mode_timer.cancel()
            self._quiescent_mode_timer = asyncio.get_event_loop().call_later(
                QUIESCENT_MODE_TIMEOUT, self.stop_quiescent_mode
            )

    def stop_quiescent_mode(self) -> None:
        if self._in_quiescent_mode:
            if self._quiescent_mode_timer:
                self._quiescent_mode_timer.cancel()
                self._quiescent_mode_timer = None
            self._lock.release()
            self._in_quiescent_mode = False
