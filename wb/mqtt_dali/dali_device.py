import asyncio
from dataclasses import dataclass

import jsonschema
from dali.address import GearShort
from dali.sequences import QueryDeviceTypes

from .common_gear_parameters import CommonParameters
from .type1_parameters import Type1Parameters
from .type4_parameters import Type4Parameters
from .type5_parameters import Type5Parameters
from .type6_parameters import Type6Parameters
from .type7_parameters import Type7Parameters
from .type8_parameters import Type8Parameters
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
# Type 8 type8_parameters.py
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
        self._parameter_handlers: list = []

    async def load_info(self, driver: WBDALIDriver, force_reload: bool = False) -> None:
        if self.params and not force_reload:
            return
        short_addr = GearShort(self.address.short)
        types = await driver.run_sequence(QueryDeviceTypes(short_addr))
        if types is None:
            raise RuntimeError(
                f"Device at short address {short_addr.address} did not respond to QueryDeviceTypes"
            )
        parameter_handlers = self._get_parameters(types)
        schema = {}
        params = {
            "short_address": self.address.short,
            "random_address": self.address.random,
            "types": types,
        }
        awaitables = [param_handler.read(driver, short_addr) for param_handler in parameter_handlers]
        results_iterable = iter(await asyncio.gather(*awaitables))
        for _ in parameter_handlers:
            type_params = next(results_iterable)
            params.update(type_params)
        awaitables = [param_handler.get_schema(driver, short_addr) for param_handler in parameter_handlers]
        results_iterable = iter(await asyncio.gather(*awaitables))
        for _ in parameter_handlers:
            type_schema = next(results_iterable)
            if type_schema is not None:
                merge_json_schemas(schema, type_schema)
        self._parameter_handlers = parameter_handlers
        self.params = params
        self.schema = schema
        self.types = types

    async def apply_parameters(self, driver: WBDALIDriver, new_values: dict) -> None:
        if not self.params:
            await self.load_info(driver)
        jsonschema.validate(
            instance=new_values, schema=self.schema, format_checker=jsonschema.draft4_format_checker
        )
        short_addr = GearShort(self.address.short)
        updated_parameters = {}
        for param_handler in self._parameter_handlers:
            updated_parameters.update(await param_handler.write(driver, short_addr, new_values))
        self.params.update(updated_parameters)

    def _get_parameters(self, types: list[int]) -> list:
        # Type 8 Colour control has own scenes parameter, so exclude common scenes parameter
        exclude_scenes = 8 in types
        res = [CommonParameters(exclude_scenes)]
        gear_type_params = {
            1: Type1Parameters(),
            4: Type4Parameters(),
            5: Type5Parameters(),
            6: Type6Parameters(),
            7: Type7Parameters(),
            8: Type8Parameters(),
            17: Type17Parameters(),
            20: Type20Parameters(),
        }
        for gear_type in types:
            param_handler = gear_type_params.get(gear_type)
            if param_handler is not None:
                res.append(param_handler)
        return res


def make_device(bus_uid: str, address: DaliDeviceAddress) -> DaliDevice:
    return DaliDevice(
        uid=f"{bus_uid}_{address.short}",
        name=f"Dev {address.short}:{address.random:#x}",
        address=address,
    )
