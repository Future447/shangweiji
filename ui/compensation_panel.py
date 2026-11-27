"""公共补偿标定的采集、工况匹配与文件操作。"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PyQt5.QtCore import QIODevice, QSaveFile, Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QComboBox, QFileDialog, QGridLayout, QLabel, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

from app_paths import get_calibration_dir
from core.channel_layout import is_valid_map, normalize_channel_map
from core.compensation import CompensationProfile
from ui.components import SectionHeader

_CONDITIONS = {
    "scope": "数据来源", "profile": "ADC 配置", "device": "设备",
    "frequency": "频率", "freq_unit": "频率单位", "gain": "增益",
    "coil": "线圈", "plate": "试件", "enabled_channels": "启用通道",
}


class CompensationPanel(QWidget):
    profile_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.profile = None
        self.active_profile = None
        self._saved_context = None
        self._context = None
        self._can_capture = False
        self._last_valid = False
        self._capture = None
        self._name = ""
        self._notice = ""
        self._directory = get_calibration_dir()
        self._timer = QTimer(self, singleShot=True)
        self._timer.timeout.connect(self._timeout)
        self._build_ui()
        self._refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(SectionHeader(
            "测量参考", "固定提离和激励设置",
        ))
        controls = QGridLayout()
        controls.setSpacing(6)
        self.frame_count = QSpinBox()
        self.frame_count.setRange(3, 500)
        self.frame_count.setValue(16)
        self.frame_count.setSuffix(" 帧")
        self.frame_count.setAccessibleName("每个标定点的采集帧数")
        self.baseline_button = self._button("采集完好参考", lambda: self._start("baseline"))
        self.baseline_button.setToolTip("在同材同厚的完好区域采集。重新采集会建立新的补偿配置。")
        self.cancel_button = self._button("取消采集", self.reset_stream)
        controls.addWidget(self.baseline_button, 0, 0, 1, 2)
        controls.addWidget(self.frame_count, 0, 2)
        controls.addWidget(self.cancel_button, 1, 2)
        self.load_button = self._button("加载…", self._choose_load)
        self.save_button = self._button("保存…", self._choose_save)
        controls.addWidget(self.load_button, 1, 0)
        controls.addWidget(self.save_button, 1, 1)
        layout.addLayout(controls)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)

        advanced = QPushButton("展开通道耦合标定")
        advanced.setCheckable(True)
        advanced.setProperty("role", "secondary")
        layout.addWidget(advanced)
        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        hint = QLabel(
            "可选。保持完好试件和探头位置固定，等到出现足够的均匀电平变化时，"
            "可采第二组背景以估计逐路公共增益。随后将同一小标定件放在每个格子中心，"
            "逐格采集串扰响应。每次只扰动一处，试件、标定件姿态和提离保持一致。"
        )
        hint.setWordWrap(True)
        detail_layout.addWidget(hint)
        self.background_button = self._button("采集第二组均匀背景", lambda: self._start("background"))
        detail_layout.addWidget(self.background_button)
        self.channel_picker = QComboBox()
        self.channel_picker.setAccessibleName("局部标定件所在格子")
        self.template_button = self._button("采集该格", lambda: self._start("template"))
        grid = QGridLayout()
        grid.addWidget(self.channel_picker, 0, 0)
        grid.addWidget(self.template_button, 0, 1)
        detail_layout.addLayout(grid)
        layout.addWidget(detail)
        detail.hide()

        def toggle_detail(expanded):
            detail.setVisible(expanded)
            advanced.setText("收起通道耦合标定" if expanded else "展开通道耦合标定")

        advanced.toggled.connect(toggle_detail)
        layout.addStretch(1)

    @staticmethod
    def _button(text, slot):
        button = QPushButton(text)
        button.setProperty("role", "secondary")
        button.clicked.connect(slot)
        return button

    def set_channels(self, mapping, labels):
        previous = self.channel_picker.currentData()
        self.channel_picker.clear()
        for channel, (row, col) in sorted(mapping.items(), key=lambda entry: entry[1]):
            self.channel_picker.addItem(f"第 {row + 1} 行第 {col + 1} 列 · {labels[channel]}", channel)
        index = self.channel_picker.findData(previous)
        if index >= 0:
            self.channel_picker.setCurrentIndex(index)

    def set_context(self, context, can_capture):
        if context == self._context and bool(can_capture) == self._can_capture:
            return
        self._context = context
        self._can_capture = bool(can_capture)
        self.reset_stream()
        self._match()

    def _match(self):
        current = self._context
        saved = self._saved_context
        self.active_profile = None
        self._reason = ""
        if current is None:
            self._reason = "等待设备或包含通道布局的回放。"
        elif current.get("enabled_channels") not in (None, list(range(16))):
            self._reason = "请启用全部 16 路后再标定。"
        elif saved is None:
            self._reason = "请在完好试件上固定探头和提离，采集完好参考，或加载同工况标定。"
        elif saved.get("map") != current.get("map"):
            self._reason = "当前物理通道布局与标定文件不同。"
        else:
            for key, label in _CONDITIONS.items():
                old, new = saved.get(key), current.get(key)
                if old not in (None, "") and new not in (None, "") and old != new:
                    self._reason = f"当前{label}与已保存的公共补偿标定不同。"
                    break
            else:
                self.active_profile = self.profile
        self._refresh()

    def _refresh(self):
        active = self.active_profile
        capturing = self._capture is not None
        ready = (
            self._can_capture and self._last_valid and not capturing and self._context is not None
            and self._context.get("enabled_channels") in (None, list(range(16)))
        )
        self.baseline_button.setEnabled(ready)
        self.frame_count.setEnabled(not capturing)
        self.load_button.setEnabled(not capturing)
        self.save_button.setEnabled(self.profile is not None and not capturing)
        self.cancel_button.setVisible(capturing)
        self.background_button.setEnabled(ready and active is not None)
        self.template_button.setEnabled(ready and active is not None and self.channel_picker.count() == 16)
        self.channel_picker.setEnabled(not capturing)
        if capturing:
            kind, channel, frames, count = self._capture
            text = f"采集中 {len(frames)}/{count} 帧。请保持工况和探头位置。"
        elif active is not None:
            if active.correction_mode == "coupling":
                text = f"{self._name} · 公共补偿与 16 格耦合修正已启用。"
            else:
                text = f"{self._name} · 公共补偿已启用。耦合标定 {active.template_count}/16 格。"
        else:
            text = getattr(self, "_reason", "") or "等待设备或包含通道布局的回放。"
        if self._notice and not capturing:
            text += "\n" + self._notice
        if self.status.text() != text:
            self.status.setText(text)

    def _start(self, kind):
        if self._capture is not None or not self._can_capture or not self._last_valid or self._context is None:
            return
        if self._context.get("enabled_channels") not in (None, list(range(16))):
            return
        if kind != "baseline" and self.active_profile is None:
            return
        count = self.frame_count.value()
        self._notice = ""
        self._capture = (kind, self.channel_picker.currentData(), [], count)
        self._timer.start(max(30000, 5000 * count))
        self._refresh()

    @property
    def is_capturing(self):
        return self._capture is not None

    def reset_stream(self):
        self._capture = None
        self._last_valid = False
        self._notice = ""
        self._timer.stop()
        self._refresh()

    def _timeout(self):
        self.reset_stream()
        self._notice = "采集超时。请检查 16 路数据是否都有效。"
        self._refresh()

    def on_frame(self, raw):
        valid = bool(np.isfinite(raw).all())
        if self._last_valid != valid:
            self._last_valid = valid
            self._refresh()
        if self._capture is None or not valid:
            return
        kind, channel, frames, count = self._capture
        frames.append(raw.copy())
        if len(frames) < count:
            self._refresh()
            return
        self._capture = None
        self._timer.stop()
        try:
            if kind == "baseline":
                profile = CompensationProfile(CompensationProfile.measure(frames))
            elif kind == "background":
                profile = self.active_profile.with_uniform_background(frames)
            else:
                active = self.active_profile
                templates = dict(active.templates)
                templates[channel] = CompensationProfile.measure(frames)
                profile = CompensationProfile(active.baseline, active.common_gains, templates)
        except ValueError as error:
            self._notice = str(error)
            self._refresh()
            return
        self._set_profile(profile, self._context, "本次标定")
        if kind == "template":
            missing = next((i for i in range(16)
                            if self.channel_picker.itemData(i) not in profile.templates), -1)
            if missing >= 0:
                self.channel_picker.setCurrentIndex(missing)

    def _set_profile(self, profile, context, name):
        self.profile = profile
        self._saved_context = context
        self._name = name
        self._notice = ""
        self._match()
        self.profile_changed.emit()

    def save_calibration(self, path):
        if self.profile is None:
            raise ValueError("请先采集或加载公共补偿标定。")
        data = json.dumps({
            "format": "eddy-array-compensation",
            "version": 1,
            "context": self._saved_context,
            "profile": self.profile.to_dict(),
        }, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        target = QSaveFile(str(path))
        if not target.open(QIODevice.WriteOnly) or target.write(data) != len(data) or not target.commit():
            raise OSError(target.errorString())

    def load_calibration(self, path):
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict) or payload.get("format") != "eddy-array-compensation" or payload.get("version") != 1:
            raise ValueError("请选择第 1 步保存的公共补偿标定 JSON。")
        context = payload.get("context")
        mapping = context.get("map") if isinstance(context, dict) else None
        mapping = normalize_channel_map(mapping)
        if not is_valid_map(mapping):
            raise ValueError("文件缺少有效的物理通道布局。")
        context = dict(context, map={str(channel): list(cell) for channel, cell in mapping.items()})
        profile = CompensationProfile.from_dict(payload.get("profile"))
        self._set_profile(profile, context, Path(path).name)

    def _choose_load(self):
        path, _ = QFileDialog.getOpenFileName(self, "加载公共补偿标定", str(self._directory), "JSON (*.json)")
        if path:
            try:
                self.load_calibration(path)
                self._directory = Path(path).parent
            except (OSError, TypeError, ValueError) as error:
                self._notice = f"加载失败。{error}"
                self._refresh()

    def _choose_save(self):
        suggested = self._directory / "公共补偿标定.json"
        path, _ = QFileDialog.getSaveFileName(self, "保存公共补偿标定", str(suggested), "JSON (*.json)")
        if path:
            try:
                self.save_calibration(path)
                self._directory = Path(path).parent
                self._notice = f"已保存 {Path(path).name}。"
            except (OSError, TypeError, ValueError) as error:
                self._notice = f"保存失败。{error}"
            self._refresh()
