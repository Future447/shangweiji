"""
单通道状态：当前采样（mV）、EMA 滤波、基线、变化量、状态。

v3 毫伏协议：所有数值均为 mV（固件 D 帧直接给出，权威单位）。
不再保存 raw ADC count。
"""

from __future__ import annotations

import enum
import math
import time
from dataclasses import dataclass, field
from typing import Optional


class ChannelStatus(enum.IntEnum):
    """单通道当前状态。"""

    UNKNOWN = 0
    NO_DATA = 1
    INVALID = 2            # 本帧 D 帧中 valid_mask=0
    SATURATED = 3
    NOT_CALIBRATED = 4
    OK = 5
    WARMING_UP = 6         # 滤波（重）建立中：有效样本不足，值不代表当前


@dataclass
class ChannelState:
    """单个通道的运行时状态（全部数值为 mV）。"""

    channel: int
    sample_mv: float = 0.0                      # 最新采样（mV，来自 D 帧）
    filtered_mv: Optional[float] = None         # EMA 后的值（mV）
    baseline_mv: Optional[float] = None         # 标定基线（mV，滤波值空间）
    last_update_monotonic: float = field(default_factory=time.monotonic)
    sample_count: int = 0                       # 累计有效样本数
    last_status: ChannelStatus = ChannelStatus.NO_DATA

    # 中值滤波窗口（仅保留最新 N 个样本），用于轻量去尖峰
    median_window: int = 3
    _median_buf: list = field(default_factory=list)

    # EMA 滤波参数
    ema_alpha: float = 0.2

    # 滤波重建间隔门限（秒）：两个被接受的有效样本之间超过该间隔
    # （持续饱和/无效/断流后的恢复），EMA/中值状态不再代表当前
    # 物理量，自动重新播种并重新预热。短暂失效（<= 该值）期间没有
    # 任何样本进入滤波链，旧状态仍然有效，恢复时直接继续使用。
    FILTER_GAP_RESET_S = 2.0
    # 最近一次"被滤波链接纳"的有效样本时间（饱和/无效样本不计）
    _last_accept_monotonic: Optional[float] = None

    # 回放派生量覆盖（仅 CSV 回放路径设置）。
    # use_replay_derived=True 时，衰减量/下降比例直接取 CSV 保存值
    # （可能为 None = 未知），不按公式重算、不再经 invert_sign 翻转。
    use_replay_derived: bool = False
    attenuation_override_mv: Optional[float] = None
    drop_pct_override: Optional[float] = None

    # ------------------------------------------------------------------
    # 兼容别名（旧接口名，逐步迁移；新代码请用 *_mv）
    # ------------------------------------------------------------------
    @property
    def raw(self) -> float:
        return self.sample_mv

    @raw.setter
    def raw(self, v: float) -> None:
        self.sample_mv = 0.0 if v is None else float(v)

    @property
    def filtered(self) -> Optional[float]:
        return self.filtered_mv

    @filtered.setter
    def filtered(self, v: Optional[float]) -> None:
        self.filtered_mv = v

    @property
    def baseline(self) -> Optional[float]:
        return self.baseline_mv

    @baseline.setter
    def baseline(self, v: Optional[float]) -> None:
        self.baseline_mv = v

    # ------------------------------------------------------------------
    # 防止基线为 0 触发除零
    @staticmethod
    def safe_baseline(b: Optional[float]) -> Optional[float]:
        if b is None or not math.isfinite(b):
            return None
        if abs(b) < 1e-9:
            return None
        return b

    @property
    def filter_settled(self) -> bool:
        """滤波是否已（重）建立：中值窗被本通道有效样本填满。

        预热计数只看本通道被滤波链实际接纳的有效样本，与全局帧数
        无关（饱和/无效帧不算）。未稳定期间的滤波值不得当作正常
        测量：UI 显示"预热中"，衰减量/CSV 派生列留空。
        """
        return len(self._median_buf) >= max(1, int(self.median_window))

    def _trim_median_buf(self) -> None:
        """把中值缓冲裁到当前窗口长度（保留最新样本）。

        运行中缩小窗口时立即生效——旧实现每次 update_mv 只弹 1 个
        元素，缓冲长度永远卡在旧窗口值，新配置静默不生效。
        """
        w = max(1, int(self.median_window))
        if len(self._median_buf) > w:
            del self._median_buf[:-w]

    def set_median_window(self, n: int) -> None:
        """更新中值窗口并立即裁剪缓冲（保留最新 n 个样本）。"""
        self.median_window = max(1, int(n))
        self._trim_median_buf()

    def reset_filter(self) -> None:
        """仅清空滤波；保留基线。"""
        self.filtered_mv = None
        self._median_buf.clear()
        self.sample_count = 0
        self._last_accept_monotonic = None
        self.clear_replay_overrides()

    def reset_all(self) -> None:
        """完全清空（基线、滤波、状态）。"""
        self.filtered_mv = None
        self.baseline_mv = None
        self._median_buf.clear()
        self.sample_count = 0
        self._last_accept_monotonic = None
        self.last_status = ChannelStatus.NO_DATA
        self.last_update_monotonic = time.monotonic()
        self.clear_replay_overrides()

    def clear_replay_overrides(self) -> None:
        """清除回放派生量覆盖（回到实时公式语义）。"""
        self.use_replay_derived = False
        self.attenuation_override_mv = None
        self.drop_pct_override = None

    def set_replay_derived(
        self,
        attenuation_mv: Optional[float],
        drop_pct: Optional[float],
    ) -> None:
        """回放路径写入已保存的派生量（None 表示该文件无此量）。"""
        self.use_replay_derived = True
        self.attenuation_override_mv = attenuation_mv
        self.drop_pct_override = drop_pct

    def update_mv(self, sample_mv: float) -> None:
        """推入一个新的有效采样（mV）。带中值 + EMA。

        仅限有效且未饱和的样本调用（调用方负责先筛查）：饱和
        rail 值/非有限值是前端异常状态，不是被测物理量，进入
        中值/EMA 会污染滤波链与后续标定。

        长间隔（> FILTER_GAP_RESET_S，如持续饱和/断流后的恢复）
        自动重新播种滤波：EMA 是指数加权历史，长时间无新输入后
        旧状态不再代表当前物理量；此后中值窗需重新填满才算稳定
        （filter_settled），预热进度只计本通道新到的有效样本。
        """
        # 实时帧到达：任何回放覆盖立即失效
        self.clear_replay_overrides()
        v = float(sample_mv)
        now = time.monotonic()
        if (
            self._last_accept_monotonic is not None
            and now - self._last_accept_monotonic > self.FILTER_GAP_RESET_S
        ):
            # 长中断恢复：抛弃陈旧滤波状态，重新播种 + 重新预热
            self._median_buf.clear()
            self.filtered_mv = None
        self._last_accept_monotonic = now
        self.sample_mv = v
        self.last_update_monotonic = now
        # 中值窗口
        self._median_buf.append(v)
        self._trim_median_buf()
        sorted_buf = sorted(self._median_buf)
        median_val = sorted_buf[len(sorted_buf) // 2]

        # EMA（基于中值后再做）
        if self.filtered_mv is None:
            self.filtered_mv = float(median_val)
        else:
            a = self.ema_alpha
            if a < 0.0:
                a = 0.0
            elif a > 1.0:
                a = 1.0
            self.filtered_mv = (
                a * float(median_val) + (1.0 - a) * self.filtered_mv
            )

        self.sample_count += 1

    # 兼容旧名
    def update_raw(self, raw: float) -> None:
        self.update_mv(raw)

    def delta(self) -> Optional[float]:
        """有符号 delta = filtered_mv - baseline_mv（mV）。"""
        if self.filtered_mv is None or self.baseline_mv is None:
            return None
        return self.filtered_mv - self.baseline_mv

    def delta_mv(self) -> Optional[float]:
        return self.delta()

    def relative_percent(self) -> Optional[float]:
        """相对变化百分比 = delta / |baseline| * 100。"""
        b = ChannelState.safe_baseline(self.baseline_mv)
        if b is None or self.filtered_mv is None:
            return None
        d = self.filtered_mv - b
        return d / abs(b) * 100.0

    def set_status(self, status: ChannelStatus) -> None:
        self.last_status = status

    def to_dict(self) -> dict:
        """快照用于 CSV 写入（mV 单位）。"""
        return {
            "sample_mv": self.sample_mv,
            "filtered_mv": self.filtered_mv,
            "baseline_mv": self.baseline_mv,
            "delta_mv": self.delta(),
            "relative_percent": self.relative_percent(),
            "status": int(self.last_status),
        }
