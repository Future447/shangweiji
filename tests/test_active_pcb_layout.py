"""
有源线圈 PCB 布局 + 协议/分压验收测试。

覆盖：
1. 有源板 4×4 布局坐标表（含四角 CH09/CH12/CH04/CH01）；
2. 旧面包板布局保持不变（向后兼容）；
3. 用一帧 16 个不同电压逐一验证热力图坐标与通道标签；
4. 单比特 valid_mask 时"有效性 ↔ 物理位置"始终对应；掩码=1 的 0.000 仍有效；
5. 掩码=0 的通道不参与滤波/基线/派生量/有效值显示；
6. 布局与分压设置互相独立，分压补偿只执行一次；
7. 切换布局不改变 CSV 列顺序与数据列语义；
8. 旧配置（无 layout_id）按映射反推布局；旧实验记录按记录时布局回放；
9. 换布局后旧基线不自动套用（基线按布局隔离）。
"""

import csv
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from PyQt5.QtCore import QCoreApplication
from PyQt5.QtWidgets import QApplication, QLabel

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.channel_layout import (  # noqa: E402
    ACTIVE_PCB_CELL_DATA_CHANNEL,
    ACTIVE_PCB_CHANNEL_MAP,
    ACTIVE_PCB_LABEL_POSITIONS,
    BINDING_DESIGN,
    BINDING_LEGACY_V341,
    HARDWARE_CHANNEL_MAP,
    LAYOUT_ACTIVE_PCB,
    LAYOUT_BREADBOARD,
    cell_data_channel,
    data_label_map,
    data_position_map,
    layout_id_for_map,
    layout_map,
)
from core.config_manager import SCOPE_SERIAL, ConfigManager  # noqa: E402
from models.adc_profile import build_default_profiles  # noqa: E402
from models.frame import parse_d_frame  # noqa: E402


def pump(seconds: float = 0.05) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.005)


def d_line(scan: int, samples, valid_mask: int = 0xFFFF) -> bytes:
    parts = ["D", str(scan), "0", str(valid_mask), "0", "mV"]
    parts.extend(f"{float(v):.3f}" for v in samples)
    return (",".join(parts) + "\r\n").encode("ascii")


#: 有源线圈 PCB 的目标热力图（行 → CH 编号，1 基）
EXPECTED_PCB_GRID = [
    [9, 10, 11, 12],
    [13, 14, 15, 16],
    [8, 7, 6, 5],
    [4, 3, 2, 1],
]


def grid_from_map(mapping):
    grid = [[None] * 4 for _ in range(4)]
    for ch, (r, c) in mapping.items():
        grid[r][c] = ch + 1
    return grid


class TestLayoutPresets(unittest.TestCase):
    """布局预设表本身（不依赖 Qt）。"""

    def test_active_pcb_positions_match_spec(self):
        """标签位置网格：CH09..CH12 / CH13..CH16 / CH08..CH05 / CH04..CH01。"""
        self.assertEqual(
            grid_from_map(ACTIVE_PCB_LABEL_POSITIONS), EXPECTED_PCB_GRID
        )

    def test_four_corners(self):
        # 坐标/标签不变
        m = ACTIVE_PCB_LABEL_POSITIONS
        self.assertEqual(m[8], (0, 0))    # CH09 左上
        self.assertEqual(m[11], (0, 3))   # CH12 右上
        self.assertEqual(m[3], (3, 0))    # CH04 左下
        self.assertEqual(m[0], (3, 3))    # CH01 右下
        # 默认绑定 = PCB 设计对应：CHn 读第 n 路
        dmap = data_position_map(LAYOUT_ACTIVE_PCB, BINDING_DESIGN)
        lmap = data_label_map(LAYOUT_ACTIVE_PCB, BINDING_DESIGN)
        self.assertEqual(dmap[3], (3, 0))
        self.assertEqual(lmap[3], "CH04")
        # v3.4.1 旧表（可选绑定）：CH04←第12路 等
        self.assertEqual(ACTIVE_PCB_CELL_DATA_CHANNEL[3], 11)
        legacy_dmap = data_position_map(
            LAYOUT_ACTIVE_PCB, BINDING_LEGACY_V341
        )
        legacy_lmap = data_label_map(
            LAYOUT_ACTIVE_PCB, BINDING_LEGACY_V341
        )
        self.assertEqual(legacy_dmap[11], (3, 0))
        self.assertEqual(legacy_lmap[11], "CH04")
        self.assertEqual(legacy_lmap[4], "CH12")
        self.assertEqual(legacy_lmap[0], "CH16")

    def test_breadboard_layout_preserved(self):
        """旧面包板布局不得改动（CH01 在左下，列优先）。"""
        m = HARDWARE_CHANNEL_MAP
        self.assertEqual(m[0], (3, 0))    # CH01
        self.assertEqual(m[3], (0, 0))    # CH04
        self.assertEqual(m[12], (3, 3))   # CH13
        self.assertEqual(grid_from_map(m), [
            [4, 8, 12, 16],
            [3, 7, 11, 15],
            [2, 6, 10, 14],
            [1, 5, 9, 13],
        ])

    def test_layout_id_detection(self):
        self.assertEqual(layout_id_for_map(ACTIVE_PCB_CHANNEL_MAP),
                         LAYOUT_ACTIVE_PCB)
        self.assertEqual(layout_id_for_map(HARDWARE_CHANNEL_MAP),
                         LAYOUT_BREADBOARD)
        custom = dict(HARDWARE_CHANNEL_MAP)
        custom[0], custom[1] = custom[1], custom[0]
        self.assertEqual(layout_id_for_map(custom), "custom")

    def test_layout_map_fallback_is_breadboard(self):
        self.assertEqual(layout_map("no_such_layout"), dict(HARDWARE_CHANNEL_MAP))


class _WinBase(unittest.TestCase):
    def setUp(self) -> None:
        import ui.main_window as mw_mod

        self._tmp = tempfile.mkdtemp(prefix="pcblay_")
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

    def tearDown(self) -> None:
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
        import app_paths
        import ui.main_window as mw_mod
        mw_mod.ConfigManager = self._orig_cm  # type: ignore
        app_paths.get_default_csv_dir = self._orig_csv  # type: ignore
        pump(0.05)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def use_layout(
        self, layout_id: str, binding_id: str = ""
    ) -> None:
        self.win._config.layout_id = layout_id
        if binding_id:
            self.win._config.cell_binding = binding_id
        self.win._config.save()
        self.win._apply_channel_map_to_heatmap()
        pump(0.02)

    def feed(self, samples, valid_mask: int = 0xFFFF, scan: int = 1) -> None:
        if self.win._source_state.name == "DISCONNECTED":
            self.win._on_serial_connected("COM_TEST")
        self.win._on_serial_data(d_line(scan, samples, valid_mask))
        self.win._update_all_displays()

    def heatmap_channel_at(self, row: int, col: int):
        cell = self.win._heatmap._cells.get((row, col))
        return None if cell is None else cell.channel

    def cell_texts(self, row: int, col: int):
        cell = self.win._heatmap._cells.get((row, col))
        if cell is None:
            return []
        return [lbl.text() for lbl in cell.findChildren(QLabel)]


class TestHeatmapLayout(_WinBase):
    def test_active_pcb_positions_and_labels(self):
        """坐标与 CHxx 标签保持原样，只有每格读取的数据通道随绑定变化。"""
        for binding in (BINDING_DESIGN, BINDING_LEGACY_V341):
            self.use_layout(LAYOUT_ACTIVE_PCB, binding)
            for r in range(4):
                for c in range(4):
                    label_no = EXPECTED_PCB_GRID[r][c]      # 1 基标签号
                    slot = label_no - 1
                    expect_data = cell_data_channel(
                        LAYOUT_ACTIVE_PCB, slot, binding
                    )
                    self.assertEqual(
                        self.heatmap_channel_at(r, c), expect_data,
                        f"[{binding}] ({r},{c}) 标签 CH{label_no:02d} 应读"
                        f"数据通道 {expect_data + 1}",
                    )
                    cell = self.win._heatmap._cells[(r, c)]
                    self.assertEqual(
                        cell.title, f"CH{label_no:02d}",
                        f"({r},{c}) 单元格标签应为 CH{label_no:02d}",
                    )
                    self.assertEqual(cell.accessibleName(), cell.title)

    def test_16_distinct_voltages_positions(self):
        """一帧 16 个不同电压：值 ↔ 通道号 ↔ 坐标 三者一致。"""
        self.use_layout(LAYOUT_ACTIVE_PCB)
        samples = [100.0 + 100.0 * i for i in range(16)]  # 100..1600 mV
        self.feed(samples)
        dmap = data_position_map(LAYOUT_ACTIVE_PCB)
        lmap = data_label_map(LAYOUT_ACTIVE_PCB)
        for ch in range(16):
            pos = dmap[ch]
            self.assertEqual(self.heatmap_channel_at(*pos), ch)
            self.assertEqual(self.win._heatmap._cells[pos].title, lmap[ch])
            snap = self.win._processor.measurement(ch, True)
            # D 帧值即 ADS1115 输入端电压：sample 必须与帧内该字段一致
            self.assertAlmostEqual(snap.sample_mv, samples[ch], places=6)
            cell = self.win._heatmap._cells[pos]
            self.assertEqual(cell.channel, ch)

    def test_switch_layout_does_not_touch_profile(self):
        prof_before = self.win._config.adc_profile.name
        self.use_layout(LAYOUT_ACTIVE_PCB)
        self.assertEqual(self.win._config.adc_profile.name, prof_before)
        self.assertEqual(self.win._heatmap.channel_map(),
                         dict(ACTIVE_PCB_CHANNEL_MAP))
        self.use_layout(LAYOUT_BREADBOARD)
        self.assertEqual(self.win._config.adc_profile.name, prof_before)
        self.assertEqual(self.win._heatmap.channel_map(),
                         dict(HARDWARE_CHANNEL_MAP))

    def test_old_breadboard_layout_still_available(self):
        self.use_layout(LAYOUT_BREADBOARD)
        self.assertEqual(self.win._heatmap.channel_map(),
                         dict(HARDWARE_CHANNEL_MAP))


class TestValidMaskSemantics(_WinBase):
    def test_single_bit_mask_validity_and_position(self):
        self.use_layout(LAYOUT_ACTIVE_PCB)
        for ch in range(16):
            samples = [500.0] * 16
            samples[ch] = 123.375
            self.feed(samples, valid_mask=(1 << ch), scan=ch + 1)
            pos = data_position_map(LAYOUT_ACTIVE_PCB)[ch]
            self.assertEqual(self.heatmap_channel_at(*pos), ch)
            snap = self.win._processor.measurement(ch)
            self.assertTrue(snap.frame_valid, f"CH{ch + 1:02d} 应有效")
            self.assertAlmostEqual(snap.sample_mv, 123.375, places=6)
            for other in range(16):
                if other == ch:
                    continue
                o = self.win._processor.measurement(other)
                self.assertFalse(
                    o.frame_valid, f"掩码只有 bit{ch}，CH{other + 1:02d} 不应有效"
                )
                self.assertIsNone(o.sample_mv)
                self.assertIsNone(o.filtered_mv)
                self.assertIsNone(o.attenuation_mv)

    def test_zero_volt_with_mask_bit_is_valid_reading(self):
        """掩码=1 的 0.000 仍是有效读数（不得被当成"无数据"）。"""
        self.use_layout(LAYOUT_ACTIVE_PCB)
        samples = [0.0] * 16
        self.feed(samples, valid_mask=0xFFFF)
        snap = self.win._processor.measurement(0)
        self.assertTrue(snap.frame_valid)
        self.assertIsNotNone(snap.sample_mv)
        self.assertEqual(snap.sample_mv, 0.0)

    def test_invalid_channel_excluded_from_baseline_and_derived(self):
        """掩码=0 的通道不进入滤波/基线/衰减计算。"""
        self.use_layout(LAYOUT_ACTIVE_PCB)
        # 关掉滤波滞后（EMA=1、中值窗=1），便于精确验证派生量
        self.win._processor.set_ema_alpha(1.0)
        self.win._processor.set_median_window(1)
        for scan in range(1, 4):
            self.feed([1000.0] * 16, valid_mask=0xFFFF, scan=scan)
        for ch in range(16):
            self.win._processor.get(ch).baseline_mv = 1000.0
        # 只让 CH01（channel 0）无效，其余通道 900 mV
        for scan in range(10, 15):
            self.feed([900.0] * 16, valid_mask=0xFFFE, scan=scan)

        snap0 = self.win._processor.measurement(0)
        self.assertFalse(snap0.frame_valid)
        self.assertIsNone(snap0.sample_mv)
        self.assertIsNone(snap0.filtered_mv)
        self.assertIsNone(snap0.attenuation_mv)
        # 其它通道正常（900 mV，基线 1000 → 衰减 100）
        snap1 = self.win._processor.measurement(1)
        self.assertTrue(snap1.frame_valid)
        self.assertAlmostEqual(snap1.sample_mv, 900.0, places=6)
        self.assertAlmostEqual(snap1.attenuation_mv, 100.0, places=6)


class TestDividerAppliedOnce(_WinBase):
    def test_divider_multiplies_once(self):
        profiles = build_default_profiles()
        div2 = profiles["ads1115_4v096_div2"]
        direct = profiles["ads1115_4v096_direct"]
        self.assertEqual(div2.detector_divider, 2.0)
        self.assertEqual(direct.detector_divider, 1.0)

        self.win._processor.set_adc_profile(div2)
        self.feed([1000.0] * 16)
        s1 = self.win._processor.measurement(0, True)
        s2 = self.win._processor.measurement(0, True)
        # 采样值保持 ADS1115 输入端电压不变，只有 VOUT 估算乘一次
        self.assertAlmostEqual(s1.sample_mv, 1000.0, places=6)
        self.assertAlmostEqual(s1.detector_vout_mv, 2000.0, places=6)
        self.assertAlmostEqual(s2.detector_vout_mv, 2000.0, places=6)

        self.win._processor.set_adc_profile(direct)
        self.feed([1000.0] * 16, scan=2)
        s3 = self.win._processor.measurement(0, True)
        self.assertAlmostEqual(s3.sample_mv, 1000.0, places=6)
        self.assertAlmostEqual(s3.detector_vout_mv, 1000.0, places=6)


class TestCsvAndReplayLayout(_WinBase):
    def _record_one(self, layout_id: str) -> str:
        self.use_layout(layout_id)
        self.feed([100.0 + i for i in range(16)])
        df = parse_d_frame(
            d_line(1, [100.0 + i for i in range(16)]).decode().strip()
        )
        params = dict(self.win._config.experiment_params)
        path = self.win._recorder.start_recording(
            params, self._tmp, self.win._config.adc_profile,
            session_meta={"source": "serial", "session_id": "s-" + layout_id},
        )
        self.win._recorder.write_dframe(
            df, self.win._processor,
            channel_map=self.win._config.channel_map,
            enabled_channels=self.win._config.enabled_channels,
        )
        self.win._recorder.stop_recording()
        return path

    def test_switch_layout_keeps_csv_columns_and_channel_order(self):
        p1 = self._record_one(LAYOUT_BREADBOARD)
        p2 = self._record_one(LAYOUT_ACTIVE_PCB)
        with open(p1, encoding="utf-8-sig", newline="") as f:
            r1 = list(csv.reader(f))
        with open(p2, encoding="utf-8-sig", newline="") as f:
            r2 = list(csv.reader(f))
        self.assertEqual(r1[0], r2[0], "切换布局不得改变 CSV 列顺序")
        hdr = r1[0]
        idx = hdr.index("ch01_wire_mv")
        # 数据列始终按通道号：ch01 永远是第 1 个电压
        self.assertEqual(r1[1][idx], "100.000")
        self.assertEqual(r2[1][idx], "100.000")

    def test_old_record_replays_with_recorded_layout(self):
        path = self._record_one(LAYOUT_ACTIVE_PCB)
        # 当前配置切回旧面包板，回放应按"记录时布局"显示
        self.use_layout(LAYOUT_BREADBOARD)
        self.win._replay.load(path)
        self.win._apply_replay_recorded_map()
        self.assertEqual(self.win._heatmap.channel_map(),
                         dict(ACTIVE_PCB_CHANNEL_MAP))
        self.assertTrue(self.win._replay_using_recorded_map)

    def test_old_config_without_layout_id_infers_from_map(self):
        legacy = os.path.join(self._tmp, "legacy_layout.json")
        cfg = ConfigManager(config_path=legacy)
        data = dict(cfg.data)
        data.pop("layout_id", None)
        data["channel_map"] = {
            str(ch): [r, c] for ch, (r, c) in ACTIVE_PCB_CHANNEL_MAP.items()
        }
        import json
        with open(legacy, "w", encoding="utf-8") as f:
            json.dump(data, f)
        cfg2 = ConfigManager(config_path=legacy)
        self.assertEqual(cfg2.layout_id, LAYOUT_ACTIVE_PCB)
        self.assertEqual(cfg2.channel_map, ACTIVE_PCB_CHANNEL_MAP)


class TestBaselineLayoutIsolation(_WinBase):
    def test_baseline_not_reused_across_layouts(self):
        cfg = self.win._config
        cfg.layout_id = LAYOUT_BREADBOARD
        cfg.set_baselines(SCOPE_SERIAL, {0: 1000.0}, {"device_id": "USB-1"})
        # 旧面包板布局下可用
        self.assertAlmostEqual(
            cfg.get_baselines(SCOPE_SERIAL, "USB-1", LAYOUT_BREADBOARD)[0],
            1000.0,
        )
        # 换到有源板布局 → 不得套用
        bl = cfg.get_baselines(SCOPE_SERIAL, "USB-1", LAYOUT_ACTIVE_PCB)
        self.assertTrue(all(v is None for v in bl.values()))

        # UI 侧给出可见提示
        self.win._current_scope = SCOPE_SERIAL
        self.win._current_device_id = "USB-1"
        cfg.layout_id = LAYOUT_ACTIVE_PCB
        self.win._apply_scope_baselines()
        self.assertFalse(self.win._scope_baselines_applied)
        self.assertIn("布局", self.win._message_bar._lbl_text.text())

    def test_new_calibration_records_layout(self):
        """新标定必须把当前布局写进 meta（供以后区分）。"""
        with patch(
            "ui.main_window.QMessageBox.warning", new=MagicMock()
        ), patch(
            "ui.main_window.QMessageBox.information", new=MagicMock()
        ):
            self.use_layout(LAYOUT_ACTIVE_PCB)
            self.win._processor.set_enabled_channels([0])
            self.win._processor.set_median_window(1)
            for scan in range(1, 4):
                self.feed([1000.0] * 16, scan=scan)
            self.win._confirm_plate_removed = lambda: True
            self.win._sp_calib.setValue(3)
            self.win._on_calib_clicked()
            scan = 10
            for _ in range(300):
                pump(0.02)
                if not self.win._calib.is_running():
                    break
                self.feed([1000.0] * 16, scan=scan)
                scan += 1
            meta = self.win._config.get_baseline_meta(
                self.win._current_scope
            )
            self.assertIsNotNone(meta, "标定未产生基线")
            self.assertEqual(meta.get("layout"), LAYOUT_ACTIVE_PCB)


if __name__ == "__main__":
    unittest.main()
