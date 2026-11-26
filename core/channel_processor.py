"""
单通道处理。

职责：
- 维护 16 个 ChannelState
- v3 毫伏协议：D 帧样本即为 ADS1115 输入端电压（mV，权威单位）
- 接收 D 帧（来自 FrameParser）后，对每个通道：
  - 始终记录 wire mV（用于 CSV 真实性，无论是否启用）
  - 若启用：推入 mV -> 更新中值+EMA（全部在 mV 空间运算）
  - 若启用且 valid_mask=1：样本进入处理链
  - 若启用且 valid_mask=0：标记为 INVALID
- 标定：只对 enabled 通道进行；无效通道不计入
- 统一测量快照：measurement() 委托 core.measurement（衰减量/下降比例
  的唯一公式入口；详情、热力图、tooltip、曲线、CSV 同源）
- invert_sign 仅保留给旧诊断量 delta（filtered - baseline）；
  绝不再翻转 attenuation_mv
- 关闭"未启用则丢弃数据"——已修复 P0-2
"""

from __future__ import annotations

import math
import time
from typing import Dict, List, Optional, Tuple

from core.measurement import MeasurementSnapshot, build_snapshot
from models.adc_profile import AdcProfile, profile_by_name, build_default_profiles
from models.channel_state import ChannelState, ChannelStatus
from models.frame import (
    DFrame,
    MAX_CHANNEL_COUNT,
    MV_MAX,
    MV_MIN,
)


class ChannelProcessor:
    """16 通道状态聚合。"""

    def __init__(
        self,
        adc_profile: Optional[AdcProfile] = None,
        enabled_channels: Optional[List[int]] = None,
    ) -> None:
        self._profile = adc_profile or profile_by_name(
            "ads1115_4v096_div2",
            build_default_profiles(),
        )
        # 启用的通道（默认前 4 个）
        if enabled_channels is None:
            enabled_channels = list(range(4))
        self._enabled = list(enabled_channels)
        # 16 个 ChannelState
        self._states: Dict[int, ChannelState] = {
            i: ChannelState(channel=i) for i in range(MAX_CHANNEL_COUNT)
        }
        # 最近一帧的所有 16 通道 wire mV（包括未启用/无效通道）
        # 用于 CSV 真实性（P0-2）
        self._last_dframe: Optional[DFrame] = None
        self._last_dframe_wire_mv: List[Optional[float]] = [None] * MAX_CHANNEL_COUNT
        self._last_dframe_valid: List[bool] = [False] * MAX_CHANNEL_COUNT
        self._last_dframe_received_monotonic: float = 0.0
        self.invert_sign: bool = False
        self.ema_alpha: float = 0.2
        self.median_window: int = 3
        # 分压比覆盖（回放专用）：回放必须用"记录时的 ADC Profile"
        # 计算历史 VOUT，而不是当前实时配置。None = 用实时 Profile；
        # unknown = 记录里没有可识别的 Profile（VOUT 显示为未知）。
        self._divider_override: Optional[float] = None
        self._divider_override_unknown: bool = False

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    @property
    def adc_profile(self) -> AdcProfile:
        return self._profile

    def set_adc_profile(self, profile: AdcProfile) -> None:
        self._profile = profile

    @property
    def enabled_channels(self) -> List[int]:
        return list(self._enabled)

    def set_enabled_channels(self, channels: List[int]) -> None:
        # 自动截断到合法范围、去重、排序
        clean = sorted({c for c in channels if 0 <= c < MAX_CHANNEL_COUNT})
        self._enabled = clean

    def set_ema_alpha(self, alpha: float) -> None:
        self.ema_alpha = max(0.0, min(1.0, float(alpha)))
        for s in self._states.values():
            s.ema_alpha = self.ema_alpha

    def set_median_window(self, n: int) -> None:
        n = max(1, int(n))
        self.median_window = n
        for s in self._states.values():
            # 经 ChannelState.set_median_window：缩窗立即裁剪缓冲生效
            s.set_median_window(n)

    def set_invert_sign(self, invert: bool) -> None:
        self.invert_sign = bool(invert)

    # ------------------------------------------------------------------
    # 分压比覆盖（回放使用"记录时 Profile"）
    # ------------------------------------------------------------------
    def set_detector_divider_override(
        self, divider: Optional[float], unknown: bool = False
    ) -> None:
        """回放时用记录中的分压比计算历史 VOUT。

        divider=None 且 unknown=True 表示记录未标注可识别的 ADC
        Profile → VOUT 未知（显示"—"），绝不按当前配置臆测。
        """
        self._divider_override = (
            None if divider is None else float(divider)
        )
        self._divider_override_unknown = bool(unknown)

    def clear_detector_divider_override(self) -> None:
        """退出回放：恢复使用实时 Profile 的分压比。"""
        self._divider_override = None
        self._divider_override_unknown = False

    @property
    def effective_detector_divider(self) -> Optional[float]:
        """当前生效的分压比；None 表示"未知"（仅回放缺 Profile 时）。"""
        if self._divider_override_unknown:
            return None
        if self._divider_override is not None:
            return self._divider_override
        return float(self._profile.detector_divider)

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------
    def get(self, channel: int) -> ChannelState:
        return self._states[channel]

    def snapshot_enabled(self) -> List[Tuple[int, ChannelState]]:
        return [(c, self._states[c]) for c in self._enabled]

    def all_channels(self) -> List[ChannelState]:
        return [self._states[i] for i in range(MAX_CHANNEL_COUNT)]

    @property
    def last_dframe(self) -> Optional[DFrame]:
        return self._last_dframe

    @property
    def last_dframe_received_monotonic(self) -> float:
        return self._last_dframe_received_monotonic

    # ------------------------------------------------------------------
    # 接收 D 帧
    # ------------------------------------------------------------------
    def ingest(self, dframe: DFrame) -> None:
        """处理一个 D 帧。

        - 对所有 16 通道：记录 wire mV 与 validity（无论是否启用）
        - 仅对 enabled 通道：推入处理链（EMA、状态机，全部 mV 空间）
        - 对未启用通道：状态保持 NOT_USED（由 sample_count==0 反映）
        """
        # 始终记录本帧的 wire mV（包含未启用通道），用于 CSV 真实性
        self._last_dframe = dframe
        self._last_dframe_received_monotonic = time.monotonic()
        for ch in range(MAX_CHANNEL_COUNT):
            valid = dframe.is_valid(ch)
            self._last_dframe_valid[ch] = valid
            self._last_dframe_wire_mv[ch] = (
                dframe.samples_mv[ch] if valid else None
            )

        # 推入处理链：仅对 enabled 通道
        for ch in range(MAX_CHANNEL_COUNT):
            st = self._states[ch]
            if ch not in self._enabled:
                # 未启用：保留 NO_DATA 语义；不更新处理链
                # 但 wire mV 仍记录在 _last_dframe_wire_mv
                st.set_status(ChannelStatus.NO_DATA)
                continue
            if not dframe.is_valid(ch):
                # 标记无效；不清滤波（短暂失效保留滤波状态，
                # 长失效由 update_mv 的间隔检测在恢复时重建）
                st.set_status(ChannelStatus.INVALID)
                continue
            sample_mv = dframe.samples_mv[ch]
            # 有限性/饱和检查必须先于滤波更新：饱和 rail 值进入
            # 中值/EMA 会污染滤波链并进一步污染标定基线
            if not math.isfinite(sample_mv):
                st.set_status(ChannelStatus.INVALID)
                continue
            if sample_mv <= MV_MIN or sample_mv >= MV_MAX:
                # 饱和（毫伏量程：触碰满度上下限）：保留真实 wire 值
                # 供诊断与 CSV（状态列明确标记 SATURATED），但绝不
                # 进入滤波链、不产生有效衰减量；持续饱和后的恢复
                # 由 update_mv 间隔检测触发重新预热
                st.sample_mv = float(sample_mv)
                st.last_update_monotonic = time.monotonic()
                st.clear_replay_overrides()
                st.set_status(ChannelStatus.SATURATED)
                continue
            st.update_mv(sample_mv)
            if not st.filter_settled:
                # 滤波（重）建立中：预热期与正常测量明确区分
                st.set_status(ChannelStatus.WARMING_UP)
            elif st.baseline_mv is None:
                st.set_status(ChannelStatus.NOT_CALIBRATED)
            else:
                st.set_status(ChannelStatus.OK)

    def last_wire_mv(self, channel: int) -> Optional[float]:
        """最近一帧该通道的 wire mV（无论是否启用/有效）。

        - enabled + valid：返回 dframe.samples_mv[ch]
        - enabled + invalid：返回 None（固件未提供有效值）
        - not enabled：返回 dframe.samples_mv[ch]（固件已发送）
          或 None（如果没有收到过任何 D 帧）
        """
        if not (0 <= channel < MAX_CHANNEL_COUNT):
            return None
        return self._last_dframe_wire_mv[channel]

    def last_wire_raw(self, channel: int) -> Optional[float]:
        """兼容别名：语义为 wire mV。新代码请用 last_wire_mv。"""
        return self.last_wire_mv(channel)

    def last_wire_valid(self, channel: int) -> bool:
        if not (0 <= channel < MAX_CHANNEL_COUNT):
            return False
        return self._last_dframe_valid[channel]

    # ------------------------------------------------------------------
    # 标定相关
    # ------------------------------------------------------------------
    def snapshot_for_calibration(self) -> Dict[int, Optional[float]]:
        """取所有启用通道的当前 filtered 值；标定结束后作为基线。"""
        return {c: self._states[c].filtered_mv for c in self._enabled}

    def apply_calibration(
        self, new_baselines: Dict[int, Optional[float]]
    ) -> bool:
        """
        原子性提交新基线（仅全部有效才生效）。
        返回 True 表示成功，False 表示放弃（保留旧基线）。

        局部重新标定策略（既定语义）：提交成功后，未参与本轮标定的
        通道基线一律失效——它们的旧基线属于上一轮实验条件，继续
        保留会在新 baseline_id 下产出语义造假的衰减量。校验失败/
        取消不动任何通道（原子性）。失效通道重新启用后显示"未标定"，
        采样照常显示，衰减量/百分比为空。
        """
        # 校验：所有 enabled 通道都必须有非 None 的基线
        for c in self._enabled:
            v = new_baselines.get(c)
            if v is None or not math.isfinite(v):
                return False
        for c in self._enabled:
            self._states[c].baseline_mv = float(new_baselines[c])
            # 标定成功 → 状态提升（如果当前没有更新更紧迫的）。
            # WARMING_UP 不得提升为 OK：滤波中值窗未填满时快照的
            # filtered/attenuation 必须为 None（预热语义），提升会
            # 绕过统一有效性门槛把未稳定的值当正常测量。
            if self._states[c].last_status not in (
                ChannelStatus.SATURATED,
                ChannelStatus.INVALID,
                ChannelStatus.WARMING_UP,
            ):
                self._states[c].set_status(ChannelStatus.OK)
        # 未参与本轮标定的通道：基线失效（config 侧由调用方按
        # 参与通道集合保存，缺失通道读取时自动为 None，两处一致）
        for c in range(MAX_CHANNEL_COUNT):
            if c in new_baselines:
                continue
            st = self._states[c]
            if st.baseline_mv is not None:
                st.baseline_mv = None
                if st.last_status == ChannelStatus.OK:
                    st.set_status(ChannelStatus.NOT_CALIBRATED)
        return True

    def reset_filter_only(self) -> None:
        """只清滤波（标定过程中会用到）。"""
        for s in self._states.values():
            s.reset_filter()

    def reset_all(self) -> None:
        """完全清空。"""
        for s in self._states.values():
            s.reset_all()
        # 同时清空最近帧缓存
        self._last_dframe = None
        self._last_dframe_wire_mv = [None] * MAX_CHANNEL_COUNT
        self._last_dframe_valid = [False] * MAX_CHANNEL_COUNT
        self._last_dframe_received_monotonic = 0.0

    # ------------------------------------------------------------------
    # 物理量（v3：串口样本本身就是 ADS1115 输入端 mV，禁止二次换算）
    # ------------------------------------------------------------------
    def sample_to_vadc_mv(self, sample_mv: float) -> float:
        """D 帧样本即 ADS1115 输入端电压（mV），恒等返回。"""
        return float(sample_mv)

    def sample_to_detector_mv(self, sample_mv: float) -> float:
        """AD8362 VOUT 估算值（mV）= ADS1115 输入电压 × 前端分压比。"""
        divider = self.effective_detector_divider
        return float(sample_mv) * (1.0 if divider is None else divider)

    # ------------------------------------------------------------------
    # 统一测量快照（衰减量主入口）
    # ------------------------------------------------------------------
    def measurement(
        self,
        channel: int,
        frame_valid: Optional[bool] = None,
    ) -> MeasurementSnapshot:
        """通道 channel 的统一测量快照。

        frame_valid 缺省时取最近一帧的 valid_mask 位；调用方掌握
        断流信息（如串口超时）时应显式传 False。
        """
        if frame_valid is None:
            frame_valid = self.last_wire_valid(channel)
        divider = self.effective_detector_divider
        snap = build_snapshot(
            self._states[channel],
            frame_valid=frame_valid,
            enabled=channel in self._enabled,
            detector_divider=1.0 if divider is None else divider,
        )
        if divider is None:
            # 记录未标注可识别 Profile：历史 VOUT 未知，不按当前配置臆测
            snap.detector_vout_mv = None
        return snap

    def clear_session_data(self) -> None:
        """切换数据源/会话时调用：清滤波、回放覆盖与当前帧有效性缓存。

        基线是否更换由调用方按来源作用域决定（本方法不动基线）。
        """
        for s in self._states.values():
            s.reset_filter()
        self._last_dframe = None
        self._last_dframe_wire_mv = [None] * MAX_CHANNEL_COUNT
        self._last_dframe_valid = [False] * MAX_CHANNEL_COUNT
        self._last_dframe_received_monotonic = 0.0

    # ------------------------------------------------------------------
    # 变化量（含符号方向反转）— P0-5
    # 旧诊断量：delta = filtered - baseline。invert_sign 只作用于它，
    # 与 attenuation_mv（core.measurement）无关。
    # ------------------------------------------------------------------
    def delta_signed(self, channel: int) -> Optional[float]:
        """带 invert_sign 的有符号旧诊断 delta（filtered - baseline）。

        仅旧语义/诊断与 CSV 旧列使用；界面主测量量请用
        measurement() 的 attenuation_mv（不受 invert_sign 影响）。
        """
        st = self._states[channel]
        d = st.delta()
        if d is None:
            return None
        if self.invert_sign:
            return -d
        return d

    def relative_signed(self, channel: int) -> Optional[float]:
        st = self._states[channel]
        rp = st.relative_percent()
        if rp is None:
            return None
        if self.invert_sign:
            return -rp
        return rp
