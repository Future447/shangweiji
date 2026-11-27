"""Independent, immutable preprocessing of complete 16-channel raw mV frames.

The spatial median assumes that fewer than half of the supported channels carry
a local change. It removes a uniform/relative-gain background, including a broad
physical response; it cannot distinguish that response from broad damage. Full
probe calibration describes the measured static response, not its physical cause.
"""

from __future__ import annotations

import hashlib
import json
from numbers import Integral
from types import MappingProxyType
from typing import Any, Mapping, Optional

import numpy as np

from models.frame import MAX_CHANNEL_COUNT, MV_STEP


_KIND = "compensation-profile"
_VERSION = 1
_ALGORITHM = "relative-median-regularized-v1"
_MIN_UNIFORM_CHANNEL_MV = 4 * MV_STEP
_MIN_CALIBRATION_MV = 8 * MV_STEP
_MAX_CONDITION = 20.0
_MAX_ROW_AMPLIFICATION = 5.0
_MAX_SPECTRAL_AMPLIFICATION = 10.0
_REGULARIZATION_FRACTION = 0.01


def _numeric(values: Any, name: str) -> np.ndarray:
    try:
        array = np.asarray(values)
        if array.dtype.kind not in "iuf":
            raise ValueError
        result = array.astype(float, copy=True)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name}须为数值数组。") from error
    if not np.isfinite(result).all():
        raise ValueError(f"{name}包含缺失或非有限数值。")
    return result


def _vector(values: Any, name: str) -> np.ndarray:
    result = _numeric(values, name)
    if result.shape != (MAX_CHANNEL_COUNT,):
        raise ValueError(f"{name}须包含恰好 16 个有限数值。")
    return result


def _readonly_copy(values: np.ndarray) -> np.ndarray:
    result = values.copy()
    result.setflags(write=False)
    return result


class CompensationProfile:
    """Common-response correction, optionally followed by static coupling fit.

    Templates map acquisition channel indices (0..15) to raw median frames from
    a local probe placed over that channel in the unchanged reference scene.
    Zero through fifteen templates use common-only correction. All sixteen must
    form an identifiable, low-amplification response before coupling is enabled.
    Corrected output is signed current-minus-baseline mV in acquisition order.
    Source, physical mapping and measurement compatibility belong to the caller.
    """

    def __init__(
        self,
        baseline: Any,
        common_gains: Optional[Any] = None,
        templates: Optional[Mapping[int, Any]] = None,
    ) -> None:
        self._baseline = _vector(baseline, "补偿参考")
        gains = np.ones(MAX_CHANNEL_COUNT) if common_gains is None else _vector(common_gains, "公共响应比例")
        if np.any(gains <= 0):
            raise ValueError("公共响应比例须全部为正；混合正负响应不能作为统一背景标定。")
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            center = float(np.median(gains))
            # Avoid a second floating-point normalization changing persisted
            # coefficients and their fingerprint on an otherwise exact reload.
            if not np.isclose(center, 1.0, rtol=0.0, atol=1e-12):
                gains = gains / center
        if not np.isfinite(gains).all() or np.any(gains < 0.1) or np.any(gains > 10.0):
            raise ValueError("公共响应比例差异过大，无法可靠估计公共背景。")
        self._common_gains = gains
        if templates is not None and not isinstance(templates, Mapping):
            raise ValueError("局部标定须为采集通道编号到 16 路原始中位数帧的映射。")
        parsed = {}
        for channel, frame in (templates or {}).items():
            if isinstance(channel, (bool, np.bool_)) or not isinstance(channel, Integral) or not 0 <= channel < MAX_CHANNEL_COUNT:
                raise ValueError("局部标定通道编号须为 0 至 15 的整数。")
            measured = _vector(frame, f"通道 {channel + 1} 局部标定")
            try:
                with np.errstate(over="raise", invalid="raise", divide="raise"):
                    delta = measured - self._baseline
                    common = float(np.median(delta / self._common_gains))
                    target_response = float(delta[int(channel)] - self._common_gains[int(channel)] * common)
            except FloatingPointError as error:
                raise ValueError(f"通道 {channel + 1} 局部标定数值不稳定。") from error
            if abs(target_response) < _MIN_CALIBRATION_MV:
                raise ValueError(f"通道 {channel + 1} 局部标定不充分：目标通道去公共后的变化须至少达到 8 个 ADC 步长。")
            parsed[int(channel)] = measured
        self._templates = dict(sorted(parsed.items()))
        self._inverse = None
        self._common_axis = None
        self._common_support = None
        self._diagnostics = {}
        if len(self._templates) == MAX_CHANNEL_COUNT:
            self._fit_coupling()
        for values in (self._baseline, self._common_gains, *self._templates.values()):
            values.setflags(write=False)
        # A reference belongs to the active transform, not to unfinished probe
        # captures. Partial templates remain in JSON but do not alter the output.
        effective = {
            "algorithm": _ALGORITHM,
            "baseline_mv": self._baseline.tolist(),
            "common_gains": self._common_gains.tolist(),
            "templates": None if self._inverse is None else self.to_dict()["templates"],
        }
        encoded = json.dumps(effective, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self._fingerprint = hashlib.sha256(encoded).hexdigest()

    @property
    def baseline(self) -> np.ndarray:
        return _readonly_copy(self._baseline)

    @property
    def common_gains(self) -> np.ndarray:
        return _readonly_copy(self._common_gains)

    @property
    def templates(self) -> Mapping[int, np.ndarray]:
        return MappingProxyType({key: _readonly_copy(value) for key, value in self._templates.items()})

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    @property
    def template_count(self) -> int:
        return len(self._templates)

    @property
    def correction_mode(self) -> str:
        return "coupling" if self._inverse is not None else "common_only"

    @property
    def fit_diagnostics(self) -> dict:
        return dict(self._diagnostics)

    @staticmethod
    def measure(frames: Any) -> np.ndarray:
        """Robust median from at least three complete finite raw mV frames."""
        samples = _numeric(frames, "标定采样")
        if samples.ndim != 2 or samples.shape[1] != MAX_CHANNEL_COUNT or len(samples) < 3:
            raise ValueError("标定须至少采集 3 个完整、有效的 16 路帧。")
        try:
            with np.errstate(over="raise", invalid="raise"):
                result = np.median(samples, axis=0)
        except FloatingPointError as error:
            raise ValueError("标定数值范围过大，无法计算中位数。") from error
        if not np.isfinite(result).all():
            raise ValueError("标定中位数不是有限数值。")
        return _readonly_copy(result)

    @staticmethod
    def fit_common_gains(baseline: Any, background: Any) -> np.ndarray:
        """Fit relative gains from two uniformly different reference medians.

        All 16 channels must agree in sign and exceed four ADC steps; the median
        change must exceed eight steps. These checks reject weak or mixed scenes,
        but uniformity and unchanged lift-off still need experimental validation.
        """
        first = _vector(baseline, "补偿参考")
        second = _vector(background, "第二均匀背景")
        with np.errstate(over="ignore", invalid="ignore"):
            delta = second - first
        if not np.isfinite(delta).all():
            raise ValueError("均匀背景差值超出有效数值范围。")
        with np.errstate(over="ignore", invalid="ignore"):
            center = float(np.median(delta))
        if not np.isfinite(center):
            raise ValueError("均匀背景中位变化超出有效数值范围。")
        if abs(center) < _MIN_CALIBRATION_MV:
            raise ValueError("第二均匀背景变化过弱：16 路中位变化须至少达到 8 个 ADC 步长。")
        if np.any(np.sign(delta) != np.sign(center)):
            raise ValueError("第二均匀背景出现混合方向响应，请保持参考场景和提离条件一致。")
        if np.any(np.abs(delta) < _MIN_UNIFORM_CHANNEL_MV):
            raise ValueError("第二均匀背景支持不足：每一路变化均须至少达到 4 个 ADC 步长。")
        return CompensationProfile(first, delta / center).common_gains

    def with_uniform_background(self, frames: Any) -> "CompensationProfile":
        """Return a new gain-calibrated profile; old local templates are cleared."""
        gains = self.fit_common_gains(self._baseline, self.measure(frames))
        return CompensationProfile(self._baseline, gains)

    def _fit_coupling(self) -> None:
        try:
            with np.errstate(over="raise", invalid="raise", divide="raise"):
                response = np.column_stack([self._templates[index] - self._baseline for index in range(MAX_CHANNEL_COUNT)])
                common = np.median(response / self._common_gains[:, None], axis=0)
                local = response - self._common_gains[:, None] * common[None, :]
                amplitudes = np.diag(local).copy()
                if np.any(np.abs(amplitudes) < _MIN_CALIBRATION_MV):
                    raise ValueError("16 点局部标定不充分：各目标通道去公共后的变化须至少达到 8 个 ADC 步长。")
                response_matrix = local / amplitudes[None, :]
                left, singular, right = np.linalg.svd(response_matrix, full_matrices=False)
                if singular[-1] <= 0 or singular[0] / singular[-1] > _MAX_CONDITION:
                    raise ValueError("16 点响应不可区分或矩阵接近退化；请重新检查局部探块、位置与提离。")
                exact_inverse = (right.T / singular) @ left.T
                amplification = float(np.max(np.linalg.norm(exact_inverse, axis=1)))
                if amplification > _MAX_ROW_AMPLIFICATION or 1.0 / singular[-1] > _MAX_SPECTRAL_AMPLIFICATION:
                    raise ValueError("16 点耦合修正会过度放大噪声，请重新采集可区分的局部标定。")
                regularization = (_REGULARIZATION_FRACTION * singular[0]) ** 2
                inverse = (right.T * (singular / (singular ** 2 + regularization))) @ left.T
                axis = inverse @ self._common_gains
                support = np.abs(axis) >= max(float(np.median(np.abs(axis))) * 0.1, 1e-10)
                if np.count_nonzero(support) < 12:
                    raise ValueError("耦合矩阵缺少可靠的公共背景方向，无法同时稳定去除背景。")
        except (FloatingPointError, np.linalg.LinAlgError) as error:
            raise ValueError("16 点耦合矩阵数值不稳定，无法启用完整修正。") from error
        self._inverse = inverse
        self._common_axis = axis
        self._common_support = support
        self._diagnostics = {
            "condition_number": float(singular[0] / singular[-1]),
            "max_noise_amplification": amplification,
            "regularization": regularization,
            "common_support": int(np.count_nonzero(support)),
        }

    def apply(self, frame: Any) -> np.ndarray:
        """Return signed compensated mV; any invalid channel makes all unknown.

        In full mode the inverse is fitted before imposing the robust common
        constraint in reconstructed coordinates, avoiding inversion of a singular
        linear common-projected matrix. Broad changes remain unidentifiable.
        """
        unknown = np.full(MAX_CHANNEL_COUNT, np.nan)
        try:
            sample = _vector(frame, "当前帧")
            with np.errstate(over="raise", invalid="raise", divide="raise"):
                delta = sample - self._baseline
                if self._inverse is None:
                    common = float(np.median(delta / self._common_gains))
                    result = delta - self._common_gains * common
                else:
                    reconstructed = self._inverse @ delta
                    common = float(np.median(reconstructed[self._common_support] / self._common_axis[self._common_support]))
                    result = reconstructed - self._common_axis * common
        except (ValueError, TypeError, FloatingPointError, OverflowError):
            return unknown
        return result if np.isfinite(result).all() else unknown

    def to_dict(self) -> dict:
        return {
            "kind": _KIND,
            "version": _VERSION,
            "algorithm": _ALGORITHM,
            "baseline_mv": self._baseline.tolist(),
            "common_gains": self._common_gains.tolist(),
            "templates": {str(key): value.tolist() for key, value in self._templates.items()},
        }

    @classmethod
    def from_dict(cls, data: Any) -> "CompensationProfile":
        keys = {"kind", "version", "algorithm", "baseline_mv", "common_gains", "templates"}
        if not isinstance(data, dict) or set(data) != keys:
            raise ValueError("补偿文件字段不完整或包含不支持的字段。")
        if data["kind"] != _KIND or type(data["version"]) is not int or data["version"] != _VERSION or data["algorithm"] != _ALGORITHM:
            raise ValueError("不支持的补偿文件格式、版本或算法。")
        if not isinstance(data["templates"], dict):
            raise ValueError("补偿文件的局部标定格式错误。")
        templates = {}
        for key, value in data["templates"].items():
            if not isinstance(key, str) or key not in {str(index) for index in range(MAX_CHANNEL_COUNT)}:
                raise ValueError("补偿文件通道编号须为 0 至 15 的十进制字符串。")
            templates[int(key)] = value
        return cls(data["baseline_mv"], data["common_gains"], templates)
