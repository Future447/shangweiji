"""
换板 / 标定 / 记录回放修复的验收测试。

覆盖：
1. 基线按（来源 + 设备 + 布局 + 测量条件）分库存放：两块板各自标定后
   往返切换、重启后都能恢复；旧配置单槽基线自动迁移保留；
2. 换布局/换 Profile 会清掉上一实验的滤波与显示状态；
3. 录制期间锁定设置（布局/Profile/实验条件），提示停止记录后再改；
4. 混合布局 CSV 能被检测并提示；Profile 不一致/缺失也提示；
5. 回放按**记录时 Profile** 计算 VOUT（旧 1:2 记录的 1000 mV → 2000 mV），
   缺失/不可识别时 VOUT 未知；退出回放恢复实时分压；
6. 四角 CH09/CH12/CH04/CH01 与单比特有效掩码仍正确。
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
from PyQt5.QtWidgets import QApplication, QDialog

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.channel_layout import (  # noqa: E402
    ACTIVE_PCB_CHANNEL_MAP,
    HARDWARE_CHANNEL_MAP,
    LAYOUT_ACTIVE_PCB,
    LAYOUT_BREADBOARD,
)
from core.config_manager import SCOPE_SERIAL, ConfigManager  # noqa: E402
from core.replay_source import ReplaySource  # noqa: E402
from models.adc_profile import build_default_profiles, profile_by_name  # noqa: E402
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


class TestBaselineStore(unittest.TestCase):
    """配置层：基线分库、迁移、条件匹配。"""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="blstore_")
        self.path = os.path.join(self._tmp, "config.json")

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_two_layouts_keep_separate_baselines(self):
        cfg = ConfigManager(config_path=self.path)
        cfg.set_baselines(
            SCOPE_SERIAL, {0: 1000.0},
            {"device_id": "DEV-1", "layout": LAYOUT_BREADBOARD,
             "adc_profile": "ads1115_4v096_div2", "frequency": 1000.0},
        )
        cfg.layout_id = LAYOUT_ACTIVE_PCB
        cfg.set_baselines(
            SCOPE_SERIAL, {0: 1500.0},
            {"device_id": "DEV-1", "layout": LAYOUT_ACTIVE_PCB,
             "adc_profile": "ads1115_4v096_direct", "frequency": 1000.0},
        )
        ok, err = cfg.save()
        self.assertTrue(ok, err)

        # 切回旧面包板：拿到旧板基线
        bl_old = cfg.get_baselines(
            SCOPE_SERIAL, "DEV-1", LAYOUT_BREADBOARD,
            {"adc_profile": "ads1115_4v096_div2", "frequency": 1000.0},
        )
        self.assertAlmostEqual(bl_old[0], 1000.0)
        # 有源板：拿到新板基线
        bl_new = cfg.get_baselines(
            SCOPE_SERIAL, "DEV-1", LAYOUT_ACTIVE_PCB,
            {"adc_profile": "ads1115_4v096_direct", "frequency": 1000.0},
        )
        self.assertAlmostEqual(bl_new[0], 1500.0)

        # 重启（重新加载同一文件）后依然各自恢复
        cfg2 = ConfigManager(config_path=self.path)
        self.assertAlmostEqual(
            cfg2.get_baselines(
                SCOPE_SERIAL, "DEV-1", LAYOUT_BREADBOARD,
                {"adc_profile": "ads1115_4v096_div2", "frequency": 1000.0},
            )[0],
            1000.0,
        )
        self.assertAlmostEqual(
            cfg2.get_baselines(
                SCOPE_SERIAL, "DEV-1", LAYOUT_ACTIVE_PCB,
                {"adc_profile": "ads1115_4v096_direct", "frequency": 1000.0},
            )[0],
            1500.0,
        )

    def test_conditions_change_blocks_reuse(self):
        cfg = ConfigManager(config_path=self.path)
        cfg.set_baselines(
            SCOPE_SERIAL, {0: 1000.0},
            {"device_id": "DEV-1", "layout": LAYOUT_BREADBOARD,
             "adc_profile": "ads1115_4v096_div2", "frequency": 1000.0},
        )
        # 频率改了 → 不再套用
        bl = cfg.get_baselines(
            SCOPE_SERIAL, "DEV-1", LAYOUT_BREADBOARD,
            {"adc_profile": "ads1115_4v096_div2", "frequency": 2000.0},
        )
        self.assertTrue(all(v is None for v in bl.values()))
        # Profile 改了 → 不再套用
        bl2 = cfg.get_baselines(
            SCOPE_SERIAL, "DEV-1", LAYOUT_BREADBOARD,
            {"adc_profile": "ads1115_4v096_direct", "frequency": 1000.0},
        )
        self.assertTrue(all(v is None for v in bl2.values()))

    def test_old_single_slot_baseline_migrated_and_kept(self):
        """旧配置只有 baseline_sets：加载后迁移进历史库且仍可用。"""
        cfg = ConfigManager(config_path=self.path)
        data = dict(cfg.data)
        data["baseline_sets"] = {
            s: {"meta": None, "baselines": {str(c): None for c in range(16)}}
            for s in ("serial", "tcp", "demo")
        }
        data["baseline_sets"]["serial"] = {
            "meta": {
                "source": "serial", "unit": "mV", "device_id": "OLD-1",
                "channels": [0], "calibrated_at": "2025-01-01T00:00:00",
                "baseline_id": "bl-old", "formula": "x",
            },
            "baselines": {str(c): (1000.0 if c == 0 else None)
                          for c in range(16)},
        }
        data.pop("baseline_entries", None)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

        cfg2 = ConfigManager(config_path=self.path)
        entries = cfg2.list_baseline_entries()
        self.assertTrue(
            any(e["baseline_id"] == "bl-old" for e in entries),
            f"旧基线未迁移: {entries}",
        )
        # 旧基线（无布局/条件字段）在默认面包板布局下仍可用
        bl = cfg2.get_baselines(
            SCOPE_SERIAL, "OLD-1", LAYOUT_BREADBOARD,
            {"adc_profile": "ads1115_4v096_div2", "frequency": 1000.0},
        )
        self.assertAlmostEqual(bl[0], 1000.0)

        # 新板标定不覆盖旧条目
        cfg2.layout_id = LAYOUT_ACTIVE_PCB
        cfg2.set_baselines(
            SCOPE_SERIAL, {0: 1500.0},
            {"device_id": "OLD-1", "layout": LAYOUT_ACTIVE_PCB},
        )
        entries2 = cfg2.list_baseline_entries()
        self.assertTrue(any(e["baseline_id"] == "bl-old" for e in entries2))
        self.assertAlmostEqual(
            cfg2.get_baselines(
                SCOPE_SERIAL, "OLD-1", LAYOUT_BREADBOARD,
                {"adc_profile": "ads1115_4v096_div2", "frequency": 1000.0},
            )[0],
            1000.0,
        )


class _WinBase(unittest.TestCase):
    def setUp(self) -> None:
        import ui.main_window as mw_mod

        self._tmp = tempfile.mkdtemp(prefix="blwin_")
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

    def feed(self, samples, valid_mask: int = 0xFFFF, scan: int = 1) -> None:
        if self.win._source_state.name == "DISCONNECTED":
            self.win._on_serial_connected("COM_TEST")
        self.win._on_serial_data(d_line(scan, samples, valid_mask))
        self.win._update_all_displays()

    def record_csv(self, samples, profile_name: str, layout_id: str) -> str:
        profiles = build_default_profiles()
        prof = profile_by_name(profile_name, profiles)
        self.win._config.layout_id = layout_id
        self.win._processor.set_adc_profile(prof)
        df = parse_d_frame(d_line(1, samples).decode().strip())
        self.win._processor.ingest(df)
        path = self.win._recorder.start_recording(
            dict(self.win._config.experiment_params), self._tmp, prof,
            session_meta={"source": "serial", "session_id": "s1"},
        )
        for scan in (1, 2):
            df_i = parse_d_frame(
                d_line(scan, samples).decode().strip()
            )
            self.win._processor.ingest(df_i)
            self.win._recorder.write_dframe(
                df_i, self.win._processor,
                channel_map=self.win._config.channel_map,
                enabled_channels=self.win._config.enabled_channels,
            )
        self.win._recorder.stop_recording()
        return path


class TestStartupAndSwitch(_WinBase):
    def test_startup_applies_baseline_for_current_layout(self):
        cfg = self.win._config
        cfg.set_baselines(
            SCOPE_SERIAL, {0: 1234.0},
            {"device_id": "", "layout": LAYOUT_ACTIVE_PCB,
             "adc_profile": cfg.adc_profile.name},
        )
        cfg.layout_id = LAYOUT_ACTIVE_PCB
        cfg.save()
        # 重新建窗口：启动路径必须走统一匹配入口
        import ui.main_window as mw_mod
        win2 = mw_mod.MainWindow()
        try:
            self.assertAlmostEqual(
                win2._processor.get(0).baseline_mv, 1234.0
            )
        finally:
            win2._refresh_timer.stop()
            win2._stop_all_sources()
            win2.close()

    def test_switch_layout_clears_filter_and_display(self):
        self.feed([1000.0] * 16)
        self.assertGreater(self.win._processor.get(0).sample_count, 0)

        cfg = self.win._config
        rv = {
            "profile_name": cfg.adc_profile.name,
            "enabled_channels": list(cfg.enabled_channels),
            "ema_alpha": cfg.ema_alpha,
            "median_window": cfg.median_window,
            "heatmap_min": cfg.heatmap_min,
            "heatmap_max": cfg.heatmap_max,
            "heatmap_auto": cfg.heatmap_auto,
            "curve_window_s": cfg.curve_window_s,
            "invert_sign": cfg.invert_sign,
            "experiment_params": dict(cfg.experiment_params),
            "restore_hw_layout": False,
            "layout_id": LAYOUT_ACTIVE_PCB,
        }

        class _FakeDialog:
            Accepted = QDialog.Accepted

            def __init__(self, **kwargs):
                pass

            def exec_(self):
                return QDialog.Accepted

            @property
            def result_values(self):
                return rv

        with patch("ui.main_window.SettingsDialog", _FakeDialog):
            self.win._on_open_settings()

        self.assertEqual(self.win._config.layout_id, LAYOUT_ACTIVE_PCB)
        self.assertEqual(self.win._heatmap.channel_map(),
                         dict(ACTIVE_PCB_CHANNEL_MAP))
        # 换板后上一实验的滤波/显示状态被清理
        self.assertEqual(
            self.win._processor.get(0).sample_count, 0,
            "换布局后必须清掉上一块板的滤波状态",
        )
        self.assertIsNone(self.win._last_dframe)

    def test_recording_blocks_settings(self):
        cfg = self.win._config
        self.win._recorder.start_recording(
            dict(cfg.experiment_params), self._tmp, cfg.adc_profile,
            session_meta={"source": "serial", "session_id": "s"},
        )
        try:
            with patch("ui.main_window.SettingsDialog") as dlg_cls, \
                    patch("ui.main_window.QMessageBox.information") as info:
                self.win._on_open_settings()
                dlg_cls.assert_not_called()
                self.assertTrue(info.called)
                self.assertIn("记录", info.call_args[0][1])
        finally:
            self.win._recorder.stop_recording()


class TestMixedCsvAndReplayProfile(_WinBase):
    def _rewrite_column(self, path: str, column: str, value: str,
                        row_filter=lambda r: True) -> None:
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        idx = rows[0].index(column)
        for r in rows[1:]:
            if row_filter(r):
                while len(r) <= idx:
                    r.append("")
                r[idx] = value
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerows(rows)

    def test_mixed_layout_detected_and_noted(self):
        path = self.record_csv([1000.0] * 16,
                              "ads1115_4v096_direct", LAYOUT_ACTIVE_PCB)
        # 最后一行改成面包板布局，制造"录制中换过布局"的混写文件
        from core.channel_layout import format_compact_map
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        idx = rows[0].index("channel_map")
        self.assertGreaterEqual(len(rows), 3, "需要至少 2 行数据")
        rows[-1][idx] = format_compact_map(HARDWARE_CHANNEL_MAP)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerows(rows)

        rep = ReplaySource()
        rep.load(path)
        self.assertTrue(rep.layout_conflict, "混合布局未被检出")
        self.assertGreaterEqual(rep.layout_variant_count, 2)

        # UI 侧给出提示（按首行布局显示）
        self.win._replay.load(path)
        self.win._apply_replay_recorded_map()
        self.win._apply_replay_experiment_context()
        self.assertIn("布局", self.win._message_bar._lbl_text.text())

    def test_replay_uses_recorded_divider(self):
        """旧 1:2 文件：ADC 1000 mV 回放 VOUT 应为 2000 mV。"""
        path = self.record_csv([1000.0] * 16,
                              "ads1115_4v096_div2", LAYOUT_BREADBOARD)
        # 实时配置改成"无分压"（有源板）
        direct = build_default_profiles()["ads1115_4v096_direct"]
        self.win._processor.set_adc_profile(direct)
        self.win._config.adc_profile = direct

        self.win._replay.load(path)
        self.win._apply_replay_experiment_context()
        self.assertAlmostEqual(
            self.win._processor.effective_detector_divider, 2.0
        )
        self.win._on_replay_frame(self.win._replay._rows[0])
        snap = self.win._processor.measurement(0, True)
        self.assertAlmostEqual(snap.sample_mv, 1000.0, places=6)
        self.assertAlmostEqual(snap.detector_vout_mv, 2000.0, places=6)

        # 退出回放 → 恢复实时配置（直连 1.0）
        self.win._set_app_mode("live")
        self.assertAlmostEqual(
            self.win._processor.effective_detector_divider, 1.0
        )
        # 实时链路重新采样：同一 ADC 1000 mV 现在 VOUT = 1000 mV
        self.feed([1000.0] * 16)
        snap2 = self.win._processor.measurement(0, True)
        self.assertAlmostEqual(snap2.detector_vout_mv, 1000.0, places=6)

    def test_replay_unknown_profile_marks_vout_unknown(self):
        path = self.record_csv([1000.0] * 16,
                              "ads1115_4v096_div2", LAYOUT_BREADBOARD)
        self._rewrite_column(path, "ADC_Profile", "")
        self.win._replay.load(path)
        self.win._apply_replay_experiment_context()
        self.assertIsNone(self.win._processor.effective_detector_divider)
        self.win._on_replay_frame(self.win._replay._rows[0])
        snap = self.win._processor.measurement(0, True)
        self.assertAlmostEqual(snap.sample_mv, 1000.0, places=6)
        self.assertIsNone(snap.detector_vout_mv, "Profile 缺失时 VOUT 必须未知")
        self.assertIn("Profile", self.win._message_bar._lbl_text.text())


class TestCornersAndMaskStillCorrect(_WinBase):
    def test_four_corners(self):
        """四角：标签/坐标不变，数据通道随所选绑定变化。"""
        from core.channel_layout import BINDING_DESIGN, BINDING_LEGACY_V341

        expected = {
            BINDING_DESIGN: {
                (0, 0): ("CH09", 8),
                (0, 3): ("CH12", 11),
                (3, 0): ("CH04", 3),
                (3, 3): ("CH01", 0),
            },
            BINDING_LEGACY_V341: {
                (0, 0): ("CH09", 7),    # CH09 ← 通道 8
                (0, 3): ("CH12", 4),    # CH12 ← 通道 5
                (3, 0): ("CH04", 11),   # CH04 ← 通道 12
                (3, 3): ("CH01", 8),    # CH01 ← 通道 9
            },
        }
        for binding, table in expected.items():
            self.win._config.layout_id = LAYOUT_ACTIVE_PCB
            self.win._config.cell_binding = binding
            self.win._apply_channel_map_to_heatmap()
            cells = self.win._heatmap._cells
            for pos, (label, data_ch) in table.items():
                cell = cells[pos]
                self.assertEqual(
                    cell.title, label, f"[{binding}] {pos}"
                )
                self.assertEqual(
                    cell.channel, data_ch, f"[{binding}] {pos}"
                )

    def test_single_bit_mask(self):
        self.win._config.layout_id = LAYOUT_ACTIVE_PCB
        self.win._apply_channel_map_to_heatmap()
        for data_ch in (7, 4, 11, 8):   # CH09 / CH12 / CH04 / CH01 的数据通道
            samples = [500.0] * 16
            samples[data_ch] = 321.0
            self.feed(samples, valid_mask=1 << data_ch, scan=data_ch + 1)
            snap = self.win._processor.measurement(data_ch)
            self.assertTrue(snap.frame_valid)
            self.assertAlmostEqual(snap.sample_mv, 321.0, places=6)
            other = 15 - data_ch
            self.assertFalse(
                self.win._processor.measurement(other).frame_valid
            )


if __name__ == "__main__":
    unittest.main()
