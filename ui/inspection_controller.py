"""良品参考、实时和回放异常事件。保留原始测量及旧版覆盖标定。"""

from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PyQt5.QtCore import QIODevice, QSaveFile, QTimer
from PyQt5.QtWidgets import QFileDialog

from core.channel_layout import is_valid_map, normalize_channel_map
from app_paths import get_calibration_dir
from core.inspection import DEFAULT_INSPECTION_THRESHOLD, InspectionReference
from core.replay_source import CsvSchemaError, ReplaySource
from models.frame import MAX_CHANNEL_COUNT, MV_MAX, MV_MIN


MODE_NAMES = {"fatigue": "疲劳裂纹筛查", "weld": "焊缝表面检测"}
MAX_THRESHOLD = 1000000.0
_CONTEXT_NAMES = {
    "scope": "来源类型", "profile": "ADC 配置", "device": "设备", "frequency": "频率",
    "freq_unit": "频率单位", "gain": "增益", "coil": "线圈", "plate": "试件",
    "enabled_channels": "启用通道", "geometry": "阵列几何", "coil_geometry": "线圈几何",
    "pitch_mm": "线圈中心距", "coil_diameter_mm": "线圈直径",
}
_GEOMETRY_KEYS = {"geometry", "coil_geometry", "pitch_mm", "coil_diameter_mm"}


def _context_difference(first, second, *, strict=False):
    """布局/已声明几何严格相同；其他缺失元数据沿用既有匹配规则。"""
    if first.get("map") != second.get("map"):
        return "物理布局与当前数据不同。"
    keys = (set(first) | set(second)) - {"map", "compensation_id"} if strict else set(_CONTEXT_NAMES) - _GEOMETRY_KEYS
    for key in keys:
        old, new = first.get(key), second.get(key)
        if key in _GEOMETRY_KEYS:
            if old != new:
                return f"{_CONTEXT_NAMES[key]}缺失或与当前数据不同。"
        elif old not in (None, "") and new not in (None, "") and old != new:
            return f"{_CONTEXT_NAMES.get(key, key)}与当前数据不同。"
    return ""


def compatible_device(device: str) -> str:
    """USB 序列号稳定时，去掉 USB 端口位置。"""
    if device.startswith("USB ") and " SER=" in device:
        return re.sub(r" LOCATION=\S+$", "", device)
    return device


def reference_context(mapping, scope, profile, conditions=None, device=""):
    mapping = normalize_channel_map(mapping)
    if not is_valid_map(mapping):
        return None
    context = dict(conditions or {})
    context.update(
        map={str(ch): list(location) for ch, location in sorted(mapping.items())},
        scope=scope, profile=profile, device=compatible_device(device),
    )
    return context


def replay_context(replay):
    if replay is None or not replay.total_rows or (
        replay.layout_conflict or replay.profile_conflict or replay.source_conflict
        or replay.inspection_conditions_conflict
    ):
        return None
    return reference_context(
        replay.recorded_channel_map, replay.recorded_source_scope,
        replay.recorded_profile_name, replay.inspection_conditions,
    )


def usable_samples(values, status, valid_mask, raw_valid=None):
    frame = np.full(MAX_CHANNEL_COUNT, np.nan)
    if status != 0:
        return frame
    for ch, value in enumerate(values[:MAX_CHANNEL_COUNT]):
        if valid_mask & (1 << ch) and (
            raw_valid is None or (ch < len(raw_valid) and raw_valid[ch] == 1)
        ) and value is not None and math.isfinite(value) and MV_MIN < value < MV_MAX:
            frame[ch] = value
    return frame


@dataclass
class SavedReference:
    reference: InspectionReference
    context: dict
    name: str


class InspectionController:
    def __init__(self, panel, parent, update_grid, *, require_compensation=False):
        self.panel = panel
        self.parent = parent
        self.update_grid = update_grid
        # The optional flag preserves the standalone/raw-mV API. The primary
        # workspace requires the exact Step 1 profile for both good and test data.
        self.require_compensation = bool(require_compensation)
        self.compensation = None
        self._compensation_id = None
        self.mode = "fatigue"
        self.references = {}
        self.thresholds = dict.fromkeys(MODE_NAMES, DEFAULT_INSPECTION_THRESHOLD)
        self.context = None
        self.can_capture = False
        self.last_valid = False
        self.capture = None
        self.event_count = 0
        self.in_event = False
        self.last_packet = None
        self._status = ""
        self._reference_directory = get_calibration_dir()
        self._capture_timeout = QTimer(panel)
        self._capture_timeout.setSingleShot(True)
        self._capture_timeout.timeout.connect(self._on_capture_timeout)
        panel.capture_requested.connect(self.start_capture)
        panel.cancel_capture_requested.connect(self.cancel_capture)
        panel.clear_requested.connect(self.clear_reference)
        panel.save_requested.connect(self._choose_save)
        panel.load_requested.connect(self._choose_load)
        panel.import_reference_requested.connect(self._choose_reference_csv)
        panel.threshold_changed.connect(self._set_threshold)
        panel.set_mode(self.mode)
        panel.set_threshold(DEFAULT_INSPECTION_THRESHOLD)
        self._refresh_controls()
        self._set_status(self._reference_status())

    def set_mode(self, mode):
        if mode == self.mode:
            return
        if mode not in MODE_NAMES:
            raise ValueError("检测模式无效。")
        self.mode = mode
        self.panel.set_mode(mode)
        self.panel.set_threshold(self.thresholds[mode])
        self.reset_stream()

    def set_context(self, context, *, can_capture, compensation=None):
        profile = compensation if self.require_compensation else None
        fingerprint = getattr(profile, "fingerprint", None)
        fingerprint = fingerprint if isinstance(fingerprint, str) and fingerprint else None
        bound_context = deepcopy(context)
        if bound_context is not None and self.require_compensation:
            bound_context.pop("compensation_id", None)
            if fingerprint is not None:
                bound_context["compensation_id"] = fingerprint
        unchanged = (
            bound_context == self.context and bool(can_capture) == self.can_capture
            and fingerprint == self._compensation_id
        )
        self.compensation, self._compensation_id = profile, fingerprint
        semantic_setter = getattr(self.panel, "set_compensation_applied", None)
        if callable(semantic_setter):
            semantic_setter(self.require_compensation and self._compensation_ready())
        if unchanged:
            return
        self.context, self.can_capture = bound_context, bool(can_capture)
        self.reset_stream()

    def _compensation_ready(self):
        return (
            self.compensation is not None and self._compensation_id is not None
            and callable(getattr(self.compensation, "apply", None))
        )

    def _prepare_sample(self, raw):
        """同一路径用于检测与参考，原始输入由瀑布图另行保留。"""
        if not self.require_compensation:
            return raw, ""
        if not self._compensation_ready():
            return np.full(MAX_CHANNEL_COUNT, np.nan), "请先在步骤 1 启用匹配的阵列补偿，再采集或导入良品参考。"
        if self.context is None:
            return np.full(MAX_CHANNEL_COUNT, np.nan), "等待与步骤 1 补偿匹配的完整物理布局和采集工况。"
        try:
            sample = np.asarray(self.compensation.apply(raw), dtype=float)
            if sample.shape != (MAX_CHANNEL_COUNT,):
                raise ValueError("补偿结果须包含 16 路数据。")
            if not np.isfinite(sample).all():
                return np.full(MAX_CHANNEL_COUNT, np.nan), "本帧缺失或无效，不能计算步骤 1 补偿；等待完整 16 路采样。"
        except (TypeError, ValueError, OverflowError, FloatingPointError) as error:
            return np.full(MAX_CHANNEL_COUNT, np.nan), f"步骤 1 补偿不可用。{error}"
        return sample.copy(), ""

    def _check_reference_compensation(self, context):
        if not self.require_compensation:
            if context.get("compensation_id") not in (None, ""):
                raise ValueError("该良品参考已使用步骤 1 补偿，请在启用同一补偿的检测模式中加载。")
            return
        fingerprint = context.get("compensation_id")
        if not isinstance(fingerprint, str) or not fingerprint:
            raise ValueError("该参考基于原始电压或旧流程。请启用步骤 1 补偿后重新采集，或重新导入原始良品 CSV。")
        if self._compensation_id is not None and fingerprint != self._compensation_id:
            raise ValueError("该良品参考使用了不同的步骤 1 补偿。请重新采集，或在当前补偿下重新导入原始良品 CSV。")

    def _set_status(self, text):
        if text != self._status:
            self._status = text
            self.panel.set_status(text)

    def _refresh_controls(self):
        self.panel.set_available(self.mode in self.references)
        count, target = (0, 0) if self.capture is None else (
            len(self.capture[0]), self.capture[1],
        )
        self.panel.set_capture_state(
            self.can_capture and self.context is not None and self.last_valid
            and (not self.require_compensation or self._compensation_ready()),
            self.capture is not None, count, target,
        )

    def _match_reference(self):
        saved = self.references.get(self.mode)
        if self.require_compensation and not self._compensation_ready():
            return None, "请先在步骤 1 启用匹配的阵列补偿，再采集或导入良品参考。"
        if saved is None:
            return None, "请在确认完好的部位采集良品，或导入同工况的良品 CSV。"
        if self.context is None:
            return None, f"已读取 {saved.name}。等待有完整物理布局的实时数据或回放。"
        try:
            self._check_reference_compensation(saved.context)
        except ValueError as error:
            return None, str(error)
        difference = _context_difference(saved.context, self.context, strict=self.require_compensation)
        if difference:
            return None, f"{saved.name} 的{difference}"
        return saved, ""

    def _reference_status(self):
        saved, reason = self._match_reference()
        if saved is None:
            return reason
        kind = "位置参考" if saved.reference.has_positions else "固定参考"
        return f"已启用 {saved.name} · {kind}。等待同工况的有效数据。"

    def start_capture(self, count):
        if not self.can_capture or not self.last_valid or self.context is None or self.capture is not None:
            return
        if self.require_compensation and not self._compensation_ready():
            self._set_status("请先在步骤 1 启用匹配的阵列补偿，再采集良品参考。")
            return
        self.capture = ([], max(3, int(count)))
        self._capture_timeout.start(max(30000, self.capture[1] * 5000))
        self._refresh_controls()
        self._set_status("正在采集良品，请保持探头位置和提离。只统计有效的后续采样。")

    def _stop_capture(self):
        capturing = self.capture is not None
        self._capture_timeout.stop()
        self.capture = None
        return capturing

    def cancel_capture(self):
        if self._stop_capture():
            self._refresh_controls()
            self._set_status("已取消良品采集。" + self._reference_status())

    def _on_capture_timeout(self):
        if self._stop_capture():
            self._refresh_controls()
            self._set_status("良品采集超时，请检查有效数据。" + self._reference_status())

    def clear_view(self):
        self.event_count = 0
        self.in_event = False
        self.last_packet = None
        self.panel.clear_history()
        self.panel.set_indicator("等待数据")
        self.update_grid(None)
        self._set_status(self._reference_status())

    def clear_reference(self):
        self.references.pop(self.mode, None)
        self.clear_view()
        self._refresh_controls()

    def reset_stream(self):
        self._stop_capture()
        self.last_valid = False
        self.clear_view()
        self._refresh_controls()

    def lose_frame(self):
        if not self.last_valid and self.last_packet is None:
            return
        self._stop_capture()
        self.last_valid = False
        self.in_event = False
        self.last_packet = None
        self._refresh_controls()
        self.panel.set_indicator("断流", "warning")
        self._set_status("实时数据中断。等待新的有效采样。")
        self.update_grid(None)

    def _apply_reference(self, saved, threshold=None):
        self._check_reference_compensation(saved.context)
        saved = SavedReference(saved.reference, deepcopy(saved.context), saved.name)
        self._stop_capture()
        self.references[self.mode] = saved
        if threshold is not None:
            self.thresholds[self.mode] = threshold
            self.panel.set_threshold(threshold)
        self.clear_view()
        self._refresh_controls()

    def _set_threshold(self, value):
        self.thresholds[self.mode] = float(value)
        self.event_count = 0
        self.in_event = False
        if self.last_packet is not None:
            self._analyze(*self.last_packet, update_event=False, add_history=False)

    def on_frame(self, time_s, raw, position_mm=None):
        sample, input_reason = self._prepare_sample(raw)
        self.last_valid = bool(np.isfinite(sample).any())
        self.last_packet = (time_s, raw, position_mm)
        if self.capture is not None:
            frames, target = self.capture
            if self.last_valid:
                frames.append(sample.copy())
            if len(frames) == target:
                try:
                    reference = InspectionReference.fit(frames)
                    self._apply_reference(SavedReference(reference, self.context, "本次良品"))
                    self.last_packet = (time_s, raw, position_mm)
                except ValueError as error:
                    self._stop_capture()
                    self._refresh_controls()
                    self.panel.set_indicator("参考无效", "warning")
                    self._set_status(str(error))
                    self.update_grid(None)
                    return
            else:
                self.panel.add_frame(time_s, raw, None, None)
                self._refresh_controls()
                self.panel.set_indicator("采集中", "accent")
                self._set_status(f"正在采集良品 {len(frames)}/{target}，请保持提离与位置。{input_reason}")
                self.update_grid(None)
                return
        self._refresh_controls()
        self._analyze(time_s, raw, position_mm, sample=sample, input_reason=input_reason)

    def _analyze(self, time_s, raw, position_mm, *, update_event=True, add_history=True, sample=None, input_reason=""):
        if sample is None:
            sample, input_reason = self._prepare_sample(raw)
        saved, reason = self._match_reference()
        result = None
        if input_reason:
            reason = input_reason
        elif saved is not None:
            try:
                result = saved.reference.analyze(sample, position_mm, threshold=self.thresholds[self.mode])
            except ValueError as error:
                reason = str(error)
        residual = result.residual_mv if result is not None else None
        scores = result.scores if result is not None else None
        hit = result is not None and bool(result.hit_mask.any())
        if update_event:
            if hit and not self.in_event:
                self.event_count += 1
            self.in_event = hit
        if add_history:
            self.panel.add_frame(time_s, raw, residual, scores)
        if result is None:
            if input_reason:
                self.panel.set_indicator("待有效帧" if self._compensation_ready() and self.context is not None else "待补偿", "warning")
            else:
                self.panel.set_indicator("等待参考")
            self._set_status(reason)
            self.update_grid(None)
            return
        unavailable = {
            "position_required": "该参考按毫米建立。请回放带 position_mm 的记录，或采集固定良品参考。",
            "outside_reference_range": "当前位置超出良品参考范围。",
            "reference_unavailable": "当前位置没有可用的良品参考。",
            "invalid_frame": "本帧无可用通道，等待有效数据。",
        }
        if not result.valid_mask.any():
            self.panel.set_indicator("待比较", "warning")
            self._set_status(unavailable.get(result.quality, "等待可与良品比较的数据。"))
            self.update_grid(residual)
            return
        peak = int(np.nanargmax(scores))
        place = f" · {position_mm:g} mm" if position_mm is not None else ""
        state = "疑似异常" if hit else "本帧未超门槛"
        self.panel.set_indicator("疑似异常" if hit else "比较中", "warning" if hit else "neutral")
        self._set_status(
            f"{saved.name} · {state}{place} · 第 {peak + 1} 路 {scores[peak]:.2f} · "
            f"试算门槛 {result.threshold:g} · 本段 {self.event_count} 次异常 · {int(result.valid_mask.sum())}/16 路"
        )
        self.update_grid(residual)

    def save_reference(self, path):
        saved = self.references.get(self.mode)
        if saved is None:
            raise ValueError("请先采集或导入良品参考。")
        self._check_reference_compensation(saved.context)
        payload = json.dumps({
            "format": "eddy-array-inspection", "version": 1, "mode": self.mode,
            "context": saved.context, "threshold": self.thresholds[self.mode],
            "reference": saved.reference.to_dict(),
        }, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        target = QSaveFile(str(path))
        if not target.open(QIODevice.WriteOnly) or target.write(payload) != len(payload) or not target.commit():
            raise OSError(target.errorString())

    def load_reference(self, path):
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or data.get("format") != "eddy-array-inspection" or data.get("version") != 1:
            raise ValueError("请选择疲劳或焊缝模式保存的良品标定 JSON。")
        if data.get("mode") != self.mode:
            raise ValueError("文件属于另一检测模式。请先切换到保存时的模式。")
        context = data.get("context")
        if not isinstance(context, dict) or not is_valid_map(normalize_channel_map(context.get("map"))):
            raise ValueError("文件缺少有效的物理通道布局。")
        context = dict(context, map={str(ch): list(loc) for ch, loc in normalize_channel_map(context["map"]).items()})
        self._check_reference_compensation(context)
        threshold = float(data.get("threshold", DEFAULT_INSPECTION_THRESHOLD))
        if not 0.1 <= threshold <= MAX_THRESHOLD:
            raise ValueError("文件中的试算门槛超出范围。")
        reference = InspectionReference.from_dict(data.get("reference"))
        self._apply_reference(SavedReference(reference, context, Path(path).name), threshold)

    def import_reference_csv(self, path):
        source = ReplaySource()
        source.load(str(path))
        context = replay_context(source)
        if context is None or source.n_channels != MAX_CHANNEL_COUNT:
            raise ValueError("良品 CSV 须包含一致的采集工况和完整的物理通道布局。")
        if self.require_compensation:
            if not self._compensation_ready() or self.context is None:
                raise ValueError("请先在步骤 1 启用与当前数据匹配的阵列补偿，再导入原始良品 CSV。")
            difference = _context_difference(context, self.context, strict=True)
            if difference:
                raise ValueError(f"良品 CSV 的{difference}请使用与步骤 1 补偿相同工况的原始记录。")
            # Identity records the actual transform used, never the original CSV
            # baseline or a reference imported before this profile was selected.
            context = dict(context, compensation_id=self._compensation_id)
        rows = []
        for row in source.inspection_rows:
            raw = usable_samples(row.wire_mv, row.status, row.valid_mask, row.raw_valid)
            sample, reason = self._prepare_sample(raw)
            if np.isfinite(sample).any():
                rows.append((row.position_mm, sample))
        if self.require_compensation and not rows:
            raise ValueError("良品 CSV 没有可用于步骤 1 补偿的完整有效 16 路采样。")
        located = [pair for pair in rows if pair[0] is not None]
        if located:
            rows = located
            if len({p for p, _ in rows}) < 2:
                raise ValueError("带位置的良品参考至少需要两个不同的实测毫米坐标。")
        reference = InspectionReference.fit(
            [raw for _, raw in rows], [p for p, _ in rows] if located else None,
        )
        self._apply_reference(SavedReference(reference, context, Path(path).name))

    def _choose_save(self):
        suggested = self._reference_directory / f"{MODE_NAMES[self.mode]}_良品.json"
        path, _ = QFileDialog.getSaveFileName(self.parent, "保存良品标定", str(suggested), "JSON (*.json)")
        if path:
            try:
                self.save_reference(path)
                self._reference_directory = Path(path).parent
                self._set_status(f"已保存 {Path(path).name}。")
            except (OSError, TypeError, ValueError) as error:
                self._set_status(f"保存失败。{error}")

    def _choose_load(self):
        path, _ = QFileDialog.getOpenFileName(
            self.parent, "加载良品标定", str(self._reference_directory), "JSON (*.json)"
        )
        if path:
            try:
                self.load_reference(path)
                self._reference_directory = Path(path).parent
            except (OSError, TypeError, ValueError) as error:
                self._set_status(f"加载失败。{error}")

    def _choose_reference_csv(self):
        path, _ = QFileDialog.getOpenFileName(self.parent, "导入已经确认完好的扫查记录", "", "CSV (*.csv)")
        if path:
            try:
                self.import_reference_csv(path)
            except (CsvSchemaError, OSError, TypeError, ValueError) as error:
                self._set_status(f"导入失败。{error}")
