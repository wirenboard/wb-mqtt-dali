from typing import Optional, Union

from dali.address import DeviceShort, GearShort
from dali.command import Command, NumericResponse, NumericResponseMask, Response
from dali.gear import general as control_gear

from .wbdali import MASK


class DaliCommandsCompatibilityLayer:
    def __init__(self) -> None:
        self.Compare = control_gear.Compare
        self.QueryShortAddress = control_gear.QueryShortAddress
        self.Randomise = control_gear.Randomise
        self.Terminate = control_gear.Terminate
        self.VerifyShortAddress = control_gear.VerifyShortAddress
        self.Withdraw = control_gear.Withdraw
        self.SetSearchAddrH = control_gear.SetSearchAddrH
        self.SetSearchAddrM = control_gear.SetSearchAddrM
        self.SetSearchAddrL = control_gear.SetSearchAddrL

    def Initialise(self, short_address: Optional[int]) -> Command:
        """
        Initialise the DALI control gear.
        Parameters
        ----------
        short_address : Optional[int]
            The short (0-63) address of the control gear to initialise.
            If None, the command will be sent to all control gear without a short address.
            If MASK (255), the command will be sent to all control gear regardless of their short address.
        """
        if short_address == MASK:
            return control_gear.Initialise(broadcast=True)
        return control_gear.Initialise(address=short_address, broadcast=False)

    def ProgramShortAddress(self, short_address: int) -> Command:
        if short_address == MASK:
            return control_gear.ProgramShortAddress("MASK")
        return control_gear.ProgramShortAddress(short_address)

    def QueryShortAddressResponseValue(
        self, resp: Union[NumericResponse, NumericResponseMask]
    ) -> Optional[int]:
        value = resp.value
        if isinstance(value, int):
            return value >> 1
        if value == "MASK":
            return MASK
        return None

    def QueryRandomAddressH(self, short_address: int):
        return control_gear.QueryRandomAddressH(GearShort(short_address))

    def QueryRandomAddressM(self, short_address: int):
        return control_gear.QueryRandomAddressM(GearShort(short_address))

    def QueryRandomAddressL(self, short_address: int):
        return control_gear.QueryRandomAddressL(GearShort(short_address))

    def QueryRandomAddressResponseValue(self, resp: Optional[Response]) -> Optional[int]:
        # Control gear returns Response where value is BackwardFrame
        if resp is None or resp.raw_value is None or resp.raw_value.error:
            return None
        return resp.value.as_integer

    def DTR0(self, value: int) -> Command:
        return control_gear.DTR0(value)

    def ReadMemoryLocation(self, short_address: int) -> Command:
        return control_gear.ReadMemoryLocation(GearShort(short_address))

    def QueryVersionNumber(self, short_address: int) -> Command:
        return control_gear.QueryVersionNumber(GearShort(short_address))

    def getAddress(self, short_address: int) -> Union[GearShort, DeviceShort]:
        return GearShort(short_address)

    def setShortAddressCommands(self, short_address: int, new_short_address: int) -> list[Command]:
        # Convert to gear short address format
        new_short_address = (new_short_address << 1) | 1
        return [self.DTR0(new_short_address), control_gear.SetShortAddress(GearShort(short_address))]
