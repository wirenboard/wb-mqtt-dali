# Service lifecycle conformance

## Goal

Bring the default `wb-mqtt-dali` daemon mode in line with the controller service
requirements without changing the DALI configuration format, MQTT API, or systemd unit.

## Scenarios

- A valid configuration is reported in the journal, including the number of explicitly
  configured gateways. An empty gateway list remains valid: the service exposes
  configuration-independent RPC and can discover gateways from wb-mqtt-serial.
- An invalid configuration exits with code 6, as before.
- An invalid MQTT broker URL supplied on the command line exits with code 2 without a
  traceback.
- MQTT authentication refusal exits with code 2 instead of reconnecting indefinitely.
  Other broker failures keep reconnecting.
- SIGINT and SIGTERM received during startup or normal operation request an orderly stop
  and result in exit code 0.
- When shutdown happens while MQTT is unavailable, the gateway is still stopped and the
  service exits with code 0, but reports that retained MQTT topics could not be cleared.

## Approach

- Install signal handlers before reading the configuration and constructing the product
  database, then pass the same stop request into the broker-session lifecycle.
- Classify MQTT CONNACK authentication reason codes separately from retryable broker
  errors and return the daemon exit status from the session runner.
- Validate broker-client construction at the default-service boundary and map malformed
  command-line URLs to the command-line error status.
- Preserve the existing retained-message mirror, reconnect loop, gateway lifetime, RPC
  endpoints, and shutdown cleanup order.

## Verification

- Add isolated regression tests for both exit-code mappings, early signals, configuration
  logging, and shutdown during an MQTT outage. Unit tests use fakes only.
- Run the repository formatting, pylint, and full pytest pipeline in the pinned Python
  3.13.5 environment.
- Build the Debian package on Jenkins and install that artifact on a controller.
- Repeat lifecycle checks on the ordinary controller and verify real WB-DALI transport
  recovery on the online demo kit, where the gateway is RS485-1 slave 118.

## Out of scope

- Configuration/schema changes, code-7 behavior for an empty configuration, systemd unit
  changes, and refactoring DALI application logic.
