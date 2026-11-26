"""
统一通道测量快照与衰减公式（"无钢板标定 → 放钢板衰减"实验语义）。

唯一权威公式入口。详情面板、热力图、tooltip、曲线读数、CSV
全部从这里取数，禁止各自另写公式。

业务量定义（全部在 ADC 电压域，单位 mV）：
- sample_mv：       本帧串口 ADC 输入电压（ADS1115 AIN0 节点）
- filtered_mv：     滤波后的 ADC 输入电压 = 当前检波电压（ADC 侧）
- baseline_mv：     无钢板时标定的 ADC 输入电压基线
- detector_vout_mv：依据分压比恢复的 AD8362 检波器输出电压估计值
- attenuation_mv = baseline_mv - filtered_mv
      > 0：相对无钢板基线下降（放入钢板的预期响应）
      ≈ 0：接近基线
      < 0：信号高于基线（如实显示负值，绝不取绝对值/截零/翻转）
- detector_drop_percent = attenuation_mv / baseline_mv * 100
      仅在基线为正且 >= MIN_VALID_BASELINE_MV 时计算；
      否则为 None（UI 显示 "—"），绝不产生 NaN/Inf/巨大伪百分比。
      该量的名称是"检波电压下降比例"，不是"峰峰值衰减率"。

旧语义 delta = filtered_mv - baseline_mv 保留为诊断量（delta_legacy_mv），
仅旧 invert_sign 作用于它；invert_sign 绝不再翻转 attenuation_mv。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from models.channel_state import ChannelState, ChannelStatus


# 衰减公式版本标识：写入 CSV（attenuation_formula 列）以便追溯。
# 改公式必须升版本并同步 CSV schema。
ATTENUATION_FORMULA_ID = "attenuation_mv=baseline_mv-filtered_mv@v1"

# 百分比有效基线下限（mV）。
# 依据：基线接近 0 时比值对噪声极度敏感（分母 ~ 噪声量级会产出
# 数百/数千 % 的伪百分比）。1.0 mV = 8 LSB，低于此值百分比无物理意义。
# 这是数值有效性下限，不是硬件合格标准。
MIN_VALID_BASELINE_MV = 1.0


def compute_attenuation_mv(
    baseline_mv: Optional[float],
    filtered_mv: Optional[float],
) -> Optional[float]:
    """attenuation = baseline - filtered。任一缺失/非有限 → None。"""
    if baseline_mv is None or filtered_mv is None:
        return None
    b = float(baseline_mv)
    f = float(filtered_mv)
    if not (math.isfinite(b) and math.isfinite(f)):
        return None
    return b - f


def compute_detector_drop_pct(
    attenuation_mv: Optional[float],
    baseline_mv: Optional[float],
) -> Optional[float]:
    """检波电压下降比例（%）。

    仅在 attenuation 有效且基线为正、>= MIN_VALID_BASELINE_MV 时计算；
    否则 None。负衰减给出负百分比（信号升高），如实保留符号。
    """
    if attenuation_mv is None or baseline_mv is None:
        return None
    b = float(baseline_mv)
    if not math.isfinite(b) or b < MIN_VALID_BASELINE_MV:
        return None
    a = float(attenuation_mv)
    if not math.isfinite(a):
        return None
    return a / b * 100.0


@dataclass
class MeasurementSnapshot:
    """单通道统一测量快照（详情/热力图/tooltip/曲线/CSV 同源）。"""

    channel: int
    status: ChannelStatus
    enabled: bool
    frame_valid: bool                       # 当前帧有效且数据新鲜
    sample_mv: Optional[float]              # 串口采样（ADS1115 输入端）
    filtered_mv: Optional[float]            # 当前检波电压（滤波，ADC mV）
    baseline_mv: Optional[float]            # 无钢板基线（历史标定参数）
    detector_vout_mv: Optional[float]       # AD8362 输出估算
    attenuation_mv: Optional[float]         # 相对基线衰减量（主测量量）
    detector_drop_pct: Optional[float]      # 检波电压下降比例（%）
    delta_legacy_mv: Optional[float]        # 旧语义 filtered-baseline（诊断）

    @property
    def has_current_measurement(self) -> bool:
        """当前帧测量是否可用。

        无效/饱和/断流/无数据/预热中为 False——预热期（滤波重建
        中）的滤波值不代表稳定物理量，不得冒充正常测量。
        """
        return (
            self.frame_valid
            and self.status not in (
                ChannelStatus.NO_DATA,
                ChannelStatus.UNKNOWN,
                ChannelStatus.INVALID,
                ChannelStatus.SATURATED,
                ChannelStatus.WARMING_UP,
            )
        )


# 衰减量允许计算的状态集合：无效/饱和/无数据的当前帧不产生
# 看似正常的衰减值（保留真实负值，但不伪造正常值）。
_ATTENUATION_OK_STATUSES = (
    ChannelStatus.OK,
    ChannelStatus.NOT_CALIBRATED,  # baseline 为 None，公式自然给 None
)


def attenuation_for_state(
    st: ChannelState,
    frame_valid: bool,
) -> Optional[float]:
    """从 ChannelState 求衰减量（含回放覆盖与当前帧有效性门槛）。"""
    # 回放语义：直接使用 CSV 保存的衰减值（可能为 None = 未知），
    # 不重算、不再翻转（回放不得二次反转已保存的衰减值）。
    # 但无效帧不得绕过有效性门槛：旧 CSV 在无效帧留下的历史残留
    # 值不得显示成当前有效值。
    # getattr 兜底：鸭子类型的伪状态（测试夹具）可能没有回放字段。
    if getattr(st, "use_replay_derived", False):
        if not frame_valid:
            return None
        return getattr(st, "attenuation_override_mv", None)
    if not frame_valid:
        return None
    if st.last_status not in _ATTENUATION_OK_STATUSES:
        return None
    return compute_attenuation_mv(st.baseline_mv, st.filtered_mv)


def drop_pct_for_state(
    st: ChannelState,
    frame_valid: bool,
) -> Optional[float]:
    """从 ChannelState 求检波电压下降比例（含回放覆盖）。"""
    if getattr(st, "use_replay_derived", False):
        if not frame_valid:
            return None
        return getattr(st, "drop_pct_override", None)
    att = attenuation_for_state(st, frame_valid)
    return compute_detector_drop_pct(att, st.baseline_mv)


# 采样值允许作为"当前值"呈现的状态集合：饱和帧给出真实 rail 值
#（由状态列/徽章明确标记为诊断）；预热帧给出真实未滤波采样。
# 无效/无数据/未知 → None（sample_mv 字段里可能是上一帧残留）。
_SAMPLE_OK_STATUSES = (
    ChannelStatus.OK,
    ChannelStatus.NOT_CALIBRATED,
    ChannelStatus.SATURATED,
    ChannelStatus.WARMING_UP,
)


def sample_for_state(
    st: ChannelState,
    frame_valid: bool,
) -> Optional[float]:
    """当前采样值（mV）：仅当前帧真实到达的值，否则 None。

    统一"当前测量有效性"规则：无效/断流/无数据帧不外发残留值。
    回放路径按文件值原样给出，但同样受 frame_valid 门槛约束。
    """
    if getattr(st, "use_replay_derived", False):
        if not frame_valid:
            return None
        v = getattr(st, "sample_mv", None)
    else:
        if not frame_valid:
            return None
        if st.last_status not in _SAMPLE_OK_STATUSES:
            return None
        # SATURATED 状态必然由真实 rail 帧设置（真实 wire 诊断值）；
        # 其余状态要求确有样本进入过滤波链（排除默认 0.0）
        if (
            getattr(st, "sample_count", 0) <= 0
            and st.last_status != ChannelStatus.SATURATED
        ):
            return None
        v = st.sample_mv
    if v is None:
        return None
    fv = float(v)
    return fv if math.isfinite(fv) else None


def filtered_for_state(
    st: ChannelState,
    frame_valid: bool,
) -> Optional[float]:
    """当前滤波值（mV）：仅当前测量有效时给出，否则 None。

    与衰减量同一有效性门槛（无效/饱和/断流/无数据/预热中 →
    None）：滤波字段里的历史残留值不得冒充当前测量——详情、
    热力图 tooltip、曲线、CSV 必须一致。回放按文件值原样给出，
    但无效帧不得绕过门槛。
    """
    if getattr(st, "use_replay_derived", False):
        if not frame_valid:
            return None
        v = st.filtered_mv
    else:
        if not frame_valid:
            return None
        if st.last_status not in _ATTENUATION_OK_STATUSES:
            return None
        v = st.filtered_mv
    if v is None:
        return None
    fv = float(v)
    return fv if math.isfinite(fv) else None


def legacy_delta_for_state(
    st: ChannelState,
    frame_valid: bool,
) -> Optional[float]:
    """旧语义诊断 Δ = filtered_mv - baseline_mv（含当前帧门槛）。

    与衰减量同一有效性门槛：当前帧无效/饱和/无数据/断流时不产出
    看似正常的诊断值（详情面板与 CSV 必须一致——同一入口）。
    该量不含 invert_sign（符号翻转由调用方按 P0-5 自行应用）。
    """
    if not frame_valid:
        return None
    if st.last_status not in _ATTENUATION_OK_STATUSES:
        return None
    # compute_attenuation_mv(b, f) = b - f；旧 Δ = filtered - baseline，
    # 参数对调即得旧语义，不引入新公式。
    return compute_attenuation_mv(st.filtered_mv, st.baseline_mv)


def build_snapshot(
    st: ChannelState,
    *,
    frame_valid: bool,
    enabled: bool = True,
    detector_divider: float = 1.0,
) -> MeasurementSnapshot:
    """由 ChannelState 构建统一快照。

    统一"当前测量有效性"规则（唯一入口）：
    - 无效/饱和/断流/无数据/预热中帧：filtered/attenuation/drop_pct/
      旧 Δ 均为 None（UI 显示 "—"、CSV 留空、曲线断点）——字段里
      的历史残留值不得冒充当前测量。
    - sample（串口采样）在饱和帧保留真实 rail 值（状态列明确标记
      饱和，作为诊断）；无效/断流帧为 None。
    - baseline 作为历史标定参数始终保留显示。
    - 回放路径按文件值原样给出，但无效帧不得绕过上述门槛（旧 CSV
      残留值不得显示成当前有效值）。
    """
    att = attenuation_for_state(st, frame_valid)
    pct = drop_pct_for_state(st, frame_valid)
    # 旧诊断量与衰减量同一有效性门槛（无效/饱和帧不产出派生值），
    # 与 CSV 写出的 chXX_delta_mv 同入口、同结果
    delta_legacy = legacy_delta_for_state(st, frame_valid)
    sample = sample_for_state(st, frame_valid)
    filtered = filtered_for_state(st, frame_valid)
    vout = (
        sample * float(detector_divider)
        if sample is not None
        else None
    )
    return MeasurementSnapshot(
        channel=st.channel,
        status=st.last_status,
        enabled=enabled,
        frame_valid=bool(frame_valid),
        sample_mv=sample,
        filtered_mv=filtered,
        baseline_mv=st.baseline_mv,
        detector_vout_mv=vout,
        attenuation_mv=att,
        detector_drop_pct=pct,
        delta_legacy_mv=delta_legacy,
    )
