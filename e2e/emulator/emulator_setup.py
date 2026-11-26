# The emulator on the controller itself: service configs (backed up and restored), the emulator
# process with its pty and the wb-mqtt-serial config that points the gateway's port at it.

from __future__ import annotations

import asyncio
import json
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from e2e.emulator.installation_profile import InstallationProfile

EMULATOR_DIR = Path("/run/e2e-emulator")
SERIAL_PTY = EMULATOR_DIR / "ttyEmulator"
EMULATOR_LOG = EMULATOR_DIR / "emulator.log"
EMULATOR_SCRIPT = Path(__file__).resolve().parent / "emulator.py"
SERIAL_CONFIG = Path("/etc/wb-mqtt-serial.conf")
BACKUP_SUFFIX = ".e2e_emulator.bak"
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


async def active_services(*units: str) -> list[str]:
    """Those of `units` that are running now."""
    return [u for u in units if (await run_cmd("systemctl", "is-active", u, check=False)).strip() == "active"]


def build_serial_config(profile: InstallationProfile) -> dict:
    return {"debug": False, "ports": [{**profile.serial_config, "path": str(SERIAL_PTY)}]}


def install_serial_config(serial_config: dict) -> None:
    SERIAL_CONFIG.write_text(json.dumps(serial_config, indent=2) + "\n", encoding="utf-8")


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
        EMULATOR_DIR.mkdir(parents=True, exist_ok=True)
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
