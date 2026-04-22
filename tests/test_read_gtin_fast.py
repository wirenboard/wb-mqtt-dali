"""Tests for ``read_gtin_fast`` — the low-traffic commissioning-path GTIN read."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dali.address import GearShort
from dali.exceptions import MemoryLocationNotImplemented
from dali.gear.general import DTR0, DTR1, QueryVersionNumber, ReadMemoryLocation

from wb.mqtt_dali.common_dali_device import read_gtin_fast, read_product_name
from wb.mqtt_dali.dali_compat import DaliCommandsCompatibilityLayer

# pylint: disable=protected-access


def _ok_response(value: int):
    """Build a response mock matching the shape expected by the sequence code."""
    return SimpleNamespace(raw_value=SimpleNamespace(error=False, as_integer=value))


def _empty_response():
    """Mimic a gear that does not respond to the read (``raw_value is None``)."""
    return SimpleNamespace(raw_value=None)


class _SequenceDriver:
    """Replays the ``run_sequence`` contract against a scripted bank-to-bytes map.

    Records every command yielded by the sequence so tests can assert on the
    exact DALI traffic. Feeds back successful responses for ``ReadMemoryLocation``
    and empty responses for everything else.
    """

    # pylint: disable=too-few-public-methods

    def __init__(self, bank_bytes, *, raise_not_implemented_for=()):
        # bank_bytes: dict[int, list[int] | None] — bytes returned per bank.
        # ``None`` means "no response for this bank" (bytes missing).
        self._bank_bytes = bank_bytes
        self._raise = set(raise_not_implemented_for)
        self.commands: list = []
        self.current_bank = None

    def _handle_batch(self, cmd_list):
        self.commands.extend(cmd_list)
        if not cmd_list or not isinstance(cmd_list[0], ReadMemoryLocation):
            return [SimpleNamespace(raw_value=None) for _ in cmd_list]
        if self.current_bank in self._raise:
            raise MemoryLocationNotImplemented(f"bank {self.current_bank} not implemented")
        bytes_ = self._bank_bytes.get(self.current_bank)
        if bytes_ is None:
            return [_empty_response() for _ in cmd_list]
        # Per-byte: ``None`` entries map to a no-response; ints map to OK responses.
        return [_empty_response() if b is None else _ok_response(b) for b in bytes_]

    def _handle_single(self, cmd):
        self.commands.append(cmd)
        if isinstance(cmd, DTR1):
            self.current_bank = cmd.frame.as_integer & 0xFF
        return SimpleNamespace(raw_value=None)

    async def run_sequence(self, seq, progress=None):
        del progress
        response = None
        started = False
        try:
            while True:
                try:
                    cmd = next(seq) if not started else seq.send(response)
                    started = True
                except StopIteration as stop:
                    return stop.value
                if isinstance(cmd, list):
                    response = self._handle_batch(cmd)
                else:
                    response = self._handle_single(cmd)
        finally:
            seq.close()


def _bytes_for_gtin(gtin: int) -> list:
    """Return the 6 big-endian bytes for a GTIN integer (bank-0 layout)."""
    return list(gtin.to_bytes(6, "big"))


# -------- 1. Happy path --------
@pytest.mark.asyncio
async def test_read_gtin_fast_returns_decoded_gtin_from_bank_0():
    known_gtin = 8721103129109
    driver = _SequenceDriver({0: _bytes_for_gtin(known_gtin)})

    result = await read_gtin_fast(driver, GearShort(5), DaliCommandsCompatibilityLayer())

    assert result == known_gtin

    # Traffic assertion: DTR1(0) + DTR0(3) + 6 ReadMemoryLocation — nothing else.
    dtr1_cmds = [c for c in driver.commands if isinstance(c, DTR1)]
    dtr0_cmds = [c for c in driver.commands if isinstance(c, DTR0)]
    read_cmds = [c for c in driver.commands if isinstance(c, ReadMemoryLocation)]

    assert len(dtr1_cmds) == 1
    assert dtr1_cmds[0].frame.as_integer & 0xFF == 0  # bank 0
    assert len(dtr0_cmds) == 1
    assert dtr0_cmds[0].frame.as_integer & 0xFF == 0x03  # GTIN start offset
    assert len(read_cmds) == 6

    # Optimization invariant: exactly 8 commands total (1×DTR1 + 1×DTR0 + 6×Read)
    # — no QueryVersionNumber, no LastAddress probe.
    assert len(driver.commands) == 8
    assert not any(isinstance(c, QueryVersionNumber) for c in driver.commands)


# -------- 2. Short-circuit: bank 0 valid → bank 1 never read --------
@pytest.mark.asyncio
async def test_read_gtin_fast_skips_bank_1_when_bank_0_has_valid_gtin():
    driver = _SequenceDriver({0: _bytes_for_gtin(42), 1: _bytes_for_gtin(999)})

    result = await read_gtin_fast(driver, GearShort(3), DaliCommandsCompatibilityLayer())

    assert result == 42

    bank_1_dtr1 = [c for c in driver.commands if isinstance(c, DTR1) and c.frame.as_integer & 0xFF == 1]
    # No DTR1 selecting bank 1 → bank 1 was never touched.
    assert bank_1_dtr1 == []


# -------- 3. Fallback: bank 0 unprogrammed → read bank 1 --------
@pytest.mark.asyncio
async def test_read_gtin_fast_falls_back_to_bank_1_when_bank_0_is_all_ff():
    driver = _SequenceDriver(
        {
            0: [0xFF] * 6,
            1: _bytes_for_gtin(7777),
        }
    )

    result = await read_gtin_fast(driver, GearShort(4), DaliCommandsCompatibilityLayer())

    assert result == 7777

    # Both banks were selected via DTR1.
    dtr1_banks = [c.frame.as_integer & 0xFF for c in driver.commands if isinstance(c, DTR1)]
    assert dtr1_banks == [0, 1]


# -------- 4. Both banks empty → None --------
@pytest.mark.asyncio
async def test_read_gtin_fast_returns_none_when_both_banks_are_empty():
    driver = _SequenceDriver(
        {
            0: [0xFF] * 6,
            1: [0xFF] * 6,
        }
    )

    result = await read_gtin_fast(driver, GearShort(2), DaliCommandsCompatibilityLayer())

    assert result is None


# -------- 5. Bank 0 raises MemoryLocationNotImplemented → silently try bank 1 --------
@pytest.mark.asyncio
async def test_read_gtin_fast_silently_handles_bank_0_memory_not_implemented():
    driver = _SequenceDriver(
        {1: _bytes_for_gtin(12345)},
        raise_not_implemented_for=(0,),
    )

    result = await read_gtin_fast(driver, GearShort(1), DaliCommandsCompatibilityLayer())

    assert result == 12345


# -------- 5b. Bank 0 silent → catastrophic bus error, propagate RuntimeError --------
@pytest.mark.asyncio
async def test_read_gtin_fast_raises_when_bank_0_has_no_responses():
    # A bank that does not respond at all (distinct from MemoryLocationNotImplemented)
    # is treated as a catastrophic bus error — no fallback to bank 1.
    driver = _SequenceDriver({0: None, 1: _bytes_for_gtin(5555)})

    with pytest.raises(RuntimeError):
        await read_gtin_fast(driver, GearShort(6), DaliCommandsCompatibilityLayer())

    # Bank 1 must NOT have been attempted after the bank 0 failure.
    dtr1_banks = [c.frame.as_integer & 0xFF for c in driver.commands if isinstance(c, DTR1)]
    assert 1 not in dtr1_banks


# -------- 5c. Bank 0 partial response → catastrophic bus error, propagate RuntimeError --------
@pytest.mark.asyncio
async def test_read_gtin_fast_raises_when_bank_0_has_partial_response():
    # Partial response (some bytes missing) also indicates a bus-level failure,
    # not an "absent bank" — must not silently fall back to bank 1.
    driver = _SequenceDriver(
        {
            0: [0x12, 0x34, 0x56, 0x78, None, None],
            1: _bytes_for_gtin(6543210),
        }
    )

    with pytest.raises(RuntimeError):
        await read_gtin_fast(driver, GearShort(8), DaliCommandsCompatibilityLayer())

    dtr1_banks = [c.frame.as_integer & 0xFF for c in driver.commands if isinstance(c, DTR1)]
    assert 1 not in dtr1_banks


# -------- 6. Integration: read_product_name now uses read_gtin_fast --------
@pytest.mark.asyncio
async def test_read_product_name_delegates_to_read_gtin_fast():
    driver = AsyncMock()
    gtin_db = MagicMock()
    gtin_db.get_info_by_gtin.return_value = {"product_name": "FastLamp"}

    with patch(
        "wb.mqtt_dali.common_dali_device.read_gtin_fast",
        new=AsyncMock(return_value=8721103129109),
    ) as fast_spy:
        name = await read_product_name(driver, GearShort(7), DaliCommandsCompatibilityLayer(), gtin_db)

    assert name == "FastLamp"
    fast_spy.assert_awaited_once()
    gtin_db.get_info_by_gtin.assert_called_once_with(8721103129109)
