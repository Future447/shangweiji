"""
v3 毫伏协议解析器（STM32 固件当前实际输出）。

帧格式（权威定义见 models/frame.py 与 ../v2/PROTOCOL.md）：
- D 帧：D,<scan>,<status>,<valid_mask>,<i2c_err>,mV,<16×mV 3位小数>（22 字段）
- E 帧：E,INIT,<init_status>,<init_attempts>,<recover_attempts>,<total_errors>

特性：
- 半包、多帧粘包
- 跨 read 缓冲
- CR / LF / CRLF
- 空行 / `#` 诊断行
- D 帧 / E 帧分别尝试；其它非空行作为 unknown
- 帧字段数量错误、缺少 mV 标记、非 3 位小数、越出量程、非 0.125 整数倍
  等错误不抛异常，而是返回 `Unknown` 行 + 增加错误计数
- 超长未结束缓存保护（> MAX_RX_BUFFER 触发 reset）
- 显式 reset() 方法
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import List, Optional, Tuple

from models.frame import (
    DFrame,
    EFrame,
    FrameParseError,
    parse_d_frame,
    parse_e_frame,
)


MAX_RX_BUFFER = 64 * 1024  # 64KB 上限，远大于单帧 145 字节


class FrameKind(enum.IntEnum):
    D = 0
    E = 1
    DIAG = 2       # 以 '#' 开头的诊断行
    UNKNOWN = 3    # 非空、非 D/E/# 的行
    EMPTY = 4      # 空行


@dataclass(frozen=True)
class ParsedFrame:
    """解析出的一行结果。"""

    kind: FrameKind
    raw_line: str            # 原始行（去换行符）
    d_frame: Optional[DFrame] = None
    e_frame: Optional[EFrame] = None
    error: Optional[str] = None  # 当 kind == UNKNOWN 且解析失败时填


class FrameParser:
    """
    状态化的行解析器。
    一次接收一坨字节（bytes 或 str），内部缓冲并产出 N 个 ParsedFrame。
    """

    def __init__(self, max_buffer: int = MAX_RX_BUFFER) -> None:
        self._buf: str = ""
        self._max = max_buffer
        self.reset_count = 0
        self.error_count = 0
        self.diag_count = 0
        self.d_frame_count = 0
        self.e_frame_count = 0

    # ------------------------------------------------------------------
    # 状态控制
    # ------------------------------------------------------------------
    def reset(self) -> None:
        """完全重置内部缓冲。"""
        self._buf = ""
        self.reset_count += 1

    def stats(self) -> dict:
        return {
            "d_frame_count": self.d_frame_count,
            "e_frame_count": self.e_frame_count,
            "diag_count": self.diag_count,
            "error_count": self.error_count,
            "reset_count": self.reset_count,
            "buffer_bytes": len(self._buf),
        }

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def feed(self, data) -> List[ParsedFrame]:
        """
        接收 bytes 或 str，返回解析出的所有完整行。
        """
        if isinstance(data, (bytes, bytearray)):
            try:
                text = data.decode("utf-8", errors="replace")
            except Exception:
                self.error_count += 1
                return []
        else:
            text = str(data)

        self._buf += text
        return self._drain()

    def flush(self) -> List[ParsedFrame]:
        """
        把当前残留缓冲当一行处理（即使无换行符）。
        通常用于断连时清理。
        """
        return self._drain(force=True)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _drain(self, force: bool = False) -> List[ParsedFrame]:
        # 统一换行
        buf = self._buf.replace("\r\n", "\n").replace("\r", "\n")

        if force:
            parts = [buf]
            self._buf = ""
        else:
            if "\n" not in buf:
                # 没有完整行，先做超长保护
                if len(buf) > self._max:
                    # 超长且无换行 → 视为错误，重置
                    self._buf = ""
                    self.reset_count += 1
                    self.error_count += 1
                else:
                    self._buf = buf
                return []
            parts = buf.split("\n")
            # 最后一段可能是不完整行
            self._buf = parts[-1]
            parts = parts[:-1]

        # 超长保护
        if len(self._buf) > self._max:
            self._buf = ""
            self.reset_count += 1
            self.error_count += 1

        out: List[ParsedFrame] = []
        for line in parts:
            out.append(self._parse_one(line))
        return out

    def _parse_one(self, line: str) -> ParsedFrame:
        stripped = line.strip()
        raw = line
        if not stripped:
            return ParsedFrame(kind=FrameKind.EMPTY, raw_line=raw)

        if stripped.startswith("#"):
            self.diag_count += 1
            return ParsedFrame(kind=FrameKind.DIAG, raw_line=raw)

        if stripped.startswith("D,") or stripped == "D":
            try:
                d = parse_d_frame(stripped)
                self.d_frame_count += 1
                return ParsedFrame(
                    kind=FrameKind.D, raw_line=raw, d_frame=d
                )
            except FrameParseError as e:
                self.error_count += 1
                return ParsedFrame(
                    kind=FrameKind.UNKNOWN,
                    raw_line=raw,
                    error=str(e),
                )

        if stripped.startswith("E,") or stripped == "E":
            try:
                ef = parse_e_frame(stripped)
                self.e_frame_count += 1
                return ParsedFrame(
                    kind=FrameKind.E, raw_line=raw, e_frame=ef
                )
            except FrameParseError as e:
                self.error_count += 1
                return ParsedFrame(
                    kind=FrameKind.UNKNOWN,
                    raw_line=raw,
                    error=str(e),
                )

        # 既不是 D / E / # 也不为空 → unknown
        self.error_count += 1
        return ParsedFrame(
            kind=FrameKind.UNKNOWN,
            raw_line=raw,
            error="无法识别的帧类型",
        )
