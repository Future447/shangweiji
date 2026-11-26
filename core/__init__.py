"""
核心逻辑层：协议解析、串口/TCP worker、通道处理、标定、记录、配置。
"""
from .protocol_parser import FrameParser, MAX_RX_BUFFER
from .serial_worker import SerialWorker, list_serial_ports
from .tcp_worker import (
    DEFAULT_DATA_TIMEOUT_S,
    DEFAULT_LISTEN_HOST,
    DEFAULT_LISTEN_PORT,
    TcpServerWorker,
    format_endpoint,
)
from .channel_processor import ChannelProcessor
from .calibration_manager import CalibrationManager
from .recorder import Recorder, build_csv_filename, sanitize_filename
from .config_manager import ConfigManager
from .demo_source import DemoSource

__all__ = [
    "FrameParser",
    "MAX_RX_BUFFER",
    "SerialWorker",
    "list_serial_ports",
    "TcpServerWorker",
    "format_endpoint",
    "DEFAULT_LISTEN_HOST",
    "DEFAULT_LISTEN_PORT",
    "DEFAULT_DATA_TIMEOUT_S",
    "ChannelProcessor",
    "CalibrationManager",
    "Recorder",
    "build_csv_filename",
    "sanitize_filename",
    "ConfigManager",
    "DemoSource",
]
