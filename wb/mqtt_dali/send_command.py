import inspect
import re
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Dict, Optional, Type

import dali.gear.colour as gear_colour
import dali.gear.converter as gear_converter
import dali.gear.emergency as gear_emergency
import dali.gear.general as gear_general
import dali.gear.incandescent as gear_incandescent
import dali.gear.led as gear_led
from dali.address import (
    DeviceAddress,
    DeviceBroadcast,
    DeviceGroup,
    DeviceShort,
    GearAddress,
    GearBroadcast,
    GearGroup,
    GearShort,
    InstanceNumber,
)
from dali.command import Command, Response
from dali.device import general as device_general
from dali.device import light as device_light
from dali.device import occupancy as device_occupancy
from dali.device import pushbutton as device_pushbutton

from .device import absolute_input_device, feedback, general_purpose_sensor
from .gear import (
    demand_response,
    dimming_curve,
    integrated_power_supply,
    switching_function,
    thermal_gear_protection,
    thermal_lamp_protection,
)


@dataclass
class CommandInfo:
    cls: Type[Command]
    kind: str
    device_type: int
    needs_data: bool


def _is_concrete_command(obj: Any) -> bool:
    if not inspect.isclass(obj):
        return False
    if obj.__name__.startswith("_"):
        return False
    if obj.__name__.endswith("Response") or obj.__name__.endswith("Mixin"):
        return False
    # Gear commands use _cmdval, device commands use _opcode
    has_cmdval = getattr(obj, "_cmdval", None) is not None
    has_opcode = getattr(obj, "_opcode", None) is not None
    return has_cmdval or has_opcode


def _collect_commands(module: ModuleType, base_class: Type[Command]) -> Dict[str, Type[Command]]:
    result: Dict[str, Type[Command]] = {}
    for name, obj in inspect.getmembers(module):
        if _is_concrete_command(obj) and issubclass(obj, base_class):
            result[name] = obj
    return result


def build_command_registry() -> Dict[str, CommandInfo]:
    registry: Dict[str, CommandInfo] = {}

    # --- Gear standard commands (from dali.gear.general) ---
    gear_standard_base = gear_general._StandardCommand  # pylint: disable=protected-access
    for name, cls in _collect_commands(gear_general, gear_standard_base).items():
        if name == "UnknownGearCommand":
            continue
        registry[name] = CommandInfo(
            cls=cls,
            kind="gear_standard",
            device_type=0,
            needs_data=getattr(cls, "_hasparam", None) is True,
        )

    # DAPC (special case: takes data argument)
    registry["DAPC"] = CommandInfo(
        cls=gear_general.DAPC,
        kind="gear_dapc",
        device_type=0,
        needs_data=True,
    )

    # --- Gear special commands (no address) ---
    gear_specials = {
        "DTR0": gear_general.DTR0,
        "DTR1": gear_general.DTR1,
        "DTR2": gear_general.DTR2,
        "Terminate": gear_general.Terminate,
    }
    for name, cls in gear_specials.items():
        registry[name] = CommandInfo(
            cls=cls,
            kind="gear_special",
            device_type=0,
            needs_data=name.startswith("DTR"),
        )

    # --- DT-specific gear commands ---
    dt_modules = [
        (1, gear_emergency),
        (4, gear_incandescent),
        (5, gear_converter),
        (6, gear_led),
        (7, switching_function),
        (8, gear_colour),
        (16, thermal_gear_protection),
        (17, dimming_curve),
        (20, demand_response),
        (21, thermal_lamp_protection),
        (49, integrated_power_supply),
    ]
    for dt_num, module in dt_modules:
        for name, cls in _collect_commands(module, gear_standard_base).items():
            key = f"DT{dt_num}.{name}"
            registry[key] = CommandInfo(
                cls=cls,
                kind="gear_standard",
                device_type=dt_num,
                needs_data=getattr(cls, "_hasparam", None) is True,
            )

    # --- Device standard commands (FF24 prefix) ---
    device_std_base = device_general._StandardDeviceCommand  # pylint: disable=protected-access
    for name, cls in _collect_commands(device_general, device_std_base).items():
        if name == "UnknownDeviceCommand":
            continue
        key = f"FF24.{name}"
        registry[key] = CommandInfo(
            cls=cls,
            kind="device_standard",
            device_type=0,
            needs_data=getattr(cls, "_hasparam", None) is True,
        )

    # --- Device instance commands from dali.device.general (FF24.Ix prefix) ---
    device_inst_base = device_general._StandardInstanceCommand  # pylint: disable=protected-access
    for name, cls in _collect_commands(device_general, device_inst_base).items():
        key = f"FF24.Ix.{name}"
        registry[key] = CommandInfo(
            cls=cls,
            kind="device_instance",
            device_type=0,
            needs_data=getattr(cls, "_hasparam", None) is True,
        )

    # --- Instance-type-specific device instance commands (DT<instance_type>.Ix prefix) ---
    instance_type_modules = [
        device_pushbutton,
        absolute_input_device,
        device_occupancy,
        device_light,
        general_purpose_sensor,
        feedback,
    ]
    for module in instance_type_modules:
        it = module.instance_type
        for name, cls in _collect_commands(module, device_inst_base).items():
            key = f"FF24.DT{it}.Ix.{name}"
            registry[key] = CommandInfo(
                cls=cls,
                kind="device_instance",
                device_type=0,
                needs_data=getattr(cls, "_hasparam", None) is True,
            )

    # --- Device special commands (FF24 prefix, no address) ---
    device_specials = {
        "FF24.DTR0": device_general.DTR0,
        "FF24.DTR1": device_general.DTR1,
        "FF24.DTR2": device_general.DTR2,
        "FF24.Terminate": device_general.Terminate,
    }
    for key, cls in device_specials.items():
        registry[key] = CommandInfo(
            cls=cls,
            kind="device_special",
            device_type=0,
            needs_data=key.endswith("DTR0") or key.endswith("DTR1") or key.endswith("DTR2"),
        )

    return registry


_INSTANCE_PATTERN = re.compile(r"^(.+)\.I(\d+)\.(.+)$")


def build_gear_address(
    address: Optional[int], group: Optional[int], broadcast: bool
) -> Optional[GearAddress]:
    if broadcast:
        return GearBroadcast()
    if group is not None:
        return GearGroup(group)
    if address is not None:
        return GearShort(address)
    return None


def build_device_address(
    address: Optional[int], group: Optional[int], broadcast: bool
) -> Optional[DeviceAddress]:
    if broadcast:
        return DeviceBroadcast()
    if group is not None:
        return DeviceGroup(group)
    if address is not None:
        return DeviceShort(address)
    return None


def parse_and_build_command(  # pylint: disable=too-many-arguments, too-many-positional-arguments, too-many-return-statements, too-many-branches, too-many-statements
    command_name: str,
    registry: Dict[str, CommandInfo],
    address: Optional[int] = None,
    data: Optional[int] = None,
    group: Optional[int] = None,
    broadcast: bool = False,
) -> Command:
    # Handle <prefix>.I<n>.<name> pattern for instance commands
    # e.g., FF24.I0.EnableInstance, FF24.DT6.I0.SetReportTimer
    instance_match = _INSTANCE_PATTERN.match(command_name)
    instance_number = None
    lookup_key = command_name

    if instance_match:
        prefix = instance_match.group(1)
        instance_number = int(instance_match.group(2))
        remainder = instance_match.group(3)
        lookup_key = f"{prefix}.Ix.{remainder}"

    if lookup_key not in registry:
        available = ", ".join(sorted(registry.keys()))
        raise ValueError(f"Unknown command: {command_name}\nAvailable commands:\n{available}")

    info = registry[lookup_key]

    # Validate arguments
    if address is not None and broadcast:
        raise ValueError(f"Command '{command_name}' cannot use --address together with --broadcast")

    if address is not None and (address < 0 or address > 63):
        raise ValueError(f"--address must be in range 0-63, got {address}")

    if group is not None and address is not None:
        raise ValueError(f"Command '{command_name}' cannot use --group together with --address")

    if group is not None and broadcast:
        raise ValueError(f"Command '{command_name}' cannot use --group together with --broadcast")

    if group is not None:
        if (info.kind == "gear_standard") and (group < 0 or group > 15):
            raise ValueError(f"--group must be in range 0-15, got {group}")
        if (info.kind == "device_standard") and (group < 0 or group > 31):
            raise ValueError(f"--group must be in range 0-31, got {group}")

    if info.kind in ("gear_special", "device_special"):
        if address is not None:
            raise ValueError(f"Command '{command_name}' is a special command and does not take --address")
        if group is not None:
            raise ValueError(f"Command '{command_name}' is a special command and does not take --group")
    elif info.kind == "gear_dapc":
        if data is None:
            raise ValueError("DAPC command requires --data argument")
    elif info.kind in ("gear_standard", "device_standard", "device_instance"):
        if address is None and group is None and not broadcast:
            raise ValueError(f"Command '{command_name}' requires --address, --group or --broadcast")

    if info.needs_data and data is None:
        raise ValueError(f"Command '{command_name}' requires --data argument")

    # Build the command
    if info.kind == "gear_dapc":
        addr = build_gear_address(address, group, broadcast)
        return info.cls(addr, data)

    if info.kind == "gear_standard":
        addr = build_gear_address(address, group, broadcast)
        if info.needs_data:
            return info.cls(addr, data)
        return info.cls(addr)

    if info.kind == "gear_special":
        if info.needs_data:
            return info.cls(data)
        return info.cls()

    if info.kind == "device_standard":
        addr = build_device_address(address, group, broadcast)
        if info.needs_data:
            return info.cls(addr, data)
        return info.cls(addr)

    if info.kind == "device_instance":
        if instance_number is None:
            # Extract prefix (e.g., "FF24.Ix" or "DT2.Ix") to suggest correct syntax
            prefix = lookup_key.rsplit(".Ix.", 1)[0]
            cmd_name = lookup_key.rsplit(".", 1)[-1]
            raise ValueError(
                f"Command '{command_name}' is an instance command. "
                f"Use {prefix}.I<n>.{cmd_name} syntax (e.g., {prefix}.I0.{cmd_name})"
            )
        addr = build_device_address(address, group, broadcast)
        if info.needs_data:
            return info.cls(addr, InstanceNumber(instance_number), data)
        return info.cls(addr, InstanceNumber(instance_number))

    if info.kind == "device_special":
        if info.needs_data:
            return info.cls(data)
        return info.cls()

    raise ValueError(f"Unknown command kind: {info.kind}")


def format_response(response: Optional[Response]) -> str:
    if response is None:
        return "No response"

    if not isinstance(response, Response):
        return str(response)

    raw = response.raw_value
    if raw is None:
        return "No response (timeout)"

    raw_int = raw.as_integer

    # Try to get a meaningful string representation
    parts = [f"Raw: {raw_int} (0x{raw_int:02x})"]

    # Check for common response attributes
    if hasattr(response, "value") and response.value is not None:
        parts.append(f"Value: {response.value}")

    return ", ".join(parts)


def list_commands(registry: Dict[str, CommandInfo]) -> str:
    groups = {}
    for key, info in sorted(registry.items()):

        # Determine group
        if ".Ix." in key:
            # Instance commands: e.g., FF24.Ix.EnableInstance -> "FF24.Ix"
            # or DT2.Ix.SetReportTimer -> "DT2.Ix"
            group = key.split(".Ix.")[0] + ".Ix"
        elif info.kind in ("device_special", "device_standard"):
            group = "FF24 General"
        elif "." in key:
            group = key.split(".")[0]
        else:
            group = "General"

        groups.setdefault(group, []).append((key, info))

    def _sort_key(name):
        if name == "General":
            return (0,)
        # FF24.Ix before FF24.DTx.Ix: replace "Ix" with "DT0.Ix" for sorting
        sort_name = name.replace("FF24.Ix", "FF24.DT0.Ix") if name == "FF24.Ix" else name
        parts = re.split(r"(\d+)", sort_name)
        return (2,) + tuple(int(p) if p.isdigit() else p.lower() for p in parts)

    group_descriptions = {
        "DT1": "Emergency Lighting",
        "DT4": "Incandescent Lamps",
        "DT5": "Converter",
        "DT6": "LED Gear",
        "DT7": "Switching Function",
        "DT8": "Colour Control",
        "DT16": "Thermal Gear Protection",
        "DT17": "Dimming Curve",
        "DT20": "Demand Response",
        "DT21": "Thermal Lamp Protection",
        "DT49": "Integrated Power Supply",
        "FF24.Ix": "Device Instance",
        "FF24.DT1.Ix": "Pushbutton",
        "FF24.DT2.Ix": "Absolute Input Device",
        "FF24.DT3.Ix": "Occupancy Sensor",
        "FF24.DT4.Ix": "Light Sensor",
        "FF24.DT6.Ix": "General Purpose Sensor",
        "FF24.DT32.Ix": "Feedback",
    }

    lines = []
    for group_name in sorted(groups.keys(), key=_sort_key):
        desc = group_descriptions.get(group_name, "")
        header = f"{group_name} ({desc})" if desc else group_name
        lines.append(f"\n{header}:")
        for key, info in sorted(groups[group_name]):
            suffix = ""
            if info.needs_data:
                suffix = " (requires --data)"
            if info.kind in ("gear_special", "device_special"):
                suffix += " (no address)"
            has_response = info.cls.response is not None
            if has_response:
                suffix += " [query]"
            lines.append(f"  {key}{suffix}")

    return "\n".join(lines)
