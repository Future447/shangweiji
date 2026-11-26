"""
模型层 - 数据类与不可变结构。
v2 固件相关：单帧、通道状态、ADC Profile。
"""
from .frame import (
    DFrame,
    EFrame,
    parse_d_frame,
    parse_e_frame,
    FrameParseError,
    MAX_CHANNEL_COUNT,
    RECEIVER_STATUS_NAMES,
)
from .channel_state import ChannelState, ChannelStatus
from .adc_profile import AdcProfile, build_default_profiles, profile_by_name

__all__ = [
    "DFrame",
    "EFrame",
    "parse_d_frame",
    "parse_e_frame",
    "FrameParseError",
    "MAX_CHANNEL_COUNT",
    "RECEIVER_STATUS_NAMES",
    "ChannelState",
    "ChannelStatus",
    "AdcProfile",
    "build_default_profiles",
    "profile_by_name",
]
