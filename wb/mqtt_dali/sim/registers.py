"""The WB-DALI register map, re-exported for the simulator's callers.

The map is production knowledge — the register link and the simulated module
both implement it — so it lives in :mod:`wb.mqtt_dali.wbdali_registers`.
"""

from ..wbdali_registers import *  # noqa: F401,F403  pylint: disable=wildcard-import,unused-wildcard-import
