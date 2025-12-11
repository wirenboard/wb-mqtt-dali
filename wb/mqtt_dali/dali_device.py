from dataclasses import dataclass, field


@dataclass
class DaliDeviceAddress:
    short: int
    random: int


@dataclass
class DaliDevice:
    uid: str
    name: str
    address: DaliDeviceAddress
    groups: list[str] = field(default_factory=list)


def make_device(bus_uid: str, address: DaliDeviceAddress) -> DaliDevice:
    return DaliDevice(
        uid=f"{bus_uid}_{address.short}",
        name=f"Dev {address.short}:{address.random:#x}",
        address=address,
    )
