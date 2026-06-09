"""Unit tests for ``query_device_types_sequence``.

The sequence is a generator: it yields a ``QueryDeviceType`` and then a chain of
``QueryNextDeviceType`` commands, receiving one response per yield. These tests
drive it directly with scripted responses (one ``as_integer`` value per yielded
command) to assert how the multi-type (value 255) branch decodes the chain:
device type 0 is a valid first entry, types must arrive strictly ascending, and
the terminating 254 / single-type / no-type branches behave as specified.
"""

from types import SimpleNamespace

import pytest
from dali.address import GearShort

from wb.mqtt_dali.dali_device import query_device_types_sequence


def _resp(value: int) -> SimpleNamespace:
    """A minimal stand-in for a query Response carrying ``as_integer``."""
    return SimpleNamespace(raw_value=SimpleNamespace(as_integer=value, error=False))


def _run_sequence(values: list[int]) -> list[int]:
    """Drive the sequence, feeding one response value per yielded command, and
    return its result (or propagate whatever it raises)."""
    gen = query_device_types_sequence(GearShort(0))
    value_iter = iter(values)
    to_send = None
    try:
        while True:
            gen.send(to_send)
            to_send = _resp(next(value_iter))
    except StopIteration as stop:
        return stop.value


def test_single_device_type():
    assert _run_sequence([7]) == [7]


def test_no_device_types():
    assert not _run_sequence([254])


def test_multiple_types_including_zero():
    # 255 enters the multi-type loop; 0 is a valid first type and must not be
    # rejected as "out of order"; 254 terminates the chain.
    assert _run_sequence([255, 0, 5, 8, 254]) == [0, 5, 8]


def test_out_of_order_raises():
    # last_seen must advance, so a descending type is rejected.
    with pytest.raises(RuntimeError, match="out of order"):
        _run_sequence([255, 5, 3, 254])


def test_repeated_type_raises():
    with pytest.raises(RuntimeError, match="out of order"):
        _run_sequence([255, 5, 5, 254])


def test_empty_multi_type_chain_raises():
    with pytest.raises(RuntimeError, match="No device types"):
        _run_sequence([255, 254])
