# Type 8

import enum
from dataclasses import dataclass

MASK_2BYTES = 65535


class ColourComponent(enum.Enum):
    RED = "red"
    GREEN = "green"
    BLUE = "blue"
    WHITE = "white"
    AMBER = "amber"
    FREE_COLOUR = "free_colour"
    COLOUR_TEMPERATURE = "tc"
    PRIMARY_N0 = "primary_n0"
    PRIMARY_N1 = "primary_n1"
    PRIMARY_N2 = "primary_n2"
    PRIMARY_N3 = "primary_n3"
    PRIMARY_N4 = "primary_n4"
    PRIMARY_N5 = "primary_n5"
    X_COORDINATE = "x_coordinate"
    Y_COORDINATE = "y_coordinate"


@dataclass
class Type8Limits:
    tc_min_mirek: int
    tc_max_mirek: int
