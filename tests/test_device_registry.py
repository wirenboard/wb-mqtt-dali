from unittest.mock import MagicMock

from dali.address import DeviceBroadcast, GearBroadcast, GearGroup, GearShort

from wb.mqtt_dali.common_dali_device import DaliDeviceAddress, DaliDeviceBase
from wb.mqtt_dali.dali2_device import Dali2Device
from wb.mqtt_dali.dali_device import DaliDevice
from wb.mqtt_dali.device_registry import DeviceRegistry

# Prevent file system access inside DaliDeviceBase.__init__
DaliDeviceBase._common_schema = {"title": "test-schema"}  # pylint: disable=protected-access


class _Gear(DaliDevice):
    """DaliDevice test double exposing directly-settable group membership.

    Real group membership only comes from a bus read, so the registry tests
    override the public `groups` property instead of reaching into the param.
    """

    def __init__(self, short: int, groups=()) -> None:
        super().__init__(DaliDeviceAddress(short=short, random=0), "bus", MagicMock())
        self._member_groups = set(groups)

    @property
    def groups(self) -> set[int]:
        return self._member_groups


def _dali2(short: int) -> Dali2Device:
    return Dali2Device(DaliDeviceAddress(short=short, random=0), "bus", MagicMock())


def test_resolve_short_present_and_absent():
    """GearShort yields the gear at that address, or an empty list if none exists."""
    gear = _Gear(5)
    registry = DeviceRegistry()
    registry.set_gear_devices([gear])

    assert registry.resolve(GearShort(5)) == [gear]
    assert registry.resolve(GearShort(7)) == []


def test_resolve_group_uses_device_groups():
    """GearGroup returns exactly the gear whose `groups` contains the group number."""
    in_group = _Gear(1, groups={2, 4})
    other_group = _Gear(2, groups={3})
    no_groups = _Gear(3)
    registry = DeviceRegistry()
    registry.set_gear_devices([in_group, other_group, no_groups])

    assert registry.resolve(GearGroup(2)) == [in_group]
    assert registry.resolve(GearGroup(3)) == [other_group]
    assert registry.resolve(GearGroup(7)) == []


def test_resolve_broadcast_returns_all_gear():
    """GearBroadcast returns every gear device on the bus, regardless of groups."""
    gear = [_Gear(1, groups={0}), _Gear(2), _Gear(3, groups={5})]
    registry = DeviceRegistry()
    registry.set_gear_devices(gear)

    assert registry.resolve(GearBroadcast()) == gear


def test_resolve_excludes_dali2_devices():
    """DALI-2 control devices are indexed for events but never returned by gear resolve.

    A gear and a DALI-2 device may share a short address (separate address
    spaces); resolve must stay on gear while the DALI-2 lookup stays on DALI-2.
    """
    gear = _Gear(3, groups={2})
    dali2 = _dali2(3)
    registry = DeviceRegistry()
    registry.set_gear_devices([gear])
    registry.set_dali2_devices([dali2])

    assert registry.resolve(GearShort(3)) == [gear]
    assert registry.resolve(GearBroadcast()) == [gear]
    # group membership comes from `device.groups`; Dali2Device.groups is always empty
    assert registry.resolve(GearGroup(2)) == [gear]
    assert registry.dali2_device_by_short(3) is dali2


def test_dali2_lookup_present_and_absent():
    """The DALI-2 short lookup returns the device or None without raising."""
    dali2 = _dali2(8)
    registry = DeviceRegistry()
    registry.set_dali2_devices([dali2])

    assert registry.dali2_device_by_short(8) is dali2
    assert registry.dali2_device_by_short(9) is None


def test_add_and_remove_update_resolution():
    """add()/remove() change which gear resolve returns; remove() is by identity."""
    first = _Gear(5)
    registry = DeviceRegistry()
    registry.add(first)
    assert registry.resolve(GearShort(5)) == [first]

    registry.remove(first)
    assert registry.resolve(GearShort(5)) == []

    # Removing a stale object that no longer owns its short address is a no-op.
    current = _Gear(6)
    superseded = _Gear(6)
    registry.add(current)
    registry.remove(superseded)
    assert registry.resolve(GearShort(6)) == [current]


def test_update_short_address_moves_gear():
    """update_short_address() re-keys the device and frees its old short address."""
    gear = _Gear(5)
    registry = DeviceRegistry()
    registry.add(gear)

    gear.address.short = 9
    registry.update_short_address(gear, old_short=5)

    assert registry.resolve(GearShort(5)) == []
    assert registry.resolve(GearShort(9)) == [gear]


def test_update_short_address_keeps_old_slot_taken_by_other_device():
    """update_short_address() leaves the old short alone when another device now owns it.

    Device A is registered at short 5, then a different device B is added at short
    5, clobbering A in the gear index. Moving A to short 9 (with old_short=5) must
    NOT delete short 5, because that slot belongs to B, not the stale A.
    """
    device_a = _Gear(5)
    device_b = _Gear(5)
    registry = DeviceRegistry()
    registry.add(device_a)
    registry.add(device_b)

    device_a.address.short = 9
    registry.update_short_address(device_a, old_short=5)

    assert registry.resolve(GearShort(5)) == [device_b]
    assert registry.resolve(GearShort(9)) == [device_a]


def test_resolve_unknown_destination_returns_empty():
    """A non-gear destination (a device-side address) resolves to an empty list."""
    registry = DeviceRegistry()
    registry.set_gear_devices([_Gear(1)])

    assert registry.resolve(DeviceBroadcast()) == []


def test_add_remove_route_by_device_kind():
    """add()/remove() route gear and DALI-2 devices to their own indexes by type."""
    gear = _Gear(4)
    dali2 = _dali2(4)
    registry = DeviceRegistry()
    registry.add(gear)
    registry.add(dali2)

    assert registry.resolve(GearShort(4)) == [gear]
    assert registry.dali2_device_by_short(4) is dali2

    registry.remove(dali2)
    assert registry.dali2_device_by_short(4) is None
    # Removing the DALI-2 device leaves the gear at the same short untouched.
    assert registry.resolve(GearShort(4)) == [gear]
