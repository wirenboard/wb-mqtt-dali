import uuid
from dataclasses import dataclass
from typing import Optional

from .wbdali import WBDALIDriver


@dataclass
class DaliDeviceAddress:
    short: int
    random: int


class DaliDeviceBase:

    def __init__(
        self,
        address: DaliDeviceAddress,
        bus_id: str,
        default_name_prefix: str,
        default_mqtt_id_part: str,
        mqtt_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> None:
        self.uid = str(uuid.uuid4())
        self.address = address
        self.params: dict = {}
        self.schema: dict = {}

        self._bus_id = bus_id
        self._default_name_prefix = default_name_prefix
        self._default_mqtt_id_part = default_mqtt_id_part
        if mqtt_id != self.default_mqtt_id:
            self._mqtt_id = mqtt_id
        if name != self.default_name:
            self._name = name
        self._parameter_handlers: list = []

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
        raise NotImplementedError()

    async def apply_parameters(self, driver: WBDALIDriver, new_values: dict) -> None:
        raise NotImplementedError()

    # Must be implemented by subclasses
    async def _set_short_address(self, driver: WBDALIDriver, new_short_address: int) -> None:
        raise NotImplementedError()

    def _get_common_parameters(self) -> dict:
        return {
            "short_address": self.address.short,
            "random_address": self.address.random,
            "name": self.name,
            "mqtt_id": self.mqtt_id,
        }

    async def _apply_common_parameters(self, driver: WBDALIDriver, new_values: dict) -> None:
        new_short_address = new_values.get("short_address", self.address.short)
        if new_short_address != self.address.short:
            await self._set_short_address(driver, new_short_address)
            self.address.short = new_short_address

        self.name = new_values.get("name", self.name)
        self.mqtt_id = new_values.get("mqtt_id", self.mqtt_id)
        self.params["short_address"] = self.address.short
        self.params["name"] = self.name
        self.params["mqtt_id"] = self.mqtt_id
