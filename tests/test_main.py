import pytest

from wb.mqtt_dali.config_validator import validate_config


def _make_config(gateways):
    return {"gateways": gateways}


def _make_gateway(device_id, buses):
    return {"device_id": device_id, "buses": buses}


def _make_bus(devices):
    return {"devices": devices}


def _make_device(short, random=0x123456, dali2=False, mqtt_id=None):
    dev = {"short": short, "random": random}
    if dali2:
        dev["dali2"] = True
    if mqtt_id is not None:
        dev["mqtt_id"] = mqtt_id
    return dev


# --- valid configurations ---


def test_validate_config_empty():
    validate_config({})


def test_validate_config_no_conflicts():
    config = _make_config(
        [
            _make_gateway(
                "gw1",
                [
                    _make_bus([_make_device(0), _make_device(1)]),
                    _make_bus([_make_device(0), _make_device(1)]),
                ],
            )
        ]
    )
    validate_config(config)


def test_validate_config_dali2_same_short_no_conflict():
    # DALI and DALI 2 devices may share the same short address on the same bus
    # because their default mqtt_ids differ ("gw_bus_1_0" vs "gw_bus_1_dali2_0")
    config = _make_config([_make_gateway("gw", [_make_bus([_make_device(0), _make_device(0, dali2=True)])])])
    validate_config(config)


def test_validate_config_custom_mqtt_id_no_conflict():
    config = _make_config(
        [
            _make_gateway(
                "gw",
                [_make_bus([_make_device(0, mqtt_id="light_a"), _make_device(1, mqtt_id="light_b")])],
            )
        ]
    )
    validate_config(config)


# --- duplicate mqtt_id ---


def test_validate_config_duplicate_short_same_bus():
    config = _make_config([_make_gateway("gw", [_make_bus([_make_device(5), _make_device(5)])])])
    with pytest.raises(ValueError, match="Duplicate mqtt_id"):
        validate_config(config)


def test_validate_config_duplicate_explicit_mqtt_id():
    config = _make_config(
        [
            _make_gateway(
                "gw",
                [_make_bus([_make_device(0, mqtt_id="same_id"), _make_device(1, mqtt_id="same_id")])],
            )
        ]
    )
    with pytest.raises(ValueError, match="Duplicate mqtt_id 'same_id'"):
        validate_config(config)


def test_validate_config_explicit_mqtt_id_conflicts_with_default():
    # Device 1 has explicit mqtt_id equal to device 0's auto-generated id
    config = _make_config(
        [_make_gateway("gw", [_make_bus([_make_device(0), _make_device(1, mqtt_id="gw_bus_1_0")])])]
    )
    with pytest.raises(ValueError, match="Duplicate mqtt_id 'gw_bus_1_0'"):
        validate_config(config)


def test_validate_config_error_message_contains_location():
    config = _make_config([_make_gateway("my_gw", [_make_bus([_make_device(7), _make_device(7)])])])
    with pytest.raises(ValueError, match="gateway 'my_gw', bus 1, short address 7"):
        validate_config(config)


def test_validate_config_multiple_errors_all_reported():
    config = _make_config(
        [
            _make_gateway(
                "gw",
                [
                    _make_bus([_make_device(3), _make_device(3)]),
                    _make_bus([_make_device(5), _make_device(5)]),
                ],
            )
        ]
    )
    with pytest.raises(ValueError) as exc_info:
        validate_config(config)
    msg = str(exc_info.value)
    assert "bus 1" in msg
    assert "bus 2" in msg
