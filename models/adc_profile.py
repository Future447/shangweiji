"""
ADC Profile 定义。

固件 v3 实际硬件是 ADS1115（16-bit signed，±4.096V PGA），前端 1/2 分压 + RC。
v3 协议下 D 帧样本本身就是 ADS1115 输入端电压（mV），串口链路不再
调用 raw_to_vadc_mv / raw_to_detector_mv（避免二次换算）。
raw_to_* 仅保留给 count 域的旧数据 / 兼容 Profile 使用。
本模块不依赖 numpy。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


# ADS1115 16-bit signed raw 范围
ADS1115_RAW_MIN = -32768
ADS1115_RAW_MAX = 32767

# STM32 12-bit ADC raw 范围
STM32_ADC_RAW_MIN = 0
STM32_ADC_RAW_MAX = 4095


@dataclass(frozen=True)
class AdcProfile:
    """一个 ADC 转换规则集合。"""

    name: str                              # 内部唯一键
    display_name: str                      # UI 显示
    raw_min: int                           # 原始下界
    raw_max: int                           # 原始上界
    raw_is_signed: bool                    # 原始值是否带符号
    vref_mv: int                           # 等效参考电压 mV（Vadc = raw × Vref / range）
    raw_count: int                         # 量化档数（2^bits）
    detector_divider: float = 1.0          # Vadc -> AD8362_VOUT 倍率（前端 1/2 时为 2.0）
    description: str = ""

    def raw_to_vadc_mv(self, raw: int) -> int:
        """
        把原始值换算成 ADC 输入端电压 (mV)。四舍五入。

        - signed (e.g. ADS1115 ±4.096V)：
            Vadc = raw * Vref / 2^N
        - unsigned (e.g. STM32 12-bit ADC)：
            Vadc = raw * Vref / (2^N - 1)
        """
        if self.raw_is_signed:
            return _signed_round(raw * self.vref_mv, self.raw_count)
        else:
            return _signed_round(raw * self.vref_mv, self.raw_count - 1)

    def raw_to_detector_mv(self, raw: int) -> int:
        """把原始值换算为 AD8362 输出端电压 (mV)。"""
        vadc = self.raw_to_vadc_mv(raw)
        return int(round(vadc * self.detector_divider))

    def mv_to_detector_mv(self, sample_mv: float) -> float:
        """v3：ADS1115 输入端电压（mV）-> AD8362 VOUT 估算值（mV）。

        前端 1/2 分压时 detector_divider=2.0，即 VOUT ≈ 2 × 样本值。
        """
        return float(sample_mv) * self.detector_divider

    def is_saturated(self, raw: int) -> bool:
        """原始值是否已饱和（到达量程上下限）。仅用于 count 域。"""
        if self.raw_is_signed:
            return raw <= self.raw_min or raw >= self.raw_max
        return raw <= self.raw_min or raw >= self.raw_max

    def validate(self) -> None:
        """对 Profile 自身做一致性校验。"""
        if not self.name:
            raise ValueError("AdcProfile.name 不能为空")
        if self.raw_count <= 0:
            raise ValueError("raw_count 必须 > 0")
        if self.vref_mv <= 0:
            raise ValueError("vref_mv 必须 > 0")
        if self.detector_divider <= 0:
            raise ValueError("detector_divider 必须 > 0")


def _signed_round(num: int, denom: int) -> int:
    """带符号四舍五入：按半值调整。"""
    if denom <= 0:
        raise ValueError("denom 必须 > 0")
    # 把 num 放大再除以 denom，附加 denom/2 用于四舍五入
    half = denom // 2
    if num >= 0:
        return (num * 1 + half) // denom
    else:
        return -((-num + half) // denom)


def _unsigned_round(num: int, denom: int) -> int:
    if denom <= 0:
        raise ValueError("denom 必须 > 0")
    return (num + denom // 2) // denom

def _unsigned_round_minus1(num: int, denom: int) -> int:
    """针对 N-bit unsigned ADC 的"raw * Vref / (2^N - 1)"舍入。"""
    if denom <= 0:
        raise ValueError("denom 必须 > 0")
    # 标准做法: (num + denom/2) // denom, 但要使用 denom 而非 denom-1
    return (num + denom // 2) // denom


# ----------------------------------------------------------------------------
# 内置 Profile 集合
# ----------------------------------------------------------------------------

def build_default_profiles() -> Dict[str, AdcProfile]:
    """
    返回内置 ADC Profile 集合。
    名称为内部唯一键（不展示给用户）。
    """
    return {
        "ads1115_4v096_div2": AdcProfile(
            name="ads1115_4v096_div2",
            display_name="ADS1115 / ±4.096V / 1:2 分压（旧面包板）",
            raw_min=ADS1115_RAW_MIN,
            raw_max=ADS1115_RAW_MAX,
            raw_is_signed=True,
            vref_mv=4096,
            raw_count=32768,
            detector_divider=2.0,
            description=(
                "16-bit signed ADS1115，PGA=±4.096V，前端 1:2 分压。\n"
                "Vadc_mV = D 帧值（ADS1115 输入端）\n"
                "AD8362_VOUT_mV = 2 × Vadc_mV\n"
                "仅在实物确认存在 1:2 分压时使用本 Profile。"
            ),
        ),
        "ads1115_4v096_direct": AdcProfile(
            name="ads1115_4v096_direct",
            display_name="ADS1115 / ±4.096V / 无分压（有源线圈 PCB）",
            raw_min=ADS1115_RAW_MIN,
            raw_max=ADS1115_RAW_MAX,
            raw_is_signed=True,
            vref_mv=4096,
            raw_count=32768,
            detector_divider=1.0,
            description=(
                "16-bit signed ADS1115，PGA=±4.096V，检波输出直连 AD1115 输入"
                "（无 1:2 分压）。\n"
                "Vadc_mV = D 帧值\n"
                "VOUT 估算 = 1 × Vadc_mV（不再乘 2）"
            ),
        ),
        "stm32_adc12": AdcProfile(
            name="stm32_adc12",
            display_name="STM32 内部 ADC / 12-bit / 3.3V（仅兼容）",
            raw_min=STM32_ADC_RAW_MIN,
            raw_max=STM32_ADC_RAW_MAX,
            raw_is_signed=False,
            vref_mv=3300,
            raw_count=4096,
            detector_divider=1.0,
            description=(
                "兼容旧版 STM32 内部 12-bit ADC。\n"
                "Vadc_mV = raw × 3300 / 4095"
            ),
        ),
    }


def profile_by_name(name: str, profiles: Optional[Dict[str, AdcProfile]] = None
                    ) -> AdcProfile:
    """按名字取 Profile；找不到时回退到 ads1115_4v096_div2。"""
    pool = profiles if profiles is not None else build_default_profiles()
    if name in pool:
        return pool[name]
    return pool["ads1115_4v096_div2"]
