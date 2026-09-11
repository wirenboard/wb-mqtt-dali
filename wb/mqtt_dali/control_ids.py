"""Control ids named by more than one site.

Defined here (not on the controls) so every site that names a control shares one spelling; the
state<->setpoint pairing lives in virtual_devices.py, which needs it for the group card.
"""

ACTUAL_LEVEL = "actual_level"
WANTED_LEVEL = "wanted_level"
DAPC = "dapc"
LAST_ACTED = "last_acted"

CURRENT_RGB = "current_rgb"
SET_RGB = "set_rgb"

CURRENT_WHITE = "current_white"
SET_WHITE = "set_white"

CURRENT_COLOUR_TEMPERATURE = "current_colour_temperature"
SET_COLOUR_TEMPERATURE = "set_colour_temperature"

CURRENT_X_COORDINATE = "current_x_coordinate"
SET_X_COORDINATE = "set_x_coordinate"

CURRENT_Y_COORDINATE = "current_y_coordinate"
SET_Y_COORDINATE = "set_y_coordinate"

# Formatted with the primary index at the construction loop and the pairing table.
CURRENT_PRIMARY_N = "current_primary_n{}"
SET_PRIMARY_N = "set_primary_n{}"
PRIMARY_N_MAX = 6
