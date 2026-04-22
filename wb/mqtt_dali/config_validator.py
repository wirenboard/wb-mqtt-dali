def _device_default_mqtt_id(gateway_device_id: str, bus_index: int, dev_conf: dict) -> str:
    bus_uid = f"{gateway_device_id}_bus_{bus_index}"
    mqtt_id_part = "dali2_" if dev_conf.get("dali2", False) else ""
    return f"{bus_uid}_{mqtt_id_part}{dev_conf['short']}"


def validate_config(config: dict) -> None:
    seen: dict[str, str] = {}  # mqtt_id -> human-readable location
    errors: list[str] = []
    for gw_conf in config.get("gateways", []):
        gateway_device_id = gw_conf["device_id"]
        for bus_index, bus_conf in enumerate(gw_conf.get("buses", []), 1):
            for dev_conf in bus_conf.get("devices", []):
                short = dev_conf["short"]
                location = f"gateway '{gateway_device_id}', bus {bus_index}, short address {short}"
                explicit_mqtt_id = dev_conf.get("mqtt_id")
                effective_mqtt_id = explicit_mqtt_id or _device_default_mqtt_id(
                    gateway_device_id, bus_index, dev_conf
                )
                if effective_mqtt_id in seen:
                    errors.append(
                        f"Duplicate mqtt_id '{effective_mqtt_id}': "
                        f"{location} conflicts with {seen[effective_mqtt_id]}"
                    )
                else:
                    seen[effective_mqtt_id] = location
    if errors:
        raise ValueError("Invalid configuration:\n" + "\n".join(errors))
