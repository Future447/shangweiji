"""
ui/replay_controls.py — 回放模式控制条。

按钮：打开 CSV / 播放 / 暂停 / 停止 / 0.5x / 1x / 2x / 5x + 时间滑块。
所有交互通过信号转发给 MainWindow。
"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QStyle,
    QVBoxLayout,
    QWidget,
)


class ReplayControls(QFrame):
    """回放模式工具条。"""

    open_csv_requested = pyqtSignal()
    play_requested = pyqtSignal()
    pause_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    seek_requested = pyqtSignal(int)        # row index
    speed_requested = pyqtSignal(float)     # 倍速
    state_changed = pyqtSignal(str)         # 状态镜像

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("ReplayControls")
        self._build_ui()
        self.set_speed(1.0)
        self.set_state("stopped")

    def _build_ui(self) -> None:
        v = QVBoxLayout()
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(4)

        style = self.style()

        # 顶部按钮行
        row = QHBoxLayout()
        row.setSpacing(6)

        self._btn_open = QPushButton()
        self._btn_open.setIcon(style.standardIcon(QStyle.SP_DirOpenIcon))
        self._btn_open.setText("打开 CSV")
        self._btn_open.setProperty("role", "secondary")
        self._btn_open.clicked.connect(self.open_csv_requested.emit)
        self._btn_open.setAccessibleName("打开回放 CSV")
        row.addWidget(self._btn_open)

        self._btn_play = QPushButton()
        self._btn_play.setIcon(style.standardIcon(QStyle.SP_MediaPlay))
        self._btn_play.setText("播放")
        self._btn_play.setProperty("role", "primary")
        self._btn_play.clicked.connect(self.play_requested.emit)
        self._btn_play.setAccessibleName("开始回放")
        row.addWidget(self._btn_play)

        self._btn_pause = QPushButton()
        self._btn_pause.setIcon(style.standardIcon(QStyle.SP_MediaPause))
        self._btn_pause.setText("暂停")
        self._btn_pause.setProperty("role", "secondary")
        self._btn_pause.clicked.connect(self.pause_requested.emit)
        self._btn_pause.setAccessibleName("暂停回放")
        row.addWidget(self._btn_pause)

        self._btn_stop = QPushButton()
        self._btn_stop.setIcon(style.standardIcon(QStyle.SP_MediaStop))
        self._btn_stop.setText("停止")
        self._btn_stop.setProperty("role", "ghost")
        self._btn_stop.clicked.connect(self.stop_requested.emit)
        self._btn_stop.setAccessibleName("停止回放")
        row.addWidget(self._btn_stop)

        row.addSpacing(8)
        row.addWidget(QLabel("速度:"))
        self._cmb_speed = QComboBox()
        for s in ("0.5x", "1x", "2x", "5x"):
            self._cmb_speed.addItem(s)
        self._cmb_speed.setCurrentText("1x")
        self._cmb_speed.currentTextChanged.connect(self._on_speed_changed)
        self._cmb_speed.setAccessibleName("回放速度")
        row.addWidget(self._cmb_speed)

        row.addStretch(1)
        self._lbl_state = QLabel("未加载")
        self._lbl_state.setObjectName("Caption")
        row.addWidget(self._lbl_state)
        v.addLayout(row)

        # 时间滑块
        srow = QHBoxLayout()
        srow.setSpacing(6)
        self._slider = QSlider(Qt.Horizontal)
        self._slider.setMinimum(0)
        self._slider.setMaximum(0)
        self._slider.setValue(0)
        self._slider.setEnabled(False)
        self._slider.setAccessibleName("回放时间位置")
        self._slider.sliderMoved.connect(self.seek_requested.emit)
        srow.addWidget(QLabel("时间:"))
        srow.addWidget(self._slider, 1)
        self._lbl_t = QLabel("0/0")
        self._lbl_t.setObjectName("Caption")
        srow.addWidget(self._lbl_t)
        v.addLayout(srow)

        self.setLayout(v)

    def _on_speed_changed(self, text: str) -> None:
        try:
            speed = float(text.rstrip("x"))
        except ValueError:
            return
        self.speed_requested.emit(speed)

    def set_speed(self, speed: float) -> None:
        text = f"{speed:g}x"
        idx = self._cmb_speed.findText(text)
        if idx >= 0:
            self._cmb_speed.setCurrentIndex(idx)

    def set_state(self, state: str) -> None:
        self._state = state
        text_map = {
            "stopped": "已停止",
            "playing": "播放中",
            "paused": "已暂停",
            "ended": "已结束",
        }
        self._lbl_state.setText(text_map.get(state, state))
        self.state_changed.emit(state)
        playing = state == "playing"
        self._btn_play.setEnabled(state in ("stopped", "paused", "ended"))
        self._btn_pause.setEnabled(playing)
        self._btn_stop.setEnabled(state in ("playing", "paused"))

    def set_loaded(self, total_rows: int) -> None:
        self._slider.blockSignals(True)
        self._slider.setMaximum(max(0, total_rows - 1))
        self._slider.setValue(0)
        self._slider.setEnabled(total_rows > 0)
        self._slider.blockSignals(False)
        self._lbl_t.setText(f"0/{total_rows}")

    def set_index(self, index: int) -> None:
        self._slider.blockSignals(True)
        self._slider.setValue(index)
        self._slider.blockSignals(False)
        self._lbl_t.setText(f"{index}/{self._slider.maximum() + 1}")

    def set_open_path(self, path: str) -> None:
        """打开后，更新标签显示。"""
        if path:
            import os
            self._lbl_state.setText(os.path.basename(path))
