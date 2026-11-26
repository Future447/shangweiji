"""
TCP 监听线程 + 主窗口集成的验收测试。

覆盖任务书要求的验证项：
- 完整帧 / 半帧（多次发送）/ 多帧合并 / D 帧 / E 帧
- 客户端断开重连（监听保持、跨连接不拼半帧）
- 已连接但长时间无数据（数据超时 ≠ 断线）
- 单设备策略（第二台设备被明确拒绝）
- 停止监听后端口释放、可重新监听
- 旧配置（只有 last_serial）兼容加载

全部为**模拟验证**（本机 loopback socket + 模拟客户端），
不涉及 Hi3863 / STM32 硬件。
"""

import json
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest

from PyQt5.QtCore import QCoreApplication
from PyQt5.QtWidgets import QApplication

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.protocol_parser import FrameKind, FrameParser  # noqa: E402
from core.tcp_worker import TcpServerWorker  # noqa: E402


def pump(seconds: float = 0.05) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.005)


def wait_until(pred, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    QCoreApplication.processEvents()
    return bool(pred())


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def d_frame(scan: int, samples=None, valid_mask: int = 0xFFFF) -> bytes:
    vals = samples or [1000.0] * 16
    parts = ["D", str(scan), "0", str(valid_mask), "0", "mV"]
    parts.extend(f"{v:.3f}" for v in vals)
    return (",".join(parts) + "\r\n").encode("ascii")


def e_frame(status=0, inits=1, recovers=0, errs=0) -> bytes:
    return (
        f"E,INIT,{status},{inits},{recovers},{errs}\r\n"
    ).encode("ascii")


class TestTcpServerWorker(unittest.TestCase):
    """TCP 监听线程的行为（纯模拟）。"""

    def setUp(self) -> None:
        QApplication.instance() or QApplication(sys.argv)
        self.port = free_port()
        self.events = {
            "listening": [],
            "connected": [],
            "data": [],
            "lost": 0,
            "rejected": [],
            "timeout": [],
            "resumed": 0,
            "error": [],
            "stopped": 0,
        }
        self.worker = TcpServerWorker(
            "127.0.0.1", self.port, data_timeout_s=0.6
        )
        self.worker.listening.connect(
            lambda s: self.events["listening"].append(s))
        self.worker.connected.connect(
            lambda s: self.events["connected"].append(s))
        self.worker.data_received.connect(
            lambda b: self.events["data"].append(b))
        self.worker.connection_lost.connect(self._on_lost)
        self.worker.client_rejected.connect(
            lambda s: self.events["rejected"].append(s))
        self.worker.data_timeout.connect(
            lambda v: self.events["timeout"].append(v))
        self.worker.data_resumed.connect(self._on_resumed)
        self.worker.error_occurred.connect(
            lambda s: self.events["error"].append(s))
        self.worker.stopped.connect(self._on_stopped)
        self.worker.start()
        self.assertTrue(
            wait_until(lambda: self.events["listening"]),
            "监听未能启动",
        )
        self.clients = []

    def tearDown(self) -> None:
        for c in self.clients:
            try:
                c.close()
            except OSError:
                pass
        try:
            self.worker.stop()
        except Exception:
            pass
        pump(0.05)

    # -- 回调（可能在 worker 线程执行，只做计数/追加）--
    def _on_lost(self) -> None:
        self.events["lost"] += 1

    def _on_resumed(self) -> None:
        self.events["resumed"] += 1

    def _on_stopped(self) -> None:
        self.events["stopped"] += 1

    # -- 工具 --
    def connect_client(self) -> socket.socket:
        c = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        self.clients.append(c)
        self.assertTrue(
            wait_until(lambda: len(self.events["connected"]) >= 1),
            "客户端连接未被识别",
        )
        return c

    def payload(self) -> bytes:
        return b"".join(self.events["data"])

    # ------------------------------------------------------------------
    def test_listening_is_not_connected(self):
        """开始监听不得被当成设备已连接。"""
        self.assertTrue(self.events["listening"])
        self.assertEqual(self.events["connected"], [])
        # 监听地址应包含端口
        self.assertIn(str(self.port), self.events["listening"][0])

    def test_full_frame_passthrough(self):
        c = self.connect_client()
        data = d_frame(1)
        c.sendall(data)
        self.assertTrue(wait_until(lambda: len(self.payload()) >= len(data)))
        self.assertEqual(self.payload(), data)
        # 原始字节可直接被现有解析器消费
        parser = FrameParser()
        frames = parser.feed(self.payload())
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].kind, FrameKind.D)
        self.assertEqual(frames[0].d_frame.scan_number, 1)

    def test_half_frame_split(self):
        """半帧不产生解析结果，补齐后才出完整帧。"""
        c = self.connect_client()
        data = d_frame(7)
        parser = FrameParser()
        mid = len(data) // 2
        c.sendall(data[:mid])
        self.assertTrue(wait_until(lambda: len(self.payload()) >= mid))
        self.assertEqual(parser.feed(self.payload()), [])
        c.sendall(data[mid:])
        self.assertTrue(wait_until(lambda: len(self.payload()) >= len(data)))
        frames = parser.feed(self.payload()[mid:])
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].d_frame.scan_number, 7)

    def test_multi_frame_batch(self):
        """一次 send 里多帧合并（粘包）→ 解析出多帧。"""
        c = self.connect_client()
        blob = b"".join(d_frame(i) for i in (1, 2, 3))
        c.sendall(blob)
        self.assertTrue(wait_until(lambda: len(self.payload()) >= len(blob)))
        parser = FrameParser()
        frames = parser.feed(self.payload())
        self.assertEqual([f.d_frame.scan_number for f in frames], [1, 2, 3])

    def test_e_frame(self):
        c = self.connect_client()
        data = e_frame(status=1, inits=2, recovers=1, errs=5)
        c.sendall(data)
        self.assertTrue(wait_until(lambda: len(self.payload()) >= len(data)))
        frames = FrameParser().feed(self.payload())
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].kind, FrameKind.E)
        self.assertEqual(frames[0].e_frame.init_status, 1)

    def test_disconnect_then_reconnect(self):
        """设备断开后继续监听，允许自动重连。"""
        c = self.connect_client()
        c.sendall(d_frame(1))
        self.assertTrue(wait_until(lambda: self.payload()))
        c.close()
        self.assertTrue(wait_until(lambda: self.events["lost"] >= 1))

        c2 = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        self.clients.append(c2)
        self.assertTrue(
            wait_until(lambda: len(self.events["connected"]) >= 2),
            "重连未被接受",
        )
        c2.sendall(d_frame(2))
        self.assertTrue(
            wait_until(lambda: b"2,0,65535,0,mV" in self.payload())
        )

    def test_second_client_rejected(self):
        """单设备策略：第二台设备被拒绝，第一台不受影响。"""
        c1 = self.connect_client()
        c1.sendall(d_frame(1))
        self.assertTrue(wait_until(lambda: self.payload()))

        c2 = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        self.clients.append(c2)
        self.assertTrue(
            wait_until(lambda: self.events["rejected"]),
            "第二台设备未被拒绝",
        )
        self.assertEqual(len(self.events["connected"]), 1)

        # 第一台设备仍可继续发数据
        before = len(self.payload())
        c1.sendall(d_frame(2))
        self.assertTrue(wait_until(lambda: len(self.payload()) > before))
        self.assertIn(b"2,0,65535,0,mV", self.payload())

    def test_idle_timeout_is_not_disconnect(self):
        """读取超时只上报数据超时，不断开连接。"""
        c = self.connect_client()
        self.assertTrue(
            wait_until(lambda: self.events["timeout"], timeout=3.0),
            "未上报数据超时",
        )
        self.assertEqual(self.events["lost"], 0)
        c.sendall(d_frame(1))
        self.assertTrue(wait_until(lambda: self.events["resumed"]))
        self.assertEqual(self.events["lost"], 0)

    def test_stop_releases_port(self):
        self.worker.stop()
        self.assertTrue(wait_until(lambda: self.events["stopped"]))
        # 端口可被重新绑定 = 资源确实释放
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", self.port))
        finally:
            s.close()


class TestMainWindowTcpIntegration(unittest.TestCase):
    """主窗口在 TCP 模式下的行为（离屏，纯模拟）。"""

    def setUp(self) -> None:
        from core.config_manager import ConfigManager
        import ui.main_window as mw_mod

        self._tmp = tempfile.mkdtemp(prefix="tcpint_")
        self._cfg_path = os.path.join(self._tmp, "config.json")
        self._orig_cm = mw_mod.ConfigManager
        mw_mod.ConfigManager = lambda: ConfigManager(  # type: ignore
            config_path=self._cfg_path
        )
        import app_paths
        self._orig_csv = app_paths.get_default_csv_dir
        app_paths.get_default_csv_dir = lambda: self._tmp  # type: ignore

        QApplication.instance() or QApplication(sys.argv)
        self.win = mw_mod.MainWindow()
        self.win.show()
        pump(0.05)
        self.port = free_port()
        self.clients = []

    def tearDown(self) -> None:
        import app_paths
        import ui.main_window as mw_mod

        for c in self.clients:
            try:
                c.close()
            except OSError:
                pass
        try:
            self.win._refresh_timer.stop()
        except Exception:
            pass
        try:
            self.win._stop_all_sources()
        except Exception:
            pass
        try:
            self.win.close()
        except Exception:
            pass
        mw_mod.ConfigManager = self._orig_cm  # type: ignore
        app_paths.get_default_csv_dir = self._orig_csv  # type: ignore
        pump(0.05)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _start_listening(self) -> None:
        from ui.main_window import SourceState, TRANSPORT_TCP

        self.win._transport = TRANSPORT_TCP
        self.win._update_transport_ui()
        self.win._edt_listen_host.setText("127.0.0.1")
        self.win._sp_listen_port.setValue(self.port)
        self.win._on_connect_clicked()
        self.assertTrue(
            wait_until(
                lambda: self.win._source_state == SourceState.LISTENING
            ),
            "未进入监听状态",
        )

    def _connect(self):
        c = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        self.clients.append(c)
        return c

    def test_listening_state_label(self):
        from ui.main_window import SourceState

        self._start_listening()
        self.assertEqual(self.win._source_state, SourceState.LISTENING)
        text = self.win._lbl_state.text()
        self.assertIn("监听中", text)
        self.assertNotIn("设备已连接", text)

    def test_frames_reach_pipeline(self):
        from ui.main_window import SourceState

        self._start_listening()
        c = self._connect()
        self.assertTrue(
            wait_until(
                lambda: self.win._source_state == SourceState.SERIAL_CONNECTED
            )
        )
        c.sendall(d_frame(11))
        self.assertTrue(wait_until(lambda: self.win._frame_count >= 1))
        self.assertEqual(self.win._last_dframe.scan_number, 11)
        # 会话来源标记为 tcp（CSV 元数据可追溯）
        self.assertEqual(self.win._session_source_name(), "tcp")

    def test_reconnect_does_not_splice_half_frame(self):
        """跨连接残留半帧必须被丢弃，不得与新连接的数据拼接成帧。"""
        from ui.main_window import SourceState

        self._start_listening()
        c = self._connect()
        self.assertTrue(
            wait_until(
                lambda: self.win._source_state == SourceState.SERIAL_CONNECTED
            )
        )
        data = d_frame(21)
        c.sendall(data[:20])  # 半帧后断开
        pump(0.2)
        c.close()
        self.assertTrue(
            wait_until(
                lambda: self.win._source_state == SourceState.LISTENING
            )
        )
        frames_before = self.win._frame_count

        c2 = self._connect()
        self.assertTrue(
            wait_until(
                lambda: self.win._source_state == SourceState.SERIAL_CONNECTED
            )
        )
        c2.sendall(data[20:])  # 只发剩下的一半
        pump(0.5)
        self.assertEqual(self.win._frame_count, frames_before)
        self.assertGreaterEqual(self.win._error_count, 1)

    def test_device_disconnect_keeps_listening(self):
        from ui.main_window import SourceState

        self._start_listening()
        c = self._connect()
        self.assertTrue(
            wait_until(
                lambda: self.win._source_state == SourceState.SERIAL_CONNECTED
            )
        )
        c.sendall(d_frame(1))
        self.assertTrue(wait_until(lambda: self.win._frame_count >= 1))
        c.close()
        self.assertTrue(
            wait_until(
                lambda: self.win._source_state == SourceState.LISTENING
            ),
            "断线后未回到监听态",
        )
        self.assertEqual(self.win._tcp_peer, "")

    def test_stop_listening_releases_port(self):
        from ui.main_window import SourceState

        self._start_listening()
        self.win._on_disconnect_clicked()
        self.assertTrue(
            wait_until(
                lambda: self.win._source_state == SourceState.DISCONNECTED
            )
        )
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", self.port))
        finally:
            s.close()

    def test_config_roundtrip_and_legacy_compat(self):
        """旧配置（仅 last_serial）加载后：串口参数保留、TCP 参数取默认。"""
        from core.config_manager import ConfigManager

        legacy = os.path.join(self._tmp, "legacy.json")
        with open(legacy, "w", encoding="utf-8") as f:
            json.dump({"version": 4, "last_serial": {
                "port": "COM9", "baudrate": 921600}}, f)
        cfg = ConfigManager(config_path=legacy)
        self.assertEqual(cfg.last_serial["port"], "COM9")
        self.assertEqual(cfg.last_serial["baudrate"], 921600)
        self.assertEqual(cfg.transport, "serial")
        self.assertEqual(cfg.tcp_listen_port, 8080)
        self.assertEqual(cfg.tcp_listen_host, "0.0.0.0")

        # 写入 TCP 参数后不破坏旧串口配置
        cfg.transport = "tcp"
        cfg.tcp_listen_host = "127.0.0.1"
        cfg.tcp_listen_port = 9001
        cfg.device_id = "EDDY-01"
        ok, err = cfg.save()
        self.assertTrue(ok, err)
        cfg2 = ConfigManager(config_path=legacy)
        self.assertEqual(cfg2.transport, "tcp")
        self.assertEqual(cfg2.tcp_listen_host, "127.0.0.1")
        self.assertEqual(cfg2.tcp_listen_port, 9001)
        self.assertEqual(cfg2.device_id, "EDDY-01")
        self.assertEqual(cfg2.last_serial["port"], "COM9")

    def test_tcp_baseline_scope_isolated_from_serial(self):
        """TCP 与串口基线互不套用（切换链路不静默沿用旧基线）。"""
        from core.config_manager import SCOPE_SERIAL, SCOPE_TCP

        cfg = self.win._config
        cfg.set_baselines(SCOPE_SERIAL, {0: 1000.0},
                          {"device_id": "EDDY-01"})
        bl_tcp = cfg.get_baselines(SCOPE_TCP, "EDDY-01")
        self.assertTrue(all(v is None for v in bl_tcp.values()))
        bl_serial = cfg.get_baselines(SCOPE_SERIAL, "EDDY-01")
        self.assertAlmostEqual(bl_serial[0], 1000.0)


if __name__ == "__main__":
    unittest.main()
