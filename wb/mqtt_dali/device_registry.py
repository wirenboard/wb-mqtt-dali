from typing import Optional, Union

from dali.address import GearBroadcast, GearGroup, GearShort

from .dali2_device import Dali2Device
from .dali_device import DaliDevice

ShortAddress = int
BusDevice = Union[DaliDevice, Dali2Device]
GearDestination = Union[GearShort, GearGroup, GearBroadcast]


class DeviceRegistry:
    """Short-address index of a bus's devices and resolver of gear destinations.

    Control gear (`DaliDevice`) and DALI-2 control devices (`Dali2Device`) live in
    separate DALI short-address spaces (a gear and a control device may legally
    share a short address), so they are indexed separately. Only control gear is
    addressed by gear commands, so `resolve` returns `DaliDevice`s; the DALI-2 index
    serves event-frame decoding via `dali2_device_by_short`.
    """

    def __init__(self) -> None:
        self._gear_by_short: dict[ShortAddress, DaliDevice] = {}
        self._dali2_by_short: dict[ShortAddress, Dali2Device] = {}

    def set_gear_devices(self, devices: list[DaliDevice]) -> None:
        self._gear_by_short = {d.address.short: d for d in devices}

    def set_dali2_devices(self, devices: list[Dali2Device]) -> None:
        self._dali2_by_short = {d.address.short: d for d in devices}

    def add(self, device: BusDevice) -> None:
        self._index_for(device)[device.address.short] = device

    def remove(self, device: BusDevice) -> None:
        index = self._index_for(device)
        if index.get(device.address.short) is device:
            del index[device.address.short]

    def update_short_address(self, device: BusDevice, old_short: ShortAddress) -> None:
        index = self._index_for(device)
        if index.get(old_short) is device:
            del index[old_short]
        index[device.address.short] = device

    def dali2_device_by_short(self, short_address: ShortAddress) -> Optional[Dali2Device]:
        return self._dali2_by_short.get(short_address)

    def resolve(self, destination: GearDestination) -> list[DaliDevice]:
        """Gear targeted by a python-dali gear destination.

        Unknown short address yields an empty list rather than an error.
        """
        if isinstance(destination, GearShort):
            device = self._gear_by_short.get(destination.address)
            return [device] if device is not None else []
        if isinstance(destination, GearGroup):
            return [d for d in self._gear_by_short.values() if destination.group in d.groups]
        if isinstance(destination, GearBroadcast):
            return list(self._gear_by_short.values())
        return []

    # --- Private ---

    def _index_for(self, device: BusDevice) -> dict[ShortAddress, BusDevice]:
        if isinstance(device, Dali2Device):
            return self._dali2_by_short
        return self._gear_by_short
