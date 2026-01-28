from typing import Optional

from .dali_device import DaliDeviceAddress
from .wbdali import WBDALIDriver


class Dali2Device:

    def __init__(self, uid: str, name: str, address: DaliDeviceAddress) -> None:
        self.uid = uid
        self.name = name
        self.address = address
        self.params: Optional[dict] = None

    async def load_info(self, driver: WBDALIDriver, force_reload: bool = False) -> None:
        if self.params and not force_reload:
            return

    async def apply_parameters(self, driver: WBDALIDriver, new_values: dict) -> None:
        if not self.params:
            await self.load_info(driver)

    def _get_parameters(self) -> list:
        return []


def make_dali2_device(bus_uid: str, address: DaliDeviceAddress) -> Dali2Device:
    return Dali2Device(
        uid=f"{bus_uid}_dali2_{address.short}",
        name=f"DALI-2 {address.short}:{address.random:#x}",
        address=address,
    )
