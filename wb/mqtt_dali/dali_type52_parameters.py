# Type 52 Diagnostics and maintenance (IEC 62386-253:2023)

from dali.exceptions import MemoryLocationNotImplemented, ResponseError
from dali.memory.diagnostics import (
    BANK_205,
    BANK_206,
    ControlGearExternalSupplyOvervoltage,
    ControlGearExternalSupplyOvervoltageCounter,
    ControlGearExternalSupplyUndervoltage,
    ControlGearExternalSupplyUndervoltageCounter,
    ControlGearExternalSupplyVoltage,
    ControlGearExternalSupplyVoltageFrequency,
    ControlGearOperatingTime,
    ControlGearOutputCurrentPercent,
    ControlGearOutputPowerLimitation,
    ControlGearOutputPowerLimitationCounter,
    ControlGearOverallFailureCondition,
    ControlGearOverallFailureConditionCounter,
    ControlGearPowerFactor,
    ControlGearStartCounter,
    ControlGearTemperature,
    ControlGearThermalDerating,
    ControlGearThermalDeratingCounter,
    ControlGearThermalShutdown,
    ControlGearThermalShutdownCounter,
    LightSourceCurrent,
    LightSourceOnTime,
    LightSourceOnTimeResettable,
    LightSourceOpenCircuit,
    LightSourceOpenCircuitCounter,
    LightSourceOverallFailureCondition,
    LightSourceOverallFailureConditionCounter,
    LightSourceShortCircuit,
    LightSourceShortCircuitCounter,
    LightSourceStartCounter,
    LightSourceStartCounterResettable,
    LightSourceTemperature,
    LightSourceThermalDerating,
    LightSourceThermalDeratingCounter,
    LightSourceThermalShutdown,
    LightSourceThermalShutdownCounter,
    LightSourceVoltage,
)
from dali.memory.location import FlagValue
from dali.memory.maintenance import (
    BANK_207,
    InternalControlGearReferenceTemperature,
    RatedMedianUsefulLifeOfLuminaire,
    RatedMedianUsefulLightSourceStarts,
)

from .common_dali_device import read_memory_bank
from .dali_compat import DaliCommandsCompatibilityLayer
from .dali_parameters import TypeParameters
from .wbdali_utils import WBDALIDriver


def _get(data: dict, cls):
    """Return the value from a memory bank data dict, or None if unavailable."""
    v = data.get(cls)
    if v is None or isinstance(v, FlagValue):
        return None
    return v


def _to_float(v) -> float:
    return round(float(v), 6)


def _seconds_to_dhms(total: int) -> str:
    d, rem = divmod(total, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    hms = f"{h:02}:{m:02}:{s:02}"
    return f"{d}d {hms}" if d else hms


class Type52Parameters(TypeParameters):

    def __init__(self) -> None:
        super().__init__()
        self._compat = DaliCommandsCompatibilityLayer()

    async def read(self, driver: WBDALIDriver, short_address: int) -> dict:
        cg: dict = {}
        ls: dict = {}
        try:
            data = await driver.run_sequence(read_memory_bank(BANK_205, short_address, self._compat))
            cg.update(self._extract_bank205(data))
        except (MemoryLocationNotImplemented, ResponseError) as e:
            raise RuntimeError(f"Failed to read DT52 memory bank 205: {e}") from e
        try:
            data = await driver.run_sequence(read_memory_bank(BANK_206, short_address, self._compat))
            ls.update(self._extract_bank206(data))
        except (MemoryLocationNotImplemented, ResponseError) as e:
            raise RuntimeError(f"Failed to read DT52 memory bank 206: {e}") from e
        try:
            data = await driver.run_sequence(read_memory_bank(BANK_207, short_address, self._compat))
            v = _get(data, InternalControlGearReferenceTemperature)
            if v is not None:
                cg["reference_temperature_c"] = int(v)
            v = _get(data, RatedMedianUsefulLifeOfLuminaire)
            if v is not None:
                ls["rated_median_useful_life_h"] = int(v)
            v = _get(data, RatedMedianUsefulLightSourceStarts)
            if v is not None:
                ls["rated_median_useful_starts"] = int(v)
        except (MemoryLocationNotImplemented, ResponseError) as e:
            raise RuntimeError(f"Failed to read DT52 memory bank 207: {e}") from e
        res = {}
        if cg:
            res["type52_cg"] = cg
        if ls:
            res["type52_ls"] = ls
        return res

    def _extract_bank205(self, data: dict) -> dict:
        res = {}
        v = _get(data, ControlGearOperatingTime)
        if v is not None:
            res["operating_time_s"] = _seconds_to_dhms(int(v))
        for key, cls in [
            ("start_counter", ControlGearStartCounter),
            ("supply_frequency_hz", ControlGearExternalSupplyVoltageFrequency),
            ("overall_failure_counter", ControlGearOverallFailureConditionCounter),
            ("undervoltage_counter", ControlGearExternalSupplyUndervoltageCounter),
            ("overvoltage_counter", ControlGearExternalSupplyOvervoltageCounter),
            ("power_limitation_counter", ControlGearOutputPowerLimitationCounter),
            ("thermal_derating_counter", ControlGearThermalDeratingCounter),
            ("thermal_shutdown_counter", ControlGearThermalShutdownCounter),
            ("temperature_c", ControlGearTemperature),
            ("output_current_pct", ControlGearOutputCurrentPercent),
        ]:
            v = _get(data, cls)
            if v is not None:
                res[key] = int(v)
        for key, cls in [
            ("supply_voltage_v", ControlGearExternalSupplyVoltage),
            ("power_factor", ControlGearPowerFactor),
        ]:
            v = _get(data, cls)
            if v is not None:
                res[key] = _to_float(v)
        for key, cls in [
            ("overall_failure", ControlGearOverallFailureCondition),
            ("supply_undervoltage", ControlGearExternalSupplyUndervoltage),
            ("supply_overvoltage", ControlGearExternalSupplyOvervoltage),
            ("output_power_limitation", ControlGearOutputPowerLimitation),
            ("thermal_derating", ControlGearThermalDerating),
            ("thermal_shutdown", ControlGearThermalShutdown),
        ]:
            v = _get(data, cls)
            if v is not None:
                res[key] = bool(v)
        return res

    def _extract_bank206(self, data: dict) -> dict:
        res = {}
        for key, cls in [
            ("start_counter", LightSourceStartCounter),
            ("start_counter_resettable", LightSourceStartCounterResettable),
            ("overall_failure_counter", LightSourceOverallFailureConditionCounter),
            ("short_circuit_counter", LightSourceShortCircuitCounter),
            ("open_circuit_counter", LightSourceOpenCircuitCounter),
            ("thermal_derating_counter", LightSourceThermalDeratingCounter),
            ("thermal_shutdown_counter", LightSourceThermalShutdownCounter),
            ("temperature_c", LightSourceTemperature),
        ]:
            v = _get(data, cls)
            if v is not None:
                res[key] = int(v)
        for key, cls in [
            ("on_time_s", LightSourceOnTime),
            ("on_time_resettable_s", LightSourceOnTimeResettable),
        ]:
            v = _get(data, cls)
            if v is not None:
                res[key] = _seconds_to_dhms(int(v))
        for key, cls in [
            ("voltage_v", LightSourceVoltage),
            ("current_a", LightSourceCurrent),
        ]:
            v = _get(data, cls)
            if v is not None:
                res[key] = _to_float(v)
        for key, cls in [
            ("overall_failure", LightSourceOverallFailureCondition),
            ("short_circuit", LightSourceShortCircuit),
            ("open_circuit", LightSourceOpenCircuit),
            ("thermal_derating", LightSourceThermalDerating),
            ("thermal_shutdown", LightSourceThermalShutdown),
        ]:
            v = _get(data, cls)
            if v is not None:
                res[key] = bool(v)
        return res

    def get_schema(self) -> dict:
        ro = {"wb": {"read_only": True}}
        ro_6 = {"wb": {"read_only": True}, "grid_columns": 6}
        return {
            "properties": {
                "type52_cg": {
                    "type": "object",
                    "title": "Control gear",
                    "propertyOrder": 500,
                    "options": ro_6,
                    "format": "card",
                    "properties": {
                        "operating_time_s": {
                            "type": "string",
                            "title": "Operating time",
                            "options": ro_6,
                            "propertyOrder": 1,
                        },
                        "start_counter": {
                            "type": "integer",
                            "title": "Start counter",
                            "options": ro_6,
                            "propertyOrder": 2,
                        },
                        "supply_voltage_v": {
                            "type": "number",
                            "title": "Supply voltage (V)",
                            "options": ro_6,
                            "propertyOrder": 3,
                        },
                        "supply_frequency_hz": {
                            "type": "integer",
                            "title": "Supply frequency (Hz)",
                            "options": ro_6,
                            "propertyOrder": 4,
                        },
                        "power_factor": {
                            "type": "number",
                            "title": "Power factor",
                            "options": ro_6,
                            "propertyOrder": 5,
                        },
                        "output_current_pct": {
                            "type": "integer",
                            "title": "Output current (%)",
                            "options": ro_6,
                            "propertyOrder": 6,
                        },
                        "temperature_c": {
                            "type": "integer",
                            "title": "Temperature (°C)",
                            "options": ro,
                            "propertyOrder": 7,
                        },
                        "reference_temperature_c": {
                            "type": "integer",
                            "title": "Reference temperature (°C)",
                            "options": ro,
                            "propertyOrder": 8,
                        },
                        "overall_failure": {
                            "type": "boolean",
                            "title": "Overall failure",
                            "format": "switch",
                            "options": ro_6,
                            "propertyOrder": 9,
                        },
                        "overall_failure_counter": {
                            "type": "integer",
                            "title": "Overall failure counter",
                            "options": ro_6,
                            "propertyOrder": 10,
                        },
                        "supply_undervoltage": {
                            "type": "boolean",
                            "title": "Supply undervoltage",
                            "format": "switch",
                            "options": ro_6,
                            "propertyOrder": 11,
                        },
                        "undervoltage_counter": {
                            "type": "integer",
                            "title": "Undervoltage counter",
                            "options": ro_6,
                            "propertyOrder": 12,
                        },
                        "supply_overvoltage": {
                            "type": "boolean",
                            "title": "Supply overvoltage",
                            "format": "switch",
                            "options": ro_6,
                            "propertyOrder": 13,
                        },
                        "overvoltage_counter": {
                            "type": "integer",
                            "title": "Overvoltage counter",
                            "options": ro_6,
                            "propertyOrder": 14,
                        },
                        "output_power_limitation": {
                            "type": "boolean",
                            "title": "Output power limitation",
                            "format": "switch",
                            "options": ro_6,
                            "propertyOrder": 15,
                        },
                        "power_limitation_counter": {
                            "type": "integer",
                            "title": "Power limitation counter",
                            "options": ro_6,
                            "propertyOrder": 16,
                        },
                        "thermal_derating": {
                            "type": "boolean",
                            "title": "Thermal derating",
                            "format": "switch",
                            "options": ro_6,
                            "propertyOrder": 17,
                        },
                        "thermal_derating_counter": {
                            "type": "integer",
                            "title": "Thermal derating counter",
                            "options": ro_6,
                            "propertyOrder": 18,
                        },
                        "thermal_shutdown": {
                            "type": "boolean",
                            "title": "Thermal shutdown",
                            "format": "switch",
                            "options": ro_6,
                            "propertyOrder": 19,
                        },
                        "thermal_shutdown_counter": {
                            "type": "integer",
                            "title": "Thermal shutdown counter",
                            "options": ro_6,
                            "propertyOrder": 20,
                        },
                    },
                },
                "type52_ls": {
                    "type": "object",
                    "title": "Light source",
                    "propertyOrder": 530,
                    "options": ro_6,
                    "format": "card",
                    "properties": {
                        "start_counter": {
                            "type": "integer",
                            "title": "Start counter",
                            "options": ro_6,
                            "propertyOrder": 1,
                        },
                        "start_counter_resettable": {
                            "type": "integer",
                            "title": "Start counter (resettable)",
                            "options": ro_6,
                            "propertyOrder": 2,
                        },
                        "on_time_s": {
                            "type": "string",
                            "title": "On time",
                            "options": ro_6,
                            "propertyOrder": 3,
                        },
                        "on_time_resettable_s": {
                            "type": "string",
                            "title": "On time resettable",
                            "options": ro_6,
                            "propertyOrder": 4,
                        },
                        "voltage_v": {
                            "type": "number",
                            "title": "Voltage (V)",
                            "options": ro_6,
                            "propertyOrder": 5,
                        },
                        "current_a": {
                            "type": "number",
                            "title": "Current (A)",
                            "options": ro_6,
                            "propertyOrder": 6,
                        },
                        "temperature_c": {
                            "type": "integer",
                            "title": "Temperature (°C)",
                            "options": ro_6,
                            "propertyOrder": 7,
                        },
                        "rated_median_useful_life_h": {
                            "type": "integer",
                            "title": "Rated median useful life (1000h)",
                            "options": ro_6,
                            "propertyOrder": 8,
                        },
                        "rated_median_useful_starts": {
                            "type": "integer",
                            "title": "Rated median useful starts (×100)",
                            "options": ro_6,
                            "propertyOrder": 9,
                        },
                        "overall_failure": {
                            "type": "boolean",
                            "title": "Overall failure",
                            "format": "switch",
                            "options": ro_6,
                            "propertyOrder": 10,
                        },
                        "overall_failure_counter": {
                            "type": "integer",
                            "title": "Overall failure counter",
                            "options": ro_6,
                            "propertyOrder": 11,
                        },
                        "short_circuit": {
                            "type": "boolean",
                            "title": "Short circuit",
                            "format": "switch",
                            "options": ro_6,
                            "propertyOrder": 12,
                        },
                        "short_circuit_counter": {
                            "type": "integer",
                            "title": "Short circuit counter",
                            "options": ro_6,
                            "propertyOrder": 13,
                        },
                        "open_circuit": {
                            "type": "boolean",
                            "title": "Open circuit",
                            "format": "switch",
                            "options": ro_6,
                            "propertyOrder": 14,
                        },
                        "open_circuit_counter": {
                            "type": "integer",
                            "title": "Open circuit counter",
                            "options": ro_6,
                            "propertyOrder": 15,
                        },
                        "thermal_derating": {
                            "type": "boolean",
                            "title": "Thermal derating",
                            "format": "switch",
                            "options": ro_6,
                            "propertyOrder": 16,
                        },
                        "thermal_derating_counter": {
                            "type": "integer",
                            "title": "Thermal derating counter",
                            "options": ro_6,
                            "propertyOrder": 17,
                        },
                        "thermal_shutdown": {
                            "type": "boolean",
                            "title": "Thermal shutdown",
                            "format": "switch",
                            "options": ro_6,
                            "propertyOrder": 18,
                        },
                        "thermal_shutdown_counter": {
                            "type": "integer",
                            "title": "Thermal shutdown counter",
                            "options": ro_6,
                            "propertyOrder": 19,
                        },
                    },
                },
            },
            "translations": {
                "ru": {
                    "Control gear": "Устройство управления",
                    "Light source": "Источник света",
                    "Operating time": "Время работы",
                    "Start counter": "Счётчик запусков",
                    "Supply voltage (V)": "Напряжение питания (В)",
                    "Supply frequency (Hz)": "Частота питания (Гц)",
                    "Power factor": "Коэффициент мощности",
                    "Output current (%)": "Выходной ток (%)",
                    "Temperature (°C)": "Температура (°C)",
                    "Reference temperature (°C)": "Эталонная температура (°C)",
                    "Overall failure": "Общая неисправность",
                    "Overall failure counter": "Счётчик общих неисправностей",
                    "Supply undervoltage": "Пониженное напряжение питания",
                    "Undervoltage counter": "Счётчик пониженного напряжения",
                    "Supply overvoltage": "Повышенное напряжение питания",
                    "Overvoltage counter": "Счётчик повышенного напряжения",
                    "Output power limitation": "Ограничение мощности",
                    "Power limitation counter": "Счётчик ограничения мощности",
                    "Thermal derating": "Тепловая коррекция",
                    "Thermal derating counter": "Счётчик тепловой коррекции",
                    "Thermal shutdown": "Тепловое отключение",
                    "Thermal shutdown counter": "Счётчик теплового отключения",
                    "Start counter (resettable)": "Сбрасываемый счётчик запусков",
                    "On time": "Время работы",
                    "On time resettable": "Сбрасываемое время работы",
                    "Voltage (V)": "Напряжение (В)",
                    "Current (A)": "Ток (А)",
                    "Rated median useful life (1000h)": "Номинальный медианный ресурс (×1000 ч)",
                    "Rated median useful starts (×100)": "Номинальное количество включений (×100)",
                    "Short circuit": "Короткое замыкание",
                    "Short circuit counter": "Счётчик КЗ",
                    "Open circuit": "Обрыв цепи",
                    "Open circuit counter": "Счётчик обрывов",
                },
            },
        }
