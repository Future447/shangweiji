"""16 路原始 mV 的空场差分及固定直板边位置模板匹配。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from numbers import Real
from typing import Any, Mapping, Optional, Tuple

import numpy as np

from models.frame import MAX_CHANNEL_COUNT, MV_STEP


@dataclass(frozen=True)
class ArrayAnalysisResult:
    """位置单位为中心距 p，score 为逐通道加权残差的 RMS。"""

    delta: np.ndarray
    position: Optional[float] = None
    range: Optional[Tuple[float, float]] = None
    score: Optional[float] = None
    rejected: bool = False
    ambiguous: bool = False
    ranges: Tuple[Tuple[float, float], ...] = ()
    candidate_range: Optional[Tuple[float, float]] = None


def _finite_scalar(value: Any, name: str) -> float:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ValueError(f"{name}必须为有限数值。")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name}必须为有限数值。")
    return result


def _numeric_array(values: Any, name: str) -> np.ndarray:
    try:
        array = np.asarray(values)
        if array.dtype.kind not in "iuf":
            raise ValueError(f"{name}必须为数值数组。")
        array = array.astype(float, copy=True)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name}必须为规则数值数组。") from error
    return array


def _frames(values: Any) -> np.ndarray:
    array = _numeric_array(values, "采样帧")
    if array.shape == (MAX_CHANNEL_COUNT,):
        array = array.reshape(1, MAX_CHANNEL_COUNT)
    if array.ndim != 2 or array.shape[1] != MAX_CHANNEL_COUNT or not len(array):
        raise ValueError("采样帧必须为非空 N×16 数组。")
    if not np.isfinite(array).all():
        raise ValueError("采样帧包含缺失或非有限数值。")
    return array


def _frame(values: Any) -> np.ndarray:
    array = _numeric_array(values, "当前帧")
    if array.shape != (MAX_CHANNEL_COUNT,) or not np.isfinite(array).all():
        raise ValueError("当前帧必须包含 16 个有限数值。")
    return array


def spatial_differences(delta_grid: Any) -> Tuple[np.ndarray, np.ndarray]:
    """返回水平右减左 (4×3)、垂直下减上 (3×4)，NaN 原样传播。"""
    grid = _numeric_array(delta_grid, "网格")
    if grid.shape != (4, 4) or np.isinf(grid).any():
        raise ValueError("网格须为 4×4，可包含 NaN，须无无穷大。")
    try:
        with np.errstate(over="raise", invalid="ignore"):
            return np.diff(grid, axis=1), np.diff(grid, axis=0)
    except FloatingPointError as error:
        raise ValueError("数值范围过大，无法计算网格差分。") from error


def coverage_grid(
    position_range: Optional[Tuple[float, float]], direction: str
) -> np.ndarray:
    """四个等宽网格中的几何覆盖率，兼容位置有分歧处为 NaN。

    p=0 是进入侧最外列/行中心向外半个中心距的网格边界。
    """
    if direction not in ("left", "right", "top", "bottom"):
        raise ValueError("进入方向须为 left、right、top 或 bottom。")
    if position_range is None:
        return np.full((4, 4), np.nan)
    endpoints = _numeric_array(position_range, "位置范围")
    if endpoints.shape != (2,):
        raise ValueError("位置范围须包含两个有限端点。")
    low = _finite_scalar(endpoints[0], "位置下界")
    high = _finite_scalar(endpoints[1], "位置上界")
    if low > high:
        raise ValueError("位置下界不能大于上界。")
    row, column = np.indices((4, 4))
    offsets = {"left": column, "right": 3 - column, "top": row, "bottom": 3 - row}[direction]
    first = np.clip(low - offsets, 0.0, 1.0)
    last = np.clip(high - offsets, 0.0, 1.0)
    return np.where(np.isclose(first, last, rtol=0.0, atol=1e-10), first, np.nan)


def compensate_common_response(
    attenuation: np.ndarray, coverage: np.ndarray, gains: Optional[np.ndarray] = None,
) -> Tuple[np.ndarray, Optional[float], int]:
    """用明确未覆盖的格子估计公共响应，至少需要一排线圈数量的参考格。"""
    references = attenuation[(coverage == 0.0) & np.isfinite(attenuation)]
    count = len(references)
    if count < 4:
        return np.full_like(attenuation, np.nan, dtype=float), None, count
    common_mv = float(np.median(references))
    return attenuation - (common_mv if gains is None else gains * common_mv), common_mv, count


class ArrayAnalysis:
    """固定工况模板，以每个停点的原始帧保留噪声及标定信息。

    noise_floor_mv 为加权时的标准差下限，默认采用协议的一步
    MV_STEP。该数值用于防止零方差通道权重失控，阈值需独立设定。
    """

    def __init__(self, noise_floor_mv: float = MV_STEP) -> None:
        floor = _finite_scalar(noise_floor_mv, "噪声下限")
        if floor <= 0 or not math.isfinite(floor * floor) or floor * floor == 0:
            raise ValueError("噪声下限须为可计算方差的正数。")
        self.noise_floor_mv = floor
        self.baseline: Optional[np.ndarray] = None
        self.session_baseline: Optional[np.ndarray] = None
        self._templates: dict[float, np.ndarray] = {}
        self._positions: Tuple[float, ...] = ()
        self._centers = np.empty((0, MAX_CHANNEL_COUNT))
        self.pooled_variance = np.zeros(MAX_CHANNEL_COUNT)
        self.variance = np.full(MAX_CHANNEL_COUNT, floor * floor)

    @property
    def positions(self) -> Tuple[float, ...]:
        return self._positions

    @property
    def has_model(self) -> bool:
        return self.baseline is not None and len(self._positions) >= 2

    def set_baseline(self, frames: Any) -> None:
        """以每通道中位数设空场，成功后清除全部位置模板。"""
        self.baseline = self._measure_baseline(frames)
        self.clear_session_baseline()
        self._commit_templates({})

    @staticmethod
    def _measure_baseline(frames: Any) -> np.ndarray:
        samples = _frames(frames)
        try:
            with np.errstate(over="raise", invalid="raise"):
                baseline = np.median(samples, axis=0)
        except FloatingPointError as error:
            raise ValueError("采样数值范围过大，无法计算空场。") from error
        baseline.setflags(write=False)
        return baseline

    def set_session_baseline(self, frames: Any) -> None:
        """重采当前空场，保留模板及其原始标定参考。"""
        if not self.has_model:
            raise ValueError("请先加载或完成至少两个位置的标定模板。")
        self.session_baseline = self._measure_baseline(frames)

    def clear_session_baseline(self) -> None:
        self.session_baseline = None

    def _commit_templates(self, templates: Mapping[float, np.ndarray]) -> None:
        """全部计算成功后才替换缓存，坏停点不影响已存在的模型。"""
        positions = tuple(sorted(templates))
        centers = []
        variance_sum = np.zeros(MAX_CHANNEL_COUNT)
        degrees = 0
        try:
            with np.errstate(over="raise", invalid="raise", divide="raise"):
                for position in positions:
                    frames = templates[position]
                    centers.append(np.median(frames, axis=0) - self.baseline)
                    if len(frames) > 1:
                        variance_sum += np.var(frames, axis=0, ddof=1) * (len(frames) - 1)
                        degrees += len(frames) - 1
                pooled = variance_sum / degrees if degrees else variance_sum
                variance = np.maximum(pooled, self.noise_floor_mv ** 2)
        except FloatingPointError as error:
            raise ValueError("采样数值范围过大，无法建立模板。") from error
        self._templates = dict(templates)
        self._positions = positions
        self._centers = np.asarray(centers).reshape(-1, MAX_CHANNEL_COUNT)
        self.pooled_variance = pooled
        self.variance = variance

    def add_template(self, position: float, frames: Any) -> bool:
        """加入停点。位置相同时以本次帧窗口替换，返回是否发生替换。"""
        if self.baseline is None:
            raise ValueError("请先设置空场。")
        position = _finite_scalar(position, "模板位置")
        samples = _frames(frames)
        if self.session_baseline is not None:
            try:
                with np.errstate(over="raise", invalid="raise"):
                    samples = (samples - self.session_baseline) + self.baseline
            except FloatingPointError as error:
                raise ValueError("采样数值范围过大，无法换算到模板参考空场。") from error
        replaced = position in self._templates
        templates = {**self._templates, position: samples}
        self._commit_templates(templates)
        return replaced

    def remove_template(self, position: float) -> bool:
        position = _finite_scalar(position, "模板位置")
        if position not in self._templates:
            return False
        templates = dict(self._templates)
        del templates[position]
        self._commit_templates(templates)
        return True

    def common_response_gains(self, channels: np.ndarray, direction: str) -> np.ndarray:
        """从各路确定未覆盖的停点估算公共响应比例，样本不足的通道沿用等比例补偿。"""
        product = np.zeros((4, 4))
        energy = np.zeros((4, 4))
        counts = np.zeros((4, 4), dtype=int)
        for position, center in zip(self._positions, self._centers):
            attenuation = -center[channels]
            coverage = coverage_grid((position, position), direction)
            _, common, _ = compensate_common_response(attenuation, coverage)
            if common is None or abs(common) < 8 * self.noise_floor_mv:
                continue
            uncovered = coverage == 0.0
            product[uncovered] += common * attenuation[uncovered]
            energy[uncovered] += common ** 2
            counts[uncovered] += 1
        return np.divide(product, energy, out=np.ones((4, 4)), where=(counts >= 2))

    def analyze(self, frame: Any, threshold: Optional[float] = None) -> ArrayAnalysisResult:
        """匹配全部相邻线段，阈值留空时只返回试算结果。"""
        sample = _frame(frame)
        if threshold is not None:
            threshold = _finite_scalar(threshold, "残差阈值")
            if threshold < 0:
                raise ValueError("残差阈值须为非负数或留空。")
        if self.baseline is None:
            return ArrayAnalysisResult(delta=np.full(MAX_CHANNEL_COUNT, np.nan))
        try:
            with np.errstate(over="raise", invalid="raise"):
                reference = self.baseline if self.session_baseline is None else self.session_baseline
                delta = sample - reference
        except FloatingPointError as error:
            raise ValueError("数值范围过大，无法计算空场差分。") from error
        if not self.has_model:
            return ArrayAnalysisResult(delta=delta)
        candidates = []
        try:
            with np.errstate(over="raise", invalid="raise", divide="raise"):
                scale = np.sqrt(self.variance)
                residuals = (delta - self._centers) / scale
                distances = np.sum(residuals ** 2, axis=1)
                candidates.extend((float(score), (position, position)) for score, position in zip(distances, self._positions))
                for index in range(len(self._positions) - 1):
                    first, second = self._positions[index:index + 2]
                    direction = (self._centers[index + 1] - self._centers[index]) / scale
                    length_squared = float(np.dot(direction, direction))
                    if length_squared == 0.0:
                        candidates.append((float(distances[index]), (first, second)))
                        continue
                    fraction = float(np.clip(np.dot(direction, residuals[index]) / length_squared, 0.0, 1.0))
                    error = residuals[index] - fraction * direction
                    position = first + fraction * (second - first)
                    if not math.isfinite(position):
                        raise ValueError("位置范围过大，无法插值。")
                    candidates.append((float(np.dot(error, error)), (position, position)))
        except FloatingPointError as error:
            raise ValueError("数值范围过大，无法计算模板距离。") from error
        best = min(score for score, _ in candidates)
        tolerance = 1e-12 * max(1.0, best)
        matches = sorted(interval for score, interval in candidates if score <= best + tolerance)
        ranges: list[Tuple[float, float]] = []
        for low, high in matches:
            if ranges and low <= ranges[-1][1] + 1e-10:
                ranges[-1] = (ranges[-1][0], max(high, ranges[-1][1]))
            else:
                ranges.append((low, high))
        compatible = (ranges[0][0], ranges[-1][1])
        ambiguous = len(ranges) > 1 or compatible[1] - compatible[0] > 1e-10
        score = math.sqrt(best / MAX_CHANNEL_COUNT)
        rejected = threshold is not None and score > threshold
        return ArrayAnalysisResult(
            delta=delta,
            position=None if rejected or ambiguous else compatible[0],
            range=None if rejected else compatible,
            score=score,
            rejected=rejected,
            ambiguous=ambiguous,
            ranges=tuple(ranges),
            candidate_range=compatible,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "noise_floor_mv": self.noise_floor_mv,
            "baseline": None if self.baseline is None else self.baseline.tolist(),
            "templates": [
                {"position": position, "frames": self._templates[position].tolist()}
                for position in self._positions
            ],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ArrayAnalysis":
        """建立新对象后返回，调用方可在验证失败时保留旧标定。

        兼容未标注 version 的同结构数据及早期 samples 模板字段。
        """
        if not isinstance(payload, Mapping) or "baseline" not in payload:
            raise ValueError("标定文件缺少空场字段。")
        version = payload.get("version", 1)
        if type(version) is not int or version != 1:
            raise ValueError("不支持此阵列标定版本。")
        templates = payload.get("templates", payload.get("samples", []))
        if not isinstance(templates, list):
            raise ValueError("位置模板须为数组。")
        engine = cls(payload.get("noise_floor_mv", MV_STEP))
        baseline = payload["baseline"]
        if baseline is None:
            if templates:
                raise ValueError("有位置模板时必须包含有效空场。")
            return engine
        engine.baseline = _frame(baseline)
        engine.baseline.setflags(write=False)
        positions = set()
        for template in templates:
            if not isinstance(template, Mapping) or "position" not in template or "frames" not in template:
                raise ValueError("停点须包含 position 和 frames。")
            position = _finite_scalar(template["position"], "模板位置")
            if position in positions:
                raise ValueError("标定文件中的位置重复。")
            positions.add(position)
            engine.add_template(position, template["frames"])
        return engine
