"""
通道处理 / ADC Profile / 标定测试（v3 毫伏协议）。

- 数据链路全部 mV：ingest → 中值+EMA 滤波 → 基线 → Δ
- 饱和判定基于 mV 量程边界（±4096 mV）
- AdcProfile 的 raw_to_* 为计数域遗留接口（串口路径不再调用）；
  mv_to_detector_mv 为 v3 的 VOUT 估算接口（Vadc × 分压比）
"""

import math
import os
import tempfile
import time
import unittest

from core.channel_processor import ChannelProcessor
from core.calibration_manager import CalibrationManager
from core.demo_source import DemoSource
from models.adc_profile import (
    AdcProfile, build_default_profiles, profile_by_name,
)
from models.channel_state import ChannelStatus
from models.frame import DFrame, MAX_CHANNEL_COUNT, MV_MAX, MV_MIN


def make_d(
    scan=1, mask=0xF,
    samples=(100.0, 200.0, 300.0, 400.0) + (0.0,) * 12,
) -> DFrame:
    return DFrame(
        scan_number=scan,
        scan_status=0,
        valid_mask=mask,
        i2c_error_count=0,
        samples_mv=tuple(float(s) for s in samples),
    )


class TestAdcProfile(unittest.TestCase):
    """计数域遗留接口 + v3 mV 接口。"""

    def setUp(self):
        self.profiles = build_default_profiles()

    def test_default_is_ads1115(self):
        p = self.profiles["ads1115_4v096_div2"]
        self.assertEqual(p.name, "ads1115_4v096_div2")
        self.assertTrue(p.raw_is_signed)
        self.assertEqual(p.raw_count, 32768)
        self.assertAlmostEqual(p.detector_divider, 2.0)

    def test_raw_to_vadc_zero(self):
        p = self.profiles["ads1115_4v096_div2"]
        self.assertEqual(p.raw_to_vadc_mv(0), 0)

    def test_raw_to_vadc_max_positive(self):
        p = self.profiles["ads1115_4v096_div2"]
        self.assertEqual(p.raw_to_vadc_mv(32767), 4096)

    def test_raw_to_vadc_negative(self):
        p = self.profiles["ads1115_4v096_div2"]
        self.assertEqual(p.raw_to_vadc_mv(-32768), -4096)

    def test_detector_doubled(self):
        p = self.profiles["ads1115_4v096_div2"]
        v = p.raw_to_vadc_mv(16384)
        d = p.raw_to_detector_mv(16384)
        self.assertEqual(d, 2 * v)

    def test_stm32_profile(self):
        p = self.profiles["stm32_adc12"]
        self.assertFalse(p.raw_is_signed)
        self.assertEqual(p.raw_to_vadc_mv(0), 0)
        self.assertEqual(p.raw_to_vadc_mv(4095), 3300)

    def test_saturation(self):
        p = self.profiles["ads1115_4v096_div2"]
        self.assertTrue(p.is_saturated(32767))
        self.assertTrue(p.is_saturated(-32768))
        self.assertFalse(p.is_saturated(0))

    def test_profile_by_name_fallback(self):
        p = profile_by_name("not_exist", self.profiles)
        self.assertEqual(p.name, "ads1115_4v096_div2")

    def test_mv_to_detector_mv(self):
        """v3：VOUT 估算 = Vadc(mV) × 分压比（×2），无 raw 反算。"""
        p = self.profiles["ads1115_4v096_div2"]
        self.assertAlmostEqual(p.mv_to_detector_mv(424.625), 849.25)
        self.assertAlmostEqual(p.mv_to_detector_mv(0.0), 0.0)
        self.assertAlmostEqual(p.mv_to_detector_mv(-100.0), -200.0)


class TestChannelState(unittest.TestCase):
    def test_delta_no_baseline(self):
        from models.channel_state import ChannelState
        s = ChannelState(channel=0)
        s.update_mv(100.0)
        self.assertIsNone(s.delta())
        self.assertIsNone(s.relative_percent())

    def test_delta_with_baseline_mv(self):
        from models.channel_state import ChannelState
        s = ChannelState(channel=0, baseline_mv=200.0)
        s.update_mv(150.0)
        self.assertAlmostEqual(s.filtered_mv, 150.0)
        self.assertAlmostEqual(s.delta(), -50.0)
        self.assertAlmostEqual(s.relative_percent(), -25.0)

    def test_compat_aliases(self):
        """旧接口名 raw/filtered/baseline 仍可用（mV 语义）。"""
        from models.channel_state import ChannelState
        s = ChannelState(channel=0)
        s.update_raw(424.625)
        self.assertEqual(s.raw, 424.625)
        self.assertEqual(s.sample_mv, 424.625)
        self.assertEqual(s.filtered, s.filtered_mv)
        s.baseline = 400.0
        self.assertEqual(s.baseline_mv, 400.0)

    def test_mv_precision_kept(self):
        """0.125 mV 步进精度在滤波链中不丢失。"""
        from models.channel_state import ChannelState
        s = ChannelState(channel=0, ema_alpha=1.0)
        s.update_mv(424.625)
        self.assertEqual(s.filtered_mv, 424.625)

    def test_baseline_zero_safety(self):
        from models.channel_state import ChannelState
        s = ChannelState(channel=0, baseline_mv=0.0)
        s.update_mv(100.0)
        self.assertIsNone(s.relative_percent())
        self.assertIsNotNone(s.delta())

    def test_median_filter(self):
        from models.channel_state import ChannelState
        s = ChannelState(channel=0, baseline_mv=100.0, median_window=3)
        s.update_mv(100.0)
        s.update_mv(200.0)
        s.update_mv(100.0)
        self.assertIsNotNone(s.filtered_mv)
        self.assertGreater(s.filtered_mv, 90)
        self.assertLess(s.filtered_mv, 180)

    def test_ema_alpha(self):
        from models.channel_state import ChannelState
        s = ChannelState(channel=0, baseline_mv=100.0, ema_alpha=0.5,
                         median_window=1)
        s.update_mv(1000.0)
        s.update_mv(1000.0)
        self.assertAlmostEqual(s.filtered_mv, 1000.0)


class TestChannelProcessor(unittest.TestCase):
    def setUp(self):
        self.proc = ChannelProcessor(
            adc_profile=profile_by_name("ads1115_4v096_div2",
                                        build_default_profiles()),
            enabled_channels=[0, 1, 2, 3],
        )

    def test_ingest_4ch(self):
        df = make_d()
        self.proc.ingest(df)
        for c in (0, 1, 2, 3):
            self.assertIsNotNone(self.proc.get(c).filtered_mv)
        self.assertEqual(
            self.proc.get(15).last_status, ChannelStatus.NO_DATA
        )

    def test_ingest_stores_mv_exactly(self):
        self.proc.ingest(make_d(mask=0x1, samples=(424.625,) + (0.0,) * 15))
        self.assertEqual(self.proc.get(0).sample_mv, 424.625)
        self.assertEqual(self.proc.get(0).filtered_mv, 424.625)

    def test_ingest_invalid_marked(self):
        df = make_d(mask=0x1)
        self.proc.ingest(df)
        self.assertEqual(self.proc.get(0).sample_mv, 100.0)
        self.assertEqual(
            self.proc.get(1).last_status, ChannelStatus.INVALID
        )

    def test_valid_mask_zero_excluded_from_filter(self):
        """valid_mask=0 的通道不进入滤波/基线链。"""
        self.proc.ingest(make_d(mask=0x0))
        for c in (0, 1, 2, 3):
            st = self.proc.get(c)
            self.assertIsNone(st.filtered_mv)
            self.assertEqual(st.last_status, ChannelStatus.INVALID)

    def test_saturation_mv_range(self):
        """饱和 = mV 量程边界（不再是 ADC 码值边界）。"""
        df = make_d(mask=0x3, samples=(MV_MAX, MV_MIN) + (0.0,) * 14)
        self.proc.ingest(df)
        self.assertEqual(
            self.proc.get(0).last_status, ChannelStatus.SATURATED
        )
        self.assertEqual(
            self.proc.get(1).last_status, ChannelStatus.SATURATED
        )
        # 量程内正常：前 median_window 个有效样本为滤波预热期
        # （WARMING_UP），预热完成后才进入正常状态
        self.proc.ingest(make_d(scan=2, mask=0x1, samples=(100.0,) + (0.0,) * 15))
        self.assertEqual(self.proc.get(0).last_status, ChannelStatus.WARMING_UP)
        self.proc.ingest(make_d(scan=3, mask=0x1, samples=(100.0,) + (0.0,) * 15))
        self.proc.ingest(make_d(scan=4, mask=0x1, samples=(100.0,) + (0.0,) * 15))
        self.assertEqual(self.proc.get(0).last_status, ChannelStatus.NOT_CALIBRATED)
        # 饱和 rail 值从未进入滤波链（否则 filtered 会被拉向 MV_MAX）
        self.assertAlmostEqual(self.proc.get(0).filtered_mv, 100.0, places=1)

    def test_ingest_full_16ch(self):
        proc16 = ChannelProcessor(
            adc_profile=profile_by_name("ads1115_4v096_div2",
                                        build_default_profiles()),
            enabled_channels=list(range(16)),
        )
        df = make_d(mask=0xFFFF, samples=tuple(float(c) for c in range(16)))
        proc16.ingest(df)
        for c in range(16):
            self.assertIsNotNone(proc16.get(c).filtered_mv)
            self.assertEqual(proc16.get(c).sample_mv, float(c))

    def test_calibration_atomic_success(self):
        for i in range(5):
            self.proc.ingest(make_d(scan=i, mask=0xF))
        snap = self.proc.snapshot_for_calibration()
        self.assertEqual(set(snap.keys()), {0, 1, 2, 3})
        ok = self.proc.apply_calibration(snap)
        self.assertTrue(ok)
        for c in (0, 1, 2, 3):
            self.assertIsNotNone(self.proc.get(c).baseline_mv)

    def test_calibration_baseline_is_mv(self):
        """基线来自滤波值（mV），424.625 级别的精度保留。"""
        samples = (424.625, 200.0, 300.0, 400.0) + (0.0,) * 12
        for i in range(5):
            self.proc.ingest(make_d(scan=i, mask=0xF, samples=samples))
        snap = self.proc.snapshot_for_calibration()
        ok = self.proc.apply_calibration(snap)
        self.assertTrue(ok)
        self.assertAlmostEqual(self.proc.get(0).baseline_mv, 424.625, places=3)

    def test_calibration_failure_preserves_old(self):
        self.proc.get(0).baseline_mv = 999.0
        snap = {0: 100.0, 1: None, 2: 200.0, 3: 300.0}
        ok = self.proc.apply_calibration(snap)
        self.assertFalse(ok)
        self.assertAlmostEqual(self.proc.get(0).baseline_mv, 999.0)

    def test_delta_mv_chain(self):
        """滤波(mV) - 基线(mV) = Δ(mV)。"""
        for _ in range(3):
            self.proc.ingest(make_d(mask=0x1, samples=(1000.0,) + (0.0,) * 15))
        self.proc.apply_calibration({0: 1000.0, 1: 200.0,
                                     2: 300.0, 3: 400.0})
        # EMA 收敛需要多帧；20 帧后 filtered≈511，Δ≈-489
        for i in range(20):
            self.proc.ingest(make_d(scan=9 + i, mask=0x1, samples=(500.0,) + (0.0,) * 15))
        d = self.proc.delta_signed(0)
        self.assertIsNotNone(d)
        self.assertGreater(d, -550.0)
        self.assertLess(d, -450.0)

    def test_invert_sign(self):
        for _ in range(3):
            self.proc.ingest(
                make_d(mask=0x1, samples=(1000.0,) + (0.0,) * 15)
            )
        self.proc.apply_calibration({0: 1000.0, 1: 200.0,
                                     2: 300.0, 3: 400.0})
        for _ in range(3):
            self.proc.ingest(
                make_d(mask=0x1, samples=(500.0,) + (0.0,) * 15)
            )
        d_normal = self.proc.delta_signed(0)
        self.proc.set_invert_sign(True)
        d_inv = self.proc.delta_signed(0)
        self.assertIsNotNone(d_normal)
        self.assertIsNotNone(d_inv)
        self.assertAlmostEqual(d_inv, -d_normal)

    def test_set_enabled_channels(self):
        self.proc.set_enabled_channels([0, 5, 10])
        self.assertEqual(self.proc.enabled_channels, [0, 5, 10])

    def test_reset_filter_keeps_baseline(self):
        for _ in range(3):
            self.proc.ingest(make_d())
        self.proc.apply_calibration({0: 100.0, 1: 200.0,
                                     2: 300.0, 3: 400.0})
        self.proc.reset_filter_only()
        for c in (0, 1, 2, 3):
            self.assertIsNone(self.proc.get(c).filtered_mv)
            self.assertIsNotNone(self.proc.get(c).baseline_mv)

    def test_reset_all_clears_baseline(self):
        self.proc.apply_calibration({0: 100.0, 1: 200.0,
                                     2: 300.0, 3: 400.0})
        self.proc.reset_all()
        for c in (0, 1, 2, 3):
            self.assertIsNone(self.proc.get(c).baseline_mv)
            self.assertIsNone(self.proc.get(c).filtered_mv)

    def test_per_channel_independent(self):
        self.proc.ingest(make_d())
        self.proc.ingest(
            make_d(scan=2, mask=0x1, samples=(500.0,) + (0.0,) * 15)
        )
        self.assertEqual(self.proc.get(0).sample_mv, 500.0)
        self.assertEqual(
            self.proc.get(1).last_status, ChannelStatus.INVALID
        )

    def test_last_wire_mv(self):
        """wire 值 = D 帧原始 mV（未滤波）。"""
        self.proc.ingest(make_d(mask=0x1, samples=(424.625,) + (0.0,) * 15))
        self.assertEqual(self.proc.last_wire_mv(0), 424.625)
        # 兼容别名
        self.assertEqual(self.proc.last_wire_raw(0), 424.625)


class TestCalibrationManager(unittest.TestCase):
    def setUp(self):
        self.proc = ChannelProcessor(
            adc_profile=profile_by_name("ads1115_4v096_div2",
                                        build_default_profiles()),
            enabled_channels=[0, 1, 2, 3],
        )
        self.calib = CalibrationManager(self.proc)

    def test_progress_signals(self):
        progress = []
        self.calib.progress.connect(
            lambda c, t, f_, tf_: progress.append((c, t, f_, tf_))
        )
        self.calib.start(target_samples=2)
        for i in range(5):
            df = make_d(scan=i + 1, mask=0xF)
            self.proc.ingest(df)
            # P0-1 整改：calibration 由 on_frame 驱动
            self.calib.on_frame(df)
        self.assertGreaterEqual(len(progress), 1)
        if self.calib.is_running():
            self.calib.cancel()

    def test_cancelled_preserves_old_baseline(self):
        self.proc.apply_calibration({0: 100.0, 1: 200.0,
                                     2: 300.0, 3: 400.0})
        cancelled = []
        self.calib.cancelled.connect(lambda: cancelled.append(True))
        self.calib.start(target_samples=3)
        for i in range(2):
            df = make_d(scan=i + 1, mask=0xF)
            self.proc.ingest(df)
            self.calib.on_frame(df)
            QTimer_helper_process()
        self.calib.cancel()
        QTimer_helper_process()
        self.assertEqual(len(cancelled), 1)
        self.assertAlmostEqual(self.proc.get(0).baseline_mv, 100.0)


def QTimer_helper_process():
    """让 QTimer.timeout 跑一次（用毫秒级等待）。"""
    from PyQt5.QtCore import QCoreApplication
    QCoreApplication.processEvents()
    time.sleep(0.01)
    QCoreApplication.processEvents()


class TestDemoSource(unittest.TestCase):
    def test_emits_d_frame_mv(self):
        proc = ChannelProcessor(
            adc_profile=profile_by_name("ads1115_4v096_div2",
                                        build_default_profiles()),
            enabled_channels=[0, 1, 4, 5],
        )
        demo = DemoSource(processor=proc, enabled_channels=[0, 1, 4, 5])
        demo._scan_number = 0
        frames = []
        demo.frame_ready.connect(lambda d: frames.append(d))
        demo._emit_frame()
        self.assertEqual(len(frames), 1)
        df = frames[0]
        self.assertEqual(df.scan_number, 1)
        self.assertEqual(df.unit, "mV")
        # 有效位应包含 0,1,4,5
        self.assertEqual(df.valid_mask & 0x33, 0x33)
        # 其余通道为 0
        self.assertEqual(df.valid_mask & 0xCC, 0)
        self.assertEqual(len(df.samples_mv), 16)

    def test_demo_values_are_legal_mv(self):
        """DemoSource 输出与串口路径同语义：量程内、0.125 整数倍。"""
        from models.frame import is_valid_mv_value
        proc = ChannelProcessor(
            adc_profile=profile_by_name("ads1115_4v096_div2",
                                        build_default_profiles()),
            enabled_channels=list(range(16)),
        )
        demo = DemoSource(processor=proc, enabled_channels=list(range(16)))
        frames = []
        demo.frame_ready.connect(lambda d: frames.append(d))
        for _ in range(3):
            demo._emit_frame()
        for df in frames:
            for ch in range(16):
                if df.is_valid(ch):
                    self.assertTrue(
                        is_valid_mv_value(df.samples_mv[ch]),
                        msg=f"ch{ch}={df.samples_mv[ch]}",
                    )

    def test_demo_magnitudes_are_mv(self):
        """演示数据在 mV 量级（静态工作点 ~1500 mV，不是 ~12000 count）。"""
        proc = ChannelProcessor(
            adc_profile=profile_by_name("ads1115_4v096_div2",
                                        build_default_profiles()),
            enabled_channels=[0],
        )
        demo = DemoSource(processor=proc, enabled_channels=[0])
        frames = []
        demo.frame_ready.connect(lambda d: frames.append(d))
        demo._emit_frame()
        v = frames[0].samples_mv[0]
        self.assertGreater(v, 500.0)
        self.assertLess(v, 3000.0)


if __name__ == "__main__":
    unittest.main()
