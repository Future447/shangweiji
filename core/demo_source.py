"""
演示模式数据源：模拟"无钢板标定 → 放入钢板信号衰减"实验（v3 毫伏语义）。

物理模型（与实验流程一致）：
    sample = air_baseline − metal_response + drift + noise
- 默认状态为【无钢板】：metal_response = 0，信号在空气基线附近，
  可先演示真实的标定操作顺序；
- 【放入钢板】后：钢板涡流使检波电压下降，衰减量（基线 − 当前）增大；
- 【移除钢板】后：信号回到基线附近（衰减量接近 0，不强制归零）。

特点：
- 不依赖硬件，可测试热力图/标定/CSV/曲线
- 与真实串口共用同一套 ChannelProcessor 链路（同样输出 mV DFrame）
- 内置基础噪声、缓慢基线漂移、可选偶发尖峰
- 所有样本量化为 0.125 mV 步进、限制在合法量程，与 v3 固件一致
  （此量化/限幅只作用于模拟数据，真实硬件数据绝不经过这里）
- 钢板响应幅度为非负约束（放入钢板只会使电压下降或不变）
- 只发 frame_ready，不模拟任何"串口连接成功"信号；UI 始终标注模拟数据
"""

from __future__ import annotations

import math
import random
import time
from typing import List, Optional, Tuple

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from models.channel_state import ChannelStatus
from models.frame import DFrame, MAX_CHANNEL_COUNT, MV_MAX, MV_MIN
from .channel_layout import map_to_grid_list
from .channel_processor import ChannelProcessor


# 4×4 默认映射：channel 0..15 -> (row, col)，统一引用
# core.channel_layout 的硬件实际接线布局（列优先、每列自下而上），
# 禁止再写一套独立行优先默认。
DEFAULT_MAP_4x4: List[Tuple[int, int]] = map_to_grid_list()


def default_enabled_4ch() -> List[int]:
    """
    4 启用通道：CH01/CH02/CH05/CH06。

    物理位置 = 4×4 阵列的【左下 2×2 区域】（实际接线的第 1、2 列
    底部各两个线圈，与原 coil_config 中 S00/S01/S10/S11 概念一致）。
    通道号索引不变（只声明物理选区，不重排数据）。
    """
    return [0, 1, 4, 5]


def default_enabled_16ch() -> List[int]:
    return list(range(16))


class DemoSource(QObject):
    """
    模拟数据源（明确的"无钢板/已放入钢板"状态机）。

    内部按用户配置的 enabled channels 逐个推送 D 帧。
    """

    frame_ready = pyqtSignal(object)  # DFrame

    def __init__(
        self,
        processor: ChannelProcessor,
        enabled_channels: Optional[List[int]] = None,
        map_to_grid: Optional[List[Tuple[int, int]]] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._proc = processor
        self._enabled = (
            list(enabled_channels)
            if enabled_channels is not None
            else default_enabled_4ch()
        )
        self._map = (
            list(map_to_grid)
            if map_to_grid is not None
            else DEFAULT_MAP_4x4
        )
        self._timer: Optional[QTimer] = None
        self._scan_number: int = 0
        self._t0: float = time.monotonic()
        # 全部为 mV 域常量（对应旧 count 域 ×0.125）
        self._noise_sigma: float = 1.5          # 旧 12 counts
        self._drift_per_min: float = 3.75       # 旧 30 counts/min
        self._spike_prob: float = 0.0
        self._plate_pos: Tuple[float, float] = (1.5, 1.5)  # 行/列坐标
        self._plate_radius: float = 1.2
        self._plate_amplitude: float = 187.5    # 旧 1500 counts，非负
        self._baseline_center: float = 1500.0   # 旧 12000 counts
        self._plate_inserted: bool = False      # 默认无钢板
        self._rng = random.Random(20260831)

    # ------------------------------------------------------------------
    def configure(
        self,
        enabled_channels: Optional[List[int]] = None,
        map_to_grid: Optional[List[Tuple[int, int]]] = None,
    ) -> None:
        if enabled_channels is not None:
            self._enabled = list(enabled_channels)
        if map_to_grid is not None:
            self._map = list(map_to_grid)

    # ---- 钢板状态（实验流程核心）----
    @property
    def plate_inserted(self) -> bool:
        """当前是否处于"已放入钢板"状态。"""
        return self._plate_inserted

    def set_plate_inserted(self, inserted: bool) -> None:
        """放入/移除钢板。移除后信号回到基线附近（含漂移与噪声）。"""
        self._plate_inserted = bool(inserted)

    def set_plate_pos(self, row: float, col: float) -> None:
        self._plate_pos = (float(row), float(col))

    def set_plate_radius(self, r: float) -> None:
        self._plate_radius = max(0.1, float(r))

    def set_plate_amplitude(self, a: float) -> None:
        """钢板响应幅度（mV，非负：放入钢板只使电压下降或不变）。"""
        self._plate_amplitude = max(0.0, float(a))

    # 兼容旧命名（metal == plate）
    def set_metal_pos(self, row: float, col: float) -> None:
        self.set_plate_pos(row, col)

    def set_metal_radius(self, r: float) -> None:
        self.set_plate_radius(r)

    def set_metal_amplitude(self, a: float) -> None:
        self.set_plate_amplitude(a)

    def set_noise_sigma(self, s: float) -> None:
        self._noise_sigma = max(0.0, float(s))

    def set_drift_per_min(self, d: float) -> None:
        self._drift_per_min = float(d)

    def set_spike_prob(self, p: float) -> None:
        self._spike_prob = max(0.0, min(1.0, float(p)))

    # ------------------------------------------------------------------
    def is_running(self) -> bool:
        return self._timer is not None

    def start(self, interval_ms: int = 200) -> None:
        if self.is_running():
            return
        self._t0 = time.monotonic()
        self._timer = QTimer(self)
        self._timer.setInterval(max(20, int(interval_ms)))
        self._timer.timeout.connect(self._emit_frame)
        self._timer.start()

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    # ------------------------------------------------------------------
    @staticmethod
    def _quantize_mv(v: float) -> float:
        """量化到 0.125 mV 步进并截断到量程（与固件一致）。

        仅用于模拟数据；真实硬件数据不经过此函数。
        """
        q = round(v / 0.125) * 0.125
        if q > MV_MAX:
            q = MV_MAX
        elif q < MV_MIN:
            q = MV_MIN
        return q

    def _emit_frame(self) -> None:
        t = time.monotonic() - self._t0
        drift = (t / 60.0) * self._drift_per_min
        samples: List[float] = []
        valid_mask = 0
        for ch in range(MAX_CHANNEL_COUNT):
            if ch not in self._enabled:
                # 未启用通道 → 标记无效（固件约定无效通道报 0.000）
                samples.append(0.0)
                continue
            row, col = self._map[ch] if ch < len(self._map) else (0, 0)
            air_baseline = self._baseline_center + drift
            # 钢板涡流响应：仅在已放入钢板时存在，高斯空间分布，非负
            if self._plate_inserted:
                pr, pc = self._plate_pos
                d2 = (row - pr) ** 2 + (col - pc) ** 2
                response = self._plate_amplitude * math.exp(
                    -d2 / (2.0 * self._plate_radius ** 2)
                )
            else:
                response = 0.0
            noise = self._rng.gauss(0.0, self._noise_sigma)
            spike = (
                self._rng.uniform(-375.0, 375.0)
                if self._rng.random() < self._spike_prob
                else 0.0
            )
            # sample = air_baseline − metal_response + drift + noise
            # （drift 已含在 air_baseline 中）
            mv_val = self._quantize_mv(
                air_baseline - response + noise + spike
            )
            samples.append(mv_val)
            valid_mask |= (1 << ch)

        self._scan_number += 1
        frame = DFrame(
            scan_number=self._scan_number,
            scan_status=0,
            valid_mask=valid_mask,
            i2c_error_count=0,
            samples_mv=tuple(samples),
        )
        self.frame_ready.emit(frame)


def moving_metal_trajectory(
    duration_s: float, fps: float = 5.0
) -> List[Tuple[float, float, float]]:
    """
    工具函数：生成一条钢板目标在 4x4 上移动的轨迹。
    返回 [(t, row, col), ...] 三元组（t 为秒）；调用方按 t 自行在
    setter 中插值使用。
    """
    pts: List[Tuple[float, float, float]] = []
    frames = max(1, int(duration_s * fps))
    for i in range(frames):
        t = i / fps
        # 来回折线：扫描一行，再回扫
        if (i // 4) % 2 == 0:
            r = (i % 4)
            c = (i // 4) % 4
        else:
            r = 3 - (i % 4)
            c = (i // 4) % 4
        pts.append((float(t), float(r), float(c)))
    return pts
