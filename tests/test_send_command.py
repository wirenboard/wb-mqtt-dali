import unittest

from dali.address import GearBroadcast, GearShort
from dali.command import Response
from dali.frame import BackwardFrame
from dali.gear.general import DAPC, DTR0, Off, Terminate

from wb.mqtt_dali.send_command import (
    build_command_registry,
    format_response,
    list_commands,
    parse_and_build_command,
)


class TestBuildCommandRegistry(unittest.TestCase):
    def setUp(self):
        self.registry = build_command_registry()

    def test_registry_not_empty(self):
        self.assertGreater(len(self.registry), 0)

    def test_gear_standard_commands_present(self):
        for name in ["Off", "Up", "Down", "StepUp", "StepDown", "RecallMaxLevel", "RecallMinLevel"]:
            self.assertIn(name, self.registry, f"Missing gear standard command: {name}")
            self.assertEqual(self.registry[name].kind, "gear_standard")

    def test_dapc_present(self):
        self.assertIn("DAPC", self.registry)
        self.assertEqual(self.registry["DAPC"].kind, "gear_dapc")
        self.assertTrue(self.registry["DAPC"].needs_data)

    def test_gear_special_commands_present(self):
        for name in ["DTR0", "DTR1", "DTR2", "Terminate"]:
            self.assertIn(name, self.registry, f"Missing gear special command: {name}")
            self.assertEqual(self.registry[name].kind, "gear_special")

    def test_dtr_needs_data(self):
        self.assertTrue(self.registry["DTR0"].needs_data)
        self.assertTrue(self.registry["DTR1"].needs_data)
        self.assertTrue(self.registry["DTR2"].needs_data)
        self.assertFalse(self.registry["Terminate"].needs_data)

    def test_dt_specific_commands_present(self):
        self.assertIn("DT8.Activate", self.registry)
        self.assertEqual(self.registry["DT8.Activate"].device_type, 8)

        self.assertIn("DT17.QueryDimmingCurve", self.registry)
        self.assertEqual(self.registry["DT17.QueryDimmingCurve"].device_type, 17)

        self.assertIn("DT16.QueryFailureStatus", self.registry)
        self.assertEqual(self.registry["DT16.QueryFailureStatus"].device_type, 16)

    def test_device_standard_commands_present(self):
        self.assertIn("FF24.QueryDeviceStatus", self.registry)
        self.assertEqual(self.registry["FF24.QueryDeviceStatus"].kind, "device_standard")

        self.assertIn("FF24.Reset", self.registry)
        self.assertEqual(self.registry["FF24.Reset"].kind, "device_standard")

    def test_device_instance_commands_present(self):
        self.assertIn("FF24.Ix.EnableInstance", self.registry)
        self.assertEqual(self.registry["FF24.Ix.EnableInstance"].kind, "device_instance")

        self.assertIn("FF24.Ix.QueryInstanceType", self.registry)
        self.assertEqual(self.registry["FF24.Ix.QueryInstanceType"].kind, "device_instance")

    def test_device_special_commands_present(self):
        self.assertIn("FF24.DTR0", self.registry)
        self.assertEqual(self.registry["FF24.DTR0"].kind, "device_special")
        self.assertTrue(self.registry["FF24.DTR0"].needs_data)

        self.assertIn("FF24.Terminate", self.registry)
        self.assertEqual(self.registry["FF24.Terminate"].kind, "device_special")
        self.assertFalse(self.registry["FF24.Terminate"].needs_data)

    def test_instance_type_specific_commands_present(self):
        # feedback: instance_type=32
        self.assertIn("FF24.DT32.Ix.ActivateFeedback", self.registry)
        self.assertEqual(self.registry["FF24.DT32.Ix.ActivateFeedback"].kind, "device_instance")

        # absolute_input_device: instance_type=2
        self.assertIn("FF24.DT2.Ix.QuerySwitch", self.registry)
        self.assertEqual(self.registry["FF24.DT2.Ix.QuerySwitch"].kind, "device_instance")

        # general_purpose_sensor: instance_type=6
        self.assertIn("FF24.DT6.Ix.SetReportTimer", self.registry)
        self.assertEqual(self.registry["FF24.DT6.Ix.SetReportTimer"].kind, "device_instance")

    def test_no_private_classes_in_registry(self):
        for key in self.registry:
            parts = key.split(".")
            for part in parts:
                self.assertFalse(part.startswith("_"), f"Private class in registry: {key}")

    def test_query_commands_have_response(self):
        for key, info in self.registry.items():
            if "Query" in key:
                self.assertIsNotNone(
                    info.cls.response,
                    f"Query command {key} should have a response class",
                )


class TestParseAndBuildCommand(unittest.TestCase):
    def setUp(self):
        self.registry = build_command_registry()

    def test_gear_standard_with_address(self):
        cmd = parse_and_build_command("Off", self.registry, address=5)
        self.assertIsInstance(cmd, Off)

    def test_gear_standard_broadcast(self):
        cmd = parse_and_build_command("Off", self.registry, broadcast=True)
        self.assertIsInstance(cmd, Off)
        self.assertEqual(cmd.frame.as_integer, Off(GearBroadcast()).frame.as_integer)

    def test_gear_standard_requires_address_or_broadcast(self):
        with self.assertRaises(ValueError):
            parse_and_build_command("Off", self.registry)

    def test_dapc_with_data(self):
        cmd = parse_and_build_command("DAPC", self.registry, address=5, data=200)
        self.assertIsInstance(cmd, DAPC)
        self.assertEqual(cmd.frame.as_integer, DAPC(GearShort(5), 200).frame.as_integer)

    def test_dapc_requires_data(self):
        with self.assertRaises(ValueError):
            parse_and_build_command("DAPC", self.registry, address=5)

    def test_gear_special_no_address(self):
        cmd = parse_and_build_command("DTR0", self.registry, data=128)
        self.assertIsInstance(cmd, DTR0)
        self.assertEqual(cmd.frame.as_integer, DTR0(128).frame.as_integer)

    def test_gear_special_rejects_address(self):
        with self.assertRaises(ValueError):
            parse_and_build_command("DTR0", self.registry, address=5, data=128)

    def test_terminate_no_args(self):
        cmd = parse_and_build_command("Terminate", self.registry)
        self.assertIsInstance(cmd, Terminate)

    def test_dt_specific_command(self):
        from wb.mqtt_dali.gear.dimming_curve import QueryDimmingCurve

        cmd = parse_and_build_command("DT17.QueryDimmingCurve", self.registry, address=5)
        self.assertIsInstance(cmd, QueryDimmingCurve)

    def test_device_standard_command(self):
        from dali.device.general import QueryDeviceStatus

        cmd = parse_and_build_command("FF24.QueryDeviceStatus", self.registry, address=5)
        self.assertIsInstance(cmd, QueryDeviceStatus)

    def test_device_instance_command(self):
        from dali.device.general import EnableInstance

        cmd = parse_and_build_command("FF24.I0.EnableInstance", self.registry, address=5)
        self.assertIsInstance(cmd, EnableInstance)

    def test_device_instance_different_numbers(self):
        from dali.device.general import EnableInstance

        cmd0 = parse_and_build_command("FF24.I0.EnableInstance", self.registry, address=5)
        cmd3 = parse_and_build_command("FF24.I3.EnableInstance", self.registry, address=5)
        self.assertIsInstance(cmd0, EnableInstance)
        self.assertIsInstance(cmd3, EnableInstance)
        # Different instance numbers should produce different frames
        self.assertNotEqual(cmd0.frame.as_integer, cmd3.frame.as_integer)

    def test_instance_type_specific_command(self):
        from wb.mqtt_dali.device.feedback import ActivateFeedback

        cmd = parse_and_build_command("FF24.DT32.I2.ActivateFeedback", self.registry, address=5)
        self.assertIsInstance(cmd, ActivateFeedback)

    def test_device_special_command(self):
        from dali.device.general import DTR0 as DeviceDTR0

        cmd = parse_and_build_command("FF24.DTR0", self.registry, data=42)
        self.assertIsInstance(cmd, DeviceDTR0)

    def test_device_standard_broadcast(self):
        from dali.device.general import QueryDeviceStatus

        cmd = parse_and_build_command("FF24.QueryDeviceStatus", self.registry, broadcast=True)
        self.assertIsInstance(cmd, QueryDeviceStatus)

    def test_address_and_broadcast_conflict(self):
        with self.assertRaises(ValueError) as ctx:
            parse_and_build_command("Off", self.registry, address=5, broadcast=True)
        self.assertIn("cannot use --address together with --broadcast", str(ctx.exception))

    def test_address_out_of_range(self):
        with self.assertRaises(ValueError):
            parse_and_build_command("Off", self.registry, address=64)
        with self.assertRaises(ValueError):
            parse_and_build_command("Off", self.registry, address=-1)

    def test_unknown_command(self):
        with self.assertRaises(ValueError) as ctx:
            parse_and_build_command("NotARealCommand", self.registry)
        self.assertIn("Unknown command", str(ctx.exception))


class TestFormatResponse(unittest.TestCase):
    def test_none_response(self):
        self.assertEqual(format_response(None), "No response")

    def test_response_with_backward_frame(self):
        frame = BackwardFrame(42)
        response = Response(frame)
        result = format_response(response)
        self.assertIn("42", result)
        self.assertIn("0x2a", result)

    def test_response_with_none_frame(self):
        response = Response(None)
        result = format_response(response)
        self.assertIn("timeout", result.lower())


class TestListCommands(unittest.TestCase):
    def setUp(self):
        self.registry = build_command_registry()

    def test_list_all_commands(self):
        result = list_commands(self.registry)
        self.assertIn("Off", result)
        self.assertIn("DAPC", result)
        self.assertIn("DTR0", result)
        self.assertIn("General:", result)

    def test_query_commands_marked(self):
        result = list_commands(self.registry)
        self.assertIn("[query]", result)

    def test_data_required_marked(self):
        result = list_commands(self.registry)
        self.assertIn("(requires --data)", result)


if __name__ == "__main__":
    unittest.main()
