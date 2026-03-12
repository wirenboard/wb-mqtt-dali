import asyncio
import json
import logging
import uuid
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Union

import jsonschema
from dali.command import Command, Response
from dali.exceptions import MemoryLocationNotImplemented, ResponseError
from dali.memory import info, location, oem

from .dali2_compat import Dali2CommandsCompatibilityLayer
from .dali_compat import DaliCommandsCompatibilityLayer
from .device_publisher import ControlInfo
from .gtin_db import DaliDatabase
from .settings import SettingsParamBase, SettingsParamName
from .utils import merge_json_schemas
from .wbdali_utils import WBDALIDriver


class MqttControlBase:
    def __init__(self, control_info: ControlInfo) -> None:
        # the property value is used as default value for the control
        self.control_info = control_info

    def is_readable(self) -> bool:
        return False

    def is_writable(self) -> bool:
        return False

    def get_query(self, short_address: int) -> Optional[Command]:
        return None

    def format_response(self, response: Response) -> str:
        return ""

    def get_setup_commands(self, short_address: int, value_to_set: str) -> list[Command]:
        return []


class MqttControl(MqttControlBase):
    def __init__(
        self,
        control_info: ControlInfo,
        query_builder: Optional[Callable[[int], object]] = None,
        value_formatter: Optional[Callable[[Response], str]] = None,
        commands_builder: Optional[Callable[[int, str], list[Command]]] = None,
    ) -> None:
        super().__init__(control_info)
        self.query_builder = query_builder
        self.value_formatter = value_formatter
        self.commands_builder = commands_builder

    def get_query(self, short_address: int) -> Optional[Command]:
        if self.query_builder is not None:
            return self.query_builder(short_address)
        return None

    def format_response(self, response: Response) -> str:
        if self.value_formatter is not None:
            return self.value_formatter(response)
        return ""

    def get_setup_commands(self, short_address: int, value_to_set: str) -> list[Command]:
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
    short_address: int,
    compat: Union[DaliCommandsCompatibilityLayer, Dali2CommandsCompatibilityLayer],
):
    last_address = yield from bank.LastAddress.read(compat.getAddress(short_address))
    if isinstance(last_address, location.FlagValue):
        raise ResponseError(
            f"Cannot read memory bank {bank.address}: last address location is {last_address.value}"
        )
    # Reading the last address also sets DTR1 appropriately

    # Bank 0 has a useful value at address 0x02; all other banks
    # use this for the lock/latch byte
    start_address = 0x02 if bank.address == 0 else 0x03
    yield compat.DTR0(start_address)
    raw_data = [None] * start_address
    commands_count = last_address - start_address + 1
    r = yield [compat.ReadMemoryLocation(short_address) for _ in range(commands_count)]
    for i in range(commands_count):
        if r[i].raw_value is not None:
            if r[i].raw_value.error:
                raise ResponseError(
                    f"Framing error while reading memory bank "
                    f"{short_address} location {i + start_address}"
                )
            raw_data.append(r[i].raw_value.as_integer)
        else:
            raw_data.append(None)
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
        super().__init__(SettingsParamName("General memory parameters"))
        self._compat = compat
        self._gtin_db = gtin_db

    async def read(self, driver: WBDALIDriver, short_address: int) -> dict:
        res = {}
        try:
            v = await driver.send(self._compat.QueryVersionNumber(short_address))
            if v is None or v.raw_value is None or v.value == 1:
                bank0 = info.BANK_0_legacy
            else:
                bank0 = info.BANK_0
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

        self._controls_lock = asyncio.Lock()
        self._controls: dict[str, MqttControlBase] = {}
        self._polling_controls: list[MqttControlBase] = []

        self._compat = compat

        if not self._common_schema:
            schema_path = Path("/usr/share/wb-mqtt-dali/schemas/common_device.schema.json")
            with open(schema_path, "r", encoding="utf-8") as f:
                self._common_schema = json.load(f)

        self._gtin_db = gtin_db

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

    async def load_info(self, driver: WBDALIDriver, force_reload: bool = False) -> None:
        if self.params and not force_reload:
            return
        parameter_handlers: list[SettingsParamBase] = [GeneralMemoryParams(self._compat, self._gtin_db)]
        parameter_handlers.extend(await self._get_parameter_handlers(driver))
        params = {
            "short_address": self.address.short,
            "random_address": hex(self.address.random),
            "name": self.name,
            "mqtt_id": self.mqtt_id,
        }
        schema = deepcopy(self._common_schema)
        awaitables = [param_handler.read(driver, self.address.short) for param_handler in parameter_handlers]
        results_iterable = iter(await asyncio.gather(*awaitables))
        for _ in parameter_handlers:
            type_params = next(results_iterable)
            params.update(type_params)
        schemas = [param_handler.get_schema() for param_handler in parameter_handlers]
        for type_schema in schemas:
            if type_schema is not None:
                merge_json_schemas(schema, type_schema)
        self._parameter_handlers = parameter_handlers
        self.params = params
        self.schema = schema

    async def apply_parameters(self, driver: WBDALIDriver, new_values: dict) -> None:
        if not self.params:
            await self.load_info(driver)
        jsonschema.validate(
            instance=new_values, schema=self.schema, format_checker=jsonschema.draft4_format_checker
        )
        updated_parameters = {}
        for param_handler in self._parameter_handlers:
            updated_parameters.update(await param_handler.write(driver, self.address.short, new_values))
        self.params.update(updated_parameters)
        await self._apply_common_parameters(driver, new_values)

    async def get_mqtt_controls(self, driver: WBDALIDriver) -> list[ControlInfo]:
        await self._update_mqtt_controls_list(driver)
        return [descriptor.control_info for descriptor in self._controls.values()]

    async def execute_control(self, driver: WBDALIDriver, control_id: str, value: str) -> None:
        control = self._controls.get(control_id)
        if control is not None and control.is_writable():
            await driver.send_commands(control.get_setup_commands(self.address.short, value))
            return

    async def poll_controls(self, driver: WBDALIDriver) -> list[ControlPollResult]:
        await self._update_mqtt_controls_list(driver)
        queries = []
        for descriptor in self._polling_controls:
            queries.append(descriptor.get_query(self.address.short))
        if not queries:
            return []
        responses = await driver.send_commands(queries)

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

    def setLogger(self, logger: logging.Logger) -> None:
        self.logger = logger

    async def _apply_common_parameters(self, driver: WBDALIDriver, new_values: dict) -> None:
        self.name = new_values.get("name", self.name)
        self.mqtt_id = new_values.get("mqtt_id", self.mqtt_id)

        new_short_address = new_values.get("short_address", self.address.short)
        if new_short_address != self.address.short:
            await driver.send_commands(
                self._compat.setShortAddressCommands(self.address.short, new_short_address)
            )
            self.address.short = new_short_address

        self.params["short_address"] = self.address.short
        self.params["name"] = self.name
        self.params["mqtt_id"] = self.mqtt_id

    async def _update_mqtt_controls_list(self, driver: WBDALIDriver) -> None:
        async with self._controls_lock:
            if not self._controls:
                controls = await self._get_mqtt_controls(driver)
                self._polling_controls = []
                self._controls = {}
                for control in controls:
                    if control.is_readable():
                        self._polling_controls.append(control)
                    self._controls[control.control_info.id] = control

    # Must be implemented by subclasses
    async def _get_parameter_handlers(self, driver: WBDALIDriver) -> list[SettingsParamBase]:
        raise NotImplementedError()

    # Can be implemented by subclasses to provide controls for MQTT topics
    async def _get_mqtt_controls(self, driver: WBDALIDriver) -> list[MqttControlBase]:
        return []
