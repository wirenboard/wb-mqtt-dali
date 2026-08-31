"""A hardware-free DALI installation for wb-mqtt-dali.

Three layers, each usable on its own:

* :mod:`.dali_bus` / :mod:`.control_gear` — control gear and DALI-2 control
  devices that answer real frames, built on python-dali's test fakes, with
  commissioning, groups, scenes, DT8 colour and button events;
* :mod:`.gateway` / :mod:`.network` — WB-DALI modules addressed over Modbus,
  modelling the register map in :mod:`wb.mqtt_dali.wbdali_registers` the way
  the firmware behaves (pointer-ordered queue, reply registers, the four-slot
  monitor ring);
* :mod:`.broker` / :mod:`.serial_service` — an in-process MQTT broker and a
  stand-in for wb-mqtt-serial, so the unmodified daemon runs against all of
  the above end to end.

:func:`~wb.mqtt_dali.sim.scenario.build_network` builds an installation from
a plain description; :func:`~wb.mqtt_dali.sim.scenario.default_scenario` is
a small mixed one.

The names below resolve on first use rather than at import: a host may stand
:mod:`.broker` in for ``aiomqtt`` itself, and the daemon modules the other
layers import must not be pulled in while that shim is still loading.
"""

from importlib import import_module

_EXPORTS = {
    "Broker": ".broker",
    "Client": ".broker",
    "FakeWbMqttSerial": ".serial_service",
    "default_serial_config": ".serial_service",
    "SimulatedControlDevice": ".control_gear",
    "SimulatedControlGear": ".control_gear",
    "SimulatedDaliBus": ".dali_bus",
    "VirtualWbDaliGateway": ".gateway",
    "SimulatedModbusNetwork": ".network",
    "build_network": ".scenario",
    "default_scenario": ".scenario",
    "serial_config": ".scenario",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module, __name__), name)
