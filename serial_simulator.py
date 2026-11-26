"""
v3 毫伏协议串口模拟器。

向指定虚拟串口写 v3 固件 D 帧 / E 帧，供测试或在没有硬件时调试。
D 帧格式（22 字段，与 ../v2/PROTOCOL.md 一致）：
    D,<scan>,<status>,<valid_mask>,<i2c_err>,mV,<16×mV 3位小数>\r\n
每个测量值是 0.125 mV 的整数倍，范围 [-4096.000, 4095.875]。

用法：
    python serial_simulator.py COM10
    python serial_simulator.py COM10 --channels 16
    python serial_simulator.py COM10 --channels 16 --scan-interval 0.3
"""

import argparse
import math
import os
import random
import sys
import time

# 强制使用项目根目录
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

try:
    import serial  # type: ignore
except ImportError:
    print("需要 pyserial: pip install pyserial", file=sys.stderr)
    sys.exit(1)

from models.frame import MV_MAX, MV_MIN, format_mv


def build_d_frame(
    scan_number: int,
    scan_status: int,
    valid_mask: int,
    i2c_err: int,
    samples_mv,
) -> bytes:
    """v3 D 帧：22 字段，parts[5]='mV'，测量值固定 3 位小数。"""
    parts = [
        "D",
        str(scan_number),
        str(scan_status),
        str(valid_mask),
        str(i2c_err),
        "mV",
    ]
    parts.extend(format_mv(float(s)) for s in samples_mv)
    return (",".join(parts) + "\r\n").encode("ascii")


def build_e_frame(init_status, init_attempts, recovers, errs) -> bytes:
    return (
        f"E,INIT,{init_status},{init_attempts},{recovers},{errs}\r\n"
    ).encode("ascii")


def _quantize_mv(v: float) -> float:
    """量化到 0.125 mV 并截断到量程。"""
    q = round(v / 0.125) * 0.125
    return max(MV_MIN, min(MV_MAX, q))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("port", help="虚拟串口名 (例如 COM10)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument(
        "--channels",
        type=int,
        default=16,
        help="启用通道数 (4 或 16)",
    )
    ap.add_argument(
        "--scan-interval",
        type=float,
        default=0.3,
        help="每帧之间间隔秒数（v3 实机约 0.29s，~3.5 fps）",
    )
    ap.add_argument(
        "--amplitude",
        type=float,
        default=187.5,
        help="金属响应幅度（mV，非负；放入金属使检波电压下降）",
    )
    ap.add_argument(
        "--noise",
        type=float,
        default=1.5,
        help="高斯噪声 σ（mV）",
    )
    ap.add_argument(
        "--mode",
        choices=["loop", "metal", "calm"],
        default="metal",
    )
    args = ap.parse_args()

    if args.channels not in (4, 16):
        print("仅支持 4 或 16 通道", file=sys.stderr)
        sys.exit(2)

    rng = random.Random(int(time.time()))

    try:
        ser = serial.Serial(
            port=args.port,
            baudrate=args.baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.05,
        )
    except (serial.SerialException, OSError) as e:
        print(f"无法打开 {args.port}: {e}", file=sys.stderr)
        sys.exit(1)

    print(
        f"v3 simulator (mV) -> {args.port} {args.baud}8N1, "
        f"{args.channels} ch, mode={args.mode}"
    )
    scan_number = 0
    init_status = 0
    init_attempts = 1
    recovers = 0
    errs = 0

    t0 = time.monotonic()
    try:
        while True:
            scan_number += 1
            t = time.monotonic() - t0
            valid_mask = 0
            samples = []
            for ch in range(16):
                if args.channels == 4 and ch not in (0, 1, 4, 5):
                    # 未启用通道：valid bit 置 0，值给 0.000
                    samples.append(0.0)
                    continue
                base = 1500.0  # 无钢板空气基线静态工作点（mV）
                amp = max(0.0, args.amplitude)  # 响应幅度非负
                if args.mode == "metal":
                    # 金属来回扫：放入金属 → 检波电压下降（衰减实验语义）
                    pos = (t / 4.0) % 4.0
                    d2 = (
                        (ch // 4 - pos) ** 2
                        + (ch % 4 - pos) ** 2
                    )
                    resp = amp * math.exp(-d2 / 2.0)
                elif args.mode == "calm":
                    resp = 0.0
                else:
                    resp = math.sin(t * 0.5 + ch) * amp * 0.3
                # sample = air_baseline − metal_response + noise
                val = _quantize_mv(
                    base - resp + rng.gauss(0, args.noise)
                )
                samples.append(val)
                valid_mask |= (1 << ch)

            frame = build_d_frame(
                scan_number, 0, valid_mask, 0, samples
            )
            ser.write(frame)

            # 偶发 E 帧（心跳，每 ~5 帧）
            if scan_number % 5 == 0:
                ef = build_e_frame(
                    init_status, init_attempts, recovers, errs
                )
                ser.write(ef)

            time.sleep(args.scan_interval)
    except KeyboardInterrupt:
        print("\n退出")
    finally:
        try:
            ser.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
