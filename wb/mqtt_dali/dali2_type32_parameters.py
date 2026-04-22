# Type 32 feedback

from typing import List

from dali.address import InstanceNumber

from .dali2_parameters import InstanceParam
from .device.feedback import (
    QueryActiveFeedbackBrightness,
    QueryActiveFeedbackColour,
    QueryActiveFeedbackPitch,
    QueryActiveFeedbackVolume,
    QueryFeedbackTiming,
    QueryInactiveFeedbackBrightness,
    QueryInactiveFeedbackColour,
    SetActiveFeedbackBrightness,
    SetActiveFeedbackColour,
    SetActiveFeedbackPitch,
    SetActiveFeedbackVolume,
    SetFeedbackTiming,
    SetInactiveFeedbackBrightness,
    SetInactiveFeedbackColour,
)
from .settings import SettingsParamName


class ActiveFeedbackBrightnessParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Active feedback brightness", "Яркость активной обратной связи"),
            "active_feedback_brightness",
            instance_number,
            QueryActiveFeedbackBrightness,
            SetActiveFeedbackBrightness,
        )
        self.grid_columns = 3
        self.property_order = 10


class ActiveFeedbackColourParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Active feedback colour", "Цвет активной обратной связи"),
            "active_feedback_colour",
            instance_number,
            QueryActiveFeedbackColour,
            SetActiveFeedbackColour,
        )
        self.grid_columns = 3
        self.property_order = 11


class ActiveFeedbackPitchParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Active feedback pitch", "Тон активной обратной связи"),
            "active_feedback_pitch",
            instance_number,
            QueryActiveFeedbackPitch,
            SetActiveFeedbackPitch,
        )
        self.grid_columns = 3
        self.property_order = 12


class ActiveFeedbackVolumeParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Active feedback volume", "Громкость активной обратной связи"),
            "active_feedback_volume",
            instance_number,
            QueryActiveFeedbackVolume,
            SetActiveFeedbackVolume,
        )
        self.grid_columns = 3
        self.property_order = 13


class FeedbackTimingParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Feedback timing", "Тайминг обратной связи"),
            "feedback_timing",
            instance_number,
            QueryFeedbackTiming,
            SetFeedbackTiming,
        )
        self.grid_columns = 3
        self.property_order = 14


class InactiveFeedbackBrightnessParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Inactive feedback brightness", "Яркость неактивной обратной связи"),
            "inactive_feedback_brightness",
            instance_number,
            QueryInactiveFeedbackBrightness,
            SetInactiveFeedbackBrightness,
        )
        self.grid_columns = 3
        self.property_order = 15


class InactiveFeedbackColourParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Inactive feedback colour", "Цвет неактивной обратной связи"),
            "inactive_feedback_colour",
            instance_number,
            QueryInactiveFeedbackColour,
            SetInactiveFeedbackColour,
        )
        self.grid_columns = 3
        self.property_order = 16


def build_type32_feedback_parameters(instance_number: InstanceNumber) -> List[InstanceParam]:
    return [
        ActiveFeedbackBrightnessParam(instance_number),
        ActiveFeedbackColourParam(instance_number),
        ActiveFeedbackPitchParam(instance_number),
        ActiveFeedbackVolumeParam(instance_number),
        FeedbackTimingParam(instance_number),
        InactiveFeedbackBrightnessParam(instance_number),
        InactiveFeedbackColourParam(instance_number),
    ]
