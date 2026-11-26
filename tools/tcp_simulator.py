"""
TCP 模拟采集设备（客户端）：向运行中的上位机监听端口发送 v3/v4 D/E 帧。

用途：
- 没有 Hi3863 硬件时验证"TCP 监听 → 解析 → 曲线/热力图/记录"整条链路；
- 复现半帧、粘包、断线重连、长时间静默等边界场景。

协议（与 STM32 现行固件一致，v4 pair semantics，报文布局不变）：
    D,<scan>,<status>,<valid_mask>,<i2c_err>,mV,<P1..P16 mV 3 位小数>\r\n
    E,INIT,<init_status>,<init_attempts>,<recover_attempts>,<total_errors>\r\n

用法：
    python tools/tcp_simulator.py                      # 默认 127.0.0.1:8080
    python tools/tcp_simulator.py --port 8080 --scenario split
    python tools/tcp_simulator.py --scenario all --frames 6

场景（--scenario）：
    basic     连续完整帧（默认）
    eframe    先发 E 帧（INIT 状态 0），再发 D 帧
    split     每帧拆成多次发送（先发前半段，再发后半段）
    batch     一次 send 里合并多帧（粘包）
    reconnect 发几帧后断开，1 秒后重连再发
    idle      连接后静默（不发数据），用于验证"数据超时≠断线"
    all       依次执行 eframe → split → batch → reconnect → idle
"""

from __future__ import annotations

import argparse
import os
import random
import socket
import sys
import time

_script_dir = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_script_dir)
if _root not in sys.path:
    sys.path.insert(0, _root)

from models.frame import MV_MAX, MV_MIN, format_mv  # noqa: E402


def quantize_mv(v: float) -> float:
    """量化到 0.125 mV 并限制在量程内（与固件 ADC 步进一致）。"""
    q = round(v / 0.125) * 0.125
    return max(MV_MIN, min(MV_MAX, q))


def build_d_frame(
    scan_number: int,
    scan_status: int,
    valid_mask: int,
    i2c_err: int,
    samples_mv,
) -> bytes:
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


def build_e_frame(
    init_status: int, init_attempts: int, recovers: int, errs: int
) -> bytes:
    return (
        f"E,INIT,{init_status},{init_attempts},{recovers},{errs}\r\n"
    ).encode("ascii")


def make_samples(
    scan: int, rng: random.Random, amplitude: float = 180.0, noise: float = 1.5
) -> tuple:
    """16 通道 mV 样本：模拟金属靠近时检波电压下降。"""
    samples = []
    for ch in range(16):
        pos = (scan / 8.0) % 4.0
        d2 = (ch // 4 - pos) ** 2 + (ch % 4 - pos) ** 2
        resp = amplitude * pow(2.718281828, -d2 / 2.0)
        samples.append(quantize_mv(1500.0 - resp + rng.gauss(0.0, noise)))
    return samples


class SimClient:
    def __init__(self, host: str, port: int, timeout: float = 5.0) -> None:
        self._host = host
        self._port = port
        self._sock: socket.socket | None = None
        self._timeout = timeout

    def connect(self) -> None:
        self._sock = socket.create_connection(
            (self._host, self._port), timeout=self._timeout
        )
        print(f"[sim] 已连接 {self._host}:{self._port}")

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            print("[sim] 连接已关闭")

    def send(self, data: bytes) -> None:
        if self._sock is None:
            raise RuntimeError("未连接")
        self._sock.sendall(data)

    def send_split(self, data: bytes, parts: int = 2, gap: float = 0.08) -> None:
        """把一帧拆成 parts 段先后发送，模拟半帧到达。"""
        step = max(1, len(data) // parts)
        for i in range(0, len(data), step):
            self.send(data[i:i + step])
            time.sleep(gap)


def _frames(rng, start_scan: int, count: int):
    for i in range(count):
        scan = start_scan + i
        yield build_d_frame(
            scan, 0, 0xFFFF, 0, make_samples(scan, rng)
        )


def run_scenario(client: SimClient, name: str, frames: int,
                 interval: float, rng: random.Random) -> None:
    if name == "eframe":
        client.send(build_e_frame(0, 1, 0, 0))
        time.sleep(interval)
        for data in _frames(rng, 1, frames):
            client.send(data)
            time.sleep(interval)
    elif name == "split":
        for data in _frames(rng, 1, frames):
            client.send_split(data, parts=3)
            time.sleep(interval)
    elif name == "batch":
        chunk = b"".join(_frames(rng, 1, max(2, frames)))
        client.send(chunk)
        print(f"[sim] 一次性发送 {len(chunk)} 字节（多帧合并）")
    elif name == "reconnect":
        for data in _frames(rng, 1, 2):
            client.send(data)
            time.sleep(interval)
        client.close()
        time.sleep(1.0)
        client.connect()
        for data in _frames(rng, 3, frames):
            client.send(data)
            time.sleep(interval)
    elif name == "idle":
        print(f"[sim] 保持连接但静默 {frames} 秒（验证数据超时≠断线）")
        time.sleep(float(frames))
    else:  # basic
        for data in _frames(rng, 1, frames):
            client.send(data)
            time.sleep(interval)


def main() -> int:
    ap = argparse.ArgumentParser(description="v3/v4 协议 TCP 模拟客户端")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument(
        "--scenario",
        choices=["basic", "eframe", "split", "batch", "reconnect", "idle", "all"],
        default="basic",
    )
    ap.add_argument("--frames", type=int, default=8)
    ap.add_argument("--interval", type=float, default=0.3,
                    help="帧间隔秒（实机约 0.29s）")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    client = SimClient(args.host, args.port)
    try:
        client.connect()
        if args.scenario == "all":
            for sc in ("eframe", "split", "batch", "reconnect", "idle"):
                print(f"\n=== 场景: {sc} ===")
                if client._sock is None:
                    client.connect()
                run_scenario(client, sc, args.frames, args.interval, rng)
        else:
            run_scenario(client, args.scenario, args.frames, args.interval, rng)
        print("[sim] 完成")
        return 0
    except OSError as e:
        print(f"[sim] 连接/发送失败: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[sim] 中断")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
