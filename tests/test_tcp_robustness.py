"""
TCP 链路的健壮性行为测试（真实 loopback socket，不直接调 UI 槽充数）。

覆盖任务书要求的行为：
1. 正常收帧 → 断线 → 重连后持续乱码：本连接段内必须在阈值内报"协议不匹配"；
2. 重连后只有 E 故障心跳、随后恢复 D 帧；
3. 标定进行中断线：立即终止（不得跨连接段累计）；
4. 扫描号缺口/重复/重启/回绕分类（缺口 ≠ 无线丢包率）；
5. 设备编号：匹配/不匹配/为空时的基线应用与可见提示，在线编辑被锁定；
6. 录制跨断线：分文件、CSV 元数据与时间轴可追溯；
7. 端口占用、停止后重启监听、关窗释放端口、第二客户端、串口回归。
"""

import csv
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from PyQt5.QtCore import QCoreApplication
from PyQt5.QtWidgets import QApplication

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.config_manager import SCOPE_TCP  # noqa: E402


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


def d_line(scan: int, samples=None, status: int = 0) -> bytes:
    vals = samples or [1000.0] * 16
    parts = ["D", str(scan), str(status), "65535", "0", "mV"]
    parts.extend(f"{v:.3f}" for v in vals)
    return (",".join(parts) + "\r\n").encode("ascii")


def e_line(status=1, inits=2, recovers=1, errs=3) -> bytes:
    return f"E,INIT,{status},{inits},{recovers},{errs}\r\n".encode("ascii")


class _TcpWindowBase(unittest.TestCase):
    """离屏主窗口 + 真实 TCP socket 的测试基类。"""

    def setUp(self) -> None:
        from core.config_manager import ConfigManager
        import ui.main_window as mw_mod

        self._tmp = tempfile.mkdtemp(prefix="tcprob_")
        self._cfg_path = os.path.join(self._tmp, "config.json")
        self._orig_cm = mw_mod.ConfigManager
        mw_mod.ConfigManager = lambda: ConfigManager(  # type: ignore
            config_path=self._cfg_path
        )
        import app_paths
        self._orig_csv = app_paths.get_default_csv_dir
        app_paths.get_default_csv_dir = lambda: self._tmp  # type: ignore

        self._msg_patch = patch(
            "ui.main_window.QMessageBox.warning", new=MagicMock()
        )
        self._msg_patch.start()

        QApplication.instance() or QApplication(sys.argv)
        self.win = mw_mod.MainWindow()
        self.win.show()
        pump(0.05)
        self.port = free_port()
        self.clients = []

    def tearDown(self) -> None:
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
            self.win._recorder.stop_recording()
        except Exception:
            pass
        try:
            self.win.close()
        except Exception:
            pass
        self._msg_patch.stop()
        import app_paths
        import ui.main_window as mw_mod
        mw_mod.ConfigManager = self._orig_cm  # type: ignore
        app_paths.get_default_csv_dir = self._orig_csv  # type: ignore
        pump(0.05)
        shutil.rmtree(self._tmp, ignore_errors=True)

    # -- 工具 -----------------------------------------------------------
    def listen(self, port: int = 0) -> int:
        from ui.main_window import SourceState, TRANSPORT_TCP
        port = port or self.port
        self.win._transport = TRANSPORT_TCP
        self.win._update_transport_ui()
        self.win._edt_listen_host.setText("127.0.0.1")
        self.win._sp_listen_port.setValue(port)
        self.win._on_connect_clicked()
        self.assertTrue(
            wait_until(
                lambda: self.win._source_state == SourceState.LISTENING
            ),
            "未进入监听状态",
        )
        return port

    def connect(self) -> socket.socket:
        from ui.main_window import SourceState
        c = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        self.clients.append(c)
        self.assertTrue(
            wait_until(
                lambda: self.win._source_state == SourceState.SERIAL_CONNECTED
            ),
            "设备未进入已连接状态",
        )
        return c

    def send_frames(self, c: socket.socket, scans) -> None:
        before = self.win._frame_count
        for s in scans:
            c.sendall(d_line(s))
        self.assertTrue(
            wait_until(
                lambda: self.win._frame_count >= before + len(list(scans))
            ),
            f"未收到 {list(scans)} 对应的帧",
        )


class TestLinkSegmentHealth(_TcpWindowBase):
    """要求 A：连接段级健康判断 + 首次/重连乱码都要能报错。"""

    def test_first_connection_garbage_reports_mismatch(self):
        """首次连接（尚未收过任何 D 帧）持续乱码也必须报协议不匹配。"""
        self.listen()
        c = self.connect()
        c.sendall(b"# diag only\r\n" * 4)
        self.assertTrue(wait_until(lambda: self.win._segment_byte_count > 0))
        self.assertEqual(self.win._segment_dframe_count, 0)
        self.win._segment_start_time -= 6.0
        self.win._session_start_time -= 6.0
        self.win._check_data_freshness()
        self.assertEqual(self.win._data_timeout_reason, "parse")
        self.win._on_refresh()
        self.assertEqual(self.win._lbl_state.text(), "协议不匹配")

    def test_reconnect_garbage_reports_mismatch(self):
        self.listen()
        c = self.connect()
        self.send_frames(c, [1, 2])
        self.assertEqual(self.win._segment_dframe_count, 2)

        c.close()
        from ui.main_window import SourceState
        self.assertTrue(
            wait_until(
                lambda: self.win._source_state == SourceState.LISTENING
            )
        )

        c2 = self.connect()
        # 新连接段：段计数必须清零（旧实现的累计帧数会掩盖问题）
        self.assertEqual(self.win._segment_dframe_count, 0)
        c2.sendall(b"garbage line one\r\n" * 3)
        self.assertTrue(
            wait_until(lambda: self.win._segment_byte_count > 0)
        )
        # 把段起点回拨，模拟已过阈值（数据确实经 socket 到达）
        self.win._segment_start_time -= 6.0
        self.win._session_start_time -= 6.0
        self.win._check_data_freshness()
        self.assertTrue(self.win._data_timeout_reported)
        self.assertEqual(self.win._data_timeout_reason, "parse")
        self.win._on_refresh()
        self.assertEqual(self.win._lbl_state.text(), "协议不匹配")

        # 合法 D 帧到达 → 自动解除
        c2.sendall(d_line(50))
        self.assertTrue(wait_until(lambda: self.win._frame_count >= 1))
        self.win._on_refresh()
        self.assertFalse(self.win._data_timeout_reported)
        self.assertEqual(self.win._lbl_state.text(), "接收中")

    def test_eframe_only_then_recover(self):
        self.listen()
        c = self.connect()
        c.sendall(e_line(status=1) * 2)
        self.assertTrue(
            wait_until(lambda: self.win._segment_eframe_count >= 2)
        )
        self.assertEqual(self.win._segment_dframe_count, 0)
        self.win._on_refresh()
        # E 故障心跳单独表达，不被当成"接收中"或"断线"
        self.assertIn("设备故障", self.win._lbl_state.text())
        self.assertTrue(self.win._device_fault)

        c.sendall(d_line(7, status=0))
        self.assertTrue(wait_until(lambda: self.win._segment_dframe_count > 0))
        self.win._on_refresh()
        self.assertFalse(self.win._device_fault)
        self.assertEqual(self.win._lbl_state.text(), "接收中")

    def test_eframe_only_without_fault_shows_heartbeat_state(self):
        self.listen()
        c = self.connect()
        c.sendall(e_line(status=0))
        self.assertTrue(wait_until(lambda: self.win._segment_eframe_count > 0))
        self.win._on_refresh()
        self.assertEqual(self.win._lbl_state.text(), "仅心跳（设备未就绪）")

    def test_no_bytes_after_connect_reports_no_data(self):
        self.listen()
        self.connect()
        self.win._segment_start_time -= 6.0
        self.win._session_start_time -= 6.0
        self.win._check_data_freshness()
        self.assertEqual(self.win._data_timeout_reason, "no_bytes")


class TestCalibrationAcrossSegments(_TcpWindowBase):
    """要求 B：断线立即取消标定，不得跨连接段累计。"""

    def test_calibration_cancelled_on_disconnect(self):
        self.listen()
        c = self.connect()
        self.send_frames(c, [1, 2, 3])
        self.win._confirm_plate_removed = lambda: True
        self.win._sp_calib.setValue(50)  # 目标大，保证仍在进行
        self.win._on_calib_clicked()
        self.assertTrue(self.win._calib.is_running(), "标定未启动")

        c.close()
        from ui.main_window import SourceState
        self.assertTrue(
            wait_until(
                lambda: self.win._source_state == SourceState.LISTENING
            )
        )
        self.assertFalse(
            self.win._calib.is_running(), "断线后标定必须立即终止"
        )

        # 迅速重连：仍不得恢复旧标定
        self.connect()
        pump(0.2)
        self.assertFalse(self.win._calib.is_running())


class TestScanNumberClassification(_TcpWindowBase):
    """要求 D：缺口只叫"未观测扫描"，回绕/重启/重复/倒序分开统计。"""

    def test_gap_duplicate_restart_over_socket(self):
        self.listen()
        c = self.connect()
        self.send_frames(c, [1, 2, 3])
        self.assertEqual(self.win._scan_gaps, 0)

        c.sendall(d_line(6))  # 期望 4 → 跳过 4、5
        self.assertTrue(wait_until(lambda: self.win._scan_gaps == 2))
        self.assertEqual(self.win._dropped_frames, 2)

        c.sendall(d_line(6))  # 同一号重复
        self.assertTrue(wait_until(lambda: self.win._scan_duplicates == 1))

        c.sendall(d_line(0))  # 归零 → 设备重启（新运行周期）
        self.assertTrue(wait_until(lambda: self.win._scan_restarts == 1))
        self.assertGreaterEqual(self.win._scan_epoch, 2)

        before = self.win._scan_gaps
        c.sendall(d_line(1))  # 重启后连续 → 不增加缺口
        self.assertTrue(wait_until(lambda: self.win._frame_count >= 7))
        self.assertEqual(self.win._scan_gaps, before)

    def test_wrap_classification(self):
        win = self.win
        win._expected_scan_number = 0xFFFFFFF1  # prev = 0xFFFFFFF0
        win._classify_scan_number(5)  # 跨 2^32 边界
        self.assertEqual(win._scan_wraps, 1)
        self.assertEqual(win._scan_restarts, 0)

    def test_reorder_classification(self):
        win = self.win
        win._expected_scan_number = 100  # prev = 99
        win._classify_scan_number(97)  # 小幅回退 → 延迟帧
        self.assertEqual(win._scan_reorders, 1)
        self.assertEqual(win._scan_restarts, 0)
        self.assertEqual(win._scan_gaps, 0)


class TestIdentityAndBaselines(_TcpWindowBase):
    """要求 C：编号为空/不匹配时不自动套用基线；在线编辑被锁定。"""

    def _seed_baseline(self, device: str) -> None:
        cfg = self.win._config
        cfg.set_baselines(
            SCOPE_TCP, {0: 1000.0, 1: 1001.0}, {"device_id": device}
        )
        ok, err = cfg.save()
        self.assertTrue(ok, err)

    def test_matching_identity_applies_baseline(self):
        self._seed_baseline("EDDY-01")
        self.win._config.device_id = "EDDY-01"
        self.win._edt_device_id.setText("EDDY-01")
        self.listen()
        self.connect()
        self.assertAlmostEqual(
            self.win._processor.get(0).baseline_mv, 1000.0
        )
        self.assertTrue(self.win._scope_baselines_applied)

    def test_mismatched_identity_blocks_baseline(self):
        from ui.main_window import SourceState
        self._seed_baseline("EDDY-01")
        self.listen()
        self.connect()
        self.win._on_disconnect_clicked()
        self.assertTrue(
            wait_until(
                lambda: self.win._source_state == SourceState.DISCONNECTED
            )
        )
        self.win._config.device_id = "EDDY-02"  # 换设备
        self.win._edt_device_id.setText("EDDY-02")
        self.win._on_connect_clicked()
        self.assertTrue(
            wait_until(
                lambda: self.win._source_state == SourceState.LISTENING
            )
        )
        self.connect()
        self.assertIsNone(self.win._processor.get(0).baseline_mv)
        self.assertFalse(self.win._scope_baselines_applied)
        self.assertIn(
            "不一致", self.win._message_bar._lbl_text.text()
        )

    def test_empty_identity_blocks_baseline_with_notice(self):
        from ui.main_window import SourceState
        self._seed_baseline("EDDY-01")
        self.win._config.device_id = ""
        self.win._edt_device_id.setText("")
        self.listen()
        self.connect()
        self.assertIsNone(self.win._processor.get(0).baseline_mv)
        self.assertFalse(self.win._scope_baselines_applied)
        self.assertIn(
            "未填写采集设备编号", self.win._message_bar._lbl_text.text()
        )
        # 不得声称自动识别设备
        self.assertNotIn("已识别", self.win._message_bar._lbl_text.text())
        self.assertEqual(self.win._source_state, SourceState.SERIAL_CONNECTED)

    def test_identity_edit_locked_while_listening(self):
        self.listen()
        self.assertTrue(self.win._edt_device_id.isReadOnly())
        self.win._config.device_id = "EDDY-01"
        self.win._edt_device_id.setText("HACK-99")
        self.win._on_device_id_edited()
        self.assertEqual(self.win._config.device_id, "EDDY-01")
        self.assertEqual(self.win._edt_device_id.text(), "EDDY-01")

        self.win._on_disconnect_clicked()
        pump(0.1)
        self.assertFalse(self.win._edt_device_id.isReadOnly())
        self.win._edt_device_id.setText("EDDY-09")
        self.win._on_device_id_edited()
        self.assertEqual(self.win._config.device_id, "EDDY-09")


class TestRecordingAcrossSegments(_TcpWindowBase):
    """要求 B：跨断线记录可追溯（分文件、时间轴不倒退、元数据正确）。"""

    def test_recording_splits_on_disconnect(self):
        from ui.main_window import SourceState
        self.win._config.last_csv_dir = self._tmp
        self.listen()
        c = self.connect()
        self.send_frames(c, [1, 2])

        self.win._btn_rec.setChecked(True)
        self.assertTrue(self.win._recorder.is_recording)
        first_path = self.win._recorder.file_path
        first_session = self.win._session_id
        self.send_frames(c, [3, 4])

        c.close()
        self.assertTrue(
            wait_until(
                lambda: self.win._source_state == SourceState.LISTENING
            )
        )
        self.assertFalse(
            self.win._recorder.is_recording,
            "断线后应停止记录（每段连接单独成文件）",
        )
        self.assertTrue(os.path.isfile(first_path))

        with open(first_path, "r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertGreaterEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "tcp")
        self.assertEqual(rows[0]["session_id"], first_session)
        times = [float(r["相对时间/s"]) for r in rows]
        self.assertEqual(times, sorted(times), "同一文件内时间轴不得倒退")

        # 重连 = 新会话/新文件
        c2 = self.connect()
        self.assertNotEqual(self.win._session_id, first_session)
        self.send_frames(c2, [10])
        self.win._btn_rec.setChecked(True)
        self.assertTrue(self.win._recorder.is_recording)
        second_path = self.win._recorder.file_path
        self.assertNotEqual(second_path, first_path, "不得覆盖旧 CSV")
        self.win._btn_rec.setChecked(False)


class TestListeningLifecycle(_TcpWindowBase):
    """要求 E/10：端口占用、重启监听、关窗释放、第二客户端、串口回归。"""

    def test_port_occupied_reports_error(self):
        from ui.main_window import SourceState
        dummy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            dummy.setsockopt(
                socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1
            )
        try:
            dummy.bind(("127.0.0.1", self.port))
            dummy.listen(1)
            from ui.main_window import TRANSPORT_TCP
            self.win._transport = TRANSPORT_TCP
            self.win._update_transport_ui()
            self.win._edt_listen_host.setText("127.0.0.1")
            self.win._sp_listen_port.setValue(self.port)
            self.win._on_connect_clicked()
            self.assertTrue(
                wait_until(
                    lambda: self.win._source_state == SourceState.DISCONNECTED,
                    timeout=5.0,
                ),
                "端口被占用时未报告监听失败",
            )
            self.assertIn("无法监听", self.win._log_view.toPlainText())
        finally:
            dummy.close()

    def test_stop_then_restart_listening(self):
        from ui.main_window import SourceState
        self.listen()
        self.win._on_disconnect_clicked()
        self.assertTrue(
            wait_until(
                lambda: self.win._source_state == SourceState.DISCONNECTED
            )
        )
        self.win._on_connect_clicked()
        self.assertTrue(
            wait_until(
                lambda: self.win._source_state == SourceState.LISTENING
            ),
            "停止后无法在同一端口重新监听",
        )
        self.connect()

    def test_second_client_rejected_and_first_keeps_working(self):
        self.listen()
        c1 = self.connect()
        self.send_frames(c1, [1])

        c2 = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        self.clients.append(c2)
        c2.settimeout(2.0)
        self.assertEqual(
            c2.recv(16), b"", "第二台设备应被立即关闭（单设备策略）"
        )
        self.assertEqual(self.win._tcp_connect_count, 1)

        before = self.win._frame_count
        c1.sendall(d_line(2))
        self.assertTrue(wait_until(lambda: self.win._frame_count > before))

    def test_close_window_releases_port(self):
        self.listen()
        self.connect()
        self.win.close()
        pump(0.3)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", self.port))
        finally:
            s.close()
        # 监听线程必须已停止并释放
        self.assertIsNone(self.win._tcp, "关窗后监听线程未被释放")

    def test_kick_client_keeps_listening(self):
        """数据超时后可用"清除设备"显式断开，继续监听（不自动踢人）。"""
        from ui.main_window import SourceState
        self.listen()
        c = self.connect()
        self.send_frames(c, [1])
        self.assertTrue(self.win._btn_kick.isEnabled())

        self.win._on_kick_client_clicked()
        self.assertTrue(
            wait_until(
                lambda: self.win._source_state == SourceState.LISTENING
            ),
            "清除设备后应回到监听态",
        )
        c.settimeout(2.0)
        try:
            data = c.recv(16)
        except OSError:
            data = b""
        self.assertEqual(data, b"", "被清除的连接应被关闭")

        # 仍可接受新设备
        c2 = self.connect()
        self.send_frames(c2, [2])

    def test_keepalive_option_applied(self):
        """keepalive 只做链路探活，参数可配置，确实下发到套接字。"""
        from core.tcp_worker import TcpServerWorker

        s1, s2 = socket.socketpair()
        try:
            worker = TcpServerWorker(keepalive_enabled=True,
                                     keepalive_idle_s=30)
            worker._apply_keepalive(s1)
            self.assertEqual(
                s1.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE), 1
            )
        finally:
            s1.close()
            s2.close()

    def test_serial_mode_still_works(self):
        """串口回归：本改动不得影响原有串口数据通路。"""
        self.win._on_serial_connected("COM_TEST")
        self.win._on_serial_data(d_line(1))
        self.assertEqual(self.win._frame_count, 1)
        self.win._on_refresh()
        self.assertEqual(self.win._lbl_state.text(), "接收中")
        self.assertEqual(self.win._session_source_name(), "serial")


if __name__ == "__main__":
    unittest.main()
