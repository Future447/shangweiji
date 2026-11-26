"""
test_replay_source.py — 验证 CSV 回放源（v3，mV）。

覆盖：
- 解析 schema_version=3 的 CSV（全部测量列为 mV）
- mV 精度：424.625 不被截断为 int
- 拒绝 schema_version=2（旧 raw count）并给出明确原因
- 拒绝其他 schema_version
- 播放/暂停/停止/进度/倍速/seek
- 与 Recorder v3 输出的端到端兼容（记录→回放）
"""

from __future__ import annotations

import csv
import os
import sys
import tempfile

import pytest

# 确保项目根目录在 sys.path
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QCoreApplication
from PyQt5.QtWidgets import QApplication


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance()
    if a is None:
        a = QApplication([])
    return a


def _build_columns_v3(n_ch: int = 16):
    cols = [
        "schema_version", "app_version",
        "相对时间/s", "时间戳", "帧号", "状态", "valid_mask", "i2c_err",
        "source", "session_id", "measurement_unit",
    ]
    for c in range(1, n_ch + 1):
        cols.extend([
            f"ch{c:02d}_wire_mv",
            f"ch{c:02d}_raw_valid",
            f"ch{c:02d}_filtered_mv",
            f"ch{c:02d}_baseline_mv",
            f"ch{c:02d}_delta_mv",
            f"ch{c:02d}_delta_signed_mv",
            f"ch{c:02d}_percent_signed",
            f"ch{c:02d}_status",
        ])
    cols.extend([
        "ADC_Profile", "vref_mv", "invert_sign",
        "enabled_channels", "channel_map",
        "实验备注", "激励频率", "频率单位", "增益", "线圈", "铁板",
    ])
    return cols


def _build_columns_v2(n_ch: int = 16):
    """旧 v2 raw count 列结构（用于拒绝测试）。"""
    cols = [
        "schema_version", "app_version",
        "相对时间/s", "时间戳", "帧号", "状态", "valid_mask", "i2c_err",
        "source", "session_id",
    ]
    for c in range(1, n_ch + 1):
        cols.extend([
            f"ch{c:02d}_wire_raw",
            f"ch{c:02d}_raw_valid",
            f"ch{c:02d}_filtered",
            f"ch{c:02d}_baseline",
            f"ch{c:02d}_delta",
            f"ch{c:02d}_delta_signed",
            f"ch{c:02d}_percent_signed",
            f"ch{c:02d}_status",
        ])
    cols.extend([
        "ADC_Profile", "vref_mv", "invert_sign",
        "enabled_channels", "channel_map",
        "实验备注", "激励频率", "频率单位", "增益", "线圈", "铁板",
    ])
    return cols


def _write_csv_v3(path: str, n_rows: int = 5, n_ch: int = 16,
                  ch1_wire: float = 424.625) -> None:
    cols = _build_columns_v3(n_ch)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for i in range(n_rows):
            row = [3, "3.0.0"]
            row += [f"{i * 0.29:.3f}", f"2026-09-01T00:00:{i:02d}Z",
                    i, 0, 0xFFFF, 0, "serial", f"sess{i}", "mV"]
            for c in range(1, n_ch + 1):
                wire = ch1_wire if c == 1 else 100.0 + c * 0.125
                row += [
                    f"{wire:.3f}", 1,
                    f"{wire:.3f}", f"{wire - 0.125:.3f}",
                    "0.125", "0.125", "0.029", 5,
                ]
            row += ["ads1115_4v096_div2", 4096, 0, "0-15", "",
                    "备注", "100", "kHz", "1", "标准", "无"]
            w.writerow(row)


def _write_csv_v2(path: str, n_rows: int = 3) -> None:
    cols = _build_columns_v2(16)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for i in range(n_rows):
            row = [2, "2.0.0"]
            row += [f"{i * 0.5:.3f}", "2026-01-01T00:00:00Z",
                    i, 0, 0xFFFF, 0, "demo", "sess"]
            for c in range(1, 17):
                row += [c * 100, 1, float(c * 100),
                        float(c * 100), 0.0, 0.0, 0.0, 5]
            row += ["ads1115_4v096_div2", 3300, 0, "", "",
                    "备注", "100", "kHz", "1", "标准", "无"]
            w.writerow(row)


def test_replay_load_v3(app):
    from core.replay_source import ReplaySource
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "ok.csv")
        _write_csv_v3(path, n_rows=9)
        r = ReplaySource()
        r.load(path)
        assert r.schema_version == 3
        # 首数据行既用于 schema 检测也作为数据（不再丢弃首帧）
        assert r.total_rows == 9
        assert r.n_channels == 16


def test_replay_mv_precision_not_truncated(app):
    """回放必须保留 mV 精度：424.625 不能变成 424 或 425。"""
    from core.replay_source import ReplaySource
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "prec.csv")
        _write_csv_v3(path, n_rows=3, ch1_wire=424.625)
        r = ReplaySource()
        r.load(path)
        frames = []
        r.frame_ready.connect(lambda row: frames.append(row))
        r.play()
        r._on_tick()  # 同步驱动一帧（等价于定时器触发）
        r.stop()
        assert frames, "未收到回放帧"
        row = frames[0]
        assert row.wire_mv[0] == pytest.approx(424.625, abs=1e-9)
        assert row.filtered[0] == pytest.approx(424.625, abs=1e-9)
        assert row.baseline[0] == pytest.approx(424.500, abs=1e-9)
        assert row.delta[0] == pytest.approx(0.125, abs=1e-9)


def test_replay_reject_v2_with_reason(app):
    """schema_version=2 明确拒绝，原因指向单位不兼容。"""
    from core.replay_source import CsvSchemaError, ReplaySource
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "old.csv")
        _write_csv_v2(path)
        r = ReplaySource()
        with pytest.raises(CsvSchemaError) as exc:
            r.load(path)
        msg = str(exc.value)
        assert "raw" in msg or "mV" in msg


def test_replay_reject_v1(app):
    from core.replay_source import CsvSchemaError, ReplaySource
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "v1.csv")
        cols = _build_columns_v3(16)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerow([1, "1.0"] + [""] * (len(cols) - 2))
        r = ReplaySource()
        with pytest.raises(CsvSchemaError):
            r.load(path)


def test_replay_play_pause_stop(app):
    from core.replay_source import ReplaySource
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "x.csv")
        _write_csv_v3(path, n_rows=4)
        r = ReplaySource()
        r.load(path)
        r.play()
        QCoreApplication.processEvents()
        assert r.state in ("playing", "ended")
        r.pause()
        assert r.state == "paused"
        r.stop()
        assert r.state == "stopped"
        assert r.current_index == 0


def test_replay_speed(app):
    from core.replay_source import ReplaySource
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sp.csv")
        _write_csv_v3(path, n_rows=3)
        r = ReplaySource()
        r.load(path)
        r.set_speed(2.0)
        assert r.speed == 2.0
        r.set_speed(0.5)
        assert r.speed == 0.5


def test_replay_seek(app):
    from core.replay_source import ReplaySource
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sk.csv")
        _write_csv_v3(path, n_rows=10)
        r = ReplaySource()
        r.load(path)
        r.seek(5)
        assert r.current_index == 5


def test_replay_empty_file(app):
    from core.replay_source import CsvSchemaError, ReplaySource
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "empty.csv")
        with open(path, "w", encoding="utf-8") as f:
            f.write("")
        r = ReplaySource()
        with pytest.raises(CsvSchemaError):
            r.load(path)


def test_replay_missing_file(app):
    from core.replay_source import CsvSchemaError, ReplaySource
    r = ReplaySource()
    with pytest.raises(CsvSchemaError):
        r.load("/nonexistent/file.csv")


def test_replay_load_during_play_resets_state(app):
    """播放中加载新文件：必须停止定时器、回到停止态、索引归零。

    回归：旧实现播放中 load 后沿用旧时间轴位置起播/直接跳结束，
    新旧文件的帧交错发出。
    """
    from core.replay_source import ReplaySource
    with tempfile.TemporaryDirectory() as d:
        p1 = os.path.join(d, "a.csv")
        p2 = os.path.join(d, "b.csv")
        _write_csv_v3(p1, n_rows=10)
        _write_csv_v3(p2, n_rows=4)
        r = ReplaySource()
        r.load(p1)
        states = []
        r.state_changed.connect(lambda s: states.append(s))
        r.play()
        assert r.state == "playing"
        r.seek(7)
        # 播放中加载第二个文件 → 立即回到停止态
        r.load(p2)
        assert r.state == "stopped"
        assert r.current_index == 0
        assert r.total_rows == 4
        assert "stopped" in states
        # 重新起播：从索引 0 开始（不沿用旧文件位置）
        frames = []
        r.frame_ready.connect(lambda row: frames.append(row))
        r.play()
        r._on_tick()
        r.stop()
        assert frames
        assert frames[0].scan_number == 0


def test_replay_empty_cells_parse_as_none(app):
    """空测量单元格 → None（未知），绝不默认为 0.0（v3 解析路径）。"""
    from core.replay_source import ReplaySource
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "blanks.csv")
        cols = _build_columns_v3(16)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            row = [3, "3.0.0", "0.000", "2026-09-01T00:00:00Z",
                   0, 0, 0xFFFE, 0, "serial", "sess", "mV"]
            for c in range(1, 17):
                if c == 2:
                    # CH02：本帧无效 + 无滤波/基线 → 全部留空
                    row += ["", 0, "", "", "", "", "", 2]
                else:
                    wire = 100.0 + c * 0.125
                    row += [f"{wire:.3f}", 1, f"{wire:.3f}",
                            f"{wire - 0.125:.3f}", "0.125", "0.125",
                            "0.029", 5]
            row += ["ads1115_4v096_div2", 4096, 0, "0-15", "",
                    "备注", "100", "kHz", "1", "标准", "无"]
            w.writerow(row)
        r = ReplaySource()
        r.load(path)
        row0 = r._rows[0]
        assert row0.wire_mv[0] == pytest.approx(100.125)
        assert row0.wire_mv[1] is None
        assert row0.filtered[1] is None
        assert row0.baseline[1] is None
        assert row0.delta[1] is None
        assert row0.delta_signed[1] is None
        assert row0.percent_signed[1] is None


def test_recorder_v4_output_replays(app):
    """端到端：Recorder v4 记录的文件可直接被 ReplaySource 加载。"""
    from core.recorder import Recorder
    from core.replay_source import ReplaySource
    from models.adc_profile import build_default_profiles
    from models.frame import DFrame

    with tempfile.TemporaryDirectory() as d:
        rec = Recorder()
        profiles = build_default_profiles()
        params = {"frequency": 1000, "freq_unit": "kHz", "gain": "1",
                  "coil": "c", "plate": "", "note": ""}
        path = rec.start_recording(
            params, d, profiles["ads1115_4v096_div2"]
        )

        class _P:
            class _S:
                filtered_mv = 424.625
                baseline_mv = 424.500
                last_status = 5

            def get(self, ch):
                return self._S()

        for i in range(3):
            df = DFrame(
                scan_number=i, scan_status=0, valid_mask=0xFFFF,
                i2c_error_count=0,
                samples_mv=(424.625,) + (306.250,) + (0.0,) * 14,
            )
            ok, err = rec.write_dframe(df, _P())
            assert ok, err
        rec.stop_recording()

        r = ReplaySource()
        r.load(path)  # 不抛异常即兼容
        assert r.schema_version == 4
        assert r.n_channels == 16
        assert r.total_rows == 3
