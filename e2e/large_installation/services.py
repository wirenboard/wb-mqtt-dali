# Services of the large-installation test: wb-mqtt-dali's config from the profile, restarts and
# journal records of both services and the MQTT watcher the scenarios wait on.

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from e2e.emulator.emulator_setup import run_cmd
from e2e.emulator.installation_profile import InstallationProfile
from e2e.large_installation.log_records import LogRecord, record_from_journal_json
from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher

DALI_CONFIG = Path("/etc/wb-mqtt-dali.conf")


async def service_restarts(*units: str) -> dict[str, int]:
    """systemd's `NRestarts` of each unit."""
    restarts = {}
    for unit in units:
        output = await run_cmd("systemctl", "show", "-p", "NRestarts", "--value", unit)
        restarts[unit] = int(output.strip() or 0)
    return restarts


def restarted_services(before: dict[str, int], after: dict[str, int]) -> list[str]:
    """One message per unit that systemd restarted between the two snapshots."""
    return [
        f"{unit} restarted during the iteration: NRestarts {count} -> {after[unit]}"
        for unit, count in before.items()
        if unit in after and after[unit] != count
    ]


def build_dali_config(profile: InstallationProfile) -> dict:
    """The installed wb-mqtt-dali config: the profile's one but for `debug`."""
    config = dict(profile.dali_config)
    # DEBUG logging changes wb-mqtt-dali's timing; the object ran without it.
    config.pop("debug", None)
    return config


def install_dali_config(dali_config: dict) -> None:
    DALI_CONFIG.write_text(json.dumps(dali_config, indent=2) + "\n", encoding="utf-8")


async def read_journal(identifier: str, since: float) -> list[LogRecord]:
    """Journal records of one syslog identifier since a wall-clock time."""
    output = await run_cmd(
        "journalctl", "-o", "json", "--no-pager", f"--since=@{since:.6f}", f"SYSLOG_IDENTIFIER={identifier}"
    )
    return [record_from_journal_json(line) for line in output.splitlines() if line.strip()]


@dataclass
class MetaWatcher:
    """Times of the first live `/meta` publication of each device after `start`."""

    seen: dict[str, float] = field(default_factory=dict)

    async def start(self, dispatcher: MQTTDispatcher) -> None:
        await dispatcher.subscribe("/devices/+/meta", self.handle)

    async def stop(self, dispatcher: MQTTDispatcher) -> None:
        await dispatcher.unsubscribe("/devices/+/meta", self.handle)

    def handle(self, message) -> None:
        if message.retain or not message.payload:
            return
        self.seen.setdefault(str(message.topic).split("/")[2], time.time())


def full_start_time(seen: dict[str, float], device_ids: set[str]) -> Optional[float]:
    """When the last of `device_ids` published its `/meta`, None while any is still missing."""
    if not device_ids <= set(seen):
        return None
    return max(seen[i] for i in device_ids)
