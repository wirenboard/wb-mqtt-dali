"""Unit tests for ``query_device_types_sequence``.

The sequence is a generator: it yields a ``QueryDeviceType`` and then a chain of
``QueryNextDeviceType`` commands, receiving one response per yield. These tests
drive it directly with scripted responses (one ``as_integer`` value per yielded
command) to assert how the multi-type (value 255) branch decodes the chain:
device type 0 is a valid first entry, types must arrive strictly ascending, and
the terminating 254 / single-type / no-type branches behave as specified.
"""

import logging
from types import SimpleNamespace
from typing import Optional

import pytest
from dali.address import GearShort

from wb.mqtt_dali.dali_device import query_device_types_sequence
from wb.mqtt_dali.wbdali_utils import NoAnswerError


def _resp(value: int) -> SimpleNamespace:
    """A minimal stand-in for a query Response carrying ``as_integer``."""
    return SimpleNamespace(raw_value=SimpleNamespace(as_integer=value, error=False))


def _run_sequence(values: list[int]) -> list[int]:
    """Drive the sequence, feeding one response value per yielded command, and
    return its result (or propagate whatever it raises)."""
    gen = query_device_types_sequence(GearShort(0), logging.getLogger("test"))
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


def _run_sequence_with_silence(
    values: list[Optional[int]], logger: Optional[logging.Logger] = None
) -> list[int]:
    """Like ``_run_sequence``, but ``None`` in ``values`` stands for gear silence."""
    gen = query_device_types_sequence(GearShort(0), logger)
    value_iter = iter(values)
    to_send = None
    try:
        while True:
            gen.send(to_send)
            value = next(value_iter)
            to_send = None if value is None else _resp(value)
    except StopIteration as stop:
        return stop.value


def test_silence_after_types_ends_chain(caplog):
    """Gear silent on every retry instead of 254 after 6, 50, 51: the types read so far are
    the result, and a warning is logged."""
    logger = logging.getLogger("test_query_device_types")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        assert _run_sequence_with_silence([255, 6, 50, 51, None, None, None], logger) == [6, 50, 51]
    assert "did not answer QueryNextDeviceType" in caplog.text


def test_silence_before_any_type_raises():
    with pytest.raises(NoAnswerError):
        _run_sequence_with_silence([255, None, None, None])


def test_framing_error_after_types_raises_without_retry():
    """A garbled answer to QueryNextDeviceType fails the chain at once, with no retry."""
    framing_error = SimpleNamespace(raw_value=SimpleNamespace(as_integer=0, error=True))
    gen = query_device_types_sequence(GearShort(0), logging.getLogger("test"))
    gen.send(None)
    gen.send(_resp(255))
    gen.send(_resp(6))
    with pytest.raises(RuntimeError, match="Framing error") as exc_info:
        gen.send(framing_error)
    assert not isinstance(exc_info.value, NoAnswerError)


def test_silence_then_answer_continues_chain():
    """A query the gear did not decode is retried: silence followed by an answer keeps the chain."""
    assert _run_sequence_with_silence([255, 6, None, 50, 254]) == [6, 50]
