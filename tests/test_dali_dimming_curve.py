import pytest

from wb.mqtt_dali.dali_controls import WantedLevelControl
from wb.mqtt_dali.dali_dimming_curve import DimmingCurveState, DimmingCurveType


class TestDimmingCurveStateGetLevel:
    def setup_method(self):
        self.state = DimmingCurveState()  # pylint: disable=attribute-defined-outside-init

    def test_zero_register_returns_zero(self):
        assert self.state.get_level(0) == 0.0

    def test_negative_register_returns_zero(self):
        assert self.state.get_level(-1) == 0.0

    def test_max_register_returns_100(self):
        assert self.state.get_level(254) == 100.0

    def test_above_max_register_returns_100(self):
        assert self.state.get_level(255) == 100.0

    def test_min_logarithmic(self):
        assert self.state.get_level(1) == 0.1

    def test_linear_mid_value(self):
        self.state.curve_type = DimmingCurveType.LINEAR
        assert self.state.get_level(127) == 50.0


class TestDimmingCurveStateGetRawValue:
    def setup_method(self):
        self.state = DimmingCurveState()  # pylint: disable=attribute-defined-outside-init

    def test_zero_returns_zero(self):
        assert self.state.get_raw_value(0.0) == 0

    def test_negative_returns_zero(self):
        assert self.state.get_raw_value(-5.0) == 0

    def test_100_returns_254(self):
        assert self.state.get_raw_value(100.0) == 254

    def test_above_100_returns_254(self):
        assert self.state.get_raw_value(150.0) == 254

    def test_linear_mid_value(self):
        self.state.curve_type = DimmingCurveType.LINEAR
        assert self.state.get_raw_value(50.0) == 127

    def test_logarithmic_min(self):
        assert self.state.get_raw_value(0.1) == 1

    def test_logarithmic_below_min_clamped(self):
        """Level below 0.1% should clamp to raw=1, not go negative."""
        assert self.state.get_raw_value(0.05) == 1
        assert self.state.get_raw_value(0.001) == 1

    def test_logarithmic_roundtrip(self):
        """get_raw_value should be the inverse of get_level for all DALI values."""
        for raw in range(1, 254):
            level = self.state.get_level(raw)
            assert self.state.get_raw_value(level) == raw, f"Roundtrip failed for raw={raw}, level={level}"

    def test_linear_roundtrip(self):
        """get_raw_value should be the inverse of get_level for linear curve."""
        self.state.curve_type = DimmingCurveType.LINEAR
        for raw in range(1, 254):
            level = self.state.get_level(raw)
            assert self.state.get_raw_value(level) == raw, f"Roundtrip failed for raw={raw}, level={level}"


class TestWantedLevelControlValidation:
    def setup_method(self):
        self.state = DimmingCurveState()  # pylint: disable=attribute-defined-outside-init

    def test_nan_rejected(self):
        control = WantedLevelControl(self.state)
        with pytest.raises(ValueError):
            control.get_setup_commands(None, "nan")

    def test_inf_rejected(self):
        control = WantedLevelControl(self.state)
        with pytest.raises(ValueError):
            control.get_setup_commands(None, "inf")

    def test_negative_rejected(self):
        control = WantedLevelControl(self.state)
        with pytest.raises(ValueError):
            control.get_setup_commands(None, "-1")

    def test_over_100_rejected(self):
        control = WantedLevelControl(self.state)
        with pytest.raises(ValueError):
            control.get_setup_commands(None, "101")
