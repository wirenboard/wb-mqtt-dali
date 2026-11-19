# Type 8

import enum

from .control_ids import PRIMARY_N_MAX
from .wbdali_utils import MASK, MASK_2BYTES


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


PRIMARY_N_BY_INDEX: dict[int, ColourComponent] = {
    index: ColourComponent(f"primary_n{index}") for index in range(PRIMARY_N_MAX)
}

# Component -> the raw "not available / leave unchanged" value (62386-209). Same byte both
# directions: our own filler for the fields a command does not set when we write it, the gear
# refusing to name the value when it answers it.
UNSET_RAW_VALUE: dict[ColourComponent, int] = {
    ColourComponent.RED: MASK,
    ColourComponent.GREEN: MASK,
    ColourComponent.BLUE: MASK,
    ColourComponent.WHITE: MASK,
    ColourComponent.AMBER: MASK,
    ColourComponent.FREE_COLOUR: MASK,
    ColourComponent.COLOUR_TEMPERATURE: MASK_2BYTES,
    ColourComponent.X_COORDINATE: MASK_2BYTES,
    ColourComponent.Y_COORDINATE: MASK_2BYTES,
    **{component: MASK_2BYTES for component in PRIMARY_N_BY_INDEX.values()},
}


def is_unset_component_value(component: ColourComponent, value: int) -> bool:
    """True when the raw value carries no colour information: it is a placeholder, not a reading."""
    # 62386-209 allows Tc only in 1..65534 mirek; a 0 is not a temperature and has no Kelvin.
    if component is ColourComponent.COLOUR_TEMPERATURE and value == 0:
        return True
    return UNSET_RAW_VALUE.get(component) == value
