"""
串口读取工作线程。

设计：
- 复用 v2 协议不可知性：v2 固件**不**响应任何命令，所以本工作线程
  是纯读取 + Qt 信号发射。
- 线程安全停止：threading.Event
- 串口读取/打开异常统一通过 error_occurred 信号报告
- 拆包：每次 read() 后整块喂给 FrameParser.feed()
- 主动重连：UI 端决定何时重新 start
"""

from __future__ import annotations

import threading
import time
from typing import List, Optional

import serial
import serial.tools.list_ports
from PyQt5.QtCore import QThread, pyqtSignal


def list_serial_ports() -> List[tuple]:
    """枚举当前可用串口，返回 [(device, description), ...]。"""
    return [
        (p.device, p.description)
        for p in serial.tools.list_ports.comports()
    ]


def list_serial_ports_detailed() -> List[tuple]:
    """枚举当前可用串口，返回 [(device, description, device_id), ...]。

    device_id 取 pyserial 的 hwid（含 USB VID:PID 与序列号），用于
    判断"是否同一台设备"——不只看 COM 端口号（同一设备换口会换 COM，
    不同设备也可能先后占用同一 COM）。无 hwid 时退化为 device 名。
    """
    out = []
    for p in serial.tools.list_ports.comports():
        hwid = (p.hwid or "").strip()
        out.append((p.device, p.description, hwid or p.device))
    return out


def device_id_for_port(port: str) -> str:
    """查指定串口设备的标识（hwid）；找不到时返回空串（未知）。"""
    for device, _desc, dev_id in list_serial_ports_detailed():
        if device == port:
            return dev_id
    return ""


class SerialWorker(QThread):
    """
    串口读取 worker。

    信号：
      connected(port)        - 串口成功打开
      data_received(bytes)   - 原始字节
      error_occurred(str)    - 打开 / 读取错误
      connection_lost()      - 串口被拔出 / 异常断开
      stopped()              - 线程结束（正常或异常）
    """

    connected = pyqtSignal(str)
    data_received = pyqtSignal(bytes)
    error_occurred = pyqtSignal(str)
    connection_lost = pyqtSignal()
    stopped = pyqtSignal()

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        read_chunk_timeout: float = 0.05,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._port = port
        self._baudrate = baudrate
        self._stop_event = threading.Event()
        self._serial: Optional[serial.Serial] = None
        self._connection_lost_reported = False

    def stop(self) -> None:
        """请求线程退出。"""
        self._stop_event.set()
        try:
            if self._serial and self._serial.is_open:
                self._serial.cancel_read()
        except Exception:
            pass
        self.wait(3000)

    def run(self) -> None:
        if self._stop_event.is_set():
            self.stopped.emit()
            return

        try:
            self._serial = serial.Serial(
                port=self._port,
                baudrate=self._baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.05,
                write_timeout=1.0,
            )
        except (serial.SerialException, OSError) as e:
            self.error_occurred.emit(f"无法打开串口 {self._port}: {e}")
            self.stopped.emit()
            return

        if self._stop_event.is_set():
            self._close()
            self.stopped.emit()
            return

        self.connected.emit(self._port)

        while not self._stop_event.is_set():
            try:
                waiting = (
                    self._serial.in_waiting
                    if self._serial and self._serial.is_open
                    else 0
                )
                if waiting > 0:
                    chunk = self._serial.read(waiting)
                    if chunk and not self._stop_event.is_set():
                        self.data_received.emit(chunk)
                else:
                    self._stop_event.wait(0.01)
            except (serial.SerialException, OSError):
                if not self._stop_event.is_set():
                    self._report_lost()
                break

        self._close()
        self.stopped.emit()

    # ------------------------------------------------------------------
    def _report_lost(self) -> None:
        if not self._connection_lost_reported:
            self._connection_lost_reported = True
            self.connection_lost.emit()

    def _close(self) -> None:
        try:
            if self._serial and self._serial.is_open:
                self._serial.close()
        except Exception:
            pass
