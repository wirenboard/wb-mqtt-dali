"""Tests for decoding and publishing general purpose sensor measurement events.

IEC 62386-306 measurement events carry a 9-bit value in the 10-bit event field;
bit 9 is the measurement-event discriminator, not data. These tests assert the
event round-trips through `Command.from_frame` without raising (the old code
wrote the 10-bit value into a 9-bit frame slot and crashed), that the decoded
measurement excludes the discriminator bit, and that the publish path emits the
actual value.
"""

import unittest
from unittest.mock import AsyncMock

from dali.address import DeviceShort
from dali.command import Command
from dali.device.helpers import DeviceInstanceTypeMapper

from wb.mqtt_dali.dali2_controls import publish_dali2_event
from wb.mqtt_dali.device import general_purpose_sensor

# Bit 9 of the 10-bit event field flags a measurement event (vs. data).
_MEASUREMENT_FLAG = 1 << 9


def _measurement_data(value: int) -> int:
    return _MEASUREMENT_FLAG | value


class MeasurementEventDecodeTests(unittest.TestCase):
    def test_measurement_event_roundtrip_decodes(self):
        """A Device-scheme measurement event builds a frame, decodes back via
        `from_frame` as a `MeasurementEvent`, and yields the exact 9-bit value.
        """
        event = general_purpose_sensor.MeasurementEvent(
            short_address=DeviceShort(3), data=_measurement_data(300)
        )

        decoded = Command.from_frame(event.frame)

        self.assertIsInstance(decoded, general_purpose_sensor.MeasurementEvent)
        self.assertEqual(decoded.measurement, 300)
        self.assertEqual(decoded.short_address.address, 3)

    def test_measurement_event_boundary_values(self):
        """The minimum (0) and maximum (511) 9-bit values decode without raising
        and without the +512 offset from the discriminator bit.
        """
        for value in (0, 0x1FF):
            event = general_purpose_sensor.MeasurementEvent(
                short_address=DeviceShort(0), data=_measurement_data(value)
            )

            decoded = Command.from_frame(event.frame)

            self.assertIsInstance(decoded, general_purpose_sensor.MeasurementEvent)
            self.assertEqual(decoded.measurement, value)

    def test_measurement_event_device_instance_scheme(self):
        """In the Device/Instance scheme the instance type is resolved through a
        `dev_inst_map`; the event still decodes to the exact 9-bit value.
        """
        event = general_purpose_sensor.MeasurementEvent(
            short_address=DeviceShort(5), instance_number=2, data=_measurement_data(123)
        )
        dev_inst_map = DeviceInstanceTypeMapper()
        dev_inst_map.add_type(short_address=5, instance_number=2, instance_type=general_purpose_sensor)

        decoded = Command.from_frame(event.frame, dev_inst_map=dev_inst_map)

        self.assertIsInstance(decoded, general_purpose_sensor.MeasurementEvent)
        self.assertEqual(decoded.measurement, 123)
        self.assertEqual(decoded.instance_number, 2)


class MeasurementEventPublishTests(unittest.IsolatedAsyncioTestCase):
    async def test_measurement_event_published_value(self):
        """Publishing a measurement event writes the actual value (no +512) to
        the `measurement{instance}` control topic.
        """
        event = general_purpose_sensor.MeasurementEvent(
            short_address=DeviceShort(1), instance_number=4, data=_measurement_data(257)
        )
        mqtt_client = AsyncMock()

        await publish_dali2_event(event, "wb-dali_1_1", mqtt_client)

        mqtt_client.publish.assert_awaited_once()
        topic, value = mqtt_client.publish.await_args.args
        self.assertEqual(topic, "/devices/wb-dali_1_1/controls/measurement4")
        self.assertEqual(value, "257")
