"""
CSV schema v4 记录与回放测试（core/recorder.py + core/replay_source.py）。

验收点（任务书 七 / 八-4/8）：
- schema_version=4；新增 chXX_attenuation_mv / chXX_detector_drop_pct /
  attenuation_formula / baseline_source / baseline_id / baseline_device_id
- 衰减量 = 基线 − 滤波（200.000 / -50.000 如实写入，不取绝对值）
- 当前帧无效 → 派生列（衰减量/百分比/旧Δ）全部留空，不留残留值
- 基线近零 → 衰减量照写、百分比留空（未知 ≠ 0）
- 旧 v3 文件按旧语义读取：attenuation 一律 None（未知）
- 新 v4 文件回放：保存值直通，不发生符号重复翻转
- update_session_meta 只影响之后写入的行
"""

import csv
import os
import sys
import tempfile
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QCoreApplication
from PyQt5.QtWidgets import QApplication

from core.channel_processor import ChannelProcessor
from core.recorder import APP_VERSION, SCHEMA_VERSION, Recorder
from core.replay_source import ReplaySource
from models.adc_profile import build_default_profiles
from models.channel_state import ChannelStatus
from models.frame import DFrame


PARAMS = {"frequency": 1000, "freq_unit": "kHz", "gain": "1",
          "coil": "c", "plate": "", "note": ""}


def make_d(scan=1, mask=0xFFFF, samples=None) -> DFrame:
    if samples is None:
        samples = tuple(800.0 for _ in range(16))
    return DFrame(
        scan_number=scan, scan_status=0, valid_mask=mask,
        i2c_error_count=0, samples_mv=tuple(float(s) for s in samples),
    )


def _proc_with(baseline, filtered, status=ChannelStatus.OK):
    """直接设置 filtered_mv 的处理器（不经过滤波收敛）。"""
    proc = ChannelProcessor(enabled_channels=list(range(16)))
    for c in range(16):
        st = proc.get(c)
        st.baseline_mv = baseline
        st.filtered_mv = filtered
        st.sample_mv = filtered if filtered is not None else 0.0
        st.sample_count = 1
        st.last_status = status
    return proc


class CsvV4Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="csv4_")
        self.profiles = build_default_profiles()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        QCoreApplication.processEvents()

    def _start(self, rec, session_meta=None):
        return rec.start_recording(
            PARAMS, self.tmp, self.profiles["ads1115_4v096_div2"],
            session_meta=session_meta,
        )

    def _read_rows(self, path):
        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))


class TestSchemaV4Columns(CsvV4Base):
    def test_schema_version_is_4(self):
        self.assertEqual(SCHEMA_VERSION, 4)

    def test_new_columns_present(self):
        cols = Recorder().columns
        for c in range(1, 17):
            self.assertIn(f"ch{c:02d}_attenuation_mv", cols)
            self.assertIn(f"ch{c:02d}_detector_drop_pct", cols)
        for col in ("attenuation_formula", "baseline_source",
                    "baseline_id", "baseline_device_id"):
            self.assertIn(col, cols)
        # 旧列保持（语义不变）
        for c in range(1, 17):
            self.assertIn(f"ch{c:02d}_delta_mv", cols)
            self.assertIn(f"ch{c:02d}_delta_signed_mv", cols)
            self.assertIn(f"ch{c:02d}_percent_signed", cols)

    def test_row_carries_formula_and_baseline_meta(self):
        rec = Recorder()
        path = self._start(rec, session_meta={
            "source": "serial", "session_id": "s1",
            "baseline_source": "serial", "baseline_id": "bl-abc123",
            "baseline_device_id": "USB VID:PID=1234:5678 SER=ABC",
        })
        rec.write_dframe(make_d(), _proc_with(1000.0, 800.0))
        rec.stop_recording()
        row = self._read_rows(path)[0]
        self.assertEqual(row["schema_version"], "4")
        self.assertEqual(row["app_version"], APP_VERSION)
        self.assertEqual(row["measurement_unit"], "mV")
        self.assertEqual(
            row["attenuation_formula"],
            "attenuation_mv=baseline_mv-filtered_mv@v1",
        )
        self.assertEqual(row["baseline_source"], "serial")
        self.assertEqual(row["baseline_id"], "bl-abc123")
        self.assertEqual(
            row["baseline_device_id"], "USB VID:PID=1234:5678 SER=ABC"
        )


class TestAttenuationValues(CsvV4Base):
    def test_drop_200mv_20pct(self):
        """基线 1000 / 当前 800 → +200.000 mV、+20.000%。"""
        rec = Recorder()
        path = self._start(rec)
        rec.write_dframe(make_d(), _proc_with(1000.0, 800.0))
        rec.stop_recording()
        row = self._read_rows(path)[0]
        self.assertEqual(row["ch01_attenuation_mv"], "200.000")
        self.assertEqual(row["ch01_detector_drop_pct"], "20.000")
        # 旧语义 delta = filtered - baseline = -200（不变）
        self.assertEqual(row["ch01_delta_mv"], "-200.000")

    def test_rise_minus50_minus5pct(self):
        """基线 1000 / 当前 1050 → -50.000 mV、-5.000%（不取绝对值）。"""
        rec = Recorder()
        path = self._start(rec)
        rec.write_dframe(
            make_d(samples=tuple(1050.0 for _ in range(16))),
            _proc_with(1000.0, 1050.0),
        )
        rec.stop_recording()
        row = self._read_rows(path)[0]
        self.assertEqual(row["ch01_attenuation_mv"], "-50.000")
        self.assertEqual(row["ch01_detector_drop_pct"], "-5.000")

    def test_near_zero_baseline_pct_empty(self):
        """基线 0.5 mV（< 有效下限）→ 衰减量照写，百分比留空。"""
        rec = Recorder()
        path = self._start(rec)
        rec.write_dframe(make_d(), _proc_with(0.5, 800.0))
        rec.stop_recording()
        row = self._read_rows(path)[0]
        self.assertEqual(row["ch01_attenuation_mv"], "-799.500")
        self.assertEqual(row["ch01_detector_drop_pct"], "")

    def test_invalid_frame_derived_empty(self):
        """当前帧无效 → 派生列全部留空（不使用上一帧残留值）。"""
        rec = Recorder()
        path = self._start(rec)
        proc = _proc_with(1000.0, 800.0)
        # CH02 本帧无效
        df = make_d(mask=0xFFFF & ~0x2)
        rec.write_dframe(df, proc)
        rec.stop_recording()
        row = self._read_rows(path)[0]
        # 有效通道正常
        self.assertEqual(row["ch01_attenuation_mv"], "200.000")
        # 无效通道：wire 与全部派生值留空
        self.assertEqual(row["ch02_wire_mv"], "")
        self.assertEqual(row["ch02_raw_valid"], "0")
        for col in ("ch02_attenuation_mv", "ch02_detector_drop_pct",
                    "ch02_delta_mv", "ch02_delta_signed_mv",
                    "ch02_percent_signed"):
            self.assertEqual(
                row[col], "",
                f"无效帧 {col} 应留空，实际 {row[col]!r}",
            )
        # 基线作为历史标定参数仍写出（不因此标记为正常）
        self.assertEqual(row["ch02_baseline_mv"], "1000.000")

    def test_saturated_channel_derived_empty(self):
        """饱和通道：衰减量/百分比留空。"""
        rec = Recorder()
        path = self._start(rec)
        proc = _proc_with(1000.0, 800.0, status=ChannelStatus.SATURATED)
        rec.write_dframe(make_d(), proc)
        rec.stop_recording()
        row = self._read_rows(path)[0]
        self.assertEqual(row["ch01_attenuation_mv"], "")
        self.assertEqual(row["ch01_detector_drop_pct"], "")

    def test_saturated_channel_legacy_delta_empty(self):
        """饱和通道：旧 Δ 列与衰减列同一门槛，一并留空。

        回归：旧实现衰减列留空而旧 Δ 列照写——同一行里
        "衰减未知但 Δ=-200" 自相矛盾。现在面板与 CSV 共用
        legacy_delta_for_state 单一门槛。
        """
        rec = Recorder()
        path = self._start(rec)
        proc = _proc_with(1000.0, 800.0, status=ChannelStatus.SATURATED)
        rec.write_dframe(make_d(), proc)
        rec.stop_recording()
        row = self._read_rows(path)[0]
        for col in ("ch01_attenuation_mv", "ch01_detector_drop_pct",
                    "ch01_delta_mv", "ch01_delta_signed_mv",
                    "ch01_percent_signed"):
            self.assertEqual(
                row[col], "",
                f"饱和通道 {col} 应留空，实际 {row[col]!r}",
            )

    def test_update_session_meta_only_later_rows(self):
        """重新标定后 update_session_meta：只影响之后写入的行。"""
        rec = Recorder()
        path = self._start(rec, session_meta={
            "source": "serial", "session_id": "s1",
            "baseline_id": "bl-old",
        })
        rec.write_dframe(make_d(), _proc_with(1000.0, 800.0))
        rec.update_session_meta(baseline_id="bl-new")
        rec.write_dframe(make_d(scan=2), _proc_with(1000.0, 800.0))
        rec.stop_recording()
        rows = self._read_rows(path)
        self.assertEqual(rows[0]["baseline_id"], "bl-old")
        self.assertEqual(rows[1]["baseline_id"], "bl-new")


class TestReplayCompatibility(CsvV4Base):
    def _write_csv_v3(self, path, n_rows=3):
        """手写 v3 文件（无衰减列）。"""
        cols = [
            "schema_version", "app_version",
            "相对时间/s", "时间戳", "帧号", "状态", "valid_mask", "i2c_err",
            "source", "session_id", "measurement_unit",
        ]
        for c in range(1, 17):
            cols.extend([
                f"ch{c:02d}_wire_mv", f"ch{c:02d}_raw_valid",
                f"ch{c:02d}_filtered_mv", f"ch{c:02d}_baseline_mv",
                f"ch{c:02d}_delta_mv", f"ch{c:02d}_delta_signed_mv",
                f"ch{c:02d}_percent_signed", f"ch{c:02d}_status",
            ])
        cols.extend(["ADC_Profile", "vref_mv", "invert_sign",
                     "enabled_channels", "channel_map",
                     "实验备注", "激励频率", "频率单位", "增益", "线圈", "铁板"])
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for i in range(n_rows):
                row = [3, "3.0.0", f"{i * 0.29:.3f}",
                       f"2026-09-01T00:00:{i:02d}Z", i, 0, 0xFFFF, 0,
                       "serial", "sess", "mV"]
                for c in range(1, 17):
                    wire = 800.0 + c * 0.125
                    row += [f"{wire:.3f}", 1, f"{wire:.3f}", "1000.000",
                            "-199.875", "-199.875", "-19.988", 5]
                row += ["ads1115_4v096_div2", 4096, 0, "0-15", "",
                        "备注", "100", "kHz", "1", "标准", "无"]
                w.writerow(row)

    def test_v3_file_attenuation_unknown(self):
        """旧 v3 文件按旧语义读取：attenuation 一律 None（未知≠0）。"""
        path = os.path.join(self.tmp, "old_v3.csv")
        self._write_csv_v3(path, n_rows=4)
        r = ReplaySource()
        r.load(path)
        self.assertEqual(r.schema_version, 3)
        self.assertEqual(r.total_rows, 4, "首数据行不应再被丢弃")
        frames = []
        r.frame_ready.connect(lambda row: frames.append(row))
        r.play()
        r._on_tick()
        r.stop()
        self.assertTrue(frames)
        self.assertIsNone(frames[0].attenuation[0])
        self.assertIsNone(frames[0].drop_pct[0])
        # 旧列仍按旧语义读取
        self.assertAlmostEqual(frames[0].delta[0], -199.875, places=3)

    def test_v4_replay_passthrough_no_double_flip(self):
        """新 v4 文件回放：保存的衰减值直通，不发生符号重复翻转。"""
        rec = Recorder()
        path = self._start(rec)
        # 第一帧：衰减 +200；第二帧：衰减 -50（信号升高）
        rec.write_dframe(make_d(), _proc_with(1000.0, 800.0))
        rec.write_dframe(
            make_d(scan=2, samples=tuple(1050.0 for _ in range(16))),
            _proc_with(1000.0, 1050.0),
        )
        rec.stop_recording()

        r = ReplaySource()
        r.load(path)
        self.assertEqual(r.schema_version, 4)
        self.assertEqual(r.total_rows, 2)
        # 直接检查解析行（避免依赖 t_rel 计时；信号路径由其他用例覆盖）
        self.assertAlmostEqual(r._rows[0].attenuation[0], 200.0, places=3)
        self.assertAlmostEqual(r._rows[0].drop_pct[0], 20.0, places=3)
        # 负值原样回放：不翻转、不取绝对值
        self.assertAlmostEqual(r._rows[1].attenuation[0], -50.0, places=3)
        self.assertAlmostEqual(r._rows[1].drop_pct[0], -5.0, places=3)
        # 信号路径：回放帧来自解析行（对象身份）
        frames = []
        r.frame_ready.connect(lambda row: frames.append(row))
        r.play()
        r._on_tick()
        r.stop()
        self.assertTrue(frames)
        self.assertTrue(any(frames[0] is row for row in r._rows))

    def test_v4_replay_empty_cell_is_none(self):
        """v4 文件中留空的衰减单元格 → 回放为 None（未知），不为 0。"""
        rec = Recorder()
        path = self._start(rec)
        rec.write_dframe(make_d(mask=0xFFFF & ~0x2),
                         _proc_with(1000.0, 800.0))
        rec.stop_recording()
        r = ReplaySource()
        r.load(path)
        frames = []
        r.frame_ready.connect(lambda row: frames.append(row))
        r.play()
        r._on_tick()
        r.stop()
        self.assertAlmostEqual(frames[0].attenuation[0], 200.0, places=3)
        self.assertIsNone(frames[0].attenuation[1])   # CH02 无效 → 未知
        self.assertIsNone(frames[0].drop_pct[1])

    def test_v4_replay_empty_wire_filtered_baseline_is_none(self):
        """wire/filtered/baseline 空单元格 → None（未知），绝不默认为 0.0。

        回归：旧解析把空单元格落成 0.0，回放状态/曲线出现
        假的 0 mV 点（无数据显示成了看似真实的 0）。
        """
        rec = Recorder()
        path = self._start(rec)
        proc = _proc_with(1000.0, 800.0)
        # CH02：滤波/基线均无（None）→ 对应单元格留空
        proc.get(1).filtered_mv = None
        proc.get(1).baseline_mv = None
        rec.write_dframe(make_d(mask=0xFFFF & ~0x2), proc)
        rec.stop_recording()
        r = ReplaySource()
        r.load(path)
        row = r._rows[0]
        # CH01 有效：正常数值
        self.assertAlmostEqual(row.wire_mv[0], 800.0, places=3)
        self.assertAlmostEqual(row.filtered[0], 800.0, places=3)
        self.assertAlmostEqual(row.baseline[0], 1000.0, places=3)
        # CH02：wire（本帧无效）/filtered/baseline 空 → 全部 None
        self.assertIsNone(row.wire_mv[1])
        self.assertIsNone(row.filtered[1])
        self.assertIsNone(row.baseline[1])
        self.assertIsNone(row.delta[1])
        self.assertIsNone(row.delta_signed[1])
        self.assertIsNone(row.percent_signed[1])


class TestCrossViewConsistency(CsvV4Base):
    """验收 八-4：同一帧详情、热力图、tooltip、导出结果一致。"""

    def test_panel_heatmap_tooltip_csv_same_value(self):
        from ui.channel_panel import ChannelPanel
        from ui.heatmap_widget import HeatmapWidget

        proc = _proc_with(1000.0, 800.0)
        snap = proc.measurement(0, True)

        panel = ChannelPanel()
        panel.update_snapshot(0, snap)
        heatmap = HeatmapWidget()
        heatmap.update_cell(
            0, snap.attenuation_mv, snap.status, True,
            extras={
                "sample_mv": snap.sample_mv,
                "filtered_mv": snap.filtered_mv,
                "baseline_mv": snap.baseline_mv,
                "drop_pct": snap.detector_drop_pct,
                "valid": True,
            },
        )

        rec = Recorder()
        path = self._start(rec)
        rec.write_dframe(make_d(), proc)
        rec.stop_recording()
        row = self._read_rows(path)[0]

        # 面板大字 / 热力图文本 / tooltip / CSV 同为 +200 / +20
        self.assertEqual(panel._lbl_attenuation.text(), "+200.000")
        self.assertEqual(panel._lbl_percent.text(), "+20.00%")
        # 按通道查物理位置（布局由 channel_layout 统一决定，不写死坐标）
        cell = heatmap._cells[heatmap.channel_position(0)]
        self.assertEqual(cell.value_text, "+200.0")
        self.assertIn("衰减量: +200.000 mV", cell.toolTip())
        self.assertIn("下降比例: +20.00 %", cell.toolTip())
        self.assertEqual(row["ch01_attenuation_mv"], "200.000")
        self.assertEqual(row["ch01_detector_drop_pct"], "20.000")

        panel.deleteLater()
        heatmap.deleteLater()


if __name__ == "__main__":
    unittest.main()
