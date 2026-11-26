"""
实验记录模块：CSV 写入（schema v4，毫伏单位 + 衰减量列）。

v3 协议：D 帧样本即 ADS1115 输入端电压（mV，权威单位）。
- 接口 `write_dframe(dframe, processor, profile)`：
  从 DFrame 拉 wire mV，从 processor 拉滤波值/基线/状态（全部 mV）。
- valid_mask=0 的通道：测量值字段为空（不冒充本帧数据）。
- 所有毫伏数值保留 3 位小数（0.125 mV 分辨率不丢精度）。
- CSV 每帧都 flush，磁盘写失败时返回错误让 UI 停止。
- 元数据含 schema_version、app_version、measurement_unit、session_id、
  source、enabled_channels、channel_map、invert_sign、profile_name、
  vref_mv 等可追溯信息。

schema v4（相对 v3 的新增，旧列含义一律不变）：
- 每通道新增 chXX_attenuation_mv（= baseline − filtered，"相对无钢板
  基线的衰减量"，正值=信号下降）与 chXX_detector_drop_pct（检波电压
  下降比例 %，仅在基线为正且不低于有效下限时给出）。
- 新增全局列 attenuation_formula（公式版本标识）、baseline_source /
  baseline_id / baseline_device_id（基线来源与标识）。
- 衰减量/百分比由 core.measurement 统一公式计算；当前帧无效、饱和、
  断流或数据缺失时对应派生列留空（未知≠0），不使用上一帧残留值。
- 旧 delta 列保持旧语义（filtered − baseline）不变；schema 版本号
  随本次新增从 3 升到 4，回放按版本分别解析。
"""

from __future__ import annotations

import csv
import os
import re
import time
from datetime import datetime
from typing import Dict, Optional, Sequence, Tuple

from models.adc_profile import AdcProfile

from .channel_layout import format_compact_map
from .measurement import (
    ATTENUATION_FORMULA_ID,
    attenuation_for_state,
    drop_pct_for_state,
    filtered_for_state,
    legacy_delta_for_state,
)


SCHEMA_VERSION = 4  # v4: 新增 attenuation/drop_pct 列与基线来源列
APP_VERSION = "3.5.2"
MEASUREMENT_UNIT = "mV"


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = name.strip(" .")
    if not name:
        name = "unnamed"
    if len(name) > 100:
        name = name[:100]
    return name


def build_csv_filename(params: dict, save_dir: str) -> str:
    """根据实验参数生成唯一 CSV 文件名（独占创建模式）。"""
    now = datetime.now()
    date_str = now.strftime("%Y%m%d")

    freq = sanitize_filename(str(params.get("frequency", "1000")))
    unit = sanitize_filename(str(params.get("freq_unit", "kHz")))
    gain_raw = sanitize_filename(str(params.get("gain", "1")))
    gain_clean = (
        gain_raw.replace("×", "").replace("x", "").replace("X", "").strip()
    )
    if gain_clean and gain_clean.isdigit():
        gain_label = f"{gain_clean}倍"
    else:
        gain_label = gain_raw

    coil = sanitize_filename(str(params.get("coil", "默认")))
    plate = sanitize_filename(str(params.get("plate", "")))
    note = sanitize_filename(str(params.get("note", "")))[:20]

    parts = [date_str, f"{freq}{unit}", gain_label, coil]
    if plate:
        parts.append(f"铁板{plate}")
    if note:
        parts.append(note)

    base_name = "_".join(parts)

    for i in range(1, 100000):
        if i == 1:
            filename = f"{base_name}.csv"
        else:
            filename = f"{base_name}_{i}.csv"
        full_path = os.path.join(save_dir, filename)
        try:
            fd = os.open(
                full_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            )
            os.close(fd)
            return full_path
        except FileExistsError:
            continue
        except OSError:
            break

    ts = now.strftime("%H%M%S%f")[:10]
    filename = f"{base_name}_{ts}.csv"
    return os.path.join(save_dir, filename)


# 16 通道统一列名（即使未启用也保留列）
N_CHANNELS = 16


def build_columns() -> list:
    """CSV 列定义（schema v4，全部测量列为 mV）。

    关键列（按顺序）：
      - 基础元数据：相对时间, 时间戳, 帧号, 状态, valid_mask, i2c_err,
                    source, session_id, measurement_unit
      - 每通道：chXX_wire_mv, chXX_raw_valid, chXX_filtered_mv,
                chXX_baseline_mv, chXX_delta_mv, chXX_delta_signed_mv,
                chXX_percent_signed, chXX_attenuation_mv,
                chXX_detector_drop_pct, chXX_status
      - 尾部：attenuation_formula, baseline_source, baseline_id,
              baseline_device_id, ADC_Profile, vref_mv, invert_sign,
              enabled_channels, channel_map, 实验参数...
    """
    cols = [
        "schema_version", "app_version",
        "相对时间/s", "时间戳", "帧号", "状态", "valid_mask", "i2c_err",
        "source", "session_id", "measurement_unit",
    ]
    for c in range(1, N_CHANNELS + 1):
        cols.extend([
            f"ch{c:02d}_wire_mv",           # 来自当前 D 帧（mV）
            f"ch{c:02d}_raw_valid",         # bool/int 0/1
            f"ch{c:02d}_filtered_mv",       # EMA（mV）
            f"ch{c:02d}_baseline_mv",       # 无钢板基线（mV，历史标定参数）
            f"ch{c:02d}_delta_mv",          # 旧语义 filtered - baseline（mV）
            f"ch{c:02d}_delta_signed_mv",   # 旧诊断：应用 invert_sign（mV）
            f"ch{c:02d}_percent_signed",    # 旧诊断：相对百分比（带 invert）
            f"ch{c:02d}_attenuation_mv",    # baseline - filtered（mV）
            f"ch{c:02d}_detector_drop_pct",  # 检波电压下降比例（%）
            f"ch{c:02d}_status",            # ChannelStatus int
        ])
    cols.extend([
        "attenuation_formula",              # 衰减公式版本标识
        "baseline_source",                  # 基线来源（serial/demo/空）
        "baseline_id",                      # 基线标识
        "baseline_device_id",               # 基线标定时的设备标识
        "ADC_Profile", "vref_mv", "invert_sign",
        "enabled_channels", "channel_map",
        "实验备注", "激励频率", "频率单位", "增益", "线圈", "铁板",
    ])
    return cols


class Recorder:
    """实验数据 CSV 记录器（v2 阵列）。"""

    def __init__(self) -> None:
        self._file = None
        self._writer = None
        self._start_time: Optional[float] = None
        self._params_snapshot: dict = {}
        self._session_meta: dict = {}
        self._point_count: int = 0
        self._flush_interval: int = 1   # 整改：每帧 flush（低帧率下数据安全）
        self._path: str = ""
        self._error_reported: bool = False
        self._columns = build_columns()

    # ------------------------------------------------------------------
    @property
    def is_recording(self) -> bool:
        return self._file is not None

    @property
    def point_count(self) -> int:
        return self._point_count

    @property
    def file_path(self) -> str:
        return self._path

    @property
    def columns(self) -> list:
        return list(self._columns)

    # ------------------------------------------------------------------
    def start_recording(
        self,
        params: dict,
        save_dir: str,
        profile: AdcProfile,
        session_meta: Optional[dict] = None,
    ) -> str:
        """开始记录。

        session_meta 建议包含：source、session_id、baseline_source、
        baseline_id、baseline_device_id（基线来源与标识，随每行写入）。
        """
        if self._file is not None:
            raise RuntimeError("已在记录中，请先停止")

        os.makedirs(save_dir, exist_ok=True)
        self._path = build_csv_filename(params, save_dir)
        self._params_snapshot = dict(params)
        self._session_meta = dict(session_meta or {})
        self._start_time = time.monotonic()
        self._point_count = 0
        self._error_reported = False

        try:
            self._file = open(self._path, "w", encoding="utf-8-sig", newline="")
            self._writer = csv.writer(self._file)
            self._writer.writerow(self._columns)
            self._file.flush()
        except OSError as e:
            self._cleanup()
            raise OSError(f"无法创建CSV文件: {e}") from e

        self._profile_name = profile.name
        self._vref_mv = getattr(profile, "vref_mv", 4096)
        return self._path

    def update_session_meta(self, **kwargs) -> None:
        """记录中更新会话元数据（如重新标定后的 baseline_id）。

        只影响之后写入的行；已写入的行保持原样（历史真实）。
        """
        self._session_meta.update(kwargs)

    # ------------------------------------------------------------------
    def write_dframe(
        self,
        dframe,
        processor,
        invert_sign: bool = False,
        enabled_channels: Optional[Sequence[int]] = None,
        channel_map: Optional[Dict[int, Tuple[int, int]]] = None,
        baseline_quality: Optional[Dict[int, dict]] = None,
    ) -> tuple:
        """从 DFrame 直接写一行（P0-2 整改接口）。

        - 每通道 wire_mv 直接来自 dframe.samples_mv[ch]（仅 valid_mask=1 时写入）
        - valid_mask=0 时 wire_mv 留空，且本帧派生值（衰减量/百分比/旧Δ）
          一律留空——当前帧无效不输出残留值
        - 衰减量/下降比例由 core.measurement 统一公式计算（不经 invert_sign）
        - delta_signed 应用 invert_sign（旧诊断量，P0-5）
        """
        if self._file is None:
            return True, ""
        if dframe is None:
            return True, ""

        now = datetime.now()
        elapsed = time.monotonic() - self._start_time
        timestamp = (
            now.strftime("%Y-%m-%d %H:%M:%S.")
            + f"{now.microsecond // 1000:03d}"
        )
        p = self._params_snapshot
        meta = self._session_meta

        row = [
            SCHEMA_VERSION, APP_VERSION,
            f"{elapsed:.3f}", timestamp,
            dframe.scan_number, dframe.scan_status,
            dframe.valid_mask, dframe.i2c_error_count,
            meta.get("source", ""),
            meta.get("session_id", ""),
            MEASUREMENT_UNIT,
        ]
        for c in range(N_CHANNELS):
            valid = dframe.is_valid(c)
            wire_mv = dframe.samples_mv[c] if valid else None
            st = processor.get(c)
            # filtered 与衰减量同一有效性门槛（无效/饱和/预热中 → 空），
            # 不得把上一帧残留的滤波值写成本帧测量（P2 修复）
            filtered = filtered_for_state(st, valid)
            baseline = st.baseline_mv
            # 新语义（统一公式入口）：当前帧有效才产生派生值
            attenuation = attenuation_for_state(st, valid)
            drop_pct = drop_pct_for_state(st, valid)
            # raw delta（旧语义 filtered - baseline，不含 invert_sign）。
            # 与详情面板同一入口同一门槛：饱和/无效帧派生值一律留空，
            # 不得出现"衰减列留空而旧Δ列照写"的自相矛盾行
            raw_delta = legacy_delta_for_state(st, valid)
            # signed delta（旧诊断，应用 invert_sign）
            if raw_delta is not None:
                signed = -raw_delta if invert_sign else raw_delta
            else:
                signed = None
            # signed percent（旧诊断）
            if signed is not None and baseline is not None and abs(baseline) > 1e-9:
                signed_pct = signed / abs(baseline) * 100.0
            else:
                signed_pct = None
            row.extend([
                f"{wire_mv:.3f}" if wire_mv is not None else "",
                1 if valid else 0,
                f"{filtered:.3f}" if filtered is not None else "",
                f"{baseline:.3f}" if baseline is not None else "",
                f"{raw_delta:.3f}" if raw_delta is not None else "",
                f"{signed:.3f}" if signed is not None else "",
                f"{signed_pct:.3f}" if signed_pct is not None else "",
                f"{attenuation:.3f}" if attenuation is not None else "",
                f"{drop_pct:.3f}" if drop_pct is not None else "",
                int(st.last_status),
            ])

        # enabled_channels 与 channel_map 序列化为紧凑字符串
        enabled_str = (
            ",".join(str(c) for c in enabled_channels)
            if enabled_channels is not None else ""
        )
        if channel_map is not None:
            # 紧凑格式统一由 core.channel_layout 定义（回放据此解析，
            # 格式冻结不得变更）
            channel_map_str = format_compact_map(channel_map)
        else:
            channel_map_str = ""

        row.extend([
            ATTENUATION_FORMULA_ID,
            meta.get("baseline_source", ""),
            meta.get("baseline_id", ""),
            meta.get("baseline_device_id", ""),
            self._profile_name,
            self._vref_mv,
            1 if invert_sign else 0,
            enabled_str,
            channel_map_str,
            p.get("note", ""),
            p.get("frequency", ""),
            p.get("freq_unit", ""),
            p.get("gain", ""),
            p.get("coil", ""),
            p.get("plate", ""),
        ])

        if len(row) != len(self._columns):
            return False, f"列数不匹配: {len(row)} vs {len(self._columns)}"

        try:
            self._writer.writerow(row)
            self._point_count += 1
            if self._point_count % self._flush_interval == 0:
                self._file.flush()
                try:
                    os.fsync(self._file.fileno())
                except OSError:
                    # fsync 在某些 Windows 文件系统可能失败，但 flush 已成功
                    pass
        except OSError as e:
            return False, str(e)
        return True, ""

    # ------------------------------------------------------------------
    # 兼容旧接口
    # ------------------------------------------------------------------
    def write_frame(
        self,
        frame_meta: dict,
        channel_snapshots: Dict[int, dict],
    ) -> tuple:
        """
        旧接口：保留以兼容旧测试与可能的旧调用方。
        内部转发到 write_dframe 路径：构造伪 DFrame。
        """
        from models.frame import (
            DFrame, MAX_CHANNEL_COUNT, is_valid_mv_value,
        )
        # 构造 samples_mv：snapshots 给的 sample_mv/raw 视为 mV 浮点
        samples = [0.0] * MAX_CHANNEL_COUNT
        mask = 0
        for c1, snap in channel_snapshots.items():
            ch = c1 - 1
            if 0 <= ch < MAX_CHANNEL_COUNT:
                r = snap.get("sample_mv", snap.get("raw"))
                if r is None:
                    continue
                try:
                    fv = float(r)
                except (TypeError, ValueError):
                    continue
                if is_valid_mv_value(fv):
                    samples[ch] = fv
                    mask |= (1 << ch)
        # 写一个 DFrame（仅用于内部兼容；性能上无要求）
        dframe = DFrame(
            scan_number=int(frame_meta.get("scan_number", 0) or 0),
            scan_status=int(frame_meta.get("scan_status", 0) or 0),
            valid_mask=int(frame_meta.get("valid_mask", mask) or 0),
            i2c_error_count=int(frame_meta.get("i2c_err", 0) or 0),
            samples_mv=tuple(samples),
        )
        return self.write_dframe(dframe, _FakeProcessor(channel_snapshots))

    # ------------------------------------------------------------------
    def stop_recording(self) -> tuple:
        if self._file is None:
            return True, ""
        err = ""
        try:
            self._file.flush()
            try:
                os.fsync(self._file.fileno())
            except OSError:
                pass
            self._file.close()
        except OSError as e:
            err = str(e)
            try:
                self._file.close()
            except OSError:
                pass
        self._cleanup()
        return err == "", err

    def _cleanup(self) -> None:
        self._file = None
        self._writer = None
        self._start_time = None


class _FakeProcessor:
    """兼容旧 write_frame 调用方：仅提供 get(ch) 通道状态访问。"""

    class _State:
        def __init__(self, snap):
            self._snap = snap
            self.last_status = snap.get("status", 1)
            # 回放覆盖字段缺省：不进入回放语义
            self.use_replay_derived = False
            self.attenuation_override_mv = None
            self.drop_pct_override = None

        @property
        def filtered_mv(self):
            return self._snap.get("filtered_mv", self._snap.get("filtered"))

        @property
        def baseline_mv(self):
            return self._snap.get("baseline_mv", self._snap.get("baseline"))

    def __init__(self, snaps: dict):
        self._snaps = snaps

    def get(self, ch_0based: int):
        return self._State(self._snaps.get(ch_0based + 1, {}))
