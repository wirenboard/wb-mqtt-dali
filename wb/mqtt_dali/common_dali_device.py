import asyncio
import json
import logging
import uuid
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Union

import jsonschema
from dali.address import Address
from dali.command import Command, Response
from dali.exceptions import MemoryLocationNotImplemented, ResponseError
from dali.memory import info, location, oem

from .dali2_compat import Dali2CommandsCompatibilityLayer
from .dali_compat import DaliCommandsCompatibilityLayer
from .device_publisher import ControlInfo
from .gtin_db import DaliDatabase
from .settings import SettingsParamBase, SettingsParamName
from .utils import merge_json_schemas
from .wbdali_utils import (
    WBDALIDriver,
    check_query_response,
    is_transmission_error_response,
    query_response,
    send_commands_with_retry,
)


def request_with_retry_sequence(cmd):
    last_error = "unknown error"
    for _ in range(3):
        result = yield cmd
        if not is_transmission_error_response(result):
            return result
        last_error = str(result)
    raise RuntimeError(f"No response to {cmd} after 3 attempts; last error: {last_error}")


class MqttControlBase:
    def __init__(self, control_info: ControlInfo) -> None:
        # the property value is used as default value for the control
        self.control_info = control_info

    def is_readable(self) -> bool:
        return False

    def is_writable(self) -> bool:
        return False

    def get_query(self, short_address: Address) -> Optional[Command]:
        return None

    def format_response(self, response: Response) -> str:
        return ""

    def get_setup_commands(self, short_address: Address, value_to_set: str) -> list[Command]:
        return []


class MqttControl(MqttControlBase):
    def __init__(
        self,
        control_info: ControlInfo,
        query_builder: Optional[Callable[[Address], object]] = None,
        value_formatter: Optional[Callable[[Response], str]] = None,
        commands_builder: Optional[Callable[[Address, str], list[Command]]] = None,
    ) -> None:
        super().__init__(control_info)
        self.query_builder = query_builder
        self.value_formatter = value_formatter
        self.commands_builder = commands_builder

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
    title: Optional[str] = None


@dataclass
class DaliDeviceAddress:
    short: int
    random: int


def read_memory_bank(
    bank: info.MemoryBank,
    short_address: Address,
    compat: Union[DaliCommandsCompatibilityLayer, Dali2CommandsCompatibilityLayer],
):
    last_address = yield from bank.LastAddress.read(short_address)
    if isinstance(last_address, location.FlagValue):
        raise ResponseError(
            f"Cannot read memory bank {bank.address}: last address location is {last_address.value}"
        )
    # Reading the last address also sets DTR1 appropriately

    # Bank 0 has a useful value at address 0x02; all other banks
    # use this for the lock/latch byte
    start_address = 0x02 if bank.address == 0 else 0x03
    commands_count = last_address - start_address + 1
    raw_data = [None] * start_address + [None] * commands_count

    first_unread_index = 0
    last_error = "unknown error"
    for _ in range(3):
        current_address = start_address + first_unread_index
        yield from request_with_retry_sequence(compat.DTR0(current_address))

        remaining_count = commands_count - first_unread_index
        responses = yield [compat.ReadMemoryLocation(short_address) for _ in range(remaining_count)]

        next_unread_index: Optional[int] = None
        for offset, response in enumerate(responses):
            i = first_unread_index + offset
            try:
                check_query_response(response)
            except RuntimeError as e:
                next_unread_index = i
                last_error = str(e)
                break
            raw_data[start_address + i] = response.raw_value.as_integer

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
                )
                if v.value == 1:
                    bank0 = info.BANK_0_legacy
                else:
                    bank0 = info.BANK_0
            except RuntimeError:
                bank0 = info.BANK_0_legacy
            self._update_info(
                res, await driver.run_sequence(read_memory_bank(bank0, short_address, self._compat))
            )
        except MemoryLocationNotImplemented:
            # Some devices do not implement this general information memory bank
            pass
        try:
            self._update_info(
                res, await driver.run_sequence(read_memory_bank(oem.BANK_1, short_address, self._compat))
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
                if isinstance(value, int):
                    value_to_check = value
                    is_empty = True
                    while value_to_check > 0:
                        if value_to_check & 0xFF != 0xFF:
                            is_empty = False
                            break
                        value_to_check >>= 8
                    if is_empty:
                        continue
                dst[param] = value


class DaliDeviceBase:
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
        self._polling_controls: list[MqttControlBase] = []

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
        return f"{self._default_name_prefix} {self.address.short}:{self.address.random:#x}"

    @property
    def default_mqtt_id(self) -> str:
        return f"{self._bus_id}_{self._default_mqtt_id_part}{self.address.short}"

    async def initialize(self, driver: WBDALIDriver) -> None:
        async with self._initialize_lock:
            if self.is_initialized:
                return
            [parameter_handlers, mqtt_controls, group_parameter_handlers] = await self._initialize_impl(
                driver
            )
            parameter_handlers.insert(0, GeneralMemoryParams(self._compat, self._gtin_db))
            self._parameter_handlers = parameter_handlers
            self._group_parameter_handlers = group_parameter_handlers
            self._controls.clear()
            self._polling_controls.clear()
            for control in mqtt_controls:
                if control.is_readable():
                    self._polling_controls.append(control)
                self._controls[control.control_info.id] = control
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

    async def apply_parameters(self, driver: WBDALIDriver, new_values: dict) -> None:
        if not self.is_initialized:
            raise RuntimeError(
                f"Device {self.name} is not initialized. Call initialize() before applying parameters."
            )

        if not self.params:
            raise RuntimeError(
                f"Device {self.name} info is not loaded. Call load_info() before applying parameters."
            )

        jsonschema.validate(
            instance=new_values, schema=self.schema, format_checker=jsonschema.draft4_format_checker
        )
        updated_parameters = {}
        for param_handler in self._parameter_handlers:
            try:
                updated_parameters.update(
                    await param_handler.write(
                        driver,
                        self._compat.getAddress(self.address.short),
                        new_values,
                        self.logger,
                    )
                )
            except Exception as e:
                raise RuntimeError(f'Error writing "{param_handler.name.en}": {e}') from e
        self.params.update(updated_parameters)
        await self._apply_common_parameters(driver, new_values)

    def get_mqtt_controls(self) -> list[ControlInfo]:
        if not self.is_initialized:
            raise RuntimeError(
                f"Device {self.name} is not initialized. Call initialize() before getting MQTT controls."
            )
        return [descriptor.control_info for descriptor in self._controls.values()]

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

    async def poll_controls(self, driver: WBDALIDriver) -> list[ControlPollResult]:
        if not self.is_initialized:
            raise RuntimeError(
                f"Device {self.name} is not initialized. Call initialize() before polling controls."
            )

        queries = []
        for descriptor in self._polling_controls:
            queries.append(descriptor.get_query(self._compat.getAddress(self.address.short)))
        if not queries:
            return []
        responses = await send_commands_with_retry(driver, queries, self.logger)

        res = []
        for descriptor, response in zip(self._polling_controls, responses):
            if response is None or response.raw_value is None or response.raw_value.error:
                res.append(ControlPollResult(control_id=descriptor.control_info.id, value="", error="r"))
                continue

            if descriptor.control_info.meta.control_type == "alarm":
                alarm_title = descriptor.format_response(response)
                alarm_active = "1" if getattr(response, "error", False) else "0"
                res.append(
                    ControlPollResult(
                        control_id=descriptor.control_info.id,
                        value=alarm_active,
                        title=alarm_title,
                    )
                )
                continue

            res.append(
                ControlPollResult(
                    control_id=descriptor.control_info.id,
                    value=descriptor.format_response(response),
                )
            )
        return res

    def set_logger(self, logger: logging.Logger) -> None:
        self.logger = logger

    def get_group_parameter_handlers(self) -> list[SettingsParamBase]:
        return self._group_parameter_handlers

    def get_common_mqtt_controls(self) -> list[MqttControlBase]:
        return []

    async def _apply_common_parameters(self, driver: WBDALIDriver, new_values: dict) -> None:
        self.name = new_values.get("name", self.name)
        self.mqtt_id = new_values.get("mqtt_id", self.mqtt_id)

        new_short_address = new_values.get("short_address", self.address.short)
        if new_short_address != self.address.short:
            await send_commands_with_retry(
                driver,
                self._compat.setShortAddressCommands(self.address.short, new_short_address),
                self.logger,
            )
            self.address.short = new_short_address

        self.params["short_address"] = self.address.short
        self.params["name"] = self.name
        self.params["mqtt_id"] = self.mqtt_id

    # Must be implemented by subclasses
    async def _initialize_impl(
        self, driver: WBDALIDriver
    ) -> tuple[list[SettingsParamBase], list[MqttControlBase], list[SettingsParamBase]]:
        raise NotImplementedError()
