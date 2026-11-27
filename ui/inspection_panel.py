"""疲劳裂纹/焊缝筛查的操作面板与按真实时间绘制的 16 路时序图。

本组件只接受已有分析结果，不采集设备、不解释参考文件、不判定缺陷。
数组始终按采集通道 0..15 排列；纵轴不是二维空间，也不是缺陷深度。
"""

from __future__ import annotations

from collections import deque
import math

import numpy as np
import pyqtgraph as pg
from PyQt5.QtCore import QEvent, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QSpinBox, QToolButton, QVBoxLayout, QWidget,
)

from ui.components import SectionHeader, StatusBadge
from ui.theme import DATA_VIZ, PALETTE, FONT_FAMILY


class InspectionPanel(QWidget):
    """独立操作界面。调用者负责来源隔离、参考管理和所有分析计算。"""

    capture_requested = pyqtSignal(int)
    cancel_capture_requested = pyqtSignal()
    clear_requested = pyqtSignal()
    save_requested = pyqtSignal()
    load_requested = pyqtSignal()
    import_reference_requested = pyqtSignal()
    threshold_changed = pyqtSignal(float)

    _MODES = {
        "fatigue": (
            "疲劳裂纹筛查", "采集良品",
            "先在已确认无裂纹的同类区域采集参考，再扫描待测区域。",
        ),
        "weld": (
            "焊缝缺陷筛查", "采集良品",
            "先在同类、已确认完好的焊缝上采集参考，再沿焊缝扫描。",
        ),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("InspectionPanel")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._mode = "fatigue"
        self._frames = deque(maxlen=600)
        self._time_origin = None
        self._window_s = 30.0
        self._channel_labels = {}
        self._has_reference = False
        self._is_capturing = False
        self._levels = {"raw": (0.0, 4096.0), "residual": (-10.0, 10.0)}
        self._autoscaled = {"raw": False, "residual": False}
        self._display_intervals = []
        self._build_ui()
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(100)
        self._render_timer.timeout.connect(self._render_history)
        self.set_mode("fatigue")
        self.set_capture_state(False, False)
        self._update_channel_axis()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(4)
        self.header = SectionHeader("疲劳裂纹筛查")
        self.header.hide()  # The primary workspace already names the workflow.
        layout.addWidget(self.header)
        self.hint_label = QLabel()
        self.hint_label.setObjectName("Caption")
        self.hint_label.setTextFormat(Qt.PlainText)
        self.hint_label.setWordWrap(True)
        self.hint_label.hide()
        layout.addWidget(self.hint_label)

        capture_row = QHBoxLayout()
        capture_row.setSpacing(4)
        self.capture_button = QPushButton("采集良品")
        self.capture_button.setObjectName("inspectionCaptureReference")
        self.capture_button.clicked.connect(lambda: self.capture_requested.emit(self.frame_count.value()))
        capture_row.addWidget(self.capture_button)
        self.frame_count = QSpinBox()
        self.frame_count.setObjectName("inspectionCaptureFrames")
        self.frame_count.setRange(3, 200)
        self.frame_count.setValue(20)
        self.frame_count.setSuffix(" 帧")
        self.frame_count.setToolTip("点击采集后所需的完整新帧数；请保持位置与提离不变。")
        self.frame_count.setAccessibleName("参考采集帧数")
        capture_row.addWidget(self.frame_count)
        self.cancel_button = self._tool_button("取消", "取消本次良品参考采集。")
        self.cancel_button.setObjectName("inspectionCancelCapture")
        self.cancel_button.clicked.connect(self.cancel_capture_requested.emit)
        capture_row.addWidget(self.cancel_button)
        file_row = QHBoxLayout()
        file_row.setSpacing(6)
        for row, attribute, text, object_name, signal in (
            (file_row, "load_button", "加载标定…", "inspectionLoadReference", self.load_requested),
            (file_row, "save_button", "保存标定…", "inspectionSaveReference", self.save_requested),
            (file_row, "import_button", "良品 CSV…", "inspectionImportReference", self.import_reference_requested),
            (capture_row, "clear_button", "清除参考", "inspectionClearReference", self.clear_requested),
        ):
            button = self._tool_button(text, text)
            if attribute in {"load_button", "save_button"}:
                button.setToolTip(f"{text} 使用当前模式独立的良品标定 JSON 文件。")
            if attribute == "import_button":
                button.setToolTip("从已确认完好的记录中导入参考；不要用待测区域记录代替良品。")
            button.setObjectName(object_name)
            button.clicked.connect(signal.emit)
            setattr(self, attribute, button)
            row.addWidget(button)
        capture_row.addStretch(1)
        layout.addLayout(capture_row)
        file_row.addStretch(1)
        layout.addLayout(file_row)
        self.progress_label = QLabel("等待完整数据")
        self.progress_label.setObjectName("Caption")
        self.progress_label.setTextFormat(Qt.PlainText)
        self.progress_label.hide()

        status_row = QHBoxLayout()
        self.indicator = StatusBadge("等待参考", "neutral")
        status_row.addWidget(self.indicator)
        self.status_label = QLabel("连接数据源后可采集或加载参考。")
        self.status_label.setObjectName("inspectionStatus")
        self.status_label.setTextFormat(Qt.PlainText)
        self.status_label.setWordWrap(True)
        policy = self.status_label.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Ignored)
        self.status_label.setSizePolicy(policy)
        status_row.addWidget(self.status_label, 1)
        status_row.addWidget(self.progress_label)
        layout.addLayout(status_row)

        analysis_controls = QHBoxLayout()
        analysis_controls.setSpacing(6)
        threshold_label = QLabel("异常阈值")
        analysis_controls.addWidget(threshold_label)
        self.threshold_spin = QDoubleSpinBox()
        self.threshold_spin.setObjectName("inspectionThreshold")
        self.threshold_spin.setRange(0.1, 1000000.0)
        self.threshold_spin.setDecimals(2)
        self.threshold_spin.setValue(6.0)
        self.threshold_spin.setSingleStep(0.5)
        self.threshold_spin.setToolTip("标准化异常分数的阈值；须用独立良品与已知缺陷试样验证。")
        self.threshold_spin.setAccessibleName("标准化异常分数阈值")
        threshold_label.setBuddy(self.threshold_spin)
        self.threshold_spin.valueChanged.connect(self.threshold_changed.emit)
        analysis_controls.addWidget(self.threshold_spin)
        self.window_combo = QComboBox()
        self.window_combo.setObjectName("inspectionTimeWindow")
        self.window_combo.setToolTip("时序图显示的最近一段时间。")
        for seconds in (10, 30, 60):
            self.window_combo.addItem(f"{seconds} 秒", seconds)
        self.window_combo.setCurrentIndex(1)
        self.window_combo.currentIndexChanged.connect(self._window_changed)
        analysis_controls.addWidget(self.window_combo)
        analysis_controls.addStretch(1)
        layout.addLayout(analysis_controls)

        plot_controls = QHBoxLayout()
        plot_controls.setSpacing(6)
        self.display_combo = QComboBox()
        self.display_combo.setObjectName("inspectionWaterfallMode")
        self.display_combo.addItem("良品残差 / mV", "residual")
        self.display_combo.addItem("原始响应 / mV", "raw")
        self.set_compensation_applied(False)
        self.display_combo.currentIndexChanged.connect(self._display_changed)
        plot_controls.addWidget(self.display_combo)
        self.scale_button = self._tool_button("色标适配", "按当前窗口数据设置色标；残差始终以零为中心。")
        self.scale_button.clicked.connect(self._autoscale)
        plot_controls.addWidget(self.scale_button)
        self.clear_history_button = self._tool_button("清空时序", "只清除显示历史，保留已采参考。")
        self.clear_history_button.clicked.connect(self.clear_history)
        plot_controls.addWidget(self.clear_history_button)
        plot_controls.addStretch(1)
        layout.addLayout(plot_controls)

        self._plot = pg.PlotWidget(background=DATA_VIZ.no_data_bg)
        self._plot.setObjectName("inspectionWaterfall")
        self._plot.installEventFilter(self)
        self._plot.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        self._plot.setMinimumHeight(110)
        self._plot.setAccessibleName("按相对时间排列的16路响应，不代表空间C扫")
        self._plot.setMouseEnabled(x=False, y=False)
        self._plot.setMenuEnabled(False)
        self._plot.hideButtons()
        self._plot.getPlotItem().invertY(True)
        self._plot.setLabel("bottom", "本段相对时间", units="s")
        self._plot.setLabel("left", "采集通道")
        self._plot.setYRange(0.0, 16.0, padding=0)
        self._plot.setXRange(0.0, self._window_s, padding=0)
        for name in ("left", "bottom"):
            axis = self._plot.getAxis(name)
            axis.setPen(pg.mkPen(PALETTE.border_strong))
            axis.setTextPen(pg.mkPen(PALETTE.text_secondary))
            axis.setStyle(tickFont=QFont(FONT_FAMILY, 8))
        self._signed_map = pg.ColorMap(
            [0.0, 0.5, 1.0],
            [QColor(DATA_VIZ.heat_neg), QColor(DATA_VIZ.heat_zero), QColor(DATA_VIZ.heat_pos)],
        )
        self._raw_map = pg.ColorMap(
            [0.0, 1.0], [QColor(DATA_VIZ.heat_zero), QColor(DATA_VIZ.heat_neg)],
        )
        self._mesh = pg.PColorMeshItem(colorMap=self._signed_map, levels=(-10.0, 10.0), enableAutoLevels=False)
        self._plot.addItem(self._mesh)
        self._colorbar = pg.ColorBarItem(
            values=(-10.0, 10.0), colorMap=self._signed_map, label="mV",
            interactive=False, colorMapMenu=False, pen=PALETTE.text_secondary,
        )
        self._colorbar.setImageItem(self._mesh, insert_in=self._plot.getPlotItem())
        self._plot.scene().sigMouseMoved.connect(self._mouse_moved)
        layout.addWidget(self._plot, 1)

        self.summary_label = QLabel("当前帧：等待数据")
        self.summary_label.setObjectName("inspectionFrameSummary")
        self.summary_label.setTextFormat(Qt.PlainText)
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        self.cursor_label = QLabel("灰色表示无结果或时间缺口；横轴为时间，不能用于测量缺陷长度。")
        self.cursor_label.setObjectName("Caption")
        self.cursor_label.setTextFormat(Qt.PlainText)
        self.cursor_label.setWordWrap(True)
        layout.addWidget(self.cursor_label)

    @staticmethod
    def _tool_button(text, tooltip):
        button = QToolButton()
        button.setText(text)
        button.setToolTip(tooltip)
        button.setProperty("role", "plotTool")
        button.setFixedHeight(28)
        return button

    def set_mode(self, mode: str):
        if mode not in self._MODES:
            raise ValueError("检查模式须为 fatigue 或 weld。")
        changed = mode != self._mode
        self._mode = mode
        title, capture_title, hint = self._MODES[mode]
        self.header.set_title(title)
        self.capture_button.setText(capture_title)
        self.hint_label.setText(hint)
        self.capture_button.setToolTip(hint)
        if changed:
            self.clear_history()

    def set_compensation_applied(self, applied: bool) -> None:
        """说明残差是否已扣除公共响应，保留显示模式和时间轴。"""
        index = self.display_combo.findData("residual")
        title = "补偿后残差 / mV" if applied else "良品残差 / mV"
        if self.display_combo.itemText(index) != title:
            self.display_combo.setItemText(index, title)
        meaning = "当前响应减去良品参考和公共响应。" if applied else "当前响应减去良品参考。"
        tooltip = f"{meaning} 可以切换查看原始响应。"
        if self.display_combo.toolTip() != tooltip:
            self.display_combo.setToolTip(tooltip)

    def set_threshold(self, value):
        value = float(value)
        if not math.isfinite(value) or not self.threshold_spin.minimum() <= value <= self.threshold_spin.maximum():
            raise ValueError("异常阈值超出有效范围。")
        self.threshold_spin.blockSignals(True)
        self.threshold_spin.setValue(value)
        self.threshold_spin.blockSignals(False)

    def set_capture_state(self, can_capture, is_capturing, count=0, target=0):
        capturing = bool(is_capturing)
        self._is_capturing = capturing
        self.capture_button.setEnabled(bool(can_capture) and not capturing)
        self.frame_count.setEnabled(not capturing)
        self.cancel_button.setVisible(capturing)
        self.cancel_button.setEnabled(capturing)
        for button in (self.load_button, self.import_button):
            button.setEnabled(not capturing)
        for button in (self.save_button, self.clear_button):
            button.setEnabled(self._has_reference and not capturing)
        self.progress_label.setVisible(capturing)
        if capturing:
            self.progress_label.setText(f"采集 {max(0, int(count))}/{max(0, int(target))} 帧")
        else:
            self.progress_label.setText("可采集参考" if can_capture else "等待完整数据")

    def set_available(self, has_reference: bool):
        """控制保存/清除参考；加载与导入在未采集中始终可用。"""
        self._has_reference = bool(has_reference)
        for button in (self.save_button, self.clear_button):
            button.setEnabled(self._has_reference and not self._is_capturing)

    def set_status(self, text):
        status = str(text)
        self.status_label.setText(status)
        self.status_label.setToolTip(status)

    def set_indicator(self, text, role="neutral"):
        if role not in ("ok", "warning", "danger", "neutral", "accent", "demo"):
            role = "neutral"
        self.indicator.set_status(str(text), role)

    def set_channel_labels(self, labels):
        """可选的 {采集索引: 物理CH标签}；只改标签，不重排输入数组。"""
        labels = {int(channel): str(label) for channel, label in dict(labels or {}).items() if 0 <= int(channel) < 16}
        if labels != self._channel_labels:
            self._channel_labels = labels
            self.clear_history()
            self._update_channel_axis()

    def _update_channel_axis(self):
        ticks = []
        available_height = max(50, self._plot.height() - 55)
        step = max(1, int(math.ceil(16 * 13 / available_height)))
        for channel in range(16):
            if channel % step != 0 and channel != 15:
                continue
            label = f"{channel + 1:02d}"
            if channel in self._channel_labels:
                label += f" · {self._channel_labels[channel]}"
            ticks.append((channel + 0.5, label))
        self._plot.getAxis("left").setTicks([ticks])

    def eventFilter(self, watched, event):
        if watched is self._plot and event.type() == QEvent.Resize:
            self._update_channel_axis()
        return super().eventFilter(watched, event)

    @staticmethod
    def _vector(values):
        if values is None:
            return np.full(16, np.nan)
        try:
            result = np.array(values, dtype=float, copy=True)
        except (TypeError, ValueError, OverflowError):
            return np.full(16, np.nan)
        if result.shape != (16,):
            return np.full(16, np.nan)
        result[~np.isfinite(result)] = np.nan
        return result

    def add_frame(self, time_s, raw16, residual16_or_None=None, score16_or_None=None):
        """追加一帧；时间为调用者的采集/回放时间，不应传入绘图刷新时间。"""
        timestamp = float(time_s)
        if not math.isfinite(timestamp):
            raise ValueError("帧时间须为有限秒数。")
        if self._frames and timestamp < self._frames[-1][0]:
            self.clear_history()
        if self._time_origin is None:
            self._time_origin = timestamp
        frame = (timestamp, self._vector(raw16), self._vector(residual16_or_None), self._vector(score16_or_None))
        if self._frames and timestamp == self._frames[-1][0]:
            self._frames[-1] = frame
        else:
            self._frames.append(frame)
        while len(self._frames) > 1 and self._frames[0][0] < timestamp - 60.0:
            self._frames.popleft()
        self._update_summary(frame)
        if not self._render_timer.isActive():
            self._render_timer.start()

    def _update_summary(self, frame):
        timestamp, raw, residual, scores = frame
        current = residual[np.isfinite(residual)]
        score = scores[np.isfinite(scores)]
        elapsed = timestamp - self._time_origin
        if len(current):
            extremes = f"残差 {np.min(current):+.3f} ～ {np.max(current):+.3f} mV"
        else:
            extremes = "残差未知"
        scored = f"最高异常分数 {np.max(score):.2f}" if len(score) else "异常分数未知"
        self.summary_label.setText(
            f"当前帧 {elapsed:.3f} s · 完整值 {np.isfinite(raw).sum()}/16 · {extremes} · {scored}"
        )

    def clear_history(self):
        self._frames.clear()
        self._time_origin = None
        self._display_intervals = []
        self._autoscaled = {"raw": False, "residual": False}
        if hasattr(self, "_render_timer"):
            self._render_timer.stop()
        self._mesh.setData()
        self._plot.setXRange(0.0, self._window_s, padding=0)
        self.summary_label.setText("当前帧：等待数据")
        self.cursor_label.setText("灰色表示无结果或时间缺口；横轴为时间，不能用于测量缺陷长度。")

    def _display_changed(self, *_):
        mode = self.display_combo.currentData()
        self._colorbar.setColorMap(self._signed_map if mode == "residual" else self._raw_map)
        self._colorbar.setLevels(self._levels[mode])
        self._render_history()

    def _window_changed(self, *_):
        self._window_s = float(self.window_combo.currentData())
        self._render_history()

    def _visible_frames(self):
        if not self._frames:
            return []
        earliest = self._frames[-1][0] - self._window_s
        return [frame for frame in self._frames if frame[0] >= earliest]

    def _autoscale(self, *_):
        frames = self._visible_frames()
        if not frames:
            return
        mode = self.display_combo.currentData()
        column = 2 if mode == "residual" else 1
        values = np.asarray([frame[column] for frame in frames])
        finite = values[np.isfinite(values)]
        if not len(finite):
            return
        if mode == "residual":
            limit = max(0.125, float(np.max(np.abs(finite))))
            levels = (-limit, limit)
        else:
            low = min(0.0, float(np.min(finite)))
            high = max(low + 0.125, float(np.max(finite)))
            levels = (low, high)
        self._levels[mode] = levels
        self._autoscaled[mode] = True
        self._colorbar.setLevels(levels)

    def _render_history(self):
        frames = self._visible_frames()
        if not frames:
            self._mesh.setData()
            return
        mode = self.display_combo.currentData()
        column = 2 if mode == "residual" else 1
        times = np.array([frame[0] - self._time_origin for frame in frames])
        differences = np.diff(times)
        cadence = float(np.median(differences)) if len(differences) else 0.1
        cadence = max(cadence, 1e-6)
        # Each row occupies at most one typical sampling interval. Longer pauses
        # get an explicit NaN strip rather than stretching the previous sample.
        left = times - np.r_[cadence, np.minimum(differences, cadence)] * 0.5
        right = times + np.r_[np.minimum(differences, cadence), cadence] * 0.5
        left = np.maximum(left, 0.0)
        edges = [float(left[0])]
        rows = []
        self._display_intervals = []
        for index, frame in enumerate(frames):
            if left[index] > edges[-1] + 1e-9:
                rows.append(np.full(16, np.nan))
                edges.append(float(left[index]))
            rows.append(frame[column])
            edges.append(float(right[index]))
            self._display_intervals.append((float(left[index]), float(right[index]), frame))
        x, y = np.meshgrid(np.asarray(edges), np.arange(17), indexing="ij")
        values = np.asarray(rows)
        self._mesh.setData(x, y, values, autoLevels=False)
        finite = values[np.isfinite(values)]
        if finite.size and (
            not self._autoscaled[mode]
            or (mode == "residual" and np.max(np.abs(finite)) > self._levels[mode][1])
        ):
            self._autoscale()
        end = max(float(right[-1]), min(self._window_s, 1.0))
        self._plot.setXRange(max(0.0, end - self._window_s), end, padding=0)

    def _mouse_moved(self, scene_position):
        if not self._plot.getPlotItem().sceneBoundingRect().contains(scene_position):
            return
        point = self._plot.getPlotItem().vb.mapSceneToView(scene_position)
        channel = int(math.floor(point.y()))
        if not 0 <= channel < 16:
            return
        for left, right, frame in self._display_intervals:
            if left <= point.x() <= right:
                column = 2 if self.display_combo.currentData() == "residual" else 1
                value = frame[column][channel]
                label = f"采集第 {channel + 1} 路"
                if channel in self._channel_labels:
                    label += f" / {self._channel_labels[channel]}"
                text = f"{value:+.3f} mV" if np.isfinite(value) else "未知"
                self.cursor_label.setText(f"{frame[0] - self._time_origin:.3f} s · {label} · {text}")
                return
        self.cursor_label.setText(f"{point.x():.3f} s · 时间缺口，无采样值")
