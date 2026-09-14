# Service contract tests

Run from the repository root:

```shell
.venv/bin/pytest tests/service_contract/
```

The tests use in-process fakes. They require no MQTT broker, controller, DALI gateway,
network connection, or other hardware.

`test_service_lifecycle.py` covers the daemon's configuration, broker, signal, and exit-code
contract.

| Test | What it verifies |
|---|---|
| `test_mqtt_authentication_failure_returns_2` | MQTT v3/v5 authentication refusals are fatal |
| `test_non_connect_code_error_remains_retryable` | Runtime Paho errors are not mistaken for CONNACK refusals |
| `test_non_authentication_connack_error_remains_retryable` | Other CONNACK failures still reconnect |
| `test_invalid_broker_url_returns_2` | A malformed command-line broker URL maps to code 2 |
| `test_signals_during_config_load_return_0` | SIGINT/SIGTERM handling is active before config loading |
| `test_stop_during_outage_reports_uncleared_topics` | Brokerless shutdown logs failed retained cleanup and returns 0 |
| `test_stop_during_initial_outage_reports_uncleared_topics` | The same cleanup contract applies before the first session |
