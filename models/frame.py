"""
D 帧 / E 帧 数据模型（v3 毫伏协议）。

v3 固件 D 帧格式（参考 v2/PROTOCOL.md，权威）：
    D,<scan_number>,<scan_status>,<valid_mask>,<i2c_error_count>,mV,<S1_mV>..<S16_mV>\r\n
    - 字段总数固定 22 个
    - parts[5] 必须是字面量 "mV"
    - S1_mV..S16_mV：带符号十进制，固定 3 位小数
    - 合法范围 [-4096.000, 4095.875] mV，步进 0.125 mV（1 LSB = 125 µV）
    - 上位机以 mV 为权威测量值，绝不反算 raw count

v3 固件 E 帧格式（保持不变）：
    E,INIT,<init_status>,<init_attempts>,<recover_attempts>,<total_errors>\r\n
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

# 16 通道上限
MAX_CHANNEL_COUNT = 16

# ReceiverStatus 枚举（与 receiver_array.h 一一对应）
RECEIVER_STATUS_NAMES = {
    0: "OK",
    1: "PARTIAL_DATA",
    2: "ERROR_PARAM",
    3: "ERROR_ADS1115",
    4: "ERROR_NO_VALID_SAMPLE",
    5: "ERROR_I2C",
}

# ---------------------------------------------------------------------------
# v3 毫伏协议常量
# ---------------------------------------------------------------------------
MV_UNIT = "mV"

# 量程（对应 ADS1115 PGA=±4.096V，1 LSB = 0.125 mV）
MV_MIN = -4096.0
MV_MAX = 4095.875
MV_STEP = 0.125

# 微伏整数域（解析时先以 µV 验证，避免浮点误差）
UV_STEP = 125                    # 1 LSB = 125 µV
MV_MIN_UV = -4_096_000           # -4096.000 mV
MV_MAX_UV = 4_095_875            # +4095.875 mV

# D 帧字段数：1 头 + 4 元数据 + 1 单位标记 + 16 通道
D_FRAME_FIELD_COUNT = 1 + 4 + 1 + MAX_CHANNEL_COUNT

# 测量字段格式：可选负号 + 1~4 位整数 + "." + 恰好 3 位小数
# 合法长度 5 字符（"0.000"）到 9 字符（"-4096.000"）
_MV_FIELD_RE = re.compile(r"^-?\d{1,4}\.\d{3}$")


class FrameParseError(Exception):
    """协议解析错误的统一异常类型。"""


def parse_mv_field(text: str, channel: int) -> float:
    """
    把单个测量字段解析为 float mV。

    解析路径：字符串 -> 整数 µV（验证范围与 125 µV 步进）-> float mV。
    合法值都是 0.125 mV 的整数倍（即 k/8），可精确表示为二进制浮点，
    不会引入累计误差。

    拒绝：普通整数、缺少小数位、小数位不是 3 位、科学计数法、
    NaN / Inf、超出量程、不是 0.125 mV 整数倍。
    """
    if not _MV_FIELD_RE.match(text):
        raise FrameParseError(
            f"通道 {channel} 字段不是合法 mV 格式"
            f"（须为 [-]d{{1,4}}.ddd）: {text!r}"
        )
    neg = text.startswith("-")
    body = text[1:] if neg else text
    int_part, frac_part = body.split(".")
    uv = int(int_part) * 1000 + int(frac_part)
    if neg:
        uv = -uv
    if not (MV_MIN_UV <= uv <= MV_MAX_UV):
        raise FrameParseError(
            f"通道 {channel} 超出 mV 量程 "
            f"[{MV_MIN:.3f}, {MV_MAX:.3f}]: {text!r}"
        )
    if uv % UV_STEP != 0:
        raise FrameParseError(
            f"通道 {channel} 不是 0.125 mV 的整数倍: {text!r}"
        )
    return uv / 1000.0


def mv_to_uv(mv: float) -> int:
    """float mV -> 整数 µV（要求值为 0.125 mV 整数倍）。"""
    uv = round(mv * 1000.0)
    return int(uv)


def format_mv(mv: float) -> str:
    """格式化为固件一致的 3 位小数字符串（供模拟器/测试使用）。"""
    uv = mv_to_uv(mv)
    neg = uv < 0
    a = -uv if neg else uv
    s = f"{a // 1000}.{a % 1000:03d}"
    return ("-" + s) if neg else s


def is_valid_mv_value(v: float) -> bool:
    """值是否为合法 mV 测量值（范围 + 0.125 步进）。"""
    if not (MV_MIN <= v <= MV_MAX):
        return False
    k = v * 8.0
    return abs(k - round(k)) < 1e-9


@dataclass(frozen=True)
class DFrame:
    """一次 16 通道扫描的 D 帧（v3，毫伏语义）。"""

    scan_number: int              # uint32
    scan_status: int              # ReceiverStatus 0..5
    valid_mask: int               # uint16
    i2c_error_count: int          # uint32
    samples_mv: Tuple[float, ...]  # 16 个 mV 测量值（权威单位）
    unit: str = MV_UNIT            # 固定 "mV"

    def __post_init__(self) -> None:
        if self.unit != MV_UNIT:
            raise ValueError(
                f"DFrame.unit 必须为 {MV_UNIT!r}，实际 {self.unit!r}"
            )
        if len(self.samples_mv) != MAX_CHANNEL_COUNT:
            raise ValueError(
                f"DFrame.samples_mv 长度必须为 {MAX_CHANNEL_COUNT}，"
                f"实际 {len(self.samples_mv)}"
            )
        if not (0 <= self.scan_status <= 5):
            raise ValueError(f"非法 scan_status: {self.scan_status}")
        if not (0 <= self.valid_mask <= 0xFFFF):
            raise ValueError(f"valid_mask 越界: {self.valid_mask}")
        for idx, val in enumerate(self.samples_mv):
            fv = float(val)
            if fv != fv or fv in (float("inf"), float("-inf")):
                raise ValueError(f"通道 {idx} 样本不是有限值: {val!r}")
            if not (MV_MIN <= fv <= MV_MAX):
                raise ValueError(
                    f"通道 {idx} 样本 {fv} 越出 mV 量程 "
                    f"[{MV_MIN}, {MV_MAX}]"
                )
            k = fv * 8.0
            if abs(k - round(k)) > 1e-9:
                raise ValueError(
                    f"通道 {idx} 样本 {fv} 不是 0.125 mV 的整数倍"
                )

    def is_valid(self, channel: int) -> bool:
        """通道 channel（0-based）是否本帧有效。"""
        if not (0 <= channel < MAX_CHANNEL_COUNT):
            return False
        return bool((self.valid_mask >> channel) & 0x1)

    def valid_count(self) -> int:
        """本帧有效通道数。"""
        return bin(self.valid_mask).count("1")


@dataclass(frozen=True)
class EFrame:
    """E 帧（心跳 / 错误状态）。"""

    subtype: str               # 固定 "INIT"
    init_status: int           # int32
    init_attempts: int         # uint32
    recover_attempts: int      # uint32
    total_errors: int          # uint32


# ----------------------------------------------------------------------------
# 行解析
# ----------------------------------------------------------------------------

def _strip_crlf(s: str) -> str:
    """去除行尾的 CR/LF/CRLF。"""
    if s.endswith("\r\n"):
        return s[:-2]
    if s.endswith("\n") or s.endswith("\r"):
        return s[:-1]
    return s


def parse_d_frame(line: str) -> DFrame:
    """
    解析一行 D 帧（v3 毫伏协议）。

    返回 DFrame。FrameParseError 当行不是合法 D 帧时抛出。
    """
    s = _strip_crlf(line).strip()
    if not s.startswith("D,"):
        raise FrameParseError("非 D 帧行")

    parts = s.split(",")
    # v3：固定 22 个字段（旧 21 字段 raw-count 帧在此被拒绝）
    if len(parts) != D_FRAME_FIELD_COUNT:
        raise FrameParseError(
            f"D 帧字段数错误: 期望 {D_FRAME_FIELD_COUNT}（v3 mV 协议）, "
            f"实际 {len(parts)}"
        )

    try:
        scan_number = int(parts[1])
        scan_status = int(parts[2])
        valid_mask = int(parts[3])
        i2c_err = int(parts[4])
    except ValueError as e:
        raise FrameParseError(f"D 帧元数据字段不是整数: {e}") from e

    if not (0 <= scan_number <= 0xFFFFFFFF):
        raise FrameParseError(f"scan_number 越界: {scan_number}")
    if not (0 <= i2c_err <= 0xFFFFFFFF):
        raise FrameParseError(f"i2c_error_count 越界: {i2c_err}")

    # parts[5] 必须严格是字面量 "mV"（防御性契约：拒绝任何无单位标记的变体）
    if parts[5] != MV_UNIT:
        raise FrameParseError(
            f"D 帧缺少 mV 单位标记（字段 5 期望 'mV'，实际 {parts[5]!r}）"
        )

    samples: List[float] = []
    for i, field_text in enumerate(parts[6:]):
        samples.append(parse_mv_field(field_text, i))

    return DFrame(
        scan_number=scan_number,
        scan_status=scan_status,
        valid_mask=valid_mask,
        i2c_error_count=i2c_err,
        samples_mv=tuple(samples),
    )


def parse_e_frame(line: str) -> EFrame:
    """
    解析一行 E 帧。
    """
    s = _strip_crlf(line).strip()
    if not s.startswith("E,"):
        raise FrameParseError("非 E 帧行")

    parts = s.split(",")
    if len(parts) != 6:
        raise FrameParseError(
            f"E 帧字段数错误: 期望 6, 实际 {len(parts)}"
        )

    subtype = parts[1]
    if subtype != "INIT":
        raise FrameParseError(f"未知的 E 帧子类型: {subtype}")

    try:
        init_status = int(parts[2])
        init_attempts = int(parts[3])
        recover_attempts = int(parts[4])
        total_errors = int(parts[5])
    except ValueError as e:
        raise FrameParseError(f"E 帧字段不是整数: {e}") from e

    if not (0 <= init_status <= 5):
        raise FrameParseError(f"init_status 越界: {init_status}")
    for name, v in (
        ("init_attempts", init_attempts),
        ("recover_attempts", recover_attempts),
        ("total_errors", total_errors),
    ):
        if not (0 <= v <= 0xFFFFFFFF):
            raise FrameParseError(f"{name} 越界: {v}")

    return EFrame(
        subtype=subtype,
        init_status=init_status,
        init_attempts=init_attempts,
        recover_attempts=recover_attempts,
        total_errors=total_errors,
    )
