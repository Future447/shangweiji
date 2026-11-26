"""
基线存储隔离测试（core/config_manager.py，CONFIG_VERSION=4）。

验收点（任务书 四 / 八-8）：
- 基线按来源作用域隔离：演示标定不得覆盖真实硬件基线
- 每套基线记录来源、设备标识、单位、通道、时间、ADC/检波配置、
  频率、增益、baseline_id、公式版本
- 设备标识匹配：不只根据 COM 端口号判断是否为同一设备
  （设备标识不一致 → 不返回该基线）
- 旧配置（扁平 baselines，无来源信息）→ 备份到
  legacy_baselines_backup 并标记"待确认/需重新标定"，绝不静默沿用
- v4 往返保存/加载
"""

import json
import os
import tempfile
import unittest

from core.config_manager import (
    BASELINE_SCOPES,
    CONFIG_VERSION,
    SCOPE_DEMO,
    SCOPE_SERIAL,
    ConfigManager,
)


def _write_json(d: dict) -> str:
    f = tempfile.NamedTemporaryFile(
        suffix=".json", delete=False, mode="w", encoding="utf-8"
    )
    json.dump(d, f)
    f.close()
    return f.name


def _tmp_path():
    f = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    f.close()
    return f.name


class TestScopes(unittest.TestCase):
    def setUp(self):
        self.path = _tmp_path()

    def tearDown(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_default_scopes_empty(self):
        cfg = ConfigManager(self.path)
        for scope in BASELINE_SCOPES:
            bl = cfg.get_baselines(scope)
            self.assertEqual(len(bl), 16)
            self.assertTrue(all(v is None for v in bl.values()))
            self.assertIsNone(cfg.get_baseline_meta(scope))
        self.assertFalse(cfg.baselines_invalidated)
        self.assertEqual(cfg.baseline_notice, "")

    def test_set_and_get_serial(self):
        cfg = ConfigManager(self.path)
        cfg.set_baselines(SCOPE_SERIAL, {0: 1000.0, 1: 1001.0})
        bl = cfg.get_baselines(SCOPE_SERIAL)
        self.assertAlmostEqual(bl[0], 1000.0)
        self.assertAlmostEqual(bl[1], 1001.0)
        self.assertIsNone(bl[2])
        meta = cfg.get_baseline_meta(SCOPE_SERIAL)
        self.assertIsNotNone(meta)
        self.assertEqual(meta["source"], SCOPE_SERIAL)
        self.assertEqual(meta["unit"], "mV")
        self.assertTrue(meta["baseline_id"].startswith("bl-"))
        self.assertTrue(meta["calibrated_at"])
        self.assertEqual(meta["channels"], [0, 1])

    def test_demo_does_not_overwrite_serial(self):
        """演示标定不得覆盖真实硬件基线（任务书 四 核心）。"""
        cfg = ConfigManager(self.path)
        cfg.set_baselines(SCOPE_SERIAL, {0: 1000.0})
        cfg.set_baselines(SCOPE_DEMO, {0: 1500.0})
        self.assertAlmostEqual(cfg.get_baselines(SCOPE_SERIAL)[0], 1000.0)
        self.assertAlmostEqual(cfg.get_baselines(SCOPE_DEMO)[0], 1500.0)
        # 再写一次 demo，serial 仍不动
        cfg.set_baselines(SCOPE_DEMO, {0: 1600.0})
        self.assertAlmostEqual(cfg.get_baselines(SCOPE_SERIAL)[0], 1000.0)

    def test_clear_scope_only_affects_that_scope(self):
        cfg = ConfigManager(self.path)
        cfg.set_baselines(SCOPE_SERIAL, {0: 1000.0})
        cfg.set_baselines(SCOPE_DEMO, {0: 1500.0})
        cfg.clear_baselines(SCOPE_SERIAL)
        self.assertIsNone(cfg.get_baselines(SCOPE_SERIAL)[0])
        self.assertAlmostEqual(cfg.get_baselines(SCOPE_DEMO)[0], 1500.0)

    def test_unknown_scope_raises(self):
        cfg = ConfigManager(self.path)
        with self.assertRaises(ValueError):
            cfg.set_baselines("replay", {0: 1.0})

    def test_meta_records_experiment_context(self):
        """meta 记录 ADC/检波配置、频率、增益、公式版本。"""
        cfg = ConfigManager(self.path)
        cfg.set_baselines(
            SCOPE_SERIAL, {0: 1000.0},
            meta={
                "device_id": "USB VID:PID=1234:5678 SER=ABC",
                "adc_profile": "ads1115_4v096_div2",
                "detector_divider": 2.0,
                "frequency": 100.0,
                "freq_unit": "kHz",
                "gain": "1",
                "formula": "attenuation_mv=baseline_mv-filtered_mv@v1",
            },
        )
        meta = cfg.get_baseline_meta(SCOPE_SERIAL)
        self.assertEqual(meta["device_id"], "USB VID:PID=1234:5678 SER=ABC")
        self.assertEqual(meta["adc_profile"], "ads1115_4v096_div2")
        self.assertAlmostEqual(meta["detector_divider"], 2.0)
        self.assertAlmostEqual(meta["frequency"], 100.0)
        self.assertEqual(meta["gain"], "1")
        self.assertEqual(
            meta["formula"], "attenuation_mv=baseline_mv-filtered_mv@v1"
        )


class TestDeviceIdentity(unittest.TestCase):
    """不只根据 COM 端口号判断是否为同一设备。"""

    def setUp(self):
        self.path = _tmp_path()

    def tearDown(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _cfg_with_device(self, device_id: str) -> ConfigManager:
        cfg = ConfigManager(self.path)
        cfg.set_baselines(
            SCOPE_SERIAL, {0: 1000.0}, meta={"device_id": device_id}
        )
        return cfg

    def test_same_device_returns_baselines(self):
        cfg = self._cfg_with_device("USB VID:PID=1234:5678 SER=ABC")
        bl = cfg.get_baselines(
            SCOPE_SERIAL, device_id="USB VID:PID=1234:5678 SER=ABC"
        )
        self.assertAlmostEqual(bl[0], 1000.0)

    def test_different_device_hides_baselines(self):
        """换了设备（即使同一 COM 口）→ 不返回旧设备基线。"""
        cfg = self._cfg_with_device("USB VID:PID=1234:5678 SER=ABC")
        bl = cfg.get_baselines(
            SCOPE_SERIAL, device_id="USB VID:PID=1234:5678 SER=XYZ"
        )
        self.assertTrue(all(v is None for v in bl.values()))

    def test_missing_current_device_id_passes(self):
        """当前设备标识未知（老设备无序列号）→ 放行。"""
        cfg = self._cfg_with_device("USB VID:PID=1234:5678 SER=ABC")
        bl = cfg.get_baselines(SCOPE_SERIAL, device_id=None)
        self.assertAlmostEqual(bl[0], 1000.0)
        bl2 = cfg.get_baselines(SCOPE_SERIAL, device_id="")
        self.assertAlmostEqual(bl2[0], 1000.0)

    def test_saved_without_device_id_passes_any(self):
        """保存时无设备标识 → 任意设备可用（向后兼容）。"""
        cfg = self._cfg_with_device("")
        bl = cfg.get_baselines(SCOPE_SERIAL, device_id="ANYTHING")
        self.assertAlmostEqual(bl[0], 1000.0)


class TestLegacyMigration(unittest.TestCase):
    """旧配置安全迁移（任务书 八-8）。"""

    def test_v3_flat_baselines_backed_up_and_invalidated(self):
        flat = {str(c): 1000.0 + c for c in range(16)}
        path = _write_json({
            "version": 3,
            "baseline_unit": "mV",
            "measurement_unit": "mV",
            "baselines": flat,
            "ema_alpha": 0.35,
        })
        try:
            cfg = ConfigManager(path)
            self.assertTrue(cfg.baselines_invalidated)
            self.assertEqual(
                cfg.baseline_notice,
                "旧配置中的基线缺少来源信息，已备份并标记为待确认；"
                "请重新进行无钢板基线标定。",
            )
            # 绝不静默沿用
            self.assertTrue(
                all(v is None
                    for v in cfg.get_baselines(SCOPE_SERIAL).values())
            )
            # 备份完整保留旧值
            backup = cfg.legacy_baselines_backup
            self.assertIsNotNone(backup)
            self.assertEqual(backup["version"], 3)
            self.assertEqual(backup["baselines"], flat)
            # 其他设置保留
            self.assertAlmostEqual(cfg.ema_alpha, 0.35)
        finally:
            os.unlink(path)

    def test_v2_raw_baselines_backed_up(self):
        path = _write_json({
            "version": 2,
            "baselines": {str(c): 12000.0 + c for c in range(16)},
        })
        try:
            cfg = ConfigManager(path)
            self.assertTrue(cfg.baselines_invalidated)
            backup = cfg.legacy_baselines_backup
            self.assertIsNotNone(backup)
            self.assertEqual(backup["version"], 2)
            self.assertIsNone(cfg.get_baselines(SCOPE_SERIAL)[0])
        finally:
            os.unlink(path)

    def test_flat_baselines_in_v4_file_also_backed_up(self):
        """v4 文件里残留无来源扁平 baselines → 同样备份作废。"""
        path = _write_json({
            "version": CONFIG_VERSION,
            "baseline_unit": "mV",
            "measurement_unit": "mV",
            "baselines": {"0": 424.625},
        })
        try:
            cfg = ConfigManager(path)
            self.assertTrue(cfg.baselines_invalidated)
            self.assertIsNone(cfg.get_baselines(SCOPE_SERIAL)[0])
            backup = cfg.legacy_baselines_backup
            self.assertIsNotNone(backup)
            self.assertEqual(backup["baselines"], {"0": 424.625})
        finally:
            os.unlink(path)

    def test_legacy_without_baselines_no_notice(self):
        path = _write_json({"version": 2, "baselines": {}})
        try:
            cfg = ConfigManager(path)
            self.assertFalse(cfg.baselines_invalidated)
            self.assertEqual(cfg.baseline_notice, "")
            self.assertIsNone(cfg.legacy_baselines_backup)
        finally:
            os.unlink(path)

    def test_backup_survives_save_load_cycle(self):
        """旧基线备份不得在一次保存后被丢弃。

        回归：旧实现 save() 写出的配置不含 legacy_baselines_backup，
        下次加载时备份永久丢失——用户再无机会核对旧基线。
        """
        flat = {str(c): 1000.0 + c for c in range(16)}
        path = _write_json({
            "version": 3,
            "baseline_unit": "mV",
            "measurement_unit": "mV",
            "baselines": flat,
        })
        try:
            cfg = ConfigManager(path)
            self.assertTrue(cfg.baselines_invalidated)
            ok, err = cfg.save()
            self.assertTrue(ok, err)
            # 保存 → 重新加载：备份必须仍在
            cfg2 = ConfigManager(path)
            backup = cfg2.legacy_baselines_backup
            self.assertIsNotNone(backup, "保存/加载周期后备份不得丢失")
            self.assertEqual(backup["baselines"], flat)
            # 再保存一次 → 第三次加载仍在（不得只在首轮保留）
            ok, err = cfg2.save()
            self.assertTrue(ok, err)
            cfg3 = ConfigManager(path)
            self.assertIsNotNone(cfg3.legacy_baselines_backup)
            self.assertEqual(cfg3.legacy_baselines_backup["baselines"], flat)
        finally:
            os.unlink(path)

    def test_version_mismatch_clears_stale_baseline_sets(self):
        """版本/单位不符文件里的 baseline_sets 不得被沿用。

        回归：旧实现 d.update 把文件中的 baseline_sets 原样拷入，
        旧版本文件的 v4 作用域基线被静默当作有效——绕过"作废"语义。
        现在必须清空、备份并标记待确认。
        """
        stale_sets = {
            SCOPE_SERIAL: {
                "meta": {"source": SCOPE_SERIAL, "unit": "mV",
                         "baseline_id": "bl-stale"},
                "baselines": {"0": 999.0},
            },
            SCOPE_DEMO: {
                "meta": None,
                "baselines": {str(c): None for c in range(16)},
            },
        }
        path = _write_json({
            "version": 3,  # 旧版本号 + 却带 v4 结构 → 整体可疑
            "baseline_unit": "mV",
            "measurement_unit": "mV",
            "baseline_sets": stale_sets,
        })
        try:
            cfg = ConfigManager(path)
            self.assertTrue(cfg.baselines_invalidated)
            # 不得沿用
            self.assertTrue(
                all(v is None
                    for v in cfg.get_baselines(SCOPE_SERIAL).values())
            )
            self.assertIsNone(cfg.get_baseline_meta(SCOPE_SERIAL))
            # 备份里保留旧结构供人工核对
            backup = cfg.legacy_baselines_backup
            self.assertIsNotNone(backup)
            self.assertIn("baseline_sets", backup)
            self.assertEqual(
                backup["baseline_sets"][SCOPE_SERIAL]["baselines"]["0"],
                999.0,
            )
        finally:
            os.unlink(path)


class TestRoundTrip(unittest.TestCase):
    def test_save_load_preserves_scopes_and_meta(self):
        path = _tmp_path()
        try:
            cfg = ConfigManager(path)
            cfg.set_baselines(
                SCOPE_SERIAL, {0: 1000.0, 5: 1005.0},
                meta={"device_id": "DEV-1", "gain": "4"},
            )
            cfg.set_baselines(SCOPE_DEMO, {0: 1500.0})
            ok, err = cfg.save()
            self.assertTrue(ok, err)

            cfg2 = ConfigManager(path)
            self.assertFalse(cfg2.baselines_invalidated)
            self.assertAlmostEqual(
                cfg2.get_baselines(SCOPE_SERIAL, device_id="DEV-1")[0],
                1000.0,
            )
            self.assertAlmostEqual(
                cfg2.get_baselines(SCOPE_SERIAL)[5], 1005.0
            )
            self.assertAlmostEqual(cfg2.get_baselines(SCOPE_DEMO)[0], 1500.0)
            meta = cfg2.get_baseline_meta(SCOPE_SERIAL)
            self.assertEqual(meta["device_id"], "DEV-1")
            self.assertEqual(meta["gain"], "4")
            self.assertTrue(meta["baseline_id"].startswith("bl-"))
        finally:
            os.unlink(path)

    def test_invalid_values_in_scope_dropped(self):
        """作用域内非法基线值（inf/nan/超范围/非数）被丢弃，其余保留。"""
        path = _write_json({
            "version": CONFIG_VERSION,
            "baseline_unit": "mV",
            "baseline_sets": {
                SCOPE_SERIAL: {
                    "meta": {
                        "source": SCOPE_SERIAL, "unit": "mV",
                        "baseline_id": "bl-test", "channels": [0],
                    },
                    "baselines": {
                        "0": 1.0, "1": None, "2": "bad",
                        "3": float("inf"), "4": float("nan"),
                        "5": -1e9,
                    },
                },
            },
        })
        try:
            cfg = ConfigManager(path)
            bl = cfg.get_baselines(SCOPE_SERIAL)
            for c in (1, 2, 3, 4, 5):
                self.assertIsNone(bl[c])
            self.assertAlmostEqual(bl[0], 1.0)
            self.assertFalse(cfg.baselines_invalidated)
            # meta 仍有效（至少一个基线保留）
            self.assertIsNotNone(cfg.get_baseline_meta(SCOPE_SERIAL))
        finally:
            os.unlink(path)

    def test_meta_wrong_unit_rejected(self):
        """非 mV 单位的 meta 不得进入 mV 空间。"""
        path = _write_json({
            "version": CONFIG_VERSION,
            "baseline_unit": "mV",
            "baseline_sets": {
                SCOPE_SERIAL: {
                    "meta": {"source": SCOPE_SERIAL, "unit": "raw",
                             "baseline_id": "bl-x"},
                    "baselines": {"0": 12000.0},
                },
            },
        })
        try:
            cfg = ConfigManager(path)
            self.assertIsNone(cfg.get_baseline_meta(SCOPE_SERIAL))
            self.assertIsNone(cfg.get_baselines(SCOPE_SERIAL)[0])
        finally:
            os.unlink(path)

    def test_compat_property_delegates_to_serial(self):
        """旧 baselines 属性 = serial 作用域。"""
        path = _tmp_path()
        try:
            cfg = ConfigManager(path)
            cfg.baselines = {c: 1000.0 + c for c in range(16)}
            self.assertAlmostEqual(cfg.baselines[3], 1003.0)
            self.assertAlmostEqual(
                cfg.get_baselines(SCOPE_SERIAL)[3], 1003.0
            )
            ok, err = cfg.save()
            self.assertTrue(ok, err)
            cfg2 = ConfigManager(path)
            self.assertAlmostEqual(cfg2.baselines[3], 1003.0)
            self.assertFalse(cfg2.baselines_invalidated)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
