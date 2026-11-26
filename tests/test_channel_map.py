"""
tests/test_channel_map.py — 通道物理位置映射回归测试（Phase C）。

任务书第六节要求的 8 项回归：
1. 完整 4×4 布局符合给定矩阵（4 列、每列自下而上 CH01→CH16）
2. 每通道不同测试值 → 值/标签/位置三元组一致（移动完整单元格，
   不是调换文字标签伪造位置）
3. 点击左下角选中 CH01、右上角选中 CH16；键盘方向键按物理相邻移动
4. 布局变更前后：基线/滤波状态/valid_mask 解释/CSV 测量列完全不变
5. 旧行优先默认配置一次性迁移：布局更新、重启保持、真实基线不丢
6. 合法自定义映射不被静默覆盖（含显式"恢复硬件默认布局"入口）
7. 演示目标左下/右上 → CH01/CH16 响应最大；DemoSource 创建与更新
   均传入当前映射
8. 新 CSV 保存生效映射（测量列顺序不变）；回放按记录时布局显示、
   退出回放恢复实时布局；无映射的历史文件明确按当前布局显示

隔离要求：全部使用临时 config 与临时 CSV 目录，
绝不读取/覆盖用户真实基线与真实实验数据。
"""

import csv
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QCoreApplication, QEvent, Qt
from PyQt5.QtGui import QKeyEvent
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QMessageBox

from core.channel_layout import (
    HARDWARE_CHANNEL_MAP,
    LEGACY_ROW_MAJOR_MAP,
    MAPPING_VERSION_CURRENT,
    is_valid_map,
    map_to_grid_list,
    maps_equal,
    normalize_channel_map,
    parse_compact_map,
    validate_channel_map,
)
from core.config_manager import (
    SCOPE_SERIAL,
    ConfigManager,
)
from core.demo_source import DEFAULT_MAP_4x4, DemoSource
from core.recorder import Recorder
from core.replay_source import ReplaySource
from models.adc_profile import build_default_profiles, profile_by_name
from models.channel_state import ChannelStatus
from models.frame import DFrame, MAX_CHANNEL_COUNT
from ui.heatmap_widget import HeatmapWidget
from ui.main_window import MainWindow


def make_d(scan=1, mask=0xFFFF, samples=None):
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


def _new_processor(enabled=None):
    proc_profiles = build_default_profiles()
    from core.channel_processor import ChannelProcessor
    proc = ChannelProcessor(
        adc_profile=profile_by_name("ads1115_4v096_div2", proc_profiles),
        enabled_channels=list(enabled) if enabled is not None
        else list(range(16)),
    )
    return proc


# 期望矩阵（任务书 一）：热力图自上而下
EXPECTED_ROWS = [
    [3, 7, 11, 15],    # CH04 CH08 CH12 CH16
    [2, 6, 10, 14],    # CH03 CH07 CH11 CH15
    [1, 5, 9, 13],     # CH02 CH06 CH10 CH14
    [0, 4, 8, 12],     # CH01 CH05 CH09 CH13
]


class _MainWindowTestBase(unittest.TestCase):
    """与 test_practicality_fixes 相同的隔离模式：临时 config + 临时 CSV。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="chmap_")
        import app_paths
        self._orig_csv_dir = app_paths.get_default_csv_dir
        app_paths.get_default_csv_dir = lambda: self._tmp  # type: ignore
        import ui.main_window as mw_mod
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


# ---------------------------------------------------------------------------
# 1. 完整 4×4 布局符合给定矩阵；映射来源唯一
# ---------------------------------------------------------------------------
class TestHardwareLayoutMatrix(unittest.TestCase):
    def test_formula_and_matrix(self):
        # 公式：row = 3 - channel % 4；col = channel // 4
        for ch in range(16):
            self.assertEqual(
                HARDWARE_CHANNEL_MAP[ch], (3 - ch % 4, ch // 4),
                f"通道 {ch} 坐标不符公式",
            )
        # 16 通道、16 个唯一坐标
        ok, reason = validate_channel_map(HARDWARE_CHANNEL_MAP)
        self.assertTrue(ok, reason)
        self.assertEqual(len(set(HARDWARE_CHANNEL_MAP.values())), 16)

    def test_heatmap_cells_match_given_matrix(self):
        QApplication.instance()
        heat = HeatmapWidget()
        try:
            for r in range(4):
                for c in range(4):
                    cell = heat._cells[(r, c)]
                    expected_ch = EXPECTED_ROWS[r][c]
                    self.assertEqual(
                        cell.channel, expected_ch,
                        f"({r},{c}) 应为内部通道 {expected_ch}",
                    )
                    # 标签按通道号生成（CH01–CH16），不伪造
                    self.assertEqual(
                        cell.title, f"CH{expected_ch + 1:02d}"
                    )
                    self.assertEqual(cell.position_label, f"R{r + 1}C{c + 1}")
            # 角点：左下 CH01、右下 CH13、左上 CH04、右上 CH16
            self.assertEqual(heat.channel_position(0), (3, 0))
            self.assertEqual(heat.channel_position(12), (3, 3))
            self.assertEqual(heat.channel_position(3), (0, 0))
            self.assertEqual(heat.channel_position(15), (0, 3))
        finally:
            heat.deleteLater()

    def test_single_source_agreement(self):
        """config 默认 / 热力图默认 / 演示默认全部引用同一硬件映射。"""
        QApplication.instance()
        with tempfile.NamedTemporaryFile(
                suffix=".json", delete=False) as f:
            path = f.name
        try:
            cfg = ConfigManager(config_path=path)
            self.assertEqual(cfg.channel_map, HARDWARE_CHANNEL_MAP)
        finally:
            os.unlink(path)
        heat = HeatmapWidget()
        try:
            self.assertEqual(heat.channel_map(), HARDWARE_CHANNEL_MAP)
        finally:
            heat.deleteLater()
        self.assertEqual(
            DEFAULT_MAP_4x4,
            [HARDWARE_CHANNEL_MAP[ch] for ch in range(16)],
        )
        self.assertEqual(
            map_to_grid_list(),
            [HARDWARE_CHANNEL_MAP[ch] for ch in range(16)],
        )

    def test_validator_rejects_bad_maps(self):
        # 重叠坐标
        dup = {ch: (0, 0) for ch in range(16)}
        ok, reason = validate_channel_map(dup)
        self.assertFalse(ok)
        self.assertIn("duplicate", reason)
        # 缺失通道
        missing = {ch: HARDWARE_CHANNEL_MAP[ch] for ch in range(15)}
        ok, reason = validate_channel_map(missing)
        self.assertFalse(ok)
        self.assertIn("missing", reason)
        # 坐标越界
        oob = dict(HARDWARE_CHANNEL_MAP)
        oob[0] = (4, 0)
        ok, reason = validate_channel_map(oob)
        self.assertFalse(ok)
        self.assertIn("range", reason)
        # 格式错误
        ok, reason = validate_channel_map({"x": [0, 0]})
        self.assertFalse(ok)
        # 旧行优先默认本身合法（用于迁移识别）
        self.assertTrue(is_valid_map(LEGACY_ROW_MAJOR_MAP))
        self.assertFalse(maps_equal(
            LEGACY_ROW_MAJOR_MAP, HARDWARE_CHANNEL_MAP))

    def test_heatmap_rejects_invalid_map_without_overlap(self):
        """非法输入显式回退硬件默认：绝不静默产生重叠单元格。"""
        QApplication.instance()
        heat = HeatmapWidget()
        try:
            bad = {ch: (0, 0) for ch in range(16)}  # 全部重叠
            heat.set_channel_map(bad)
            self.assertEqual(heat.channel_map(), HARDWARE_CHANNEL_MAP)
            self.assertEqual(len(heat._cells), 16)
            self.assertEqual(len(set(heat.channel_map().values())), 16)
        finally:
            heat.deleteLater()


# ---------------------------------------------------------------------------
# 2. 值/标签/位置三元组一致（移动完整单元格，非调换标签）
# ---------------------------------------------------------------------------
class TestValueLabelPositionTriple(unittest.TestCase):
    def test_distinct_values_follow_whole_cell(self):
        QApplication.instance()
        heat = HeatmapWidget()
        try:
            for ch in range(16):
                heat.update_cell(
                    ch, 100.0 + ch, ChannelStatus.OK, True,
                    extras={"sample_mv": 900.0 + ch, "valid": True},
                )
            for r in range(4):
                for c in range(4):
                    cell = heat._cells[(r, c)]
                    ch = cell.channel
                    # 位置 ↔ 通道 ↔ 标签 ↔ 值 四者同源
                    self.assertEqual((r, c), (3 - ch % 4, ch // 4))
                    self.assertEqual(heat.channel_position(ch), (r, c))
                    self.assertEqual(cell.title, f"CH{ch + 1:02d}")
                    self.assertAlmostEqual(cell.attenuation_value, 100.0 + ch)
                    self.assertAlmostEqual(cell.sample_mv, 900.0 + ch)
            # 具体抽查：左下角单元格是完整的 CH01 单元格
            cell_bl = heat._cells[(3, 0)]
            self.assertEqual(cell_bl.channel, 0)
            self.assertEqual(cell_bl.title, "CH01")
            self.assertAlmostEqual(cell_bl.attenuation_value, 100.0)
            # 右上角是完整的 CH16 单元格
            cell_tr = heat._cells[(0, 3)]
            self.assertEqual(cell_tr.channel, 15)
            self.assertEqual(cell_tr.title, "CH16")
            self.assertAlmostEqual(cell_tr.attenuation_value, 115.0)
        finally:
            heat.deleteLater()


# ---------------------------------------------------------------------------
# 3. 点击左下/右上角选中 CH01/CH16；键盘按物理相邻移动
# ---------------------------------------------------------------------------
class TestClickAndKeyboardNav(_MainWindowTestBase):
    def test_click_bottom_left_selects_ch01_top_right_ch16(self):
        win = self._new_window()
        cell_bl = win._heatmap._cells[(3, 0)]
        self.assertEqual(cell_bl.channel, 0)
        QTest.mouseClick(cell_bl, Qt.LeftButton)
        _process_events(30)
        self.assertEqual(win._selected_channel, 0)
        self.assertIn("CH01", win._channel_panel._lbl_title.text())
        self.assertEqual(
            win._channel_panel._lbl_position.text(), "R4C1")

        cell_tr = win._heatmap._cells[(0, 3)]
        self.assertEqual(cell_tr.channel, 15)
        QTest.mouseClick(cell_tr, Qt.LeftButton)
        _process_events(30)
        self.assertEqual(win._selected_channel, 15)
        self.assertIn("CH16", win._channel_panel._lbl_title.text())
        self.assertEqual(
            win._channel_panel._lbl_position.text(), "R1C4")
        self._safe_close(win)

    def test_keyboard_nav_moves_by_physical_adjacency(self):
        win = self._new_window()
        heat = win._heatmap
        heat.select_channel(0)   # CH01 在左下 (3,0)

        def key(k):
            heat.keyPressEvent(
                QKeyEvent(QEvent.KeyPress, k, Qt.NoModifier))

        key(Qt.Key_Up)           # (3,0) → (2,0) = CH02
        self.assertEqual(heat.selected(), 1)
        key(Qt.Key_Right)        # (2,0) → (2,1) = CH06
        self.assertEqual(heat.selected(), 5)
        key(Qt.Key_Down)         # (2,1) → (3,1) = CH05
        self.assertEqual(heat.selected(), 4)
        key(Qt.Key_Left)         # (3,1) → (3,0) = CH01
        self.assertEqual(heat.selected(), 0)
        key(Qt.Key_Down)         # 越界：保持 CH01
        self.assertEqual(heat.selected(), 0)
        # 右上角 CH16：再 Up 越界保持
        heat.select_channel(15)
        key(Qt.Key_Up)
        self.assertEqual(heat.selected(), 15)
        self._safe_close(win)


# ---------------------------------------------------------------------------
# 4. 布局变更前后：数据通道语义完全不变
# ---------------------------------------------------------------------------
class TestDataNotReordered(_MainWindowTestBase):
    def test_state_and_csv_columns_unchanged_by_layout_change(self):
        win = self._new_window()
        win._start_demo()
        _process_events(300)
        win._demo.stop()
        _process_events(120)

        # 预置 16 通道互不相同且非 None 的基线——否则"布局变更
        # 前后基线不变"退化为 None==None 空断言，无法发现真实
        # 基线被交换或清空
        for ch in range(16):
            win._processor.get(ch).baseline_mv = 11000.0 + 100 * ch

        samples1 = tuple(1000.0 + 10 * i for i in range(16))
        df1 = make_d(scan=5001, mask=0xFFFF, samples=samples1)
        win._on_dframe(df1)
        win._update_all_displays()

        # 记录 CSV（临时目录）
        win._config.last_csv_dir = self._tmp
        win._config.save()
        win._btn_rec.setChecked(True)
        _process_events(80)
        self.assertTrue(win._recorder.is_recording)
        win._on_dframe(df1)
        win._btn_rec.setChecked(False)
        _process_events(50)
        csv1 = win._recorder.file_path if hasattr(
            win._recorder, "file_path") else None
        # file_path 在 stop 后仍保留；若为空则从目录里找
        if not csv1 or not os.path.isfile(csv1):
            csvs = [f for f in os.listdir(self._tmp) if f.endswith(".csv")]
            self.assertTrue(csvs, "未生成 CSV")
            csv1 = os.path.join(self._tmp, csvs[0])

        # 布局变更前的状态快照
        base_before = {
            ch: win._processor.get(ch).baseline_mv for ch in range(16)}
        filt_before = {
            ch: win._processor.get(ch).filtered_mv for ch in range(16)}
        valid_before = {
            ch: win._processor.last_wire_valid(ch) for ch in range(16)}
        enabled_before = list(win._processor.enabled_channels)
        with open(csv1, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        header_before = rows[0]
        data1 = rows[1]
        idx_ch01_wire = header_before.index("ch01_wire_mv")
        ch01_wire_before = data1[idx_ch01_wire]

        # ---- 改布局（合法自定义：转置映射）----
        custom = {ch: (ch % 4, ch // 4) for ch in range(16)}
        self.assertTrue(is_valid_map(custom))
        win._config.channel_map = custom
        win._apply_channel_map_to_heatmap()
        _process_events(30)
        self.assertEqual(win._heatmap.channel_map(), custom)
        # 转置后 CH01 在左上 (0,0)
        self.assertEqual(win._heatmap.channel_position(0), (0, 0))

        # ---- 布局变更本身不得触碰任何按通道号索引的状态 ----
        # （在喂入新帧之前断言，隔离"布局调整"这一动作本身）
        base_now = {ch: win._processor.get(ch).baseline_mv
                    for ch in range(16)}
        self.assertEqual(base_before, base_now)
        # 基线非 None 且逐通道互异：相等性比较才有鉴别力
        # （任何交换/清空都会立即失败）
        self.assertTrue(all(v is not None for v in base_now.values()))
        self.assertEqual(len(set(base_now.values())), 16)
        # 滤波状态逐通道原样延续（未被交换/清零）
        self.assertEqual(filt_before, {
            ch: win._processor.get(ch).filtered_mv for ch in range(16)})
        self.assertTrue(all(v is not None for v in filt_before.values()))
        self.assertEqual(valid_before, {
            ch: win._processor.last_wire_valid(ch) for ch in range(16)})
        self.assertEqual(
            enabled_before, list(win._processor.enabled_channels))

        # 布局变更后再喂一帧并记录
        samples2 = tuple(2000.0 + 10 * i for i in range(16))
        df2 = make_d(scan=5002, mask=0xFFFF, samples=samples2)
        win._on_dframe(df2)
        win._btn_rec.setChecked(True)
        _process_events(80)
        win._on_dframe(df2)
        win._btn_rec.setChecked(False)
        _process_events(50)
        csvs = sorted(
            f for f in os.listdir(self._tmp) if f.endswith(".csv"))
        self.assertGreaterEqual(len(csvs), 2)
        csv2 = os.path.join(self._tmp, csvs[-1])
        with open(csv2, encoding="utf-8-sig", newline="") as f:
            rows2 = list(csv.reader(f))
        header_after = rows2[0]
        data2 = rows2[1]

        # ---- 数据通道语义不变的硬断言（喂入新帧之后）----
        # 基线/启用/valid_mask 解释仍未被布局调整交换或清空
        # （滤波状态随新帧按通道号继续演化，其"布局动作不变"
        # 断言已在上方喂帧前完成）
        self.assertEqual(base_before, {
            ch: win._processor.get(ch).baseline_mv for ch in range(16)})
        for ch in range(16):
            self.assertIsNotNone(win._processor.get(ch).filtered_mv)
        # CSV 测量列顺序完全不变
        self.assertEqual(header_before, header_after)
        # samples_mv[0] 始终写入 ch01_* 列（数据未随显示位置重排）
        self.assertEqual(
            data2[header_after.index("ch01_wire_mv")], f"{samples2[0]:.3f}")
        self.assertEqual(
            data2[header_after.index("ch16_wire_mv")], f"{samples2[15]:.3f}")
        self.assertEqual(ch01_wire_before, f"{samples1[0]:.3f}")
        # channel_map 列记录的是生效映射（布局变更后应为自定义映射）
        map_cell = data2[header_after.index("channel_map")]
        self.assertEqual(parse_compact_map(map_cell), custom)
        map_cell1 = data1[header_before.index("channel_map")]
        self.assertEqual(parse_compact_map(map_cell1), HARDWARE_CHANNEL_MAP)
        self._safe_close(win)


# ---------------------------------------------------------------------------
# 5. 旧行优先默认配置：一次性迁移 + 重启保持 + 基线不丢
# ---------------------------------------------------------------------------
class TestLegacyMapMigration(unittest.TestCase):
    def _write_legacy_config(self, directory: str) -> str:
        path = os.path.join(directory, "config.json")
        legacy_map = {str(i): [i // 4, i % 4] for i in range(16)}
        doc = {
            "version": 4,
            "baseline_unit": "mV",
            "channel_map": legacy_map,
            "enabled_channels": [0, 1, 2, 3],
            "ema_alpha": 0.33,
            "baseline_sets": {
                "serial": {
                    "meta": {
                        "source": "serial", "unit": "mV",
                        "device_id": "USB-T123", "channels": list(range(16)),
                    },
                    "baselines": {
                        str(c): 1200.0 + c for c in range(16)},
                },
                "demo": {
                    "meta": None,
                    "baselines": {str(c): None for c in range(16)},
                },
            },
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        return path

    def test_legacy_default_map_migrated_once(self):
        tmp = tempfile.mkdtemp(prefix="mig_")
        try:
            path = self._write_legacy_config(tmp)
            cfg = ConfigManager(config_path=path)
            # 迁移到实际接线布局
            self.assertEqual(cfg.channel_map, HARDWARE_CHANNEL_MAP)
            self.assertNotEqual(cfg.map_migration_notice, "")
            # 真实基线不受布局迁移影响（未交换、未清空）
            bl = cfg.get_baselines(SCOPE_SERIAL, "USB-T123")
            self.assertEqual(bl[0], 1200.0)
            self.assertEqual(bl[15], 1215.0)
            # 其他参数保留
            self.assertEqual(cfg.enabled_channels, [0, 1, 2, 3])
            self.assertAlmostEqual(cfg.ema_alpha, 0.33)
            # 迁移前已备份原配置
            bak = path + ".premap_v2.bak"
            self.assertTrue(os.path.isfile(bak), "迁移前未备份配置")
            with open(bak, encoding="utf-8") as f:
                bak_doc = json.load(f)
            self.assertEqual(
                bak_doc["channel_map"]["0"], [0, 0],
                "备份应保留旧行优先映射")
            # 迁移标记与新映射已写盘
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(
                saved.get("mapping_version"), MAPPING_VERSION_CURRENT)
            self.assertEqual(saved["channel_map"]["0"], [3, 0])
            self.assertEqual(saved["channel_map"]["15"], [0, 3])
            # 基线在写盘文件中同样保留
            self.assertEqual(
                saved["baseline_sets"]["serial"]["baselines"]["0"], 1200.0)
            # CONFIG_VERSION 未被 bump（v4 基线加载门槛不受影响）
            self.assertEqual(saved["version"], 4)

            # 重启（重新加载）：布局保持，不重复迁移
            bak_mtime = os.path.getmtime(bak)
            cfg2 = ConfigManager(config_path=path)
            self.assertEqual(cfg2.channel_map, HARDWARE_CHANNEL_MAP)
            self.assertEqual(cfg2.map_migration_notice, "")
            self.assertEqual(os.path.getmtime(bak), bak_mtime)
            bl2 = cfg2.get_baselines(SCOPE_SERIAL, "USB-T123")
            self.assertEqual(bl2[0], 1200.0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_missing_map_uses_new_default(self):
        tmp = tempfile.mkdtemp(prefix="mig2_")
        try:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"version": 4, "baseline_unit": "mV"}, f)
            cfg = ConfigManager(config_path=path)
            self.assertEqual(cfg.channel_map, HARDWARE_CHANNEL_MAP)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_malformed_map_repaired_and_persisted(self):
        """channel_map 键存在但值畸形（null/空表/字符串）：
        迁移必须显式写回硬件默认并落盘——畸形值不得随
        mapping_version 标记永久留在文件里；提示文案区分
        "缺少映射"与"格式非法"。"""
        for bad_value in (None, [], {}, "0:3x0;1:2x0"):
            tmp = tempfile.mkdtemp(prefix="migbad_")
            try:
                path = os.path.join(tmp, "config.json")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({
                        "version": 4, "baseline_unit": "mV",
                        "channel_map": bad_value,
                    }, f)
                cfg = ConfigManager(config_path=path)
                # 运行时回退硬件默认
                self.assertEqual(
                    cfg.channel_map, HARDWARE_CHANNEL_MAP,
                    f"bad value: {bad_value!r}")
                self.assertIn("格式非法", cfg.map_migration_notice)
                # 落盘文件已被修复（不是把畸形值永久留在盘上）
                with open(path, encoding="utf-8") as f:
                    saved = json.load(f)
                self.assertEqual(
                    saved.get("mapping_version"), MAPPING_VERSION_CURRENT)
                self.assertEqual(saved["channel_map"]["0"], [3, 0])
                self.assertEqual(saved["channel_map"]["15"], [0, 3])
                self.assertEqual(len(saved["channel_map"]), 16)
                # 迁移前已备份（原畸形值可追溯）
                self.assertTrue(os.path.isfile(path + ".premap_v2.bak"))
                # 二次加载：不重复迁移、不再提示
                cfg2 = ConfigManager(config_path=path)
                self.assertEqual(cfg2.channel_map, HARDWARE_CHANNEL_MAP)
                self.assertEqual(cfg2.map_migration_notice, "")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

    def test_inf_coordinate_no_crash_and_repaired(self):
        """json 可解析 1e999/Infinity 为 float('inf')，int(inf) 抛
        OverflowError——normalize 必须显式丢弃而非让启动迁移崩溃。"""
        # 单元级：normalize/validate 对 inf 坐标不抛异常
        norm = normalize_channel_map(
            {"0": [1e999, 0], "1": [float("inf"), 1]})
        self.assertEqual(norm, {})
        ok, reason = validate_channel_map({"0": [1e999, 0]})
        self.assertFalse(ok)

        # 集成级：含 inf 坐标的旧配置正常完成迁移，不崩溃
        tmp = tempfile.mkdtemp(prefix="miginf_")
        try:
            path = os.path.join(tmp, "config.json")
            doc = {
                "version": 4, "baseline_unit": "mV",
                "channel_map": {
                    **{str(i): [i // 4, i % 4] for i in range(16)},
                    "3": [1e999, 0],
                },
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(doc, f)  # 默认允许 Infinity 字面量
            cfg = ConfigManager(config_path=path)
            self.assertEqual(cfg.channel_map, HARDWARE_CHANNEL_MAP)
            self.assertNotEqual(cfg.map_migration_notice, "")
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(
                saved.get("mapping_version"), MAPPING_VERSION_CURRENT)
            self.assertEqual(saved["channel_map"]["3"], [0, 0])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 6. 合法自定义映射不被静默覆盖；显式恢复入口可用
# ---------------------------------------------------------------------------
class TestCustomMapPreservedAndRestore(_MainWindowTestBase):
    def _write_custom_config(self, path: str, custom: dict) -> None:
        doc = {
            "version": 4,
            "baseline_unit": "mV",
            "channel_map": {str(k): list(v) for k, v in custom.items()},
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)

    def test_custom_map_not_overwritten_and_restore_entry(self):
        from ui.settings_dialog import SettingsDialog

        custom = {ch: (ch % 4, ch // 4) for ch in range(16)}  # 转置
        self._write_custom_config(self._cfg_path, custom)

        # 加载：自定义映射保留（ConfigManager 直接读该文件验证）
        cfg = ConfigManager(config_path=self._cfg_path)
        self.assertEqual(cfg.channel_map, custom)
        self.assertNotEqual(cfg.channel_map, HARDWARE_CHANNEL_MAP)

        win = self._new_window()
        # 主窗口按自定义布局显示
        self.assertEqual(win._heatmap.channel_map(), custom)
        self.assertEqual(win._heatmap.channel_position(0), (0, 0))

        # 设置对话框：标注自定义布局，提供显式恢复入口
        dlg = SettingsDialog(
            current_profile=win._config.adc_profile,
            enabled_channels=win._config.enabled_channels,
            ema_alpha=win._config.ema_alpha,
            median_window=win._config.median_window,
            heatmap_min=win._config.heatmap_min,
            heatmap_max=win._config.heatmap_max,
            heatmap_auto=win._config.heatmap_auto,
            curve_window_s=win._config.curve_window_s,
            invert_sign=win._config.invert_sign,
            experiment_params=win._config.experiment_params,
            channel_map=win._config.channel_map,
            parent=win,
        )
        self.assertFalse(dlg._is_hw_layout)
        self.assertTrue(dlg._btn_restore_layout.isEnabled())
        # 启用网格也按自定义物理位置摆放（转置后 CH01 在左上）
        w00 = dlg._chips_grid.itemAtPosition(0, 0).widget()
        self.assertEqual(w00.channel, 0)
        # 取消（未点恢复）：result_values 不含恢复请求
        with patch.object(
                QMessageBox, "question", return_value=QMessageBox.No):
            dlg._on_restore_hw_layout()
        self.assertFalse(dlg._restore_hw_layout)
        # 确认恢复：标记生效，网格立即按硬件默认重排
        with patch.object(
                QMessageBox, "question", return_value=QMessageBox.Yes):
            dlg._on_restore_hw_layout()
        self.assertTrue(dlg._restore_hw_layout)
        w30 = dlg._chips_grid.itemAtPosition(3, 0).widget()
        self.assertEqual(w30.channel, 0, "恢复后 CH01 应回到左下")
        dlg._on_accept()
        self.assertTrue(dlg.result_values.get("restore_hw_layout"))
        dlg.deleteLater()
        self._safe_close(win)

    def test_hw_layout_dialog_disables_restore(self):
        from ui.settings_dialog import SettingsDialog

        win = self._new_window()  # 临时配置默认即硬件布局
        self.assertEqual(win._heatmap.channel_map(), HARDWARE_CHANNEL_MAP)
        dlg = SettingsDialog(
            current_profile=win._config.adc_profile,
            enabled_channels=win._config.enabled_channels,
            ema_alpha=win._config.ema_alpha,
            median_window=win._config.median_window,
            heatmap_min=win._config.heatmap_min,
            heatmap_max=win._config.heatmap_max,
            heatmap_auto=win._config.heatmap_auto,
            curve_window_s=win._config.curve_window_s,
            invert_sign=win._config.invert_sign,
            experiment_params=win._config.experiment_params,
            channel_map=win._config.channel_map,
            parent=win,
        )
        self.assertTrue(dlg._is_hw_layout)
        self.assertFalse(dlg._btn_restore_layout.isEnabled())
        dlg._on_accept()
        self.assertFalse(dlg.result_values.get("restore_hw_layout"))
        dlg.deleteLater()
        self._safe_close(win)


# ---------------------------------------------------------------------------
# 7. 演示物理响应：左下目标 CH01 最大，右上目标 CH16 最大
# ---------------------------------------------------------------------------
class TestDemoPhysicalResponse(_MainWindowTestBase):
    def _emit_with_plate(self, demo: DemoSource, row: float, col: float):
        frames = []
        demo.frame_ready.connect(frames.append)
        demo.set_plate_inserted(True)
        demo.set_plate_pos(row, col)
        demo._emit_frame()
        self.assertTrue(frames)
        return frames[-1]

    def test_plate_bottom_left_max_ch01_top_right_max_ch16(self):
        proc = _new_processor()
        demo = DemoSource(
            processor=proc,
            enabled_channels=list(range(16)),
            map_to_grid=map_to_grid_list(),
        )
        # 左下角 (3,0)：CH01 响应最大（采样最低）
        df = self._emit_with_plate(demo, 3.0, 0.0)
        drops = [1500.0 - s for s in df.samples_mv]
        self.assertEqual(
            max(range(16), key=lambda ch: drops[ch]), 0,
            "目标在左下角时 CH01 响应应最大")
        self.assertGreater(drops[0], drops[15])
        # 右上角 (0,3)：CH16 响应最大
        df2 = self._emit_with_plate(demo, 0.0, 3.0)
        drops2 = [1500.0 - s for s in df2.samples_mv]
        self.assertEqual(
            max(range(16), key=lambda ch: drops2[ch]), 15,
            "目标在右上角时 CH16 响应应最大")
        self.assertGreater(drops2[15], drops2[0])

    def test_mainwindow_passes_map_on_create_and_update(self):
        win = self._new_window()
        win._start_demo()
        _process_events(80)
        # 创建时传入当前（硬件默认）映射
        self.assertEqual(
            list(win._demo._map),
            [HARDWARE_CHANNEL_MAP[ch] for ch in range(16)],
        )
        win._demo.stop()
        # 更新：配置换成合法自定义映射后重启演示，映射跟随
        custom = {ch: (ch % 4, ch // 4) for ch in range(16)}
        win._config.channel_map = custom
        win._start_demo()
        _process_events(50)
        self.assertEqual(
            list(win._demo._map), [custom[ch] for ch in range(16)])
        win._demo.stop()
        self._safe_close(win)


# ---------------------------------------------------------------------------
# 8. CSV 记录生效映射；回放记录布局 + 退出恢复
# ---------------------------------------------------------------------------
class TestCsvMapAndReplayRestore(_MainWindowTestBase):
    def _write_csv_with_map(self, directory: str, channel_map) -> str:
        rec = Recorder()
        params = {"frequency": 1000, "freq_unit": "kHz", "gain": "1",
                  "coil": "c", "plate": "", "note": ""}
        profiles = build_default_profiles()
        path = rec.start_recording(
            params, directory, profiles["ads1115_4v096_div2"],
            session_meta={"source": "serial", "session_id": "map-test"},
        )
        proc = _new_processor()
        df = make_d(scan=1, mask=0xFFFF,
                    samples=tuple(1000.0 + i for i in range(16)))
        proc.ingest(df)
        ok, err = rec.write_dframe(
            df, proc, invert_sign=False,
            enabled_channels=list(range(16)),
            channel_map=channel_map,
        )
        self.assertTrue(ok, err)
        rec.stop_recording()
        return path

    def test_new_csv_saves_effective_map_and_replay_restores(self):
        win = self._new_window()
        # 新 CSV 写入当前生效映射（硬件默认），测量列顺序不变
        csv_hw = self._write_csv_with_map(self._tmp, HARDWARE_CHANNEL_MAP)
        with open(csv_hw, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        header = rows[0]
        self.assertEqual(
            parse_compact_map(rows[1][header.index("channel_map")]),
            HARDWARE_CHANNEL_MAP,
        )
        ch_cols = [header.index(f"ch{i:02d}_wire_mv") for i in range(1, 17)]
        self.assertEqual(ch_cols, sorted(ch_cols), "测量列顺序必须不变")

        # 回放带自定义记录映射的 CSV：按记录时布局显示并【明确标注】
        custom = {ch: (ch % 4, ch // 4) for ch in range(16)}
        csv_custom = self._write_csv_with_map(self._tmp, custom)
        win._set_app_mode("replay")
        win._replay.load(csv_custom)
        self.assertEqual(win._replay.recorded_channel_map, custom)
        win._apply_replay_recorded_map()
        self.assertTrue(win._replay_using_recorded_map)
        self.assertEqual(win._heatmap.channel_map(), custom)
        # 标注必须真实可见且说明"仅位置不同/退出后恢复"
        self.assertFalse(win._message_bar.isHidden())
        self.assertIn("记录时布局", win._message_bar._lbl_text.text())
        self.assertIn("退出回放后恢复", win._message_bar._lbl_text.text())
        # 回放临时映射绝不写入实时配置：即使随后保存配置，
        # 落盘的仍是当前硬件布局而非回放临时映射
        self.assertEqual(win._config.channel_map, HARDWARE_CHANNEL_MAP)
        ok, err = win._config.save()
        self.assertTrue(ok, err)
        with open(win._config.path, encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(
            normalize_channel_map(on_disk.get("channel_map")),
            HARDWARE_CHANNEL_MAP)

        # 记录布局与当前布局一致的文件：无需临时切换，且旧的
        # "记录时布局"标注不得残留（布局已回到当前硬件布局）
        win._replay.load(csv_hw)
        win._apply_replay_recorded_map()
        self.assertFalse(win._replay_using_recorded_map)
        self.assertEqual(win._heatmap.channel_map(), HARDWARE_CHANNEL_MAP)
        self.assertTrue(win._message_bar.isHidden())

        # 再切回自定义文件，然后退出回放：恢复当前硬件布局，
        # 且"按记录时布局显示"标注必须撤下（回到实时模式后
        # 该现在时断言已为假，不得残留）
        win._replay.load(csv_custom)
        win._apply_replay_recorded_map()
        self.assertTrue(win._replay_using_recorded_map)
        self.assertFalse(win._message_bar.isHidden())
        win._set_app_mode("live")
        self.assertFalse(win._replay_using_recorded_map)
        self.assertEqual(win._heatmap.channel_map(), HARDWARE_CHANNEL_MAP)
        self.assertTrue(win._message_bar.isHidden())
        self.assertEqual(win._config.channel_map, HARDWARE_CHANNEL_MAP)

        # live→replay 往返（文件仍加载）：重新进入回放时必须恢复
        # 该文件的"记录时布局"显示并重新标注，而不是静默按当前
        # 接线布局显示历史数据
        win._set_app_mode("replay")
        self.assertTrue(win._replay_using_recorded_map)
        self.assertEqual(win._heatmap.channel_map(), custom)
        self.assertFalse(win._message_bar.isHidden())
        self.assertIn("记录时布局", win._message_bar._lbl_text.text())
        win._set_app_mode("live")
        self.assertFalse(win._replay_using_recorded_map)
        self._safe_close(win)

    def test_settings_applied_during_replay_keeps_recorded_layout(self):
        """回放"记录时布局"显示中应用设置（如仅改 EMA alpha）：
        布局显示不得被静默切回硬件布局，标志位与消息栏标注必须
        保持与显示一致。"""
        from PyQt5.QtWidgets import QDialog

        custom = {ch: (ch % 4, ch // 4) for ch in range(16)}
        csv_custom = self._write_csv_with_map(self._tmp, custom)

        win = self._new_window()
        win._set_app_mode("replay")
        win._replay.load(csv_custom)
        win._apply_replay_recorded_map()
        self.assertTrue(win._replay_using_recorded_map)
        self.assertEqual(win._heatmap.channel_map(), custom)

        # 伪设置对话框：接受并仅修改 ema_alpha（其余保持当前值）
        cfg = win._config

        class _FakeDialog:
            Accepted = QDialog.Accepted

            def __init__(self, **kwargs):
                pass

            def exec_(self):
                return QDialog.Accepted

            @property
            def result_values(self):
                return {
                    "profile_name": cfg.adc_profile.name,
                    "enabled_channels": list(cfg.enabled_channels),
                    "ema_alpha": 0.5 if cfg.ema_alpha != 0.5 else 0.6,
                    "median_window": cfg.median_window,
                    "heatmap_min": cfg.heatmap_min,
                    "heatmap_max": cfg.heatmap_max,
                    "heatmap_auto": cfg.heatmap_auto,
                    "curve_window_s": cfg.curve_window_s,
                    "invert_sign": cfg.invert_sign,
                    "experiment_params": dict(cfg.experiment_params),
                    "restore_hw_layout": False,
                }

        with patch("ui.main_window.SettingsDialog", _FakeDialog):
            win._on_open_settings()

        # 设置确实被应用（ema_alpha 已变）……
        self.assertIn(win._config.ema_alpha, (0.5, 0.6))
        # ……但"记录时布局"显示/标志/标注三者必须保持一致
        self.assertTrue(win._replay_using_recorded_map)
        self.assertEqual(win._heatmap.channel_map(), custom)
        self.assertFalse(win._message_bar.isHidden())
        self.assertIn("记录时布局", win._message_bar._lbl_text.text())
        # 实时配置映射从未被回放临时映射污染
        self.assertEqual(win._config.channel_map, HARDWARE_CHANNEL_MAP)

        win._set_app_mode("live")
        self.assertEqual(win._heatmap.channel_map(), HARDWARE_CHANNEL_MAP)
        self._safe_close(win)

    def test_csv_without_map_uses_current_layout(self):
        QApplication.instance()
        # 无 channel_map 列的历史文件：明确按当前布局显示，不假称恢复
        path = os.path.join(self._tmp, "nomap.csv")
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "schema_version", "相对时间/s", "帧号", "状态",
                "valid_mask", "i2c_err", "ch01_delta_signed_mv",
            ])
            w.writerow(["4", "0.000", "1", "0", "1", "0", "12.5"])
        rep = ReplaySource()
        rep.load(path)
        self.assertIsNone(rep.recorded_channel_map)

        win = self._new_window()
        before = win._heatmap.channel_map()
        win._set_app_mode("replay")
        win._replay.load(path)
        win._apply_replay_recorded_map()
        self.assertFalse(win._replay_using_recorded_map)
        self.assertEqual(win._heatmap.channel_map(), before)
        # 必须明确告知"按当前布局显示、未恢复历史物理位置"，
        # 不得无提示静默显示
        self.assertFalse(win._message_bar.isHidden())
        self.assertIn("不含有效的通道映射信息",
                      win._message_bar._lbl_text.text())
        self.assertIn("未恢复历史物理位置",
                      win._message_bar._lbl_text.text())
        win._set_app_mode("live")
        self._safe_close(win)


if __name__ == "__main__":
    unittest.main()
