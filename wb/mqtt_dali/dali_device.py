import asyncio
from dataclasses import dataclass
from enum import IntEnum

import jsonschema
from dali.address import GearShort
from dali.sequences import QueryDeviceTypes

from .common_gear_parameters import CommonParameters
from .extended_gear_parameters import TypeParameters
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


class DaliDeviceType(IntEnum):
    FLUORESCENT_LAMP_BALLAST = 0
    SELF_CONTAINED_EMERGENCY_LIGHTING = 1
    DISCHARGE_LAMPS = 2
    LOW_VOLTAGE_HALOGEN_LAMPS = 3
    SUPPLY_VOLTAGE_CONTROLLER_FOR_INCANDESCENT_LAMPS = 4
    CONVERSION_FROM_DIGITAL_SIGNAL_INTO_DC_VOLTAGE = 5
    LED_MODULES = 6
    SWITCHING_FUNCTION = 7
    COLOUR_CONTROL = 8
    SEQUENCER = 9
    LOAD_REFERENCING = 15
    THERMAL_GEAR_PROTECTION = 16
    DIMMING_CURVE_SELECTION = 17
    DEMAND_RESPONSE = 20
    THERMAL_LAMP_PROTECTION = 21
    NON_REPLACEABLE_LAMP_SOURCE = 22
    INTEGRATED_POWER_SUPPLY = 49
    ENERGY_REPORTING_DEVICE = 51


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
        schemas = [param_handler.get_schema() for param_handler in parameter_handlers]
        for type_schema in schemas:
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

    def _get_parameters(self, types: list[int]) -> list[TypeParameters]:
        # Colour control has own scenes, power on level and system failure level parameters,
        # so exclude common alternatives
        exclude_scenes_and_levels = DaliDeviceType.COLOUR_CONTROL.value in types
        res: list[TypeParameters] = [CommonParameters(exclude_scenes_and_levels)]
        gear_type_params = {
            DaliDeviceType.SELF_CONTAINED_EMERGENCY_LIGHTING: Type1Parameters(),
            DaliDeviceType.SUPPLY_VOLTAGE_CONTROLLER_FOR_INCANDESCENT_LAMPS: Type4Parameters(),
            DaliDeviceType.CONVERSION_FROM_DIGITAL_SIGNAL_INTO_DC_VOLTAGE: Type5Parameters(),
            DaliDeviceType.LED_MODULES: Type6Parameters(),
            DaliDeviceType.SWITCHING_FUNCTION: Type7Parameters(),
            DaliDeviceType.COLOUR_CONTROL: Type8Parameters(),
            DaliDeviceType.DIMMING_CURVE_SELECTION: Type17Parameters(),
            DaliDeviceType.DEMAND_RESPONSE: Type20Parameters(),
        }
        for gear_type in types:
            try:
                res.append(gear_type_params[DaliDeviceType(gear_type)])
            except (ValueError, KeyError):
                continue
        return res


def make_dali_device(bus_uid: str, address: DaliDeviceAddress) -> DaliDevice:
    return DaliDevice(
        uid=f"{bus_uid}_{address.short}",
        name=f"DALI {address.short}:{address.random:#x}",
        address=address,
    )
