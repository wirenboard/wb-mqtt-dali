# Installation profile: a directory with real wb-mqtt-serial and wb-mqtt-dali configs and
# devices-state.json, what the devices store and no config holds. The emulator and the e2e script
# load the same directory, so the emulated devices and the installed configs agree.

from __future__ import annotations

import enum
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from wb.mqtt_dali.helvar_device import HELVAR_321_GTIN, HelvarFirmware

SERIAL_CONFIG_FILE = "wb-mqtt-serial.conf"
DALI_CONFIG_FILE = "wb-mqtt-dali.conf"
DEVICES_STATE_FILE = "devices-state.json"
GATEWAY_DEVICE_TYPE = "WB-DALI"
DALI_PRIORITIES = 5


class Parity(enum.Enum):
    NONE = "N"
    EVEN = "E"
    ODD = "O"


class GearModel(enum.Enum):
    PLAIN = "plain"
    ENERGY = "energy"
    HELVAR_311 = "helvar_311"
    HELVAR_321 = "helvar_321"
    HELVAR_312 = "helvar_312"


@dataclass(frozen=True)
class GearProduct:
    model: GearModel
    gtin: Optional[int]


# The `model` of a gear in devices-state.json. wb-mqtt-dali derives the same product names from these
# GTINs via products.csv; Helvar 311 and default_gear have no GTIN.
KNOWN_GEAR_PRODUCTS = {
    "OTI DALI 15/220-240/1A0 NFC": GearProduct(GearModel.ENERGY, 6937186107749),
    "BK-DCL010S-AHA0700ADN": GearProduct(GearModel.PLAIN, 6971571384049),
    "DC MINI JOLLY DALI BI": GearProduct(GearModel.PLAIN, 8010703381377),
    "Helvar 311": GearProduct(GearModel.HELVAR_311, None),
    "Helvar 321": GearProduct(GearModel.HELVAR_321, None),
    "Helvar 312": GearProduct(GearModel.HELVAR_312, None),
    "default_gear": GearProduct(GearModel.PLAIN, None),
}
HELVAR_GEAR_MODELS = frozenset({GearModel.HELVAR_311, GearModel.HELVAR_321, GearModel.HELVAR_312})
GEAR_STATE_KEYS = frozenset({"model", "groups", "firmware"})
CONTROL_DEVICE_STATE_KEYS = frozenset({"model", "instance_types"})
DEFAULT_INSTANCE_TYPES = (1,)
HELVAR_FIRMWARE_PATTERN = re.compile(r"(\d+)\.(\d{2})")
HELVAR_FIRMWARE_PART_MAX = 0xFF
HELVAR_312_GTIN = 5030797003125


@dataclass(frozen=True)
class ControlDeviceProduct:
    gtin: int
    instance_types: tuple[int, ...]


# The `model` of a control device in devices-state.json; without one the fake's own bank 0 GTIN.
KNOWN_CONTROL_DEVICE_PRODUCTS = {
    "Helvar 321": ControlDeviceProduct(HELVAR_321_GTIN, (3, 4)),
}


@dataclass(frozen=True)
class GearSpec:
    address: int
    random_address: int
    model: GearModel
    groups: frozenset[int]
    gtin: Optional[int] = None
    # None: the Helvar model's own version.
    helvar_firmware: Optional[HelvarFirmware] = None


@dataclass(frozen=True)
class ControlDeviceSpec:
    address: int
    random_address: int
    instance_types: tuple[int, ...]
    # Bank 0 GTIN; None keeps the fake's own.
    gtin: Optional[int] = None


@dataclass(frozen=True)
class BusProfile:
    gear: tuple[GearSpec, ...]
    control_devices: tuple[ControlDeviceSpec, ...]


@dataclass(frozen=True)
class NeighbourSpec:
    template: str
    slave_id: int


@dataclass(frozen=True)
class PortProfile:
    baud_rate: int = 115200
    parity: Parity = Parity.NONE
    stop_bits: int = 2

    @property
    def byte_time_s(self) -> float:
        bits = 1 + 8 + (0 if self.parity == Parity.NONE else 1) + self.stop_bits
        return bits / self.baud_rate


@dataclass(frozen=True)
class TimingProfile:
    forward_16_ms: float = 15.8
    forward_24_ms: float = 22.5
    backward_ms: float = 9.2
    backward_delay_ms: float = 7.0
    no_backward_wait_ms: float = 10.5
    # Minimum bus idle time before a forward frame, indexed by DALI priority 1..5.
    settling_ms: tuple[float, ...] = (13.5, 14.9, 16.3, 17.9, 19.5)

    def delay_s(self, delay_ms: float) -> float:
        return delay_ms / 1000.0

    def settling_s(self, priority: int) -> float:
        return self.delay_s(self.settling_ms[min(max(priority, 1), DALI_PRIORITIES) - 1])


@dataclass(frozen=True)
class InstallationProfile:
    port: PortProfile
    neighbours: tuple[NeighbourSpec, ...]
    gateway_slave_id: int
    buses: tuple[BusProfile, ...]
    # The wb-mqtt-serial port section holding the gateway; installed verbatim but for `path`.
    serial_config: dict
    dali_config: dict

    @property
    def gateway_device_id(self) -> str:
        return gateway_device_id(self.gateway_slave_id)


def resolve_profile_path(name_or_path: str, profiles_dir: Path) -> Path:
    """A bare name is a directory in `profiles_dir`, anything with a slash is a path."""
    return Path(name_or_path) if "/" in name_or_path else profiles_dir / name_or_path


def load_profile(directory: Path) -> InstallationProfile:
    return profile_from_configs(
        *(
            json.loads((directory / name).read_text(encoding="utf-8"))
            for name in (SERIAL_CONFIG_FILE, DALI_CONFIG_FILE, DEVICES_STATE_FILE)
        )
    )


def profile_from_configs(serial_config: dict, dali_config: dict, devices_state: dict) -> InstallationProfile:
    port, gateway = _gateway_port(serial_config)
    slave_id = int(gateway["slave_id"])
    device_id = gateway_device_id(slave_id)
    config_buses = dali_config_buses(dali_config, device_id)
    if not config_buses:
        raise ValueError(f"{DALI_CONFIG_FILE} has no buses of {device_id}")
    if unknown := set(devices_state) - {str(n) for n in range(1, len(config_buses) + 1)}:
        raise ValueError(f"{DEVICES_STATE_FILE}: buses {sorted(unknown)} are not in {DALI_CONFIG_FILE}")
    return InstallationProfile(
        port=_port_profile(port),
        neighbours=tuple(
            NeighbourSpec(template=str(d["device_type"]), slave_id=int(d["slave_id"]))
            for d in port.get("devices", [])
            if d is not gateway
        ),
        gateway_slave_id=slave_id,
        buses=tuple(
            _bus_from_dali_config(bus, devices_state.get(str(number), {}), number)
            for number, bus in enumerate(config_buses, start=1)
        ),
        serial_config=port,
        dali_config=dali_config,
    )


def _gateway_port(serial_config: dict) -> tuple[dict, dict]:
    """The port section holding the config's only WB-DALI, and that WB-DALI."""
    found = [
        (port, device)
        for port in serial_config.get("ports", [])
        for device in port.get("devices", [])
        if device.get("device_type") == GATEWAY_DEVICE_TYPE
    ]
    if len(found) != 1:
        raise ValueError(
            f"{SERIAL_CONFIG_FILE} must hold exactly one {GATEWAY_DEVICE_TYPE}, got {len(found)}"
        )
    return found[0]


def _port_profile(data: dict) -> PortProfile:
    """Line settings of a wb-mqtt-serial port section; other keys are ignored."""
    defaults = PortProfile()
    return PortProfile(
        baud_rate=int(data.get("baud_rate", defaults.baud_rate)),
        parity=Parity(data.get("parity", defaults.parity.value)),
        stop_bits=int(data.get("stop_bits", defaults.stop_bits)),
    )


def dali_config_buses(config: dict, device_id: str) -> list[dict]:
    """Buses of gateway `device_id` in a wb-mqtt-dali config, empty if it has none."""
    for gateway in config.get("gateways", []):
        if gateway.get("device_id") == device_id:
            return list(gateway.get("buses", []))
    return []


def gateway_device_id(slave_id: int) -> str:
    """MQTT id wb-mqtt-serial gives a WB-DALI by its template and slave id."""
    return f"wb-dali_{slave_id}"


@dataclass(frozen=True)
class ExpectedDevice:
    bus: int
    short: int
    dali2: bool
    mqtt_id: str
    name: str


def expected_devices(dali_config: dict, device_id: str) -> list[ExpectedDevice]:
    result = []
    for number, bus in enumerate(dali_config_buses(dali_config, device_id), start=1):
        bus_uid = f"{device_id}_bus_{number}"
        for entry in bus.get("devices", []):
            dali2 = bool(entry.get("dali2", False))
            short = int(entry["short"])
            default_part = f"dali2_{short}" if dali2 else str(short)
            result.append(
                ExpectedDevice(
                    bus=number,
                    short=short,
                    dali2=dali2,
                    mqtt_id=entry.get("mqtt_id") or f"{bus_uid}_{default_part}",
                    name=entry.get("name") or f"{'DALI-2' if dali2 else 'DALI'} {short}",
                )
            )
    return result


def bus_device_ids(dali_config: dict, device_id: str) -> list[str]:
    """MQTT ids of the broadcast devices; one appears when its bus starts working."""
    buses = dali_config_buses(dali_config, device_id)
    return [f"{device_id}_bus_{number}_broadcast" for number in range(1, len(buses) + 1)]


def _bus_from_dali_config(  # pylint: disable=too-many-locals
    data: dict, state: dict, number: int
) -> BusProfile:
    """Gear and control devices of a wb-mqtt-dali config bus with their devices-state.json entries."""
    entries = data.get("devices", [])
    gear_entries = [e for e in entries if not e.get("dali2", False)]
    device_entries = [e for e in entries if e.get("dali2", False)]
    gear_states = _device_state(state, "gear", gear_entries, number, GEAR_STATE_KEYS)
    device_states = _device_state(state, "control_devices", device_entries, number, CONTROL_DEVICE_STATE_KEYS)
    gear = []
    for entry in gear_entries:
        short = int(entry["short"])
        gear_state = gear_states.get(short, {})
        product = _gear_product(gear_state, number, short)
        gear.append(
            GearSpec(
                address=short,
                random_address=int(entry["random"]),
                model=product.model,
                groups=frozenset(int(g) for g in gear_state.get("groups", [])),
                gtin=product.gtin,
                helvar_firmware=_helvar_firmware(gear_state, product.model, number, short),
            )
        )
    devices = []
    for entry in device_entries:
        short = int(entry["short"])
        device_state = device_states.get(short, {})
        device_product = _control_device_product(device_state, number, short)
        default_instance_types = (
            DEFAULT_INSTANCE_TYPES if device_product is None else device_product.instance_types
        )
        devices.append(
            ControlDeviceSpec(
                address=short,
                random_address=int(entry["random"]),
                instance_types=tuple(
                    int(t) for t in device_state.get("instance_types", default_instance_types)
                ),
                gtin=None if device_product is None else device_product.gtin,
            )
        )
    return BusProfile(gear=tuple(gear), control_devices=tuple(devices))


def _device_state(
    state: dict, kind: str, entries: list[dict], number: int, keys: frozenset[str]
) -> dict[int, dict]:
    """Short address -> entry of one `kind` of a bus; every address must be such a device in the config."""
    values = {int(short): item for short, item in state.get(kind, {}).items()}
    if unknown := set(values) - {int(e["short"]) for e in entries}:
        raise ValueError(
            f"{DEVICES_STATE_FILE}: bus {number} {kind} {sorted(unknown)} are not in {DALI_CONFIG_FILE}"
        )
    for short, item in values.items():
        where = f"{DEVICES_STATE_FILE}: bus {number} {kind} {short}"
        if not isinstance(item, dict):
            raise ValueError(f"{where}: expected an object")
        if unknown_keys := set(item) - keys:
            raise ValueError(f"{where}: unknown keys {sorted(unknown_keys)}")
    return values


def _gear_product(gear_state: dict, number: int, short: int) -> GearProduct:
    """Every gear needs an entry with a known `model`."""
    where = f"{DEVICES_STATE_FILE}: bus {number} gear {short}"
    if "model" not in gear_state:
        raise ValueError(f"{where}: no model, known: {sorted(KNOWN_GEAR_PRODUCTS)}")
    product = KNOWN_GEAR_PRODUCTS.get(gear_state["model"])
    if product is None:
        raise ValueError(
            f"{where}: unknown model {gear_state['model']!r}, known: {sorted(KNOWN_GEAR_PRODUCTS)}"
        )
    return product


def _helvar_firmware(gear_state: dict, model: GearModel, number: int, short: int) -> Optional[HelvarFirmware]:
    """`firmware`: a "major.minor" string with two minor digits, Helvar models only."""
    if "firmware" not in gear_state:
        return None
    where = f"{DEVICES_STATE_FILE}: bus {number} gear {short}"
    if model not in HELVAR_GEAR_MODELS:
        raise ValueError(f"{where}: firmware is for Helvar models only")
    value = gear_state["firmware"]
    match = HELVAR_FIRMWARE_PATTERN.fullmatch(value) if isinstance(value, str) else None
    firmware = None if match is None else HelvarFirmware(int(match[1]), int(match[2]))
    if firmware is None or firmware.major > HELVAR_FIRMWARE_PART_MAX:
        raise ValueError(f'{where}: firmware {value!r} is not a string like "6.08"')
    return firmware


def _control_device_product(device_state: dict, number: int, short: int) -> Optional[ControlDeviceProduct]:
    if "model" not in device_state:
        return None
    product = KNOWN_CONTROL_DEVICE_PRODUCTS.get(device_state["model"])
    if product is None:
        raise ValueError(
            f"{DEVICES_STATE_FILE}: bus {number} control_devices {short}: unknown model "
            f"{device_state['model']!r}, known: {sorted(KNOWN_CONTROL_DEVICE_PRODUCTS)}"
        )
    return product
