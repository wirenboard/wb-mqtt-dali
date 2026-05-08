import importlib.util
import os

from dali.address import GearBroadcast, GearGroup, GearShort
from dali.frame import BackwardFrame, ForwardFrame
from dali.gear.general import DAPC, QueryActualLevel

from wb.mqtt_dali.bus_traffic import BusTrafficItem, BusTrafficSource


def _load_e2e_module():
    # The e2e script is not part of the wb.mqtt_dali package, so we load it by path.
    module_path = os.path.join(os.path.dirname(__file__), "..", "e2e", "on_latency", "e2e_on_latency.py")
    spec = importlib.util.spec_from_file_location("e2e_on_latency", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_e2e = _load_e2e_module()
is_target_dapc_frame = _e2e.is_target_dapc_frame

TARGET_SHORT = 5
OTHER_SHORT = 7


def _bus_item(frame, source: BusTrafficSource = BusTrafficSource.BUS) -> BusTrafficItem:
    return BusTrafficItem(request=frame, response=None, request_source=source, frame_counter=0)


class TestIsTargetDapcFrame:
    def test_match_picks_dapc_with_expected_level(self):
        frame = DAPC(GearShort(TARGET_SHORT), 42).frame
        assert is_target_dapc_frame(_bus_item(frame), TARGET_SHORT, 42) is True

    def test_match_skips_other_levels(self):
        frame = DAPC(GearShort(TARGET_SHORT), 100).frame
        assert is_target_dapc_frame(_bus_item(frame), TARGET_SHORT, 42) is False

    def test_match_skips_other_short_addresses(self):
        frame = DAPC(GearShort(OTHER_SHORT), 42).frame
        assert is_target_dapc_frame(_bus_item(frame), TARGET_SHORT, 42) is False

    def test_match_skips_broadcast(self):
        frame = DAPC(GearBroadcast(), 42).frame
        assert is_target_dapc_frame(_bus_item(frame), TARGET_SHORT, 42) is False

    def test_match_skips_group(self):
        frame = DAPC(GearGroup(0), 42).frame
        assert is_target_dapc_frame(_bus_item(frame), TARGET_SHORT, 42) is False

    def test_match_skips_query_command(self):
        frame = QueryActualLevel(GearShort(TARGET_SHORT)).frame
        assert is_target_dapc_frame(_bus_item(frame), TARGET_SHORT, 42) is False

    def test_match_skips_wb_source(self):
        frame = DAPC(GearShort(TARGET_SHORT), 42).frame
        item = _bus_item(frame, BusTrafficSource.WB)
        assert is_target_dapc_frame(item, TARGET_SHORT, 42) is False

    def test_match_skips_backward_frame(self):
        item = _bus_item(BackwardFrame(0xFE))
        assert is_target_dapc_frame(item, TARGET_SHORT, 42) is False

    def test_match_skips_broken_frame(self):
        frame = DAPC(GearShort(TARGET_SHORT), 42).frame
        frame._error = True  # pylint: disable=protected-access
        assert is_target_dapc_frame(_bus_item(frame), TARGET_SHORT, 42) is False

    def test_match_skips_non_16bit_frame(self):
        assert is_target_dapc_frame(_bus_item(ForwardFrame(24, 0)), TARGET_SHORT, 42) is False
