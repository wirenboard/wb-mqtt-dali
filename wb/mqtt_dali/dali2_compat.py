from typing import Optional, Union

from dali.address import DeviceBroadcast, DeviceShort, GearBroadcast, GearShort
from dali.command import Command, NumericResponse, Response
from dali.device import general as control_device


class Dali2CommandsCompatibilityLayer:
    def __init__(self) -> None:
        self.Compare = control_device.Compare
        self.QueryShortAddress = control_device.QueryShortAddress
        self.Randomise = control_device.Randomise
        self.Terminate = control_device.Terminate
        self.VerifyShortAddress = control_device.VerifyShortAddress
        self.Withdraw = control_device.Withdraw
        self.SetSearchAddrH = control_device.SearchAddrH
        self.SetSearchAddrM = control_device.SearchAddrM
        self.SetSearchAddrL = control_device.SearchAddrL
        self.ProgramShortAddress = control_device.ProgramShortAddress
        self.QueryVersionNumber = control_device.QueryVersionNumber
        self.ReadMemoryLocation = control_device.ReadMemoryLocation
        self.DTR0 = control_device.DTR0

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
        if short_address is None:
            return control_device.Initialise(0x7F)
        return control_device.Initialise(short_address)

    def QueryShortAddressResponseValue(self, resp: Response) -> Optional[int]:
        value = resp.value
        if isinstance(value, int):
            return value
        return None

    def QueryRandomAddressH(self, short_address: int):
        return control_device.QueryRandomAddressH(DeviceShort(short_address))

    def QueryRandomAddressM(self, short_address: int):
        return control_device.QueryRandomAddressM(DeviceShort(short_address))

    def QueryRandomAddressL(self, short_address: int):
        return control_device.QueryRandomAddressL(DeviceShort(short_address))

    def QueryRandomAddressResponseValue(self, resp: Optional[Response]) -> Optional[int]:
        if (
            resp is None
            or resp.raw_value is None
            or resp.raw_value.error
            or not isinstance(resp, NumericResponse)
        ):
            return None
        # Control device returns NumericResponse where value is int
        return resp.value

    def Reset(self, short_address: Optional[int]) -> Command:
        return control_device.Reset(self.getAddress(short_address))

    def getAddress(
        self, short_address: Optional[int]
    ) -> Union[GearShort, DeviceShort, GearBroadcast, DeviceBroadcast]:
        if short_address is None:
            return DeviceBroadcast()
        return DeviceShort(short_address)

    def setShortAddressCommands(self, short_address: Optional[int], new_short_address: int) -> list[Command]:
        return [self.DTR0(new_short_address), control_device.SetShortAddress(self.getAddress(short_address))]
