import asyncio
import time

from ._virtual_time import VirtualTimeTestCase


class TestVirtualTime(VirtualTimeTestCase):
    async def test_an_hour_long_sleep_takes_no_real_time(self):
        """A one-hour sleep: the loop clock moves by an hour, the wall clock by well under a second."""
        loop = asyncio.get_running_loop()
        started = time.monotonic()

        await asyncio.sleep(3600)

        self.assertEqual(loop.time(), 3600)
        self.assertLess(time.monotonic() - started, 1.0)

    async def test_timers_fire_in_order_at_their_time(self):
        """Timers set for 2 s and 0.5 s: each runs when the clock reaches its time, the earlier first."""
        loop = asyncio.get_running_loop()
        fired = []
        loop.call_later(2, lambda: fired.append(("late", loop.time())))
        loop.call_later(0.5, lambda: fired.append(("early", loop.time())))

        await asyncio.sleep(3)

        self.assertEqual(fired, [("early", 0.5), ("late", 2)])

    async def test_wait_for_times_out_at_its_deadline(self):
        loop = asyncio.get_running_loop()

        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(loop.create_future(), 1.5)

        self.assertEqual(loop.time(), 1.5)

    async def test_the_clock_stands_while_callbacks_are_ready(self):
        """Tasks passing control back and forth with zero sleeps: the clock does not move."""

        async def ping_pong() -> None:
            for _ in range(100):
                await asyncio.sleep(0)

        await asyncio.gather(ping_pong(), ping_pong())

        self.assertEqual(asyncio.get_running_loop().time(), 0)
