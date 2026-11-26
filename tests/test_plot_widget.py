"""
曲线组件回归测试（ui/plot_widget.py）。

对抗评审发现：
- add_point 接受 None（未知值）→ NaN 断点（connect="finite"），
  不崩溃、不把未知值画成看似真实的 0 mV
- 三条并行序列（t/raw/filt）在窗口淘汰后必须保持等长——
  旧实现按当前点是否为 None 决定是否 pop filt 序列，序列逐渐
  错位，最终 setData 长度不一致直接崩溃
"""

import math
import os
import sys
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication

from ui.plot_widget import PlotWidget


class TestPlotSeriesIntegrity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.plot = PlotWidget()
        self.plot.set_selected_channel(0)

    def tearDown(self):
        self.plot.deleteLater()

    def _lengths(self, ch=0):
        return (
            len(self.plot._t_offsets.get(ch, [])),
            len(self.plot._raw_series.get(ch, [])),
            len(self.plot._filt_series.get(ch, [])),
        )

    def test_add_point_none_no_crash(self):
        """None 采样/滤波值 → NaN 断点，不抛异常。"""
        self.plot.add_point(0, None, None)
        self.plot.add_point(0, 800.0, None)
        self.plot.add_point(0, None, 799.5)
        self.plot.add_point(0, 800.0, 799.5)
        lt, lr, lf = self._lengths()
        self.assertEqual((lt, lr, lf), (4, 4, 4))
        self.assertTrue(math.isnan(self.plot._raw_series[0][0]))
        self.assertTrue(math.isnan(self.plot._filt_series[0][0]))
        # 真实值不被 NaN 污染
        self.assertAlmostEqual(self.plot._raw_series[0][3], 800.0)
        self.assertAlmostEqual(self.plot._filt_series[0][3], 799.5)

    def test_eviction_keeps_arrays_parallel(self):
        """窗口淘汰三条序列同步裁剪（混合 None/数值场景）。

        回归：旧实现 filt 序列的 pop 由"当前点 filtered 是否为 None"
        决定——None 点只截 t/raw 不截 filt，三条序列逐渐错位，
        最终 setData 因长度不一致崩溃。
        """
        self.plot.set_window(5.0)  # _max_points = max(60, 100) = 100
        max_points = self.plot._max_points
        # 交替 None/数值超额填充（时间窗口 5s 内完成，只触发数量淘汰）
        for i in range(max_points + 50):
            if i % 3 == 0:
                self.plot.add_point(0, None, 800.0 + i * 0.125)
            elif i % 3 == 1:
                self.plot.add_point(0, 800.0, None)
            else:
                self.plot.add_point(0, 800.0 + i * 0.125, 799.0)
        lt, lr, lf = self._lengths()
        self.assertEqual(lt, lr, "t/raw 序列长度必须一致")
        self.assertEqual(lr, lf, "raw/filt 序列长度必须一致")
        self.assertLessEqual(lt, max_points)
        # 每次 add_point 都执行了 setData：长度不一致会在 pyqtgraph
        # 内部抛异常——能走到这里即证明序列未错位

    def test_attenuation_none_breakpoint(self):
        """衰减量序列：None → NaN 断点（基线缺失/无效帧不留假值）。"""
        self.plot.add_attenuation_point(0, 200.0)
        self.plot.add_attenuation_point(0, None)
        self.plot.add_attenuation_point(0, -50.0)
        series = self.plot._atten_series[0]
        self.assertEqual(len(series), 3)
        self.assertAlmostEqual(series[0], 200.0)
        self.assertTrue(math.isnan(series[1]))
        self.assertAlmostEqual(series[2], -50.0)


if __name__ == "__main__":
    unittest.main()
