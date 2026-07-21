"""Shared fixture for the on_off feature tests: a scripted fake DALI driver able to
carry a real ``DaliDevice`` through ``initialize()`` without any bus I/O."""

from dali.command import Response
from dali.frame import BackwardFrame
from dali.sequences import progress as seq_progress
from dali.sequences import sleep as seq_sleep

# Non-zero query answers: QueryDeviceType 254 = "no extended device types",
# QueryVersionNumber 2 = modern gear (non-legacy memory bank layout).
_QUERY_RESPONSES = {"QueryDeviceType": 254, "QueryVersionNumber": 2}


class ScriptedDriver:
    """Fake WBDALIDriver answering every query with a fixed per-command-type byte."""

    def __init__(self):
        self.sent = []

    async def send(self, cmd, source=None, priority=None):
        del source, priority
        self.sent.append(cmd)
        if cmd.response is None:
            return Response(None)
        return cmd.response(BackwardFrame(_QUERY_RESPONSES.get(type(cmd).__name__, 0)))

    async def send_commands(self, cmds, source=None, priority=None):
        return [await self.send(cmd, source, priority) for cmd in cmds]

    async def run_sequence(self, seq, priority=None, progress=None):
        """Mirror WBDALIDriver.run_sequence's generator protocol."""
        del priority, progress
        response = None
        started = False
        try:
            while True:
                try:
                    cmd = next(seq) if not started else seq.send(response)
                    started = True
                except StopIteration as stop:
                    return stop.value
                response = Response(None)
                if isinstance(cmd, (seq_sleep, seq_progress)):
                    continue
                if isinstance(cmd, list):
                    response = await self.send_commands(cmd)
                else:
                    response = await self.send(cmd)
        finally:
            seq.close()
