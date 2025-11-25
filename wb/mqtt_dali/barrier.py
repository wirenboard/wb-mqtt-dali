import asyncio
import enum
import logging
import threading

_global_lock = threading.Lock()


class _LoopBoundMixin:  # pylint: disable=R0903
    _loop = None

    def _get_loop(self):
        loop = asyncio.events._get_running_loop()  # pylint: disable=W0212

        if self._loop is None:
            with _global_lock:
                if self._loop is None:
                    self._loop = loop
        if loop is not self._loop:
            raise RuntimeError(f"{self!r} is bound to a different event loop")
        return loop


class _BarrierState(enum.Enum):
    FILLING = "filling"
    DRAINING = "draining"
    RESETTING = "resetting"
    BROKEN = "broken"


class BrokenBarrierError(RuntimeError):
    """Barrier is broken by barrier.abort() call."""


class Barrier(_LoopBoundMixin):
    """Asyncio equivalent to threading.Barrier
    Implements a Barrier primitive.
    Useful for synchronizing a fixed number of tasks at known synchronization
    points. Tasks block on 'wait()' and are simultaneously awoken once they
    have all made their call.

    There is timeout too. If timeout is reached, the barrier is released and
    all waiting tasks are simultaneously awoken.

    This implementation also allows passing payloads to wait calls. When the
    barrier is released, all payloads are sent to all tasks that were
    waiting on the barrier alongside with their index in the barrier.
    It allows one of the simultaneously awoken tasks to perform some work
    on behalf of all tasks that were waiting on the barrier.
    """

    def __init__(self, parties, default_timeout=None):
        """Create a barrier, initialised to 'parties' tasks."""
        if parties < 1:
            raise ValueError("parties must be > 0")

        self._cond = asyncio.Condition()  # notify all tasks when state changes

        self._parties = parties
        self._state = _BarrierState.FILLING
        self._count = 0  # count tasks in Barrier
        self._payloads = {}
        self._default_timeout = default_timeout

    def __repr__(self):
        res = super().__repr__()
        extra = f"{self._state}"
        if not self.broken:
            extra += f", waiters:{self.n_waiting}/{self.parties}"
        return f"<{res[1:-1]} [{extra}]>"

    async def __aenter__(self):
        # wait for the barrier reaches the parties number
        # when start draining release and return index of waited task
        return await self.wait()

    async def __aexit__(self, *args):
        pass

    async def wait(self, payload=None, timeout=None):
        """Wait for the barrier.
        When the specified number of tasks have started waiting, they are all
        simultaneously awoken.
        Returns an unique and individual index number from 0 to 'parties-1'.
        """
        async with self._cond:
            await self._block()  # Block while the barrier drains or resets.
            try:
                index = self._count
                self._count += 1
                self._payloads[index] = payload
                if index + 1 == self._parties:
                    # We release the barrier
                    await self._release()
                else:
                    await self._wait(timeout=timeout)
                return index, self._payloads.values()
            except:
                self._payloads.pop(index, None)
                raise
            finally:
                self._count -= 1
                # Wake up any tasks waiting for barrier to drain.
                self._exit()

    async def _block(self):
        # Block until the barrier is ready for us,
        # or raise an exception if it is broken.
        #
        # It is draining or resetting, wait until done
        # unless a CancelledError occurs
        await self._cond.wait_for(
            lambda: self._state not in (_BarrierState.DRAINING, _BarrierState.RESETTING)
        )

        # see if the barrier is in a broken state
        if self._state is _BarrierState.BROKEN:
            raise BrokenBarrierError("Barrier aborted")

    async def _release(self):
        # Release the tasks waiting in the barrier.

        # Enter draining state.
        # Next waiting tasks will be blocked until the end of draining.
        self._state = _BarrierState.DRAINING
        self._cond.notify_all()

    async def _wait(self, timeout=None):
        # Wait in the barrier until we are released. Raise an exception
        # if the barrier is reset or broken.

        # wait for end of filling
        # unless a CancelledError occurs
        try:
            await asyncio.wait_for(
                self._cond.wait_for(lambda: self._state is not _BarrierState.FILLING),
                timeout=timeout or self._default_timeout,
            )
        except asyncio.TimeoutError:
            logging.debug("Barrier wait timed out, releasing with %d parties!!!!", self._count)
            await self._release()

        if self._state in (_BarrierState.BROKEN, _BarrierState.RESETTING):
            raise BrokenBarrierError("Abort or reset of barrier")

    def _exit(self):
        # If we are the last tasks to exit the barrier, signal any tasks
        # waiting for the barrier to drain.
        if self._count == 0:
            if self._state in (_BarrierState.RESETTING, _BarrierState.DRAINING):
                self._state = _BarrierState.FILLING
                self._payloads = {}
            self._cond.notify_all()

    async def reset(self):
        """Reset the barrier to the initial state.
        Any tasks currently waiting will get the BrokenBarrier exception
        raised.
        """
        async with self._cond:
            if self._count > 0:
                if self._state is not _BarrierState.RESETTING:
                    # reset the barrier, waking up tasks
                    self._state = _BarrierState.RESETTING
            else:
                self._state = _BarrierState.FILLING
                self._payloads = {}
            self._cond.notify_all()

    async def abort(self):
        """Place the barrier into a 'broken' state.
        Useful in case of error.  Any currently waiting tasks and tasks
        attempting to 'wait()' will have BrokenBarrierError raised.
        """
        async with self._cond:
            self._state = _BarrierState.BROKEN
            self._cond.notify_all()

    @property
    def parties(self):
        """Return the number of tasks required to trip the barrier."""
        return self._parties

    @property
    def n_waiting(self):
        """Return the number of tasks currently waiting at the barrier."""
        if self._state is _BarrierState.FILLING:
            return self._count
        return 0

    @property
    def broken(self):
        """Return True if the barrier is in a broken state."""
        return self._state is _BarrierState.BROKEN
