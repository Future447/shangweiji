"""
无钢板基线标定流程测试（core/calibration_manager.py）。

验收点（任务书 三 / 八-5）：
- 滤波预热：前 WARMUP_FRAMES 帧只预热不计数
- 同一帧（同 scan_number）不得重复计数
- 无效 / 非有限 / 饱和样本不进入标定
- 非当前会话的帧被剔除
- 取消 / 失败保留旧基线（原子提交，绝不半成品覆盖）
- 持续有帧但某通道始终无有效样本 → 卡死早判，失败原因列出通道
- 无新帧停滞超时、整体超时
- 基线不稳定（原始样本 MAD/|median| 超门限）→ 失败
- 质量报告：原始域噪声/漂移/异常点
- 端到端：median/EMA 真实收敛后基线接近样本真值
"""

import os
import sys
import time
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from PyQt5.QtCore import QCoreApplication
from PyQt5.QtWidgets import QApplication

from core.calibration_manager import (
    CalibrationManager,
    MIN_STUCK_FRAMES,
    STABILITY_MAX_CV_MAD,
    STUCK_FRAME_FACTOR,
    WARMUP_FRAMES,
)
from core.channel_processor import ChannelProcessor
from models.frame import DFrame, MV_MAX, MV_MIN


def make_d(scan=1, mask=0xF, samples=None) -> DFrame:
    if samples is None:
        samples = tuple(100.0 + i for i in range(16))
    return DFrame(
        scan_number=scan,
        scan_status=0,
        valid_mask=mask,
        i2c_error_count=0,
        samples_mv=tuple(float(s) for s in samples),
    )


def _pump(ms=10):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.002)


class CalibrationFlowBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.proc = ChannelProcessor(enabled_channels=[0, 1, 2, 3])
        self.calib = CalibrationManager(self.proc)

    def tearDown(self):
        if self.calib.is_running():
            self.calib.cancel()
        _pump(5)

    def _feed(self, df, session_id=None):
        self.proc.ingest(df)
        self.calib.on_frame(df, session_id=session_id)


class TestWarmup(CalibrationFlowBase):
    def test_warmup_frames_not_counted(self):
        """前 WARMUP_FRAMES 个新帧只预热滤波，不进入样本累计。"""
        self.assertGreaterEqual(WARMUP_FRAMES, 1)
        self.calib.start(target_samples=3)
        for i in range(WARMUP_FRAMES):
            self._feed(make_d(scan=i + 1))
        snap = self.calib.state_snapshot()
        self.assertEqual(snap["current_samples"], 0)
        self.assertEqual(
            snap["per_channel"].get(0, 0), 0,
            "预热帧不得计入样本数",
        )
        self.assertEqual(snap["warmup_remaining"], 0)

    def test_completion_needs_warmup_plus_target_frames(self):
        """target=N 需要 WARMUP + N 个不同帧才能完成。"""
        finished = []
        self.calib.finished.connect(lambda b: finished.append(b))
        self.calib.start(target_samples=3)
        total = WARMUP_FRAMES + 3
        for i in range(total - 1):
            self._feed(make_d(scan=i + 1))
        self.assertFalse(finished, "预热 + target-1 帧不应完成")
        self._feed(make_d(scan=total))
        self.assertEqual(len(finished), 1, "预热 + target 帧后应完成")

    def test_warmup_zero_counts_once_per_channel_filter_settles(self):
        """warmup_frames=0：全局预热关闭，但每通道滤波未稳定不计数。

        标定不得采纳未稳定的滤波值——预热进度按本通道被滤波链
        实际接纳的有效样本计数（中值窗填满），而非全局帧数。
        同帧仍去重。
        """
        calib = CalibrationManager(self.proc, warmup_frames=0)
        try:
            calib.start(target_samples=5)
            # 前 median_window-1 帧：通道滤波预热中，不计数
            for i in (1, 2):
                df = make_d(scan=i)
                self.proc.ingest(df)
                calib.on_frame(df)
                self.assertEqual(
                    calib.state_snapshot()["current_samples"], 0,
                    "通道滤波预热期样本不得计入标定",
                )
            # 第 median_window 帧：中值窗填满（filter_settled）→ 计入
            df3 = make_d(scan=3)
            self.proc.ingest(df3)
            calib.on_frame(df3)
            self.assertEqual(
                calib.state_snapshot()["current_samples"], 1,
                "通道滤波稳定后应立即计数",
            )
            calib.on_frame(df3)  # 同 scan 去重 → 不再增加
            self.assertEqual(
                calib.state_snapshot()["current_samples"], 1
            )
        finally:
            if calib.is_running():
                calib.cancel()


class TestDedupAndSampleGates(CalibrationFlowBase):
    def test_same_frame_not_counted_twice(self):
        """同一帧同一通道不得重复计数（同 scan_number 去重）。"""
        self.calib.start(target_samples=5)
        df = make_d(scan=42)
        self.proc.ingest(df)
        for _ in range(20):
            self.calib.on_frame(df)
        self.assertEqual(self.calib.state_snapshot()["frames_seen"], 1)
        self.assertLessEqual(
            self.calib.state_snapshot()["current_samples"], 1
        )

    def test_invalid_channel_not_counted(self):
        """valid_mask=0 的通道不计数。"""
        self.proc.set_enabled_channels([0, 1])
        self.calib.start(target_samples=3)
        for i in range(WARMUP_FRAMES + 3):
            self._feed(make_d(scan=i + 1, mask=0x1))
        snap = self.calib.state_snapshot()
        self.assertEqual(snap["per_channel"].get(0, 0), 3)
        self.assertEqual(snap["per_channel"].get(1, 0), 0)

    def test_saturated_samples_excluded(self):
        """触及量程上下限的样本（饱和）不进入标定。"""
        self.proc.set_enabled_channels([0])
        self.calib.start(target_samples=2)
        n = WARMUP_FRAMES + 4
        for i in range(n):
            samples = [MV_MAX] + [100.0] * 15  # CH0 饱和
            self._feed(make_d(scan=i + 1, mask=0xFFFF, samples=samples))
        self.assertEqual(
            self.calib.state_snapshot()["per_channel"].get(0, 0), 0,
            "饱和样本不得计入标定",
        )

    def test_rail_min_excluded(self):
        self.proc.set_enabled_channels([0])
        self.calib.start(target_samples=2)
        for i in range(WARMUP_FRAMES + 2):
            samples = [MV_MIN] + [100.0] * 15
            self._feed(make_d(scan=i + 1, mask=0xFFFF, samples=samples))
        self.assertEqual(
            self.calib.state_snapshot()["per_channel"].get(0, 0), 0
        )

    def test_session_gate(self):
        """非当前会话的 D 帧被剔除，且不占帧数。"""
        self.calib.start(target_samples=2, session_id="sess-A")
        for i in range(5):
            self._feed(make_d(scan=i + 1), session_id="sess-B")
        snap = self.calib.state_snapshot()
        self.assertEqual(snap["frames_seen"], 0)
        self.assertEqual(snap["current_samples"], 0)
        # 正确会话的帧正常计数
        for i in range(WARMUP_FRAMES + 2):
            self._feed(make_d(scan=100 + i), session_id="sess-A")
        self.assertEqual(
            self.calib.state_snapshot()["current_samples"], 2
        )


class TestAtomicity(CalibrationFlowBase):
    def test_cancel_preserves_old_baseline(self):
        """取消：旧基线完整保留。"""
        self.proc.apply_calibration({0: 100.0, 1: 200.0,
                                     2: 300.0, 3: 400.0})
        cancelled = []
        self.calib.cancelled.connect(lambda: cancelled.append(True))
        self.calib.start(target_samples=5)
        for i in range(WARMUP_FRAMES + 2):
            self._feed(make_d(scan=i + 1))
        self.calib.cancel()
        _pump()
        self.assertEqual(len(cancelled), 1)
        self.assertAlmostEqual(self.proc.get(0).baseline_mv, 100.0)

    def test_failure_preserves_old_baseline(self):
        """标定失败（不稳定）：旧基线不被半成品覆盖。"""
        self.proc.apply_calibration({0: 999.0, 1: 200.0,
                                     2: 300.0, 3: 400.0})
        failed = []
        finished = []
        self.calib.failed.connect(lambda r: failed.append(r))
        self.calib.finished.connect(lambda b: finished.append(b))
        self.calib.start(target_samples=4)
        # 交替 1000/1300 → MAD/|median| ≈ 0.23 >> 门限 → 不稳定失败
        for i in range(WARMUP_FRAMES + 4):
            v = 1000.0 if i % 2 == 0 else 1300.0
            self._feed(make_d(scan=i + 1, samples=[v] * 16))
        self.assertEqual(len(failed), 1)
        self.assertFalse(finished)
        self.assertIn("不稳定", failed[0])
        self.assertAlmostEqual(self.proc.get(0).baseline_mv, 999.0)

    def test_finished_baselines_atomic_dict(self):
        """成功：一次给出所有启用通道的基线。"""
        finished = []
        self.calib.finished.connect(lambda b: finished.append(b))
        self.calib.start(target_samples=3)
        for i in range(WARMUP_FRAMES + 3):
            self._feed(make_d(scan=i + 1))
        self.assertEqual(len(finished), 1)
        self.assertEqual(set(finished[0].keys()), {0, 1, 2, 3})
        for v in finished[0].values():
            self.assertIsNotNone(v)

    def test_start_while_running_fails(self):
        self.calib.start(target_samples=5)
        failed = []
        self.calib.failed.connect(lambda r: failed.append(r))
        self.calib.start(target_samples=5)
        self.assertTrue(any("正在进行" in r for r in failed))

    def test_no_enabled_channels_fails(self):
        self.proc.set_enabled_channels([])
        failed = []
        self.calib.failed.connect(lambda r: failed.append(r))
        self.calib.start(target_samples=5)
        self.assertEqual(len(failed), 1)


class TestStuckAndTimeout(CalibrationFlowBase):
    def test_stuck_channel_early_fail_lists_channels(self):
        """帧一直来但 CH02 始终无有效样本 → 卡死早判并列出通道。"""
        self.proc.set_enabled_channels([0, 1])
        failed = []
        self.calib.failed.connect(lambda r: failed.append(r))
        self.calib.start(target_samples=3)
        stuck_limit = max(
            MIN_STUCK_FRAMES,
            STUCK_FRAME_FACTOR * self.calib._target_frames,
        )
        for i in range(stuck_limit + 1):
            self._feed(make_d(scan=i + 1, mask=0x1))  # CH02 永远无效
        self.assertEqual(len(failed), 1)
        self.assertIn("CH02", failed[0])
        self.assertIn("始终无有效样本", failed[0])

    def test_idle_timeout_fails(self):
        """停滞超时：上次新帧超过 max_idle_s → 失败。"""
        failed = []
        self.calib.failed.connect(lambda r: failed.append(r))
        self.calib.start(target_samples=100, max_idle_s=3.0)
        self._feed(make_d(scan=1))
        # 模拟"最后一帧已过去很久"
        self.calib._last_new_frame_monotonic = time.monotonic() - 10.0
        self.calib._on_watchdog()
        self.assertEqual(len(failed), 1)
        self.assertIn("未收到新 D 帧", failed[0])

    def test_no_frame_at_all_timeout(self):
        """开始后一直没有帧 → 失败。"""
        failed = []
        self.calib.failed.connect(lambda r: failed.append(r))
        self.calib.start(target_samples=100, max_idle_s=3.0)
        self.calib._start_monotonic = time.monotonic() - 10.0
        self.calib._on_watchdog()
        self.assertEqual(len(failed), 1)
        self.assertIn("无新 D 帧", failed[0])

    def test_overall_timeout_lists_lacking_channels(self):
        """整体超时：有帧但样本凑不齐 → 失败并列出缺口通道。"""
        self.proc.set_enabled_channels([0, 1])
        failed = []
        self.calib.failed.connect(lambda r: failed.append(r))
        self.calib.start(target_samples=100, max_total_s=30.0)
        self._feed(make_d(scan=1, mask=0x1))  # CH1 永远没有样本
        self.calib._start_monotonic = time.monotonic() - 40.0
        self.calib._on_watchdog()
        self.assertEqual(len(failed), 1)
        self.assertIn("整体超时", failed[0])
        self.assertIn("CH02", failed[0])


class TestQualityReport(CalibrationFlowBase):
    def _run_stable(self, values):
        quality = []
        finished = []
        self.calib.quality_report.connect(lambda q: quality.append(q))
        self.calib.finished.connect(lambda b: finished.append(b))
        self.proc.set_enabled_channels([0])
        self.calib.start(target_samples=len(values))
        scan = 0
        for _ in range(WARMUP_FRAMES):
            scan += 1
            self._feed(make_d(scan=scan, samples=[values[0]] * 16))
        for v in values:
            scan += 1
            self._feed(make_d(scan=scan, samples=[v] * 16))
        return quality, finished

    def test_quality_fields_present(self):
        """质量报告包含原始域噪声/漂移/异常点统计。"""
        quality, finished = self._run_stable([1000.0] * 6)
        self.assertEqual(len(finished), 1)
        self.assertEqual(len(quality), 1)
        q = quality[0][0]
        for key in ("mean", "median", "std", "mad", "n",
                    "cv_mad", "drift_mv", "outliers", "baseline_mv"):
            self.assertIn(key, q)
        self.assertEqual(q["n"], 6)
        self.assertAlmostEqual(q["cv_mad"], 0.0)
        self.assertEqual(q["outliers"], 0)

    def test_quality_detects_outlier(self):
        """MAD≈0 时以绝对下限识别异常点（低于下限的偏差算普通噪声）。

        下限为 OUTLIER_MIN_GATE_MV（5 mV ≈ 40 LSB）：小样本量化聚集
        会让 MAD 低估真实散布，更小的下限会把正常前端噪声误判为
        异常点。单个尖峰占比低，不阻断标定（中位数基线天然吸收）。
        """
        values = [1000.0] * 9 + [1010.0]
        quality, finished = self._run_stable(values)
        self.assertEqual(len(finished), 1)
        self.assertGreaterEqual(quality[0][0]["outliers"], 1)
        # 3 mV 偏差低于绝对下限：不算异常点（普通噪声）
        quality2, finished2 = self._run_stable([1000.0] * 9 + [1003.0])
        self.assertEqual(len(finished2), 1)
        self.assertEqual(quality2[0][0]["outliers"], 0)

    def test_quality_detects_drift(self):
        """缓变斜坡 → drift_mv 明显为正，但不触发不稳定门限。"""
        values = [1000.0 + 1.0 * i for i in range(10)]
        quality, finished = self._run_stable(values)
        self.assertEqual(len(finished), 1)
        self.assertGreater(quality[0][0]["drift_mv"], 3.0)

    def test_unstable_rejected_by_threshold(self):
        """门限语义：STABILITY_MAX_CV_MAD 是唯一来源（修复 0.05/0.5 分叉）。"""
        self.assertAlmostEqual(STABILITY_MAX_CV_MAD, 0.05)
        self.assertAlmostEqual(
            self.calib._stability_max_cv_mad, STABILITY_MAX_CV_MAD
        )


class TestMedianAndScanReset(CalibrationFlowBase):
    def test_median_even_n_averages_two_middles(self):
        """偶数样本取两中值平均。

        修复：旧实现取上中位值，基线相对真实中位水平系统性偏高
        半个间距，所有后续衰减量随之偏移。MAD 同此入口。
        """
        self.assertAlmostEqual(CalibrationManager._median([1.0, 2.0]), 1.5)
        self.assertAlmostEqual(
            CalibrationManager._median([4.0, 1.0, 3.0, 2.0]), 2.5
        )
        self.assertAlmostEqual(
            CalibrationManager._median([3.0, 1.0, 2.0]), 2.0
        )

    def test_scan_reset_recovers_dedup(self):
        """scan_number 回绕/设备重启（scan 变小）→ 清空去重缓存。

        否则新循环的 scan=1.. 会被当成"已见过的重复帧"吞掉，
        标定在设备重启后永远凑不齐样本。
        """
        self.calib.start(target_samples=100)
        for i in range(1, 6):  # scan 1..5
            self._feed(make_d(scan=i))
        self.assertEqual(self.calib.state_snapshot()["frames_seen"], 5)
        # 设备重启：scan 从 1 重新来——必须继续计数
        for i in range(1, 4):
            self._feed(make_d(scan=i))
        self.assertEqual(
            self.calib.state_snapshot()["frames_seen"], 5 + 3,
            "scan 回绕后新帧不得被去重缓存吞掉",
        )
        # 同一循环内的重复帧仍然去重
        self._feed(make_d(scan=3))
        self.assertEqual(
            self.calib.state_snapshot()["frames_seen"], 5 + 3,
            "同循环重复帧仍应去重",
        )


class TestEndToEndConvergence(CalibrationFlowBase):
    def test_baseline_converges_to_true_value(self):
        """端到端：真实 median/EMA 收敛后基线接近样本真值。"""
        finished = []
        self.calib.finished.connect(lambda b: finished.append(b))
        self.proc.set_enabled_channels([0])
        self.proc.set_ema_alpha(0.5)
        self.calib.start(target_samples=8)
        for i in range(WARMUP_FRAMES + 8):
            self._feed(make_d(scan=i + 1, mask=0x1,
                              samples=[1500.0] + [0.0] * 15))
        self.assertEqual(len(finished), 1)
        self.assertAlmostEqual(finished[0][0], 1500.0, delta=2.0)

    def test_baseline_robust_to_single_spike(self):
        """中位数基线：单个尖峰不显著拉偏。"""
        finished = []
        self.calib.finished.connect(lambda b: finished.append(b))
        self.proc.set_enabled_channels([0])
        self.proc.set_ema_alpha(0.5)
        self.calib.start(target_samples=7)
        values = [1000.0, 1000.0, 1400.0,
                  1000.0, 1000.0, 1000.0, 1000.0]
        scan = 0
        for _ in range(WARMUP_FRAMES):
            scan += 1
            self._feed(make_d(scan=scan, mask=0x1,
                              samples=[1000.0] + [0.0] * 15))
        for v in values:
            scan += 1
            self._feed(make_d(scan=scan, mask=0x1,
                              samples=[v] + [0.0] * 15))
        # 尖峰 1400 vs 中位 1000：MAD≈0 → 被记为异常点但不应失败
        self.assertEqual(len(finished), 1)
        self.assertAlmostEqual(finished[0][0], 1000.0, delta=60.0)


if __name__ == "__main__":
    unittest.main()
