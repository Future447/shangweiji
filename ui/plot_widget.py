"""
ui/plot_widget.py — 实时曲线（自包含的曲线模块）。

UI 重构要点（v2.1）：
- 自身就是完整模块：标题 + 紧凑两行工具栏 + pyqtgraph 画布
- 第一行：标题/通道 → 暂停/继续 → 清空
- 第二行：原始/滤波 显隐 → 自适应 → 复位
- v3：Y 轴为 ADC输入 (mV)，光标读数 3 位小数（424.625 mV）
- 全部使用 QToolButton 紧凑样式（~28px 高），不会撑大右栏
- 状态信息通道标签可伸缩，不会裁切其他按钮
- 与 main_window 中的 SectionHeader 不再重复（外层不再包 SectionHeader）
- 暂停/光标读数/降采样/cliptoview 业务逻辑保持不变
"""

from __future__ import annotations

import math
import time
from typing import Dict, List, Optional

import pyqtgraph as pg
from PyQt5.QtCore import QSize, Qt
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ui.theme import DATA_VIZ, PALETTE, font_family_mono, font_family_ui


def _mono_family() -> str:
    return font_family_mono().split(",")[0].strip()


def _ui_family() -> str:
    return font_family_ui().split(",")[0].strip()


def _compact_tool_button(
    text: str,
    tooltip: str,
    accessible_name: str,
    checkable: bool = False,
    checked: bool = False,
) -> QToolButton:
    """创建 ~28px 高的紧凑工具按钮（仅本模块使用，不影响全局样式）。"""
    btn = QToolButton()
    btn.setText(text)
    btn.setToolTip(tooltip)
    btn.setAccessibleName(accessible_name)
    btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
    btn.setCheckable(checkable)
    btn.setChecked(checked)
    btn.setProperty("role", "plotTool")
    btn.setFocusPolicy(Qt.StrongFocus)
    btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
    # 紧凑高度；宽度按内容
    fm = btn.fontMetrics()
    btn.setFixedHeight(28)
    btn.setMinimumWidth(max(48, fm.horizontalAdvance(text) + 24))
    return btn


class PlotWidget(QWidget):
    """实时曲线组件（自包含：标题+工具栏+画布）。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._window_s = 30.0
        self._max_points = 600
        self._paused = False
        self._show_raw = True
        self._show_filtered = True
        # 视图模式："voltage"（采样/滤波电压 + 基线参考线）
        # 或 "attenuation"（相对无钢板基线的衰减量 + 零参考线）
        self._view_mode: str = "voltage"
        self._selected_channel: Optional[int] = None
        #: {数据通道: 显示标签}；空 = 按 CH(通道+1)
        self._channel_labels: Dict[int, str] = {}
        self._pinned_channels: List[int] = []
        self._curves_raw: Dict[int, "pg.PlotDataItem"] = {}
        self._curves_filt: Dict[int, "pg.PlotDataItem"] = {}
        self._curves_atten: Dict[int, "pg.PlotDataItem"] = {}
        self._baseline_lines: Dict[int, "pg.InfiniteLine"] = {}
        self._zero_line: Optional["pg.InfiniteLine"] = None
        self._t0: float = time.monotonic()
        self._t_offsets: Dict[int, List[float]] = {}
        self._raw_series: Dict[int, List[float]] = {}
        self._filt_series: Dict[int, List[float]] = {}
        # 衰减量序列独立成册：逐点记录当时基线下的衰减值，
        # 基线变更后清空（绝不用新基线静默重算旧数据）
        self._atten_t: Dict[int, List[float]] = {}
        self._atten_series: Dict[int, List[float]] = {}
        self._cursor_label: Optional[QLabel] = None
        self._cursor_t: float = 0.0
        self._cursor_ch: Optional[int] = None
        # 最近一次发出的时间戳（所有曲线共享同一时间域）：
        # 时间倒退（回放 seek 回退）→ 清空全部序列重建，绝不倒序连线
        self._last_t: Optional[float] = None

        # 不再使用 SurfaceCard 的双重内边距
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._build_ui()
        self._configure_pyqtgraph()

    def _build_ui(self) -> None:
        # 极小外边距：外层 SurfaceCard 已经有 12/10 边距
        v = QVBoxLayout()
        v.setContentsMargins(2, 2, 2, 2)
        v.setSpacing(4)

        # 标题行（PlotWidget 自带的"实时曲线"标题，作为唯一标题）
        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        self._lbl_title = QLabel("实时曲线")
        title_font = QFont(_ui_family(), 11)
        title_font.setBold(True)
        self._lbl_title.setFont(title_font)
        self._lbl_title.setAccessibleName("实时曲线标题")
        title_row.addWidget(self._lbl_title)
        title_row.addSpacing(4)
        self._lbl_info = QLabel("未选中通道")
        self._lbl_info.setObjectName("Caption")
        self._lbl_info.setAccessibleName("选中通道信息")
        self._lbl_info.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Preferred
        )
        self._lbl_info.setMinimumWidth(0)
        title_row.addWidget(self._lbl_info, 1)
        v.addLayout(title_row)

        # 第一行工具栏：暂停 / 清空
        row1 = QHBoxLayout()
        row1.setSpacing(4)
        self._btn_pause = _compact_tool_button(
            "暂停", "暂停/继续绘制曲线（Space）",
            "暂停或继续曲线", checkable=True,
        )
        self._btn_pause.toggled.connect(self._on_pause_toggled)
        row1.addWidget(self._btn_pause)
        self._btn_clear = _compact_tool_button(
            "清空", "清空当前显示的曲线数据",
            "清空曲线",
        )
        self._btn_clear.clicked.connect(self.clear)
        row1.addWidget(self._btn_clear)
        self._btn_atten_view = _compact_tool_button(
            "衰减视图",
            "切换 电压视图 / 衰减量视图：\n"
            "电压视图 = 采样/滤波检波电压 + 基线参考线；\n"
            "衰减量视图 = 相对无钢板基线的衰减量 + 零参考线",
            "切换衰减量视图",
            checkable=True,
        )
        self._btn_atten_view.toggled.connect(self._on_atten_view_toggled)
        row1.addWidget(self._btn_atten_view)
        row1.addStretch(1)
        v.addLayout(row1)

        # 第二行工具栏：raw/filtered 显隐 + 自适应 + 复位
        row2 = QHBoxLayout()
        row2.setSpacing(4)
        # 使用 QCheckBox 但保持紧凑
        lbl_show = QLabel("显示:")
        lbl_show.setObjectName("Caption")
        row2.addWidget(lbl_show)
        self._chk_raw = QCheckBox("原始")
        self._chk_raw.setChecked(self._show_raw)
        self._chk_raw.setToolTip("切换 raw 曲线显示")
        self._chk_raw.setAccessibleName("显示原始曲线")
        self._chk_raw.toggled.connect(self._on_visibility_toggled)
        row2.addWidget(self._chk_raw)
        self._chk_filt = QCheckBox("滤波")
        self._chk_filt.setChecked(self._show_filtered)
        self._chk_filt.setToolTip("切换 filtered 曲线显示")
        self._chk_filt.setAccessibleName("显示滤波曲线")
        self._chk_filt.toggled.connect(self._on_visibility_toggled)
        row2.addWidget(self._chk_filt)
        row2.addStretch(1)
        self._btn_autorange = _compact_tool_button(
            "自适应", "自动调整曲线显示范围（Y 轴）",
            "自动范围",
        )
        self._btn_autorange.clicked.connect(self._on_autorange)
        row2.addWidget(self._btn_autorange)
        self._btn_restore = _compact_tool_button(
            "复位", "还原到完整 X 时间窗口 + 自动 Y 范围",
            "还原视图",
        )
        self._btn_restore.clicked.connect(self._on_restore)
        row2.addWidget(self._btn_restore)
        v.addLayout(row2)

        # 图表
        self._plot = pg.PlotWidget()
        self._plot.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding
        )
        v.addWidget(self._plot, 1)

        self.setLayout(v)

        # 暂停覆盖标签（贴在 plot 内部）
        self._lbl_paused = QLabel("曲线已暂停", self._plot)
        self._lbl_paused.setObjectName("PlotPausedOverlay")
        self._lbl_paused.setAlignment(Qt.AlignCenter)
        self._lbl_paused.setVisible(False)
        self._lbl_paused.setFixedSize(140, 36)
        self._lbl_paused.move(12, 12)

        # 光标读数浮层（位置由 resizeEvent 决定）
        self._lbl_cursor = QLabel("", self._plot)
        self._lbl_cursor.setObjectName("PlotCursorReadout")
        self._lbl_cursor.setVisible(False)
        self._lbl_cursor.setFixedSize(200, 56)
        self._lbl_cursor.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self._lbl_cursor.setWordWrap(True)

        # 鼠标移动光标读数
        self._plot.scene().sigMouseMoved.connect(self._on_mouse_moved)
        # 长时序列优化
        try:
            self._plot.setClipToView(True)
            self._plot.setDownsampling(ds=3, auto=True, method="peak")
            self._plot.setMouseEnabled(x=True, y=True)
        except Exception:
            pass

    def _configure_pyqtgraph(self) -> None:
        bg = QColor(PALETTE.surface)
        grid = QColor(PALETTE.border)
        axis = QColor(PALETTE.text_secondary)
        self._plot.setBackground(bg)
        self._plot.showGrid(x=True, y=True, alpha=0.5)
        for axis_name in ("bottom", "left"):
            ax = self._plot.getAxis(axis_name)
            ax.setPen(grid)
            ax.setTextPen(axis)
            ax.setStyle(tickFont=QFont(_mono_family(), 8))
        label_style = {"color": PALETTE.text_secondary, "font-size": "9pt"}
        self._axis_label_style = label_style
        # 简洁坐标轴标签（两种视图使用不同纵轴，绝不混排未标注曲线）
        self._plot.setLabel("left", "ADC输入 (mV)", **label_style)
        self._plot.setLabel("bottom", "时间 (s)", **label_style)
        # 零参考线：仅衰减量视图显示
        self._zero_line = pg.InfiniteLine(
            pos=0.0, angle=0, movable=False,
            pen=pg.mkPen(QColor(PALETTE.text_secondary), width=1,
                         style=Qt.DashLine),
        )
        self._zero_line.setVisible(False)
        self._plot.addItem(self._zero_line)
        # 图例：紧凑、不遮挡主波形
        try:
            self._plot.addLegend(
                offset=(-10, 10), labelTextSize="8pt", verSpacing=-2
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        # 光标读数浮层保持在画布右上角且不跑出画布
        if self._lbl_cursor is not None:
            w = self._lbl_cursor.width()
            h = self._lbl_cursor.height()
            x = max(4, self._plot.width() - w - 8)
            y = 8
            # 必要时贴左上
            if x + w > self._plot.width() - 4:
                x = 8
                y = max(8, self._plot.height() - h - 8)
            self._lbl_cursor.move(x, y)

    def set_window(self, window_s: float) -> None:
        self._window_s = max(5.0, float(window_s))
        self._max_points = max(60, int(self._window_s * 20))

    # ------------------------------------------------------------------
    # 格子标签（数据通道 → 显示标签）
    # ------------------------------------------------------------------
    def _label(self, channel: int) -> str:
        return self._channel_labels.get(
            channel, f"CH{channel + 1:02d}"
        )

    def set_channel_labels(self, labels: Dict[int, str]) -> None:
        """设置数据通道对应的显示标签（有源板换绑后 CHxx 不再等于索引）。

        曲线本身仍按数据通道绘制；只有图例/读数文本使用标签。
        """
        self._channel_labels = dict(labels or {})
        for curves, suffix in (
            (self._curves_raw, "采样"),
            (self._curves_filt, "滤波"),
        ):
            for ch, curve in curves.items():
                try:
                    curve.opts["name"] = f"{self._label(ch)} {suffix}"
                except Exception:
                    pass
        for ch, curve in self._curves_atten.items():
            try:
                curve.opts["name"] = f"{self._label(ch)} 衰减"
            except Exception:
                pass
        legend = getattr(self._plot, "legend", None)
        if legend is not None and hasattr(legend, "updateLabels"):
            try:
                legend.updateLabels()
            except Exception:
                pass
        if getattr(self, "_selected_channel", None) is not None:
            self.set_selected_channel(self._selected_channel)

    def set_selected_channel(self, channel: int) -> None:
        self._selected_channel = channel
        if channel is not None:
            self._lbl_info.setText(f"选中: {self._label(channel)}")
        else:
            self._lbl_info.setText("未选中通道")

    def set_pinned_channels(self, channels: List[int]) -> None:
        self._pinned_channels = list(channels)[:4]
        self._rebuild_curves()

    def _on_pause_toggled(self, checked: bool) -> None:
        self._paused = bool(checked)
        self._btn_pause.setText("继续" if checked else "暂停")
        self._lbl_paused.setVisible(checked)

    def _on_visibility_toggled(self) -> None:
        self._show_raw = self._chk_raw.isChecked()
        self._show_filtered = self._chk_filt.isChecked()
        self._apply_visibility()

    def _on_atten_view_toggled(self, checked: bool) -> None:
        self._view_mode = "attenuation" if checked else "voltage"
        self._btn_atten_view.setText("电压视图" if checked else "衰减视图")
        # 两种视图语义/参考线不同，纵轴标签随之切换并显式标注
        self._plot.setLabel(
            "left",
            "衰减量 (mV)" if checked else "ADC输入 (mV)",
            **self._axis_label_style,
        )
        self._apply_visibility()

    def _apply_visibility(self) -> None:
        atten = self._view_mode == "attenuation"
        for c, curve in self._curves_raw.items():
            curve.setVisible(not atten and self._show_raw)
        for c, curve in self._curves_filt.items():
            curve.setVisible(not atten and self._show_filtered)
        for c, curve in self._curves_atten.items():
            curve.setVisible(atten)
        for c, line in self._baseline_lines.items():
            line.setVisible(not atten)
        if self._zero_line is not None:
            self._zero_line.setVisible(atten)

    def _on_autorange(self) -> None:
        try:
            self._plot.enableAutoRange()
        except Exception:
            pass

    def _on_restore(self) -> None:
        try:
            self._plot.getViewBox().autoRange()
        except Exception:
            pass

    @staticmethod
    def _nearest_idx(xs: List[float], t: float) -> int:
        lo, hi = 0, len(xs) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if xs[mid] < t:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def _on_mouse_moved(self, scene_pos) -> None:
        try:
            view = self._plot.getViewBox()
            if view is None:
                return
            mouse_point = view.mapSceneToView(scene_pos)
            t = float(mouse_point.x())
            atten = self._view_mode == "attenuation"
            t_map = self._atten_t if atten else self._t_offsets
            best_ch = None
            best_dt = float("inf")
            for ch, xs in t_map.items():
                if not xs:
                    continue
                idx = self._nearest_idx(xs, t)
                dt = abs(xs[idx] - t)
                if dt < best_dt:
                    best_dt = dt
                    best_ch = ch
            if best_ch is None:
                return
            xs = t_map[best_ch]
            idx = self._nearest_idx(xs, t)
            t_at = xs[idx]
            lines = [f"t = {t_at:.2f} s"]
            if atten:
                v = (
                    self._atten_series[best_ch][idx]
                    if idx < len(self._atten_series.get(best_ch, []))
                    else None
                )
                if v is not None and v == v:  # 非 None 且非 NaN
                    lines.append(
                        f"{self._label(best_ch)} 衰减: {v:+.3f} mV"
                    )
            else:
                raw_v = (
                    self._raw_series[best_ch][idx]
                    if idx < len(self._raw_series.get(best_ch, []))
                    else None
                )
                filt_v = (
                    self._filt_series[best_ch][idx]
                    if idx < len(self._filt_series.get(best_ch, []))
                    else None
                )
                if raw_v is not None and raw_v == raw_v:  # 非 None 且非 NaN
                    lines.append(
                        f"{self._label(best_ch)} 采样: {raw_v:.3f} mV"
                    )
                if filt_v is not None and filt_v == filt_v:
                    lines.append(
                        f"{self._label(best_ch)} 滤波: {filt_v:.3f} mV"
                    )
            self._lbl_cursor.setText("\n".join(lines))
            self._lbl_cursor.setVisible(True)
            self._cursor_t = t_at
            self._cursor_ch = best_ch
        except Exception:
            self._lbl_cursor.setVisible(False)

    def clear(self) -> None:
        for c, curve in list(self._curves_raw.items()):
            self._plot.removeItem(curve)
        for c, curve in list(self._curves_filt.items()):
            self._plot.removeItem(curve)
        self._clear_attenuation_items()
        self._curves_raw.clear()
        self._curves_filt.clear()
        self._raw_series.clear()
        self._filt_series.clear()
        self._t_offsets.clear()
        self._t0 = time.monotonic()
        self._last_t = None

    def _clear_attenuation_items(self) -> None:
        """移除衰减曲线与数据（保留基线参考线的当前值）。"""
        for c, curve in list(self._curves_atten.items()):
            self._plot.removeItem(curve)
        self._curves_atten.clear()
        self._atten_t.clear()
        self._atten_series.clear()

    def notify_baseline_changed(self) -> None:
        """基线（重新标定/切换来源）已变更：清空派生衰减曲线。

        衰减量 = 基线 − 滤波，属于"当时基线"下的派生数据；基线变更后
        旧衰减点不再可比，清空而不是用新基线静默重算旧实验数据。
        """
        self._clear_attenuation_items()

    def set_baseline(self, channel: int, baseline_mv: Optional[float]) -> None:
        """设置/更新某通道的无钢板基线参考线（电压视图虚线）。"""
        line = self._baseline_lines.get(channel)
        if baseline_mv is None:
            if line is not None:
                self._plot.removeItem(line)
                del self._baseline_lines[channel]
            return
        if line is None:
            color = self._channel_color(channel)
            line = pg.InfiniteLine(
                pos=float(baseline_mv), angle=0, movable=False,
                pen=pg.mkPen(color, width=1, style=Qt.DashLine),
            )
            self._baseline_lines[channel] = line
            self._plot.addItem(line)
        else:
            line.setPos(float(baseline_mv))
        line.setVisible(self._view_mode == "voltage")

    def set_paused(self, paused: bool) -> None:
        self._btn_pause.setChecked(bool(paused))

    def is_paused(self) -> bool:
        return self._paused

    # ------------------------------------------------------------------
    def _visible_channels(self) -> List[int]:
        result: List[int] = []
        if self._selected_channel is not None:
            result.append(self._selected_channel)
        for c in self._pinned_channels:
            if c != self._selected_channel and c not in result:
                result.append(c)
        return result

    def visible_channels(self) -> List[int]:
        """当前有曲线的通道（选中 + 固定），供外部同步基线参考线。"""
        return self._visible_channels()

    def _channel_color(self, channel: int) -> QColor:
        if channel == self._selected_channel:
            return QColor(DATA_VIZ.curve_filtered)
        try:
            idx = self._pinned_channels.index(channel)
            return QColor(DATA_VIZ.curve_pinned[idx % len(DATA_VIZ.curve_pinned)])
        except ValueError:
            return QColor(DATA_VIZ.curve_filtered)

    def _rebuild_curves(self) -> None:
        keep = set(self._visible_channels())
        for ch, curve in list(self._curves_raw.items()):
            if ch not in keep:
                self._plot.removeItem(curve)
                del self._curves_raw[ch]
        for ch, curve in list(self._curves_filt.items()):
            if ch not in keep:
                self._plot.removeItem(curve)
                del self._curves_filt[ch]
        for ch, curve in list(self._curves_atten.items()):
            if ch not in keep:
                self._plot.removeItem(curve)
                del self._curves_atten[ch]
        for ch in keep:
            if ch not in self._curves_raw:
                color = self._channel_color(ch)
                raw_curve = self._plot.plot(
                    pen=pg.mkPen(QColor(DATA_VIZ.curve_raw), width=1),
                    name=f"{self._label(ch)} 采样",
                )
                filt_curve = self._plot.plot(
                    pen=pg.mkPen(color, width=2, style=Qt.SolidLine),
                    name=f"{self._label(ch)} 滤波",
                )
                self._curves_raw[ch] = raw_curve
                self._curves_filt[ch] = filt_curve
        self._apply_visibility()

    def _resolve_t(self, t: Optional[float]) -> float:
        """解析点时间戳：显式值（有限）优先；缺失/非有限回退内部时钟。

        - 实时路径传会话相对接收时间；回放路径传文件 t_rel（实验
          原始时间）——X 轴语义由调用方保证在同一会话内一致
          （会话/来源切换时外层会先 clear()）。
        - 缺失（None）或非有限（NaN/Inf）的原始时间：回退内部单调
          时钟，保证曲线仍可追加（显式策略：不丢弃数据点）。
        """
        if t is not None:
            try:
                ft = float(t)
            except (TypeError, ValueError):
                ft = float("nan")
            if math.isfinite(ft):
                return ft
        return time.monotonic() - self._t0

    def _clear_series_data(self) -> None:
        """清空全部点序列（保留曲线项/基线参考线/_t0）。

        时间倒退（回放 seek 回退）时调用：旧窗口属于"未来"，
        继续追加会造成倒序连线；清空后从新位置重建。
        """
        self._t_offsets.clear()
        self._raw_series.clear()
        self._filt_series.clear()
        self._atten_t.clear()
        self._atten_series.clear()
        for curve in self._curves_raw.values():
            curve.setData([], [])
        for curve in self._curves_filt.values():
            curve.setData([], [])
        for curve in self._curves_atten.values():
            curve.setData([], [])

    def _check_time_regression(self, t_now: float) -> None:
        """检测时间倒退并清窗重建（所有曲线共享同一时间域）。"""
        if self._last_t is not None and t_now < self._last_t:
            self._clear_series_data()
        self._last_t = t_now

    def add_point(
        self,
        channel: int,
        sample_mv: Optional[float],
        filtered_mv: Optional[float],
        t: Optional[float] = None,
    ) -> None:
        """追加一个采样点（单位均为 mV，v3 协议）。

        t：显式时间戳（秒）。实时 = 会话相对接收时间；回放 = 文件
        实验时间 t_rel（与回放速度/暂停无关；seek 回退触发清窗重建，
        绝不倒序连线）。None/非有限 → 内部单调时钟。

        None（本帧无此量）写成 NaN 断点，三条平行数组始终等长——
        不得出现按"当前点是否为 None"决定淘汰而错位、
        setData 长度不匹配的情况。
        """
        if self._paused:
            return
        t_now = self._resolve_t(t)
        self._check_time_regression(t_now)
        if channel not in self._raw_series:
            self._raw_series[channel] = []
            self._filt_series[channel] = []
            self._t_offsets[channel] = []
            self._rebuild_curves()

        cutoff = t_now - self._window_s
        while (
            self._t_offsets[channel]
            and self._t_offsets[channel][0] < cutoff
        ):
            self._t_offsets[channel].pop(0)
            self._raw_series[channel].pop(0)
            self._filt_series[channel].pop(0)

        self._t_offsets[channel].append(t_now)
        self._raw_series[channel].append(
            float("nan") if sample_mv is None else float(sample_mv)
        )
        self._filt_series[channel].append(
            float("nan") if filtered_mv is None else float(filtered_mv)
        )

        if len(self._t_offsets[channel]) > self._max_points:
            excess = len(self._t_offsets[channel]) - self._max_points
            self._t_offsets[channel] = self._t_offsets[channel][excess:]
            self._raw_series[channel] = self._raw_series[channel][excess:]
            self._filt_series[channel] = self._filt_series[channel][excess:]

        self._rebuild_curves()
        if channel in self._curves_raw:
            self._curves_raw[channel].setData(
                self._t_offsets[channel], self._raw_series[channel],
                connect="finite",
            )
        if channel in self._curves_filt:
            self._curves_filt[channel].setData(
                self._t_offsets[channel], self._filt_series[channel],
                connect="finite",
            )
        self._apply_visibility()

    def add_attenuation_point(
        self,
        channel: int,
        attenuation_mv: Optional[float],
        t: Optional[float] = None,
    ) -> None:
        """追加一个衰减量点（相对无钢板基线，mV；当时基线下的值）。

        t：与 add_point 同一时间域（实时会话相对时间 / 回放 t_rel）。
        None（当前帧无效/未标定/未知）写成 NaN 断点，不跨无效段连线，
        也绝不回填上一帧残留值。
        """
        if self._paused:
            return
        t_now = self._resolve_t(t)
        self._check_time_regression(t_now)
        if channel not in self._atten_series:
            self._atten_series[channel] = []
            self._atten_t[channel] = []
        cutoff = t_now - self._window_s
        while (
            self._atten_t[channel]
            and self._atten_t[channel][0] < cutoff
        ):
            self._atten_t[channel].pop(0)
            self._atten_series[channel].pop(0)

        self._atten_t[channel].append(t_now)
        v = float("nan") if attenuation_mv is None else float(attenuation_mv)
        self._atten_series[channel].append(v)

        if len(self._atten_t[channel]) > self._max_points:
            excess = len(self._atten_t[channel]) - self._max_points
            self._atten_t[channel] = self._atten_t[channel][excess:]
            self._atten_series[channel] = self._atten_series[channel][excess:]

        # 衰减曲线按需创建（与可见通道集合一致）
        if channel in self._visible_channels() and \
                channel not in self._curves_atten:
            color = self._channel_color(channel)
            self._curves_atten[channel] = self._plot.plot(
                pen=pg.mkPen(color, width=2, style=Qt.SolidLine),
                name=f"{self._label(channel)} 衰减",
            )
        if channel in self._curves_atten:
            self._curves_atten[channel].setData(
                self._atten_t[channel], self._atten_series[channel],
                connect="finite",
            )
        self._apply_visibility()

    # ------------------------------------------------------------------
    def sizeHint(self) -> QSize:  # noqa: N802
        # 极小 sizeHint：不强制大尺寸，让 layout 自然分配
        return QSize(360, 220)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        # 工具栏 + 图例 + 至少 140px 画布
        return QSize(280, 160)
