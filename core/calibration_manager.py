"""
无钢板基线标定管理器（帧驱动）。

实验语义：用户确认现场无钢板后启动；对每个启用通道累计
"无钢板"状态下的有效样本，全部达标后由 UI 原子提交新基线。
取消 / 失败 / 断连一律保留旧基线。

工作流程：
1. UI 触发 start(target_samples, target_frames, session_id)
   - target_samples: 每通道有效样本数（同一帧对每通道最多算 1 个）
   - target_frames: 至少接收多少个不同的 D 帧才完成
   - session_id: 只接受该会话的 D 帧（非当前会话的样本被剔除）
2. 每个新 D 帧到达时由 main_window 调用 on_frame()
3. on_frame() 检查：是否当前会话、是否新 D 帧（去重）、该通道
   是否有效、是否饱和、滤波值是否有限
4. 前 WARMUP_FRAMES 个新帧只用于滤波预热，不计入样本
   （避免切换数据源后的历史值/收敛过程污染基线）
5. 基线取滤波值中位数（稳健）；质量报告基于原始有效样本
   观察噪声（std/MAD）、漂移与异常点，避免 EMA 掩盖不稳定
6. 超时：max_idle_s 内无新帧 → 失败；有帧但通道始终无有效样本
   → 卡死早判或整体超时失败，失败原因明确列出通道

稳定性门限（集中配置，只在本文件出现一次；全部为软件自检门限，
不是硬件合格标准——是否达标需结合实际前端与实测数据另行评定；
门限取值一律为绝对 mV 与相对比例的较大者，避免近零分母）：
- STABILITY_MAX_CV_MAD = 0.05：无量纲比值 MAD/|median|，用原始
  （未滤波）有效样本计算。依据：静态无钢板环境下原始样本的相对
  散布应远小于 5%；该门限拦截"标定中条件变化/目标移动"造成的
  宽散布。
- 漂移门限 max(5 mV, 1%×|median|)：前后半段中位数之差。中位数
  对尖峰稳健，但台阶/持续漂移必然现形（MAD=0 的台阶也逃不掉）。
- 异常点比例 ≤ 20%：个别尖峰由中位数基线天然吸收，不判失败；
  占比更高说明信号本质不稳定。
- 末段观察窗 max(5, n/4)：末段 MAD 相对值（同 0.05）+ 末段
  中位数与候选基线的偏差（同漂移门限）——末段代表"标定完成
  时刻"的真实状态，台阶变化必须在此现形。
- 历史问题：曾有"注释 0.05、代码 0.5"的不一致；现以此常量为
  唯一来源，注释与代码不可能再分叉。
"""

from __future__ import annotations

import math
import time
from typing import Dict, List, Optional, Set, Tuple

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from .channel_processor import ChannelProcessor
from models.frame import MV_MAX, MV_MIN


# ---- 集中配置的标定常量（单位与依据见模块 docstring）----
# 全部为软件自检门限，不是硬件合格标准；门限取值均为绝对 mV 与
# 相对比例的较大者（避免基线接近 0 时比例门限被小分母放大）。
STABILITY_MAX_CV_MAD = 0.05      # MAD/|median|，原始有效样本域
# 前后半段漂移门限：漂移 = 后半中位数 − 前半中位数（中位数对
# 尖峰稳健，不被个别野值拉动）。绝对下限 5 mV ≈ 40 LSB，覆盖
# ADS1115 静态噪声与缓慢温漂；相对门限 1% 拦截高信号下的真实
# 条件变化（如标定中钢板/线圈移动）。
STABILITY_MAX_DRIFT_MV = 5.0
STABILITY_MAX_DRIFT_REL = 0.01
# 异常点比例上限：个别尖峰（占比 < 20%）由中位数基线天然吸收，
# 不判失败；占比更高说明信号本质不稳定（台阶/跳变），必须失败。
STABILITY_MAX_OUTLIER_RATIO = 0.20
# 单点异常判据的绝对下限（mV）：|x−median| > max(5×1.4826×MAD,
# OUTLIER_MIN_GATE_MV)。小样本 + 量化聚集时 MAD 会低估真实散布
# （如 MAD=0.25 mV 时 5σ̂≈1.9 mV，低于前端常态噪声 ±3σ≈4.5 mV，
# 正常噪声被误判为异常点）；下限 5 mV ≈ 40 LSB，低于此值的偏差
# 视为普通噪声而非"异常点"。
OUTLIER_MIN_GATE_MV = 5.0
# 末段观察窗：末尾 max(5, n//4) 个原始样本。末段代表"标定完成
# 时刻"的真实状态——台阶变化必须在此现形（整体 MAD=0 也逃不掉）。
TAIL_MIN_SAMPLES = 5
TAIL_FRACTION = 4
WARMUP_FRAMES = 2             # 滤波预热帧数（中值窗+EMA 收敛）
DEFAULT_MAX_IDLE_S = 10.0     # 无新 D 帧的停滞超时（秒）
DEFAULT_MAX_TOTAL_S = 180.0   # 标定整体超时（秒），防无限等待
# 每样本的时间预算（秒）：实机约 0.29 s/帧（~3.5 fps），每帧每通道
# 最多 1 个有效样本。整体超时按 target_samples × 该值放宽下限，
# 否则 UI 允许的大目标（如 1000）在 180 s 内数学上必然失败。
MAX_SECONDS_PER_SAMPLE = 0.35
STUCK_FRAME_FACTOR = 4        # 卡死早判：frames_seen >= factor × target_frames
MIN_STUCK_FRAMES = 30         # 卡死早判的最小帧数下限
# 卡死早判的帧数上限：实机 ~3.5 fps 下 150 帧 ≈ 43 s，必须落在
# 整体超时预算内——不设上限时大目标（4×200=800 帧 ≈ 229 s > 180 s）
# 会让"卡死早判"永远不可达，用户只能干等到整体超时。
MAX_STUCK_FRAMES = 150


def _ch_label(c: int) -> str:
    return f"CH{c + 1:02d}"


class CalibrationManager(QObject):
    """在主线程中由 D 帧驱动推进，避免 UI 定时器重复计数。"""

    def __init__(
        self,
        processor: ChannelProcessor,
        parent=None,
        *,
        stability_max_cv_mad: float = STABILITY_MAX_CV_MAD,
        warmup_frames: int = WARMUP_FRAMES,
    ) -> None:
        super().__init__(parent)
        self._proc = processor
        self._stability_max_cv_mad = float(stability_max_cv_mad)
        self._warmup_frames = max(0, int(warmup_frames))
        self._timer: Optional[QTimer] = None
        self._enabled: List[int] = []
        self._target_samples: int = 0
        self._target_frames: int = 0
        self._max_idle_s: float = DEFAULT_MAX_IDLE_S
        self._max_total_s: float = DEFAULT_MAX_TOTAL_S
        self._session_id: Optional[str] = None
        self._current: int = 0
        self._frames_seen: int = 0
        self._warmup_seen: int = 0
        self._per_channel_count: Dict[int, int] = {}
        # 滤波值（基线候选）与原始有效样本（质量观察）分开累计
        self._accumulator: Dict[int, List[float]] = {}
        self._raw_accumulator: Dict[int, List[float]] = {}
        self._channel_quality: Dict[int, Dict[str, float]] = {}
        self._seen_frame_keys: Set[int] = set()
        self._last_scan: Optional[int] = None
        self._last_new_frame_monotonic: float = 0.0
        self._start_monotonic: float = 0.0
        self._cancelled: bool = False

    # ------------------------------------------------------------------
    progress = pyqtSignal(int, int, int, int)
    # current, target, frames_seen, target_frames
    finished = pyqtSignal(dict)            # {channel: baseline}
    cancelled = pyqtSignal()
    failed = pyqtSignal(str)               # 失败原因（含失败通道列表）
    quality_report = pyqtSignal(dict)      # {channel: 原始域质量统计}

    def is_running(self) -> bool:
        return self._timer is not None

    def state_snapshot(self) -> dict:
        """给 UI 显示用：当前进度、质量、各通道样本数。"""
        return {
            "running": self.is_running(),
            "current_samples": self._current,
            "target_samples": self._target_samples,
            "frames_seen": self._frames_seen,
            "target_frames": self._target_frames,
            "warmup_remaining": max(
                0, self._warmup_frames - self._warmup_seen
            ),
            "per_channel": dict(self._per_channel_count),
            "elapsed_s": (
                time.monotonic() - self._start_monotonic
                if self._start_monotonic > 0 else 0.0
            ),
            "max_idle_s": self._max_idle_s,
            "max_total_s": self._max_total_s,
            "last_new_frame_age_s": (
                time.monotonic() - self._last_new_frame_monotonic
                if self._last_new_frame_monotonic > 0 else 0.0
            ),
        }

    # ------------------------------------------------------------------
    def start(
        self,
        target_samples: int = 200,
        target_frames: Optional[int] = None,
        max_idle_s: float = DEFAULT_MAX_IDLE_S,
        max_total_s: Optional[float] = None,
        session_id: Optional[str] = None,
    ) -> None:
        """开始无钢板基线标定。

        - target_samples: 每通道需累计的有效样本数
        - target_frames: 至少多少个不同的 D 帧（默认 = max(2, target_samples)）
        - max_idle_s: 超过该秒数无新 D 帧则失败
        - max_total_s: 整体超时（有帧但样本凑不齐也失败）；
          None 时按 max(DEFAULT_MAX_TOTAL_S, target_samples ×
          MAX_SECONDS_PER_SAMPLE) 随目标放大——任何 UI 可选目标
          在实机帧率下都必须可达，不得设一个数学上必然失败的预算
        - session_id: 只累计该会话的 D 帧；None 表示不做会话门槛
        """
        if self.is_running():
            self.failed.emit("已有标定正在进行")
            return
        if target_samples <= 0:
            self.failed.emit("目标样本数必须 > 0")
            return

        self._enabled = list(self._proc.enabled_channels)
        if not self._enabled:
            self.failed.emit("当前没有启用任何通道")
            return

        self._accumulator = {c: [] for c in self._enabled}
        self._raw_accumulator = {c: [] for c in self._enabled}
        self._per_channel_count = {c: 0 for c in self._enabled}
        self._channel_quality = {c: {} for c in self._enabled}
        self._target_samples = int(target_samples)
        self._target_frames = int(
            target_frames if target_frames is not None
            else max(2, self._target_samples)
        )
        self._current = 0
        self._frames_seen = 0
        self._warmup_seen = 0
        self._seen_frame_keys.clear()
        self._last_scan = None
        self._cancelled = False
        self._max_idle_s = max(3.0, float(max_idle_s))
        if max_total_s is None:
            max_total_s = max(
                DEFAULT_MAX_TOTAL_S,
                self._target_samples * MAX_SECONDS_PER_SAMPLE,
            )
        self._max_total_s = max(self._max_idle_s, float(max_total_s))
        self._session_id = session_id
        self._start_monotonic = time.monotonic()
        self._last_new_frame_monotonic = 0.0

        # 清空旧滤波：标定从空开始，避免历史值污染基线
        self._proc.reset_filter_only()

        # 启动 idle watchdog（仅用于超时检测，不用于驱动进度）
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._on_watchdog)
        self._timer.start()
        self.progress.emit(0, self._target_samples,
                           0, self._target_frames)

    def cancel(self) -> None:
        if not self.is_running():
            return
        self._cancelled = True
        self._stop_timer()
        self.cancelled.emit()

    # ------------------------------------------------------------------
    def on_frame(
        self, dframe, session_id: Optional[str] = None
    ) -> None:
        """每个新 D 帧到达时由 main_window 调用。

        门槛：会话匹配、新帧去重、通道有效、不饱和、滤波值有限；
        前 WARMUP_FRAMES 帧只预热滤波不计数。
        """
        if not self.is_running() or self._cancelled:
            return
        if dframe is None:
            return
        # 非当前会话的样本剔除
        if self._session_id is not None and session_id != self._session_id:
            return

        scan = getattr(dframe, "scan_number", None)
        # 设备中途复位（scan_number 回退，如固件看门狗重启）：
        # 清空去重缓存——否则复位后的小号 scan 全部被误判为重复帧
        # 丢弃，看门狗随后误报"无新 D 帧"，用户无从得知真正原因
        if (
            scan is not None
            and self._last_scan is not None
            and scan < self._last_scan
        ):
            self._seen_frame_keys.clear()
        if scan is not None:
            self._last_scan = scan
        if scan in self._seen_frame_keys:
            return
        self._seen_frame_keys.add(scan)

        self._frames_seen += 1
        self._last_new_frame_monotonic = time.monotonic()

        # 滤波预热：前若干帧只推进 EMA/中值，不进入标定累计
        if self._warmup_seen < self._warmup_frames:
            self._warmup_seen += 1
            self.progress.emit(
                self._current, self._target_samples,
                self._frames_seen, self._target_frames,
            )
            return

        # 该帧对每个有效 enabled 通道贡献 1 个样本
        for c in self._enabled:
            if not dframe.is_valid(c):
                continue
            raw_mv = float(dframe.samples_mv[c])
            # 非有限与饱和样本不进入标定
            if not math.isfinite(raw_mv):
                continue
            if raw_mv <= MV_MIN or raw_mv >= MV_MAX:
                continue
            st = self._proc.get(c)
            if st.filtered_mv is None or not math.isfinite(st.filtered_mv):
                continue
            # 滤波未（重）建立完成的通道不计入：预热期的中值/EMA
            # 不代表稳定物理量（如启动收敛、饱和恢复过程）。
            # getattr 兜底：鸭子类型的伪状态没有该属性时按已稳定处理
            if not getattr(st, "filter_settled", True):
                continue
            self._raw_accumulator[c].append(raw_mv)
            self._accumulator[c].append(float(st.filtered_mv))
            self._per_channel_count[c] += 1

        min_count = min(self._per_channel_count.values()) \
            if self._per_channel_count else 0
        self._current = min_count
        self.progress.emit(
            self._current, self._target_samples,
            self._frames_seen, self._target_frames,
        )

        if self._current >= self._target_samples and \
                self._frames_seen >= self._target_frames:
            self._finalize()
        else:
            # 卡死早判：帧一直在来，但有通道始终无有效样本。
            # 上限 MAX_STUCK_FRAMES 保证门槛在整体超时预算内可达
            # （否则大目标下早判分支为死代码）。
            stuck_limit = max(
                MIN_STUCK_FRAMES,
                min(
                    STUCK_FRAME_FACTOR * self._target_frames,
                    MAX_STUCK_FRAMES,
                ),
            )
            if self._frames_seen >= stuck_limit:
                zero_ch = [
                    c for c in self._enabled
                    if self._per_channel_count.get(c, 0) == 0
                ]
                if zero_ch:
                    names = ", ".join(_ch_label(c) for c in zero_ch)
                    self._fail(
                        f"已收到 {self._frames_seen} 帧，但通道 "
                        f"{names} 始终无有效样本（无效/饱和/超出量程）"
                    )

    # ------------------------------------------------------------------
    def _on_watchdog(self) -> None:
        """仅用于检测停滞与整体超时。"""
        if not self.is_running() or self._cancelled:
            return
        now = time.monotonic()
        # 整体超时：有帧但样本永远凑不齐，明确列出失败通道
        if now - self._start_monotonic > self._max_total_s:
            lacking = [
                f"{_ch_label(c)} {self._per_channel_count.get(c, 0)}"
                f"/{self._target_samples}"
                for c in self._enabled
                if self._per_channel_count.get(c, 0) < self._target_samples
            ]
            detail = ", ".join(lacking) if lacking else "帧数不足"
            self._fail(
                f"标定整体超时（>{self._max_total_s:.0f}s）：{detail}"
            )
            return
        if self._last_new_frame_monotonic == 0.0:
            if now - self._start_monotonic > self._max_idle_s:
                self._fail(f"{self._max_idle_s:.0f}s 内无新 D 帧")
            return
        if now - self._last_new_frame_monotonic > self._max_idle_s:
            self._fail(f"已 {self._max_idle_s:.0f}s 未收到新 D 帧")

    def _fail(self, reason: str) -> None:
        self._cancelled = True
        self._stop_timer()
        self.failed.emit(reason)

    @staticmethod
    def _median(xs: List[float]) -> float:
        """标准中位数：偶数样本取两中值平均（不取上中位值）。

        基线与 MAD 都经此入口；取上中位值会让基线相对真实中位水平
        系统性偏高半个间距，所有后续衰减量随之偏移。
        """
        s = sorted(xs)
        n = len(s)
        mid = n // 2
        if n % 2:
            return s[mid]
        return (s[mid - 1] + s[mid]) / 2.0

    def _finalize(self) -> None:
        if self._cancelled:
            return
        baselines: Dict[int, float] = {}
        quality: Dict[int, Dict[str, float]] = {}
        for c in self._enabled:
            samples = self._accumulator.get(c) or []
            raw_samples = self._raw_accumulator.get(c) or []
            if not samples or not raw_samples:
                self._fail(f"通道 {_ch_label(c)} 无有效样本")
                return

            # ---- 基线：滤波值中位数（稳健）----
            filt_median = self._median(samples)

            # ---- 质量观察：原始有效样本域（不被 EMA 掩盖）----
            n = len(raw_samples)
            mean = sum(raw_samples) / n
            raw_median = self._median(raw_samples)
            abs_dev = sorted(abs(x - raw_median) for x in raw_samples)
            mad = self._median(abs_dev)
            var = sum((x - mean) ** 2 for x in raw_samples) / max(1, n - 1)
            std = math.sqrt(var)
            # 漂移：后半中位数 − 前半中位数（mV）。
            # 用中位数而非均值：个别尖峰不会拉动半段代表水平，
            # 但台阶/持续漂移必然现形（MAD=0 的台阶也逃不掉）。
            half = max(1, n // 2)
            first_median = self._median(raw_samples[:half])
            last_median = self._median(raw_samples[-half:])
            drift = last_median - first_median
            # 异常点：|x - median| > max(5 × 1.4826 × MAD,
            # OUTLIER_MIN_GATE_MV)。MAD 是稳健散布估计，但小样本 +
            # 量化聚集时会低估真实噪声——绝对下限保证正常前端噪声
            # 不被误判为异常点（见常量注释）
            outlier_gate = max(5.0 * 1.4826 * mad, OUTLIER_MIN_GATE_MV)
            outliers = sum(
                1 for x in raw_samples
                if abs(x - raw_median) > outlier_gate
            )
            outlier_ratio = outliers / n
            # 末段观察窗：代表"标定完成时刻"的真实状态。
            # 台阶变化会让整体 MAD=0 照常通过，必须在末段现形。
            tail_n = min(n, max(TAIL_MIN_SAMPLES, n // TAIL_FRACTION))
            tail = raw_samples[-tail_n:]
            tail_median = self._median(tail)
            tail_mad = self._median(
                sorted(abs(x - tail_median) for x in tail)
            )
            denom = max(abs(raw_median), 1.0)
            cv_mad = mad / denom
            tail_cv = tail_mad / max(abs(tail_median), 1.0)
            tail_dev = abs(tail_median - filt_median)
            # 门限一律取绝对 mV 与相对比例的较大者，
            # 避免基线接近 0 时比例门限被小分母放大
            drift_gate = max(
                STABILITY_MAX_DRIFT_MV,
                STABILITY_MAX_DRIFT_REL * abs(raw_median),
            )
            tail_gate = max(
                STABILITY_MAX_DRIFT_MV,
                STABILITY_MAX_DRIFT_REL * abs(filt_median),
            )
            if cv_mad > self._stability_max_cv_mad:
                self._fail(
                    f"通道 {_ch_label(c)} 基线不稳定 "
                    f"(原始样本 MAD/|median|={cv_mad:.3f} > "
                    f"{self._stability_max_cv_mad:.2f})；"
                    f"请保持无钢板状态稳定后重试"
                )
                return
            if abs(drift) > drift_gate:
                self._fail(
                    f"通道 {_ch_label(c)} 标定过程中信号漂移："
                    f"前后半段中位数差 {drift:+.2f} mV "
                    f"（门限 {drift_gate:.2f} mV）；实验条件可能在"
                    f"标定中变化（钢板/线圈位置/激励），请恢复"
                    f"无钢板稳定状态后重新标定"
                )
                return
            if outlier_ratio > STABILITY_MAX_OUTLIER_RATIO:
                self._fail(
                    f"通道 {_ch_label(c)} 异常点过多：{outliers}/{n} "
                    f"（{outlier_ratio * 100:.0f}% > "
                    f"{STABILITY_MAX_OUTLIER_RATIO * 100:.0f}%）；"
                    f"信号存在频繁跳变，请检查接线与激励后重新标定"
                )
                return
            if tail_cv > self._stability_max_cv_mad:
                self._fail(
                    f"通道 {_ch_label(c)} 末段不稳定：末尾 {tail_n} 个"
                    f"样本 MAD/|median|={tail_cv:.3f} > "
                    f"{self._stability_max_cv_mad:.2f}；"
                    f"请待信号稳定后重新标定"
                )
                return
            if tail_dev > tail_gate:
                self._fail(
                    f"通道 {_ch_label(c)} 末段水平与候选基线不一致："
                    f"末段中位数 {tail_median:.2f} mV vs 候选基线 "
                    f"{filt_median:.2f} mV（偏差 {tail_dev:.2f} > "
                    f"{tail_gate:.2f} mV）；信号在标定过程中发生台阶"
                    f"变化，请保持无钢板状态不变后重新标定"
                )
                return
            quality[c] = {
                "mean": mean, "median": raw_median,
                "std": std, "mad": mad, "n": n,
                "cv_mad": cv_mad,
                "drift_mv": drift,
                "outliers": outliers,
                "outlier_ratio": outlier_ratio,
                "tail_median": tail_median,
                "tail_mad": tail_mad,
                "baseline_mv": filt_median,
            }
            baselines[c] = float(filt_median)
        self._stop_timer()
        self.quality_report.emit(quality)
        self.finished.emit(baselines)

    def _stop_timer(self) -> None:
        if self._timer is not None:
            try:
                self._timer.stop()
            except Exception:
                pass
            self._timer = None
