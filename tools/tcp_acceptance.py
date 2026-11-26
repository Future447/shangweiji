"""
TCP 无线链路验收脚本（纯模拟，无需 Hi3863 硬件）。

流程：离屏启动主窗口 → 进入 TCP 监听 → 调用 tools/tcp_simulator.py
依次跑 basic / split / batch / eframe / reconnect / idle 场景 →
打印帧数、错误数、数据源状态，并对关键不变量做断言。

用法：
    python tools/tcp_acceptance.py
    python tools/tcp_acceptance.py --port 18080

注意：这是**模拟验证**。真实链路验证需要 Hi3863 固件 + STM32 输出
USART 数据（当前 STM32 固件只有 USB CDC 输出）。
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QCoreApplication  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402


def pump(seconds: float = 0.1) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.005)


def wait_until(pred, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return bool(pred())


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def run_sim(port: int, scenario: str, frames: int, extra=()) -> dict:
    """启动模拟客户端，**边跑边泵 Qt 事件**，同时记录状态变化。

    必须用 Popen 而不是 subprocess.run：run() 会阻塞事件循环，
    导致 worker 的排队信号（数据超时/断线）在模拟器退出后才被处理，
    无法验证"连接期间"的状态。
    """
    cmd = [
        sys.executable, os.path.join(_HERE, "tcp_simulator.py"),
        "--port", str(port), "--scenario", scenario,
        "--frames", str(frames), "--interval", "0.15",
    ]
    cmd.extend(extra)
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    before = _WIN._tcp_connect_count if _WIN is not None else 0
    saw = {"timeout": False}
    deadline = time.monotonic() + 30.0
    while proc.poll() is None and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        sync_view()
        if _view["timeout"]:
            saw["timeout"] = True
        time.sleep(0.02)
    pump(0.25)
    out, err = proc.communicate(timeout=5)
    if proc.returncode != 0:
        print(out, err)
    after = _WIN._tcp_connect_count if _WIN is not None else before
    return {"rc": proc.returncode, "connects": after - before, **saw}


#: 运行态视图：由 sync_view() 从主窗口刷新
_view = {"state": "", "timeout": False}
#: 当前主窗口（run_sim 轮询期间读状态用）
_WIN = None


def sync_view() -> None:
    if _WIN is None:
        return
    try:
        _view["state"] = _WIN._source_state.name
        _view["timeout"] = bool(_WIN._tcp_idle_timeout_reported)
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args()
    port = args.port or free_port()

    from core.config_manager import ConfigManager
    import ui.main_window as mw
    from ui.main_window import SourceState

    tmp = tempfile.mkdtemp(prefix="tcpacc_")
    cfg_path = os.path.join(tmp, "config.json")
    mw.ConfigManager = lambda: ConfigManager(config_path=cfg_path)  # type: ignore

    app = QApplication.instance() or QApplication(sys.argv)
    win = mw.MainWindow()
    win.show()
    pump(0.1)

    win._transport = mw.TRANSPORT_TCP
    win._update_transport_ui()
    win._edt_listen_host.setText("127.0.0.1")
    win._sp_listen_port.setValue(port)
    win._on_connect_clicked()
    assert wait_until(lambda: win._source_state == SourceState.LISTENING), \
        "未进入监听状态"
    print(f"[1] 监听已启动 127.0.0.1:{port}，状态标签: {win._lbl_state.text()}")
    assert "监听中" in win._lbl_state.text()
    assert "设备已连接" not in win._lbl_state.text()

    global _WIN
    _WIN = win
    sync_view()

    results = {}
    for scenario, frames in (
        ("basic", 4), ("split", 3), ("batch", 3), ("eframe", 2),
    ):
        # 每个场景是一条独立连接段：帧/错误按进程级累计计数取增量，
        # 段内计数会在断开时清零（这正是本次改造的语义）。
        c0 = win._total_dframe_count
        sync_view()
        res = run_sim(port, scenario, frames)
        sync_view()
        new_frames = win._total_dframe_count - c0
        new_errors = win._error_count
        results[scenario] = (new_frames, new_errors, res)
        print(
            f"[{scenario}] 新增帧={new_frames} 本段错误={new_errors} "
            f"rc={res['rc']} 设备连入次数={res['connects']} "
            f"标签={win._lbl_state.text()}"
        )
        assert res["rc"] == 0, f"{scenario} 模拟器异常退出"
        assert res["connects"] >= 1, f"{scenario} 期间设备未连入"

    assert results["basic"][0] >= 1 and results["basic"][1] == 0
    assert results["split"][0] >= 1 and results["split"][1] == 0
    assert results["batch"][0] >= 2 and results["batch"][1] == 0
    assert results["eframe"][0] >= 1 and results["eframe"][1] == 0

    # 断线重连：断开后回到监听态，重连后继续收帧（新连接段）
    c0 = win._total_dframe_count
    sync_view()
    res = run_sim(port, "reconnect", 3)
    sync_view()
    print(
        f"[reconnect] 新增帧={win._total_dframe_count - c0} "
        f"rc={res['rc']} 设备连入次数={res['connects']} "
        f"当前状态={win._source_state.name}"
    )
    assert win._total_dframe_count > c0, "重连后未继续收帧"
    assert res["connects"] >= 2, "未观察到重连（连入次数 < 2）"
    assert win._source_state == SourceState.LISTENING, \
        "设备断开后未回到监听态"

    # 长时间无数据：连接保持 + 数据超时提示（不判为断线）
    sync_view()
    res = run_sim(port, "idle", 7)
    print(
        f"[idle] 上报数据超时={res['timeout']} "
        f"rc={res['rc']} 设备连入次数={res['connects']} "
        f"当前状态={win._source_state.name}"
    )
    assert res["timeout"], "未上报数据超时"
    assert res["connects"] >= 1, "idle 期间连接被误判为断开"

    win._on_disconnect_clicked()
    assert wait_until(
        lambda: win._source_state == SourceState.DISCONNECTED, timeout=4.0
    )
    print(f"[stop] 已停止监听，状态={win._source_state.name}")

    win._refresh_timer.stop()
    win.close()
    pump(0.1)
    print("\n全部模拟场景通过（注意：这是 loopback 模拟，不是硬件实测）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
