"""A hardware-free DALI installation for wb-mqtt-dali.

Three layers, each usable on its own:

* :mod:`.dali_bus` / :mod:`.control_gear` — control gear and DALI-2 control
  devices that answer real frames, built on python-dali's test fakes, with
  commissioning, groups, scenes, DT8 colour and button events;
* :mod:`.gateway` / :mod:`.network` — WB-DALI modules addressed over Modbus,
  modelling the register map in :mod:`.registers` the way the firmware behaves
  (pointer-ordered queue, reply registers, the four-slot monitor ring);
* :mod:`.broker` / :mod:`.serial_service` — an in-process MQTT broker and a
  stand-in for wb-mqtt-serial, so the unmodified daemon runs against all of
  the above end to end.

:func:`~wb.mqtt_dali.sim.scenario.build_network` builds an installation from
a plain description; :func:`~wb.mqtt_dali.sim.scenario.default_scenario` is
a small mixed one.
"""

from .broker import Broker, Client
from .control_gear import SimulatedControlDevice, SimulatedControlGear
from .dali_bus import SimulatedDaliBus
from .gateway import VirtualWbDaliGateway
from .network import SimulatedModbusNetwork
from .scenario import build_network, default_scenario, serial_config
from .serial_service import FakeWbMqttSerial, default_serial_config

__all__ = [
    "Broker",
    "Client",
    "FakeWbMqttSerial",
    "SimulatedControlDevice",
    "SimulatedControlGear",
    "SimulatedDaliBus",
    "SimulatedModbusNetwork",
    "VirtualWbDaliGateway",
    "build_network",
    "default_scenario",
    "default_serial_config",
    "serial_config",
]
