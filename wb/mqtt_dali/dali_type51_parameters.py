# Type 51 Energy reporting (IEC 62386-252:2023)

import logging
from dataclasses import dataclass, field
from typing import Optional

from dali.address import Address, GearShort
from dali.exceptions import MemoryLocationNotImplemented, ResponseError
from dali.memory.energy import (
    BANK_202,
    BANK_203,
    BANK_204,
    ActiveEnergy,
    ActiveEnergyLoadside,
    ActivePower,
    ActivePowerLoadside,
    ApparentEnergy,
    ApparentPower,
)

from .common_dali_device import (
    ControlPollResult,
    ControlsPollRequestResult,
    MqttControlBase,
    PropertyStartOrder,
    read_bank_as_dict,
    try_read_bank_as_dict,
)
from .dali_compat import DaliCommandsCompatibilityLayer
from .dali_parameters import TypeParameters
from .device_publisher import ControlInfo
from .settings import SettingsParamBase, SettingsParamName
from .wbdali import FramePriority, WBDALIDriver
from .wbmqtt import ControlMeta, TranslatedTitle

# Bank 202 / 203 / 204 layout per IEC 62386-252:2023.
# Energy (totalizer): scale at 0x04, six bytes at 0x05..0x0a.
_ENERGY_SCALE_ADDR = 0x04
_ENERGY_FIRST_DATA_ADDR = 0x05
_ENERGY_DATA_LEN = 6  # bytes
_ENERGY_CHUNK_SIZE = 2  # bytes read per polling tick via auto-increment DTR0

# 120 s pacing matches typical 1 Wh totalizer resolution (a 50 W luminaire ticks
# every ~72 s; faster polling produces near-duplicate reads).
_REFRESH_INTERVAL_S = 120.0

_ACTIVE_ENERGY_CONTROL_ID = "active_energy"


@dataclass
class _Type51EnergyReadProgress:
    """In-flight state of the chunked active-energy read.

    The buffer accumulates the six energy bytes by chunk index 0..2 (each chunk
    is two bytes). A self-contained chunk (DTR1 + DTR0 + 2x ReadMemoryLocation)
    is issued per polling tick; a failure resets the buffer so the next tick
    starts again from chunk 0.
    """

    bytes_read: list = field(default_factory=list)

    @property
    def next_chunk_index(self) -> int:
        return len(self.bytes_read) // _ENERGY_CHUNK_SIZE

    @property
    def next_data_address(self) -> int:
        return _ENERGY_FIRST_DATA_ADDR + len(self.bytes_read)

    def is_complete(self) -> bool:
        return len(self.bytes_read) >= _ENERGY_DATA_LEN


def _int_or_none(value) -> Optional[int]:
    # read_bank_as_dict already strips FlagValue entries; value here is None,
    # an int, or a numeric Decimal.
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _kwh_or_none(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value) / 1000.0, 3)
    except (TypeError, ValueError):
        return None


def _extract_energy_power(
    data: dict,
    energy_cls,
    power_cls,
    energy_key: str,
    power_key: str,
) -> dict:
    out: dict = {}
    energy = _kwh_or_none(data.get(energy_cls.name))
    if energy is not None:
        out[energy_key] = energy
    power = _int_or_none(data.get(power_cls.name))
    if power is not None:
        out[power_key] = power
    return out


class Type51EnergyParam(SettingsParamBase):
    """Settings-page handler for DT51 banks 202/203/204.

    `read` returns ``{"energy_reporting": {...}}`` with the six possible
    fields flattened into a single card. Bank 202 is mandatory — if it cannot
    be read, ``RuntimeError`` is raised. Banks 203 and 204 are optional —
    their fields are simply omitted when the bank is missing or unreadable.
    Energy values are in kWh / kVAh (float, 3 decimals); power values in
    W / VA (int).
    """

    def __init__(self) -> None:
        super().__init__(SettingsParamName("Energy reporting", "Отчёт об энергии"))
        self._compat = DaliCommandsCompatibilityLayer()
        self._has_apparent = False
        self._has_loadside = False

    async def read(
        self, driver: WBDALIDriver, short_address: Address, logger: Optional[logging.Logger] = None
    ) -> dict:
        del logger

        active_data = await read_bank_as_dict(
            driver,
            BANK_202,
            short_address,
            self._compat,
            error_label="DT51 memory bank 202",
        )

        self._has_apparent = False
        self._has_loadside = False
        fields: dict = {}
        fields.update(
            _extract_energy_power(active_data, ActiveEnergy, ActivePower, "active_energy", "active_power")
        )

        apparent_data = await try_read_bank_as_dict(driver, BANK_203, short_address, self._compat)
        if apparent_data is not None:
            self._has_apparent = True
            fields.update(
                _extract_energy_power(
                    apparent_data, ApparentEnergy, ApparentPower, "apparent_energy", "apparent_power"
                )
            )

        loadside_data = await try_read_bank_as_dict(driver, BANK_204, short_address, self._compat)
        if loadside_data is not None:
            self._has_loadside = True
            fields.update(
                _extract_energy_power(
                    loadside_data,
                    ActiveEnergyLoadside,
                    ActivePowerLoadside,
                    "loadside_energy",
                    "loadside_power",
                )
            )

        return {"energy_reporting": fields}

    def get_schema(self, group_and_broadcast: bool) -> dict:
        if group_and_broadcast:
            return {}

        options = {
            "wb": {
                "read_only": True,
            },
            "grid_columns": 6,
        }
        inner_properties: dict = {}
        translations_ru: dict = {self.name.en: self.name.ru}

        # Active fields are always declared (bank 202 is mandatory; if missing
        # at read time the params simply won't carry values).
        inner_properties["active_energy"] = {
            "type": "number",
            "title": "Active energy, kWh",
            "options": options,
            "propertyOrder": 1,
        }
        inner_properties["active_power"] = {
            "type": "integer",
            "title": "Active power, W",
            "options": options,
            "propertyOrder": 2,
        }
        translations_ru["Active energy, kWh"] = "Активная энергия, кВт·ч"
        translations_ru["Active power, W"] = "Активная мощность, Вт"

        if self._has_apparent:
            inner_properties["apparent_energy"] = {
                "type": "number",
                "title": "Apparent energy, kVAh",
                "options": options,
                "propertyOrder": 3,
            }
            inner_properties["apparent_power"] = {
                "type": "integer",
                "title": "Apparent power, VA",
                "options": options,
                "propertyOrder": 4,
            }
            translations_ru["Apparent energy, kVAh"] = "Кажущаяся энергия, кВА·ч"
            translations_ru["Apparent power, VA"] = "Кажущаяся мощность, ВА"

        if self._has_loadside:
            inner_properties["loadside_energy"] = {
                "type": "number",
                "title": "Load side energy, kWh",
                "options": options,
                "propertyOrder": 5,
            }
            inner_properties["loadside_power"] = {
                "type": "integer",
                "title": "Load side power, W",
                "options": options,
                "propertyOrder": 6,
            }
            translations_ru["Load side energy, kWh"] = "Энергия на нагрузке, кВт·ч"
            translations_ru["Load side power, W"] = "Мощность на нагрузке, Вт"

        return {
            "properties": {
                "energy_reporting": {
                    "type": "object",
                    "title": self.name.en,
                    "format": "card",
                    "propertyOrder": PropertyStartOrder.DT51.value,
                    "options": {"wb": {"read_only": True}},
                    "properties": inner_properties,
                },
            },
            "translations": {"ru": translations_ru},
        }


async def _read_active_scale(
    driver: WBDALIDriver, short_address: Address, compat: DaliCommandsCompatibilityLayer
) -> int:
    """Read the active-energy ROM scale factor (bank 202, addr 0x04)."""
    responses = await driver.send_commands(
        [
            compat.DTR1(BANK_202.address),
            compat.DTR0(_ENERGY_SCALE_ADDR),
            compat.ReadMemoryLocation(short_address),
        ],
        priority=FramePriority.CONFIGURATION,
    )
    response = responses[-1]
    if response.raw_value is None:
        raise MemoryLocationNotImplemented(
            f"Scale factor not implemented at bank 202 addr {_ENERGY_SCALE_ADDR}"
        )
    if response.raw_value.error:
        raise ResponseError("Framing error reading bank 202 scale factor")
    return response.raw_value.as_integer


class _ActiveEnergyControl(MqttControlBase):
    """Read-only `value` control for the active-energy totalizer.

    Has no `query_builder` — Type51Parameters owns the chunked polling. The
    control is registered so MQTT shows the field as soon as the device has
    initialized, even before the first cycle completes.
    """

    def __init__(self) -> None:
        super().__init__(
            ControlInfo(
                _ACTIVE_ENERGY_CONTROL_ID,
                ControlMeta(
                    "value",
                    title=TranslatedTitle("Active energy", "Активная энергия"),
                    read_only=True,
                    units="kWh",
                ),
            ),
        )

    def is_readable(self) -> bool:
        return False


class Type51Parameters(TypeParameters):  # pylint: disable=too-many-instance-attributes
    """DT51 (Energy reporting) integration.

    Owns:
      * a `Type51EnergyParam` for the settings page (banks 202/203/204);
      * the chunked active-energy polling protocol implementing the `Pollable`
        interface from `common_dali_device`, so `DaliDevice` schedules it
        alongside regular controls and `Type8Parameters`.
    """

    def __init__(self) -> None:
        super().__init__()
        self._compat = DaliCommandsCompatibilityLayer()
        self._energy_param = Type51EnergyParam()
        self._parameters = [self._energy_param]

        self._scale_byte: Optional[int] = None
        self._read_progress: Optional[_Type51EnergyReadProgress] = None

        # None means "no completed cycle yet, run immediately".
        self._last_cycle_end_time: Optional[float] = None

        self.poll_interval: Optional[float] = None
        self.last_poll_time: Optional[float] = None

    @property
    def scale_byte(self) -> Optional[int]:
        return self._scale_byte

    @scale_byte.setter
    def scale_byte(self, value: Optional[int]) -> None:
        self._scale_byte = value

    async def read_mandatory_info(
        self,
        driver: WBDALIDriver,
        short_address: GearShort,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        # Cache the ROM scale byte at init time so chunked cycles can decode
        # without re-reading it; in-cycle fallback covers init-time failures.
        try:
            self._scale_byte = await _read_active_scale(driver, short_address, self._compat)
        except Exception as e:  # pylint: disable=broad-exception-caught
            if logger is not None:
                logger.info("DT51 scale factor not available yet for %s: %s", short_address, e)
            self._scale_byte = None

    def get_mqtt_controls(self) -> list[MqttControlBase]:
        return [_ActiveEnergyControl()]

    def is_poll_due(self, now: float, default_poll_interval: float) -> bool:
        del default_poll_interval
        if self._read_progress is not None:
            return True
        if self._last_cycle_end_time is None:
            return True
        return now - self._last_cycle_end_time >= _REFRESH_INTERVAL_S

    def cancel_pending_poll(self) -> None:
        self._read_progress = None

    def has_in_progress_read(self) -> bool:
        """Whether a multi-tick energy read is in flight.

        Public observable used by tests and debug code; not part of the
        `Pollable` protocol (the polling loop tracks in-progress state via
        `has_more` / "pollable is still at the head of the round").
        """
        return self._read_progress is not None

    def next_poll_step(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        driver: WBDALIDriver,
        address: Address,
        max_commands: int,
        default_max_commands: int,
        now: float,
        logger: Optional[logging.Logger] = None,
    ) -> ControlsPollRequestResult:
        del logger
        # DT51 chunks are 4 cmds and the default tick budget is 3 — overshoot
        # is allowed but only when no other commands have been issued this tick.
        if max_commands < default_max_commands:
            return ControlsPollRequestResult(has_more=True)
        if (
            self._read_progress is None
            and self._last_cycle_end_time is not None
            and now - self._last_cycle_end_time < _REFRESH_INTERVAL_S
        ):
            return ControlsPollRequestResult(has_more=False)
        if self._read_progress is None:
            self._read_progress = _Type51EnergyReadProgress()
        self.last_poll_time = now
        progress = self._read_progress
        # Scale-byte fallback when init failed: first chunk re-reads scale, energy chunks follow.
        if self._scale_byte is None and not progress.bytes_read:
            return ControlsPollRequestResult(
                has_more=True,
                poll_coroutine=lambda: self._do_scale_chunk(driver, address, now),
                commands_count=4,
            )
        more_after_this = progress.next_chunk_index < (_ENERGY_DATA_LEN // _ENERGY_CHUNK_SIZE) - 1
        return ControlsPollRequestResult(
            has_more=more_after_this,
            poll_coroutine=lambda: self._do_chunk(driver, address, progress, now),
            commands_count=4,
        )

    async def _do_scale_chunk(
        self, driver: WBDALIDriver, address: Address, tick_now: float
    ) -> list[ControlPollResult]:
        # 4-cmd chunk reading scale at 0x04. The second ReadMemoryLocation advances
        # DTR0 past addr 0x05; its result is discarded and energy re-read next tick.
        cmds = [
            self._compat.DTR1(BANK_202.address),
            self._compat.DTR0(_ENERGY_SCALE_ADDR),
            self._compat.ReadMemoryLocation(address),
            self._compat.ReadMemoryLocation(address),
        ]
        try:
            responses = await driver.send_commands(cmds, priority=FramePriority.PERIODIC_QUERY)
        except Exception:  # pylint: disable=broad-exception-caught
            return self._fail_cycle(tick_now)
        scale_resp = responses[2]
        raw = getattr(scale_resp, "raw_value", None)
        if raw is None or getattr(raw, "error", False):
            return self._fail_cycle(tick_now)
        self._scale_byte = raw.as_integer
        return []

    async def _do_chunk(
        self,
        driver: WBDALIDriver,
        address: Address,
        progress: _Type51EnergyReadProgress,
        tick_now: float,
    ) -> list[ControlPollResult]:
        cmds = [
            self._compat.DTR1(BANK_202.address),
            self._compat.DTR0(progress.next_data_address),
            self._compat.ReadMemoryLocation(address),
            self._compat.ReadMemoryLocation(address),
        ]
        try:
            responses = await driver.send_commands(cmds, priority=FramePriority.PERIODIC_QUERY)
        except Exception:  # pylint: disable=broad-exception-caught
            return self._fail_cycle(tick_now)

        read_responses = responses[-_ENERGY_CHUNK_SIZE:]
        new_bytes: list = []
        for response in read_responses:
            raw = getattr(response, "raw_value", None)
            if raw is None or getattr(raw, "error", False):
                return self._fail_cycle(tick_now)
            new_bytes.append(raw.as_integer)

        progress.bytes_read.extend(new_bytes)

        if not progress.is_complete():
            return []

        return self._finish_cycle(progress.bytes_read, tick_now)

    def _fail_cycle(self, cycle_end_time: float) -> list[ControlPollResult]:
        self._read_progress = None
        self._last_cycle_end_time = cycle_end_time
        return [ControlPollResult(control_id=_ACTIVE_ENERGY_CONTROL_ID, value="", error="r")]

    def _finish_cycle(self, energy_bytes: list, cycle_end_time: float) -> list[ControlPollResult]:
        self._read_progress = None
        self._last_cycle_end_time = cycle_end_time
        if self._scale_byte is None:
            return [ControlPollResult(control_id=_ACTIVE_ENERGY_CONTROL_ID, value="", error="r")]
        raw = bytes([self._scale_byte, *energy_bytes])
        check = ActiveEnergy.check_raw(raw)
        if check is not None:
            # MASK / TMASK / Invalid — surface as error so the UI is not misled.
            return [ControlPollResult(control_id=_ACTIVE_ENERGY_CONTROL_ID, value="", error="r")]
        value = ActiveEnergy.raw_to_value(raw)
        kwh = float(value) / 1000.0
        return [ControlPollResult(control_id=_ACTIVE_ENERGY_CONTROL_ID, value=f"{kwh:.3f}", error=None)]
