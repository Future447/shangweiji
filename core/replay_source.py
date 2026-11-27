"""
core/replay_source.py — CSV 回放数据源（只读，schema_version 3/4，mV）。

设计要点：
- 接受 schema_version=3 与 4 的 CSV（全部测量列为 mV）：
  - v3：无衰减量列 → attenuation/drop_pct 一律 None（未知，不默认为 0）
  - v4：直接读取已保存的 attenuation/drop_pct；空单元格 → None（未知）
  - schema_version=2（旧 raw count 单位）明确拒绝并给出原因
- 回放直接使用已保存的派生值：不重新计算、不做任何符号翻转
  （回放不得再次反转已保存的衰减值）
- 复用既有的 filtered/baseline/delta_signed/status
- 暴露 play/pause/stop/seek/set_speed 信号
- 严格与 serial/demo 互斥（由 MainWindow 负责切换）
- 不修改任何记录文件；回放也不得把历史基线保存成当前硬件基线
"""

from __future__ import annotations

import csv
import math
import os
import statistics
import time
from dataclasses import dataclass
from typing import List, Optional

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from core.channel_layout import parse_compact_map


# 接受 schema_version 3（无衰减列）与 4（含衰减列）；v2 拒绝
SUPPORTED_SCHEMA_VERSIONS = (3, 4)
SUPPORTED_SCHEMA_VERSION = max(SUPPORTED_SCHEMA_VERSIONS)  # 兼容旧引用

_INSPECTION_CONDITION_COLUMNS = {
    "frequency": "激励频率", "freq_unit": "频率单位", "gain": "增益",
    "coil": "线圈", "plate": "铁板",
}


@dataclass
class ReplayRow:
    """从 CSV 解析出的一行（全部测量值为 mV）。

    attenuation / drop_pct 为 Optional：None 表示该文件/该帧无此量
    （v3 文件、当前帧无效、基线不满足百分比条件），UI 显示 "—"。
    """
    t_rel: float            # 相对时间 (s)
    scan_number: int
    status: int
    valid_mask: int
    i2c_err: int
    raw_valid: List[int]    # 0/1
    # 空单元格一律解析为 None（未知），绝不默认为 0.0——
    # "无数据显示 —，不显示看似真实的 0"
    wire_mv: List[Optional[float]]    # 本帧采样（mV）
    filtered: List[Optional[float]]   # 滤波（mV）
    baseline: List[Optional[float]]   # 基线（mV）
    delta: List[Optional[float]]      # 旧语义 delta = filtered - baseline
    delta_signed: List[Optional[float]]
    percent_signed: List[Optional[float]]
    attenuation: List[Optional[float]]   # baseline - filtered（mV，可 None）
    drop_pct: List[Optional[float]]      # 检波电压下降比例（%，可 None）
    ch_status: List[int]    # ChannelStatus int
    # 仅来自明确的 position_mm 列；无记录或非有限值为 None，不推算位置。
    position_mm: Optional[float] = None


class CsvSchemaError(Exception):
    """CSV 不符合可回放的 schema（v3/v4，mV）要求。"""


class ReplaySource(QObject):
    """CSV 回放控制器。"""

    frame_ready = pyqtSignal(object)
    state_changed = pyqtSignal(str)  # "playing" / "paused" / "stopped" / "ended"
    error_occurred = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: List[ReplayRow] = []
        self._index: int = 0
        self._timer: Optional[QTimer] = None
        self._state: str = "stopped"
        self._speed: float = 1.0
        self._start_monotonic: float = 0.0
        self._t_rel_at_start: float = 0.0
        self._csv_path: str = ""
        self._schema_version: int = -1
        self._n_channels: int = 16
        #: 加载时被修复的非有限 t_rel 行数（诊断用；0 = 文件时间轴完好）
        self.t_repaired_count: int = 0
        #: 文件记录时的通道物理映射（{ch: (row, col)}）；
        #: 文件未记录或记录非法时为 None——调用方不得假定一定存在
        self._recorded_channel_map = None
        #: 文件内出现的布局种类数（>1 → 该 CSV 混用了多种布局）
        self._layout_variant_count: int = 0
        #: 记录时的 ADC Profile（首个非空行）；缺失/未知 → ""
        self._recorded_profile: str = ""
        #: 文件内 ADC Profile 是否不一致（>1 种）
        self._profile_conflict: bool = False
        self._recorded_sources: set[str] = set()
        self._inspection_conditions: dict = {}
        self._inspection_conditions_conflict: bool = False

    # ------------------------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def csv_path(self) -> str:
        return self._csv_path

    @property
    def schema_version(self) -> int:
        return self._schema_version

    @property
    def n_channels(self) -> int:
        return self._n_channels

    @property
    def recorded_channel_map(self):
        """记录时的通道物理映射 {ch: (row, col)}；无有效记录 → None。"""
        return self._recorded_channel_map

    @property
    def layout_variant_count(self) -> int:
        """文件内出现的不同 channel_map 种类数（1 = 布局一致）。"""
        return self._layout_variant_count

    @property
    def layout_conflict(self) -> bool:
        """文件内混用了多种通道布局（录制中被改布局的旧文件）。"""
        return self._layout_variant_count > 1

    @property
    def recorded_profile_name(self) -> str:
        """记录时的 ADC Profile 名（首个非空值）；缺失 → 空串。"""
        return self._recorded_profile

    @property
    def profile_conflict(self) -> bool:
        """文件内 ADC Profile 不一致（一台机器的不同实验混写）。"""
        return self._profile_conflict

    @property
    def recorded_source_scope(self) -> str:
        return next(iter(self._recorded_sources)) if len(self._recorded_sources) == 1 else ""

    @property
    def source_conflict(self) -> bool:
        return len(self._recorded_sources) > 1

    @property
    def total_rows(self) -> int:
        return len(self._rows)

    @property
    def inspection_rows(self) -> tuple[ReplayRow, ...]:
        """已解析行的元组快照；供离线检测读取，不影响回放进度。"""
        return tuple(self._rows)

    @property
    def inspection_conditions(self) -> dict:
        """文件明确记录的检测条件副本；缺失/非法值不推断、不补默认值。"""
        return dict(self._inspection_conditions)

    @property
    def inspection_conditions_conflict(self) -> bool:
        """文件内至少一个检测条件出现两个不同的非空有效值。"""
        return self._inspection_conditions_conflict

    @property
    def current_index(self) -> int:
        return self._index

    @property
    def progress(self) -> float:
        """0..1。"""
        if not self._rows:
            return 0.0
        return min(1.0, self._index / max(1, len(self._rows)))

    # ------------------------------------------------------------------
    def load(self, csv_path: str) -> None:
        """读取 CSV（schema_version 3 或 4，mV）。失败抛 CsvSchemaError。

        加载即重置播放状态：停止定时器、回到停止态、时钟与索引归零——
        播放中加载新文件不得沿用旧时间轴位置起播或直接跳结束。
        """
        if not os.path.isfile(csv_path):
            raise CsvSchemaError(f"文件不存在: {csv_path}")
        was_active = self._state in ("playing", "paused", "ended")
        self._stop_timer()
        self._state = "stopped"
        self._start_monotonic = 0.0
        self._t_rel_at_start = 0.0
        self._rows.clear()
        self._index = 0
        self._schema_version = -1
        self.t_repaired_count = 0
        self._recorded_channel_map = None
        self._layout_variant_count = 0
        self._recorded_profile = ""
        self._profile_conflict = False
        self._recorded_sources.clear()
        self._inspection_conditions.clear()
        self._inspection_conditions_conflict = False
        self._csv_path = csv_path
        if was_active:
            self.state_changed.emit("stopped")
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if header is None:
                raise CsvSchemaError("CSV 为空")
            col_idx = {name: i for i, name in enumerate(header)}
            # 位置是可选的显式标注，大小写/首尾空格不敏感；不接受时间、
            # 帧号或其他单位的列作为位置，也不改变旧测量列的解析规则。
            position_columns = {
                "position_mm": i for i, name in enumerate(header)
                if name.strip().lower() == "position_mm"
            }
            sv_col = col_idx.get("schema_version")
            if sv_col is None:
                raise CsvSchemaError("缺少 schema_version 列")
            required = [
                "相对时间/s", "帧号", "状态", "valid_mask", "i2c_err",
            ]
            for k in required:
                if k not in col_idx:
                    raise CsvSchemaError(f"缺少必填列: {k}")
            n_ch = 16
            while n_ch > 0:
                if f"ch{n_ch:02d}_delta_signed_mv" in col_idx:
                    break
                n_ch -= 1
            if n_ch <= 0:
                raise CsvSchemaError(
                    "未找到任何 ch##_delta_signed_mv 列"
                    "（v3/v4 文件应包含 mV 列）"
                )
            self._n_channels = n_ch

            # t_rel 修复策略（缺失/非有限时间的显式处理）：
            # nan/inf 无法参与调度比较（nxt.t_rel <= now_rel 恒 False），
            # 会把回放永久冻结在坏行之前。修复 = 上一有效时间 + 最近
            # 有效行距的中位数（首行坏 → 0.0），保证时间轴单调可推进；
            # 修复行数计入 t_repaired_count 供诊断。文件内时间倒退的
            # 行保持原值（调度上立即到期；曲线由 PlotWidget 检测倒退
            # 并清窗重建，绝不倒序连线）。
            last_good_t: Optional[float] = None
            recent_dts: List[float] = []

            cm_col = col_idx.get("channel_map")
            prof_col = col_idx.get("ADC_Profile")
            source_col = col_idx.get("source")
            layout_variants = set()

            for row in reader:
                if not row or len(row) < len(header):
                    continue
                # 第一个数据行：先读 schema_version（recorder 的输出一致），
                # 该行本身仍是有效数据，继续参与解析（不丢弃首帧）。
                if self._schema_version == -1:
                    try:
                        self._schema_version = int(float(row[sv_col]))
                    except (ValueError, IndexError) as e:
                        raise CsvSchemaError(
                            f"无法解析 schema_version: {e}"
                        )
                    # 记录时的通道物理映射（recorder 每行写入同一紧凑
                    # 字符串，读首行即可）。列缺失或内容非法 → None，
                    # 调用方按"无记录映射"处理，不得假定一定存在。
                    if cm_col is not None and cm_col < len(row):
                        self._recorded_channel_map = parse_compact_map(
                            row[cm_col]
                        )
                    if self._schema_version not in SUPPORTED_SCHEMA_VERSIONS:
                        if self._schema_version == 2:
                            raise CsvSchemaError(
                                "schema_version=2 为旧 raw count 单位，"
                                "与当前 mV 协议不兼容；"
                                "请用 v3 上位机重新记录后再回放"
                            )
                        raise CsvSchemaError(
                            f"不支持的 schema_version="
                            f"{self._schema_version}，"
                            f"回放仅支持 v3/v4（mV）"
                        )
                # 逐行收集"记录时的实验条件"：布局与 ADC Profile。
                # 同一文件内出现多种取值 → 该文件混写了不同实验，
                # 必须提示用户（回放只按首行布局显示，数据列不变）。
                if cm_col is not None and cm_col < len(row):
                    cm_text = (row[cm_col] or "").strip()
                    if cm_text:
                        layout_variants.add(cm_text)
                if prof_col is not None and prof_col < len(row):
                    pname = (row[prof_col] or "").strip()
                    if pname:
                        if not self._recorded_profile:
                            self._recorded_profile = pname
                        elif pname != self._recorded_profile:
                            self._profile_conflict = True

                if source_col is not None:
                    source = row[source_col].strip().lower()
                    if source in ("serial", "tcp", "demo"):
                        self._recorded_sources.add(source)

                for name, column in _INSPECTION_CONDITION_COLUMNS.items():
                    index = col_idx.get(column)
                    if index is None:
                        continue
                    value = self._normalize_inspection_condition(name, row[index])
                    if value is None:
                        continue
                    if name not in self._inspection_conditions:
                        self._inspection_conditions[name] = value
                    elif self._inspection_conditions[name] != value:
                        self._inspection_conditions_conflict = True

                sv = self._schema_version
                try:
                    t_rel = float(row[col_idx["相对时间/s"]])
                    scan = int(float(row[col_idx["帧号"]]))
                    status = int(float(row[col_idx["状态"]]))
                    valid_mask = int(float(row[col_idx["valid_mask"]]))
                    i2c_err = int(float(row[col_idx["i2c_err"]]))
                except (ValueError, IndexError):
                    continue
                if not math.isfinite(t_rel):
                    # 非有限时间戳：上一有效时间 + 中位行距（首行 → 0.0）
                    if last_good_t is None:
                        t_rel = 0.0
                    else:
                        dt = (
                            statistics.median(recent_dts)
                            if recent_dts else 1.0
                        )
                        t_rel = last_good_t + dt
                    self.t_repaired_count += 1
                    # 修复值同样推进时间轴（连续坏行不会停在同一时刻）
                    last_good_t = t_rel
                else:
                    if last_good_t is not None:
                        d = t_rel - last_good_t
                        if d > 0:
                            recent_dts.append(d)
                            if len(recent_dts) > 20:
                                recent_dts.pop(0)
                    last_good_t = t_rel
                wire_mv: List[Optional[float]] = []
                raw_valid: List[int] = []
                filtered: List[Optional[float]] = []
                baseline: List[Optional[float]] = []
                delta: List[Optional[float]] = []
                delta_signed: List[Optional[float]] = []
                percent_signed: List[Optional[float]] = []
                attenuation: List[Optional[float]] = []
                drop_pct: List[Optional[float]] = []
                ch_status: List[int] = []
                for c in range(1, n_ch + 1):
                    wire_mv.append(
                        self._opt_float(row, col_idx, f"ch{c:02d}_wire_mv")
                    )
                    raw_valid.append(
                        self._int_or(row, col_idx, f"ch{c:02d}_raw_valid", 0)
                    )
                    filtered.append(
                        self._opt_float(row, col_idx, f"ch{c:02d}_filtered_mv")
                    )
                    baseline.append(
                        self._opt_float(row, col_idx, f"ch{c:02d}_baseline_mv")
                    )
                    delta.append(
                        self._opt_float(row, col_idx, f"ch{c:02d}_delta_mv")
                    )
                    delta_signed.append(
                        self._opt_float(
                            row, col_idx, f"ch{c:02d}_delta_signed_mv"
                        )
                    )
                    percent_signed.append(
                        self._opt_float(
                            row, col_idx, f"ch{c:02d}_percent_signed"
                        )
                    )
                    # 衰减量/下降比例：v4 直接读保存值（空=未知 None）；
                    # v3 无此列 → None（未知）。绝不重算、绝不翻转。
                    if sv >= 4:
                        attenuation.append(
                            self._opt_float(row, col_idx,
                                            f"ch{c:02d}_attenuation_mv")
                        )
                        drop_pct.append(
                            self._opt_float(row, col_idx,
                                            f"ch{c:02d}_detector_drop_pct")
                        )
                    else:
                        attenuation.append(None)
                        drop_pct.append(None)
                    ch_status.append(
                        self._int_or(row, col_idx, f"ch{c:02d}_status", 0)
                    )
                self._rows.append(ReplayRow(
                    t_rel=t_rel, scan_number=scan, status=status,
                    valid_mask=valid_mask, i2c_err=i2c_err,
                    wire_mv=wire_mv, raw_valid=raw_valid,
                    filtered=filtered, baseline=baseline, delta=delta,
                    delta_signed=delta_signed,
                    percent_signed=percent_signed,
                    attenuation=attenuation, drop_pct=drop_pct,
                    ch_status=ch_status,
                    position_mm=self._finite_position(row, position_columns),
                ))
            # 布局一致性：同一 CSV 只应有一种 channel_map
            self._layout_variant_count = len(layout_variants)

        if not self._rows:
            raise CsvSchemaError("CSV 中没有有效行")

    @staticmethod
    def _normalize_inspection_condition(name, text):
        value = text.strip()
        if not value:
            return None
        if name == "frequency":
            try:
                number = float(value)
            except ValueError:
                return None
            return number if math.isfinite(number) and number >= 0 else None
        if name == "freq_unit":
            return {"hz": "Hz", "khz": "kHz", "mhz": "MHz", "ghz": "GHz"}.get(value.lower(), value)
        return value

    @staticmethod
    def _finite_position(row, position_columns) -> Optional[float]:
        value = ReplaySource._opt_float(row, position_columns, "position_mm")
        return value if value is not None and math.isfinite(value) else None

    @staticmethod
    def _int_or(row, col_idx, name, default):
        i = col_idx.get(name)
        if i is None or i >= len(row):
            return default
        try:
            return int(float(row[i]))
        except (ValueError, IndexError):
            return default

    @staticmethod
    def _opt_float(row, col_idx, name) -> Optional[float]:
        """可选浮点：列缺失或单元格为空 → None（未知，不默认为 0）。"""
        i = col_idx.get(name)
        if i is None or i >= len(row):
            return None
        s = row[i]
        if s is None or str(s).strip() == "":
            return None
        try:
            return float(s)
        except (ValueError, IndexError):
            return None

    # ------------------------------------------------------------------
    def play(self, from_index: Optional[int] = None) -> None:
        if not self._rows:
            self.error_occurred.emit("尚未加载 CSV")
            return
        if from_index is not None:
            self._index = max(0, min(from_index, len(self._rows) - 1))
        self._state = "playing"
        self._start_monotonic = time.monotonic()
        self._t_rel_at_start = self._rows[self._index].t_rel
        self.state_changed.emit("playing")
        self._start_timer()

    def pause(self) -> None:
        if self._state != "playing":
            return
        self._state = "paused"
        self._stop_timer()
        self.state_changed.emit("paused")

    def stop(self) -> None:
        self._state = "stopped"
        self._index = 0
        self._stop_timer()
        self.state_changed.emit("stopped")

    def seek(self, index: int) -> None:
        if not self._rows:
            return
        self._index = max(0, min(index, len(self._rows) - 1))
        if self._state == "playing":
            self._start_monotonic = time.monotonic()
            self._t_rel_at_start = self._rows[self._index].t_rel

    def set_speed(self, speed: float) -> None:
        if speed <= 0:
            return
        self._speed = float(speed)
        if self._state == "playing":
            self._start_monotonic = time.monotonic()
            self._t_rel_at_start = (
                self._rows[self._index].t_rel
                if self._index < len(self._rows) else 0.0
            )

    def is_running(self) -> bool:
        return self._state == "playing"

    # ------------------------------------------------------------------
    def _start_timer(self) -> None:
        self._stop_timer()
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()

    def _stop_timer(self) -> None:
        if self._timer is not None:
            try:
                self._timer.stop()
            except Exception:
                pass
            self._timer = None

    def _on_tick(self) -> None:
        """按文件时间轴发行：每行恰好发一次，不多发不漏发。

        - 索引未前进时一行也不发：重复发同一行会让曲线按墙钟
          追加重合点，低速播放时点预算（_max_points）被白耗，
          可视时间窗被压缩（X 轴是文件时间，点数却按墙钟消耗）
        - 多行同时到期（高速播放/密行数据）逐行全发：被 while
          跨过的中间行若只发最后一行，永远进不了曲线/热图
        - 每行的 X 坐标由 PlotWidget 取 row.t_rel，播放速度只
          影响调度节奏，不影响 X 轴
        """
        if self._state != "playing":
            return
        if self._index >= len(self._rows):
            self._state = "ended"
            self._stop_timer()
            self.state_changed.emit("ended")
            return
        now_rel = self._t_rel_at_start + (
            (time.monotonic() - self._start_monotonic) * self._speed
        )
        while self._index < len(self._rows):
            row = self._rows[self._index]
            if row.t_rel > now_rel:
                break
            self.frame_ready.emit(row)
            self._index += 1
        if self._index >= len(self._rows):
            self._state = "ended"
            self._stop_timer()
            self.state_changed.emit("ended")
