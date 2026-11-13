#!/usr/bin/env python3
# Emulator process of the large-installation stand: the RS-485 line on a pty with the WB-DALI
# gateway (its DALI buses and devices) and the neighbour devices from the installation profile.

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path

# Puts the checkout root on sys.path when run as a script; pylint does not see the script directory.
import checkout_path  # pylint: disable=import-error,unused-import

from e2e.large_installation.dali_fakes import ControlDevice, DaliBus, EmulatedGear
from e2e.large_installation.installation_profile import (
    GATEWAY_DEVICE_TYPE,
    InstallationProfile,
    TimingProfile,
    load_profile,
)
from e2e.large_installation.modbus_rtu import ModbusSlave, RtuLine
from e2e.large_installation.template_device import (
    INSTALLED_TEMPLATES_DIR,
    TemplateDevice,
    load_installed_template,
)
from e2e.large_installation.wbdali_gateway import EmulatedWbDali

GATEWAY_FW_VERSION = "1.2.3"
# wb-mqtt-serial looks the gateway's firmware up in the release by this signature.
GATEWAY_FW_SIGNATURE = "dali3G15"

log = logging.getLogger("emulator")


class Emulator:  # pylint: disable=too-few-public-methods
    def __init__(self, profile: InstallationProfile, templates_dir: Path) -> None:
        dali_buses = [
            DaliBus([EmulatedGear(g) for g in bus.gear] + [ControlDevice(d) for d in bus.control_devices])
            for bus in profile.buses
        ]
        self.gateway = EmulatedWbDali(
            profile.gateway_slave_id,
            replace(
                load_installed_template(GATEWAY_DEVICE_TYPE, templates_dir), signature=GATEWAY_FW_SIGNATURE
            ),
            TimingProfile(),
            dali_buses,
            GATEWAY_FW_VERSION,
        )
        templates = {
            template: load_installed_template(template, templates_dir)
            for template in {n.template for n in profile.neighbours}
        }
        slaves: list[ModbusSlave] = [self.gateway]
        slaves += [TemplateDevice(n.slave_id, templates[n.template], 0.0) for n in profile.neighbours]
        self.line = RtuLine(slaves, profile.port)

    async def serve(self, fd: int) -> None:
        await asyncio.gather(self.line.serve(fd), self.gateway.run())


async def run(args: argparse.Namespace) -> None:
    profile = load_profile(args.profile)
    emulator = Emulator(profile, Path(args.templates))
    master, slave = os.openpty()
    # The slave end stays open for the emulator's life, so wb-mqtt-serial can close and reopen the port.
    link = Path(args.port)
    link.unlink(missing_ok=True)
    link.symlink_to(os.ttyname(slave))
    log.info(
        "Serving %s: gateway %d, %d neighbours", args.port, profile.gateway_slave_id, len(profile.neighbours)
    )
    try:
        await emulator.serve(master)
    finally:
        os.close(master)
        os.close(slave)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="RS-485 / WB-DALI emulator of the large-installation stand")
    parser.add_argument("--profile", default="site", help="Profile name or directory")
    parser.add_argument("--port", required=True, help="Symlink to create for the serial side of the pty")
    parser.add_argument("--templates", default=str(INSTALLED_TEMPLATES_DIR), help="wb-mqtt-serial templates")
    args = parser.parse_args(argv[1:])
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
