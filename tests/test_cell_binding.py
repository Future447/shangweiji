"""
线圈标签 ↔ 串口电压序号绑定验收。

覆盖：
- 4×4 坐标与 CHxx 标签不变，只有"每格读取第几路电压"随绑定变化；
- 两张冲突表（PCB 设计对应 / v3.4.1 旧映射）都必须显式选择并存盘；
- 详情面板：线圈标签、帧号、串口电压序号、ADC 原始 mV、有效位；
- 实机回归帧 #5252：第 2 个电压 310.750 mV、第 10 个 359.000 mV，
  设计绑定下 CH02 显示前者、旧绑定下显示后者；
- 设置页通道按钮复用线圈标签，保存写入对应的原始数据索引；
- 跨布局回放同步还原坐标、标签、详情与图例，退出恢复实时布局；
- CSV 列序与数据通道顺序不受绑定影响。
"""

import csv
import json
import os
import shutil
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

from core.channel_layout import (  # noqa: E402
    ACTIVE_PCB_LABEL_POSITIONS,
    BINDING_DESIGN,
    BINDING_LEGACY_V341,
    HARDWARE_CHANNEL_MAP,
    LAYOUT_ACTIVE_PCB,
    LAYOUT_BREADBOARD,
    cell_data_channel,
    data_label_map,
    data_position_map,
    format_compact_map,
)
from core.config_manager import ConfigManager  # noqa: E402
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


EXPECTED_LABEL_GRID = [
    ["CH09", "CH10", "CH11", "CH12"],
    ["CH13", "CH14", "CH15", "CH16"],
    ["CH08", "CH07", "CH06", "CH05"],
    ["CH04", "CH03", "CH02", "CH01"],
]


class _WinBase(unittest.TestCase):
    def setUp(self) -> None:
        import ui.main_window as mw_mod

        self._tmp = tempfile.mkdtemp(prefix="binding_")
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

    def use_binding(self, binding_id: str) -> None:
        self.win._config.layout_id = LAYOUT_ACTIVE_PCB
        self.win._config.cell_binding = binding_id
        self.win._apply_channel_map_to_heatmap()
        pump(0.02)

    def feed(self, samples, valid_mask: int = 0xFFFF, scan: int = 1) -> None:
        if self.win._source_state.name == "DISCONNECTED":
            self.win._on_serial_connected("COM_TEST")
        self.win._on_serial_data(d_line(scan, samples, valid_mask))
        self.win._update_all_displays()
        pump(0.01)

    def cell_at_label(self, label: str):
        for (r, c), cell in self.win._heatmap._cells.items():
            if cell.title == label:
                return cell
        raise AssertionError(f"找不到标签 {label}")


class TestLayoutAndBinding(_WinBase):
    def test_positions_and_labels_unchanged(self):
        for binding in (BINDING_DESIGN, BINDING_LEGACY_V341):
            self.use_binding(binding)
            cells = self.win._heatmap._cells
            for r in range(4):
                for c in range(4):
                    self.assertEqual(
                        cells[(r, c)].title, EXPECTED_LABEL_GRID[r][c]
                    )

    def test_binding_tables(self):
        """设计绑定恒等；旧表为 [9,10,…,16,8,…,1]（1 基）。"""
        design = [BINDING_DESIGN and cell_data_channel(
            LAYOUT_ACTIVE_PCB, slot, BINDING_DESIGN
        ) + 1 for slot in range(16)]
        self.assertEqual(design, list(range(1, 17)))
        legacy = [cell_data_channel(
            LAYOUT_ACTIVE_PCB, slot, BINDING_LEGACY_V341
        ) + 1 for slot in range(16)]
        self.assertEqual(
            legacy,
            [9, 10, 11, 12, 13, 14, 15, 16, 8, 7, 6, 5, 4, 3, 2, 1],
        )

    def test_16_distinct_voltages_bound_once(self):
        samples = [100.0 + 100.0 * i for i in range(16)]
        for binding in (BINDING_DESIGN, BINDING_LEGACY_V341):
            self.use_binding(binding)
            self.feed(samples)
            labels = data_label_map(LAYOUT_ACTIVE_PCB, binding)
            seen = set()
            for (r, c), cell in self.win._heatmap._cells.items():
                data = cell.channel
                self.assertNotIn(data, seen, "每个数据通道只应绑定一个格子")
                seen.add(data)
                self.assertEqual(cell.title, labels[data])
                self.assertAlmostEqual(
                    cell.sample_mv, samples[data], places=6,
                    msg=f"[{binding}] {cell.title} 读第 {data + 1} 路",
                )
            self.assertEqual(len(seen), 16)

    def test_single_bit_mask_spot_checks(self):
        # (标签, 设计绑定通道, 旧绑定通道)
        checks = [
            ("CH04", 3, 11),
            ("CH12", 11, 4),
            ("CH16", 15, 0),
        ]
        for label, design_ch, legacy_ch in checks:
            for binding, data_ch in (
                (BINDING_DESIGN, design_ch),
                (BINDING_LEGACY_V341, legacy_ch),
            ):
                self.use_binding(binding)
                samples = [500.0] * 16
                samples[data_ch] = 777.125
                self.feed(samples, valid_mask=1 << data_ch, scan=7)
                cell = self.cell_at_label(label)
                self.assertEqual(cell.channel, data_ch)
                self.assertAlmostEqual(cell.sample_mv, 777.125, places=6)
                self.assertTrue(cell.valid_in_frame)
                for other in range(16):
                    if other != data_ch:
                        self.assertFalse(
                            self.win._processor.measurement(other).frame_valid
                        )

    def test_single_bit_mask_all_channels(self):
        """逐路单比特掩码：16 个通道各自有效，格子/曲线标签一致。"""
        for binding in (BINDING_DESIGN, BINDING_LEGACY_V341):
            self.use_binding(binding)
            labels = data_label_map(LAYOUT_ACTIVE_PCB, binding)
            for data_ch in range(16):
                samples = [100.0] * 16
                samples[data_ch] = 123.375
                self.feed(samples, valid_mask=1 << data_ch, scan=data_ch)
                cell = self.win._heatmap._cells[
                    data_position_map(LAYOUT_ACTIVE_PCB, binding)[data_ch]
                ]
                self.assertEqual(cell.title, labels[data_ch])
                self.assertAlmostEqual(
                    cell.sample_mv, 123.375, places=6
                )
                self.assertTrue(cell.valid_in_frame)
                self.assertFalse(
                    self.win._processor.measurement(
                        (data_ch + 1) % 16
                    ).frame_valid
                )
                # 曲线图例使用同一标签
                self.assertEqual(
                    self.win._plot._label(data_ch), labels[data_ch]
                )

    def test_binding_persists_after_restart(self):
        self.win._config.layout_id = LAYOUT_ACTIVE_PCB
        self.win._config.cell_binding = BINDING_LEGACY_V341
        ok, err = self.win._config.save()
        self.assertTrue(ok, err)
        cfg2 = ConfigManager(config_path=self._cfg_path)
        self.assertEqual(cfg2.layout_id, LAYOUT_ACTIVE_PCB)
        self.assertEqual(cfg2.cell_binding, BINDING_LEGACY_V341)
        self.assertEqual(
            cfg2.channel_map,
            data_position_map(LAYOUT_ACTIVE_PCB, BINDING_LEGACY_V341),
        )

    def test_legacy_config_without_binding_field(self):
        """旧配置只有映射、没有 cell_binding：识别为旧表，不改写映射。"""
        cfg = ConfigManager(config_path=self._cfg_path)
        data = dict(cfg.data)
        data["channel_map"] = {
            str(k): [r, c] for k, (r, c) in
            data_position_map(LAYOUT_ACTIVE_PCB, BINDING_LEGACY_V341).items()
        }
        data["layout_id"] = LAYOUT_ACTIVE_PCB
        data.pop("cell_binding", None)
        with open(self._cfg_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        cfg2 = ConfigManager(config_path=self._cfg_path)
        self.assertEqual(cfg2.cell_binding, BINDING_LEGACY_V341)
        self.assertEqual(
            cfg2.channel_map,
            data_position_map(LAYOUT_ACTIVE_PCB, BINDING_LEGACY_V341),
        )
        self.assertTrue(cfg2.binding_migration_notice)


class TestDetailReconciliation(_WinBase):
    def test_detail_fields(self):
        self.use_binding(BINDING_DESIGN)
        self.feed([100.0 * (i + 1) for i in range(16)], scan=5252)
        cell = self.cell_at_label("CH04")
        self.win._select_channel(cell.channel)
        panel = self.win._channel_panel
        title = panel._lbl_title.text()
        self.assertIn("CH04", title)
        self.assertIn("第 4 路电压", title)
        self.assertIn(f"第 {cell.channel + 1} 个", panel._m_index._val.text())
        self.assertEqual(panel._m_scan._val.text(), "5252")
        self.assertEqual(
            panel._m_valid._val.text(), f"bit {cell.channel} = 1"
        )
        self.assertAlmostEqual(
            float(panel._m_raw._val.text()), 400.0, places=3
        )
        # 原始 / 滤波 / VOUT 三个量分别展示
        self.assertEqual(panel._m_filtered._lbl.text(), "滤波后 mV")
        self.assertEqual(panel._m_vout._lbl.text(), "VOUT(分压后) mV")
        self.assertIn("绑定", panel._lbl_binding.text())

    def test_detail_marks_invalid_bit(self):
        self.use_binding(BINDING_DESIGN)
        samples = [100.0] * 16
        self.feed(samples, valid_mask=0xFFFB, scan=9)  # bit2 = 0 → CH03
        cell = self.cell_at_label("CH03")
        self.win._select_channel(cell.channel)
        panel = self.win._channel_panel
        self.assertEqual(
            panel._m_valid._val.text(), f"bit {cell.channel} = 0"
        )
        self.assertEqual(panel._m_raw._val.text(), "—")

    def test_regression_frame_5252(self):
        """实机第 5252 帧：第 2 个电压 310.750，第 10 个 359.000。"""
        samples = [0.0] * 16
        samples[1] = 310.750
        samples[9] = 359.000
        for binding, expect in (
            (BINDING_DESIGN, 310.750),
            (BINDING_LEGACY_V341, 359.000),
        ):
            self.use_binding(binding)
            self.feed(samples, scan=5252)
            cell = self.cell_at_label("CH02")
            self.assertEqual(cell.channel, 1 if binding == BINDING_DESIGN else 9)
            self.assertAlmostEqual(
                cell.sample_mv, expect, places=3,
                msg=f"[{binding}] CH02 应显示 {expect}",
            )
            self.win._select_channel(cell.channel)
            panel = self.win._channel_panel
            self.assertIn("CH02", panel._lbl_title.text())
            self.assertEqual(panel._m_scan._val.text(), "5252")
            self.assertIn(
                f"第 {cell.channel + 1} 个", panel._m_index._val.text()
            )


class TestSettingsChips(_WinBase):
    def _make_dialog(self, binding: str):
        from ui.settings_dialog import SettingsDialog

        cfg = self.win._config
        cfg.layout_id = LAYOUT_ACTIVE_PCB
        cfg.cell_binding = binding
        return SettingsDialog(
            current_profile=cfg.adc_profile,
            enabled_channels=[0, 1, 2, 3],
            ema_alpha=cfg.ema_alpha,
            median_window=cfg.median_window,
            heatmap_min=cfg.heatmap_min,
            heatmap_max=cfg.heatmap_max,
            heatmap_auto=cfg.heatmap_auto,
            curve_window_s=cfg.curve_window_s,
            invert_sign=cfg.invert_sign,
            experiment_params=cfg.experiment_params,
            channel_map=cfg.channel_map,
            layout_id=cfg.layout_id,
            cell_binding=binding,
            binding_confirmed=True,
        )

    def test_chip_labels_follow_binding(self):
        for binding in (BINDING_DESIGN, BINDING_LEGACY_V341):
            dlg = self._make_dialog(binding)
            try:
                chips = {c.text(): c.channel for c in dlg._chips}
                labels = data_label_map(LAYOUT_ACTIVE_PCB, binding)
                for data, label in labels.items():
                    self.assertEqual(chips[label], data)
                # 网格位置与热力图一致（标签 CH04 在左下）
                self.assertEqual(chips["CH04"], cell_data_channel(
                    LAYOUT_ACTIVE_PCB, 3, binding
                ))
            finally:
                dlg.deleteLater()

    def test_checking_ch04_saves_raw_index(self):
        for binding, expect_index in (
            (BINDING_DESIGN, 3),
            (BINDING_LEGACY_V341, 11),
        ):
            dlg = self._make_dialog(binding)
            try:
                for chip in dlg._chips:
                    chip.setChecked(chip.text() == "CH04")
                dlg._on_accept()
                self.assertEqual(
                    dlg.result_values["enabled_channels"], [expect_index],
                    f"[{binding}] 勾选 CH04 应写入原始索引 {expect_index}",
                )
                self.assertEqual(
                    dlg.result_values["cell_binding"], binding
                )
            finally:
                dlg.deleteLater()


class TestReplayCrossLayout(_WinBase):
    def _record(self, samples, binding: str) -> str:
        cfg = self.win._config
        cfg.layout_id = LAYOUT_ACTIVE_PCB
        cfg.cell_binding = binding
        df = parse_d_frame(d_line(1, samples).decode().strip())
        self.win._processor.ingest(df)
        path = self.win._recorder.start_recording(
            dict(cfg.experiment_params), self._tmp, cfg.adc_profile,
            session_meta={"source": "serial", "session_id": "s1"},
        )
        for scan in (1, 2):
            d = parse_d_frame(d_line(scan, samples).decode().strip())
            self.win._recorder.write_dframe(
                d, self.win._processor,
                channel_map=cfg.channel_map,
                enabled_channels=cfg.enabled_channels,
            )
        self.win._recorder.stop_recording()
        return path

    def test_replay_restores_recorded_labels(self):
        samples = [float(i) for i in range(16)]
        path = self._record(samples, BINDING_LEGACY_V341)
        # 实时切回设计绑定/面包板
        self.win._config.layout_id = LAYOUT_BREADBOARD
        self.win._apply_channel_map_to_heatmap()

        self.win._replay.load(path)
        self.win._apply_replay_recorded_map()
        cell = self.win._heatmap._cells[(3, 0)]
        self.assertEqual(cell.title, "CH04", "记录时左下格应为 CH04")
        self.assertEqual(cell.channel, 11, "该格应读第 12 路电压")
        self.assertTrue(self.win._replay_using_recorded_map)
        # 详情/图例同步该标签
        self.win._select_channel(cell.channel)
        self.assertIn("CH04", self.win._channel_panel._lbl_title.text())
        self.assertEqual(self.win._plot._label(11), "CH04")

        # 退出回放 → 恢复实时布局
        self.win._set_app_mode("live")
        live = self.win._heatmap._cells[(3, 0)]
        self.assertEqual(
            live.title, "CH01",
            "退出回放后应恢复实时（面包板）布局标签",
        )
        self.assertFalse(self.win._replay_using_recorded_map)


class TestCsvOrder(_WinBase):
    def test_csv_columns_independent_of_binding(self):
        samples = [100.0 + i for i in range(16)]
        paths = []
        for binding in (BINDING_DESIGN, BINDING_LEGACY_V341):
            cfg = self.win._config
            cfg.layout_id = LAYOUT_ACTIVE_PCB
            cfg.cell_binding = binding
            df = parse_d_frame(d_line(1, samples).decode().strip())
            self.win._processor.ingest(df)
            path = self.win._recorder.start_recording(
                dict(cfg.experiment_params), self._tmp, cfg.adc_profile,
                session_meta={"source": "serial", "session_id": binding},
            )
            self.win._recorder.write_dframe(
                df, self.win._processor,
                channel_map=cfg.channel_map,
                enabled_channels=cfg.enabled_channels,
            )
            self.win._recorder.stop_recording()
            paths.append(path)

        rows = []
        for p in paths:
            with open(p, encoding="utf-8-sig", newline="") as f:
                rows.append(list(csv.reader(f)))
        self.assertEqual(rows[0][0], rows[1][0], "绑定不得改变 CSV 列序")
        idx = rows[0][0].index("ch01_wire_mv")
        self.assertEqual(rows[0][1][idx], "100.000")
        self.assertEqual(rows[1][1][idx], "100.000")
        idx2 = rows[0][0].index("ch16_wire_mv")
        self.assertEqual(rows[0][1][idx2], "115.000")
        self.assertEqual(rows[1][1][idx2], "115.000")

    def test_recorded_map_identifies_binding(self):
        self.assertEqual(
            format_compact_map(
                data_position_map(LAYOUT_ACTIVE_PCB, BINDING_LEGACY_V341)
            ) != format_compact_map(
                data_position_map(LAYOUT_ACTIVE_PCB, BINDING_DESIGN)
            ),
            True,
        )
        # 面包板映射仍可识别
        self.assertEqual(
            format_compact_map(HARDWARE_CHANNEL_MAP),
            format_compact_map(data_position_map(LAYOUT_BREADBOARD)),
        )


if __name__ == "__main__":
    unittest.main()
