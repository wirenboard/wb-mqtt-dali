from unittest.mock import MagicMock

import pytest

from wb.mqtt_dali.common_dali_device import (
    request_with_retry_sequence as memory_request_with_retry_sequence,
)
from wb.mqtt_dali.dali_device import request_with_retry_sequence
from wb.mqtt_dali.wbdali_error_response import WbGatewayTransmissionError

# pylint: disable=protected-access


def _ok_response():
    response = MagicMock()
    response._expected = True
    response._error_acceptable = False
    response.raw_value = MagicMock()
    response.raw_value.error = False
    return response


def _bad_response():
    response = MagicMock()
    response._expected = True
    response._error_acceptable = False
    response.raw_value = MagicMock()
    response.raw_value.error = True
    return response


def test_request_with_retry_sequence_retries_transmission_errors():
    cmd = object()
    gen = request_with_retry_sequence(cmd)

    assert next(gen) is cmd
    assert gen.send(WbGatewayTransmissionError()) is cmd
    assert gen.send(WbGatewayTransmissionError()) is cmd
    with pytest.raises(StopIteration) as stop:
        gen.send(_ok_response())
    assert stop.value.value is not None


def test_request_with_retry_sequence_raises_after_three_failures():
    cmd = object()
    gen = request_with_retry_sequence(cmd)

    assert next(gen) is cmd
    assert gen.send(_bad_response()) is cmd
    assert gen.send(_bad_response()) is cmd
    with pytest.raises(RuntimeError, match="No response"):
        gen.send(_bad_response())


def test_memory_request_with_retry_sequence_retries_transmission_errors():
    cmd = object()
    gen = memory_request_with_retry_sequence(cmd)

    assert next(gen) is cmd
    assert gen.send(WbGatewayTransmissionError()) is cmd
    assert gen.send(WbGatewayTransmissionError()) is cmd
    with pytest.raises(StopIteration) as stop:
        gen.send(_ok_response())
    assert stop.value.value is not None
