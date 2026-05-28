import asyncio
import json
import logging
import uuid
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Protocol, Union

import jsonschema
from dali.address import Address
from dali.command import Command, Response
from dali.exceptions import MemoryLocationNotImplemented, ResponseError
from dali.memory import info, location, oem
from dali.memory.location import FlagValue

from .dali2_compat import Dali2CommandsCompatibilityLayer
from .dali_compat import DaliCommandsCompatibilityLayer
from .device_publisher import ControlInfo
from .gtin_db import DaliDatabase
from .settings import SettingsParamBase, SettingsParamName
from .utils import merge_json_schemas
from .wbdali import FramePriority, WBDALIDriver
from .wbdali_utils import (
    check_query_response,
    is_transmission_error_response,
    query_response,
    send_commands_with_retry,
)
from .wbmqtt import TranslatedTitle


class PropertyStartOrder(Enum):
    DT50 = 20  # Luminaire info
    COMMON = 100
    POWER_ON_LEVEL = 105
    SYSTEM_FAILURE_LEVEL = 106
    TC_LIMITS = 200
    DT51 = 450  # Energy reporting
    DT52 = 500  # Diagnostics and maintenance
    SPECIFIC = 600
    GROUPS = 700
    SCENES = 800


def request_with_retry_sequence(cmd):
    last_error = "unknown error"
    for _ in range(3):
        result = yield cmd
        if not is_transmission_error_response(result):
            return result
        last_error = str(result)
    raise RuntimeError(f"No response to {cmd} after 3 attempts; last error: {last_error}")


@dataclass
class ApplyResult:
    needs_mqtt_controls_refresh: bool = False


class MqttControlBase:

    # Marks a read-only control whose value is mirrored to group virtual devices.
    is_group_state_control: bool = False

    value_to_set: Optional[str] = None

    def __init__(self, control_info: ControlInfo, poll_interval: Optional[float] = None) -> None:
        # the property value is used as default value for the control
        self.control_info = control_info
        self.poll_interval: Optional[float] = poll_interval
        self.last_poll_time: Optional[float] = None

    def is_readable(self) -> bool:
        return False

    def is_writable(self) -> bool:
        return False

    def get_query(self, short_address: Address) -> Optional[Command]:
        del short_address

    def format_response(self, response: Response) -> str:
        del response
        return ""

    def format_title(self, response: Response) -> Union[str, TranslatedTitle]:
        del response
        return ""

    def get_setup_commands(self, short_address: Address, value_to_set: str) -> list[Command]:
        del short_address, value_to_set
        return []

    def is_dirty(self) -> bool:
        return self.value_to_set is not None

    def is_poll_due(self, now: float, default_poll_interval: float) -> bool:
        if self.last_poll_time is None:
            return True
        poll_interval = self.poll_interval if self.poll_interval is not None else default_poll_interval
        return now - self.last_poll_time >= poll_interval

    def next_poll_step(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        driver: "WBDALIDriver",
        address: Address,
        max_commands: int,
        default_max_commands: int,
        now: float,
        logger: Optional[logging.Logger] = None,
    ) -> "ControlsPollRequestResult":
        del default_max_commands
        if max_commands < 1:
            return ControlsPollRequestResult(has_more=True)
        self.last_poll_time = now
        return ControlsPollRequestResult(
            has_more=False,
            poll_coroutine=lambda: self._run_single_query(driver, address, logger),
            commands_count=1,
        )

    def cancel_pending_poll(self) -> None:
        pass

    async def _run_single_query(
        self,
        driver: "WBDALIDriver",
        address: Address,
        logger: Optional[logging.Logger] = None,
    ) -> list["ControlPollResult"]:
        try:
            # pylint: disable-next=assignment-from-no-return
            query = self.get_query(address)
            responses = await send_commands_with_retry(driver, [query], priority=FramePriority.PERIODIC_QUERY)
            response = responses[0]
            try:
                check_query_response(response)
            except RuntimeError:
                return [ControlPollResult(control_id=self.control_info.id, value="", error="r")]

            if self.control_info.meta.control_type == "alarm":
                title = self.format_title(response)
                value = self.format_response(response)
                return [ControlPollResult(control_id=self.control_info.id, value=value, title=title)]

            return [ControlPollResult(control_id=self.control_info.id, value=self.format_response(response))]
        except Exception as e:  # pylint: disable=broad-exception-caught
            if logger is not None:
                logger.warning("Failed to poll control %s: %s", self.control_info.id, e)
            return [ControlPollResult(control_id=self.control_info.id, value="", error="r")]


class MqttControl(MqttControlBase):
    def __init__(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        control_info: ControlInfo,
        query_builder: Optional[Callable[[Address], object]] = None,
        value_formatter: Optional[Callable[[Response], str]] = None,
        commands_builder: Optional[Callable[[Address, str], list[Command]]] = None,
        is_group_state_control: bool = False,
        poll_interval: Optional[float] = None,
    ) -> None:
        super().__init__(control_info, poll_interval)
        self.query_builder = query_builder
        self.value_formatter = value_formatter
        self.commands_builder = commands_builder
        self.is_group_state_control = is_group_state_control

    def get_query(self, short_address: Address) -> Optional[Command]:
        if self.query_builder is not None:
            return self.query_builder(short_address)
        return None

    def format_response(self, response: Response) -> str:
        if self.value_formatter is not None:
            return self.value_formatter(response)
        return ""

    def get_setup_commands(self, short_address: Address, value_to_set: str) -> list[Command]:
        if self.commands_builder is not None:
            return self.commands_builder(short_address, value_to_set)
        return []

    def is_readable(self) -> bool:
        return self.query_builder is not None and self.value_formatter is not None

    def is_writable(self) -> bool:
        return self.commands_builder is not None


@dataclass
class ControlPollResult:
    control_id: str
    value: Optional[str] = None
    error: Optional[str] = None
    title: Optional[Union[str, TranslatedTitle]] = None


ControlPollCoroutine = Callable[[], Awaitable[list[ControlPollResult]]]


@dataclass
class ControlsPollRequestResult:
    has_more: bool
    poll_coroutine: Optional[ControlPollCoroutine] = None
    commands_count: int = 0


class Pollable(Protocol):
    """Structural interface for anything `DaliDeviceBase.poll_controls` rotates.

    Regular MQTT controls (`MqttControlBase`) and chunked handlers
    (`Type8Parameters`, `Type51Parameters`) implement it. `last_poll_time` is
    owned by the pollable: it stamps the field inside `next_poll_step` when it
    actually commits to a poll (single-shot controls on every dispatch; chunked
    handlers only when starting a new cycle). `is_poll_due` /
    `time_until_next_poll` read it. `next_poll_step` returns the plan for the
    current tick (a coroutine plus command-cost), with `has_more=True`
    signalling the pollable wants to stay at the head of the round for the
    next tick. Multi-tick state is encoded entirely by `has_more`.
    """

    last_poll_time: Optional[float]
    poll_interval: Optional[float]

    def is_poll_due(self, now: float, default_poll_interval: float) -> bool:
        """Whether the pollable is eligible for the next round."""

    def next_poll_step(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        driver: Any,
        address: Address,
        max_commands: int,
        default_max_commands: int,
        now: float,
        logger: Optional[logging.Logger] = None,
    ) -> ControlsPollRequestResult:
        """Plan for the current tick: optional coroutine + command-cost + has_more.

        ``has_more=True`` keeps the pollable at the head of the round for the
        next tick. ``poll_coroutine=None`` means skip dispatch this tick.
        """

    def cancel_pending_poll(self) -> None:
        """Drop any in-flight multi-tick state. No-op for single-shot pollables."""


@dataclass
class DaliDeviceAddress:
    short: int
    random: int


def read_memory_bank(  # pylint: disable=too-many-locals, too-many-branches
    bank: info.MemoryBank,
    short_address: Address,
    compat: Union[DaliCommandsCompatibilityLayer, Dali2CommandsCompatibilityLayer],
):
    for i in range(3):
        try:
            last_address = yield from bank.LastAddress.read(short_address)
            if isinstance(last_address, location.FlagValue):
                raise ResponseError(
                    f"Cannot read memory bank {bank.address}: last address location is {last_address.value}"
                )
            break
        except Exception:  # pylint: disable=broad-exception-caught
            if i == 2:
                raise
    # Reading the last address also sets DTR1 appropriately

    # Bank 0 has a useful value at address 0x02; all other banks
    # use this for the lock/latch byte
    start_address = 0x02 if bank.address == 0 else 0x03
    commands_count = last_address - start_address + 1
    raw_data = [None] * start_address

    first_unread_index = 0
    last_error = "unknown error"
    for _ in range(3):
        current_address = start_address + first_unread_index
        remaining_count = commands_count - first_unread_index
        if remaining_count <= 0:
            break

        yield from request_with_retry_sequence(compat.DTR0(current_address))
        responses = yield [compat.ReadMemoryLocation(short_address) for _ in range(remaining_count)]

        next_unread_index: Optional[int] = None
        for offset, response in enumerate(responses):
            i = first_unread_index + offset
            try:
                if response.raw_value is not None:
                    if response.raw_value.error:
                        next_unread_index = i
                        last_error = "framing error"
                        break
                    raw_data.append(response.raw_value.as_integer)
                else:
                    raw_data.append(None)
            except RuntimeError as e:
                next_unread_index = i
                last_error = str(e)
                break

        if next_unread_index is None:
            break
        first_unread_index = next_unread_index
    else:
        failed_address = start_address + first_unread_index
        raise RuntimeError(
            f"Failed to read memory bank {bank.address} for {short_address}: "
            f"stopped at location {failed_address} (offset {first_unread_index}) "
            f"after 3 attempts; last error: {last_error}"
        )

    result = {}
    for memory_value in bank.values:
        try:
            r = memory_value.from_list(raw_data)
        except MemoryLocationNotImplemented:
            pass
        else:
            result[memory_value] = r
    return result


async def read_bank_as_dict(
    driver: WBDALIDriver,
    bank: info.MemoryBank,
    short_address: Address,
    compat: Union[DaliCommandsCompatibilityLayer, Dali2CommandsCompatibilityLayer],
    error_label: Optional[str] = None,
) -> dict[str, Any]:
    """Read memory bank and return ``{value.name: parsed}`` with FlagValue entries skipped.

    Raises ``RuntimeError`` if the bank is not implemented or unresponsive.
    ``error_label`` overrides the generic prefix in the raised RuntimeError —
    callers pass e.g. ``"DT50 memory bank"`` to preserve their existing message.
    """
    try:
        data = await driver.run_sequence(
            read_memory_bank(bank, short_address, compat), FramePriority.CONFIGURATION
        )
    except (MemoryLocationNotImplemented, ResponseError) as e:
        label = error_label or f"memory bank {bank.address}"
        raise RuntimeError(f"Failed to read {label}: {e}") from e
    return {mv.name: parsed for mv, parsed in data.items() if not isinstance(parsed, FlagValue)}


async def try_read_bank_as_dict(
    driver: WBDALIDriver,
    bank: info.MemoryBank,
    short_address: Address,
    compat: Union[DaliCommandsCompatibilityLayer, Dali2CommandsCompatibilityLayer],
) -> Optional[dict[str, Any]]:
    """Like ``read_bank_as_dict`` but returns ``None`` instead of raising on bank failure.

    Use when the bank is optional (e.g. DT51 banks 203/204) and the caller
    can proceed without it.
    """
    try:
        return await read_bank_as_dict(driver, bank, short_address, compat)
    except RuntimeError:
        return None


def _is_empty_memory_int(value: int) -> bool:
    """Return True iff ``value`` is zero or consists only of a run of 0xFF bytes from the LSB upward.

    In other words, the byte-wise little-endian representation of ``value`` must
    be a (possibly empty) contiguous sequence of 0xFF bytes with no other bytes
    present. So ``0``, ``0xFF``, ``0xFFFF``, ``0xFFFFFFFFFFFF`` return True,
    while ``0xFF00FF`` or ``0x01FF`` return False.

    Used to detect unprogrammed numeric values stored in DALI memory banks
    (typical on fresh devices where reserved bytes are left as 0xFF).
    """
    value_to_check = value
    while value_to_check > 0:
        if value_to_check & 0xFF != 0xFF:
            return False
        value_to_check >>= 8
    return True


_GTIN_START_ADDRESS = 0x03
_GTIN_LENGTH = 6


async def _read_gtin_from_bank(
    driver: WBDALIDriver,
    bank_address: int,
    gtin_value_cls,
    short_address: Address,
    compat: Union[Dali2CommandsCompatibilityLayer, DaliCommandsCompatibilityLayer],
) -> Optional[int]:
    """Read the 6 GTIN bytes from a single memory bank and decode them.

    Selects the bank via DTR1, positions DTR0 at the GTIN start offset (0x03)
    and issues all six ``ReadMemoryLocation`` commands in one batch so
    auto-promotion ties the DTR setup to the reads in a single transaction.

    Returns the decoded GTIN integer, or ``None`` if the bank is not programmed
    (all bytes 0xFF). Raises ``MemoryLocationNotImplemented`` if the bank /
    location is absent on the device.
    """
    commands = [
        compat.DTR1(bank_address),
        compat.DTR0(_GTIN_START_ADDRESS),
        *[compat.ReadMemoryLocation(short_address) for _ in range(_GTIN_LENGTH)],
    ]
    responses = await send_commands_with_retry(driver, commands, priority=FramePriority.CONFIGURATION)
    read_responses = responses[2:]
    raw_bytes: list[int] = []
    for response in read_responses:
        try:
            check_query_response(response)
            raw_bytes.append(response.raw_value.as_integer)
        except RuntimeError as e:
            raise RuntimeError(
                f"Failed to read GTIN from memory bank {bank_address} for {short_address}: {e}"
            ) from e
    padded = [None] * _GTIN_START_ADDRESS + raw_bytes
    decoded = gtin_value_cls.from_list(padded)
    if _is_empty_memory_int(decoded):
        return None
    return decoded


async def read_gtin_fast(
    driver: WBDALIDriver,
    short_address: Address,
    compat: Union[Dali2CommandsCompatibilityLayer, DaliCommandsCompatibilityLayer],
) -> Optional[int]:
    """Read the GTIN from bank 0, falling back to bank 1 only when necessary.

    This is the low-traffic variant used during commissioning: it skips the
    ``LastAddress`` probe and ``QueryVersionNumber`` since GTIN sits at the
    fixed offset 0x03..0x08 in both legacy and current bank 0 layouts per
    IEC 62386-102. Bank 1 is read only if bank 0 is absent or unprogrammed.

    Returns the GTIN integer or ``None`` if no programmed GTIN is available.
    ``MemoryLocationNotImplemented`` from the driver is treated as "bank
    absent"; all other driver errors propagate to the caller.
    """
    try:
        gtin = await _read_gtin_from_bank(driver, info.BANK_0.address, info.GTIN, short_address, compat)
        if gtin is not None:
            return gtin
    except MemoryLocationNotImplemented:
        pass

    try:
        return await _read_gtin_from_bank(
            driver, oem.BANK_1.address, oem.ManufacturerGTIN, short_address, compat
        )
    except MemoryLocationNotImplemented:
        return None


async def read_product_name(
    driver: WBDALIDriver,
    short_address: Address,
    compat: Union[Dali2CommandsCompatibilityLayer, DaliCommandsCompatibilityLayer],
    gtin_db: DaliDatabase,
    logger: Optional[logging.Logger] = None,
) -> Optional[str]:
    """Read the GTIN from memory banks and look up the product name.

    Returns the product_name string if the GTIN is found in the database, or
    None on any failure (missing banks, unreadable bus, unknown GTIN).
    Does not raise.
    """
    try:
        gtin = await read_gtin_fast(driver, short_address, compat)
    except Exception as e:  # pylint: disable=broad-exception-caught
        if logger is not None:
            logger.warning(
                "Failed to read memory banks for %s while looking up product name: %s",
                short_address,
                e,
            )
        return None
    if gtin is None:
        if logger is not None:
            logger.debug("No GTIN reported by device at %s; using default name", short_address)
        return None
    product_info = gtin_db.get_info_by_gtin(gtin)
    if product_info is None:
        if logger is not None:
            logger.debug(
                "GTIN %s of device at %s not found in product database; using default name",
                gtin,
                short_address,
            )
        return None
    return product_info.get("product_name")


class GeneralMemoryParams(SettingsParamBase):
    memory_fields_to_json_params = {
        info.GTIN: "gtin",
        info.FirmwareVersion: "firmware_version",
        info.IdentificationNumber: "identification_number",
        info.IdentifictionNumber_legacy: "identification_number",
        info.HardwareVersion: "hardware_version",
        oem.ManufacturerGTIN: "oem_gtin",
        oem.LuminaireID: "oem_identification_number",
    }

    def __init__(
        self,
        compat: Union[Dali2CommandsCompatibilityLayer, DaliCommandsCompatibilityLayer],
        gtin_db: DaliDatabase,
    ) -> None:
        super().__init__(SettingsParamName("General memory parameters", "Общие параметры памяти"))
        self._compat = compat
        self._gtin_db = gtin_db

    async def read(
        self, driver: WBDALIDriver, short_address: Address, logger: Optional[logging.Logger] = None
    ) -> dict:
        res = {}
        try:
            try:
                v = await query_response(
                    driver,
                    self._compat.QueryVersionNumber(short_address),
                    logger,
                    FramePriority.CONFIGURATION,
                )
                if v.value == 1:
                    bank0 = info.BANK_0_legacy
                else:
                    bank0 = info.BANK_0
            except RuntimeError:
                bank0 = info.BANK_0_legacy
            self._update_info(
                res,
                await driver.run_sequence(
                    read_memory_bank(bank0, short_address, self._compat),
                    FramePriority.CONFIGURATION,
                ),
            )
        except MemoryLocationNotImplemented:
            # Some devices do not implement this general information memory bank
            pass
        try:
            self._update_info(
                res,
                await driver.run_sequence(
                    read_memory_bank(oem.BANK_1, short_address, self._compat),
                    FramePriority.CONFIGURATION,
                ),
            )
        except MemoryLocationNotImplemented:
            # OEM memory bank may be absent on some devices
            pass

        product_info = None
        gtin = res.get("gtin") or res.get("oem_gtin")
        if gtin is not None:
            product_info = self._gtin_db.get_info_by_gtin(gtin)

        if product_info is not None:
            res["brand_name"] = product_info.get("brand_name")
            res["product_name"] = product_info.get("product_name")
        return res

    def _update_info(self, dst: dict, values) -> None:
        for field, param in self.memory_fields_to_json_params.items():
            value = values.get(field)
            if value is not None:
                if isinstance(value, int) and _is_empty_memory_int(value):
                    continue
                dst[param] = value


class DaliDeviceBase:  # pylint: disable=too-many-instance-attributes, too-many-arguments, too-many-public-methods, R0917
    _common_schema = {}

    def __init__(
        self,
        address: DaliDeviceAddress,
        bus_id: str,
        default_name_prefix: str,
        default_mqtt_id_part: str,
        compat: Union[DaliCommandsCompatibilityLayer, Dali2CommandsCompatibilityLayer],
        gtin_db: DaliDatabase,
        mqtt_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> None:
        self.uid = str(uuid.uuid4())
        self.address = address
        self.params: dict = {}
        self.schema: dict = {}
        self.logger = logging.getLogger()
        self.is_initialized = False

        self._bus_id = bus_id
        self._default_name_prefix = default_name_prefix
        self._default_mqtt_id_part = default_mqtt_id_part
        self._mqtt_id: Optional[str] = None
        self._name: Optional[str] = None

        if mqtt_id != self.default_mqtt_id:
            self._mqtt_id = mqtt_id
        if name != self.default_name:
            self._name = name

        self._parameter_handlers: list[SettingsParamBase] = []
        self._group_parameter_handlers: list[SettingsParamBase] = []

        self._controls: dict[str, MqttControlBase] = {}
        self._pollables: list[Pollable] = []
        self._current_round: list[Pollable] = []

        self._compat = compat

        if not self._common_schema:
            schema_path = Path("/usr/share/wb-mqtt-dali/schemas/common_device.schema.json")
            with open(schema_path, "r", encoding="utf-8") as f:
                self._common_schema = json.load(f)

        self._gtin_db = gtin_db

        self._initialize_lock = asyncio.Lock()

    @property
    def mqtt_id(self) -> str:
        return self._mqtt_id or self.default_mqtt_id

    @mqtt_id.setter
    def mqtt_id(self, value: str) -> None:
        if value == self.default_mqtt_id:
            self._mqtt_id = None
        else:
            self._mqtt_id = value

    @property
    def has_custom_mqtt_id(self) -> bool:
        return self._mqtt_id is not None

    @property
    def name(self) -> str:
        return self._name or self.default_name

    @name.setter
    def name(self, value: str) -> None:
        if value == self.default_name:
            self._name = None
        else:
            self._name = value

    @property
    def has_custom_name(self) -> bool:
        return self._name is not None

    @property
    def default_name(self) -> str:
        return f"{self._default_name_prefix} {self.address.short}"

    @property
    def default_mqtt_id(self) -> str:
        return f"{self._bus_id}_{self._default_mqtt_id_part}{self.address.short}"

    @property
    def dali_commands(self) -> Union[DaliCommandsCompatibilityLayer, Dali2CommandsCompatibilityLayer]:
        return self._compat

    async def initialize(self, driver: WBDALIDriver) -> None:
        async with self._initialize_lock:
            if self.is_initialized:
                return
            [parameter_handlers, group_parameter_handlers] = await self._initialize_impl(driver)
            parameter_handlers.insert(0, GeneralMemoryParams(self._compat, self._gtin_db))
            self._parameter_handlers = parameter_handlers
            self._group_parameter_handlers = group_parameter_handlers
            self.rebuild_mqtt_controls()
            self.is_initialized = True

    async def load_info(self, driver: WBDALIDriver, force_reload: bool = False) -> None:
        if self.params and not force_reload:
            return

        await self.initialize(driver)

        params = {
            "short_address": self.address.short,
            "random_address": hex(self.address.random),
            "name": self.name,
            "mqtt_id": self.mqtt_id,
        }
        schema = deepcopy(self._common_schema)
        awaitables = [
            param_handler.read(
                driver,
                self._compat.getAddress(self.address.short),
                self.logger,
            )
            for param_handler in self._parameter_handlers
        ]
        results = await asyncio.gather(*awaitables, return_exceptions=True)
        for param, result in zip(self._parameter_handlers, results):
            if isinstance(result, BaseException):
                raise RuntimeError(f'Error reading "{param.name.en}": {result}') from result
            params.update(result)
        schemas = [param_handler.get_schema(False) for param_handler in self._parameter_handlers]
        for type_schema in schemas:
            if type_schema is not None:
                merge_json_schemas(schema, type_schema)

        self.params = params
        self.schema = schema

    async def apply_parameters(self, driver: WBDALIDriver, new_values: dict) -> ApplyResult:
        if not self.is_initialized:
            raise RuntimeError(
                f"Device {self.name} is not initialized. Call initialize() before applying parameters."
            )

        if not self.params:
            raise RuntimeError(
                f"Device {self.name} info is not loaded. Call load_info() before applying parameters."
            )

        jsonschema.validate(
            instance=new_values,
            schema=self.schema,
            format_checker=jsonschema.draft4_format_checker,
        )
        updated_parameters = {}
        needs_refresh = False
        for param_handler in self._parameter_handlers:
            try:
                result = await param_handler.write(
                    driver,
                    self._compat.getAddress(self.address.short),
                    new_values,
                    self.logger,
                )
                updated_parameters.update(result)
                if result and param_handler.requires_mqtt_controls_refresh:
                    needs_refresh = True
            except Exception as e:
                raise RuntimeError(f'Error writing "{param_handler.name.en}": {e}') from e
        self.params.update(updated_parameters)
        if needs_refresh:
            self.rebuild_mqtt_controls()
        await self._apply_common_parameters(driver, new_values)
        return ApplyResult(needs_mqtt_controls_refresh=needs_refresh)

    def rebuild_mqtt_controls(self) -> None:
        self.reset_polling_state()
        mqtt_controls = self._build_mqtt_controls()
        self._controls.clear()
        for control in mqtt_controls:
            self._controls[control.control_info.id] = control
        self._pollables = self._build_pollables()

    def get_mqtt_controls(self) -> list[ControlInfo]:
        if not self.is_initialized:
            raise RuntimeError(
                f"Device {self.name} is not initialized. Call initialize() before getting MQTT controls."
            )
        return [descriptor.control_info for descriptor in self._controls.values()]

    def get_mqtt_control(self, control_id: str) -> Optional[MqttControlBase]:
        return self._controls.get(control_id)

    def get_group_state_controls(self) -> list[MqttControlBase]:
        if not self.is_initialized:
            return []
        return [c for c in self._controls.values() if c.is_group_state_control]

    async def execute_control(self, driver: WBDALIDriver, control_id: str, value: str) -> None:
        if not self.is_initialized:
            raise RuntimeError(
                f"Device {self.name} is not initialized. Call initialize() before executing control."
            )

        control = self._controls.get(control_id)
        if control is not None and control.is_writable():
            await send_commands_with_retry(
                driver,
                control.get_setup_commands(self._compat.getAddress(self.address.short), value),
                self.logger,
            )

    def poll_controls(
        self,
        driver: WBDALIDriver,
        now: float,
        max_commands: int,
        default_max_commands: int,
        default_poll_interval: float,
    ) -> ControlsPollRequestResult:
        if not self.is_initialized:
            raise RuntimeError(
                f"Device {self.name} is not initialized. Call initialize() before polling controls."
            )

        coroutines: list[ControlPollCoroutine] = []
        consumed_commands = 0
        address = self._compat.getAddress(self.address.short)
        if not self._current_round:
            self._refresh_round_snapshot(now, default_poll_interval)
        while self._current_round:
            head = self._current_round[0]
            remaining_budget = max_commands - consumed_commands
            step = head.next_poll_step(
                driver, address, remaining_budget, default_max_commands, now, self.logger
            )
            if step.poll_coroutine is not None:
                coroutines.append(step.poll_coroutine)
                consumed_commands += step.commands_count or 0
            if step.has_more:
                break
            self._current_round.pop(0)

        if not coroutines:
            return ControlsPollRequestResult(has_more=bool(self._current_round))

        async def _run_batch() -> list[ControlPollResult]:
            batches = await asyncio.gather(*[c() for c in coroutines])
            results: list[ControlPollResult] = []
            for batch in batches:
                results.extend(batch)
            return results

        return ControlsPollRequestResult(
            has_more=bool(self._current_round),
            poll_coroutine=_run_batch,
            commands_count=consumed_commands,
        )

    def reset_polling_state(self) -> None:
        self._current_round.clear()
        for pollable in self._pollables:
            pollable.cancel_pending_poll()

    def time_until_next_poll(self, now: float, default_poll_interval: float) -> float:
        pollables = self._current_round or self._pollables
        res = default_poll_interval
        for pollable in pollables:
            if pollable.last_poll_time is None:
                return 0
            poll_interval = (
                pollable.poll_interval if pollable.poll_interval is not None else default_poll_interval
            )
            time_until_poll = poll_interval - (now - pollable.last_poll_time)
            res = min(time_until_poll, res)
        return res

    async def sync_controls_after_broadcast(self, driver: WBDALIDriver, new_params: dict) -> bool:
        controls_updated = False
        any_changed = False
        address = self._compat.getAddress(self.address.short)
        for handler in self._parameter_handlers:
            if not handler.has_changes(new_params):
                continue
            any_changed = True
            if not handler.requires_mqtt_controls_refresh:
                continue
            try:
                await handler.read(driver, address, self.logger)
                controls_updated = True
            except Exception as exc:  # pylint: disable=broad-exception-caught
                self.logger.warning('Failed to sync "%s": %s', handler.name.en, exc)
        if controls_updated:
            self.rebuild_mqtt_controls()
        if any_changed:
            self.params = {}
            self.schema = {}
        return controls_updated

    def set_logger(self, logger: logging.Logger) -> None:
        self.logger = logger

    def get_group_parameter_handlers(self) -> list[SettingsParamBase]:
        return self._group_parameter_handlers

    # --- Hooks for subclasses ---

    def get_common_mqtt_controls(self) -> list[MqttControlBase]:
        """Controls exposed to MQTT when full initialization fails.

        Overridden by subclasses; the controls are replaced with the
        type-specific set as soon as the device initializes successfully.
        """
        return []

    def _build_mqtt_controls(self) -> list[MqttControlBase]:
        return []

    def _build_pollables(self) -> list[Pollable]:
        return [c for c in self._controls.values() if c.is_readable()]

    async def _initialize_impl(
        self, driver: WBDALIDriver
    ) -> tuple[list[SettingsParamBase], list[SettingsParamBase]]:
        raise NotImplementedError()

    # --- Private ---

    def _refresh_round_snapshot(self, now: float, default_poll_interval: float) -> None:
        for pollable in self._pollables:
            if pollable.is_poll_due(now, default_poll_interval):
                self._current_round.append(pollable)

    async def _apply_common_parameters(self, driver: WBDALIDriver, new_values: dict) -> None:
        self.name = new_values.get("name", self.name)
        self.mqtt_id = new_values.get("mqtt_id", self.mqtt_id)

        new_short_address = new_values.get("short_address", self.address.short)
        if new_short_address != self.address.short:
            await send_commands_with_retry(
                driver,
                self._compat.setShortAddressCommands(self.address.short, new_short_address),
                self.logger,
                priority=FramePriority.CONFIGURATION,
            )
            self.address.short = new_short_address

        self.params["short_address"] = self.address.short
        self.params["name"] = self.name
        self.params["mqtt_id"] = self.mqtt_id
