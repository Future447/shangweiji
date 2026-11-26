# -*- coding: utf-8 -*-
"""
COM9 实机验收：只读监听 ≥10 秒，校验 v3 毫伏协议。

硬性约束：绝不向串口写入任何字节。

验收断言：
- d_frame_count > 20
- error_count == 0（无解析错误）
- scan_number 单调递增（允许不连续，USB BUSY 丢帧）
- 至少见过一帧 valid_mask == 65535
- 所有有效样本在 [-4096.000, 4095.875] 且为 0.125 整数倍
  （DFrame __post_init__ 已强制，这里再独立核对）
- 报告实测 fps 与 scan_number 范围

用法：
    python tools/com9_listen_acceptance.py [COM9] [--seconds 12]
"""

import argparse
import os
import sys
import time

import serial

# 项目根目录加入 sys.path
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from core.protocol_parser import FrameKind, FrameParser
from models.frame import MV_MAX, MV_MIN, is_valid_mv_value


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("port", nargs="?", default="COM9")
    ap.add_argument("--seconds", type=float, default=12.0)
    args = ap.parse_args()

    parser = FrameParser()
    scan_numbers = []
    saw_full_mask = False
    sample_check_count = 0
    first_frame_line = None

    # 只读打开：timeout 读，不写任何字节
    ser = serial.Serial(
        port=args.port,
        baudrate=115200,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=0.2,
        write_timeout=0,
    )
    # 防御：立即丢弃任何输出缓冲（我们不写，但确保万无一失）
    try:
        ser.reset_output_buffer()
    except Exception:
        pass

    t0 = time.monotonic()
    print(f"listening {args.port} read-only for {args.seconds:.0f}s ...")
    try:
        while time.monotonic() - t0 < args.seconds:
            data = ser.read(4096)
            if not data:
                continue
            for pf in parser.feed(data):
                if pf.kind == FrameKind.D and pf.d_frame is not None:
                    df = pf.d_frame
                    if first_frame_line is None:
                        first_frame_line = pf.raw_line.strip()
                    scan_numbers.append(df.scan_number)
                    if df.valid_mask == 0xFFFF:
                        saw_full_mask = True
                    for ch in range(16):
                        if df.is_valid(ch):
                            v = df.samples_mv[ch]
                            assert MV_MIN <= v <= MV_MAX, (ch, v)
                            assert is_valid_mv_value(v), (ch, v)
                            sample_check_count += 1
    finally:
        ser.close()

    dt = time.monotonic() - t0
    stats = parser.stats()
    d_count = stats["d_frame_count"]
    e_count = stats["e_frame_count"]
    err_count = stats["error_count"]
    fps = d_count / dt if dt > 0 else 0.0

    scan_ok = all(
        b > a or (b == 0 and a > 0xFFFFFFF0)
        for a, b in zip(scan_numbers, scan_numbers[1:])
    )

    print("=" * 60)
    print(f"首帧: {first_frame_line}")
    print(f"时长: {dt:.1f}s")
    print(f"D 帧: {d_count}  E 帧: {e_count}  解析错误: {err_count}")
    print(f"fps: {fps:.2f}")
    if scan_numbers:
        print(
            f"scan_number: {scan_numbers[0]} .. {scan_numbers[-1]} "
            f"(单调递增: {scan_ok})"
        )
    print(f"valid_mask=65535 出现过: {saw_full_mask}")
    print(f"有效样本校验数: {sample_check_count}")
    print("=" * 60)

    failures = []
    if d_count <= 20:
        failures.append(f"d_frame_count={d_count} <= 20")
    if err_count != 0:
        failures.append(f"error_count={err_count} != 0")
    if not scan_ok:
        failures.append("scan_number 非单调递增")
    if not saw_full_mask:
        failures.append("未见过 valid_mask=65535")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("PASS: COM9 v3 毫伏协议验收全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
