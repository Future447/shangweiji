"""良品参考差分及稳健异常分数，输入、参考和残差均采用原始 mV。

本模块输出筛查特征。默认分数阈值需要用独立扫查验证。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from numbers import Real
from typing import Any, Mapping, Optional

import numpy as np

from models.frame import MAX_CHANNEL_COUNT, MV_STEP


DEFAULT_INSPECTION_THRESHOLD = 5.0
_MAD_NORMAL_SCALE = 1.482602218505602


def _scalar(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ValueError(f"{name}须为有限数值。")
    try:
        result = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError(f"{name}须为有限数值。") from error
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError(f"{name}须为有限{'正' if positive else ''}数值。")
    return result


def _array(values: Any, name: str, *, missing: bool = False, invalid_as_nan: bool = False) -> np.ndarray:
    """数据入口可标记缺测；配置入口仅接受有限数值及明确的 null。"""
    try:
        source = np.asarray(values, dtype=object)
        converted = []
        for value in source.flat:
            if value is None and missing:
                converted.append(np.nan)
                continue
            if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
                raise ValueError(f"{name}包含非数值。")
            number = float(value)
            if not math.isfinite(number):
                if not invalid_as_nan:
                    raise ValueError(f"{name}包含非有限数值。")
                number = np.nan
            converted.append(number)
        return np.asarray(converted, dtype=float).reshape(source.shape)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name}须为规则数值数组。") from error


@dataclass(frozen=True)
class InspectionResult:
    residual_mv: np.ndarray
    scores: np.ndarray
    hit_mask: np.ndarray
    valid_mask: np.ndarray
    quality: str
    reference_mv: np.ndarray
    scale_mv: np.ndarray
    threshold: Optional[float]


class InspectionReference:
    """每通道中位数参考及正态尺度 MAD。

    位置参考按完全相同的实测/重采样坐标分组。每路整体至少需要
    3 个有效样本，否则该路不参与分析。每位置不足 3 个有效重复时，
    尺度回退到其他重复位置的组内 MAD，再回退到 noise_floor_mv。
    跨位置的正常几何变化不用于估计噪声。
    相邻参考位置间采用中心和尺度的线性插值，插值误差需独立验证。
    """

    def __init__(self, centers, scales, counts, positions, noise_floor_mv):
        self.noise_floor_mv = noise_floor_mv
        self._centers = np.array(centers, dtype=float, copy=True)
        self._scales = np.array(scales, dtype=float, copy=True)
        self._counts = np.array(counts, dtype=np.int64, copy=True)
        self._positions = None if positions is None else tuple(float(value) for value in positions)
        for array in (self._centers, self._scales, self._counts):
            array.setflags(write=False)

    @property
    def has_positions(self) -> bool:
        return self._positions is not None

    @property
    def positions(self) -> tuple[float, ...]:
        return self._positions or ()

    @property
    def valid_channels(self) -> np.ndarray:
        return self._counts.sum(axis=0) >= 3

    @property
    def noise_supported(self) -> np.ndarray:
        """该路是否有至少一个位置的 3 个有效重复支持 MAD。"""
        return np.any(self._counts >= 3, axis=0)

    @classmethod
    def fit(cls, frames: Any, positions: Any = None, *, noise_floor_mv: float = MV_STEP) -> "InspectionReference":
        floor = _scalar(noise_floor_mv, "噪声尺度下限", positive=True)
        if not math.isfinite(floor * floor) or floor * floor == 0:
            raise ValueError("噪声尺度下限超出可计算范围。")
        samples = _array(frames, "良品采样", missing=True, invalid_as_nan=True)
        if samples.ndim != 2 or samples.shape[1] != MAX_CHANNEL_COUNT or not len(samples):
            raise ValueError("良品采样须为非空 N×16 数组。")
        coordinates = None
        groups = [samples]
        if positions is not None:
            values = _array(positions, "实测位置")
            if values.shape != (len(samples),):
                raise ValueError("每个良品帧须对应一个实测位置。")
            order = np.argsort(values, kind="stable")
            sorted_positions = values[order]
            boundaries = np.flatnonzero(sorted_positions[1:] != sorted_positions[:-1]) + 1
            groups = np.split(samples[order], boundaries)
            coordinates = np.unique(sorted_positions)
        counts = np.stack([np.isfinite(group).sum(axis=0) for group in groups])
        available = counts.sum(axis=0) >= 3
        if not np.any(available):
            raise ValueError("至少一路需要 3 个有效良品样本。")
        centers = np.full(counts.shape, np.nan)
        scales = np.full(counts.shape, np.nan)
        pooled_deviations = [[] for _ in range(MAX_CHANNEL_COUNT)]
        try:
            with np.errstate(over="raise", invalid="raise"):
                for group_index, group in enumerate(groups):
                    for channel in np.flatnonzero(available):
                        values = group[np.isfinite(group[:, channel]), channel]
                        if not len(values):
                            continue
                        center = float(np.median(values))
                        centers[group_index, channel] = center
                        if len(values) >= 3:
                            deviations = np.abs(values - center)
                            scales[group_index, channel] = _MAD_NORMAL_SCALE * np.median(deviations)
                            pooled_deviations[channel].append(deviations)
                for channel in np.flatnonzero(available):
                    deviations = pooled_deviations[channel]
                    pooled = _MAD_NORMAL_SCALE * np.median(np.concatenate(deviations)) if deviations else floor
                    known = np.isfinite(centers[:, channel])
                    scales[known, channel] = np.maximum(
                        np.where(np.isfinite(scales[known, channel]), scales[known, channel], pooled), floor
                    )
        except FloatingPointError as error:
            raise ValueError("良品数值范围过大，无法计算中位数或 MAD。") from error
        return cls(centers, scales, counts, coordinates, floor)

    def analyze(self, frame: Any, position: Optional[float] = None, *, threshold: Optional[float] = DEFAULT_INSPECTION_THRESHOLD) -> InspectionResult:
        """保留当前－良品的符号；scores 为绝对残差除以参考尺度。"""
        sample = _array(frame, "当前帧", missing=True, invalid_as_nan=True)
        if sample.shape != (MAX_CHANNEL_COUNT,):
            raise ValueError("当前帧须包含 16 路数值。")
        if threshold is not None:
            threshold = _scalar(threshold, "异常分数阈值", positive=True)
        center = np.full(MAX_CHANNEL_COUNT, np.nan)
        scale = np.full(MAX_CHANNEL_COUNT, np.nan)
        quality = "ok"
        if self._positions is None:
            center, scale = self._centers[0].copy(), self._scales[0].copy()
        elif position is None:
            quality = "position_required"
        else:
            coordinate = _scalar(position, "当前实测位置")
            if coordinate < self._positions[0] or coordinate > self._positions[-1]:
                quality = "outside_reference_range"
            else:
                upper = int(np.searchsorted(self._positions, coordinate))
                if self._positions[upper] == coordinate:
                    center, scale = self._centers[upper].copy(), self._scales[upper].copy()
                else:
                    lower = upper - 1
                    span = self._positions[upper] - self._positions[lower]
                    if not math.isfinite(span):
                        raise ValueError("参考位置范围过大，无法插值。")
                    fraction = (coordinate - self._positions[lower]) / span
                    center = (1 - fraction) * self._centers[lower] + fraction * self._centers[upper]
                    scale = (1 - fraction) * self._scales[lower] + fraction * self._scales[upper]
        residual = np.full(MAX_CHANNEL_COUNT, np.nan)
        scores = np.full(MAX_CHANNEL_COUNT, np.nan)
        valid = np.isfinite(sample) & np.isfinite(center) & np.isfinite(scale)
        try:
            with np.errstate(over="raise", invalid="raise", divide="raise"):
                residual[valid] = sample[valid] - center[valid]
                scores[valid] = np.abs(residual[valid]) / scale[valid]
        except FloatingPointError as error:
            raise ValueError("当前数值范围过大，无法计算良品差分。") from error
        if quality == "ok":
            if not np.isfinite(sample).any():
                quality = "invalid_frame"
            elif not valid.any():
                quality = "reference_unavailable"
            elif not valid.all():
                quality = "partial"
        hits = valid & (scores >= threshold) if threshold is not None else np.zeros(MAX_CHANNEL_COUNT, dtype=bool)
        return InspectionResult(residual, scores, hits, valid, quality, center, scale, threshold)

    @staticmethod
    def _json_array(values: np.ndarray) -> list:
        return [[float(value) if np.isfinite(value) else None for value in row] for row in values]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "inspection-reference", "version": 1,
            "noise_floor_mv": self.noise_floor_mv,
            "positions": None if self._positions is None else list(self._positions),
            "centers_mv": self._json_array(self._centers),
            "scales_mv": self._json_array(self._scales),
            "counts": self._counts.tolist(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "InspectionReference":
        """严格验证可移植摘要，成功后返回新对象。"""
        if not isinstance(payload, Mapping) or payload.get("kind") != "inspection-reference":
            raise ValueError("良品参考文件格式无效。")
        if type(payload.get("version")) is not int or payload["version"] != 1:
            raise ValueError("良品参考版本不支持。")
        floor = _scalar(payload.get("noise_floor_mv"), "噪声尺度下限", positive=True)
        if not math.isfinite(floor * floor) or floor * floor == 0:
            raise ValueError("噪声尺度下限超出可计算范围。")
        centers = _array(payload.get("centers_mv"), "良品中心", missing=True)
        scales = _array(payload.get("scales_mv"), "良品尺度", missing=True)
        counts = _array(payload.get("counts"), "良品样本数")
        if centers.ndim != 2 or not len(centers) or centers.shape[1] != MAX_CHANNEL_COUNT:
            raise ValueError("良品中心须为非空 N×16 数组。")
        if scales.shape != centers.shape or counts.shape != centers.shape:
            raise ValueError("良品中心、尺度及样本数的形状不一致。")
        max_count = np.iinfo(np.int64).max // len(counts)
        if np.any(counts < 0) or np.any(counts != np.floor(counts)) or any(int(value) > max_count for value in counts.flat):
            raise ValueError("良品样本数须为可计算的非负整数。")
        known = np.isfinite(centers)
        expected = (counts > 0) & (counts.sum(axis=0) >= 3)
        if not expected.any() or not np.array_equal(known, expected):
            raise ValueError("良品中心与有效样本数不一致。")
        if not np.array_equal(known, np.isfinite(scales)) or np.any(scales[known] < floor):
            raise ValueError("良品尺度须与中心同时有效，且不低于噪声下限。")
        positions = payload.get("positions")
        if positions is None:
            if len(centers) != 1:
                raise ValueError("无位置参考须恰有一组良品中心。")
        else:
            positions = _array(positions, "参考位置")
            if positions.shape != (len(centers),) or np.any(positions[1:] <= positions[:-1]):
                raise ValueError("参考位置须与中心对应且严格递增。")
        return cls(centers, scales, counts, positions, floor)
