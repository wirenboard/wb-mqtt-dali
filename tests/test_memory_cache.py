"""Remembering settings-shaped answers, not just memory banks.

A device page open is mostly scene tables and DT8 colour values — around 190
frames per lamp at ~46 ms each — and none of it changes unless somebody
configures the device. The memo answers the second session's reads; a config
write (send-twice command) makes it forget what it remembered for that target.
"""

from dali.address import (
    DeviceShort,
    FeatureInstanceNumber,
    GearBroadcast,
    GearShort,
    InstanceNumber,
)
from dali.device.general import QueryEventScheme
from dali.frame import BackwardFrame
from dali.gear.general import (
    DTR0,
    DTR1,
    QuerySceneLevel,
    ReadMemoryLocation,
    SetMaxLevel,
)

from wb.mqtt_dali.device.feedback import QueryFeedbackCapability
from wb.mqtt_dali.memory_cache import MemoryCache


class _Answer:  # pylint: disable=too-few-public-methods
    def __init__(self, byte):
        self.raw_value = BackwardFrame(byte)


class _NoAnswer:  # pylint: disable=too-few-public-methods
    raw_value = None


def test_a_config_write_makes_the_memo_forget_its_target():
    cache = MemoryCache()
    query = QuerySceneLevel(GearShort(0), 7)
    cache.observe(query, _Answer(42))
    assert cache.plan([query]) == {0: 42}

    # A config write to another short leaves this device's memo alone…
    cache.observe(SetMaxLevel(GearShort(3)), None)
    assert cache.plan([query]) == {0: 42}

    # …a write to this short — or to everyone — does not.
    cache.observe(SetMaxLevel(GearShort(0)), None)
    assert cache.plan([query]) is None

    cache.observe(query, _Answer(42))
    cache.observe(SetMaxLevel(GearBroadcast()), None)
    assert cache.plan([query]) is None


def test_a_batch_containing_a_config_write_is_never_served():
    cache = MemoryCache()
    query = QuerySceneLevel(GearShort(0), 7)
    cache.observe(query, _Answer(42))
    assert cache.plan([query, SetMaxLevel(GearShort(0))]) is None


def test_the_signature_tells_scenes_apart():
    cache = MemoryCache()
    cache.observe(QuerySceneLevel(GearShort(0), 7), _Answer(42))
    assert cache.plan([QuerySceneLevel(GearShort(0), 8)]) is None
    # The signature is the question itself, not the surrounding traffic: an
    # unrelated DTR write earlier in the batch must not turn the same scene
    # question into a different one (interleaved generators write DTRs all
    # the time).
    assert cache.plan([DTR0(200), QuerySceneLevel(GearShort(0), 7)]) == {1: 42}


def test_a_transient_no_answer_is_not_remembered():
    cache = MemoryCache()
    query = QuerySceneLevel(GearShort(0), 7)
    cache.observe(query, _NoAnswer())
    assert cache.plan([query]) is None

    # The next, answered read is what the memo keeps.
    cache.observe(query, _Answer(42))
    assert cache.plan([query]) == {0: 42}


def test_an_undelivered_read_does_not_advance_the_shadow_register():
    cache = MemoryCache()
    cache.observe(DTR1(0), None)
    cache.observe(DTR0(3), None)
    # The gateway never transmitted this frame — the device never saw it,
    # so its DTR0 still points at offset 3.
    cache.observe(ReadMemoryLocation(GearShort(0)), _NoAnswer(), delivered=False)
    cache.observe(ReadMemoryLocation(GearShort(0)), _Answer(0x42))

    assert cache.plan([DTR1(0), DTR0(3), ReadMemoryLocation(GearShort(0))]) == {2: 0x42}


def test_per_instance_questions_do_not_collide():
    cache = MemoryCache()
    q1 = QueryEventScheme(DeviceShort(0), InstanceNumber(1))
    q2 = QueryEventScheme(DeviceShort(0), InstanceNumber(2))
    cache.observe(q1, _Answer(2))
    assert cache.plan([q1]) == {0: 2}
    # Instance 2 was never asked — its answer must not be instance 1's.
    assert cache.plan([q2]) is None


def test_absence_is_remembered_only_with_three_strikes_of_conviction():
    cache = MemoryCache()
    probe = QueryFeedbackCapability(DeviceShort(0), FeatureInstanceNumber(2))

    cache.observe(probe, _NoAnswer())
    cache.observe(probe, _NoAnswer())
    assert cache.plan([probe]) is None  # two glitches are not a fact

    cache.observe(probe, _NoAnswer())
    # Three consecutive unanswered deliveries: the device does not implement
    # this feature, and the memo now answers the probe without the bus.
    assert cache.plan([probe]) == {0: None}

    # An actual answer resets the conviction counter.
    cache2 = MemoryCache()
    cache2.observe(probe, _NoAnswer())
    cache2.observe(probe, _Answer(1))
    cache2.observe(probe, _NoAnswer())
    cache2.observe(probe, _NoAnswer())
    assert cache2.plan([probe]) == {0: 1}
