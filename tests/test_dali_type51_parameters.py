"""Tests for DT51 (Energy reporting) support."""

from decimal import Decimal
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from dali.exceptions import MemoryLocationNotImplemented, ResponseError
from dali.gear.general import DTR0, DTR1, ReadMemoryLocation
from dali.memory.energy import (
    ActiveEnergy,
    ActiveEnergyLoadside,
    ActivePower,
    ActivePowerLoadside,
    ApparentEnergy,
    ApparentPower,
)
from dali.memory.location import FlagValue

from wb.mqtt_dali.common_dali_device import DaliDeviceAddress, DaliDeviceBase
from wb.mqtt_dali.dali_device import DaliDevice
from wb.mqtt_dali.dali_type51_parameters import Type51EnergyParam

# pylint: disable=redefined-outer-name

# Avoid filesystem reads in DaliDeviceBase.__init__.
DaliDeviceBase._common_schema = {"title": "test-schema"}  # pylint: disable=protected-access


def _ok_byte_response(value: int):
    resp = MagicMock()
    resp.raw_value = MagicMock()
    resp.raw_value.error = False
    resp.raw_value.as_integer = value
    return resp


def _bad_response():
    resp = MagicMock()
    resp.raw_value = MagicMock()
    resp.raw_value.error = True
    resp.raw_value.as_integer = 0
    return resp


@pytest.fixture
def mock_driver():
    d = AsyncMock()
    d.run_sequence = AsyncMock()
    return d


def _bank_data_active(energy=12345, power=42):
    return {ActiveEnergy: energy, ActivePower: power}


def _bank_data_apparent(energy=23456, power=84):
    return {ApparentEnergy: energy, ApparentPower: power}


def _bank_data_loadside(energy=11111, power=21):
    return {ActiveEnergyLoadside: energy, ActivePowerLoadside: power}


def _make_run_sequence_dispatcher(per_bank):
    """Build a run_sequence side-effect keyed by the bank address.

    ``per_bank`` maps ``bank.address -> result | exception``. Anything not
    listed raises MemoryLocationNotImplemented.
    """

    async def _dispatch(seq):
        # read_memory_bank is a generator we don't drive — inspect its closure for the target bank.
        # Contract: works only because read_memory_bank is a sync generator with a local named `bank`.
        # If it becomes async or the local is renamed, this lookup returns None and bank-202 tests
        # fall through to MemoryLocationNotImplemented.
        bank = seq.gi_frame.f_locals.get("bank")
        if bank is None:
            try:
                next(seq)
            except StopIteration:
                pass
            raise MemoryLocationNotImplemented("no bank in sequence frame")
        seq.close()
        result = per_bank.get(bank.address)
        if isinstance(result, BaseException):
            raise result
        if result is None:
            raise MemoryLocationNotImplemented(f"bank {bank.address} not implemented")
        return result

    return _dispatch


@pytest.mark.asyncio
async def test_type51_load_info_bank_202_only(mock_driver):
    param = Type51EnergyParam()
    mock_driver.run_sequence.side_effect = _make_run_sequence_dispatcher(
        {202: _bank_data_active(energy=1000, power=42)}
    )
    result = await param.read(mock_driver, short_address=1)

    assert set(result.keys()) == {"energy_reporting"}
    assert result["energy_reporting"] == {"active_energy": 1.0, "active_power": 42}


@pytest.mark.asyncio
async def test_type51_load_info_all_three_banks(mock_driver):
    param = Type51EnergyParam()
    mock_driver.run_sequence.side_effect = _make_run_sequence_dispatcher(
        {
            202: _bank_data_active(energy=100, power=10),
            203: _bank_data_apparent(energy=200, power=20),
            204: _bank_data_loadside(energy=80, power=8),
        }
    )

    result = await param.read(mock_driver, short_address=1)
    assert set(result.keys()) == {"energy_reporting"}
    assert result["energy_reporting"] == {
        "active_energy": 0.1,
        "active_power": 10,
        "apparent_energy": 0.2,
        "apparent_power": 20,
        "loadside_energy": 0.08,
        "loadside_power": 8,
    }


@pytest.mark.asyncio
async def test_type51_load_info_bank_202_failure_raises(mock_driver):
    param = Type51EnergyParam()
    mock_driver.run_sequence.side_effect = _make_run_sequence_dispatcher(
        {202: ResponseError("framing error")}
    )
    with pytest.raises(RuntimeError, match="Failed to read DT51 memory bank 202"):
        await param.read(mock_driver, short_address=1)


@pytest.mark.asyncio
async def test_type51_load_info_bank_202_not_implemented_raises(mock_driver):
    param = Type51EnergyParam()
    mock_driver.run_sequence.side_effect = _make_run_sequence_dispatcher({})
    with pytest.raises(RuntimeError, match="Failed to read DT51 memory bank 202"):
        await param.read(mock_driver, short_address=1)


@pytest.mark.asyncio
async def test_type51_load_info_field_flagvalue_skipped(mock_driver):
    param = Type51EnergyParam()
    mock_driver.run_sequence.side_effect = _make_run_sequence_dispatcher(
        {
            202: {ActiveEnergy: 555, ActivePower: FlagValue.MASK},
            203: {ApparentEnergy: FlagValue.TMASK, ApparentPower: 7},
        }
    )
    result = await param.read(mock_driver, short_address=1)
    assert result["energy_reporting"] == {"active_energy": 0.555, "apparent_power": 7}


@pytest.mark.asyncio
async def test_type51_load_info_energy_in_kwh_three_decimals(mock_driver):
    param = Type51EnergyParam()
    mock_driver.run_sequence.side_effect = _make_run_sequence_dispatcher(
        {
            202: {ActiveEnergy: Decimal("12345"), ActivePower: 1},
            203: {ApparentEnergy: Decimal("789"), ApparentPower: 2},
            204: {ActiveEnergyLoadside: Decimal("5000"), ActivePowerLoadside: 3},
        }
    )
    result = await param.read(mock_driver, short_address=1)
    fields = result["energy_reporting"]
    assert fields["active_energy"] == 12.345
    assert isinstance(fields["active_energy"], float)
    assert fields["apparent_energy"] == 0.789
    assert fields["loadside_energy"] == 5.0


def test_type51_schema_active_fields_always_present():
    param = Type51EnergyParam()
    schema = param.get_schema(False)
    card = schema["properties"]["energy_reporting"]
    assert card["title"] == "Energy reporting"
    assert card["propertyOrder"] == 450  # PropertyStartOrder.DT51
    inner = card["properties"]
    assert "active_energy" in inner
    assert inner["active_energy"]["type"] == "number"
    assert "active_power" in inner
    assert inner["active_power"]["type"] == "integer"
    assert "apparent_energy" not in inner
    assert "apparent_power" not in inner
    assert "loadside_energy" not in inner
    assert "loadside_power" not in inner


@pytest.mark.asyncio
async def test_type51_schema_includes_present_sections_after_read(mock_driver):
    param = Type51EnergyParam()
    mock_driver.run_sequence.side_effect = _make_run_sequence_dispatcher(
        {
            202: _bank_data_active(),
            203: _bank_data_apparent(),
        }
    )
    await param.read(mock_driver, short_address=1)
    schema = param.get_schema(False)
    inner = schema["properties"]["energy_reporting"]["properties"]
    assert "apparent_energy" in inner
    assert inner["apparent_energy"]["type"] == "number"
    assert "apparent_power" in inner
    assert inner["apparent_power"]["type"] == "integer"
    assert "loadside_energy" not in inner
    assert "loadside_power" not in inner


def test_type51_schema_groups_and_broadcast_empty():
    assert not Type51EnergyParam().get_schema(True)


@pytest.mark.asyncio
async def test_type51_mqtt_controls_only_active_energy():
    dev = await _initialize_dt51_device(scale_byte=0)
    controls = dev.get_mqtt_controls()
    energy = [c for c in controls if c.id == "active_energy"]
    assert len(energy) == 1
    assert energy[0].meta.units == "kWh"
    assert energy[0].meta.title.en == "Energy consumption"
    assert energy[0].meta.read_only is True


def _ok_int_response(value: int):
    resp = MagicMock()
    resp.raw_value = MagicMock()
    resp.raw_value.error = False
    resp.raw_value.as_integer = value
    # check_query_response reads this attr; MagicMock would auto-generate a truthy mock.
    resp._error_acceptable = False  # pylint: disable=protected-access
    return resp


async def _initialize_dt51_device(scale_byte: Optional[int]) -> DaliDevice:
    async def fake_run_sequence(seq):
        name = seq.gi_code.co_name
        if name == "query_device_types_sequence":
            seq.close()
            return [51]
        if name == "_read_active_scale_sequence":
            seq.close()
            if scale_byte is None:
                raise MemoryLocationNotImplemented("scale not implemented")
            return scale_byte
        seq.close()
        raise AssertionError(f"unexpected run_sequence target: {name}")

    async def fake_send_commands(cmds, source=None):
        del source
        return [_ok_int_response(0) for _ in cmds]

    driver = AsyncMock()
    driver.run_sequence = AsyncMock(side_effect=fake_run_sequence)
    driver.send_commands = AsyncMock(side_effect=fake_send_commands)

    dev = DaliDevice(DaliDeviceAddress(short=3, random=0), "bus1", MagicMock(), None, None)
    await dev.initialize(driver)
    return dev


async def _make_initialized_device_with_type51(scale_byte: Optional[int] = 0) -> DaliDevice:
    # Strip the generic readable controls so polling-cycle tests see the DT51 chunk alone.
    dev = await _initialize_dt51_device(scale_byte)
    # pylint: disable=protected-access
    dev._pollables = list(dev._standalone_pollables)
    dev._current_round = []
    return dev


@pytest.mark.asyncio
async def test_type51_mqtt_control_present_after_initialize():
    dev = await _initialize_dt51_device(scale_byte=0)
    control_ids = [c.id for c in dev.get_mqtt_controls()]
    assert "active_energy" in control_ids


def _energy_bytes_to_kwh_str(scale_byte: int, energy_bytes: list) -> str:
    wh = float(ActiveEnergy.raw_to_value(bytes([scale_byte, *energy_bytes])))
    return f"{wh / 1000.0:.3f}"


async def _run_one_chunk(dev: DaliDevice, driver, now: float):
    res = dev.poll_controls(
        driver, now=now, max_commands=3, default_max_commands=3, default_poll_interval=5.0
    )
    assert res.poll_coroutine is not None
    return await res.poll_coroutine()


@pytest.mark.asyncio
async def test_type51_chunked_poll_assembles_value():
    dev = await _make_initialized_device_with_type51(scale_byte=0)  # scale 10^0 = 1
    energy_bytes = [0x00, 0x00, 0x00, 0x00, 0x01, 0x2C]  # 300 Wh
    sent_calls = []

    async def fake_send(cmds):
        sent_calls.append(list(cmds))
        idx = (len(sent_calls) - 1) * 2
        return [
            MagicMock(),
            MagicMock(),
            _ok_byte_response(energy_bytes[idx]),
            _ok_byte_response(energy_bytes[idx + 1]),
        ]

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=fake_send)

    results = []
    for i in range(3):
        results.append(await _run_one_chunk(dev, driver, now=float(i)))

    assert len(sent_calls) == 3
    assert results[0] == []
    assert results[1] == []
    final = results[2]
    assert len(final) == 1
    assert final[0].control_id == "active_energy"
    assert final[0].error is None
    assert final[0].value == _energy_bytes_to_kwh_str(0, energy_bytes)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scale_byte,energy_bytes,expected_value",
    [
        # scale = 10^0 = 1 Wh per LSB; 0x3039 = 12345 Wh -> "12.345"
        (0, [0x00, 0x00, 0x00, 0x00, 0x30, 0x39], "12.345"),
        # scale = 10^3 = 1 kWh per LSB; raw_value = 5 -> 5000 Wh -> "5.000"
        (3, [0x00, 0x00, 0x00, 0x00, 0x00, 0x05], "5.000"),
    ],
)
async def test_type51_chunked_poll_publishes_kwh_three_decimals(scale_byte, energy_bytes, expected_value):
    dev = await _make_initialized_device_with_type51(scale_byte=scale_byte)
    sent_calls = []

    async def fake_send(cmds):
        sent_calls.append(list(cmds))
        idx = (len(sent_calls) - 1) * 2
        return [
            MagicMock(),
            MagicMock(),
            _ok_byte_response(energy_bytes[idx]),
            _ok_byte_response(energy_bytes[idx + 1]),
        ]

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=fake_send)

    results = []
    for i in range(3):
        results.append(await _run_one_chunk(dev, driver, now=float(i)))

    final = results[2]
    assert len(final) == 1
    assert final[0].control_id == "active_energy"
    assert final[0].error is None
    assert final[0].value == expected_value


@pytest.mark.asyncio
async def test_type51_chunked_poll_each_chunk_writes_dtr():
    dev = await _make_initialized_device_with_type51(scale_byte=0)
    energy_bytes = [0x11, 0x22, 0x33, 0x44, 0x55, 0x66]
    sent_calls = []

    async def fake_send(cmds):
        sent_calls.append(list(cmds))
        idx = (len(sent_calls) - 1) * 2
        return [
            MagicMock(),
            MagicMock(),
            _ok_byte_response(energy_bytes[idx]),
            _ok_byte_response(energy_bytes[idx + 1]),
        ]

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=fake_send)

    for i in range(3):
        await _run_one_chunk(dev, driver, now=float(i))

    assert len(sent_calls) == 3
    for chunk_index, cmds in enumerate(sent_calls):
        assert len(cmds) == 4
        assert isinstance(cmds[0], DTR1)
        assert cmds[0].param == 202
        assert isinstance(cmds[1], DTR0)
        assert cmds[1].param == 0x05 + chunk_index * 2
        assert isinstance(cmds[2], ReadMemoryLocation)
        assert isinstance(cmds[3], ReadMemoryLocation)


@pytest.mark.asyncio
async def test_type51_chunked_poll_failure_publishes_error():
    dev = await _make_initialized_device_with_type51(scale_byte=0)
    energy_bytes = [0x10, 0x20]
    sent_calls = []

    async def fake_send(cmds):
        sent_calls.append(list(cmds))
        if len(sent_calls) == 1:
            return [
                MagicMock(),
                MagicMock(),
                _ok_byte_response(energy_bytes[0]),
                _ok_byte_response(energy_bytes[1]),
            ]
        return [MagicMock(), MagicMock(), _ok_byte_response(0), _bad_response()]

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=fake_send)

    result1 = await _run_one_chunk(dev, driver, now=0.0)
    assert result1 == []
    result2 = await _run_one_chunk(dev, driver, now=1.0)
    assert len(result2) == 1
    assert result2[0].control_id == "active_energy"
    assert result2[0].error == "r"


@pytest.mark.asyncio
async def test_type51_chunked_poll_restarts_after_failure():
    dev = await _make_initialized_device_with_type51(scale_byte=0)
    energy_bytes_attempt_2 = [0x00, 0x00, 0x00, 0x00, 0x00, 0x09]  # 9 Wh
    sent_calls = []

    async def fake_send(cmds):
        sent_calls.append(list(cmds))
        if len(sent_calls) == 1:
            return [MagicMock(), MagicMock(), _ok_byte_response(0x99), _ok_byte_response(0x88)]
        if len(sent_calls) == 2:
            return [MagicMock(), MagicMock(), _bad_response(), _ok_byte_response(0)]
        idx = (len(sent_calls) - 3) * 2
        return [
            MagicMock(),
            MagicMock(),
            _ok_byte_response(energy_bytes_attempt_2[idx]),
            _ok_byte_response(energy_bytes_attempt_2[idx + 1]),
        ]

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=fake_send)

    await _run_one_chunk(dev, driver, now=0.0)
    failure = await _run_one_chunk(dev, driver, now=1.0)
    assert failure[0].error == "r"

    completion_time = 1.0 + 200.0
    result: list = []
    for i in range(3):
        result = await _run_one_chunk(dev, driver, now=completion_time + float(i))
        if i < 2:
            assert result == []
    assert result[0].control_id == "active_energy"
    assert result[0].error is None
    assert result[0].value == _energy_bytes_to_kwh_str(0, energy_bytes_attempt_2)

    # Restarting the cycle resets DTR0 back to byte 0x05.
    first_post_failure_chunk = sent_calls[2]
    assert isinstance(first_post_failure_chunk[1], DTR0)
    assert first_post_failure_chunk[1].param == 0x05


@pytest.mark.asyncio
async def test_type51_chunked_poll_no_publish_on_partial():
    dev = await _make_initialized_device_with_type51(scale_byte=0)
    sent_calls = []

    async def fake_send(cmds):
        sent_calls.append(list(cmds))
        return [MagicMock(), MagicMock(), _ok_byte_response(0xAB), _ok_byte_response(0xCD)]

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=fake_send)

    assert await _run_one_chunk(dev, driver, now=0.0) == []
    assert await _run_one_chunk(dev, driver, now=1.0) == []
    final = await _run_one_chunk(dev, driver, now=2.0)
    assert len(final) == 1
    assert final[0].error is None


@pytest.mark.asyncio
async def test_type51_refresh_paced_120s_after_success():
    dev = await _make_initialized_device_with_type51(scale_byte=0)

    async def fake_send(cmds):
        del cmds
        return [MagicMock(), MagicMock(), _ok_byte_response(0), _ok_byte_response(0)]

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=fake_send)

    # Spread the 3 chunks across 60 seconds so cycle-start (t=0) and cycle-end (t=60) differ.
    chunk_times = [0.0, 30.0, 60.0]
    for t in chunk_times:
        await _run_one_chunk(dev, driver, now=t)
    assert driver.send_commands.await_count == 3
    cycle_end = chunk_times[-1]

    # 120 s window measured from cycle-end, not cycle-start: at cycle-start + 150 we are still
    # inside the window (90 s past cycle-end) — no new cycle.
    res = dev.poll_controls(
        driver, now=cycle_end + 119.0, max_commands=3, default_max_commands=3, default_poll_interval=5.0
    )
    assert res.poll_coroutine is None
    # Past cycle-end + 120: new cycle starts.
    res = dev.poll_controls(
        driver, now=cycle_end + 121.0, max_commands=3, default_max_commands=3, default_poll_interval=5.0
    )
    assert res.poll_coroutine is not None
    await res.poll_coroutine()
    sent_call = driver.send_commands.await_args.args[0]
    assert isinstance(sent_call[1], DTR0)
    assert sent_call[1].param == 0x05


@pytest.mark.asyncio
async def test_type51_refresh_paced_120s_after_failure():
    dev = await _make_initialized_device_with_type51(scale_byte=0)
    sent_calls = []

    async def fake_send(cmds):
        sent_calls.append(list(cmds))
        # First chunk succeeds, second chunk fails — so cycle-start and cycle-end differ.
        if len(sent_calls) == 1:
            return [MagicMock(), MagicMock(), _ok_byte_response(0), _ok_byte_response(0)]
        return [MagicMock(), MagicMock(), _bad_response(), _ok_byte_response(0)]

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=fake_send)

    assert await _run_one_chunk(dev, driver, now=0.0) == []
    failure_now = 30.0
    failure = await _run_one_chunk(dev, driver, now=failure_now)
    assert failure[0].error == "r"
    assert driver.send_commands.await_count == 2

    # 120 s window measured from cycle-end (failure_now), not cycle-start (t=0).
    res = dev.poll_controls(
        driver,
        now=failure_now + 119.0,
        max_commands=3,
        default_max_commands=3,
        default_poll_interval=5.0,
    )
    assert res.poll_coroutine is None
    res = dev.poll_controls(
        driver,
        now=failure_now + 121.0,
        max_commands=3,
        default_max_commands=3,
        default_poll_interval=5.0,
    )
    assert res.poll_coroutine is not None
    await res.poll_coroutine()
    sent_call = driver.send_commands.await_args.args[0]
    assert isinstance(sent_call[1], DTR0)
    assert sent_call[1].param == 0x05


@pytest.mark.asyncio
async def test_type51_mqtt_control_error_when_bank_202_unresponsive():
    # Scale unknown (initialize failed to read it).
    dev = await _make_initialized_device_with_type51(scale_byte=None)
    control_ids = [c.id for c in dev.get_mqtt_controls()]
    assert "active_energy" in control_ids

    async def fake_send_failing(cmds):
        del cmds
        return [MagicMock(), MagicMock(), _bad_response(), _bad_response()]

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=fake_send_failing)
    result = await _run_one_chunk(dev, driver, now=0.0)
    assert result[0].error == "r"

    energy_bytes = [0, 0, 0, 0, 0x10, 0x00]  # 4096 Wh

    sent_calls = []

    async def fake_send_ok(cmds):
        sent_calls.append(list(cmds))
        if len(sent_calls) == 1:
            return [MagicMock(), MagicMock(), _ok_byte_response(0), _ok_byte_response(0)]
        energy_idx = (len(sent_calls) - 2) * 2
        return [
            MagicMock(),
            MagicMock(),
            _ok_byte_response(energy_bytes[energy_idx]),
            _ok_byte_response(energy_bytes[energy_idx + 1]),
        ]

    driver.send_commands = AsyncMock(side_effect=fake_send_ok)
    now = 200.0  # Past the 120 s pacing window.
    res_scale = await _run_one_chunk(dev, driver, now=now)
    res1 = await _run_one_chunk(dev, driver, now=now + 1)
    res2 = await _run_one_chunk(dev, driver, now=now + 2)
    res3 = await _run_one_chunk(dev, driver, now=now + 3)
    assert res_scale == []
    assert res1 == []
    assert res2 == []
    assert res3[0].error is None
    assert res3[0].value == _energy_bytes_to_kwh_str(0, energy_bytes)

    assert isinstance(sent_calls[0][1], DTR0)
    assert sent_calls[0][1].param == 0x04
    assert isinstance(sent_calls[1][1], DTR0)
    assert sent_calls[1][1].param == 0x05
