# Stand of the large-installation test on the controller itself: service configs (backed up and
# restored), the emulator process with its pty, journal reading and the MQTT watcher the
# scenarios wait on.

from __future__ import annotations

import asyncio
import json
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from e2e.large_installation.installation_profile import InstallationProfile
from e2e.large_installation.log_records import LogRecord, record_from_journal_json
from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher

STAND_DIR = Path("/run/e2e-large-installation")
SERIAL_PTY = STAND_DIR / "ttyLargeInstallation"
EMULATOR_LOG = STAND_DIR / "emulator.log"
EMULATOR_SCRIPT = Path(__file__).resolve().parent / "emulator.py"
SERIAL_CONFIG = Path("/etc/wb-mqtt-serial.conf")
DALI_CONFIG = Path("/etc/wb-mqtt-dali.conf")
BACKUP_SUFFIX = ".e2e_large_installation.bak"
DALI_SERVICE = "wb-mqtt-dali"
SERIAL_SERVICE = "wb-mqtt-serial"
PROCESS_START_TIMEOUT_S = 15.0
PROCESS_POLL_S = 0.2
PROCESS_STOP_TIMEOUT_S = 5.0


async def run_cmd(*args: str, check: bool = True) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"{' '.join(args)} exited with {proc.returncode}: {stderr.decode(errors='replace')}"
        )
    return stdout.decode(errors="replace")


async def systemctl(action: str, *units: str) -> None:
    await run_cmd("systemctl", action, *units)


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


async def active_services(*units: str) -> list[str]:
    """Those of `units` that are running now."""
    return [u for u in units if (await run_cmd("systemctl", "is-active", u, check=False)).strip() == "active"]


def build_dali_config(profile: InstallationProfile) -> dict:
    """Stand's wb-mqtt-dali config: the profile's one but for `debug`."""
    config = dict(profile.dali_config)
    # DEBUG logging changes wb-mqtt-dali's timing; the object ran without it.
    config.pop("debug", None)
    return config


def build_serial_config(profile: InstallationProfile) -> dict:
    return {"debug": False, "ports": [{**profile.serial_config, "path": str(SERIAL_PTY)}]}


class ConfigBackup:
    """Keeps the original files in memory and next to them until `restore`."""

    def __init__(self, paths: list[Path]) -> None:
        self._paths = paths
        self._originals: dict[Path, Optional[str]] = {}

    def take(self) -> None:
        for path in self._paths:
            backup = path.with_name(path.name + BACKUP_SUFFIX)
            if backup.exists():
                raise RuntimeError(
                    f"Leftover backup {backup}: a previous run did not restore {path}. "
                    f"Compare them, then restore (mv {backup} {path}) or delete the backup."
                )
        for path in self._paths:
            text = path.read_text(encoding="utf-8") if path.exists() else None
            self._originals[path] = text
            if text is not None:
                path.with_name(path.name + BACKUP_SUFFIX).write_text(text, encoding="utf-8")

    def restore(self) -> None:
        for path, text in self._originals.items():
            if text is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(text, encoding="utf-8")
            path.with_name(path.name + BACKUP_SUFFIX).unlink(missing_ok=True)


class EmulatorProcess:
    """The emulator in its own session, so that it can outlive the script."""

    def __init__(self, profile_path: str) -> None:
        self._profile_path = profile_path
        self._process: Optional[subprocess.Popen] = None

    async def start(self) -> None:
        STAND_DIR.mkdir(parents=True, exist_ok=True)
        SERIAL_PTY.unlink(missing_ok=True)
        with EMULATOR_LOG.open("a", encoding="utf-8") as emulator_log:
            self._process = subprocess.Popen(  # pylint: disable=consider-using-with
                [
                    sys.executable,
                    str(EMULATOR_SCRIPT),
                    "--profile",
                    self._profile_path,
                    "--port",
                    str(SERIAL_PTY),
                ],
                stdout=emulator_log,
                stderr=emulator_log,
                start_new_session=True,
            )
        deadline = time.monotonic() + PROCESS_START_TIMEOUT_S
        while not SERIAL_PTY.exists():
            self.check_alive()
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"Emulator not ready within {PROCESS_START_TIMEOUT_S:.0f} s; see {EMULATOR_LOG}"
                )
            await asyncio.sleep(PROCESS_POLL_S)

    def check_alive(self) -> None:
        if self._process is None or self._process.poll() is not None:
            code = None if self._process is None else self._process.returncode
            raise RuntimeError(f"Emulator is not running (exit code {code}); see {EMULATOR_LOG}")

    def stop(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.send_signal(signal.SIGTERM)
            try:
                self._process.wait(PROCESS_STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                self._process.kill()

    @property
    def pid(self) -> Optional[int]:
        return None if self._process is None else self._process.pid


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


def install_stand_files(dali_config: dict, serial_config: dict) -> None:
    DALI_CONFIG.write_text(json.dumps(dali_config, indent=2) + "\n", encoding="utf-8")
    SERIAL_CONFIG.write_text(json.dumps(serial_config, indent=2) + "\n", encoding="utf-8")
