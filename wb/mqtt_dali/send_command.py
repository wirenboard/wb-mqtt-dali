import enum
import functools
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
    FeatureDevice,
    FeatureInstanceNumber,
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
from dali.device.general import _Event

from .device import absolute_input_device, feedback, general_purpose_sensor
from .gear import (
    demand_response,
    dimming_curve,
    integrated_power_supply,
    switching_function,
    thermal_gear_protection,
    thermal_lamp_protection,
)


class InstanceMode(enum.Enum):
    """How a command relates to the `I<n>` argument.

    DISALLOWED — the command never takes `I<n>` (gear, device standard/special).
    REQUIRED  — `I<n>` is mandatory (DALI 2 general/per-type instance commands).
    OPTIONAL  — both forms are valid; without `I<n>` the command targets the
                whole device, with `I<n>` it targets one instance (Part 332
                feature commands today).
    """

    DISALLOWED = "disallowed"
    REQUIRED = "required"
    OPTIONAL = "optional"


class AddressKind(enum.Enum):
    """Which DALI address space a command targets. Special commands (without
    address) are represented as `Optional[AddressKind] = None` rather than a
    third enum value — see CommandInfo.address_kind."""

    GEAR = "gear"
    DEVICE = "device"


# Literal token for the `0xff` form of the commissioning specials — in
# python-dali this is `Initialise(address=None)` and `ProgramShortAddress`/
# `VerifyShortAddress("MASK")`, one sentinel meaning "no short address".
_NO_SHORT_ADDRESS_TOKEN = "no_short_address"

# python-dali's sentinel for the short-address-special `0xff` form.
_SHORT_ADDR_MASK = "MASK"


class CommissioningArgForm(enum.Enum):
    """Argument form of the commissioning addressing specials
    (Initialise/ProgramShortAddress/VerifyShortAddress).

    BROADCAST       — no argument (Initialise only): frame byte `0x00`.
    SHORT_ADDRESS   — `A<n>`: frame byte `(n<<1)|1`.
    NO_SHORT_ADDRESS — `no_short_address` literal: frame byte `0xff`.
    """

    BROADCAST = "broadcast"
    SHORT_ADDRESS = "short_address"
    NO_SHORT_ADDRESS = "no_short_address"


@dataclass(frozen=True)
class CommissioningArg:
    form: CommissioningArgForm
    address: Optional[int] = None  # set only when form is SHORT_ADDRESS


@dataclass(frozen=True)
class CatalogEntry:
    """Catalog row exposed via `Bus/ListCommands` — just what the frontend
    needs to render an autocomplete entry and insert it. Range/instance
    validation lives on the server; the parser owns the full contract."""

    name: str
    category: str
    snippet: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "snippet": self.snippet,
        }


@dataclass
class CommandInfo:  # pylint: disable=too-many-instance-attributes
    cls: Type[Command]
    kind: str
    device_type: int
    needs_data: bool
    instance_mode: InstanceMode
    display_name: str = ""
    snippet: str = ""
    address_kind: Optional[AddressKind] = None
    category: str = ""
    needs_address: bool = False
    has_response: bool = False

    def to_catalog_entry(self) -> CatalogEntry:
        return CatalogEntry(
            name=self.display_name,
            category=self.category,
            snippet=self.snippet,
        )


@dataclass(frozen=True)
class _KindTraits:
    address_kind: Optional[AddressKind]
    needs_address: bool
    instance_mode: InstanceMode


_KIND_TRAITS: Dict[str, _KindTraits] = {
    "gear_standard": _KindTraits(
        address_kind=AddressKind.GEAR, needs_address=True, instance_mode=InstanceMode.DISALLOWED
    ),
    "gear_special": _KindTraits(
        address_kind=None, needs_address=False, instance_mode=InstanceMode.DISALLOWED
    ),
    # Commissioning specials (Initialise/ProgramShortAddress/VerifyShortAddress)
    # carry a short-address *parameter* (token `A<n>`), not a normal A/G
    # destination, so `needs_address` stays False — the address syntax is
    # handled separately by the commissioning builder/formatter.
    "gear_commissioning": _KindTraits(
        address_kind=None, needs_address=False, instance_mode=InstanceMode.DISALLOWED
    ),
    "device_standard": _KindTraits(
        address_kind=AddressKind.DEVICE, needs_address=True, instance_mode=InstanceMode.DISALLOWED
    ),
    "device_instance": _KindTraits(
        address_kind=AddressKind.DEVICE, needs_address=True, instance_mode=InstanceMode.REQUIRED
    ),
    "device_feature": _KindTraits(
        address_kind=AddressKind.DEVICE, needs_address=True, instance_mode=InstanceMode.OPTIONAL
    ),
    "device_special": _KindTraits(
        address_kind=None, needs_address=False, instance_mode=InstanceMode.DISALLOWED
    ),
}


_DT_GEAR_CATEGORY = {
    1: "DT1 Emergency Lighting",
    4: "DT4 Incandescent Lamps",
    5: "DT5 Converter",
    6: "DT6 LED Gear",
    7: "DT7 Switching Function",
    8: "DT8 Colour Control",
    16: "DT16 Thermal Gear Protection",
    17: "DT17 Dimming Curve",
    20: "DT20 Demand Response",
    21: "DT21 Thermal Lamp Protection",
    49: "DT49 Integrated Power Supply",
}

_INSTANCE_TYPE_CATEGORY = {
    1: "FF24.DT1 Pushbutton",
    2: "FF24.DT2 Absolute Input Device",
    3: "FF24.DT3 Occupancy Sensor",
    4: "FF24.DT4 Light Sensor",
    6: "FF24.DT6 General Purpose Sensor",
}

_FEATURE_CATEGORY = {
    32: "FF24.F32 Feedback",
}


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


def _make_info(  # pylint: disable=too-many-arguments, R0917
    cls: Type[Command],
    kind: str,
    device_type: int,
    needs_data: bool,
    category: str,
    display_name: str,
    snippet_override: Optional[str] = None,
) -> CommandInfo:
    traits = _KIND_TRAITS[kind]
    snippet = (
        snippet_override if snippet_override is not None else _build_snippet(display_name, kind, needs_data)
    )
    return CommandInfo(
        cls=cls,
        kind=kind,
        device_type=device_type,
        needs_data=needs_data,
        instance_mode=traits.instance_mode,
        display_name=display_name,
        snippet=snippet,
        address_kind=traits.address_kind,
        category=category,
        needs_address=traits.needs_address,
        has_response=cls.response is not None,
    )


def _build_snippet(name: str, kind: str, needs_data: bool) -> str:
    """Snippet template uses tab-stops for each argument the user must fill in.
    `optional` instance commands include the `I<n>` tab-stop — the client (or
    parser) may drop it for the device-level form.
    """
    traits = _KIND_TRAITS[kind]
    args: list[str] = []
    tab = 1
    if traits.needs_address:
        args.append(f"${{{tab}:A0}}")
        tab += 1
    if traits.instance_mode in (InstanceMode.REQUIRED, InstanceMode.OPTIONAL):
        args.append(f"${{{tab}:I0}}")
        tab += 1
    if needs_data:
        args.append(f"${{{tab}:data}}")
        tab += 1
    if not args:
        return name
    return f"{name}({', '.join(args)})"


def _register_feature_commands(registry: Dict[str, CommandInfo], device_inst_base: Type[Command]) -> None:
    """One entry per feature command. `instance_mode=OPTIONAL` means the parser
    accepts both `FF24.F32.Name(A<n>)` (device-level) and
    `FF24.F32.Name(A<n>, I<k>)` (per-instance) forms.
    """
    feature_modules = [
        (feedback, feedback.feature_type),
    ]
    for module, ft in feature_modules:
        category = _FEATURE_CATEGORY.get(ft, f"FF24.F{ft}")
        for name, cls in _collect_commands(module, device_inst_base).items():
            needs_data = getattr(cls, "_hasparam", None) is True
            key = f"FF24.F{ft}.{name}"
            registry[key] = _make_info(
                cls=cls,
                kind="device_feature",
                device_type=0,
                needs_data=needs_data,
                category=category,
                display_name=key,
            )


def build_command_registry() -> Dict[str, CommandInfo]:  # pylint: disable=too-many-locals
    registry: Dict[str, CommandInfo] = {}

    # --- Gear standard commands (from dali.gear.general) ---
    gear_standard_base = gear_general._StandardCommand  # pylint: disable=protected-access
    for name, cls in _collect_commands(gear_general, gear_standard_base).items():
        if name == "UnknownGearCommand":
            continue
        needs_data = getattr(cls, "_hasparam", None) is True
        registry[name] = _make_info(
            cls=cls,
            kind="gear_standard",
            device_type=0,
            needs_data=needs_data,
            category="Gear General",
            display_name=name,
        )

    registry["DAPC"] = _make_info(
        cls=gear_general.DAPC,
        kind="gear_standard",
        device_type=0,
        needs_data=True,
        category="Gear General",
        display_name="DAPC",
        snippet_override="DAPC(${1:A0}, ${2:level})",
    )

    # --- Gear special commands (no address) ---
    # Includes all single-arg `_SpecialCommand` subclasses from python-dali:
    # data-byte ones (DTR0/1/2, EnableDeviceType, SetSearchAddr*) and no-arg
    # ones (Compare, Randomise, Ping, QueryShortAddress, Terminate, Withdraw).
    # The commissioning addressing specials (Initialise, ProgramShortAddress,
    # VerifyShortAddress) are registered below under `gear_commissioning`.
    # Two-byte or broadcast/address constructors (WriteMemoryLocation*) are out
    # of scope of the single-`data` shape.
    gear_specials: list[tuple[str, Type[Command], bool]] = [
        ("Compare", gear_general.Compare, False),
        ("DTR0", gear_general.DTR0, True),
        ("DTR1", gear_general.DTR1, True),
        ("DTR2", gear_general.DTR2, True),
        ("EnableDeviceType", gear_general.EnableDeviceType, True),
        ("Ping", gear_general.Ping, False),
        ("QueryShortAddress", gear_general.QueryShortAddress, False),
        ("Randomise", gear_general.Randomise, False),
        ("SetSearchAddrH", gear_general.SetSearchAddrH, True),
        ("SetSearchAddrL", gear_general.SetSearchAddrL, True),
        ("SetSearchAddrM", gear_general.SetSearchAddrM, True),
        ("Terminate", gear_general.Terminate, False),
        ("Withdraw", gear_general.Withdraw, False),
    ]
    for name, cls, needs_data in gear_specials:
        registry[name] = _make_info(
            cls=cls,
            kind="gear_special",
            device_type=0,
            needs_data=needs_data,
            category="Gear Special",
            display_name=name,
        )

    # --- Gear commissioning addressing specials ---
    # Their argument is a short address (token `A<n>`) or the `no_short_address`
    # literal (0xff frame), not a data byte; Initialise additionally accepts no
    # argument for the broadcast form.
    # Snippets hint the address forms: Initialise offers a broadcast (empty),
    # `A<n>`, or `no_short_address` choice; the short-address specials offer
    # `A<n>` or `no_short_address`.
    gear_commissioning: list[tuple[str, Type[Command], str]] = [
        ("Initialise", gear_general.Initialise, f"Initialise(${{1|,A0,{_NO_SHORT_ADDRESS_TOKEN}|}})"),
        (
            "ProgramShortAddress",
            gear_general.ProgramShortAddress,
            f"ProgramShortAddress(${{1|A0,{_NO_SHORT_ADDRESS_TOKEN}|}})",
        ),
        (
            "VerifyShortAddress",
            gear_general.VerifyShortAddress,
            f"VerifyShortAddress(${{1|A0,{_NO_SHORT_ADDRESS_TOKEN}|}})",
        ),
    ]
    for name, cls, snippet in gear_commissioning:
        registry[name] = _make_info(
            cls=cls,
            kind="gear_commissioning",
            device_type=0,
            needs_data=False,
            category="Gear Special",
            display_name=name,
            snippet_override=snippet,
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
        category = _DT_GEAR_CATEGORY.get(dt_num, f"DT{dt_num}")
        for name, cls in _collect_commands(module, gear_standard_base).items():
            key = f"DT{dt_num}.{name}"
            needs_data = getattr(cls, "_hasparam", None) is True
            registry[key] = _make_info(
                cls=cls,
                kind="gear_standard",
                device_type=dt_num,
                needs_data=needs_data,
                category=category,
                display_name=key,
            )

    # --- Device standard commands (FF24 prefix) ---
    device_std_base = device_general._StandardDeviceCommand  # pylint: disable=protected-access
    for name, cls in _collect_commands(device_general, device_std_base).items():
        if name == "UnknownDeviceCommand":
            continue
        key = f"FF24.{name}"
        needs_data = getattr(cls, "_hasparam", None) is True
        registry[key] = _make_info(
            cls=cls,
            kind="device_standard",
            device_type=0,
            needs_data=needs_data,
            category="FF24 Device General",
            display_name=key,
        )

    # --- Device instance commands from dali.device.general (FF24.<Name>) ---
    # python-dali keeps `_StandardDeviceCommand` and `_StandardInstanceCommand`
    # name sets disjoint, so registering both under `FF24.<Name>` doesn't
    # collide. They live in the same "FF24 Device General" category.
    device_inst_base = device_general._StandardInstanceCommand  # pylint: disable=protected-access
    for name, cls in _collect_commands(device_general, device_inst_base).items():
        key = f"FF24.{name}"
        needs_data = getattr(cls, "_hasparam", None) is True
        registry[key] = _make_info(
            cls=cls,
            kind="device_instance",
            device_type=0,
            needs_data=needs_data,
            category="FF24 Device General",
            display_name=key,
        )

    # --- Instance-type-specific device instance commands (FF24.DT<m>.<Name>) ---
    # Each `_INSTANCE_TYPE_CATEGORY` module exposes only `_StandardInstanceCommand`
    # subclasses, so their names cannot collide with `FF24.DT<m>.<Name>` from any
    # other source (we only ever register them here).
    instance_type_modules = [
        (device_pushbutton, device_pushbutton.instance_type),
        (absolute_input_device, absolute_input_device.instance_type),
        (device_occupancy, device_occupancy.instance_type),
        (device_light, device_light.instance_type),
        (general_purpose_sensor, general_purpose_sensor.instance_type),
    ]
    for module, it in instance_type_modules:
        category = _INSTANCE_TYPE_CATEGORY.get(it, f"FF24.DT{it}")
        for name, cls in _collect_commands(module, device_inst_base).items():
            key = f"FF24.DT{it}.{name}"
            needs_data = getattr(cls, "_hasparam", None) is True
            registry[key] = _make_info(
                cls=cls,
                kind="device_instance",
                device_type=0,
                needs_data=needs_data,
                category=category,
                display_name=key,
            )

    _register_feature_commands(registry, device_inst_base)

    # --- Device special commands (FF24 prefix, no address) ---
    # Mirrors the gear-special list; two-byte forms (DTR1DTR0, DTR2DTR1,
    # DirectWriteMemory) and address-only commands (SendTestframe) are out
    # of the single-`data` shape and omitted.
    device_specials: list[tuple[str, Type[Command], bool]] = [
        ("FF24.Compare", device_general.Compare, False),
        ("FF24.DTR0", device_general.DTR0, True),
        ("FF24.DTR1", device_general.DTR1, True),
        ("FF24.DTR2", device_general.DTR2, True),
        ("FF24.Initialise", device_general.Initialise, True),
        ("FF24.ProgramShortAddress", device_general.ProgramShortAddress, True),
        ("FF24.QueryShortAddress", device_general.QueryShortAddress, False),
        ("FF24.Randomise", device_general.Randomise, False),
        ("FF24.SearchAddrH", device_general.SearchAddrH, True),
        ("FF24.SearchAddrL", device_general.SearchAddrL, True),
        ("FF24.SearchAddrM", device_general.SearchAddrM, True),
        ("FF24.Terminate", device_general.Terminate, False),
        ("FF24.VerifyShortAddress", device_general.VerifyShortAddress, True),
        ("FF24.Withdraw", device_general.Withdraw, False),
    ]
    for key, cls, needs_data in device_specials:
        registry[key] = _make_info(
            cls=cls,
            kind="device_special",
            device_type=0,
            needs_data=needs_data,
            category="FF24 Device Special",
            display_name=key,
        )

    return registry


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


def _build_commissioning_command(info: CommandInfo, arg: CommissioningArg) -> Command:
    """Build a commissioning addressing special from its parsed argument.
    `Initialise` uses the `(broadcast, address)` constructor; the short-address
    specials take a single `int`/`"MASK"` argument. BROADCAST is valid only for
    Initialise — the parser enforces that before this is called.
    """
    if info.cls is gear_general.Initialise:
        if arg.form is CommissioningArgForm.BROADCAST:
            return info.cls(broadcast=True)
        if arg.form is CommissioningArgForm.NO_SHORT_ADDRESS:
            return info.cls(address=None)
        return info.cls(address=arg.address)
    if arg.form is CommissioningArgForm.NO_SHORT_ADDRESS:
        return info.cls(_SHORT_ADDR_MASK)
    return info.cls(arg.address)


def _build_command(  # pylint: disable=too-many-arguments, R0917, too-many-return-statements
    info: CommandInfo,
    address: Optional[int],
    group: Optional[int],
    broadcast: bool,
    data: Optional[int],
    instance_number: Optional[int],
    commissioning_arg: Optional[CommissioningArg] = None,
) -> Command:
    """Pure builder: trusts that the caller has validated all inputs against
    the kind's traits. The `needs_data=True` branches for instance/feature
    kinds are omitted — no such command exists in the current python-dali
    registry (data is delivered via a preceding DTR0/1/2 send).
    """
    if info.kind == "gear_standard":
        addr = build_gear_address(address, group, broadcast)
        if info.needs_data:
            return info.cls(addr, data)
        return info.cls(addr)

    if info.kind == "gear_special":
        if info.needs_data:
            return info.cls(data)
        return info.cls()

    if info.kind == "gear_commissioning":
        return _build_commissioning_command(info, commissioning_arg)

    if info.kind == "device_standard":
        addr = build_device_address(address, group, broadcast)
        if info.needs_data:
            return info.cls(addr, data)
        return info.cls(addr)

    if info.kind == "device_instance":
        addr = build_device_address(address, group, broadcast)
        return info.cls(addr, InstanceNumber(instance_number))

    if info.kind == "device_feature":
        addr = build_device_address(address, group, broadcast)
        if instance_number is not None:
            return info.cls(addr, FeatureInstanceNumber(instance_number))
        return info.cls(addr, FeatureDevice())

    if info.kind == "device_special":
        if info.needs_data:
            return info.cls(data)
        return info.cls()

    raise ValueError(f"Unknown command kind: {info.kind}")


# Registry keys are dotted segments where each segment starts with a letter,
# e.g. `FF24.DT1.SetEventFilter`. The regex enforces that shape so a malformed
# leading character is flagged at the parser level rather than as `Unknown command`.
_EXPRESSION_PATTERN = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9]*(?:\.[A-Za-z][A-Za-z0-9]*)*)\s*(?:\(\s*(.*?)\s*\))?\s*$", re.DOTALL
)


def _parse_token(token: str, command_name: str) -> tuple[str, int]:
    prefix = token[0].upper() if token else ""
    if prefix in ("A", "G", "I") and len(token) > 1 and token[1:].isdigit():
        value = int(token[1:])
        kind = {"A": "addr", "G": "group", "I": "instance"}[prefix]
        return kind, value
    try:
        return "data", int(token, 0)
    except ValueError as exc:
        raise ValueError(
            f"Command '{command_name}': cannot parse argument '{token}'. "
            "Use A<n>/G<n>/I<n> (unsigned) for address/group/instance, or an integer for data."
        ) from exc


def _parse_commissioning_arg(info: CommandInfo, tokens: list[str], command_name: str) -> CommissioningArg:
    """Parse the single argument of a commissioning addressing special into a
    `CommissioningArg`: `A<n>` (short address), `no_short_address` (the `0xff`
    form), or — Initialise only — no argument (broadcast)."""
    if len(tokens) > 1:
        raise ValueError(f"Command '{command_name}' takes a single A<n>/{_NO_SHORT_ADDRESS_TOKEN} argument")
    if not tokens:
        if info.cls is not gear_general.Initialise:
            raise ValueError(f"Command '{command_name}' requires an A<n>/{_NO_SHORT_ADDRESS_TOKEN} argument")
        return CommissioningArg(form=CommissioningArgForm.BROADCAST)

    token = tokens[0]
    if token == _NO_SHORT_ADDRESS_TOKEN:
        return CommissioningArg(form=CommissioningArgForm.NO_SHORT_ADDRESS)
    kind, value = _parse_token(token, command_name)
    if kind != "addr":
        raise ValueError(
            f"Command '{command_name}': argument must be A<n> or {_NO_SHORT_ADDRESS_TOKEN}, got '{token}'"
        )
    if not 0 <= value <= 63:
        raise ValueError(f"Command '{command_name}': A<n> must be in range 0..63, got {value}")
    return CommissioningArg(form=CommissioningArgForm.SHORT_ADDRESS, address=value)


def parse_expression(  # pylint: disable=too-many-locals, too-many-branches, too-many-statements
    expr: str, registry: Dict[str, CommandInfo]
) -> Command:
    """Parse `Name(args)` into a Command. Args: `A<n>` short, `G<n>` group,
    `I<n>` instance, bare integer for data. Parens optional when there are no
    args. Address-taking commands default to broadcast when no A/G is given.
    """
    # Check parens balance before the regex so the user gets an explicit
    # message; the regex would otherwise reject `Off(A5` generically.
    raw = expr.strip()
    if raw.count("(") != raw.count(")"):
        raise ValueError(f"Cannot parse command expression: {expr!r} (unbalanced parentheses)")
    match = _EXPRESSION_PATTERN.match(expr)
    if not match:
        raise ValueError(f"Cannot parse command expression: {expr!r}")

    command_name = match.group(1)
    body = match.group(2) or ""

    tokens = [t.strip() for t in body.split(",")] if body.strip() else []
    tokens = [t for t in tokens if t]

    info = registry.get(command_name)
    if info is None:
        raise ValueError(f"Unknown command: {command_name}")

    # Commissioning addressing specials have their own single-argument grammar
    # (`A<n>` / `no_short_address` / — Initialise only — broadcast).
    if info.kind == "gear_commissioning":
        arg = _parse_commissioning_arg(info, tokens, command_name)
        return _build_command(info, None, None, False, None, None, commissioning_arg=arg)

    address: Optional[int] = None
    group: Optional[int] = None
    instance: Optional[int] = None
    data: Optional[int] = None
    for token in tokens:
        kind, value = _parse_token(token, command_name)
        if kind == "addr":
            if address is not None:
                raise ValueError(f"Command '{command_name}': duplicate address argument")
            if group is not None:
                raise ValueError(f"Command '{command_name}': cannot mix A<n> and G<n>")
            address = value
        elif kind == "group":
            if group is not None:
                raise ValueError(f"Command '{command_name}': duplicate group argument")
            if address is not None:
                raise ValueError(f"Command '{command_name}': cannot mix A<n> and G<n>")
            group = value
        elif kind == "instance":
            if instance is not None:
                raise ValueError(f"Command '{command_name}': duplicate instance argument")
            instance = value
        else:  # data
            if data is not None:
                raise ValueError(f"Command '{command_name}': duplicate data argument")
            data = value

    traits = _KIND_TRAITS[info.kind]

    if not traits.needs_address and (address is not None or group is not None):
        raise ValueError(f"Command '{command_name}' is a special command and does not take A<n>/G<n>")
    if traits.instance_mode is InstanceMode.DISALLOWED and instance is not None:
        raise ValueError(f"Command '{command_name}' does not take I<n>")
    if traits.instance_mode is InstanceMode.REQUIRED and instance is None:
        raise ValueError(f"Command '{command_name}' requires I<n>")
    if not info.needs_data and data is not None:
        raise ValueError(f"Command '{command_name}' does not take a data argument")
    if info.needs_data and data is None:
        raise ValueError(f"Command '{command_name}' requires a data argument")
    if address is not None and not 0 <= address <= 63:
        raise ValueError(f"Command '{command_name}': A<n> must be in range 0..63, got {address}")
    if group is not None:
        max_group = 15 if traits.address_kind is AddressKind.GEAR else 31
        if not 0 <= group <= max_group:
            raise ValueError(f"Command '{command_name}': G<n> must be in range 0..{max_group}, got {group}")
    if instance is not None and not 0 <= instance <= 31:
        raise ValueError(f"Command '{command_name}': I<n> must be in range 0..31, got {instance}")
    if data is not None and (data < 0 or data > 255):
        raise ValueError(f"Command '{command_name}': data must be in range 0..255, got {data}")

    # No A/G on an address-taking command means broadcast.
    broadcast = traits.needs_address and address is None and group is None
    return _build_command(info, address, group, broadcast, data, instance)


@functools.lru_cache(maxsize=1)
def _formatting_tables() -> tuple[Dict[str, CommandInfo], Dict[Type[Command], CommandInfo]]:
    """Registry plus its reverse `class -> CommandInfo` index, built once for
    rendering observed bus traffic as `Name(...)` expressions. A class exposed
    under several aliases (the `QueryEventFilter{L,M,H}` vs
    `…{ZeroToSeven,EightToFifteen,SixteenToTwentyThree}` pairs) resolves to the
    longer, more descriptive name."""
    registry = build_command_registry()
    reverse: Dict[Type[Command], CommandInfo] = {}
    for info in registry.values():
        existing = reverse.get(info.cls)
        if existing is None or len(info.display_name) > len(existing.display_name):
            reverse[info.cls] = info
    return registry, reverse


def _commissioning_arg_token(command: Command) -> str:
    """Render the single argument of a decoded commissioning special as the
    inverse of `_build_commissioning_command`. Initialise carries `.broadcast`
    plus `.address` (int or None); the short-address specials carry `.address`
    (int or `"MASK"`). Empty string means the broadcast form (no argument)."""
    if isinstance(command, gear_general.Initialise):
        if command.broadcast:
            return ""
        if command.address is None:
            return _NO_SHORT_ADDRESS_TOKEN
        return f"A{command.address}"
    if command.address == _SHORT_ADDR_MASK:
        return _NO_SHORT_ADDRESS_TOKEN
    return f"A{command.address}"


def _command_expression_args(command: Command, info: CommandInfo) -> list[str]:
    """`A<n>/G<n>`, `I<n>` and data tokens read off a decoded command as the
    deterministic inverse of `_build_command`, reading the canonical field for
    the command's kind (data lives in `.power` for DAPC, `.param` for every
    other data-bearing registry command — never both)."""
    traits = _KIND_TRAITS[info.kind]
    args: list[str] = []

    destination = getattr(command, "destination", None)
    if traits.needs_address and destination is not None:
        if getattr(destination, "address", None) is not None:
            args.append(f"A{destination.address}")
        elif getattr(destination, "group", None) is not None:
            args.append(f"G{destination.group}")
        # Broadcast carries neither attribute — an omitted address means broadcast.

    if traits.instance_mode is not InstanceMode.DISALLOWED:
        instance = getattr(command, "instance", None)
        instance_value = getattr(instance, "value", None) if instance is not None else None
        if instance_value is not None:
            args.append(f"I{instance_value}")

    if info.needs_data:
        data = getattr(command, "power", None)
        if data is None:
            data = getattr(command, "param", None)
        if data is not None:
            args.append(str(data))

    return args


def _event_expression_args(event: _Event) -> list[str]:
    args: list[str] = []
    if event.short_address is not None:
        args.append(f"A{event.short_address.address}")
    elif event.device_group is not None:
        args.append(f"G{event.device_group}")
    elif event.instance_group is not None:
        args.append(f"IG{event.instance_group}")
    if event.instance_number is not None:
        args.append(f"I{event.instance_number}")
    if event.event_data is not None:
        args.append(str(event.event_data))
    return args


def format_command_expression(command: Command) -> str:
    """Render a decoded command as the `Name(A<n>, ...)` expression the RPC
    `sendcommand` accepts. Total by construction: for a command whose type is in
    the registry this is the deterministic inverse of how the command is built
    (so it reproduces the same frame); a DALI 2 event renders its `Name(A<n>,
    I<n>, ...)` form in the same token convention; any other type (UnknownGearCommand) 
    falls back to python-dali's `str()`. Never returns None."""
    if isinstance(command, _Event):
        args = _event_expression_args(command)
        name = type(command).__name__
        return name if not args else f"{name}({', '.join(args)})"

    _, reverse = _formatting_tables()
    info = reverse.get(type(command))
    if info is None:
        try:
            return str(command)
        except Exception:  # pylint: disable=broad-exception-caught
            return f"<{type(command).__name__}>"

    if info.kind == "gear_commissioning":
        token = _commissioning_arg_token(command)
        return info.display_name if not token else f"{info.display_name}({token})"

    args = _command_expression_args(command, info)
    return info.display_name if not args else f"{info.display_name}({', '.join(args)})"


class LazyCommandExpression:  # pylint: disable=too-few-public-methods
    """`%s`-friendly wrapper whose `__str__` runs `format_command_expression`
    only when a log record is actually emitted. `format_command_expression` is
    heavier than `str()`, so passing a command straight to `logger.debug(...)`
    would pay that cost even at a disabled level; this defers it. Accepts any
    object — non-Command/non-registry values degrade to `str()`."""

    __slots__ = ("_command",)

    def __init__(self, command: Any) -> None:
        self._command = command

    def __str__(self) -> str:
        return format_command_expression(self._command)


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


def _category_sort_key(category: str) -> tuple:  # pylint: disable=too-many-return-statements
    """Order categories by an explicit rank that matches the way users navigate
    the catalog: Gear General → Gear Special → DT<n> (numeric) → FF24 Device
    General → FF24 Device Special → FF24.DT<m> instance-type-specific
    (numeric) → FF24.F<ft> feature (numeric). Lexicographic ordering puts FF24
    before Gear, which buries the gear sections; this fixes that.
    """
    head = category.split(" ", 1)[0]
    if category == "Gear General":
        return (0,)
    if category == "Gear Special":
        return (1,)
    if head.startswith("DT") and head[2:].isdigit():
        return (2, int(head[2:]))
    if category == "FF24 Device General":
        return (3,)
    if category == "FF24 Device Special":
        return (4,)
    if head.startswith("FF24.DT") and head[len("FF24.DT") :].isdigit():
        return (5, int(head[len("FF24.DT") :]))
    if head.startswith("FF24.F") and head[len("FF24.F") :].isdigit():
        return (6, int(head[len("FF24.F") :]))
    return (7, category)


def build_command_catalog(registry: Dict[str, CommandInfo]) -> list[CatalogEntry]:
    """Single source for `Bus/ListCommands` and `--list-commands`: one
    `CatalogEntry` per registry entry. Sorted by (category rank, name) —
    category rank is explicit (see `_category_sort_key`) so DT4 comes before
    DT16 and gear sections come before FF24 ones.
    """
    entries = [info.to_catalog_entry() for info in registry.values()]
    return sorted(entries, key=lambda e: (_category_sort_key(e.category), e.name))


_ADDRESS_FORM_GEAR = "gear: A0..63 or G0..15; omit address for broadcast"
_ADDRESS_FORM_DEVICE = "device: A0..63 or G0..31; omit address for broadcast"
_INSTANCE_FORM_REQUIRED = "requires I0..31"
_INSTANCE_FORM_OPTIONAL = "I<n> optional"


def category_header_suffix(infos: list[CommandInfo]) -> str:
    """Return the parenthesised suffix for a category header — address form
    plus, when uniform across the category, the instance form. Empty string
    if the category is empty.

    Address form follows `address_kind`: all-gear, all-device, or all-None
    (special). Instance form is appended (after `; `) only when every entry
    shares the same `instance_mode` and that mode is REQUIRED or OPTIONAL;
    mixed categories leave the instance form off the header — individual
    commands carry per-line markers instead (see `_command_instance_marker`).
    """
    if not infos:
        return ""

    address_kinds = {info.address_kind for info in infos}
    if len(address_kinds) != 1:
        # Mixed address kinds would only happen if the catalog grouping
        # changed; treat as no useful header rather than asserting.
        return ""
    kind = next(iter(address_kinds))
    if kind is AddressKind.GEAR:
        address_part = _ADDRESS_FORM_GEAR
    elif kind is AddressKind.DEVICE:
        address_part = _ADDRESS_FORM_DEVICE
    else:
        address_part = "no address"

    parts = [address_part]
    if kind is AddressKind.DEVICE:
        modes = {info.instance_mode for info in infos}
        if modes == {InstanceMode.REQUIRED}:
            parts.append(_INSTANCE_FORM_REQUIRED)
        elif modes == {InstanceMode.OPTIONAL}:
            parts.append(_INSTANCE_FORM_OPTIONAL)
        # All-DISALLOWED or mixed: leave the instance form off.

    return f" ({'; '.join(parts)})"


def _command_instance_marker(infos: list[CommandInfo], info: CommandInfo) -> str:
    """Per-command marker shown only when the category has a mixed
    `instance_mode` and this command is not DISALLOWED — the header can't
    express the rule for the whole section, so each non-DISALLOWED line
    carries it."""
    modes = {item.instance_mode for item in infos}
    if len(modes) <= 1:
        return ""
    if info.instance_mode is InstanceMode.REQUIRED:
        return " — requires I<n>"
    if info.instance_mode is InstanceMode.OPTIONAL:
        return f" — {_INSTANCE_FORM_OPTIONAL}"
    return ""


def list_commands(registry: Dict[str, CommandInfo]) -> str:
    """Render the command catalog as human-readable text from the registry.
    Shares ordering with `Bus/ListCommands` (same `_category_sort_key`) but
    reads address/instance/data flags straight off `CommandInfo` — the RPC
    catalog entry no longer carries them.
    """
    sorted_infos = sorted(
        registry.values(), key=lambda info: (_category_sort_key(info.category), info.display_name)
    )

    grouped: Dict[str, list[CommandInfo]] = {}
    for info in sorted_infos:
        grouped.setdefault(info.category, []).append(info)

    lines = []
    # Categories already have the "<code> <label>" shape (e.g. "Gear General",
    # "FF24.F32 Feedback"); the header suffix carries address/instance form.
    for category, infos in grouped.items():
        header_suffix = category_header_suffix(infos)
        lines.append(f"\n{category}{header_suffix}:")
        for info in infos:
            data_suffix = " (requires data)" if info.needs_data else ""
            instance_marker = _command_instance_marker(infos, info)
            lines.append(f"  {info.display_name}{data_suffix}{instance_marker}")

    return "\n".join(lines)
