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


class NoTransmission(WbGatewayTransmissionError):
    @property
    def raw_value(self):
        raise RuntimeError("No transmission, internal gateway error")

    @property
    def value(self):
        raise RuntimeError("No transmission, internal gateway error")

    def __str__(self) -> str:
        return "no transmission, internal gateway error"


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


class UnknownResponseStatus(WbGatewayTransmissionError):
    @property
    def raw_value(self):
        raise RuntimeError("Unknown response status")

    @property
    def value(self):
        raise RuntimeError("Unknown response status")

    def __str__(self) -> str:
        return "unknown response status"
