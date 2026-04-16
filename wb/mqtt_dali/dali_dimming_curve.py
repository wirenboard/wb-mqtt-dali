from enum import IntEnum


def logarithmic_dimming_curve(level: int) -> float:
    """IEC 62386-102:2022, section 9.3: light_output = 10^((level-1)/253*3 - 1) %"""
    return 10 ** ((level - 1) / 253 * 3 - 1)


class DimmingCurveType(IntEnum):
    LOGARITHMIC = 0
    LINEAR = 1


class DimmingCurveState:

    def __init__(self) -> None:
        self.curve_type = DimmingCurveType.LOGARITHMIC

    def get_level(self, value_from_register: int) -> float:
        if value_from_register < 1:
            return 0.0
        if value_from_register >= 254:
            return 100.0
        if self.curve_type == DimmingCurveType.LINEAR:
            return round(value_from_register * 100.0 / 254.0, 3)
        return round(logarithmic_dimming_curve(value_from_register), 3)

    def get_raw_value(self, level: float) -> int:
        if level <= 0.0:
            return 0
        if level >= 100.0:
            return 254
        if self.curve_type == DimmingCurveType.LINEAR:
            return round(level * 254.0 / 100.0)
        # Inverse of logarithmic_dimming_curve
        return round(10 ** (level / 100 - 1) * 253 + 1)
