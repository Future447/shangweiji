"""
演示数据源完整实验流程测试（core/demo_source.py）。

验收点（任务书 六 / 八-6）：
- 默认【无钢板】状态，可演示真实操作顺序：无钢板标定 → 放入钢板
- 放入钢板后电压下降、衰减量（基线 − 当前）增大
- 移除钢板后回到基线附近（衰减量接近 0，不强制归零）
- 样本保持 0.125 mV 量化与合法量程
- 模拟响应幅度非负约束；模拟限幅不作用于真实硬件数据
- 只发 frame_ready，不模拟"串口连接成功"
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

from core.calibration_manager import CalibrationManager, WARMUP_FRAMES
from core.channel_processor import ChannelProcessor
from core.demo_source import DemoSource
from models.frame import MV_MAX, MV_MIN


def _emit_n(demo: DemoSource, proc: ChannelProcessor, n: int,
            calib=None) -> list:
    """同步驱动 n 帧；返回收集到的 DFrame 列表。"""
    frames = []
    demo.frame_ready.connect(lambda d: frames.append(d))
    for _ in range(n):
        demo._emit_frame()
    for df in frames:
        proc.ingest(df)
        if calib is not None:
            calib.on_frame(df)
    return frames


class DemoFlowBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.proc = ChannelProcessor(enabled_channels=[0, 1, 4, 5])
        self.demo = DemoSource(
            processor=self.proc, enabled_channels=[0, 1, 4, 5]
        )

    def tearDown(self):
        self.demo.stop()
        QCoreApplication.processEvents()


class TestPlateStateMachine(DemoFlowBase):
    def test_default_is_no_plate(self):
        """默认无钢板：可以先演示真实的标定操作顺序。"""
        self.assertFalse(self.demo.plate_inserted)

    def test_no_plate_samples_near_air_baseline(self):
        """无钢板：样本在空气基线（~1500 mV）附近。"""
        frames = _emit_n(self.demo, self.proc, 10)
        for df in frames:
            for ch in (0, 1, 4, 5):
                v = df.samples_mv[ch]
                self.assertGreater(v, 1400.0)
                self.assertLess(v, 1600.0)

    def test_plate_inserted_drops_voltage(self):
        """放入钢板：各通道电压下降（中心通道最明显）。"""
        base_frames = _emit_n(self.demo, self.proc, 10)
        base_mean = (
            sum(df.samples_mv[5] for df in base_frames) / len(base_frames)
        )
        self.demo.set_plate_inserted(True)
        plate_frames = _emit_n(self.demo, self.proc, 10)
        plate_mean = (
            sum(df.samples_mv[5] for df in plate_frames) / len(plate_frames)
        )
        # CH06 位于 (1,1)，靠近钢板中心 (1.5,1.5)：下降应显著
        self.assertLess(plate_mean, base_mean - 80.0)
        # 下降方向：放钢板后每个样本都低于无钢板均值 - 噪声容限
        for df in plate_frames:
            self.assertLess(df.samples_mv[5], base_mean - 40.0)

    def test_plate_removed_returns_near_baseline(self):
        """移除钢板：回到基线附近（不强制精确相等）。"""
        self.demo.set_plate_inserted(True)
        _emit_n(self.demo, self.proc, 5)
        self.demo.set_plate_inserted(False)
        frames = _emit_n(self.demo, self.proc, 10)
        for df in frames:
            self.assertGreater(df.samples_mv[5], 1400.0)
            self.assertLess(df.samples_mv[5], 1600.0)

    def test_quantization_and_range(self):
        """0.125 mV 量化 + 合法量程（含放钢板状态）。"""
        self.demo.set_plate_inserted(True)
        frames = _emit_n(self.demo, self.proc, 20)
        for df in frames:
            for ch in (0, 1, 4, 5):
                v = df.samples_mv[ch]
                self.assertGreaterEqual(v, MV_MIN)
                self.assertLessEqual(v, MV_MAX)
                q = v / 0.125
                self.assertAlmostEqual(q, round(q), places=9)

    def test_amplitude_non_negative(self):
        """响应幅度非负约束：放入钢板只使电压下降或不变。"""
        self.demo.set_plate_amplitude(-50.0)
        self.assertEqual(self.demo._plate_amplitude, 0.0)
        self.demo.set_plate_amplitude(200.0)
        self.assertEqual(self.demo._plate_amplitude, 200.0)

    def test_no_serial_connection_simulation(self):
        """演示源只有 frame_ready，绝不模拟串口连接成功信号。"""
        self.assertTrue(hasattr(self.demo, "frame_ready"))
        for name in ("connected", "connection_established",
                     "serial_connected", "port_opened"):
            self.assertFalse(
                hasattr(self.demo, name),
                f"DemoSource 不应提供 {name} 信号",
            )


class TestFullExperimentSequence(DemoFlowBase):
    def test_calibrate_then_plate_then_remove(self):
        """完整演示：无钢板标定 → 放入钢板（衰减>0）→ 移除（≈0）。

        这是有 median/EMA 收敛过程的端到端测试（与公式单测区分）。
        """
        calib = CalibrationManager(self.proc)
        finished = []
        calib.finished.connect(lambda b: finished.append(b))
        try:
            # 1) 无钢板标定
            self.assertFalse(self.demo.plate_inserted)
            calib.start(target_samples=8)
            _emit_n(self.demo, self.proc, WARMUP_FRAMES + 8, calib=calib)
            self.assertEqual(len(finished), 1, "无钢板标定应完成")
            ok = self.proc.apply_calibration(finished[0])
            self.assertTrue(ok)
            baseline5 = self.proc.get(5).baseline_mv
            self.assertIsNotNone(baseline5)
            self.assertGreater(baseline5, 1400.0)

            # 标定后、放钢板前：衰减量应接近 0
            _emit_n(self.demo, self.proc, 25)  # EMA 收敛
            snap = self.proc.measurement(5, True)
            self.assertIsNotNone(snap.attenuation_mv)
            self.assertLess(abs(snap.attenuation_mv), 30.0)

            # 2) 放入钢板：衰减量显著为正（电压下降）
            self.demo.set_plate_inserted(True)
            _emit_n(self.demo, self.proc, 30)  # EMA 重新收敛
            snap = self.proc.measurement(5, True)
            self.assertIsNotNone(snap.attenuation_mv)
            self.assertGreater(
                snap.attenuation_mv, 80.0,
                f"放入钢板后衰减量应显著为正，实际 {snap.attenuation_mv}",
            )
            self.assertIsNotNone(snap.detector_drop_pct)
            self.assertGreater(snap.detector_drop_pct, 3.0)

            # 3) 移除钢板：衰减量回到 0 附近（如实，不强制归零）
            self.demo.set_plate_inserted(False)
            _emit_n(self.demo, self.proc, 30)
            snap = self.proc.measurement(5, True)
            self.assertLess(
                abs(snap.attenuation_mv), 40.0,
                f"移除钢板后衰减量应回到基线附近，实际 {snap.attenuation_mv}",
            )
        finally:
            if calib.is_running():
                calib.cancel()


if __name__ == "__main__":
    unittest.main()
