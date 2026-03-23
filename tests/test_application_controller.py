from types import SimpleNamespace

from wb.mqtt_dali.application_controller import ApplicationController


class TestApplicationControllerVirtualGroups:
    def test_get_active_group_numbers(self):
        controller = ApplicationController.__new__(ApplicationController)
        controller.dali_devices = [
            SimpleNamespace(groups=[True, False, True, False]),
            SimpleNamespace(groups=[False, True, False, False]),
            SimpleNamespace(groups=[False, False, True, False]),
        ]

        assert getattr(controller, "_get_active_group_numbers")() == [0, 1, 2]
