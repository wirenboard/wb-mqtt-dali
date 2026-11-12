# Installation profile: a directory with real wb-mqtt-serial and wb-mqtt-dali configs and
# devices-state.json, what the devices store and no config holds. The emulator and the stand
# script load the same directory, so the emulated devices and the installed configs agree.

from __future__ import annotations

import enum
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PROFILES_DIR = Path(__file__).resolve().parent / "profiles"
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


@dataclass(frozen=True)
class GearProduct:
    model: GearModel
    gtin: int


# Product names wb-mqtt-dali derives from these GTINs via products.csv.
KNOWN_GEAR_PRODUCTS = {
    "OTI DALI 15/220-240/1A0 NFC": GearProduct(GearModel.ENERGY, 6937186107749),
    "BK-DCL010S-AHA0700ADN": GearProduct(GearModel.PLAIN, 6971571384049),
    "DC MINI JOLLY DALI BI": GearProduct(GearModel.PLAIN, 8010703381377),
}
DEFAULT_INSTANCE_TYPES = (1,)


@dataclass(frozen=True)
class GearSpec:
    address: int
    random_address: int
    model: GearModel
    groups: frozenset[int]
    gtin: Optional[int] = None


@dataclass(frozen=True)
class ControlDeviceSpec:
    address: int
    random_address: int
    instance_types: tuple[int, ...]


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
    # The wb-mqtt-serial port section holding the gateway; the stand installs it verbatim but for `path`.
    serial_config: dict
    dali_config: dict

    @property
    def gateway_device_id(self) -> str:
        return gateway_device_id(self.gateway_slave_id)


def resolve_profile_path(name_or_path: str) -> Path:
    """A bare name is a directory in `profiles/`, anything with a slash is a path."""
    return Path(name_or_path) if "/" in name_or_path else PROFILES_DIR / name_or_path


def load_profile(name_or_path: str) -> InstallationProfile:
    directory = resolve_profile_path(name_or_path)
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


def _bus_from_dali_config(data: dict, state: dict, number: int) -> BusProfile:
    """Gear and control devices of a wb-mqtt-dali config bus with their devices-state.json entries."""
    entries = data.get("devices", [])
    gear_entries = [e for e in entries if not e.get("dali2", False)]
    device_entries = [e for e in entries if e.get("dali2", False)]
    groups = _device_state(state, "gear", gear_entries, number)
    instances = _device_state(state, "control_devices", device_entries, number)
    gear = []
    for entry in gear_entries:
        product = KNOWN_GEAR_PRODUCTS.get(_product_name(entry))
        gear.append(
            GearSpec(
                address=int(entry["short"]),
                random_address=int(entry["random"]),
                model=product.model if product else GearModel.PLAIN,
                groups=frozenset(groups.get(int(entry["short"]), ())),
                gtin=product.gtin if product else None,
            )
        )
    devices = [
        ControlDeviceSpec(
            address=int(entry["short"]),
            random_address=int(entry["random"]),
            instance_types=instances.get(int(entry["short"]), DEFAULT_INSTANCE_TYPES),
        )
        for entry in device_entries
    ]
    return BusProfile(gear=tuple(gear), control_devices=tuple(devices))


def _device_state(state: dict, kind: str, entries: list[dict], number: int) -> dict[int, tuple[int, ...]]:
    """Short address -> values of one `kind` of a bus; every address must be such a device in the config."""
    values = {int(short): tuple(int(v) for v in items) for short, items in state.get(kind, {}).items()}
    if unknown := set(values) - {int(e["short"]) for e in entries}:
        raise ValueError(
            f"{DEVICES_STATE_FILE}: bus {number} {kind} {sorted(unknown)} are not in {DALI_CONFIG_FILE}"
        )
    return values


def _product_name(entry: dict) -> str:
    """`"<product> <short>"` is how wb-mqtt-dali names gear it identified; the suffix is dropped."""
    name = str(entry.get("name", ""))
    suffix = f" {entry['short']}"
    return name[: -len(suffix)] if name.endswith(suffix) else name
