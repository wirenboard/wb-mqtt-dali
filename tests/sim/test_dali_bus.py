from dali.address import GearBroadcast, GearShort
from dali.gear.general import (
    DAPC,
    Compare,
    Initialise,
    Off,
    QueryActualLevel,
    QueryControlGearPresent,
    Randomise,
    SetSearchAddrH,
    SetSearchAddrL,
    SetSearchAddrM,
    Terminate,
    Withdraw,
)
from dali.tests import fakes

from wb.mqtt_dali.sim.dali_bus import SimulatedDaliBus, TransmissionStatus


def send(bus, command):
    """Transmit a python-dali command the way the gateway would."""
    frame = command.frame
    return bus.send_frame(frame.as_integer, len(frame))


def test_command_without_answer_reports_transmission_without_response():
    bus = SimulatedDaliBus([fakes.Gear(shortaddr=0)])

    status, backward = send(bus, DAPC(GearBroadcast(), 128))

    assert status is TransmissionStatus.WITHOUT_RESPONSE
    assert backward == 0
    assert bus.gear[0].level == 128


def test_query_returns_the_gear_answer():
    gear = fakes.Gear(shortaddr=3)
    bus = SimulatedDaliBus([gear])
    send(bus, DAPC(GearShort(3), 77))

    status, backward = send(bus, QueryActualLevel(GearShort(3)))

    assert status is TransmissionStatus.WITH_BACKWARD_RESPONSE
    assert backward == 77


def test_query_to_an_absent_address_is_unanswered():
    bus = SimulatedDaliBus([fakes.Gear(shortaddr=3)])

    status, backward = send(bus, QueryControlGearPresent(GearShort(9)))

    assert status is TransmissionStatus.WITHOUT_RESPONSE
    assert backward == 0


def test_simultaneous_answers_collide():
    bus = SimulatedDaliBus([fakes.Gear(shortaddr=1), fakes.Gear(shortaddr=2)])

    status, _ = send(bus, QueryControlGearPresent(GearBroadcast()))

    assert status is TransmissionStatus.BROKEN_RESPONSE


def test_unpowered_bus_reports_no_power():
    bus = SimulatedDaliBus([fakes.Gear(shortaddr=0)])
    bus.powered = False

    status, backward = send(bus, Off(GearBroadcast()))

    assert status is TransmissionStatus.NO_POWER_ON_BUS
    assert backward == 0


def test_gear_only_answers_16_bit_frames():
    """A control device frame must not reach control gear (IEC 62386-103)."""
    bus = SimulatedDaliBus([fakes.Gear(shortaddr=0)])

    status, _ = bus.send_frame(0x123456, 24)

    assert status is TransmissionStatus.WITHOUT_RESPONSE


def test_compare_answers_while_a_device_is_initialising():
    """The COMPARE half of the commissioning binary search works end to end."""
    gear = fakes.Gear(shortaddr=None, random_preload=[0x0000FF])
    bus = SimulatedDaliBus([gear])

    send(bus, Terminate())
    send(bus, Initialise(broadcast=True))
    send(bus, Randomise())

    # The search address is the upper bound of the binary search; the gear
    # answers COMPARE while its random address is at or below it.
    send(bus, SetSearchAddrH(0xFF))
    send(bus, SetSearchAddrM(0xFF))
    send(bus, SetSearchAddrL(0xFF))

    status, backward = send(bus, Compare())
    assert status is TransmissionStatus.WITH_BACKWARD_RESPONSE
    assert backward == 0xFF

    # WITHDRAW only takes effect when the search address equals the random
    # address, which is how the search isolates one device at a time.
    send(bus, SetSearchAddrH(0x00))
    send(bus, SetSearchAddrM(0x00))
    send(bus, SetSearchAddrL(0xFF))
    send(bus, Withdraw())

    send(bus, SetSearchAddrH(0xFF))
    send(bus, SetSearchAddrM(0xFF))
    send(bus, SetSearchAddrL(0xFF))
    status, _ = send(bus, Compare())
    assert status is TransmissionStatus.WITHOUT_RESPONSE
