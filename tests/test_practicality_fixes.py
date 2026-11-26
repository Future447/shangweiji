"""
实用性审查修复的回归测试（Phase B）。

覆盖五个问题（每项对应任务书一节）：
- P1-1 饱和数据污染滤波器和标定（ingest 先更新滤波后判饱和）
- P1-2 标定缺少漂移与末段稳定性门槛（MAD=0 的台阶被判定成功）
- P1-3 局部重新标定后的内存基线未同步（主窗口集成测试）
- P2-1 回放曲线没有使用实验原始时间（t_rel）
- P2-2 无效帧的 CSV / tooltip / 曲线仍显示旧测量值

隔离要求（任务书 七-2）：全部使用临时 config 与临时 CSV 目录，
绝不读取/覆盖用户真实基线与真实实验数据。
"""

import csv
import math
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QCoreApplication
from PyQt5.QtWidgets import QApplication

from core.calibration_manager import CalibrationManager, WARMUP_FRAMES
from core.channel_processor import ChannelProcessor
from core.measurement import build_snapshot
from core.recorder import Recorder
from core.replay_source import ReplayRow
from models.adc_profile import build_default_profiles, profile_by_name
from models.channel_state import ChannelStatus
from models.frame import DFrame, MAX_CHANNEL_COUNT, MV_MAX, MV_MIN
from ui.main_window import MainWindow
from ui.plot_widget import PlotWidget


def make_d(scan=1, mask=0xF, samples=None):
    if samples is None:
        samples = (1000.0,) * MAX_CHANNEL_COUNT
    return DFrame(
        scan_number=scan,
        scan_status=0,
        valid_mask=mask,
        i2c_error_count=0,
        samples_mv=tuple(samples),
    )


def _process_events(ms: int = 30) -> None:
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.005)


def _new_processor(enabled=(0,), median_window=3, ema_alpha=0.2):
    proc = ChannelProcessor(
        adc_profile=profile_by_name(
            "ads1115_4v096_div2", build_default_profiles()
        ),
        enabled_channels=list(enabled),
    )
    proc.set_median_window(median_window)
    proc.set_ema_alpha(ema_alpha)
    return proc


def _feed(proc, df, calib=None):
    proc.ingest(df)
    if calib is not None:
        calib.on_frame(df)


def _settle(proc, value=1000.0, scan_start=1, n=5, mask=0xF):
    """喂 n 帧恒定值使滤波稳定（中值窗填满 + EMA 收敛）。"""
    for i in range(n):
        _feed(proc, make_d(scan=scan_start + i, mask=mask,
                           samples=(value,) * MAX_CHANNEL_COUNT))


# ---------------------------------------------------------------------------
# P1-1 饱和数据污染滤波器和标定
# ---------------------------------------------------------------------------
class TestSaturationNoPollution(unittest.TestCase):
    """任务书 二：饱和 rail 值不得进入中值/EMA/标定。"""

    def test_user_repro_saturated_frames_do_not_pollute_filter(self):
        """复现：前 2 帧上限饱和，随后 5 帧 1000.000 mV。

        旧行为：饱和值先进滤波再判饱和，filtered 被拉向 rail，
        标定得出 2981.360 mV 的错误基线（有效原始样本 MAD/std=0）。
        修复后：filtered 必须恰好是 1000.000。
        """
        proc = _new_processor(median_window=1, ema_alpha=1.0)
        for i in range(2):
            _feed(proc, make_d(scan=i + 1, mask=0x1,
                               samples=(MV_MAX,) + (0.0,) * 15))
        for i in range(5):
            _feed(proc, make_d(scan=10 + i, mask=0x1,
                               samples=(1000.0,) + (0.0,) * 15))
        st = proc.get(0)
        self.assertAlmostEqual(st.filtered_mv, 1000.0, places=9)
        self.assertNotAlmostEqual(st.filtered_mv, 2981.360, places=1)

    def test_saturated_frame_keeps_true_wire_but_no_filter_update(self):
        """饱和帧：真实 wire 值保留（诊断），但不进滤波、无有效衰减。"""
        proc = _new_processor()
        _settle(proc, 800.0)
        st = proc.get(0)
        self.assertTrue(st.filter_settled)
        filtered_before = st.filtered_mv
        _feed(proc, make_d(scan=99, mask=0x1,
                           samples=(MV_MAX,) + (0.0,) * 15))
        self.assertEqual(st.last_status, ChannelStatus.SATURATED)
        # 真实 rail 值保留在采样字段（CSV wire 列/诊断用）
        self.assertAlmostEqual(st.sample_mv, MV_MAX)
        # 滤波链未被触碰
        self.assertAlmostEqual(st.filtered_mv, filtered_before)
        # 快照：饱和帧无当前滤波/衰减（基线未标定本来也是 None，
        # 这里重点验证 filtered 不冒充当前值）
        snap = build_snapshot(st, frame_valid=True)
        self.assertIsNone(snap.filtered_mv)
        self.assertIsNone(snap.attenuation_mv)
        # 但采样诊断值保留（状态列明确标记饱和）
        self.assertAlmostEqual(snap.sample_mv, MV_MAX)
        self.assertEqual(snap.status, ChannelStatus.SATURATED)

    def test_calibration_after_saturation_clean_or_incomplete(self):
        """饱和后标定：基线只能 ≈1000 或标定未完成，绝不得错误基线。"""
        proc = _new_processor(median_window=1, ema_alpha=1.0)
        calib = CalibrationManager(proc, warmup_frames=0)
        finished, failed = [], []
        calib.finished.connect(lambda b: finished.append(b))
        calib.failed.connect(lambda r: failed.append(r))
        try:
            calib.start(target_samples=5)
            for i in range(2):
                _feed(proc, make_d(scan=i + 1, mask=0x1,
                                   samples=(MV_MAX,) + (0.0,) * 15),
                      calib=calib)
            for i in range(5):
                _feed(proc, make_d(scan=10 + i, mask=0x1,
                                   samples=(1000.0,) + (0.0,) * 15),
                      calib=calib)
            # 不得空转：标定必须干净完成或明确失败（防回归为
            # 永不发射 finished/failed 时本测试零断言假绿）
            self.assertTrue(
                finished or failed,
                "标定必须完成或明确失败，不得无结果空转",
            )
            if finished:
                self.assertAlmostEqual(finished[0][0], 1000.0, delta=1.0)
            # 要么干净完成（≈1000），要么未完成/失败——绝不得 2981
            for b in finished:
                self.assertLess(abs(b[0] - 1000.0), 100.0)
        finally:
            if calib.is_running():
                calib.cancel()

    def test_long_saturation_recovers_with_per_channel_warmup(self):
        """长饱和后的恢复：重新播种 + 按本通道有效样本预热。

        预热期状态为 WARMING_UP（与正常测量区分），衰减量为 None；
        预热完成后才回到正常状态——历史残留不得当作当前正常值。
        """
        proc = _new_processor()
        proc.get(0).baseline_mv = 800.0
        _settle(proc, 800.0)
        st = proc.get(0)
        self.assertEqual(st.last_status, ChannelStatus.OK)
        # 持续饱和 > FILTER_GAP_RESET_S（模拟：把最后接纳时间拨回过去）
        st._last_accept_monotonic = time.monotonic() - 10.0
        _feed(proc, make_d(scan=50, mask=0x1,
                           samples=(MV_MAX,) + (0.0,) * 15))
        self.assertEqual(st.last_status, ChannelStatus.SATURATED)
        # 恢复帧 1：重新播种，进入预热（滤波不代表当前）
        _feed(proc, make_d(scan=51, mask=0x1,
                           samples=(800.0,) + (0.0,) * 15))
        self.assertEqual(st.last_status, ChannelStatus.WARMING_UP)
        self.assertFalse(st.filter_settled)
        snap = build_snapshot(st, frame_valid=True)
        self.assertIsNone(snap.attenuation_mv)
        self.assertIsNone(snap.filtered_mv)
        # 恢复帧 2/3：中值窗填满 → 回到正常（无残留污染）
        _feed(proc, make_d(scan=52, mask=0x1,
                           samples=(800.0,) + (0.0,) * 15))
        self.assertEqual(st.last_status, ChannelStatus.WARMING_UP)
        _feed(proc, make_d(scan=53, mask=0x1,
                           samples=(800.0,) + (0.0,) * 15))
        self.assertEqual(st.last_status, ChannelStatus.OK)
        snap = build_snapshot(st, frame_valid=True)
        self.assertIsNotNone(snap.attenuation_mv)
        self.assertLess(abs(snap.attenuation_mv), 5.0)

    def test_short_gap_resumes_without_rewarmup(self):
        """短暂失效（<= FILTER_GAP_RESET_S）：滤波状态保留，直接恢复。

        失效期间没有任何样本进入滤波链，旧状态仍代表当前物理量，
        不应触发重新预热（显式策略：短失效直接续用，长流失重建）。
        """
        proc = _new_processor()
        proc.get(0).baseline_mv = 800.0
        _settle(proc, 800.0)
        st = proc.get(0)
        self.assertEqual(st.last_status, ChannelStatus.OK)
        # 1 帧无效 + 1 帧有效（间隔 << 2s）
        _feed(proc, make_d(scan=60, mask=0x0,
                           samples=(0.0,) * MAX_CHANNEL_COUNT))
        self.assertEqual(st.last_status, ChannelStatus.INVALID)
        _feed(proc, make_d(scan=61, mask=0x1,
                           samples=(800.0,) + (0.0,) * 15))
        self.assertEqual(
            st.last_status, ChannelStatus.OK,
            "短失效应直接恢复正常状态，不应重新预热",
        )
        snap = build_snapshot(st, frame_valid=True)
        self.assertIsNotNone(snap.attenuation_mv)
        self.assertLess(abs(snap.attenuation_mv), 5.0)


# ---------------------------------------------------------------------------
# P1-2 标定漂移 / 末段稳定性门槛
# ---------------------------------------------------------------------------
class TestCalibrationStabilityGates(unittest.TestCase):
    """任务书 三：MAD=0 的台阶、显著漂移必须失败；稳定/小噪声通过。"""

    def setUp(self):
        self.proc = _new_processor(enabled=(0,))
        self.calib = CalibrationManager(self.proc)
        self.finished, self.failed = [], []
        self.calib.finished.connect(lambda b: self.finished.append(b))
        self.calib.failed.connect(lambda r: self.failed.append(r))

    def tearDown(self):
        if self.calib.is_running():
            self.calib.cancel()

    def _run_values(self, values, target=None):
        """喂 WARMUP + len(values) 帧（每帧所有通道同值）。"""
        self.calib.start(target_samples=target or len(values))
        scan = 0
        for _ in range(WARMUP_FRAMES):
            scan += 1
            _feed(self.proc, make_d(scan=scan, mask=0x1,
                                    samples=(values[0],) + (0.0,) * 15),
                  calib=self.calib)
        for v in values:
            scan += 1
            _feed(self.proc, make_d(scan=scan, mask=0x1,
                                    samples=(v,) + (0.0,) * 15),
                  calib=self.calib)

    def test_user_repro_tail_step_rejected(self):
        """复现：前 15 个 1000 mV，后 5 个 800 mV（MAD=0）必须失败。

        旧行为：漂移/异常点被计算但无门限，MAD=0 照常通过。
        修复后：漂移门限（前后半段中位数差 −100 mV）触发失败，
        失败信息必须点名通道与触发指标并给出重标建议。
        """
        self.proc.set_median_window(1)
        self.proc.set_ema_alpha(1.0)
        self._run_values([1000.0] * 15 + [800.0] * 5)
        self.assertEqual(len(self.finished), 0, "台阶信号绝不得标定成功")
        self.assertEqual(len(self.failed), 1)
        reason = self.failed[0]
        self.assertIn("CH01", reason)
        self.assertTrue(
            "漂移" in reason or "末段" in reason or "异常点" in reason,
            f"失败信息应点名触发指标: {reason}",
        )
        self.assertIn("重新标定", reason)

    def test_slow_significant_drift_rejected(self):
        """缓慢但显著的漂移（+15 mV over 20 样本）不得判定稳定。"""
        self._run_values([1000.0 + 1.5 * i for i in range(20)])
        self.assertEqual(len(self.finished), 0)
        self.assertEqual(len(self.failed), 1)
        self.assertIn("CH01", self.failed[0])

    def test_stable_signal_and_small_noise_passes(self):
        """稳定信号 + 小噪声（±0.5 mV）必须通过，基线 ≈ 1000。"""
        values = [1000.0 + (0.5 if i % 2 else -0.5) for i in range(20)]
        self._run_values(values)
        self.assertEqual(len(self.failed), 0, f"不应失败: {self.failed}")
        self.assertEqual(len(self.finished), 1)
        self.assertAlmostEqual(self.finished[0][0], 1000.0, delta=1.0)

    def test_single_spike_passes_with_explicit_policy(self):
        """个别尖峰（1/16 < 20%）：中位数基线天然吸收，不一刀切失败。"""
        values = [1000.0] * 9 + [1050.0] + [1000.0] * 6
        self._run_values(values)
        self.assertEqual(len(self.failed), 0, f"单个尖峰不应失败: {self.failed}")
        self.assertEqual(len(self.finished), 1)
        self.assertAlmostEqual(self.finished[0][0], 1000.0, delta=2.0)

    def test_baseline_fixed_after_success_no_autotrack(self):
        """基线成功后固定：信号变化只体现在衰减量，不自动追踪基线。"""
        self._run_values([1000.0] * 10)
        self.assertEqual(len(self.finished), 1)
        ok = self.proc.apply_calibration(self.finished[0])
        self.assertTrue(ok)
        # 信号下移到 900（模拟放入钢板）：基线必须保持 1000
        _settle(self.proc, 900.0, scan_start=100, n=15)
        st = self.proc.get(0)
        self.assertAlmostEqual(st.baseline_mv, 1000.0, delta=1.0)
        snap = self.proc.measurement(0, True)
        self.assertIsNotNone(snap.attenuation_mv)
        self.assertGreater(snap.attenuation_mv, 80.0)

    def test_failed_calibration_preserves_old_baseline(self):
        """标定失败/取消：保留旧基线（原子性）。"""
        st = self.proc.get(0)
        st.baseline_mv = 1000.0
        self.proc.set_median_window(1)
        self.proc.set_ema_alpha(1.0)
        self._run_values([1000.0] * 15 + [800.0] * 5)
        self.assertEqual(len(self.finished), 0)
        self.assertAlmostEqual(st.baseline_mv, 1000.0)


# ---------------------------------------------------------------------------
# 主窗口测试基类：临时 config + 临时 CSV 目录（不碰用户真实数据）
# ---------------------------------------------------------------------------
class _MainWindowTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="prac_")
        import app_paths
        self._orig_csv_dir = app_paths.get_default_csv_dir
        app_paths.get_default_csv_dir = lambda: self._tmp  # type: ignore
        # 临时配置文件：绝不读写用户真实基线
        import ui.main_window as mw_mod
        from core.config_manager import ConfigManager
        self._cfg_path = os.path.join(self._tmp, "config.json")
        self._orig_cm = mw_mod.ConfigManager
        mw_mod.ConfigManager = lambda: ConfigManager(  # type: ignore
            config_path=self._cfg_path
        )

    def tearDown(self):
        import app_paths
        import ui.main_window as mw_mod
        app_paths.get_default_csv_dir = self._orig_csv_dir  # type: ignore
        mw_mod.ConfigManager = self._orig_cm  # type: ignore
        shutil.rmtree(self._tmp, ignore_errors=True)
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

    def _drive_calib(self, win: MainWindow, max_iter: int = 100) -> None:
        for _ in range(max_iter):
            _process_events(50)
            if not win._calib.is_running():
                break


# ---------------------------------------------------------------------------
# P1-3 局部重新标定后的基线同步（主窗口集成测试）
# ---------------------------------------------------------------------------
class TestPartialRecalibrationSync(_MainWindowTestBase):
    """任务书 四：重标定成功后，未参与该轮标定的通道基线失效。

    完整步骤（不只是配置文件内容）：标定 CH01+CH02 → 禁用 CH02 →
    重标定 CH01 → 重新启用 CH02，核对 processor / config / UI / CSV。
    """

    def _start_demo_two_channels(self, win):
        win._start_demo()
        _process_events(500)
        win._processor.set_enabled_channels([0, 1])
        win._demo.configure(enabled_channels=[0, 1])
        _process_events(300)
        win._confirm_plate_removed = lambda: True

    def _calibrate(self, win, target=6):
        win._sp_calib.setValue(target)
        win._on_calib_clicked()
        self._drive_calib(win)
        self.assertFalse(win._calib.is_running(), "标定应已结束")

    def test_full_partial_recalibration_flow(self):
        win = self._new_window()
        self._start_demo_two_channels(win)
        # 1) 标定 CH01+CH02
        self._calibrate(win)
        for c in (0, 1):
            self.assertIsNotNone(win._processor.get(c).baseline_mv)
        old_meta = win._config.get_baseline_meta("demo")
        self.assertIsNotNone(old_meta)
        old_id = old_meta["baseline_id"]
        # 2) 禁用 CH02（与设置对话框同一代码路径）
        win._processor.set_enabled_channels([0])
        win._demo.configure(enabled_channels=[0])
        _process_events(300)
        # 3) 重标定仅 CH01
        self._calibrate(win)
        # processor：CH02 基线已失效（内存与 config 同步）
        self.assertIsNone(
            win._processor.get(1).baseline_mv,
            "未参与本轮标定的通道基线必须失效",
        )
        self.assertIsNotNone(win._processor.get(0).baseline_mv)
        # config：CH02 为 None，baseline_id 已更新
        new_meta = win._config.get_baseline_meta("demo")
        self.assertIsNotNone(new_meta)
        self.assertNotEqual(new_meta["baseline_id"], old_id)
        cfg_map = win._config.get_baselines("demo")
        self.assertIsNotNone(cfg_map[0])
        self.assertIsNone(cfg_map[1])
        # 曲线基线参考线：CH02 已清除
        self.assertNotIn(1, win._plot._baseline_lines)
        # 4) 重新启用 CH02：采样显示、衰减量空、未标定
        win._processor.set_enabled_channels([0, 1])
        win._demo.configure(enabled_channels=[0, 1])
        _process_events(600)
        st2 = win._processor.get(1)
        self.assertIsNone(st2.baseline_mv)
        snap2 = win._processor.measurement(1, True)
        self.assertIsNotNone(snap2.sample_mv, "采样应照常显示")
        self.assertIsNone(snap2.attenuation_mv, "未标定通道衰减量必须为空")
        self.assertIsNone(snap2.detector_drop_pct)
        # 等预热完成后状态为"未标定"
        _process_events(400)
        self.assertIn(
            win._processor.get(1).last_status,
            (ChannelStatus.NOT_CALIBRATED, ChannelStatus.WARMING_UP),
        )
        # 5) CSV：记录行的 baseline_id 与新基线一致；CH02 派生列空
        win._config.last_csv_dir = self._tmp
        win._config.save()
        win._btn_rec.setChecked(True)
        _process_events(600)
        win._btn_rec.setChecked(False)
        _process_events(100)
        files = [f for f in os.listdir(self._tmp) if f.endswith(".csv")]
        self.assertGreater(len(files), 0)
        with open(os.path.join(self._tmp, files[0]),
                  encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertGreater(len(rows), 0)
        for row in rows:
            self.assertEqual(
                row["baseline_id"], new_meta["baseline_id"],
                "每行的 baseline_id 必须与当前实际基线一致",
            )
            self.assertEqual(row["ch02_attenuation_mv"], "")
            self.assertEqual(row["ch02_detector_drop_pct"], "")
            # CH01 基线列与新基线一致
            self.assertAlmostEqual(
                float(row["ch01_baseline_mv"]),
                win._processor.get(0).baseline_mv,
                places=3,
            )
        self._safe_close(win)

    def test_cancelled_recalibration_preserves_all_baselines(self):
        """取消/失败不清除任何基线（原子性）。"""
        win = self._new_window()
        self._start_demo_two_channels(win)
        self._calibrate(win)
        bl0 = win._processor.get(0).baseline_mv
        bl1 = win._processor.get(1).baseline_mv
        # 禁用 CH02 后重标定，但中途取消
        win._processor.set_enabled_channels([0])
        win._demo.configure(enabled_channels=[0])
        _process_events(200)
        win._sp_calib.setValue(50)  # 目标大，保证来得及取消
        win._on_calib_clicked()
        _process_events(300)
        self.assertTrue(win._calib.is_running())
        win._on_calib_cancel()
        _process_events(100)
        self.assertAlmostEqual(win._processor.get(0).baseline_mv, bl0)
        self.assertAlmostEqual(win._processor.get(1).baseline_mv, bl1)
        cfg_map = win._config.get_baselines("demo")
        self.assertIsNotNone(cfg_map[1])
        self._safe_close(win)

    def test_baseline_save_failure_stops_recording(self):
        """基线落盘失败：不得用旧 baseline_id 标注新基线（停止记录）。"""
        win = self._new_window()
        self._start_demo_two_channels(win)
        self._calibrate(win)
        # 开始记录（临时 CSV 目录）
        win._config.last_csv_dir = self._tmp
        win._config.save()
        win._btn_rec.setChecked(True)
        _process_events(300)
        self.assertTrue(win._recorder.is_recording)
        # 模拟落盘失败后重标定
        win._config.save = lambda: (False, "disk full (test)")  # type: ignore
        with patch("ui.main_window.QMessageBox.warning", MagicMock()):
            self._calibrate(win)
        self.assertFalse(
            win._recorder.is_recording,
            "基线落盘失败必须停止记录（避免旧 baseline_id 造假）",
        )
        self.assertFalse(win._btn_rec.isChecked())
        self._safe_close(win)


# ---------------------------------------------------------------------------
# P2-1 回放曲线使用实验原始时间
# ---------------------------------------------------------------------------
class TestPlotExplicitTimestamps(unittest.TestCase):
    """任务书 五（组件级）：显式时间戳 / 暂停 / seek 回退策略。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.plot = PlotWidget()
        self.plot.set_selected_channel(0)

    def tearDown(self):
        self.plot.deleteLater()

    def test_explicit_t_sets_x_axis(self):
        """t=0 与 t=60 的两点在图上必须相距 60 秒（大窗口）。"""
        self.plot.set_window(300.0)
        self.plot.add_point(0, 100.0, 100.0, t=0.0)
        self.plot.add_point(0, 101.0, 101.0, t=60.0)
        ts = self.plot._t_offsets[0]
        self.assertEqual(len(ts), 2)
        self.assertAlmostEqual(ts[1] - ts[0], 60.0)
        self.plot.add_attenuation_point(0, 5.0, t=0.0)
        self.plot.add_attenuation_point(0, 6.0, t=60.0)
        at = self.plot._atten_t[0]
        self.assertAlmostEqual(at[1] - at[0], 60.0)

    def test_x_coords_depend_only_on_t_not_arrival_rate(self):
        """X 坐标只由 t 决定（回放速度只影响调度不影响 X 轴）。"""
        series = []
        for _speed in (0.5, 1.0, 2.0):
            p = PlotWidget()
            p.set_selected_channel(0)
            for i, t in enumerate((0.0, 10.0, 20.0)):
                p.add_point(0, 100.0 + i, 100.0 + i, t=t)
                p.add_attenuation_point(0, float(i), t=t)
            series.append((list(p._t_offsets[0]), list(p._atten_t[0])))
            p.deleteLater()
        self.assertEqual(series[0], series[1])
        self.assertEqual(series[1], series[2])

    def test_pause_adds_no_points_no_fake_time(self):
        """暂停期间不加点、不产生假实验时间。"""
        self.plot.add_point(0, 100.0, 100.0, t=0.0)
        self.plot.set_paused(True)
        self.plot.add_point(0, 101.0, 101.0, t=10.0)  # 丢弃
        self.plot.set_paused(False)
        self.plot.add_point(0, 102.0, 102.0, t=20.0)
        self.assertEqual(list(self.plot._t_offsets[0]), [0.0, 20.0])

    def test_backward_seek_rebuilds_no_reversed_connection(self):
        """时间倒退（seek 回退）→ 清窗重建：绝不倒序连线。"""
        self.plot.add_point(0, 100.0, 100.0, t=0.0)
        self.plot.add_point(0, 101.0, 101.0, t=60.0)
        self.plot.add_attenuation_point(0, 5.0, t=60.0)
        # seek 回退到 t=30
        self.plot.add_point(0, 102.0, 102.0, t=30.0)
        self.plot.add_attenuation_point(0, 4.0, t=30.0)
        self.assertEqual(list(self.plot._t_offsets[0]), [30.0])
        self.assertEqual(list(self.plot._atten_t[0]), [30.0])
        # 时间戳严格单调（无回退连线）
        self.plot.add_point(0, 103.0, 103.0, t=40.0)
        ts = list(self.plot._t_offsets[0])
        self.assertEqual(ts, sorted(ts))
        self.assertEqual(ts, [30.0, 40.0])

    def test_missing_or_nonfinite_t_falls_back_to_internal_clock(self):
        """缺失/非有限原始时间 → 内部时钟兜底（不崩溃、不丢点）。"""
        self.plot.add_point(0, 100.0, 100.0, t=None)
        self.plot.add_point(0, 101.0, 101.0, t=float("nan"))
        self.plot.add_point(0, 102.0, 102.0, t=float("inf"))
        ts = list(self.plot._t_offsets[0])
        self.assertEqual(len(ts), 3)
        self.assertTrue(all(math.isfinite(t) for t in ts))
        self.assertEqual(ts, sorted(ts))


def _make_replay_row(t_rel, scan, valid=1, wire=800.0,
                     filtered=799.0, baseline=1000.0, attenuation=201.0,
                     drop_pct=20.1, status=5):
    n = 16
    return ReplayRow(
        t_rel=t_rel, scan_number=scan, status=0,
        valid_mask=0xFFFF if valid else 0x0, i2c_err=0,
        raw_valid=[valid] * n,
        wire_mv=[wire] * n,
        filtered=[filtered] * n,
        baseline=[baseline] * n,
        delta=[(filtered - baseline) if filtered is not None else None] * n,
        delta_signed=[(filtered - baseline)
                      if filtered is not None else None] * n,
        percent_signed=[-20.1] * n,
        attenuation=[attenuation] * n,
        drop_pct=[drop_pct] * n,
        ch_status=[status] * n,
    )


class TestReplayUsesFileTime(_MainWindowTestBase):
    """任务书 五（集成级）：回放行 t_rel 驱动两条曲线的 X 轴。"""

    def _open_replay_window(self):
        win = self._new_window()
        win._set_app_mode("replay")
        win._select_channel(0)
        return win

    def test_replay_rows_plot_at_t_rel(self):
        """t_rel=0 与 t_rel=60 的两行在图上相距 60 秒（不是 ~0.002s）。"""
        win = self._open_replay_window()
        win._plot.set_window(300.0)
        win._on_replay_frame(_make_replay_row(0.0, 1))
        win._on_replay_frame(_make_replay_row(60.0, 2))
        ts = win._plot._t_offsets[0]
        self.assertEqual(len(ts), 2)
        self.assertAlmostEqual(ts[1] - ts[0], 60.0)
        at = win._plot._atten_t[0]
        self.assertAlmostEqual(at[1] - at[0], 60.0)
        # 衰减量取文件保存值（不二次翻转）
        self.assertAlmostEqual(win._plot._atten_series[0][-1], 201.0)
        win._set_app_mode("live")
        self._safe_close(win)

    def test_replay_backward_seek_no_reversed_line(self):
        """回放 seek 回退：清窗重建，无倒序连线。"""
        win = self._open_replay_window()
        win._plot.set_window(300.0)
        win._on_replay_frame(_make_replay_row(0.0, 1))
        win._on_replay_frame(_make_replay_row(60.0, 2))
        # seek 回退到 t=30
        win._on_replay_frame(_make_replay_row(30.0, 3))
        self.assertEqual(list(win._plot._t_offsets[0]), [30.0])
        self.assertEqual(list(win._plot._atten_t[0]), [30.0])
        win._on_replay_frame(_make_replay_row(40.0, 4))
        ts = list(win._plot._t_offsets[0])
        self.assertEqual(ts, sorted(ts))
        win._set_app_mode("live")
        self._safe_close(win)

    def test_replay_invalid_row_residual_not_displayed(self):
        """旧 CSV 无效行的历史残留值不得显示成当前有效值。

        模拟旧版 bug 写出的 CSV：raw_valid=0 但 filtered/attenuation
        留有上一帧残留。回放时值可读取（文件原样），但显示层必须
        按无效处理：快照 None、曲线 NaN 断点。
        """
        win = self._open_replay_window()
        # 有效行
        win._on_replay_frame(_make_replay_row(0.0, 1))
        # 无效行：残留 filtered=1460.445 / attenuation=123.0（旧 bug）
        bad = _make_replay_row(
            1.0, 2, valid=0, wire=None,
            filtered=1460.445, attenuation=123.0,
        )
        win._on_replay_frame(bad)
        snap = win._processor.measurement(0, False)
        self.assertIsNone(snap.filtered_mv)
        self.assertIsNone(snap.attenuation_mv)
        self.assertIsNone(snap.sample_mv)
        # 基线作为历史参数保留显示
        self.assertIsNotNone(snap.baseline_mv)
        # 曲线：无效行为 NaN 断点（不是残留值连线）
        self.assertTrue(math.isnan(win._plot._filt_series[0][-1]))
        self.assertTrue(math.isnan(win._plot._atten_series[0][-1]))
        win._set_app_mode("live")
        self._safe_close(win)


# ---------------------------------------------------------------------------
# P2-2 无效帧端到端：processor / 详情 / tooltip / 曲线 / CSV 一致
# ---------------------------------------------------------------------------
class TestInvalidFrameEndToEnd(_MainWindowTestBase):
    """任务书 六：有效帧 → 无效帧 → 恢复帧，全链路一致性。"""

    def test_valid_invalid_recovered_frames_consistent(self):
        win = self._new_window()
        win._start_demo()
        _process_events(500)
        win._confirm_plate_removed = lambda: True
        win._sp_calib.setValue(6)
        win._on_calib_clicked()
        self._drive_calib(win)
        self.assertIsNotNone(win._processor.get(0).baseline_mv)
        win._select_channel(0)
        # 停掉演示源并冲刷事件队列：此后曲线/CSV 只含下面三个
        # 构造帧——断言锚定到确切索引，不与演示定时器时序耦合
        win._demo.stop()
        _process_events(150)
        # 开始记录（临时 CSV 目录）
        win._config.last_csv_dir = self._tmp
        win._config.save()
        win._btn_rec.setChecked(True)
        _process_events(100)
        self.assertTrue(win._recorder.is_recording)

        base_scan = 9000
        df_valid = make_d(scan=base_scan, mask=0xF,
                          samples=(800.0,) * MAX_CHANNEL_COUNT)
        df_bad = make_d(scan=base_scan + 1, mask=0x0,
                        samples=(0.0,) * MAX_CHANNEL_COUNT)
        df_back = make_d(scan=base_scan + 2, mask=0xF,
                         samples=(800.0,) * MAX_CHANNEL_COUNT)

        win._on_dframe(df_valid)
        snap_ok = win._processor.measurement(0, True)
        win._on_dframe(df_bad)
        win._update_all_displays()
        snap_bad = win._processor.measurement(0, False)
        # tooltip 文本（无效帧显示状态）
        cell0 = next(
            c for c in win._heatmap._cells.values() if c.channel == 0
        )
        tooltip_bad = cell0.toolTip()
        win._on_dframe(df_back)
        snap_back = win._processor.measurement(0, True)

        win._btn_rec.setChecked(False)
        _process_events(50)

        # ---- processor 快照 ----
        self.assertIsNotNone(snap_ok.filtered_mv)
        self.assertIsNone(snap_bad.filtered_mv, "无效帧不得给出当前滤波值")
        self.assertIsNone(snap_bad.attenuation_mv)
        self.assertIsNone(snap_bad.detector_drop_pct)
        self.assertIsNone(snap_bad.sample_mv, "无效帧不得显示残留采样")
        self.assertIsNotNone(snap_bad.baseline_mv, "基线是历史参数，保留")
        self.assertEqual(snap_bad.status, ChannelStatus.INVALID)
        self.assertIsNotNone(snap_back.filtered_mv, "恢复帧给出当前值")

        # ---- tooltip ----
        self.assertIn("本帧无效", tooltip_bad)
        self.assertNotIn("1460", tooltip_bad)  # 无残留数值泄漏
        # 无效帧 tooltip 不展示"当前"滤波/采样行（值为 None）
        self.assertNotIn("检波电压(滤波):", tooltip_bad)
        self.assertNotIn("串口采样:", tooltip_bad)

        # ---- 曲线：无效帧是 NaN 断点，不是残留值，也不是跳过连线 ----
        # 演示源已停：最后 3 个点恰好是三个构造帧（索引锚定，
        # 不依赖任何定时器时序）
        filt = win._plot._filt_series.get(0, [])
        atten = win._plot._atten_series.get(0, [])
        self.assertEqual(len(filt), len(win._plot._t_offsets.get(0, [])))
        self.assertGreaterEqual(len(filt), 3)
        self.assertFalse(math.isnan(filt[-3]), "有效帧应画真实滤波值")
        self.assertTrue(math.isnan(filt[-2]),
                        "无效帧应在滤波曲线上形成断点")
        self.assertFalse(math.isnan(filt[-1]), "恢复帧应画真实滤波值")
        self.assertGreaterEqual(len(atten), 3)
        self.assertFalse(math.isnan(atten[-3]))
        self.assertTrue(math.isnan(atten[-2]),
                        "无效帧应在衰减曲线上形成断点")
        self.assertFalse(math.isnan(atten[-1]))

        # ---- CSV：无效行派生列全空，wire 空，raw_valid=0 ----
        files = [f for f in os.listdir(self._tmp) if f.endswith(".csv")]
        self.assertGreater(len(files), 0)
        with open(os.path.join(self._tmp, files[0]),
                  encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        # 演示源停在后才开始记录：CSV 恰好 3 行构造帧
        self.assertEqual(len(rows), 3, "CSV 应恰好含三个构造帧")
        r_ok, r_bad, r_back = rows
        self.assertEqual(int(float(r_ok["帧号"])), base_scan)
        self.assertEqual(int(float(r_bad["帧号"])), base_scan + 1)
        self.assertEqual(int(float(r_back["帧号"])), base_scan + 2)
        self.assertEqual(r_bad["ch01_raw_valid"], "0")
        self.assertEqual(r_bad["ch01_wire_mv"], "")
        self.assertEqual(r_bad["ch01_filtered_mv"], "",
                         "无效帧 CSV 不得写残留滤波值")
        self.assertEqual(r_bad["ch01_attenuation_mv"], "")
        self.assertEqual(r_bad["ch01_delta_mv"], "")
        self.assertNotEqual(r_bad["ch01_baseline_mv"], "",
                            "基线作为历史参数保留在 CSV")
        self.assertEqual(r_ok["ch01_raw_valid"], "1")
        self.assertNotEqual(r_ok["ch01_filtered_mv"], "")
        self.assertEqual(r_back["ch01_raw_valid"], "1")
        self.assertNotEqual(r_back["ch01_filtered_mv"], "")
        self._safe_close(win)

    def test_saturated_frame_csv_keeps_wire_blanks_derived(self):
        """饱和帧：CSV 保留真实 rail wire（状态列标记），派生列全空。"""
        tmp = tempfile.mkdtemp(prefix="prac_sat_")
        try:
            profiles = build_default_profiles()
            rec = Recorder()
            proc = _new_processor(enabled=(0,))
            _settle(proc, 800.0, mask=0x1)
            proc.get(0).baseline_mv = 800.0
            path = rec.start_recording(
                {"frequency": 1000, "freq_unit": "kHz", "gain": "1",
                 "coil": "c", "plate": "", "note": ""},
                tmp, profiles["ads1115_4v096_div2"],
                session_meta={"source": "demo", "session_id": "sat-test"},
            )
            df_sat = make_d(scan=1, mask=0x1,
                            samples=(MV_MAX,) + (0.0,) * 15)
            proc.ingest(df_sat)
            ok, err = rec.write_dframe(df_sat, proc)
            self.assertTrue(ok, err)
            rec.stop_recording()
            with open(path, encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            r = rows[0]
            self.assertEqual(r["ch01_raw_valid"], "1")
            # 真实 rail 值保留（诊断），状态列明确标记饱和
            self.assertAlmostEqual(float(r["ch01_wire_mv"]), MV_MAX,
                                   places=3)
            self.assertEqual(int(r["ch01_status"]),
                             int(ChannelStatus.SATURATED))
            # 但 filtered/派生列一律为空（不冒充当前测量）
            self.assertEqual(r["ch01_filtered_mv"], "")
            self.assertEqual(r["ch01_attenuation_mv"], "")
            self.assertEqual(r["ch01_detector_drop_pct"], "")
            self.assertEqual(r["ch01_delta_mv"], "")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 对抗评审后续修复的回归锁（Phase B 复审 13 项确认发现）
# ---------------------------------------------------------------------------
class TestReviewFollowupsUnit(unittest.TestCase):
    """纯单元级：中值窗缩窗 / 标定提升 / 超时预算 / 卡死早判。"""

    def test_median_window_shrink_takes_effect_immediately(self):
        """运行中缩小中值窗立即生效（缓冲裁到新长度）。

        旧实现每次 update_mv 只弹 1 个元素：窗 3 改 1 后缓冲永远
        停在 3，新配置静默不生效。
        """
        proc = _new_processor(median_window=3)
        _settle(proc, 1000.0, n=3)
        st = proc.get(0)
        self.assertEqual(len(st._median_buf), 3)
        proc.set_median_window(1)
        self.assertLessEqual(len(st._median_buf), 1,
                             "缩窗必须立即裁剪缓冲")
        _feed(proc, make_d(scan=50, mask=0x1,
                           samples=(1000.0,) + (0.0,) * 15))
        self.assertEqual(len(st._median_buf), 1,
                         "新窗口下缓冲长度不得回到旧窗口值")
        self.assertTrue(st.filter_settled)

    def test_apply_calibration_does_not_promote_warming_up(self):
        """标定提交不得把 WARMING_UP 提升为 OK（绕过预热门槛）。"""
        proc = _new_processor(enabled=(0, 1))
        st0 = proc.get(0)
        st0.set_status(ChannelStatus.WARMING_UP)  # 中值窗未填满
        st1 = proc.get(1)
        st1.set_status(ChannelStatus.NOT_CALIBRATED)
        ok = proc.apply_calibration({0: 1000.0, 1: 1000.0})
        self.assertTrue(ok)
        self.assertEqual(
            st0.last_status, ChannelStatus.WARMING_UP,
            "预热中通道不得被提升为 OK（filtered/attenuation 仍须 None）",
        )
        self.assertEqual(st1.last_status, ChannelStatus.OK)
        # 预热通道快照仍无当前测量
        snap = proc.measurement(0, True)
        self.assertIsNone(snap.filtered_mv)
        self.assertIsNone(snap.attenuation_mv)

    def test_calib_total_timeout_scales_with_target(self):
        """整体超时随目标样本数放大：任何 UI 可选目标必须可达。

        旧实现恒 180 s：实机 ~3.5 fps 下目标 >~618 必然超时失败，
        UI 却允许选到 5000。
        """
        proc = _new_processor()
        calib = CalibrationManager(proc)
        try:
            calib.start(target_samples=1000)
            self.assertGreaterEqual(
                calib._max_total_s, 349.0,
                "目标 1000 时整体超时必须 ≥1000×0.35s（实机可达）",
            )
            calib.cancel()
            calib.start(target_samples=200)
            self.assertAlmostEqual(calib._max_total_s, 180.0,
                                   "默认目标保持 180 s 不变")
            calib.cancel()
            calib.start(target_samples=1000, max_total_s=30.0)
            self.assertAlmostEqual(calib._max_total_s, 30.0,
                                   "显式传入的超时优先")
            calib.cancel()
        finally:
            if calib.is_running():
                calib.cancel()

    def test_stuck_early_detection_reachable_at_default_target(self):
        """默认目标（200）下卡死早判必须可达（≤150 帧而非 800）。

        旧实现 4×200=800 帧 ≈ 229 s > 180 s 整体超时，早判分支
        在实机默认场景为死代码。
        """
        proc = _new_processor(enabled=(0, 1))
        calib = CalibrationManager(proc)
        failed = []
        calib.failed.connect(lambda r: failed.append(r))
        try:
            calib.start(target_samples=200)
            # CH0 持续有效、CH1 始终无效：150 帧内必须早判 CH02
            for i in range(150):
                if failed:
                    break
                _feed(proc, make_d(scan=i + 1, mask=0x1,
                                   samples=(1000.0,) + (0.0,) * 15),
                      calib=calib)
            self.assertTrue(failed, "150 帧内必须触发卡死早判")
            self.assertIn("CH02", failed[0])
        finally:
            if calib.is_running():
                calib.cancel()


class TestReplayTickAndRepair(unittest.TestCase):
    """回放调度：每行恰好发一次；非有限 t_rel 修复后不卡死。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="replay_fix_")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write_csv(self, name, t_values) -> str:
        path = os.path.join(self._tmp, name)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["相对时间/s", "帧号", "状态", "valid_mask",
                        "i2c_err", "ch01_delta_signed_mv",
                        "schema_version"])
            for i, t in enumerate(t_values):
                w.writerow([t, i + 1, 0, 0x1, 0, "-1.0", 4])
        return path

    def test_dense_rows_all_emitted_exactly_once(self):
        """行距 < 定时器间隔（50Hz 数据）：中间行不得被静默跳过。"""
        from core.replay_source import ReplaySource
        path = self._write_csv("dense.csv",
                               [0.0, 0.02, 0.04, 0.06, 0.08])
        src = ReplaySource()
        src.load(path)
        got = []
        src.frame_ready.connect(lambda r: got.append(r))
        src.play()
        src._start_monotonic -= 1.0  # 墙钟前进 1s → 全部到期
        src._on_tick()
        self.assertEqual([r.scan_number for r in got], [1, 2, 3, 4, 5],
                         "所有到期行必须逐行发出，不得只发最后一行")
        self.assertEqual(src.state, "ended")

    def test_no_duplicate_emit_when_index_not_advanced(self):
        """索引未前进时不得重复发行（低速播放不白耗曲线点预算）。"""
        from core.replay_source import ReplaySource
        path = self._write_csv("sparse.csv", [0.0, 10.0])
        src = ReplaySource()
        src.load(path)
        got = []
        src.frame_ready.connect(lambda r: got.append(r))
        src.play()
        src._on_tick()  # 首行立即到期
        self.assertEqual(len(got), 1)
        src._on_tick()  # 时间未推进 → 不得重发
        src._on_tick()
        self.assertEqual(len(got), 1, "同一行不得重复 emit")
        self.assertEqual(src.state, "playing")

    def test_nan_t_rel_repaired_and_replay_completes(self):
        """t_rel=nan：修复为前一行+中位行距，回放不冻结、能结束。"""
        from core.replay_source import ReplaySource
        path = self._write_csv("nan.csv", [0.0, "nan", 2.0])
        src = ReplaySource()
        src.load(path)
        self.assertEqual(src.t_repaired_count, 1)
        self.assertTrue(all(math.isfinite(r.t_rel) for r in src._rows))
        self.assertAlmostEqual(src._rows[1].t_rel, 1.0,
                               "坏行修复为上一有效时间+行距")
        got = []
        src.frame_ready.connect(lambda r: got.append(r))
        src.play()
        src._start_monotonic -= 5.0
        src._on_tick()
        self.assertEqual(len(got), 3, "坏行之后的有效行必须可达")
        self.assertEqual(src.state, "ended",
                         "含修复行的文件必须能播完，不得永停 playing")

    def test_first_row_nan_falls_back_to_zero(self):
        """首行即 nan：兜底 0.0（不得让 _t_rel_at_start 变 NaN）。"""
        from core.replay_source import ReplaySource
        path = self._write_csv("nan_first.csv", ["nan", 1.0])
        src = ReplaySource()
        src.load(path)
        self.assertEqual(src.t_repaired_count, 1)
        self.assertAlmostEqual(src._rows[0].t_rel, 0.0)
        got = []
        src.frame_ready.connect(lambda r: got.append(r))
        src.play()
        src._start_monotonic -= 5.0
        src._on_tick()
        self.assertEqual(len(got), 2)
        self.assertEqual(src.state, "ended")


class TestReviewFollowupsMainWindow(_MainWindowTestBase):
    """主窗口级：回放标定禁用 / 自动色阶 / 饱和首帧曲线 / 详情面板。"""

    def test_replay_mode_calib_button_stays_disabled(self):
        """切回放取消标定后，收尾槽不得重新启用标定按钮（幻影标定）。"""
        win = self._new_window()
        win._start_demo()
        _process_events(500)
        win._confirm_plate_removed = lambda: True
        win._sp_calib.setValue(100)  # 大目标：保持标定进行中
        win._on_calib_clicked()
        self.assertTrue(win._calib.is_running())
        win._set_app_mode("replay")  # 同步 cancel 进行中的标定
        self.assertFalse(win._calib.is_running())
        self.assertFalse(
            win._btn_calib.isEnabled(),
            "回放模式下标定按钮必须保持禁用",
        )
        win._on_calib_clicked()  # 纵深防御：点击也不得启动
        self.assertFalse(win._calib.is_running(),
                         "回放模式不得启动幻影标定")
        win._set_app_mode("live")
        self._safe_close(win)

    def test_heatmap_auto_range_follows_replay_attenuation(self):
        """回放模式自动色阶必须跟随回放衰减量（不得冻结在实时值）。"""
        win = self._new_window()
        win._set_app_mode("replay")
        win._processor.set_enabled_channels([0])
        win._heatmap_auto = True
        # 模拟长会话：跳过 >1.5× 突变的保持期去抖（该去抖与实时
        # 路径一致，不是本测试对象；本测试锁的是"回放中衰减量
        # 不再恒 None 导致色阶冻结"）
        win._heatmap_auto_max_set_time = 0.0
        win._select_channel(0)
        win._on_replay_frame(_make_replay_row(0.0, 1, attenuation=500.0))
        self.assertGreaterEqual(
            win._heatmap_range_max, 500.0,
            "自动色阶必须随回放衰减量扩程（旧实现恒 None 冻结）",
        )
        win._set_app_mode("live")
        self._safe_close(win)

    def test_first_frame_saturated_channel_plots_rail_point(self):
        """首帧即饱和的启用通道：曲线画 rail 诊断点，不得整条空白。"""
        win = self._new_window()
        win._select_channel(0)
        df = make_d(scan=1, mask=0x1,
                    samples=(MV_MAX,) + (0.0,) * 15)
        win._on_dframe(df)
        raw = win._plot._raw_series.get(0, [])
        self.assertEqual(len(raw), 1,
                         "饱和帧的 rail 诊断值必须画到原始曲线")
        self.assertAlmostEqual(raw[-1], MV_MAX, places=3)
        atten = win._plot._atten_series.get(0, [])
        self.assertEqual(len(atten), 1, "衰减曲线同帧推进（NaN 断点）")
        self.assertTrue(math.isnan(atten[-1]))
        self._safe_close(win)

    def test_panel_shows_sample_during_warmup_and_saturation(self):
        """详情面板与快照同源：预热/饱和时采样显示真实值（非 "—"）。"""
        from ui.channel_panel import ChannelPanel
        panel = ChannelPanel()
        try:
            proc = _new_processor()
            # 预热中：1 个有效样本（中值窗 3 未满）
            _feed(proc, make_d(scan=1, mask=0x1,
                               samples=(800.0,) + (0.0,) * 15))
            st = proc.get(0)
            self.assertEqual(st.last_status, ChannelStatus.WARMING_UP)
            snap = proc.measurement(0, True)
            panel.update_snapshot(
                0, snap, None, st.last_update_monotonic
            )
            self.assertNotEqual(
                panel._m_raw._val.text(), "—",
                "预热中的真实采样必须显示（与 tooltip 同源）",
            )
            self.assertEqual(panel._m_filtered._val.text(), "—",
                             "预热期滤波值留空")
            # 饱和：rail 诊断值显示
            _feed(proc, make_d(scan=2, mask=0x1,
                               samples=(MV_MAX,) + (0.0,) * 15))
            snap2 = proc.measurement(0, True)
            panel.update_snapshot(
                0, snap2, None, st.last_update_monotonic
            )
            self.assertAlmostEqual(
                float(panel._m_raw._val.text()), MV_MAX, places=2,
            )
            self.assertEqual(panel._m_filtered._val.text(), "—")
        finally:
            panel.deleteLater()


if __name__ == "__main__":
    unittest.main()
