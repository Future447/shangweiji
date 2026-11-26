"""
Qt 离屏烟雾测试：演示模式启动 + 标定流程 + 串口线程安全退出。
"""

import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from PyQt5.QtCore import QCoreApplication, QTimer
from PyQt5.QtWidgets import QApplication

from core.serial_worker import SerialWorker
from ui.main_window import MainWindow, SourceState


def _process_events(ms: int = 30) -> None:
    """给 Qt 一段处理时间。"""
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.005)


class TestDemoModeBoot(unittest.TestCase):
    def setUp(self):
        # 每个测试用独立的临时目录作为 CSV 保存目录，避免互相影响
        self._tmp = tempfile.mkdtemp(prefix="smoke_")
        # patch ui.main_window.ConfigManager：MainWindow 构造时使用
        # 临时配置文件——绝不读取/覆盖用户真实基线
        from core.config_manager import ConfigManager
        self._cfg_path = os.path.join(self._tmp, "config.json")
        import ui.main_window as mw_mod
        self._orig_cm = mw_mod.ConfigManager
        mw_mod.ConfigManager = lambda: ConfigManager(  # type: ignore
            config_path=self._cfg_path
        )
        # 改默认路径：直接 monkey-patch get_default_csv_dir
        import app_paths
        self._orig_csv_dir = app_paths.get_default_csv_dir
        app_paths.get_default_csv_dir = lambda: self._tmp  # type: ignore

    def tearDown(self):
        # 恢复
        import app_paths
        import ui.main_window as mw_mod
        app_paths.get_default_csv_dir = self._orig_csv_dir  # type: ignore
        mw_mod.ConfigManager = self._orig_cm  # type: ignore
        try:
            import shutil
            shutil.rmtree(self._tmp, ignore_errors=True)
        except Exception:
            pass
        _process_events(20)

    def _new_window(self) -> MainWindow:
        QApplication.instance()
        win = MainWindow()
        win.show()
        _process_events(30)
        return win

    def _safe_close(self, win: MainWindow) -> None:
        try:
            win._refresh_timer.stop()
        except Exception:
            pass
        try:
            win._calib.cancel()
        except Exception:
            pass
        try:
            win._recorder.stop_recording()
        except Exception:
            pass
        try:
            if win._demo is not None:
                win._demo.stop()
        except Exception:
            pass
        try:
            win.close()
        except Exception:
            pass
        _process_events(50)
        win.deleteLater()
        _process_events(20)

    def test_mainwindow_offscreen(self):
        """MainWindow 可以在 offscreen 平台启动。"""
        win = self._new_window()
        self.assertIsNotNone(win)
        self._safe_close(win)

    def test_start_demo_emits_frames(self):
        """演示模式启动后会产生 D 帧（v3 mV 量级）。"""
        win = self._new_window()
        win._start_demo()
        _process_events(800)  # 至少 1 帧
        self.assertGreater(win._frame_count, 0)
        # 演示数据与串口路径同语义：mV，量程 ±4096
        st = win._processor.get(win._processor.enabled_channels[0])
        self.assertGreater(st.sample_mv, -4096.0)
        self.assertLess(st.sample_mv, 4096.0)
        self.assertGreater(abs(st.sample_mv), 100.0)  # ~1500 mV 量级
        self.assertLess(abs(st.sample_mv), 3000.0)
        self._safe_close(win)

    def test_demo_with_calibration(self):
        """演示模式 + 无钢板基线标定完整流程。"""
        win = self._new_window()
        win._start_demo()
        # 等待几帧进来
        _process_events(800)
        # 自动确认"已移除钢板"对话框（无人值守测试）
        win._confirm_plate_removed = lambda: True
        # 触发标定
        win._sp_calib.setValue(5)
        win._on_calib_clicked()
        # 等标定完成（含滤波预热帧；直接驱动 timer）
        for _ in range(80):
            _process_events(50)
            if not win._calib.is_running():
                break
        self.assertFalse(win._calib.is_running())
        # 标定成功后基线被设置（mV）
        for c in win._processor.enabled_channels:
            self.assertIsNotNone(win._processor.get(c).baseline_mv)
        # 演示标定写入 demo 作用域，不覆盖 serial 硬件基线
        self.assertIsNotNone(win._config.get_baseline_meta("demo"))
        self._safe_close(win)

    def test_demo_record_csv(self):
        """演示模式 + CSV 记录。"""
        win = self._new_window()
        win._config.last_csv_dir = self._tmp
        win._config.save()
        win._start_demo()
        _process_events(800)
        # 开始记录
        win._btn_rec.setChecked(True)
        _process_events(1500)
        win._btn_rec.setChecked(False)
        _process_events(50)
        files = [f for f in os.listdir(self._tmp) if f.endswith(".csv")]
        self.assertGreater(len(files), 0)
        self._safe_close(win)


class TestModeAndButtonConsistency(TestDemoModeBoot):
    """模式切换与按钮状态一致性回归（对抗评审发现）。"""

    def test_refresh_keeps_calib_disabled_in_replay(self):
        """回放模式下 _on_refresh 不得重新启用"开始标定"。

        回归：进入回放后源状态滞留 SERIAL_CONNECTED，周期刷新按
        滞留状态重新启用标定按钮——标定永远收不到帧，只会误报断流。
        """
        win = self._new_window()
        win._set_app_mode("replay")
        self.assertEqual(win._source_state, SourceState.REPLAY)
        win._on_refresh()
        self.assertFalse(win._btn_calib.isEnabled())
        self.assertFalse(win._btn_connect.isEnabled())
        self.assertFalse(win._btn_demo.isEnabled())
        # 退出回放后恢复
        win._set_app_mode("live")
        self._safe_close(win)

    def test_calib_blocked_when_demo_plate_inserted(self):
        """演示模式【已放入钢板】状态下不得开始无钢板基线标定。"""
        win = self._new_window()
        win._start_demo()
        _process_events(300)
        # 放入钢板（信号已衰减）
        win._btn_plate.setChecked(True)
        _process_events(100)
        self.assertTrue(win._demo.plate_inserted)
        win._confirm_plate_removed = lambda: True
        with patch("ui.main_window.QMessageBox.warning", MagicMock()):
            win._on_calib_clicked()
        self.assertFalse(
            win._calib.is_running(),
            "放板状态下标定必须被拦截",
        )
        # 移除钢板后可以标定
        win._btn_plate.setChecked(False)
        _process_events(100)
        win._on_calib_clicked()
        self.assertTrue(win._calib.is_running())
        win._calib.cancel()
        self._safe_close(win)

    def test_start_new_session_stops_recording(self):
        """切换数据源（新会话）必须先停止当前记录。

        回归：记录跨来源切换而不中断，同一 CSV 混入不同
        来源/会话元数据的帧，衰减量基线标识失真。
        """
        win = self._new_window()
        win._config.last_csv_dir = self._tmp
        win._config.save()
        win._start_demo()
        _process_events(300)
        win._btn_rec.setChecked(True)
        _process_events(300)
        self.assertTrue(win._recorder.is_recording)
        # 模拟切换到串口：新会话开始 → 记录应已停止
        win._start_new_session("serial")
        _process_events(50)
        self.assertFalse(win._recorder.is_recording)
        self.assertFalse(win._btn_rec.isChecked())
        self._safe_close(win)

    def test_rec_buttons_stay_in_sync(self):
        """完整/紧凑两个记录按钮状态必须同步（同一状态源）。"""
        win = self._new_window()
        win._config.last_csv_dir = self._tmp
        win._config.save()
        win._start_demo()
        _process_events(300)
        # 从紧凑按钮开始记录 → 完整按钮同步
        win._btn_rec_inline.setChecked(True)
        _process_events(200)
        self.assertTrue(win._recorder.is_recording)
        self.assertTrue(win._btn_rec.isChecked())
        # 从完整按钮停止 → 紧凑按钮同步
        win._btn_rec.setChecked(False)
        _process_events(100)
        self.assertFalse(win._recorder.is_recording)
        self.assertFalse(win._btn_rec_inline.isChecked())
        self._safe_close(win)


class TestSerialWorker(unittest.TestCase):
    def test_open_fake_port_reports_error(self):
        """不存在的端口应报告错误并停止。"""
        QApplication.instance()
        w = SerialWorker("COM_FAKE_V2_TEST_99999", 115200)
        errs = []
        stopped = []
        w.error_occurred.connect(lambda m: errs.append(m))
        w.stopped.connect(lambda: stopped.append(True))
        w.start()
        w.wait(3000)
        _process_events(50)
        self.assertFalse(w.isRunning())
        self.assertGreaterEqual(len(errs), 1)
        self.assertEqual(len(stopped), 1)

    def test_stop_before_run(self):
        QApplication.instance()
        w = SerialWorker("COM_FAKE", 115200)
        w.start()
        w.stop()
        _process_events(50)
        self.assertFalse(w.isRunning())


if __name__ == "__main__":
    unittest.main()
