"""Helvar Digidim proprietary 17-bit frames (FF17): the 16-bit word of an FF16 frame
followed by a 17th bit, always 0 on the wire."""

from dali.address import GearShort
from dali.command import Command, NumericResponse
from dali.frame import ForwardFrame

from ..wbdali import FF17_BIT_LENGTH


class _HelvarCommand(Command):
    """An FF17 frame of an address byte and a data byte."""

    _cmdval = 0

    def __init__(self, address_byte: int, data: int) -> None:
        super().__init__(ForwardFrame(FF17_BIT_LENGTH, ((address_byte << 8) | data) << 1))


class _ShortAddressCommand(_HelvarCommand):
    """A command to a short address; `_cmdval` is the data byte."""

    def __init__(self, destination: GearShort) -> None:
        super().__init__((destination.address << 1) | 1, self._cmdval)
        self.destination = destination

    def __str__(self) -> str:
        return f"{type(self).__name__}({self.destination})"


class _SpecialCommand(_HelvarCommand):
    """A command to all devices; `_cmdval` is the address byte."""

    def __init__(self, data: int) -> None:
        super().__init__(self._cmdval, data)
        self.data = data

    def __str__(self) -> str:
        return f"{type(self).__name__}({self.data})"


class QueryMajorVersion(_ShortAddressCommand):
    response = NumericResponse
    _cmdval = 0xE5


class QueryMinorVersion(_ShortAddressCommand):
    response = NumericResponse
    _cmdval = 0xE6


class ReadE2(_ShortAddressCommand):
    """Answers the E2 byte at the address in Extended DTR MSB and DTR0, then advances DTR0."""

    response = NumericResponse
    uses_dtr0 = True
    _cmdval = 0x9A


class EnableWriteE2(_ShortAddressCommand):
    """Enables Write E2 for the next frame only."""

    uses_dtr0 = True
    _cmdval = 0x84


class WriteE2(_SpecialCommand):
    """Writes the byte at the E2 address in DTR0 of the enabled device, then advances DTR0."""

    uses_dtr0 = True
    _cmdval = 0xC3


class ExtendedDtrMsb(_SpecialCommand):
    """Sets the high byte of the E2 address; DTR0 holds the low one."""

    uses_dtr0 = True
    _cmdval = 0xC5
