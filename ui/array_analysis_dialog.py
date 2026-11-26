"""独立的 16 路原始 mV 空场与直板边位置标定窗口。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
from PyQt5.QtCore import QIODevice, QSaveFile, Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QListWidget,
    QListWidgetItem, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout,
)

from core.array_analysis import ArrayAnalysis, compensate_common_response, coverage_grid, spatial_differences
from core.channel_layout import is_valid_map, labels_for_map, normalize_channel_map
from ui.heatmap_widget import _lerp_color, _text_color_for


class ArrayAnalysisDialog(QDialog):
    """标定按来源、工况与物理映射隔离，采集仅接受点击之后的新帧。"""

    display_result_changed = pyqtSignal(object)
    calibration_applied = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("阵列局部识别")
        self.setModal(False)
        self.resize(900, 760)
        self._context_key = None
        self._token = None
        self._channel_map = {}
        self._models = {}
        self._thresholds = {}
        self._model = ArrayAnalysis()
        self._latest_frame = None
        self._held = False
        self._frame_error = False
        self._capture = None
        self._pending = None
        self._pending_reason = ""
        calibration_dir = Path(__file__).resolve().parent.parent / "涡流标定"
        self._last_directory = calibration_dir if calibration_dir.is_dir() else calibration_dir.parent
        self._message = "已有标定可直接加载；连接匹配的设备后会自动启用。"
        self._build_ui()
        self._capture_timer = QTimer(self)
        self._capture_timer.setSingleShot(True)
        self._capture_timer.timeout.connect(self._capture_timeout)
        self._display_timer = QTimer(self)
        self._display_timer.timeout.connect(self._render)
        self._display_timer.start(100)
        self._update_controls()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        self.context_label = QLabel("等待可用数据源")
        self.context_label.setWordWrap(True)
        self.context_label.setTextFormat(Qt.PlainText)
        layout.addWidget(self.context_label)
        hint = QLabel(
            "加载已有标定后，将铁板移远，点击“重采当前空场（保留模板）”更新本次测量的零点。\n"
            "新标定：连接设备 → 选进入方向 → 铁板移远采空场 → 铁板驻停于多个已知位置逐次采模板 → 保存。\n"
            "位置 p 为线圈中心距；0 为进入侧网格边界，负值为阵列外，4 为四格全覆盖。差分采用 ADC 原始毫伏值的 当前－空场。"
        )
        hint.setObjectName("arrayUsage")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        controls = QGridLayout()
        self.direction_combo = QComboBox()
        self.direction_combo.setObjectName("arrayDirection")
        for title, value in (("左侧进入 →", "left"), ("右侧进入 ←", "right"),
                             ("上侧进入 ↓", "top"), ("下侧进入 ↑", "bottom")):
            self.direction_combo.addItem(title, value)
        self.direction_combo.currentIndexChanged.connect(self._direction_changed)
        self.frame_count = QSpinBox()
        self.frame_count.setObjectName("arrayCaptureFrames")
        self.frame_count.setRange(3, 200)
        self.frame_count.setValue(10)
        self.position_spin = QDoubleSpinBox()
        self.position_spin.setObjectName("arrayPosition")
        self.position_spin.setRange(-1000, 1000)
        self.position_spin.setDecimals(3)
        self.position_spin.setSingleStep(0.25)
        self.position_spin.setSuffix(" p")
        self.position_spin.setValue(-1)
        controls.addWidget(QLabel("铁板进入方向"), 0, 0)
        controls.addWidget(self.direction_combo, 0, 1)
        controls.addWidget(QLabel("每次有效帧数"), 0, 2)
        controls.addWidget(self.frame_count, 0, 3)
        self.baseline_button = QPushButton("采集独立空场")
        self.baseline_button.setObjectName("arrayCaptureBaseline")
        self.baseline_button.clicked.connect(lambda: self._start_capture("baseline"))
        self.rebase_button = QPushButton("重采当前空场（保留模板）")
        self.rebase_button.setObjectName("arrayCaptureSessionBaseline")
        self.rebase_button.setToolTip("先将铁板完全移远。采集后更新本次连接的空场，保留已标定板位。")
        self.rebase_button.clicked.connect(lambda: self._start_capture("rebase"))
        self.template_button = QPushButton("采集该位置模板")
        self.template_button.setObjectName("arrayCaptureTemplate")
        self.template_button.clicked.connect(lambda: self._start_capture("template"))
        self.cancel_button = QPushButton("取消本次采集")
        self.cancel_button.setObjectName("arrayCancelCapture")
        self.cancel_button.clicked.connect(self._cancel_clicked)
        controls.addWidget(self.baseline_button, 1, 0, 1, 2)
        controls.addWidget(self.position_spin, 1, 2)
        controls.addWidget(self.template_button, 1, 3)
        controls.addWidget(self.cancel_button, 2, 3)
        self.calibration_label = QLabel("尚未采集独立空场")
        controls.addWidget(self.calibration_label, 2, 0, 1, 3)
        controls.addWidget(self.rebase_button, 3, 0, 1, 2)
        layout.addLayout(controls)
        body = QHBoxLayout()
        maps = QVBoxLayout()
        self.delta_table = self._make_table("arrayDeltaTable")
        self.coverage_table = self._make_table("arrayCoverageTable")
        for title, table in (("当前－空场 / mV（颜色范围随帧变化）", self.delta_table),
                             ("直边几何覆盖估计 / %（？表示位置结果有分歧）", self.coverage_table)):
            group = QGroupBox(title)
            group_layout = QVBoxLayout(group)
            group_layout.addWidget(table)
            maps.addWidget(group)
        body.addLayout(maps, 3)
        templates = QVBoxLayout()
        templates.addWidget(QLabel("已采位置模板（相同位置重采会替换）"))
        self.template_list = QListWidget()
        self.template_list.setObjectName("arrayTemplateList")
        self.template_list.currentItemChanged.connect(self._update_controls)
        templates.addWidget(self.template_list)
        self.remove_button = QPushButton("删除选中位置")
        self.remove_button.setObjectName("arrayRemoveTemplate")
        self.remove_button.clicked.connect(self._remove_template)
        templates.addWidget(self.remove_button)
        self.save_button = QPushButton("保存标定…")
        self.save_button.setObjectName("arraySaveCalibration")
        self.save_button.clicked.connect(self._save_clicked)
        self.load_button = QPushButton("加载标定…")
        self.load_button.setObjectName("arrayLoadCalibration")
        self.load_button.clicked.connect(self._load_clicked)
        templates.addWidget(self.save_button)
        templates.addWidget(self.load_button)
        self.pending_button = QPushButton("取消待启用的文件")
        self.pending_button.setObjectName("arrayCancelPending")
        self.pending_button.clicked.connect(self._cancel_pending)
        templates.addWidget(self.pending_button)
        body.addLayout(templates, 2)
        layout.addLayout(body, 1)
        self.differences_label = QLabel("邻差 / mV　等待独立空场及有效帧")
        self.differences_label.setObjectName("arrayDifferences")
        self.differences_label.setWordWrap(True)
        layout.addWidget(self.differences_label)
        thresholds = QHBoxLayout()
        self.threshold_enabled = QCheckBox("启用残差拒判阈值（须用独立数据验证）")
        self.threshold_enabled.setObjectName("arrayThresholdEnabled")
        self.threshold_enabled.toggled.connect(self._threshold_changed)
        self.threshold_spin = QDoubleSpinBox()
        self.threshold_spin.setObjectName("arrayThreshold")
        self.threshold_spin.setRange(0.001, 1000000)
        self.threshold_spin.setDecimals(3)
        self.threshold_spin.setValue(3)
        self.threshold_spin.valueChanged.connect(self._threshold_changed)
        thresholds.addWidget(self.threshold_enabled)
        thresholds.addWidget(self.threshold_spin)
        thresholds.addStretch()
        layout.addLayout(thresholds)
        self.estimate_label = QLabel("位置估计　等待有效帧")
        self.estimate_label.setObjectName("arrayEstimate")
        self.estimate_label.setWordWrap(True)
        layout.addWidget(self.estimate_label)
        self.status_label = QLabel(self._message)
        self.status_label.setObjectName("arrayStatus")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status_label)
        self._clear_display()

    def _make_table(self, name):
        table = QTableWidget(4, 4)
        table.setObjectName(name)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionMode(QTableWidget.NoSelection)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.verticalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.setHorizontalHeaderLabels([f"第 {i} 列" for i in range(1, 5)])
        table.setVerticalHeaderLabels([f"第 {i} 行" for i in range(1, 5)])
        table.setMinimumHeight(145)
        for row in range(4):
            for column in range(4):
                item = QTableWidgetItem("—")
                item.setTextAlignment(Qt.AlignCenter)
                table.setItem(row, column, item)
        return table

    def set_context(self, key, channel_map, description):
        mapping = normalize_channel_map(channel_map)
        if not is_valid_map(mapping):
            key = None
            description = f"{description}；需确认完整的 16 路物理位置映射"
        token = None if key is None else (json.dumps(key, sort_keys=True, default=str), tuple(sorted(mapping.items())))
        if self.context_label.text() != str(description):
            self.context_label.setText(str(description))
        if token == self._token:
            if self._pending is not None:
                self._try_activate_pending()
            return
        self.clear_frame()
        self._model.clear_session_baseline()
        self._context_key, self._token = key, token
        self._channel_map = mapping
        if token is not None:
            model, direction = self._models.setdefault(token, (ArrayAnalysis(), "left"))
            self._model = model
            self.direction_combo.blockSignals(True)
            self.direction_combo.setCurrentIndex(self.direction_combo.findData(direction))
            self.direction_combo.blockSignals(False)
            enabled, threshold = self._thresholds.setdefault(token, (False, 3.0))
            self._set_threshold(enabled, threshold)
            self._message = "已选择数据源；可加载已有标定，或将铁板移远后采集新空场。"
        else:
            self._message = "数据源尚不可用。可先加载文件，再连接匹配的设备。"
        if self._pending is not None:
            self._try_activate_pending()
        self._refresh_templates()
        self._update_controls()
        self._render()

    def on_frame(self, values):
        if self._token is None:
            return
        try:
            frame = np.asarray(values, dtype=float)
            valid = frame.shape == (16,) and np.isfinite(frame).all()
        except (TypeError, ValueError, OverflowError):
            valid = False
        if not valid:
            self._latest_frame = None
            self._frame_error = True
            self._clear_display()
            self._message = "当前帧缺失、饱和或无效，已跳过；等待下一帧完整原始数据。"
            self.status_label.setText(self._message)
            self._update_controls()
            self.display_result_changed.emit(self.coverage_snapshot())
            return
        resumed = self._latest_frame is None or self._held
        self._held = False
        self._latest_frame = frame.copy()
        if self._frame_error:
            self._message = "有效帧已恢复，请保持板位不变继续采集。" if self._capture is not None else "完整原始帧已恢复。"
            self._frame_error = False
        if resumed:
            self._update_controls()
        if self._capture is not None:
            self._capture["frames"].append(frame.copy())
            if len(self._capture["frames"]) == self._capture["count"]:
                capture = self._capture
                self._stop_capture()
                try:
                    if capture["kind"] == "baseline":
                        self._model.set_baseline(capture["frames"])
                        self._message = "独立空场已采集，原位置模板已清除。请在已知位置驻停后采模板。"
                    elif capture["kind"] == "rebase":
                        self._model.set_session_baseline(capture["frames"])
                        shift = np.max(np.abs(self._model.session_baseline - self._model.baseline))
                        self._message = f"当前空场已更新，最大零点变化 {shift:.2f} mV。{len(self._model.positions)} 个模板继续用于本次连接。"
                    else:
                        replaced = self._model.add_template(capture["position"], capture["frames"])
                        action = "替换" if replaced else "保存"
                        self._message = f"已{action} {capture['position']:g} p 位置模板。至少两个不同位置可建立匹配。"
                except ValueError as error:
                    self._message = str(error)
                self._refresh_templates()
                self._update_controls()

    def clear_frame(self):
        was_capturing = self._capture is not None
        self._stop_capture()
        self._latest_frame = None
        self._held = False
        self._frame_error = False
        if was_capturing:
            self._message = "数据中断或工况变化，本次采集已取消。"
        self._clear_display()
        self._update_controls()
        self.display_result_changed.emit(self.coverage_snapshot())

    @property
    def _analysis(self):
        return self._model

    def hold_frame(self):
        """回放暂停或结束时保留最后一帧供查看，同时终止标定采集。"""
        self._stop_capture()
        self._held = True
        self._message = "回放已暂停或结束，显示最后一帧；恢复播放后可继续采集。"
        self._update_controls()
        self._render()

    def _start_capture(self, kind):
        if self._token is None or self._capture is not None or self._latest_frame is None or self._held or self._pending is not None:
            return
        if kind == "template" and self._model.baseline is None:
            return
        if kind == "rebase" and not self._model.has_model:
            return
        count = self.frame_count.value()
        self._capture = {"kind": kind, "position": self.position_spin.value(), "count": count, "frames": []}
        self._capture_timer.start(max(60000, count * 4000 + 10000))
        self._message = (
            "正在重采当前空场，请保持铁板远离阵列。" if kind == "rebase"
            else "请保持铁板位置与提离不变，正在收集点击后的有效帧。"
        )
        self._update_controls()

    def _stop_capture(self):
        self._capture_timer.stop()
        self._capture = None

    def _cancel_clicked(self):
        self._stop_capture()
        self._message = "本次采集已取消，已完成的标定可继续使用。"
        self._update_controls()

    def _capture_timeout(self):
        self._stop_capture()
        self._message = "采集超时，请检查完整 16 路原始数据是否持续到达后重试。"
        self._update_controls()

    def _direction_changed(self):
        if self._token is not None:
            self._models[self._token] = (self._model, self.direction_combo.currentData())
        self._render()

    def _set_threshold(self, enabled, value):
        self.threshold_enabled.blockSignals(True)
        self.threshold_spin.blockSignals(True)
        self.threshold_enabled.setChecked(enabled)
        self.threshold_spin.setValue(value)
        self.threshold_enabled.blockSignals(False)
        self.threshold_spin.blockSignals(False)

    def _threshold_changed(self, *_):
        if self._token is not None:
            self._thresholds[self._token] = (self.threshold_enabled.isChecked(), self.threshold_spin.value())
        self._update_controls()
        self._render()

    def _refresh_templates(self):
        self.template_list.clear()
        model = self._pending["model"] if self._pending is not None else self._model
        for position in model.positions:
            item = QListWidgetItem(f"{position:g} p" + ("（阵列外）" if position < 0 else ""))
            item.setData(Qt.UserRole, position)
            self.template_list.addItem(item)

    def _remove_template(self):
        item = self.template_list.currentItem()
        if item is not None and self._capture is None:
            self._model.remove_template(item.data(Qt.UserRole))
            self._refresh_templates()
            self._message = "已删除选中位置。"
            self._update_controls()

    def _update_controls(self, *_):
        active, idle = self._token is not None, self._capture is None
        pending = self._pending is not None
        collecting = active and idle and self._latest_frame is not None and not self._held and not pending
        self.baseline_button.setEnabled(collecting)
        self.baseline_button.setText("重采空场并清空模板" if self._model.positions else "采集独立空场")
        self.rebase_button.setEnabled(collecting and self._model.has_model)
        self.template_button.setEnabled(collecting and self._model.baseline is not None)
        self.cancel_button.setEnabled(not idle)
        self.direction_combo.setEnabled(idle and not self._model.positions and not pending)
        self.frame_count.setEnabled(idle)
        self.position_spin.setEnabled(idle)
        self.remove_button.setEnabled(active and idle and not pending and self.template_list.currentItem() is not None)
        self.save_button.setEnabled(active and idle and not pending and self._model.baseline is not None)
        self.load_button.setEnabled(idle)
        self.pending_button.setVisible(pending)
        self.pending_button.setEnabled(idle)
        self.threshold_enabled.setEnabled(active and not pending)
        self.threshold_spin.setEnabled(active and not pending and self.threshold_enabled.isChecked())
        if pending:
            calibration = self._pending
            direction = self.direction_combo.itemText(self.direction_combo.findData(calibration["direction"]))
            self.calibration_label.setText(f"待启用 {calibration['name']} · {len(calibration['model'].positions)} 个位置 · {direction}")
        elif active and self._model.baseline is not None:
            zero = "已重采当前空场" if self._model.session_baseline is not None else "使用原标定空场"
            self.calibration_label.setText(f"标定已启用 · {len(self._model.positions)} 个位置 · {self.direction_combo.currentText()} · {zero}")
        elif not active:
            self.calibration_label.setText("当前无已启用的数据源；支持先加载标定文件")
        else:
            self.calibration_label.setText("当前来源尚未标定；可加载已有文件或采集新空场")
        self.status_label.setText(self._pending_reason if pending else self._message)

    def _clear_display(self):
        for table in (self.delta_table, self.coverage_table):
            for row in range(4):
                for column in range(4):
                    table.item(row, column).setText("—")
                    table.item(row, column).setBackground(Qt.white)
                    table.item(row, column).setForeground(Qt.black)
        self.estimate_label.setText("位置估计　等待有效帧；残差阈值须经独立数据验证")
        self.differences_label.setText("邻差 / mV　等待独立空场及有效帧")

    def coverage_snapshot(self):
        """返回当前覆盖及状态；隐藏窗口同样计算，暂停帧保留覆盖且 fresh=False。"""
        direction = self.direction_combo.currentData()
        snapshot = {
            "coverage": np.full((4, 4), np.nan), "fresh": False,
            "corrected": None, "common_mv": None, "reference_count": 0,
            "state": "inactive", "status": self._message,
            "direction": direction, "channel_map": dict(self._channel_map),
            "position": None, "range": None, "score": None, "result": None,
            "has_model": self._token is not None and self._model.has_model,
            "threshold_enabled": self.threshold_enabled.isChecked(),
            "pending": self._pending is not None,
        }
        if self._pending is not None:
            snapshot.update(state="pending", status=self._pending_reason, has_model=False)
            return snapshot
        if self._token is None:
            return snapshot
        if self._model.baseline is None:
            snapshot.update(state="untrained", status="当前数据源尚未标定，请加载文件或先采集独立空场。")
            return snapshot
        if self._latest_frame is None:
            text = self._message if self._frame_error else "当前标定已启用，等待新的完整 16 路原始数据帧。"
            snapshot.update(state="waiting", status=text)
            return snapshot
        threshold = self.threshold_spin.value() if self.threshold_enabled.isChecked() else None
        try:
            result = self._model.analyze(self._latest_frame, threshold=threshold)
        except ValueError as error:
            snapshot.update(state="waiting", status=str(error))
            return snapshot
        if result.score is None:
            state, text = "untrained", "独立空场已启用，请再采集至少两个不同位置的模板。"
        elif result.rejected:
            guide = (
                "先移远铁板，再在「覆盖分析」中重采当前空场。"
                if self._model.session_baseline is None
                else "请核对板位、进入方向和提离；在超限位置补采模板。"
            )
            state, text = "rejected", f"加权残差 {result.score:.3f} 超过阈值 {threshold:.3f}。{guide}"
        elif result.ambiguous:
            ranges = "，".join(f"[{low:g}, {high:g}]" for low, high in result.ranges)
            state, text = "ambiguous", f"位置有歧义，兼容范围 {ranges} p；加权残差 {result.score:.3f}。"
        else:
            preview = "试算" if threshold is None else "估计"
            low, high = self._model.positions[0], self._model.positions[-1]
            state, text = "candidate", f"位置{preview} {result.position:.3f} p；标定范围 {low:g}～{high:g} p；加权残差 {result.score:.3f}。"
            if result.position in (low, high):
                text += "已到标定端点。"
        if self._held:
            state, text = "held", f"回放已暂停或结束，保留最后一帧。{text}"
        coverage = coverage_grid(result.range, direction)
        snapshot.update(
            coverage=coverage, fresh=self._model.has_model and not self._held,
            state=state, status=text, position=result.position, range=result.range, score=result.score, result=result,
        )
        if result.range is not None:
            channels = np.empty((4, 4), dtype=int)
            for channel, location in self._channel_map.items():
                channels[location] = channel
            gains = self._model.common_response_gains(channels, direction)
            corrected, common_mv, reference_count = compensate_common_response(-result.delta[channels], coverage, gains)
            snapshot.update(corrected=corrected, common_mv=common_mv, reference_count=reference_count)
        return snapshot

    def _render(self):
        snapshot = self.coverage_snapshot()
        if self._pending is not None:
            self._clear_display()
            self.estimate_label.setText("标定文件已读取，匹配来源与工况后启用；当前覆盖未知。")
            self.status_label.setText(self._pending_reason)
            self.display_result_changed.emit(snapshot)
            return
        if self._capture is not None:
            self.status_label.setText(f"{self._message}　{len(self._capture['frames'])}/{self._capture['count']} 帧")
        else:
            self.status_label.setText(self._message)
        result = snapshot["result"]
        if result is None:
            self._clear_display()
            self.estimate_label.setText(snapshot["status"])
            self.display_result_changed.emit(snapshot)
            return
        grid = np.empty((4, 4))
        labels = labels_for_map(self._channel_map)
        scale = max(float(np.max(np.abs(result.delta))), self._model.noise_floor_mv)
        coverage = snapshot["coverage"]
        for channel, (row, column) in self._channel_map.items():
            value = result.delta[channel]
            grid[row, column] = value
            item = self.delta_table.item(row, column)
            item.setText(f"{value:+.3f}")
            item.setToolTip(f"{labels[channel]} · 原始数据第 {channel + 1} 路 · 当前－空场 {value:+.6f} mV")
            background = _lerp_color(value / scale)
            item.setBackground(background)
            item.setForeground(_text_color_for(background))
            covered = self.coverage_table.item(row, column)
            covered.setText("？" if np.isnan(coverage[row, column]) else f"{coverage[row, column] * 100:.0f}%")
        horizontal, vertical = spatial_differences(grid)
        differences = np.median(horizontal, axis=0) if self.direction_combo.currentData() in ("left", "right") else np.median(vertical, axis=1)
        axis = "右减左，沿行中位数" if self.direction_combo.currentData() in ("left", "right") else "下减上，沿列中位数"
        self.differences_label.setText(f"邻差 / mV（{axis}）　" + "　".join(f"{value:+.3f}" for value in differences))
        self.estimate_label.setText(snapshot["status"])
        self.display_result_changed.emit(snapshot)

    def save_calibration(self, path):
        if self._token is None or self._model.baseline is None or self._capture is not None or self._pending is not None:
            raise ValueError("请先在有效数据源下完成独立空场采集。")
        data = {"format": "array-analysis-calibration", "version": 1, "context": self._context_key,
                "description": self.context_label.text(), "direction": self.direction_combo.currentData(),
                "threshold_enabled": self.threshold_enabled.isChecked(), "threshold": self.threshold_spin.value(),
                "channel_map": {str(ch): list(pos) for ch, pos in self._channel_map.items()},
                "analysis": self._model.to_dict()}
        target = QSaveFile(str(path))
        payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        if not target.open(QIODevice.WriteOnly):
            raise OSError(target.errorString())
        if target.write(payload) != len(payload) or not target.commit():
            raise OSError(target.errorString())
        self._last_directory = Path(path).resolve().parent

    def load_calibration(self, path):
        """读取及验证文件，兼容当前来源时启用，否则保留为待启用文件。"""
        if self._capture is not None:
            raise ValueError("请先取消或完成当前采集，再加载标定文件。")
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or data.get("format") != "array-analysis-calibration" or data.get("version") != 1:
            raise ValueError("标定文件格式或版本不支持。")
        mapping = normalize_channel_map(data.get("channel_map"))
        if not is_valid_map(mapping):
            raise ValueError("标定文件必须包含完整且无重复的 16 路物理映射。")
        direction = data.get("direction")
        if self.direction_combo.findData(direction) < 0:
            raise ValueError("标定文件的进入方向无效。")
        context = data.get("context")
        self._validate_saved_context(context)
        model = ArrayAnalysis.from_dict(data.get("analysis"))
        if model.baseline is None:
            raise ValueError("标定文件尚未保存有效空场，请选择完成空场采集的文件。")
        enabled, threshold = data.get("threshold_enabled", False), data.get("threshold", 3.0)
        if not isinstance(enabled, bool) or isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise ValueError("标定文件的残差阈值设置无效。")
        if not np.isfinite(threshold) or not self.threshold_spin.minimum() <= threshold <= self.threshold_spin.maximum():
            raise ValueError("标定文件的残差阈值超出范围。")
        self._pending = {"name": Path(path).name, "context": context, "mapping": mapping,
                         "direction": direction, "model": model, "enabled": enabled, "threshold": float(threshold)}
        self._last_directory = Path(path).resolve().parent
        applied = self._try_activate_pending()
        self._refresh_templates()
        self._update_controls()
        self._render()
        return applied

    @staticmethod
    def _validate_saved_context(saved):
        if not isinstance(saved, (list, tuple)) or len(saved) != 3:
            raise ValueError("标定文件缺少来源与工况记录。")
        source, device, conditions = saved
        if source not in ("serial", "tcp", "demo", "replay") or not isinstance(device, str):
            raise ValueError("标定文件的数据源记录无效。")
        try:
            values = dict(conditions)
            if not all(isinstance(name, str) for name in values):
                raise ValueError
            json.dumps(values, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError("标定文件的工况记录无效。")

    def _try_activate_pending(self):
        calibration = self._pending
        if calibration is None:
            return False
        try:
            if self._token is None:
                source = {"serial": "串口设备", "tcp": "TCP 设备", "demo": "演示数据源", "replay": "对应回放文件"}[calibration["context"][0]]
                raise ValueError(f"请连接保存时的{source}，并使用相同的通道布局与测量工况。")
            self._check_import_context(calibration["context"])
            if calibration["mapping"] != self._channel_map:
                raise ValueError("物理通道布局不同，请在系统设置中选择保存时的布局和线圈绑定。")
        except ValueError as error:
            reason = f"已读取 {calibration['name']}，等待启用。{error}"
            if reason != self._pending_reason:
                self._pending_reason = reason
                self._update_controls()
                self._render()
            return False
        # 先完成状态切换再发信号，允许主窗口回调安全地再次同步来源。
        self._pending = None
        self._pending_reason = ""
        self._model = calibration["model"]
        self._models[self._token] = (self._model, calibration["direction"])
        self._thresholds[self._token] = (calibration["enabled"], calibration["threshold"])
        self._set_threshold(calibration["enabled"], calibration["threshold"])
        self.direction_combo.blockSignals(True)
        self.direction_combo.setCurrentIndex(self.direction_combo.findData(calibration["direction"]))
        self.direction_combo.blockSignals(False)
        self._message = f"已启用 {calibration['name']}。保持标定时的铁板、提离、频率和增益，移动铁板即可查看覆盖估计。"
        if not self._model.has_model:
            self._message += " 请继续采集至少两个不同位置的模板。"
        self._refresh_templates()
        self._update_controls()
        self._render()
        self.calibration_applied.emit()
        return True

    def _cancel_pending(self):
        self._pending = None
        self._pending_reason = ""
        self._message = "已取消待启用文件。可选择其他文件，或在当前数据源下采集标定。"
        self._refresh_templates()
        self._update_controls()
        self._render()

    @staticmethod
    def _same_device(first, second):
        if first == second:
            return True
        pattern = r"USB VID:PID=([0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}) SER=(\S+)(?: LOCATION=\S+)?"
        a, b = re.fullmatch(pattern, first), re.fullmatch(pattern, second)
        return bool(a and b and a[1].upper() == b[1].upper() and a[2] == b[2])

    def _check_import_context(self, saved):
        if not isinstance(saved, (list, tuple)) or len(saved) != 3:
            raise ValueError("标定文件缺少来源与工况记录。")
        source, device, conditions = saved
        current_source, current_device, current_conditions = self._context_key
        known_real = ("serial", "tcp")
        if (source == "demo" and current_source in known_real) or (current_source == "demo" and source in known_real):
            raise ValueError("演示标定与真实设备标定不能混用。")
        if source in known_real and current_source in known_real and device and current_device and not self._same_device(device, current_device):
            raise ValueError(f"设备标识不同，请连接保存时的设备（{device}）。")
        try:
            saved_conditions, current = dict(conditions), dict(current_conditions)
        except (TypeError, ValueError):
            raise ValueError("标定文件的工况记录无效。")
        saved_origin = saved_conditions.get("recorded_source_scope") or source
        current_origin = current.get("recorded_source_scope") or current_source
        if (saved_origin == "demo" and current_origin in known_real) or (current_origin == "demo" and saved_origin in known_real):
            raise ValueError("演示标定与真实设备标定不能混用。")
        ignored = {"connection", "load_revision", "recorded_source_scope"}
        for name in saved_conditions.keys() & current.keys() - ignored:
            old, new = saved_conditions[name], current[name]
            if old not in (None, "") and new not in (None, "") and json.dumps(old, sort_keys=True) != json.dumps(new, sort_keys=True):
                title = {"adc_profile": "ADC 量程配置", "frequency": "激励频率", "freq_unit": "频率单位", "gain": "增益", "coil": "线圈", "plate": "铁板信息"}.get(name, name)
                raise ValueError(f"{title}不匹配，文件为 {old}，当前为 {new}。请在系统设置中匹配工况。")

    def _save_clicked(self):
        path, _ = QFileDialog.getSaveFileName(self, "保存阵列标定", str(self._last_directory / "阵列标定.json"), "JSON 文件 (*.json)")
        if path:
            try:
                self.save_calibration(path)
                self._message = f"标定已保存到 {path}"
            except (OSError, ValueError, TypeError) as error:
                self._message = f"保存失败：{error}"
            self._update_controls()

    def _load_clicked(self):
        path, _ = QFileDialog.getOpenFileName(self, "加载阵列标定", str(self._last_directory), "JSON 文件 (*.json)")
        if path:
            try:
                self.load_calibration(path)
            except (OSError, ValueError, TypeError, KeyError) as error:
                self._message = f"加载失败：{error}"
                self._update_controls()

    def closeEvent(self, event):
        was_capturing = self._capture is not None
        self._stop_capture()
        if was_capturing:
            self._message = "已关闭标定窗口，本次采集已取消；主界面继续显示当前分析。"
        self._update_controls()
        super().closeEvent(event)
