from dali.command import Response


class WbGatewayTransmissionError(Response):
    _expected = True
    _error_acceptable = False

    def __init__(self):
        super().__init__(None)

    @property
    def raw_value(self):
        raise RuntimeError("Transmission error")

    @property
    def value(self):
        raise RuntimeError("Transmission error")

    def __str__(self) -> str:
        return "transmission error"


class NoResponseFromGateway(WbGatewayTransmissionError):
    @property
    def raw_value(self):
        raise RuntimeError("No response from gateway")

    @property
    def value(self):
        raise RuntimeError("No response from gateway")

    def __str__(self) -> str:
        return "no response from gateway"


class NoPowerOnBus(WbGatewayTransmissionError):
    @property
    def raw_value(self):
        raise RuntimeError("No power on bus")

    @property
    def value(self):
        raise RuntimeError("No power on bus")

    def __str__(self) -> str:
        return "no power on bus"


class TransmissionCancelled(WbGatewayTransmissionError):
    @property
    def raw_value(self):
        raise RuntimeError("Transmission cancelled")

    @property
    def value(self):
        raise RuntimeError("Transmission cancelled")

    def __str__(self) -> str:
        return "transmission cancelled"


class GatewayWriteFailed(WbGatewayTransmissionError):
    """wb-mqtt-serial reported an error or no ack: the write may or may not have reached the gateway."""

    @property
    def raw_value(self):
        raise RuntimeError("Gateway write failed")

    @property
    def value(self):
        raise RuntimeError("Gateway write failed")

    def __str__(self) -> str:
        return "gateway write failed"


class GatewayWriteSkipped(WbGatewayTransmissionError):
    """wb-mqtt-serial dropped the write by its total_timeout before it reached the port."""

    @property
    def raw_value(self):
        raise RuntimeError("Gateway write skipped: serial port busy")

    @property
    def value(self):
        raise RuntimeError("Gateway write skipped: serial port busy")

    def __str__(self) -> str:
        return "gateway write skipped: serial port busy"


class TransmissionFailed(WbGatewayTransmissionError):
    @property
    def raw_value(self):
        raise RuntimeError("Gateway failed to send the frame")

    @property
    def value(self):
        raise RuntimeError("Gateway failed to send the frame")

    def __str__(self) -> str:
        return "gateway failed to send the frame"


class Overheat(WbGatewayTransmissionError):
    @property
    def raw_value(self):
        raise RuntimeError("Gateway overheated")

    @property
    def value(self):
        raise RuntimeError("Gateway overheated")

    def __str__(self) -> str:
        return "gateway overheated"


class UnknownResponseStatus(WbGatewayTransmissionError):
    @property
    def raw_value(self):
        raise RuntimeError("Unknown response status")

    @property
    def value(self):
        raise RuntimeError("Unknown response status")

    def __str__(self) -> str:
        return "unknown response status"


class GatewayUnavailable(WbGatewayTransmissionError):
    """Returned while the gateway cannot be reached: wb-mqtt-serial reports it `r` on
    `/meta/error`, or the broker link carrying the Modbus RPC is down."""

    @property
    def raw_value(self):
        raise RuntimeError("Gateway unavailable")

    @property
    def value(self):
        raise RuntimeError("Gateway unavailable")

    def __str__(self) -> str:
        return "gateway unavailable"
