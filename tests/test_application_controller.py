from types import SimpleNamespace

from wb.mqtt_dali.application_controller import ApplicationController


class TestApplicationControllerVirtualGroups:
    def test_get_active_group_numbers(self):
        controller = ApplicationController.__new__(ApplicationController)
        controller.dali_devices = [
            SimpleNamespace(groups=set([0, 2])),
            SimpleNamespace(groups=set([1])),
            SimpleNamespace(groups=set([2])),
        ]

        assert getattr(controller, "_get_active_group_numbers")() == [0, 1, 2]
