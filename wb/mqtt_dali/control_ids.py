"""Control ids referenced by event-sync mirroring/ownership.

Defined here (not on the controls) so the control-construction sites and the event-sync
coordinator share one spelling; the pairing/ownership relationships live only in
event_sync_coordinator.py.
"""

ACTUAL_LEVEL = "actual_level"
WANTED_LEVEL = "wanted_level"
DAPC = "dapc"

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

# Templates formatted with the primary index at both the construction loop and the
# coordinator mirror table; PRIMARY_N_MAX bounds that loop.
CURRENT_PRIMARY_N = "current_primary_n{}"
SET_PRIMARY_N = "set_primary_n{}"
PRIMARY_N_MAX = 6
