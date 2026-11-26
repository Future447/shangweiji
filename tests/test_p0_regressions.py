"""
P0 回归测试：针对 IMPROVEMENT_PROMPTS.md 中 P0-1 ~ P0-5 列出的真实缺陷。
（v3 毫伏协议版本：全部测量值为 mV，3 位小数）

每个测试在当前未整改实现上必须失败；整改后必须通过。
不在这里放宽断言来"通过"。
"""

import math
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from PyQt5.QtCore import QCoreApplication, QTimer
from PyQt5.QtWidgets import QApplication

from core.calibration_manager import CalibrationManager
from core.channel_layout import HARDWARE_CHANNEL_MAP
from core.channel_processor import ChannelProcessor
from core.config_manager import ConfigManager
from core.demo_source import DemoSource
from core.protocol_parser import FrameKind, FrameParser
from core.recorder import Recorder, N_CHANNELS, SCHEMA_VERSION
from models.adc_profile import build_default_profiles, profile_by_name
from models.channel_state import ChannelStatus
from models.frame import DFrame, EFrame, MAX_CHANNEL_COUNT, format_mv


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_d(scan=1, mask=0xF, samples=None, status=0, i2c=0):
    if samples is None:
        samples = tuple(100.0 + i for i in range(16))
    return DFrame(
        scan_number=scan,
        scan_status=status,
        valid_mask=mask,
        i2c_error_count=i2c,
        samples_mv=tuple(float(s) for s in samples),
    )


def make_d_line(scan=1, status=0, mask=0xFFFF, i2c=0, samples=None) -> bytes:
    if samples is None:
        samples = [100.0 + i for i in range(16)]
    parts = ["D", str(scan), str(status), str(mask), str(i2c), "mV"]
    parts.extend(format_mv(float(s)) for s in samples)
    return (",".join(parts) + "\r\n").encode("ascii")


def make_e_line(s=1, ia=1, ra=0, te=1) -> bytes:
    return f"E,INIT,{s},{ia},{ra},{te}\r\n".encode("ascii")


def _process(ms: int = 30) -> None:
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.005)


# ---------------------------------------------------------------------------
# P0-1 标定必须由新 D 帧驱动；同帧不能被多次计数
# ---------------------------------------------------------------------------
class P01_CalibrationFrameDriven(unittest.TestCase):
    """P0-1: 同一硬件帧不能被多次计入；target=N 必须有 N 个不同 D 帧。"""

    def setUp(self):
        self.proc = ChannelProcessor(
            adc_profile=profile_by_name("ads1115_4v096_div2",
                                        build_default_profiles()),
            enabled_channels=[0, 1, 2, 3],
        )
        # P0-1 聚焦"帧驱动/去重"，预热行为由 test_calibration_flow 覆盖
        self.calib = CalibrationManager(self.proc, warmup_frames=0)

    def tearDown(self):
        if self.calib.is_running():
            self.calib.cancel()

    def test_single_frame_not_enough_for_target_two(self):
        """
        只注入 1 帧，调用 on_frame 1 次 + 任何 tick 触发。
        target=5 时绝不应完成。
        """
        finished = []
        self.calib.finished.connect(lambda b: finished.append(b))
        self.calib.start(target_samples=5)
        df = make_d(scan=1, mask=0xF)
        self.proc.ingest(df)
        self.calib.on_frame(df)
        # 反复调用 on_frame 同一帧不应增加进度（去重）
        for _ in range(20):
            self.calib.on_frame(df)
        self.assertFalse(
            bool(finished),
            f"target=5 但仅 1 帧就完成了标定 finished={finished}",
        )
        self.assertTrue(self.calib.is_running(),
                        "标定应在未达到 target 时保持 running")

    def test_five_distinct_frames_complete_target_five(self):
        """
        滤波稳定后 5 个不同的 D 帧应能完成 target=5。

        warmup_frames=0 关闭了全局预热，但每通道滤波需先被本通道
        有效样本填满中值窗（2 帧）才开始计数——标定不得采纳未
        稳定的滤波值。
        """
        finished = []
        self.calib.finished.connect(lambda b: finished.append(b))
        self.calib.start(target_samples=5)
        for i in range(2 + 5):
            df = make_d(
                scan=i + 1, mask=0xF,
                samples=tuple(100.0 + i + ch for ch in range(16)),
            )
            self.proc.ingest(df)
            self.calib.on_frame(df)
        self.assertEqual(
            len(finished), 1,
            f"滤波稳定 + 5 帧后应完成标定，finished={len(finished)}"
        )

    def test_invalid_channels_not_counted(self):
        """
        标定启用 [0,1] 通道，但 D 帧仅 CH0 有效。CH1 永远不计数。
        """
        failed = []
        self.calib.failed.connect(lambda r: failed.append(r))
        self.proc.set_enabled_channels([0, 1])
        self.calib.start(target_samples=3, max_idle_s=2.0)
        # 前 2 帧填满 CH0 的中值窗（滤波稳定门槛），随后 3 帧计数
        for i in range(2 + 3):
            df = make_d(
                scan=i + 1, mask=0x1,
                samples=tuple(100.0 + i for _ in range(16)),
            )
            self.proc.ingest(df)
            self.calib.on_frame(df)
        snap = self.calib.state_snapshot()
        self.assertEqual(snap["per_channel"].get(0, 0), 3)
        self.assertEqual(snap["per_channel"].get(1, 0), 0)


# ---------------------------------------------------------------------------
# P0-2 16 通道原始数据必须保留到 CSV，valid_mask=0 时不应写陈旧值
# ---------------------------------------------------------------------------
class P02_CsvAllChannels(unittest.TestCase):
    """P0-2: CSV 中每通道 wire mV 必须来自当前 D 帧。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p02_")
        self.profiles = build_default_profiles()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_disabled_channel_wire_mv_preserved(self):
        """
        仅启用 CH01 的处理器，输入 16 通道全有效 D 帧，
        CSV 中所有 16 通道的 wire_mv 必须与该帧样本完全一致（3 位小数）。
        """
        rec = Recorder()
        params = {"frequency": 1000, "freq_unit": "kHz", "gain": "1",
                  "coil": "c", "plate": "", "note": ""}
        path = rec.start_recording(
            params, self.tmp, self.profiles["ads1115_4v096_div2"],
            session_meta={"source": "demo", "session_id": "test-sess-1"},
        )
        df = make_d(scan=1, mask=0xFFFF,
                    samples=tuple(1000.0 + i * 0.125 for i in range(16)))
        proc = ChannelProcessor(
            adc_profile=self.profiles["ads1115_4v096_div2"],
            enabled_channels=[0],
        )
        proc.ingest(df)
        rec.write_dframe(
            df, proc, invert_sign=False,
            enabled_channels=proc.enabled_channels,
            channel_map=HARDWARE_CHANNEL_MAP,
        )
        rec.stop_recording()
        with open(path, encoding="utf-8-sig") as f:
            content = f.read()
        for c in range(16):
            expected = f"{1000.0 + c * 0.125:.3f}"
            col_name = f"ch{c + 1:02d}_wire_mv"
            self.assertIn(col_name, content)
            self.assertIn(
                expected, content,
                f"通道 CH{c + 1:02d} 的 wire_mv={expected} 未出现在 CSV 中"
            )

    def test_csv_mv_fidelity_424_625(self):
        """CSV 真实性：424.625 mV 必须按 3 位小数原样写出。"""
        rec = Recorder()
        params = {"frequency": 1000, "freq_unit": "kHz", "gain": "1",
                  "coil": "c", "plate": "", "note": ""}
        path = rec.start_recording(
            params, self.tmp, self.profiles["ads1115_4v096_div2"],
        )
        df = make_d(scan=1, mask=0xFFFF,
                    samples=(424.625,) + tuple(100.0 + i for i in range(15)))
        proc = ChannelProcessor(
            adc_profile=self.profiles["ads1115_4v096_div2"],
            enabled_channels=list(range(16)),
        )
        proc.ingest(df)
        rec.write_dframe(df, proc)
        rec.stop_recording()
        with open(path, encoding="utf-8-sig") as f:
            lines = f.readlines()
        header = lines[0].rstrip("\n").split(",")
        row = lines[1].rstrip("\n").split(",")
        self.assertEqual(row[header.index("ch01_wire_mv")], "424.625")
        self.assertEqual(row[header.index("measurement_unit")], "mV")

    def test_invalid_channel_no_stale_value(self):
        """
        valid_mask=0 的通道在 CSV 中不应被旧值顶替。
        """
        rec = Recorder()
        params = {"frequency": 1000, "freq_unit": "kHz", "gain": "1",
                  "coil": "c", "plate": "", "note": ""}
        path = rec.start_recording(
            params, self.tmp, self.profiles["ads1115_4v096_div2"],
        )
        df1 = make_d(scan=1, mask=0xFFFF,
                     samples=tuple(1000.0 + i for i in range(16)))
        # 第二帧 CH02 改为 invalid，样本值 2000.000（合法但应被忽略）
        df2_samples = list(1000.0 + i for i in range(16))
        df2_samples[1] = 2000.0
        df2 = DFrame(
            scan_number=2,
            scan_status=0,
            valid_mask=0xFFFF & ~0x2,
            i2c_error_count=0,
            samples_mv=tuple(df2_samples),
        )
        proc = ChannelProcessor(
            adc_profile=self.profiles["ads1115_4v096_div2"],
            enabled_channels=list(range(16)),
        )
        rec.write_dframe(df1, proc)
        rec.write_dframe(df2, proc)
        rec.stop_recording()
        with open(path, encoding="utf-8-sig") as f:
            lines = f.readlines()
        self.assertGreaterEqual(len(lines), 3)
        header = lines[0].rstrip("\n").split(",")
        ch2_wire_idx = header.index("ch02_wire_mv")
        ch2_valid_idx = header.index("ch02_raw_valid")
        first_row = lines[1].rstrip("\n").split(",")
        self.assertEqual(first_row[ch2_wire_idx], "1001.000")
        self.assertEqual(first_row[ch2_valid_idx], "1")
        second_row = lines[2].rstrip("\n").split(",")
        self.assertEqual(
            second_row[ch2_wire_idx], "",
            f"CH02 invalid 时 wire_mv 应为空，但 CSV 写入 {second_row[ch2_wire_idx]!r}"
        )
        self.assertEqual(second_row[ch2_valid_idx], "0")

    def test_csv_schema_version_present(self):
        """
        CSV 必须包含 schema_version 列（=4，含衰减量列）与 measurement_unit=mV。
        """
        rec = Recorder()
        params = {"frequency": 1000, "freq_unit": "kHz", "gain": "1",
                  "coil": "c", "plate": "", "note": ""}
        path = rec.start_recording(
            params, self.tmp, self.profiles["ads1115_4v096_div2"],
        )
        proc = ChannelProcessor(
            adc_profile=self.profiles["ads1115_4v096_div2"],
            enabled_channels=[0],
        )
        rec.write_dframe(make_d(scan=1, mask=0xFFFF), proc)
        rec.stop_recording()
        with open(path, encoding="utf-8-sig") as f:
            content = f.read()
        self.assertIn("schema_version", content)
        self.assertIn(str(SCHEMA_VERSION), content)
        self.assertEqual(SCHEMA_VERSION, 4)
        self.assertIn("measurement_unit", content)
        self.assertIn("attenuation_formula", content)


# ---------------------------------------------------------------------------
# P0-3 串口与演示模式互斥
# ---------------------------------------------------------------------------
class P03_SourceExclusivity(unittest.TestCase):
    """P0-3: 演示模式运行时不能同时连接串口。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p03_")
        import app_paths
        self._orig = app_paths.get_default_csv_dir
        app_paths.get_default_csv_dir = lambda: self.tmp

    def tearDown(self):
        import app_paths
        app_paths.get_default_csv_dir = self._orig
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        _process(20)

    def test_demo_then_connect_stops_demo(self):
        """
        演示模式启动后点击连接，必须先停止演示。
        """
        from ui.main_window import MainWindow
        QApplication.instance()
        win = MainWindow()
        win.show()
        _process(30)
        try:
            win._start_demo()
            _process(50)
            self.assertTrue(
                win._demo is not None and win._demo.is_running(),
                "演示模式应已启动"
            )
            # 直接调用 _on_connect_clicked；它应先 stop demo，
            # 然后再尝试开串口（串口会因端口不存在失败）
            win._on_connect_clicked()
            _process(50)
            self.assertFalse(
                win._demo is not None and win._demo.is_running(),
                "连接串口前必须停止演示模式"
            )
        finally:
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
            win.deleteLater()
            _process(30)


# ---------------------------------------------------------------------------
# P0-4 协议不匹配应在从未收到 D 帧时也能触发
# ---------------------------------------------------------------------------
class P04_ProtocolMismatchDetection(unittest.TestCase):
    """P0-4: 仅收到垃圾流时也应能判定协议不匹配。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p04_")
        import app_paths
        self._orig = app_paths.get_default_csv_dir
        app_paths.get_default_csv_dir = lambda: self.tmp

    def tearDown(self):
        import app_paths
        app_paths.get_default_csv_dir = self._orig
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        _process(20)

    def test_garbage_stream_triggers_mismatch(self):
        """
        串口"已连接"但只收到垃圾行（无 D 帧），
        会话开始 ≥ 5s 后应进入"协议不匹配"状态。
        """
        from ui.main_window import MainWindow
        QApplication.instance()
        win = MainWindow()
        win.show()
        _process(30)
        try:
            win._on_serial_connected("COM_FAKE_P04")
            win._on_serial_data(b"random garbage line 1\r\n")
            win._on_serial_data(b"# DIAG nothing useful\r\n")
            win._on_serial_data(b"some unknown content\r\n")
            win._session_start_time = time.monotonic() - 6.0
            win._check_data_freshness()
            self.assertTrue(
                win._data_timeout_reported,
                "纯垃圾流 + 6s 无 D 帧应触发数据超时"
            )
            self.assertEqual(
                win._data_timeout_reason, "parse",
                f"应判定为协议不匹配，实际 {win._data_timeout_reason!r}"
            )
        finally:
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
            win.deleteLater()
            _process(30)

    def test_valid_d_frame_recovers_receiving(self):
        """v3：收到合法 D 帧后，状态必须从'协议不匹配'恢复为'接收中'。"""
        from ui.main_window import MainWindow
        QApplication.instance()
        win = MainWindow()
        win.show()
        _process(30)
        try:
            win._on_serial_connected("COM_FAKE_P04B")
            # 先进入协议不匹配
            win._on_serial_data(b"garbage\r\n")
            win._session_start_time = time.monotonic() - 6.0
            win._check_data_freshness()
            self.assertTrue(win._data_timeout_reported)
            win._on_refresh()
            self.assertEqual(win._lbl_state.text(), "协议不匹配")
            # 注入合法 v3 D 帧
            win._on_serial_data(make_d_line(scan=1, samples=[424.625] * 16))
            win._on_refresh()
            self.assertFalse(win._data_timeout_reported)
            self.assertEqual(win._lbl_state.text(), "接收中")
            # 首通道解析为 424.625 mV
            self.assertAlmostEqual(
                win._processor.get(0).sample_mv, 424.625
            )
        finally:
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
            win.deleteLater()
            _process(30)


# ---------------------------------------------------------------------------
# P0-5 invert_sign 在热力图 / 通道面板 / CSV 中保持一致
# ---------------------------------------------------------------------------
class P05_InvertSignConsistency(unittest.TestCase):
    """P0-5: invert_sign 必须应用到所有显示与导出。"""

    def setUp(self):
        self.proc = ChannelProcessor(
            adc_profile=profile_by_name("ads1115_4v096_div2",
                                        build_default_profiles()),
            enabled_channels=[0],
        )
        for _ in range(3):
            self.proc.ingest(make_d(scan=1, mask=0x1,
                                    samples=(500.0,) + (0.0,) * 15))
        self.proc.apply_calibration({0: 500.0})

    def test_channel_processor_delta_signed(self):
        d_normal = self.proc.delta_signed(0)
        self.proc.set_invert_sign(True)
        d_inv = self.proc.delta_signed(0)
        self.assertIsNotNone(d_normal)
        self.assertIsNotNone(d_inv)
        self.assertAlmostEqual(d_inv, -d_normal)

    def test_csv_signed_delta_respects_invert(self):
        """
        CSV 中 ch01_delta_signed_mv 必须应用 invert_sign。
        """
        rec = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            params = {"frequency": 1000, "freq_unit": "kHz", "gain": "1",
                      "coil": "c", "plate": "", "note": ""}
            path = rec.start_recording(
                params, tmp, self.proc.adc_profile,
            )
            df = make_d(scan=1, mask=0x1,
                        samples=(550.0,) + (0.0,) * 15)
            self.proc.ingest(df)
            rec.write_dframe(df, self.proc, invert_sign=False)
            rec.write_dframe(df, self.proc, invert_sign=True)
            rec.stop_recording()
            with open(path, encoding="utf-8-sig") as f:
                lines = f.readlines()
            header = lines[0].rstrip("\n").split(",")
            d_idx = header.index("ch01_delta_signed_mv")
            row1 = lines[1].rstrip("\n").split(",")
            row2 = lines[2].rstrip("\n").split(",")
            v1 = float(row1[d_idx])
            v2 = float(row2[d_idx])
            # invert_sign=True 时应取负
            self.assertAlmostEqual(v2, -v1, places=2)


# ---------------------------------------------------------------------------
# 综合烟雾
# ---------------------------------------------------------------------------
class P00_SmokeSanity(unittest.TestCase):
    """确认 P0 整改后基础链路仍可用（v3 mV）。"""

    def test_parser_d_frame_count(self):
        parser = FrameParser()
        out = parser.feed(make_d_line())
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, FrameKind.D)
        self.assertEqual(out[0].d_frame.scan_number, 1)

    def test_parser_e_frame(self):
        parser = FrameParser()
        out = parser.feed(make_e_line())
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, FrameKind.E)

    def test_processor_keeps_wire_mv_for_disabled(self):
        """未启用通道的 wire mV 也应被记录。"""
        proc = ChannelProcessor(
            enabled_channels=[0],  # 仅启用 0
        )
        df = make_d(scan=1, mask=0xFFFF,
                    samples=tuple(100.0 + i for i in range(16)))
        proc.ingest(df)
        for c in range(16):
            self.assertEqual(
                proc.last_wire_mv(c), 100.0 + c,
                f"通道 {c} 的 wire mV 应为 {100.0 + c}"
            )


if __name__ == "__main__":
    unittest.main()
