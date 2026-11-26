"""
TCP 服务端接收线程（上位机监听，Hi3863 / STM32 侧作为 TCP 客户端连入）。

设计约束（与任务书逐条对应）：

1. 信号与 SerialWorker 保持同构（connected / data_received /
   error_occurred / connection_lost / stopped），UI 复用同一条处理链；
   额外提供 listening / client_rejected / data_timeout / data_resumed，
   用于区分"正在监听"与"设备已连接"以及"读取超时不等于断线"。
2. 只监听、只接收：本线程**绝不**向数据流写入任何字节（不插入
   HELLO / 日志 / 控制命令），D/E 报文格式完全由 STM32 决定。
3. 单设备策略：同一时刻只服务一台采集设备。已有设备在线时新到的
   连接被立即关闭并上报 client_rejected()，绝不把两台设备的数据
   混进同一条字节流。
4. 设备断线后继续监听，允许 Hi3863 自动重连；每次新设备连入都
   发 connected()（携带来源地址），UI 据此重置 FrameParser，
   避免跨连接拼接半帧。
5. 读取超时（socket timeout / 暂无数据）不算断线；只有 recv()
   返回空字节（对端关闭）或套接字错误才判定断线。
"""

from __future__ import annotations

import select
import socket
import threading
import time
from typing import Optional

from PyQt5.QtCore import QThread, pyqtSignal


DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = 8080
#: 已连接但长时间无字节时上报超时的阈值（秒）
DEFAULT_DATA_TIMEOUT_S = 5.0

#: select 轮询周期（秒）：决定停止响应速度与 CPU 占用的折中
_POLL_INTERVAL_S = 0.05
#: listen() backlog。单设备策略下不需要排队更多连接
_ACCEPT_BACKLOG = 1
#: 单次 recv 上限（字节）。D 帧约 145 字节，留足余量
_RECV_CHUNK = 64 * 1024


def format_endpoint(host: str, port: int) -> str:
    """把监听地址格式化为可读字符串（0.0.0.0 显式标注为全部网卡）。"""
    h = (host or "").strip() or DEFAULT_LISTEN_HOST
    if h in ("0.0.0.0", "::", ""):
        return f"0.0.0.0:{port}（全部网卡）"
    return f"{h}:{port}"


class TcpServerWorker(QThread):
    """
    TCP 服务端接收 worker。

    信号：
      listening(str)        - 已开始监听（参数为监听地址描述），尚无设备
      connected(str)        - 采集设备（客户端）已连入，参数为 "ip:port"
      data_received(bytes)  - 原始字节，直接喂 FrameParser.feed()
      error_occurred(str)   - 监听 / 接受失败（致命，线程随后退出）
      connection_lost()      - 已连接的设备断开（仍继续监听）
      client_rejected(str)  - 第二台设备被拒绝（单设备策略）
      data_timeout(float)   - 已连接但超过阈值未收到字节（**不是**断线）
      data_resumed()        - 超时上报后重新收到数据
      stopped()             - 线程结束（正常停止或致命错误）
    """

    listening = pyqtSignal(str)
    connected = pyqtSignal(str)
    data_received = pyqtSignal(bytes)
    error_occurred = pyqtSignal(str)
    connection_lost = pyqtSignal()
    client_rejected = pyqtSignal(str)
    data_timeout = pyqtSignal(float)
    data_resumed = pyqtSignal()
    stopped = pyqtSignal()

    def __init__(
        self,
        host: str = DEFAULT_LISTEN_HOST,
        port: int = DEFAULT_LISTEN_PORT,
        data_timeout_s: float = DEFAULT_DATA_TIMEOUT_S,
        keepalive_enabled: bool = True,
        keepalive_idle_s: int = 60,
        keepalive_interval_s: int = 10,
        keepalive_count: int = 3,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._host = (host or DEFAULT_LISTEN_HOST).strip()
        self._port = int(port)
        self._data_timeout_s = max(0.5, float(data_timeout_s))
        self._keepalive_enabled = bool(keepalive_enabled)
        self._keepalive_idle = max(10, int(keepalive_idle_s))
        self._keepalive_interval = max(1, int(keepalive_interval_s))
        self._keepalive_count = max(1, int(keepalive_count))
        self._stop_event = threading.Event()
        self._kick_requested = False
        self._listener: Optional[socket.socket] = None
        self._client: Optional[socket.socket] = None
        self._client_addr: str = ""
        self._last_rx_time: float = 0.0
        self._idle_reported = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------
    @property
    def endpoint(self) -> str:
        return format_endpoint(self._host, self._port)

    @property
    def client_address(self) -> str:
        return self._client_addr

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def stop(self) -> None:
        """请求线程退出：关闭监听套接字与当前客户端，并等待线程结束。"""
        self._stop_event.set()
        # 主动关闭，唤醒阻塞中的 select/recv（run() 内也会再关一次）
        self._close_client()
        self._close_listener()
        self.wait(3000)

    def disconnect_client(self) -> bool:
        """清除当前客户端并继续监听（用于"客户端突然掉电/半开连接"）。

        不发送任何业务数据，也不影响后续设备重连；返回 False 表示
        当前没有已连接客户端。
        """
        if self._client is None:
            return False
        self._kick_requested = True
        return True

    def run(self) -> None:  # noqa: D102 - QThread 入口
        if self._stop_event.is_set():
            self.stopped.emit()
            return
        try:
            self._serve()
        except OSError as e:
            if not self._stop_event.is_set():
                self.error_occurred.emit(f"无法监听 {self.endpoint}: {e}")
        finally:
            self._close_client()
            self._close_listener()
            self.stopped.emit()

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def _serve(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self._host, self._port))
            listener.listen(_ACCEPT_BACKLOG)
            listener.setblocking(False)
        except OSError:
            try:
                listener.close()
            except OSError:
                pass
            raise
        with self._lock:
            self._listener = listener
        self.listening.emit(self.endpoint)

        while not self._stop_event.is_set():
            if self._kick_requested:
                # 用户主动"清除设备"：断开当前连接但继续监听
                self._kick_requested = False
                self._drop_client()
            wait_list = [listener]
            client = self._client
            if client is not None:
                wait_list.append(client)
            try:
                readable, _, _ = select.select(
                    wait_list, [], [], _POLL_INTERVAL_S
                )
            except (OSError, ValueError):
                # 套接字已被 stop() 关闭
                break
            for sock_obj in readable:
                if sock_obj is listener:
                    self._accept_one()
                elif client is not None and sock_obj is client:
                    self._read_client()
            self._check_idle()

    # ------------------------------------------------------------------
    # 接受 / 读取
    # ------------------------------------------------------------------
    def _accept_one(self) -> None:
        if self._listener is None:
            return
        try:
            conn, addr = self._listener.accept()
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            return
        peer = f"{addr[0]}:{addr[1]}"

        if self._client is not None:
            # 单设备策略：明确拒绝第二台，绝不混合两台数据
            try:
                conn.close()
            except OSError:
                pass
            self.client_rejected.emit(peer)
            return

        try:
            conn.setblocking(False)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            try:
                conn.close()
            except OSError:
                pass
            return
        self._apply_keepalive(conn)

        self._client = conn
        self._client_addr = peer
        self._last_rx_time = time.monotonic()
        self._idle_reported = False
        self.connected.emit(peer)

    def _apply_keepalive(self, conn: socket.socket) -> None:
        """尽力开启 TCP keepalive（链路层探活）。

        仅用于发现"对端突然掉电导致的半开连接"，不产生任何业务数据；
        平台不支持时静默跳过（不影响业务）。参数刻意保守：默认空闲
        60s 才开始探测，绝不会因为正常的短暂无数据而断开设备。
        """
        if not self._keepalive_enabled:
            return
        try:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            return

        applied = False
        for opt_name, value in (
            ("TCP_KEEPIDLE", self._keepalive_idle),
            ("TCP_KEEPINTVL", self._keepalive_interval),
            ("TCP_KEEPCNT", self._keepalive_count),
        ):
            opt = getattr(socket, opt_name, None)
            if opt is None:
                continue
            try:
                conn.setsockopt(socket.IPPROTO_TCP, opt, int(value))
                applied = True
            except OSError:
                continue

        if applied:
            return
        # Windows：老版本没有 TCP_KEEPIDLE 等常量，用 ioctl 设置
        sio_keepalive = getattr(socket, "SIO_KEEPALIVE_VALS", None)
        ioctl = getattr(conn, "ioctl", None)
        if sio_keepalive is None or ioctl is None:
            return
        try:
            ioctl(
                sio_keepalive,
                (
                    1,
                    int(self._keepalive_idle) * 1000,
                    int(self._keepalive_interval) * 1000,
                ),
            )
        except OSError:
            pass

    def _read_client(self) -> None:
        client = self._client
        if client is None:
            return
        try:
            chunk = client.recv(_RECV_CHUNK)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._drop_client()
            return
        if not chunk:
            # 对端正常关闭：断线（不是读取超时）
            self._drop_client()
            return

        self._last_rx_time = time.monotonic()
        if self._idle_reported:
            self._idle_reported = False
            self.data_resumed.emit()
        if not self._stop_event.is_set():
            self.data_received.emit(chunk)

    # ------------------------------------------------------------------
    # 超时 / 断开
    # ------------------------------------------------------------------
    def _check_idle(self) -> None:
        """已连接但长时间无字节：上报一次 data_timeout，不判为断线。"""
        if self._client is None or self._idle_reported:
            return
        if self._data_timeout_s <= 0:
            return
        idle = time.monotonic() - self._last_rx_time
        if idle > self._data_timeout_s:
            self._idle_reported = True
            self.data_timeout.emit(float(idle))

    def _drop_client(self) -> None:
        """关闭当前客户端并回到监听态（允许设备自动重连）。"""
        had_client = self._client is not None
        self._close_client()
        if had_client:
            self._idle_reported = False
            self.connection_lost.emit()

    def _close_client(self) -> None:
        client = self._client
        self._client = None
        self._client_addr = ""
        if client is None:
            return
        try:
            client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            client.close()
        except OSError:
            pass

    def _close_listener(self) -> None:
        with self._lock:
            listener = self._listener
            self._listener = None
        if listener is None:
            return
        try:
            listener.close()
        except OSError:
            pass
