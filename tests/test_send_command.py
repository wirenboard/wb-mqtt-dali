import unittest

import pytest
from dali.address import (
    DeviceBroadcast,
    DeviceShort,
    GearBroadcast,
    GearGroup,
    GearShort,
    InstanceNumber,
)
from dali.command import Response
from dali.device.general import DTR0 as DeviceDTR0
from dali.device.general import (
    AmbiguousInstanceType,
    EnableInstance,
    QueryDeviceStatus,
    QueryEventFilterM,
)
from dali.device.general import Reset as DeviceReset
from dali.device.general import Terminate as DeviceTerminate
from dali.device.general import UnknownEvent
from dali.device.light import LightEvent
from dali.device.pushbutton import ButtonPressed, QueryShortTimer, ShortPress
from dali.frame import BackwardFrame, ForwardFrame
from dali.gear.colour import Activate as DT8Activate
from dali.gear.general import (
    DAPC,
    DTR0,
    Initialise,
    Off,
    ProgramShortAddress,
    Terminate,
    UnknownGearCommand,
    VerifyShortAddress,
)

from wb.mqtt_dali.device.feedback import QueryFeedbackActive
from wb.mqtt_dali.send_command import (
    LazyCommandExpression,
    build_command_registry,
    format_command_expression,
    format_response,
    list_commands,
    parse_expression,
)

# pylint: disable=import-outside-toplevel
# `registry` is a module-scope pytest fixture; tests receive it as a parameter,
# which pylint sees as redefining the fixture's outer name.
# pylint: disable=redefined-outer-name


@pytest.fixture(scope="module")
def registry():
    return build_command_registry()


class TestParseExpression:  # pylint: disable=too-many-public-methods
    def test_parse_off_short(self, registry):
        cmd = parse_expression("Off(A5)", registry)
        assert isinstance(cmd, Off)
        assert cmd.frame.as_integer == Off(GearShort(5)).frame.as_integer

    def test_parse_off_group(self, registry):
        cmd = parse_expression("Off(G3)", registry)
        assert isinstance(cmd, Off)
        assert cmd.frame.as_integer == Off(GearGroup(3)).frame.as_integer

    def test_parse_off_implicit_broadcast(self, registry):
        """`Off()` and `Off` (no parens) are equivalent broadcasts."""
        cmd_parens = parse_expression("Off()", registry)
        cmd_no_parens = parse_expression("Off", registry)
        broadcast_frame = Off(GearBroadcast()).frame.as_integer
        assert cmd_parens.frame.as_integer == broadcast_frame
        assert cmd_no_parens.frame.as_integer == broadcast_frame

    def test_parse_dt8_activate_no_parens(self, registry):
        cmd_no_parens = parse_expression("DT8.Activate", registry)
        cmd_parens = parse_expression("DT8.Activate()", registry)
        assert isinstance(cmd_no_parens, DT8Activate)
        assert cmd_no_parens.frame.as_integer == cmd_parens.frame.as_integer

    def test_parse_ff24_reset_no_parens(self, registry):
        cmd = parse_expression("FF24.Reset", registry)
        assert isinstance(cmd, DeviceReset)
        assert cmd.frame.as_integer == DeviceReset(DeviceBroadcast()).frame.as_integer

    def test_parse_ff24_terminate_no_parens(self, registry):
        cmd_no_parens = parse_expression("FF24.Terminate", registry)
        cmd_parens = parse_expression("FF24.Terminate()", registry)
        assert isinstance(cmd_no_parens, DeviceTerminate)
        assert cmd_no_parens.frame.as_integer == cmd_parens.frame.as_integer

    def test_parse_dapc_decimal_data(self, registry):
        cmd = parse_expression("DAPC(A5, 100)", registry)
        assert isinstance(cmd, DAPC)
        assert cmd.frame.as_integer == DAPC(GearShort(5), 100).frame.as_integer

    def test_parse_dapc_hex_data(self, registry):
        lower = parse_expression("DAPC(A5, 0xFE)", registry)
        upper = parse_expression("DAPC(A5, 0XFE)", registry)
        expected = DAPC(GearShort(5), 254).frame.as_integer
        assert lower.frame.as_integer == expected
        assert upper.frame.as_integer == expected

    def test_parse_dapc_bin_oct_data(self, registry):
        bin_cmd = parse_expression("DAPC(A5, 0b1100100)", registry)
        oct_cmd = parse_expression("DAPC(A5, 0o144)", registry)
        expected = DAPC(GearShort(5), 100).frame.as_integer
        assert bin_cmd.frame.as_integer == expected
        assert oct_cmd.frame.as_integer == expected

    def test_parse_dapc_implicit_broadcast(self, registry):
        cmd = parse_expression("DAPC(100)", registry)
        assert isinstance(cmd, DAPC)
        assert cmd.frame.as_integer == DAPC(GearBroadcast(), 100).frame.as_integer

    def test_parse_dt8_activate_broadcast(self, registry):
        cmd = parse_expression("DT8.Activate()", registry)
        assert isinstance(cmd, DT8Activate)

    def test_parse_special_terminate_no_parens(self, registry):
        cmd_no_parens = parse_expression("Terminate", registry)
        cmd_parens = parse_expression("Terminate()", registry)
        assert isinstance(cmd_no_parens, Terminate)
        assert cmd_no_parens.frame.as_integer == cmd_parens.frame.as_integer

    def test_parse_special_dtr_with_data(self, registry):
        gear_cmd = parse_expression("DTR0(42)", registry)
        device_cmd = parse_expression("FF24.DTR0(42)", registry)
        assert isinstance(gear_cmd, DTR0)
        assert gear_cmd.frame.as_integer == DTR0(42).frame.as_integer
        assert isinstance(device_cmd, DeviceDTR0)
        assert device_cmd.frame.as_integer == DeviceDTR0(42).frame.as_integer

    def test_parse_ff24_standard(self, registry):
        """Standard FF24 device commands accept short address or implicit broadcast."""
        cmd_short = parse_expression("FF24.QueryDeviceStatus(A5)", registry)
        cmd_bcast = parse_expression("FF24.Reset()", registry)
        assert isinstance(cmd_short, QueryDeviceStatus)
        assert cmd_short.frame.as_integer == QueryDeviceStatus(DeviceShort(5)).frame.as_integer
        assert isinstance(cmd_bcast, DeviceReset)
        assert cmd_bcast.frame.as_integer == DeviceReset(DeviceBroadcast()).frame.as_integer

    def test_parse_device_instance_general(self, registry):
        cmd_addr = parse_expression("FF24.EnableInstance(A3, I0)", registry)
        cmd_bcast = parse_expression("FF24.EnableInstance(I0)", registry)
        assert isinstance(cmd_addr, EnableInstance)
        assert isinstance(cmd_bcast, EnableInstance)

    def test_parse_device_instance_different_numbers(self, registry):
        """Different I<n> produce different frames for the same instance command."""
        cmd0 = parse_expression("FF24.EnableInstance(A5, I0)", registry)
        cmd3 = parse_expression("FF24.EnableInstance(A5, I3)", registry)
        assert isinstance(cmd0, EnableInstance)
        assert isinstance(cmd3, EnableInstance)
        assert cmd0.frame.as_integer != cmd3.frame.as_integer

    def test_parse_device_instance_rejects_inline_data(self, registry):
        """Instance-commands don't take inline data in the current python-dali —
        the data byte is delivered via a preceding DTR0/1/2 send. The parser
        rejects the inline-data form on both `FF24.SetEventFilter(A3, I0, 0x1F)`
        and `FF24.DT1.SetEventFilter(A3, I0, 0x1F)` (different failure paths —
        registered without `needs_data` vs. not registered at all — but both
        surface as `ValueError`).
        """
        with pytest.raises(ValueError):
            parse_expression("FF24.SetEventFilter(A3, I0, 0x1F)", registry)
        with pytest.raises(ValueError):
            parse_expression("FF24.DT1.SetEventFilter(A3, I0, 0x1F)", registry)

    def test_parse_device_instance_type_specific(self, registry):
        cmd = parse_expression("FF24.DT1.QueryShortTimer(A3, I2)", registry)
        assert isinstance(cmd, QueryShortTimer)

    def test_parse_dt_specific_command(self, registry):
        """DT-specific gear commands resolve via `DT<n>.<Name>` lookup."""
        from wb.mqtt_dali.gear.dimming_curve import QueryDimmingCurve

        cmd = parse_expression("DT17.QueryDimmingCurve(A5)", registry)
        assert isinstance(cmd, QueryDimmingCurve)

    def test_parse_device_feature_device(self, registry):
        """Without I<n> a feature command targets the device-level variant."""
        cmd_addr = parse_expression("FF24.F32.QueryFeedbackActive(A3)", registry)
        cmd_bcast = parse_expression("FF24.F32.QueryFeedbackActive()", registry)
        assert isinstance(cmd_addr, QueryFeedbackActive)
        assert isinstance(cmd_bcast, QueryFeedbackActive)

    def test_parse_device_feature_instance(self, registry):
        """With I<n> the same name resolves to the per-instance variant."""
        cmd = parse_expression("FF24.F32.QueryFeedbackActive(A3, I0)", registry)
        assert isinstance(cmd, QueryFeedbackActive)

    def test_parse_rejects_unknown_command(self, registry):
        with pytest.raises(ValueError, match="Unknown command"):
            parse_expression("WhatIsThis(A5)", registry)

    def test_parse_rejects_unbalanced_parens(self, registry):
        """Unbalanced parens get an explicit error message, not the generic
        'cannot parse' fallback.
        """
        with pytest.raises(ValueError, match="unbalanced parentheses"):
            parse_expression("Off(A5", registry)
        with pytest.raises(ValueError, match="unbalanced parentheses"):
            parse_expression("Off A5)", registry)

    def test_parse_rejects_unknown_prefix(self, registry):
        with pytest.raises(ValueError):
            parse_expression("Off(X5)", registry)

    def test_parse_rejects_address_on_special(self, registry):
        with pytest.raises(ValueError):
            parse_expression("DTR0(A5, 42)", registry)
        with pytest.raises(ValueError):
            parse_expression("Terminate(G3)", registry)

    def test_parse_rejects_two_addresses(self, registry):
        with pytest.raises(ValueError):
            parse_expression("Off(A5, G3)", registry)

    def test_parse_rejects_data_when_not_needed(self, registry):
        with pytest.raises(ValueError):
            parse_expression("Off(A5, 100)", registry)

    def test_parse_rejects_missing_data(self, registry):
        with pytest.raises(ValueError):
            parse_expression("DAPC(A5)", registry)

    def test_parse_rejects_missing_instance(self, registry):
        with pytest.raises(ValueError):
            parse_expression("FF24.EnableInstance(A3)", registry)

    def test_parse_rejects_instance_on_non_instance_command(self, registry):
        with pytest.raises(ValueError):
            parse_expression("FF24.QueryDeviceStatus(A3, I0)", registry)

    def test_parse_rejects_out_of_range(self, registry):
        with pytest.raises(ValueError):
            parse_expression("Off(A64)", registry)
        with pytest.raises(ValueError):
            parse_expression("Off(G16)", registry)
        with pytest.raises(ValueError):
            parse_expression("FF24.EnableInstance(A3, I32)", registry)
        with pytest.raises(ValueError):
            parse_expression("DAPC(A5, 256)", registry)

    def test_parse_rejects_negative_address(self, registry):
        """A<n>/G<n>/I<n> are unsigned; signed forms get a parser-level error
        instead of leaking down to dali address classes.
        """
        with pytest.raises(ValueError):
            parse_expression("Off(A-5)", registry)
        with pytest.raises(ValueError):
            parse_expression("Off(G-1)", registry)
        with pytest.raises(ValueError):
            parse_expression("FF24.EnableInstance(A3, I-3)", registry)

    def test_parse_ff24_group_upper_bound_31(self, registry):
        """FF24 commands allow groups 0..31 (vs gear 0..15)."""
        ok = parse_expression("FF24.QueryDeviceStatus(G31)", registry)
        assert ok is not None
        with pytest.raises(ValueError):
            parse_expression("FF24.QueryDeviceStatus(G32)", registry)

    def test_parse_commissioning_rejects_multiple_args(self, registry):
        """The commissioning addressing specials take a single argument; a second
        token is rejected for every one of them (Initialise included)."""
        for expr in ("Initialise(A5, A6)", "ProgramShortAddress(A5, A6)", "VerifyShortAddress(A5, A6)"):
            with pytest.raises(ValueError):
                parse_expression(expr, registry)

    def test_parse_commissioning_rejects_missing_arg_on_short_address(self, registry):
        """The address argument is mandatory for ProgramShortAddress/
        VerifyShortAddress — the no-argument broadcast form is valid only for
        Initialise."""
        with pytest.raises(ValueError):
            parse_expression("ProgramShortAddress()", registry)
        with pytest.raises(ValueError):
            parse_expression("VerifyShortAddress()", registry)

    def test_parse_commissioning_rejects_non_address_token(self, registry):
        """A data token or garbage where an A<n>/no_short_address argument is
        expected is rejected — neither a bare integer (`ProgramShortAddress(100)`)
        nor an unparseable token (`Initialise(foo)`) is a valid short address."""
        with pytest.raises(ValueError):
            parse_expression("ProgramShortAddress(100)", registry)
        with pytest.raises(ValueError):
            parse_expression("Initialise(foo)", registry)

    def test_parse_commissioning_rejects_address_out_of_range(self, registry):
        """The short address must be in 0..63 — `Initialise(A64)` is out of range."""
        with pytest.raises(ValueError):
            parse_expression("Initialise(A64)", registry)


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
        self.assertEqual(self.registry["DAPC"].kind, "gear_standard")
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
        self.assertIn("FF24.EnableInstance", self.registry)
        self.assertEqual(self.registry["FF24.EnableInstance"].kind, "device_instance")

        self.assertIn("FF24.QueryInstanceType", self.registry)
        self.assertEqual(self.registry["FF24.QueryInstanceType"].kind, "device_instance")

    def test_device_special_commands_present(self):
        self.assertIn("FF24.DTR0", self.registry)
        self.assertEqual(self.registry["FF24.DTR0"].kind, "device_special")
        self.assertTrue(self.registry["FF24.DTR0"].needs_data)

        self.assertIn("FF24.Terminate", self.registry)
        self.assertEqual(self.registry["FF24.Terminate"].kind, "device_special")
        self.assertFalse(self.registry["FF24.Terminate"].needs_data)

    def test_instance_type_specific_commands_present(self):
        # absolute_input_device: instance_type=2
        self.assertIn("FF24.DT2.QuerySwitch", self.registry)
        self.assertEqual(self.registry["FF24.DT2.QuerySwitch"].kind, "device_instance")

        # general_purpose_sensor: instance_type=6
        self.assertIn("FF24.DT6.SetReportTimer", self.registry)
        self.assertEqual(self.registry["FF24.DT6.SetReportTimer"].kind, "device_instance")

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


def _canonical_expression(name: str, info) -> str:
    """Build a representative parseable expression for a registry entry from its
    kind/flags — enough to exercise every address/instance/data slot the command
    carries. Used by the all-registry round-trip invariant."""
    from wb.mqtt_dali.send_command import InstanceMode

    parts: list[str] = []
    needs_address = info.kind in ("gear_standard", "device_standard", "device_instance", "device_feature")
    if needs_address:
        parts.append("A5")
    if info.instance_mode in (InstanceMode.REQUIRED, InstanceMode.OPTIONAL):
        parts.append("I0")
    if info.kind == "gear_commissioning":
        # All three commissioning specials accept the A<n> form.
        parts.append("A5")
    if info.needs_data:
        parts.append("7")
    if not parts:
        return name
    return f"{name}({', '.join(parts)})"


class TestFormatCommandExpression:
    """`format_command_expression` renders a decoded command in the same
    `Name(A<n>, data)` syntax the RPC accepts. It is total: registry commands
    render their `Name(...)` expression, DALI 2 events render the same
    `Name(A<n>, I<n>, ...)` token form, and anything else falls back to
    python-dali's `str()` — it never returns None."""

    @pytest.mark.parametrize(
        "command, expected",
        [
            (Off(GearShort(5)), "Off(A5)"),
            (Off(GearGroup(3)), "Off(G3)"),
            (Off(GearBroadcast()), "Off"),
            (DAPC(GearShort(5), 100), "DAPC(A5, 100)"),
            (DAPC(GearBroadcast(), 100), "DAPC(100)"),
            (DTR0(42), "DTR0(42)"),
            # DT-specific commands keep their registry prefix.
            (DT8Activate(GearShort(5)), "DT8.Activate(A5)"),
            # Device instance command renders the I<n> token.
            (EnableInstance(DeviceShort(3), InstanceNumber(2)), "FF24.EnableInstance(A3, I2)"),
        ],
    )
    def test_renders_expression(self, command, expected):
        assert format_command_expression(command) == expected

    @pytest.mark.parametrize(
        "event, expected",
        [
            # short address + instance number (Device/instance scheme)
            (ShortPress(short_address=3, instance_number=2), "ShortPress(A3, I2)"),
            # bare instance scheme — only the instance number is present
            (ButtonPressed(instance_number=5), "ButtonPressed(I5)"),
            # device group scheme renders the device group as G<n>
            (ShortPress(device_group=4), "ShortPress(G4)"),
            # instance group scheme gets its own IG<n> token
            (ShortPress(instance_group=7), "ShortPress(IG7)"),
            # events carrying data append it after the address tokens
            (LightEvent(short_address=1, instance_number=0, data=123), "LightEvent(A1, I0, 123)"),
            (
                UnknownEvent(instance_type=99, short_address=2, instance_number=1, data=5),
                "UnknownEvent(A2, I1, 5)",
            ),
            (
                AmbiguousInstanceType(short_address=2, instance_number=1, data=9),
                "AmbiguousInstanceType(A2, I1, 9)",
            ),
        ],
    )
    def test_renders_event_in_command_token_syntax(self, event, expected):
        """A decoded DALI 2 event renders as `Name(A<n>, I<n>, ...)` in the same
        `A`/`G`/`I` token convention as commands, instead of python-dali's
        `short_address=…, instance_number=…` repr. Each addressing scheme (short
        address, device group, instance group, bare instance) maps to its token,
        and event data is appended last."""
        assert format_command_expression(event) == expected

    def test_alias_resolves_to_longer_name(self):
        """A class registered under two aliases renders the longer, more
        descriptive one (`QueryEventFilterM` -> `…EightToFifteen`)."""
        command = QueryEventFilterM(DeviceShort(3), InstanceNumber(0))
        assert format_command_expression(command) == "FF24.QueryEventFilterEightToFifteen(A3, I0)"

    @pytest.mark.parametrize(
        "name",
        sorted(build_command_registry().keys()),
    )
    def test_every_registry_command_round_trips(self, registry, name):
        """For every command in the registry, parsing its canonical expression,
        formatting the decoded command, and parsing the result yields an
        identical frame (`as_integer` and length). This is the acceptance
        invariant for the totality/determinism contract (S1/S3)."""
        info = registry[name]
        command = parse_expression(_canonical_expression(name, info), registry)
        rendered = format_command_expression(command)
        rebuilt = parse_expression(rendered, registry)
        assert rebuilt.frame.as_integer == command.frame.as_integer
        assert len(rebuilt.frame) == len(command.frame)

    def test_program_short_address_renders_address_not_byte(self):
        """Gear `ProgramShortAddress(5)` renders the semantic address `A5`, not
        the encoded frame byte `11`."""
        assert format_command_expression(ProgramShortAddress(5)) == "ProgramShortAddress(A5)"

    def test_initialise_broadcast_form(self, registry):
        """Gear `Initialise` broadcast (`0x00` byte) renders without an argument
        and parses back to the same frame."""
        command = Initialise(broadcast=True)
        rendered = format_command_expression(command)
        assert rendered == "Initialise"
        assert parse_expression(rendered, registry).frame.as_integer == command.frame.as_integer

    def test_initialise_address_form(self, registry):
        """Gear `Initialise(A5)` carries the `(5<<1)|1` frame byte and round-trips
        through the `A<n>` form."""
        command = Initialise(address=5)
        rendered = format_command_expression(command)
        assert rendered == "Initialise(A5)"
        assert command.frame.as_integer & 0xFF == (5 << 1) | 1
        assert parse_expression(rendered, registry).frame.as_integer == command.frame.as_integer

    def test_initialise_no_short_address_form(self, registry):
        """Gear `Initialise` with no short address (`0xff` byte) renders the
        `no_short_address` literal and round-trips."""
        command = Initialise(address=None)
        rendered = format_command_expression(command)
        assert rendered == "Initialise(no_short_address)"
        assert command.frame.as_integer & 0xFF == 0xFF
        assert parse_expression(rendered, registry).frame.as_integer == command.frame.as_integer

    def test_short_address_no_short_address_form(self, registry):
        """`ProgramShortAddress`/`VerifyShortAddress` "no address" (`MASK`, `0xff`
        byte) render the `no_short_address` literal and round-trip."""
        for command in (ProgramShortAddress("MASK"), VerifyShortAddress("MASK")):
            rendered = format_command_expression(command)
            assert rendered == f"{type(command).__name__}(no_short_address)"
            assert command.frame.as_integer & 0xFF == 0xFF
            assert parse_expression(rendered, registry).frame.as_integer == command.frame.as_integer

    def test_device_commissioning_unchanged(self, registry):
        """Device-side `FF24.Initialise`/`ProgramShortAddress`/`VerifyShortAddress`
        keep their raw-byte `data` syntax and round-trip unchanged."""
        for name in ("FF24.Initialise", "FF24.ProgramShortAddress", "FF24.VerifyShortAddress"):
            command = parse_expression(f"{name}(255)", registry)
            rendered = format_command_expression(command)
            assert rendered == f"{name}(255)"
            assert parse_expression(rendered, registry).frame.as_integer == command.frame.as_integer

    def test_non_registry_command_falls_back_to_str(self):
        """A command whose type is not in the registry renders python-dali's
        `str()` rather than None, and the formatter does not raise."""
        command = UnknownGearCommand(ForwardFrame(16, (0x00, 0x01)))
        rendered = format_command_expression(command)
        assert rendered == str(command)
        assert rendered is not None

    def test_rendered_expression_round_trips(self, registry):
        """Whatever is rendered parses back to a frame identical to the original."""
        command = DAPC(GearShort(5), 100)
        expression = format_command_expression(command)
        rebuilt = parse_expression(expression, registry)
        assert rebuilt.frame.as_integer == command.frame.as_integer

    def test_broken_str_does_not_raise(self):
        """A non-registry object whose `__str__` returns non-string (so `str()`
        raises TypeError, as `dali.sequences.progress.__str__` does when it has
        neither a message nor a completed/size pair) renders a type tag instead
        of propagating — the canonical log path must never raise."""

        class BrokenStr:  # pylint: disable=too-few-public-methods
            # Deliberately broken: returning non-str makes `str()` raise
            # TypeError, reproducing python-dali's progress.__str__.
            def __str__(self):  # pylint: disable=invalid-str-returned
                return None  # type: ignore[return-value]

        rendered = format_command_expression(BrokenStr())
        assert rendered == "<BrokenStr>"


class TestLazyCommandExpression:
    """`LazyCommandExpression.__str__` delegates to `format_command_expression`,
    so it yields the RPC expression for a registry command and falls back to
    `str()` for a non-registry one — letting `%s` log args defer the cost."""

    def test_registry_command_renders_expression(self):
        assert str(LazyCommandExpression(Off(GearShort(5)))) == "Off(A5)"

    def test_non_registry_command_falls_back_to_str(self):
        command = UnknownGearCommand(ForwardFrame(16, (0x00, 0x01)))
        assert str(LazyCommandExpression(command)) == str(command)


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
        self.assertIn("Gear General", result)

    def test_query_marker_not_rendered(self):
        """User-facing output must not surface the internal `has_response`
        marker as `[query]`; the flag is still on the catalog entries for
        machine consumers (Bus/ListCommands) but the rendered CLI listing
        stays uncluttered.
        """
        result = list_commands(self.registry)
        self.assertNotIn("[query]", result)

    def test_data_required_marked(self):
        result = list_commands(self.registry)
        self.assertIn("(requires data)", result)

    def test_category_order(self):
        """The catalog header order matches user expectations: Gear General →
        Gear Special → DT<n> (numeric) → FF24 Device General → FF24 Device
        Special → FF24.DT<m> (numeric) → FF24.F<ft> (numeric).
        """
        import re

        result = list_commands(self.registry)
        # Headers now carry a parenthesised address/instance suffix
        # ("Gear General (gear: A0..63 ...):"); strip it so the order check
        # works on the bare category code+label.
        headers = [
            re.sub(r"\s*\(.*\)$", "", line[:-1])
            for line in result.splitlines()
            if line.endswith(":") and not line.startswith(" ")
        ]

        self.assertEqual(headers[0], "Gear General")
        self.assertEqual(headers[1], "Gear Special")

        # DT<n> gear categories follow Gear Special, in numeric order.
        dt_gear_idx = 2
        dt_numbers: list[int] = []
        while dt_gear_idx < len(headers) and re.match(r"^DT\d+ ", headers[dt_gear_idx]):
            dt_numbers.append(int(headers[dt_gear_idx].split(" ", 1)[0][2:]))
            dt_gear_idx += 1
        self.assertGreater(len(dt_numbers), 0, "expected DT<n> gear sections")
        self.assertEqual(dt_numbers, sorted(dt_numbers))

        # Next: FF24 Device General, FF24 Device Special.
        self.assertEqual(headers[dt_gear_idx], "FF24 Device General")
        self.assertEqual(headers[dt_gear_idx + 1], "FF24 Device Special")

        # Then FF24.DT<m> (numeric), then FF24.F<ft> (numeric).
        ff24_dt_numbers: list[int] = []
        i = dt_gear_idx + 2
        while i < len(headers) and headers[i].startswith("FF24.DT"):
            ff24_dt_numbers.append(int(headers[i].split(" ", 1)[0][len("FF24.DT") :]))
            i += 1
        self.assertGreater(len(ff24_dt_numbers), 0, "expected FF24.DT<m> sections")
        self.assertEqual(ff24_dt_numbers, sorted(ff24_dt_numbers))

        ff24_f_numbers: list[int] = []
        while i < len(headers) and headers[i].startswith("FF24.F"):
            ff24_f_numbers.append(int(headers[i].split(" ", 1)[0][len("FF24.F") :]))
            i += 1
        self.assertGreater(len(ff24_f_numbers), 0, "expected FF24.F<ft> sections")
        self.assertEqual(ff24_f_numbers, sorted(ff24_f_numbers))

    def test_no_duplicate_command_names_in_catalog(self):
        """Catalog is 1-to-1 with the registry — no dedup logic involved.
        Regression guard: if someone reintroduces a double-registration scheme
        (e.g. `Name` plus `Name.Ix`), this test catches it.
        """
        from wb.mqtt_dali.send_command import build_command_catalog

        catalog = build_command_catalog(self.registry)
        names = [entry.name for entry in catalog]
        self.assertEqual(len(names), len(set(names)), "duplicate command names in catalog")

    def test_special_section_has_full_set(self):
        """Gear Special and FF24 Device Special expose the full set of
        single-arg special commands from python-dali (not just DTR0/1/2
        + Terminate).
        """
        result = list_commands(self.registry)

        gear_section, _, _ = result.partition("\nDT1 ")
        # Category headers now carry an "(address-form; ...)" suffix; split on
        # the bare category code to stay agnostic of the exact header text.
        gear_special_section = gear_section.split("\nGear Special")[1]
        for expected in ("DTR0", "DTR1", "DTR2", "Terminate", "Initialise", "Randomise", "Compare"):
            self.assertIn(expected, gear_special_section, f"missing gear special: {expected}")

        ff24_section = result.split("\nFF24 Device Special")[1].split("\nFF24.")[0]
        for expected in (
            "FF24.DTR0",
            "FF24.DTR1",
            "FF24.DTR2",
            "FF24.Terminate",
            "FF24.Initialise",
            "FF24.Randomise",
            "FF24.Compare",
        ):
            self.assertIn(expected, ff24_section, f"missing device special: {expected}")

        # Each Special section should have more than 5 entries (the user's
        # explicit ask — full set, not just the bare minimum).
        gear_special_count = sum(
            1 for line in gear_special_section.splitlines() if line.strip() and line.startswith("  ")
        )
        self.assertGreater(gear_special_count, 5)
        ff24_special_count = sum(
            1 for line in ff24_section.splitlines() if line.strip() and line.startswith("  ")
        )
        self.assertGreater(ff24_special_count, 5)

    def test_category_header_gear_general_shows_address_form(self):
        result = list_commands(self.registry)
        self.assertIn("Gear General (gear: A0..63 or G0..15; omit address for broadcast)", result)

    def test_category_header_gear_special_shows_no_address(self):
        result = list_commands(self.registry)
        self.assertIn("Gear Special (no address)", result)

    def test_category_header_ff24_dtm_shows_requires_instance(self):
        """Uniform-REQUIRED instance categories surface the instance form in
        the header so the user doesn't read it off every line."""
        result = list_commands(self.registry)
        expected = (
            "FF24.DT3 Occupancy Sensor "
            + "(device: A0..63 or G0..31; omit address for broadcast; requires I0..31)"
        )
        self.assertIn(expected, result)

    def test_category_header_ff24_f32_shows_instance_optional(self):
        """Uniform-OPTIONAL feature categories — Feedback today — note the
        optional `I<n>` slot in the header."""
        result = list_commands(self.registry)
        self.assertIn(
            "FF24.F32 Feedback (device: A0..63 or G0..31; omit address for broadcast; I<n> optional)",
            result,
        )

    def test_mixed_category_marks_individual_commands(self):
        """FF24 Device General mixes REQUIRED (instance-scoped) and DISALLOWED
        (device-scoped) commands — the header drops the instance form and the
        REQUIRED rows pick up a per-line marker instead.
        """
        result = list_commands(self.registry)

        ff24_general_section = result.split("FF24 Device General")[1].split("\nFF24 Device Special")[0]
        header_line = ff24_general_section.splitlines()[0]
        self.assertIn("device: A0..63 or G0..31; omit address for broadcast", header_line)
        self.assertNotIn("requires I", header_line)
        self.assertNotIn("I<n> optional", header_line)

        enable_lines = [line for line in ff24_general_section.splitlines() if "FF24.EnableInstance" in line]
        self.assertEqual(len(enable_lines), 1)
        self.assertIn("requires I<n>", enable_lines[0])


if __name__ == "__main__":
    unittest.main()
