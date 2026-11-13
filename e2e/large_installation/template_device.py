# Generic Modbus slave whose register map is built from a wb-mqtt-serial device template:
# every object in the template carrying `address` is a register, holding unless `reg_type` says otherwise.

from __future__ import annotations

import enum
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from e2e.large_installation.fast_modbus import (
    DeviceEvents,
    EventKey,
    EventType,
    ModbusFunction,
    register_event_payload,
)

INSTALLED_TEMPLATES_DIR = Path("/usr/share/wb-mqtt-serial/templates")
DEFAULT_FW_VERSION = "9.99.0"
CONTINUOUS_READ_HOLE_VALUE = 0xFFFE
MODBUS_COIL_ON = 0xFF00
WRITE_FUNCTIONS = (
    ModbusFunction.WRITE_SINGLE_COIL,
    ModbusFunction.WRITE_SINGLE_REGISTER,
    ModbusFunction.WRITE_MULTIPLE_COILS,
    ModbusFunction.WRITE_MULTIPLE_REGISTERS,
)


class RegisterKind(enum.Enum):
    COIL = EventType.COIL
    DISCRETE = EventType.DISCRETE
    HOLDING = EventType.HOLDING
    INPUT = EventType.INPUT


READ_FUNCTION_KINDS = {
    ModbusFunction.READ_COILS: RegisterKind.COIL,
    ModbusFunction.READ_DISCRETE_INPUTS: RegisterKind.DISCRETE,
    ModbusFunction.READ_HOLDING_REGISTERS: RegisterKind.HOLDING,
    ModbusFunction.READ_INPUT_REGISTERS: RegisterKind.INPUT,
}


class ModbusException(enum.IntEnum):
    ILLEGAL_FUNCTION = 1
    ILLEGAL_DATA_ADDRESS = 2
    ILLEGAL_DATA_VALUE = 3


class ModbusError(Exception):
    def __init__(self, code: ModbusException) -> None:
        super().__init__(code.name)
        self.code = code


class WbRegister(enum.IntEnum):
    BAUD_RATE = 110
    PARITY = 111
    STOP_BITS = 112
    CONTINUOUS_READ = 114
    SLAVE_ID = 128
    DEVICE_MODEL = 200
    FW_VERSION = 250
    SERIAL_NUMBER = 270
    FW_SIGNATURE = 290
    LOCAL_TIME = 452


WB_STRING_REGISTERS = {WbRegister.DEVICE_MODEL: 20, WbRegister.FW_VERSION: 16, WbRegister.FW_SIGNATURE: 12}
WB_CONTINUOUS_READ_ENABLED = (1, 2)

REG_TYPE_KINDS = {
    "coil": RegisterKind.COIL,
    "discrete": RegisterKind.DISCRETE,
    "holding": RegisterKind.HOLDING,
    "holding_single": RegisterKind.HOLDING,
    "holding_multi": RegisterKind.HOLDING,
    "input": RegisterKind.INPUT,
    "press_counter": RegisterKind.INPUT,
}

FORMAT_WIDTHS = {
    "u24": 2,
    "s24": 2,
    "bcd24": 2,
    "u32": 2,
    "s32": 2,
    "bcd32": 2,
    "float": 2,
    "u64": 4,
    "s64": 4,
    "double": 4,
}


@dataclass(frozen=True)
class DeviceTemplate:
    signature: str
    registers: dict[RegisterKind, frozenset[int]]


def load_installed_template(
    device_type: str, templates_dir: Path = INSTALLED_TEMPLATES_DIR
) -> DeviceTemplate:
    """The template wb-mqtt-serial uses for `device_type`: of several, the last one by sorted path."""
    found: Optional[dict] = None
    for data in _installed_templates(templates_dir):
        if data.get("device_type") == device_type:
            found = data
    if found is None:
        raise FileNotFoundError(
            f"No wb-mqtt-serial template with device_type {device_type!r} in {templates_dir}"
        )
    return template_from_dict(found)


def _installed_templates(templates_dir: Path) -> Iterator[dict]:
    """Parsed template files in sorted path order; unreadable ones are skipped."""
    for path in sorted(str(p) for p in templates_dir.glob("*.json")):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            yield data


def template_from_dict(data: dict) -> DeviceTemplate:
    registers: dict[RegisterKind, set[int]] = {kind: set() for kind in RegisterKind}
    for item in _walk_objects(data.get("device", {})):
        if "address" not in item:
            continue
        # wb-mqtt-serial defaults a register without `reg_type` (setup parameters mostly) to holding.
        kind = REG_TYPE_KINDS.get(str(item.get("reg_type", "holding")))
        if kind is None:
            continue
        start = _parse_address(item["address"])
        registers[kind].update(range(start, start + _register_width(item)))
    hw = data.get("hw") or [{}]
    signature = str(hw[0].get("signature", data.get("device_type", "")))
    return DeviceTemplate(
        signature=signature,
        registers={kind: frozenset(addresses) for kind, addresses in registers.items()},
    )


class TemplateDevice:
    """Answers reads/writes of the template's registers plus the WB service registers."""

    def __init__(
        self,
        slave_id: int,
        template: DeviceTemplate,
        response_delay_s: float,
        fw_version: str = DEFAULT_FW_VERSION,
    ) -> None:
        self.slave_id = slave_id
        self.template = template
        self.response_delay_s = response_delay_s
        self._values: dict[RegisterKind, dict[int, int]] = {
            kind: dict.fromkeys(addresses, 0) for kind, addresses in template.registers.items()
        }
        self._fw_version = fw_version
        self._init_wb_registers()
        self.events = DeviceEvents(self._can_enable_event, self._event_data)

    def handle_pdu(self, pdu: bytes) -> Optional[bytes]:
        function = pdu[0]
        try:
            if function == ModbusFunction.FAST_MODBUS:
                return self.events.handle_enable(pdu)
            if function in READ_FUNCTION_KINDS:
                return self._read(ModbusFunction(function), pdu)
            if function in WRITE_FUNCTIONS:
                return self._write(ModbusFunction(function), pdu)
            raise ModbusError(ModbusException.ILLEGAL_FUNCTION)
        except ModbusError as exc:
            return bytes([function | 0x80, exc.code])
        except (ValueError, IndexError):
            return bytes([function | 0x80, ModbusException.ILLEGAL_DATA_VALUE])

    def response_delay_for(self, function: int) -> float:  # pylint: disable=unused-argument
        return self.response_delay_s

    def get(self, kind: RegisterKind, address: int) -> int:
        return self._values[kind][address]

    def set(self, kind: RegisterKind, address: int, value: int) -> None:
        """Change a register from the device side; an enabled event fires on a change."""
        registers = self._values[kind]
        if registers.get(address) == value:
            return
        registers[address] = value
        self.events.notify_change(EventKey(kind.value, address))

    def set_silently(self, kind: RegisterKind, address: int, value: int) -> None:
        self._values[kind][address] = value

    def add_registers(self, kind: RegisterKind, addresses: range) -> None:
        for address in addresses:
            self._values[kind].setdefault(address, 0)

    # --- Hooks for subclasses ---

    def _on_write(self, kind: RegisterKind, values: list[tuple[int, int]]) -> None:
        """Called once per Modbus write after all its registers are stored."""

    # --- Private ---

    def _init_wb_registers(self) -> None:
        holding = self._values[RegisterKind.HOLDING]
        for register in (
            WbRegister.BAUD_RATE,
            WbRegister.PARITY,
            WbRegister.STOP_BITS,
            WbRegister.CONTINUOUS_READ,
            WbRegister.SLAVE_ID,
        ):
            holding.setdefault(register, 0)
        holding[WbRegister.BAUD_RATE] = 1152
        holding[WbRegister.SLAVE_ID] = self.slave_id
        texts = {
            WbRegister.DEVICE_MODEL: self.template.signature,
            WbRegister.FW_VERSION: self._fw_version,
            WbRegister.FW_SIGNATURE: self.template.signature,
        }
        for register, size in WB_STRING_REGISTERS.items():
            encoded = texts[register].encode("ascii", errors="replace")[:size].ljust(size, b"\0")
            for offset, char in enumerate(encoded):
                holding[register + offset] = char
        serial = 0x10000 + self.slave_id
        for kind in (RegisterKind.HOLDING, RegisterKind.INPUT):
            self._values[kind][WbRegister.SERIAL_NUMBER] = serial >> 16
            self._values[kind][WbRegister.SERIAL_NUMBER + 1] = serial & 0xFFFF
        for offset in range(4):
            holding.setdefault(WbRegister.LOCAL_TIME + offset, 0)

    def _continuous_read(self) -> bool:
        return self._values[RegisterKind.HOLDING][WbRegister.CONTINUOUS_READ] in WB_CONTINUOUS_READ_ENABLED

    def _read(self, function: ModbusFunction, pdu: bytes) -> bytes:
        kind = READ_FUNCTION_KINDS[function]
        address = int.from_bytes(pdu[1:3], "big")
        count = int.from_bytes(pdu[3:5], "big")
        registers = self._values[kind]
        addresses = range(address, address + count)
        known = [a in registers for a in addresses]
        if count == 0 or not any(known) or (not all(known) and not self._continuous_read()):
            raise ModbusError(ModbusException.ILLEGAL_DATA_ADDRESS)
        if kind in (RegisterKind.COIL, RegisterKind.DISCRETE):
            packed = bytearray((count + 7) // 8)
            for index, a in enumerate(addresses):
                if registers.get(a, 0):
                    packed[index // 8] |= 1 << (index % 8)
            return bytes([function, len(packed)]) + bytes(packed)
        words = b"".join(
            (registers[a] if a in registers else CONTINUOUS_READ_HOLE_VALUE).to_bytes(2, "big")
            for a in addresses
        )
        return bytes([function, len(words)]) + words

    def _write(self, function: ModbusFunction, pdu: bytes) -> bytes:
        address = int.from_bytes(pdu[1:3], "big")
        if function == ModbusFunction.WRITE_SINGLE_COIL:
            self._store(
                RegisterKind.COIL, [(address, 1 if int.from_bytes(pdu[3:5], "big") == MODBUS_COIL_ON else 0)]
            )
            return pdu[:5]
        if function == ModbusFunction.WRITE_SINGLE_REGISTER:
            self._store(RegisterKind.HOLDING, [(address, int.from_bytes(pdu[3:5], "big"))])
            return pdu[:5]
        count = int.from_bytes(pdu[3:5], "big")
        data = pdu[6 : 6 + pdu[5]]
        if function == ModbusFunction.WRITE_MULTIPLE_COILS:
            values = [(address + i, (data[i // 8] >> (i % 8)) & 1) for i in range(count)]
            self._store(RegisterKind.COIL, values)
        else:
            if len(data) != count * 2:
                raise ModbusError(ModbusException.ILLEGAL_DATA_VALUE)
            values = [(address + i, int.from_bytes(data[2 * i : 2 * i + 2], "big")) for i in range(count)]
            self._store(RegisterKind.HOLDING, values)
        return pdu[:5]

    def _store(self, kind: RegisterKind, values: list[tuple[int, int]]) -> None:
        registers = self._values[kind]
        if any(address not in registers for address, _ in values):
            raise ModbusError(ModbusException.ILLEGAL_DATA_ADDRESS)
        for address, value in values:
            registers[address] = value
        self._on_write(kind, values)

    def _can_enable_event(self, key: EventKey) -> bool:
        return key.address in self._values[RegisterKind(key.type)]

    def _event_data(self, key: EventKey) -> bytes:
        return register_event_payload(key.type, self._values[RegisterKind(key.type)][key.address])


def _walk_objects(node: Any) -> Iterator[dict]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_objects(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_objects(value)


def _parse_address(raw: Any) -> int:
    if isinstance(raw, int):
        return raw
    return int(str(raw).split(":", 1)[0], 0)


def _register_width(item: dict) -> int:
    fmt = str(item.get("format", "u16"))
    if fmt == "string":
        return int(item.get("string_data_size", 16))
    if fmt == "string8":
        return math.ceil(int(item.get("string_data_size", 16)) / 2)
    return FORMAT_WIDTHS.get(fmt, 1)
