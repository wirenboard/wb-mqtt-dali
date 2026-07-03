"""Background incremental read of static settings params.

Mirrors `PollScheduler`: an orchestration component the polling loop drives from its idle branch,
one `fetch` per idle iteration, round-robin across the fetch params of every initialized device.
Membership changes only through `add_device`/`remove_device` (wired into the controller's
init-success and device-removal paths) — `fetch_step` itself does no scanning. Unit-testable in
isolation with a fake driver and fake devices, no controller needed.
"""

import logging
from dataclasses import dataclass

from .common_dali_device import DaliDeviceBase
from .dali_common_parameters import MaxLevelParam, MinLevelParam, ScenesParam
from .dali_type7_parameters import (
    DownSwitchOffThresholdParam,
    DownSwitchOnThresholdParam,
    UpSwitchOffThresholdParam,
    UpSwitchOnThresholdParam,
)
from .dali_type8_parameters import ScenesSettings
from .settings import SettingsParamBase
from .wbdali import WBDALIDriver

# Static settings params worth pre-reading in the background: prediction inputs (SOFT-7086) that are
# not already read at device init. Light/runtime params use the base one-shot fetch and are not here;
# groups, dimming curve, TC limits and fade are read at init and excluded by design.
FETCH_PARAM_CLASSES: tuple[type[SettingsParamBase], ...] = (
    MaxLevelParam,
    MinLevelParam,
    ScenesParam,
    ScenesSettings,
    UpSwitchOnThresholdParam,
    UpSwitchOffThresholdParam,
    DownSwitchOnThresholdParam,
    DownSwitchOffThresholdParam,
)


@dataclass
class _FetchEntry:
    device: DaliDeviceBase
    param: SettingsParamBase


class SettingsFetchScheduler:
    def __init__(self) -> None:
        self._entries: list[_FetchEntry] = []
        self._cursor = 0

    def add_device(self, device: DaliDeviceBase) -> None:
        """Register the device's fetch params. Call when the device finishes initialization."""
        self.remove_device(device)
        for param in device.get_settings_parameter_handlers():
            if isinstance(param, FETCH_PARAM_CLASSES):
                self._entries.append(_FetchEntry(device, param))

    def remove_device(self, device: DaliDeviceBase) -> None:
        """Drop the device's fetch params. Call from every device-removal path."""
        self._entries = [e for e in self._entries if e.device is not device]

    def is_empty(self) -> bool:
        return not self._entries

    async def fetch_step(self, driver: WBDALIDriver, logger: logging.Logger) -> bool:
        """Run at most one `fetch`, round-robin. Returns whether a fetch was actually performed.

        A param that completes or raises leaves the set with no retry; a raised fetch is logged but
        never breaks the loop. Membership is maintained by add_device/remove_device, so this does no
        per-call scanning of the device list.
        """
        if not self._entries:
            self._cursor = 0
            return False

        idx = self._cursor % len(self._entries)
        entry = self._entries[idx]
        short = entry.device.address.short
        address = entry.device.dali_commands.getAddress(short)
        try:
            done = await entry.param.fetch(driver, address, logger)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # One param's failure must not stall the others; drop it like a completed one.
            logger.warning(
                'Background fetch of "%s" (short %s) failed, dropping: %s',
                entry.param.name.en,
                short,
                exc,
            )
            done = True

        if done:
            del self._entries[idx]
            self._cursor = idx
        else:
            self._cursor = idx + 1
        return True
