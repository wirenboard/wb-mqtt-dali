from dataclasses import dataclass

from dali.address import GearShort
from dali.sequences import QueryDeviceTypes

from .common_gear_parameters import CommonParameters
from .type1_parameters import Type1Parameters
from .type4_parameters import Type4Parameters
from .type5_parameters import Type5Parameters
from .type6_parameters import Type6Parameters
from .type7_parameters import Type7Parameters
from .type17_parameters import Type17Parameters
from .type20_parameters import Type20Parameters
from .utils import merge_json_schemas
from .wbdali import WBDALIDriver


@dataclass
class DaliDeviceAddress:
    short: int
    random: int


# Type 0 fluorescent lamp ballast has no custom parameters
# Type 1 type1_parameters.py
# Type 2 Discharge lamps has no custom parameters
# Type 3 Low voltage halogen lamps has no custom parameters
# Type 4 type4_parameters.py
# Type 5 type5_parameters.py
# Type 6 type6_parameters.py
# Type 7 type7_parameters.py
# Type 8 Colour control gear
# TODO: implement
# Type 9 Sequencer
# TODO: implement
# Type 15 Load referencing has no custom parameters
# Type 16 Thermal gear protection has no custom parameters, only reset command
# Type 17 type17_parameters.py
# Type 20 type20_parameters.py
# Type 21 Thermal lamp protection has no custom parameters
# Type 22 Non-replaceable lamp source has no custom parameters
# Type 49 Integrated power supply has no custom parameters
# Type 51 Energy reporting device has no custom parameters


class DaliDevice:

    def __init__(self, uid: str, name: str, address: DaliDeviceAddress) -> None:
        self.uid = uid
        self.name = name
        self.address = address
        self.types: list[int] = []
        self.params: dict = {}
        self.schema: dict = {}
        self._gear_type_params = {
            1: Type1Parameters(),
            4: Type4Parameters(),
            5: Type5Parameters(),
            6: Type6Parameters(),
            7: Type7Parameters(),
            17: Type17Parameters(),
            20: Type20Parameters(),
        }

    async def load_info(self, driver: WBDALIDriver, force_reload: bool = False) -> None:
        if self.params and not force_reload:
            return
        short_addr = GearShort(self.address.short)
        types = await driver.run_sequence(QueryDeviceTypes(short_addr))
        if types is None:
            raise RuntimeError(
                f"Device at short address {short_addr.address} did not respond to QueryDeviceTypes"
            )
        self.types = types
        self.params = {
            "short_address": self.address.short,
            "random_address": self.address.random,
            "types": self.types,
        }
        self.params.update(await CommonParameters().read(driver, short_addr))
        for gear_type in self.types:
            param_handler = self._gear_type_params.get(gear_type)
            if param_handler is not None:
                type_params = await param_handler.read(driver, short_addr)
                self.params.update(type_params)

        common_params = CommonParameters()
        self.schema = await common_params.get_schema()
        for gear_type in self.types:
            param_handler = self._gear_type_params.get(gear_type)
            if param_handler is not None:
                type_schema = await param_handler.get_schema(driver, short_addr)
                if type_schema is not None:
                    merge_json_schemas(self.schema, type_schema)


def make_device(bus_uid: str, address: DaliDeviceAddress) -> DaliDevice:
    return DaliDevice(
        uid=f"{bus_uid}_{address.short}",
        name=f"Dev {address.short}:{address.random:#x}",
        address=address,
    )
