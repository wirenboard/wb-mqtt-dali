"""An event loop whose clock jumps to the next timer whenever no callback is ready."""

import asyncio
import selectors
import unittest
from typing import Callable, Optional


class _ClockAdvancingSelector(selectors.BaseSelector):
    def __init__(self, advance: Callable[[float], None]) -> None:
        self._selector = selectors.DefaultSelector()
        self._advance = advance

    def register(self, fileobj, events, data=None) -> selectors.SelectorKey:
        return self._selector.register(fileobj, events, data)

    def unregister(self, fileobj) -> selectors.SelectorKey:
        return self._selector.unregister(fileobj)

    def modify(self, fileobj, events, data=None) -> selectors.SelectorKey:
        return self._selector.modify(fileobj, events, data)

    def select(self, timeout: Optional[float] = None) -> list[tuple[selectors.SelectorKey, int]]:
        if timeout is None:
            return self._selector.select()
        events = self._selector.select(0)
        if not events:
            self._advance(timeout)
        return events

    def close(self) -> None:
        self._selector.close()

    def get_map(self):
        return self._selector.get_map()


class VirtualTimeEventLoop(asyncio.SelectorEventLoop):
    def __init__(self, start_s: float = 0.0) -> None:
        self._now = start_s
        super().__init__(_ClockAdvancingSelector(self._advance))

    def time(self) -> float:
        return self._now

    def set_debug(self, enabled: bool) -> None:
        """Debug mode stays off."""
        del enabled
        super().set_debug(False)

    # --- Private ---

    def _advance(self, seconds: float) -> None:
        self._now += seconds


class VirtualTimeTestCase(unittest.IsolatedAsyncioTestCase):
    """Timeouts and sleeps take no real time; the loop clock starts at 0.

    The clock stands while callbacks are ready: a test whose loop never idles, or which waits on
    threads or real I/O, stays on `IsolatedAsyncioTestCase`."""

    loop_factory = VirtualTimeEventLoop
