"""Per-bus mirror of the shared DTR0/1/2 registers.

DTR registers are bus-global (not per-device), so the snapshot lives on the
coordinator. It is fed by every observed DTR write — sniffed or our own — and
read when a consumer command (SetFadeTime, SetTemporary…) needs the value that
was loaded into the registers.
"""

from typing import Optional

from dali.command import Command
from dali.gear.general import DTR0, DTR1, DTR2


class DtrSnapshot:
    def __init__(self) -> None:
        self.dtr0 = 0
        self.dtr1 = 0
        self.dtr2 = 0

    def record(self, command: Command) -> Optional[int]:
        """Update the mirrored register from a DTR write.

        Returns the register index (0/1/2) that was written, or ``None`` if the command
        was not a DTR write.
        """
        if isinstance(command, DTR0):
            self.dtr0 = command.param
            return 0
        if isinstance(command, DTR1):
            self.dtr1 = command.param
            return 1
        if isinstance(command, DTR2):
            self.dtr2 = command.param
            return 2
        return None

    @property
    def word(self) -> int:
        """16-bit DTR1:DTR0 (MSB:LSB), as 2-byte colour components consume it."""
        return (self.dtr1 << 8) | self.dtr0
