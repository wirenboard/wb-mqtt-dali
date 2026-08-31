"""A simulated DALI bus: control gear and control devices that answer real frames.

The gear and device models come from python-dali's own test fakes
(``dali.tests.fakes``), which implement the parts of IEC 62386 that matter here:
the commissioning state machine (INITIALISE / RANDOMISE / COMPARE / WITHDRAW /
PROGRAM SHORT ADDRESS), DTR handling, memory banks, and the DT8 colour
extensions. Driving those from raw frames — instead of from decoded commands, as
python-dali's own ``Bus`` does — is what lets the unmodified wb-mqtt-dali stack
run on top: everything from frame encoding upwards is production code.

The bus is deliberately synchronous. Delay modelling belongs to the gateway
above it, which is where the real timing constraints (queue depth, response
timeout) live.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Deque, List, Optional, Sequence, Tuple

from dali.command import Command, from_frame
from dali.device.helpers import DeviceInstanceTypeMapper
from dali.frame import ForwardFrame
from dali.gear.general import EnableDeviceType

from ..wbdali_registers import TransmissionStatus

logger = logging.getLogger("wb.mqtt_dali.sim.bus")

GEAR_FRAME_BITS = 16
DEVICE_FRAME_BITS = 24

# Frames kept for inspection. A bus that has been scanned carries traffic
# continuously, so this has to be bounded — nothing reads more than the tail.
HISTORY_LENGTH = 512


class SimulatedDaliBus:
    """One DALI bus segment.

    :param gear: ``dali.tests.fakes.Gear`` instances (control gear, 16-bit frames)
    :param devices: ``dali.tests.fakes.Device`` instances (DALI-2 control devices,
        24-bit frames)
    """

    def __init__(self, gear: Sequence = (), devices: Sequence = ()) -> None:
        self.gear = list(gear)
        self.devices = list(devices)
        self.powered = True

        # The most recent frames, for tests and the debug view.
        self.history: Deque[Tuple[int, int, Optional[int]]] = deque(maxlen=HISTORY_LENGTH)
        self.frames_seen = 0

        # A DT-specific command is decodable only in the context of the
        # EnableDeviceType that immediately precedes it, exactly as on a real bus.
        self._devicetype = 0
        self._dev_inst_map = DeviceInstanceTypeMapper()

    # -- wiring ----------------------------------------------------------

    def add_gear(self, gear) -> None:
        """Wire a control gear (a python-dali fake or its simulated subclass) to the bus."""
        self.gear.append(gear)

    def add_device(self, device) -> None:
        """Wire a DALI-2 control device to the bus."""
        self.devices.append(device)

    # -- transmission ----------------------------------------------------

    def send_frame(self, data: int, bit_length: int) -> Tuple[TransmissionStatus, int]:
        """Transmit one forward frame and return ``(status, backward_frame)``.

        ``backward_frame`` is 0 whenever the status says there was no answer.
        """
        if not self.powered:
            return TransmissionStatus.NO_POWER_ON_BUS, 0

        command = self._decode(data, bit_length)
        answers = self._deliver(command, bit_length)
        self._track_devicetype(command)
        self.history.append((data, bit_length, answers[0] if answers else None))
        self.frames_seen += 1

        if not answers:
            # A frame nobody answers is a successful transmission with no
            # backward frame — the gateway cannot tell "query with no answer"
            # from "command that expects none", and neither can we.
            return TransmissionStatus.WITHOUT_RESPONSE, 0
        if len(answers) > 1:
            # Simultaneous answers collide into an unreadable backward frame.
            return TransmissionStatus.BROKEN_RESPONSE, answers[0] & 0xFF
        return TransmissionStatus.WITH_BACKWARD_RESPONSE, answers[0] & 0xFF

    def _decode(self, data: int, bit_length: int) -> Command:
        frame = ForwardFrame(bit_length, data)
        return from_frame(frame, devicetype=self._devicetype, dev_inst_map=self._dev_inst_map)

    def _deliver(self, command: Command, bit_length: int) -> List[int]:
        # A 25-bit proprietary frame addresses nothing we model.
        if bit_length == GEAR_FRAME_BITS:
            recipients = self.gear
        elif bit_length == DEVICE_FRAME_BITS:
            recipients = self.devices
        else:
            return []

        answers = []
        for recipient in recipients:
            try:
                answer = recipient.send(command)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.exception("Simulated unit failed on %s", command)
                continue
            if answer is not None:
                answers.append(answer)
        return answers

    def _track_devicetype(self, command: Command) -> None:
        """`EnableDeviceType` applies to the next frame only."""
        if isinstance(command, EnableDeviceType):
            self._devicetype = command.param
        else:
            self._devicetype = 0
