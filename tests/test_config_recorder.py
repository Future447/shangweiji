"""
配置 + CSV 记录测试（v3 毫伏协议，schema/config 均为 v4）。

- CONFIG_VERSION=4：基线按来源作用域（serial/demo）隔离；
  旧扁平基线安全作废并备份（不静默沿用），其他设置保留
- CSV schema v4：chXX_wire_mv / filtered_mv / baseline_mv / delta_mv /
  delta_signed_mv / percent_signed / attenuation_mv /
  detector_drop_pct / status，全部 3 位小数
- valid_mask=0 通道 wire 留空
"""

import csv
import json
import os
import tempfile
import time
import unittest

from core.config_manager import (
    ConfigManager, CONFIG_VERSION, default_config,
)
from core.recorder import Recorder, build_csv_filename, sanitize_filename
from models.adc_profile import build_default_profiles, profile_by_name
from models.frame import DFrame


ALL16 = list(range(16))


def _write_json(d: dict) -> str:
    f = tempfile.NamedTemporaryFile(
        suffix=".json", delete=False, mode="w", encoding="utf-8"
    )
    json.dump(d, f)
    f.close()
    return f.name


class TestConfigManager(unittest.TestCase):
    def test_default_load(self):
        with tempfile.NamedTemporaryFile(
            suffix=".json", delete=False
        ) as f:
            path = f.name
        try:
            cfg = ConfigManager(path)
            self.assertEqual(cfg.enabled_channels, ALL16)
            self.assertEqual(cfg.adc_profile.name, "ads1115_4v096_div2")
            self.assertAlmostEqual(cfg.ema_alpha, 0.2)
            self.assertEqual(cfg.median_window, 3)
            self.assertEqual(cfg.measurement_unit, "mV")
            self.assertEqual(cfg.baseline_unit, "mV")
            self.assertFalse(cfg.baselines_invalidated)
            self.assertEqual(cfg.baseline_notice, "")
        finally:
            os.unlink(path)

    def test_save_and_load(self):
        with tempfile.NamedTemporaryFile(
            suffix=".json", delete=False
        ) as f:
            path = f.name
        try:
            cfg = ConfigManager(path)
            cfg.enabled_channels = [0, 5, 10, 15]
            cfg.ema_alpha = 0.35
            cfg.median_window = 5
            cfg.invert_sign = True
            cfg.heatmap_min = -500.0
            cfg.heatmap_max = 500.0
            cfg.baselines = {c: 1000.0 + c for c in range(16)}
            ok, err = cfg.save()
            self.assertTrue(ok, err)

            cfg2 = ConfigManager(path)
            self.assertEqual(cfg2.enabled_channels, [0, 5, 10, 15])
            self.assertAlmostEqual(cfg2.ema_alpha, 0.35)
            self.assertEqual(cfg2.median_window, 5)
            self.assertTrue(cfg2.invert_sign)
            self.assertAlmostEqual(cfg2.heatmap_min, -500.0)
            self.assertAlmostEqual(cfg2.heatmap_max, 500.0)
            for c in range(16):
                self.assertAlmostEqual(
                    cfg2.baselines[c], 1000.0 + c
                )
            # v3 往返：基线保留，不触发作废
            self.assertFalse(cfg2.baselines_invalidated)
        finally:
            os.unlink(path)

    def test_corrupt_config_returns_defaults(self):
        with tempfile.NamedTemporaryFile(
            suffix=".json", delete=False, mode="w"
        ) as f:
            f.write("{ this is not valid json")
            path = f.name
        try:
            cfg = ConfigManager(path)
            self.assertEqual(cfg.enabled_channels, ALL16)
        finally:
            os.unlink(path)

    def test_missing_keys_uses_defaults(self):
        path = _write_json({"version": CONFIG_VERSION,
                            "baseline_unit": "mV"})
        try:
            cfg = ConfigManager(path)
            self.assertEqual(cfg.enabled_channels, ALL16)
            self.assertAlmostEqual(cfg.ema_alpha, 0.2)
        finally:
            os.unlink(path)

    def test_baselines_invalid_dropped(self):
        """v4 作用域内单个非法基线值被丢弃（其余保留）。"""
        path = _write_json({
            "version": CONFIG_VERSION,
            "baseline_unit": "mV",
            "measurement_unit": "mV",
            "baseline_sets": {
                "serial": {
                    "meta": {"source": "serial", "unit": "mV",
                             "baseline_id": "bl-t"},
                    "baselines": {"0": 1.0, "1": None, "2": "bad",
                                  "3": float("inf"), "4": float("nan"),
                                  "5": -1e9},
                },
            },
        })
        try:
            cfg = ConfigManager(path)
            for c in (1, 2, 3, 4, 5):
                self.assertIsNone(cfg.baselines[c])
            self.assertAlmostEqual(cfg.baselines[0], 1.0)
            self.assertFalse(cfg.baselines_invalidated)
        finally:
            os.unlink(path)

    def test_legacy_raw_baselines_invalidated(self):
        """旧 v2 raw count 基线：安全作废 + 备份 + 提示，不静默沿用。"""
        path = _write_json({
            "version": 2,
            "baselines": {str(c): 12000.0 + c for c in range(16)},
            "ema_alpha": 0.35,
            "invert_sign": True,
            "enabled_channels": [0, 1],
        })
        try:
            cfg = ConfigManager(path)
            self.assertTrue(cfg.baselines_invalidated)
            self.assertEqual(
                cfg.baseline_notice,
                "旧配置中的基线缺少来源信息，已备份并标记为待确认；"
                "请重新进行无钢板基线标定。",
            )
            for c in range(16):
                self.assertIsNone(cfg.baselines[c])
            # 旧基线备份保留（可追溯）
            backup = cfg.legacy_baselines_backup
            self.assertIsNotNone(backup)
            self.assertEqual(backup["version"], 2)
            # 其他设置保留
            self.assertAlmostEqual(cfg.ema_alpha, 0.35)
            self.assertTrue(cfg.invert_sign)
            self.assertEqual(cfg.enabled_channels, [0, 1])
            # 单位标记被强制写入
            self.assertEqual(cfg.baseline_unit, "mV")
            self.assertEqual(cfg.measurement_unit, "mV")
        finally:
            os.unlink(path)

    def test_legacy_without_baselines_no_notice(self):
        """旧配置但没有基线：不作废、无提示。"""
        path = _write_json({"version": 2, "baselines": {}})
        try:
            cfg = ConfigManager(path)
            self.assertFalse(cfg.baselines_invalidated)
            self.assertEqual(cfg.baseline_notice, "")
        finally:
            os.unlink(path)

    def test_missing_unit_marker_treated_as_legacy(self):
        """缺 baseline_unit 标记但有基线 → 视为旧 raw，作废。"""
        path = _write_json({
            "version": CONFIG_VERSION,
            "baselines": {"0": 424.625},
        })
        try:
            cfg = ConfigManager(path)
            self.assertTrue(cfg.baselines_invalidated)
            self.assertIsNone(cfg.baselines[0])
        finally:
            os.unlink(path)

    def test_wrong_unit_marker_invalidated(self):
        """单位标记不是 mV（例如 'raw'）→ 作废。"""
        path = _write_json({
            "version": CONFIG_VERSION,
            "baseline_unit": "raw",
            "baselines": {"0": 12000.0},
        })
        try:
            cfg = ConfigManager(path)
            self.assertTrue(cfg.baselines_invalidated)
            self.assertIsNone(cfg.baselines[0])
        finally:
            os.unlink(path)

    def test_channel_map_custom(self):
        """部分/非法映射不再被静默补全：整体回退硬件默认布局。"""
        from core.channel_layout import HARDWARE_CHANNEL_MAP

        path = _write_json({
            "version": CONFIG_VERSION,
            "baseline_unit": "mV",
            "channel_map": {"0": [2, 3], "1": [0, 0]},
        })
        try:
            cfg = ConfigManager(path)
            mapping = cfg.channel_map
            # 只有 2 个通道的非法映射 → 完整硬件默认（16 通道唯一坐标）
            self.assertEqual(mapping, HARDWARE_CHANNEL_MAP)
            self.assertEqual(len(set(mapping.values())), 16)
            self.assertNotEqual(cfg.map_migration_notice, "")
        finally:
            os.unlink(path)

    def test_channel_map_valid_custom_preserved(self):
        """完整合法且不同于旧默认的自定义映射：保留，绝不静默覆盖。"""
        from core.channel_layout import (
            HARDWARE_CHANNEL_MAP, LEGACY_ROW_MAJOR_MAP,
        )

        # 转置映射：合法（16 通道、16 个唯一坐标）且 ≠ 旧默认/硬件默认
        custom = {str(ch): [ch % 4, ch // 4] for ch in range(16)}
        custom_norm = {ch: (ch % 4, ch // 4) for ch in range(16)}
        assert custom_norm != LEGACY_ROW_MAJOR_MAP
        assert custom_norm != HARDWARE_CHANNEL_MAP
        path = _write_json({
            "version": CONFIG_VERSION,
            "baseline_unit": "mV",
            "channel_map": custom,
        })
        try:
            cfg = ConfigManager(path)
            self.assertEqual(cfg.channel_map, custom_norm)
            # 未被改写为硬件默认
            self.assertNotEqual(cfg.channel_map, HARDWARE_CHANNEL_MAP)
        finally:
            os.unlink(path)

    def test_old_config_migrated(self):
        # 旧 v1 配置（无 version）应当可加载；基线为空则不作废提示
        path = _write_json({
            "enabled_channels": [0, 1],
            "ema_alpha": 0.5,
        })
        try:
            cfg = ConfigManager(path)
            self.assertEqual(cfg.enabled_channels, [0, 1])
            self.assertAlmostEqual(cfg.ema_alpha, 0.5)
            self.assertFalse(cfg.baselines_invalidated)
        finally:
            os.unlink(path)


class TestFilename(unittest.TestCase):
    def test_no_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            params = {
                "frequency": 1000, "freq_unit": "kHz",
                "gain": "×4", "coil": "测试", "plate": "有",
                "note": "test1",
            }
            paths = set()
            for _ in range(5):
                p = build_csv_filename(params, d)
                with open(p, "w", encoding="utf-8") as f:
                    f.write("")
                paths.add(p)
            self.assertEqual(len(paths), 5)

    def test_sanitize(self):
        self.assertEqual(sanitize_filename("a/b\\c"), "a_b_c")
        self.assertEqual(sanitize_filename(""), "unnamed")
        self.assertEqual(sanitize_filename("   .  "), "unnamed")


class TestRecorder(unittest.TestCase):
    def setUp(self):
        self.profiles = build_default_profiles()
        self._params = {"frequency": 1000, "freq_unit": "kHz", "gain": "1",
                        "coil": "c1", "plate": "", "note": "n1"}

    def _start(self, rec, d):
        return rec.start_recording(
            self._params, d, self.profiles["ads1115_4v096_div2"]
        )

    def test_schema_v4_columns(self):
        cols = Recorder().columns
        self.assertIn("measurement_unit", cols)
        for c in range(1, 17):
            self.assertIn(f"ch{c:02d}_wire_mv", cols)
            self.assertIn(f"ch{c:02d}_raw_valid", cols)
            self.assertIn(f"ch{c:02d}_filtered_mv", cols)
            self.assertIn(f"ch{c:02d}_baseline_mv", cols)
            self.assertIn(f"ch{c:02d}_delta_mv", cols)
            self.assertIn(f"ch{c:02d}_delta_signed_mv", cols)
            self.assertIn(f"ch{c:02d}_percent_signed", cols)
            self.assertIn(f"ch{c:02d}_attenuation_mv", cols)
            self.assertIn(f"ch{c:02d}_detector_drop_pct", cols)
            self.assertIn(f"ch{c:02d}_status", cols)
        for col in ("attenuation_formula", "baseline_source",
                    "baseline_id", "baseline_device_id"):
            self.assertIn(col, cols)
        # 不再出现 raw count 列名
        for col in cols:
            self.assertNotIn("wire_raw", col)
            self.assertNotIn("_raw,", col)

    def test_columns_match_data(self):
        with tempfile.TemporaryDirectory() as d:
            rec = Recorder()
            path = self._start(rec, d)
            snaps = {
                c + 1: {
                    "sample_mv": 100.0 + c * 0.125,
                    "filtered_mv": 100.0 + c,
                    "baseline_mv": 90.0 + c,
                    "status": 5,
                }
                for c in range(16)
            }
            ok, err = rec.write_frame(
                {
                    "scan_number": 1, "scan_status": 0,
                    "valid_mask": 0xFFFF, "i2c_err": 0,
                },
                snaps,
            )
            self.assertTrue(ok, err)
            rec.stop_recording()
            with open(path, encoding="utf-8-sig") as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 2)
            header_cols = lines[0].rstrip("\n").split(",")
            data_cols = lines[1].rstrip("\n").split(",")
            self.assertEqual(
                len(header_cols), len(data_cols),
                f"header {len(header_cols)} != data {len(data_cols)}"
            )

    def test_mv_precision_3_decimals(self):
        """424.625 必须按 3 位小数写入，不能变 425 或 424.62。"""
        with tempfile.TemporaryDirectory() as d:
            rec = Recorder()
            path = self._start(rec, d)
            df = DFrame(
                scan_number=7, scan_status=0, valid_mask=0xFFFF,
                i2c_error_count=0,
                samples_mv=(424.625,) + (306.250,) + (0.0,) * 14,
            )

            class _P:
                class _S:
                    filtered_mv = 424.625
                    baseline_mv = 424.500
                    last_status = 5

                def get(self, ch):
                    return self._S()

            ok, err = rec.write_dframe(df, _P())
            self.assertTrue(ok, err)
            rec.stop_recording()
            with open(path, encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["ch01_wire_mv"], "424.625")
            self.assertEqual(row["ch02_wire_mv"], "306.250")
            self.assertEqual(row["ch01_filtered_mv"], "424.625")
            self.assertEqual(row["ch01_baseline_mv"], "424.500")
            self.assertEqual(row["ch01_delta_mv"], "0.125")
            self.assertEqual(row["measurement_unit"], "mV")
            self.assertEqual(row["schema_version"], "4")

    def test_invalid_channel_wire_empty(self):
        """valid_mask=0 的通道 wire_mv 留空，raw_valid=0。"""
        with tempfile.TemporaryDirectory() as d:
            rec = Recorder()
            path = self._start(rec, d)
            df = DFrame(
                scan_number=1, scan_status=0, valid_mask=0x0001,
                i2c_error_count=0,
                samples_mv=(424.625,) + (111.0,) * 15,
            )

            class _P:
                class _S:
                    filtered_mv = None
                    baseline_mv = None
                    last_status = 2

                def get(self, ch):
                    return self._S()

            ok, err = rec.write_dframe(df, _P())
            self.assertTrue(ok, err)
            rec.stop_recording()
            with open(path, encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            row = rows[0]
            self.assertEqual(row["ch01_wire_mv"], "424.625")
            self.assertEqual(row["ch01_raw_valid"], "1")
            self.assertEqual(row["ch02_wire_mv"], "")
            self.assertEqual(row["ch02_raw_valid"], "0")

    def test_no_record_safe(self):
        rec = Recorder()
        ok, err = rec.write_frame({}, {})
        self.assertTrue(ok)
        ok, err = rec.stop_recording()
        self.assertTrue(ok)

    def test_close_safely(self):
        with tempfile.TemporaryDirectory() as d:
            rec = Recorder()
            path = self._start(rec, d)
            rec.write_frame({"scan_number": 1, "scan_status": 0,
                             "valid_mask": 0, "i2c_err": 0}, {})
            ok, err = rec.stop_recording()
            self.assertTrue(ok, err)
            self.assertTrue(os.path.exists(path))

    def test_bom_for_excel(self):
        with tempfile.TemporaryDirectory() as d:
            rec = Recorder()
            path = self._start(rec, d)
            rec.stop_recording()
            with open(path, "rb") as f:
                head = f.read(3)
            self.assertEqual(head, b"\xef\xbb\xbf")

    def test_chinese_columns(self):
        cols = Recorder().columns
        self.assertIn("相对时间/s", cols)
        self.assertIn("帧号", cols)
        self.assertIn("实验备注", cols)


if __name__ == "__main__":
    unittest.main()
