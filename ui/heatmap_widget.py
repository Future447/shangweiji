"""
ui/heatmap_widget.py — 4×4 阵列热力图。

UI 重构要点：
- 物理位置由 config.channel_map 决定（dict[channel] = (row, col)）
- 选中用冷蓝外框 + 浅色内描边（红蓝背景上都清楚）
- SATURATED 使用危险边框 + 文字 + 斜线纹理（红色已是正 delta）
- NO_DATA / INVALID / NOT_CALIBRATED / DISABLED 通过文字和明度区分
- 单元格 tooltip：采样/滤波/基线/Δ/Vadc/VOUT/valid/最后更新（全部 mV）
- 键盘方向键切换通道（按物理位置移动），Enter 确认选择
- accessibleName + accessibleDescription 随数据更新
- 颜色对比度在最深两端 ≥ 4.5:1
- 增加与网格同宽的色阶图例（外部组件可放 HeatmapLegend）
- 覆盖显示使用物理格子面积比例，固定 0–100% 色阶，未知值灰显
- 补偿后响应独立显示有符号 mV，缺少补偿结果时显示未知
"""

from __future__ import annotations

import logging
import math
import time
from typing import Dict, Optional, Sequence, Tuple

from PyQt5.QtCore import QEvent, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QPainter, QPen
from PyQt5.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.channel_layout import (
    HARDWARE_CHANNEL_MAP,
    normalize_channel_map,
    validate_channel_map,
)
from models.channel_state import ChannelStatus
from ui.theme import DATA_VIZ, PALETTE, font_family_mono, font_family_ui

logger = logging.getLogger(__name__)

_DISPLAY_MODES = ("attenuation", "coverage", "corrected")


# ---------------------------------------------------------------------------
# 颜色辅助
# ---------------------------------------------------------------------------
def _text_color_for(bg: QColor) -> QColor:
    """根据背景亮度返回保证 ≥ 4.5:1 对比度的文字色（黑/白）。"""
    luminance = (0.2126 * bg.red() + 0.7152 * bg.green() + 0.0722 * bg.blue()) / 255.0
    if luminance > 0.55:
        return QColor(PALETTE.text_primary)
    return QColor("#FFFFFF")


def _lerp_color(t: float) -> QColor:
    """对称发散色阶：t ∈ [-1, 1] → 蓝-白-红。"""
    t = max(-1.0, min(1.0, float(t)))
    abs_t = abs(t)
    neg = QColor(DATA_VIZ.heat_neg)
    zero = QColor(DATA_VIZ.heat_zero)
    pos = QColor(DATA_VIZ.heat_pos)
    if t >= 0:
        r = int(zero.red() * (1 - abs_t) + pos.red() * abs_t)
        g = int(zero.green() * (1 - abs_t) + pos.green() * abs_t)
        b = int(zero.blue() * (1 - abs_t) + pos.blue() * abs_t)
    else:
        r = int(zero.red() * (1 - abs_t) + neg.red() * abs_t)
        g = int(zero.green() * (1 - abs_t) + neg.green() * abs_t)
        b = int(zero.blue() * (1 - abs_t) + neg.blue() * abs_t)
    return QColor(r, g, b)


def _coverage_color(fraction: float) -> QColor:
    """固定的 0–100% 顺序色阶，零覆盖也有明确颜色。"""
    low = QColor("#EFF6FF")
    high = QColor("#174EA6")
    return QColor(
        round(low.red() + (high.red() - low.red()) * fraction),
        round(low.green() + (high.green() - low.green()) * fraction),
        round(low.blue() + (high.blue() - low.blue()) * fraction),
    )


def _coverage_text_color(background: QColor) -> QColor:
    """按 sRGB 相对亮度选择黑/白文字，保持派生量数字清晰。"""
    linear = []
    for component in (background.redF(), background.greenF(), background.blueF()):
        linear.append(
            component / 12.92 if component <= 0.04045
            else ((component + 0.055) / 1.055) ** 2.4
        )
    luminance = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    return QColor("#FFFFFF" if luminance < 0.179 else "#000000")


_STATUS_COLORS: Dict[ChannelStatus, QColor] = {
    ChannelStatus.NO_DATA: QColor(DATA_VIZ.no_data_bg),
    ChannelStatus.UNKNOWN: QColor(DATA_VIZ.no_data_bg),
    ChannelStatus.INVALID: QColor(DATA_VIZ.invalid_bg),
    ChannelStatus.NOT_CALIBRATED: QColor(DATA_VIZ.not_calibrated_bg),
    ChannelStatus.SATURATED: QColor(DATA_VIZ.saturated_bg),
    ChannelStatus.WARMING_UP: QColor(DATA_VIZ.warming_up_bg),
}

_STATUS_TEXT: Dict[ChannelStatus, str] = {
    ChannelStatus.NO_DATA: "无数据",
    ChannelStatus.UNKNOWN: "未知",
    ChannelStatus.INVALID: "本帧无效",
    ChannelStatus.NOT_CALIBRATED: "未标定",
    ChannelStatus.SATURATED: "饱和",
    ChannelStatus.OK: "",
    ChannelStatus.WARMING_UP: "预热中",
}


# ---------------------------------------------------------------------------
# 单元格
# ---------------------------------------------------------------------------
class HeatmapCell(QWidget):
    """单个热力图格子。"""

    clicked = pyqtSignal(int)
    selection_requested = pyqtSignal(int)

    def __init__(self, channel: int, parent=None, title: str = "") -> None:
        super().__init__(parent)
        # channel = 数据通道（固件原始顺序）；title = 格子标签（显示位）
        self.channel = channel
        self.title = title or f"CH{channel + 1:02d}"
        self.position_label = "R?C?"
        self.value_text = "—"
        self.status = ChannelStatus.NO_DATA
        self.enabled = True
        self.selected = False
        self.has_focus = False
        # 主显示量：相对无钢板基线的衰减量 attenuation_mv（mV）
        # 正值=信号下降；负值=信号高于基线（如实显示）
        self.attenuation_value: Optional[float] = None
        self.drop_pct: Optional[float] = None
        self.range_max: float = 250.0
        self.last_update_monotonic: float = 0.0
        # v3：全部数值为 mV
        self.sample_mv: Optional[float] = None
        self.filtered: Optional[float] = None
        self.baseline: Optional[float] = None
        self.vadc_mv: Optional[float] = None
        self.detector_mv: Optional[float] = None
        self.valid_in_frame: bool = False
        # 覆盖率由上层用已应用的标定估计，使用物理格子面积比例。
        # None 为未知；和 ADC 衰减量、主界面的滤波预热状态独立。
        self.coverage_mode = False
        self.coverage_value: Optional[float] = None
        self.corrected_mode = False
        self.corrected_value: Optional[float] = None
        self.display_mode = "attenuation"

        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.setMinimumSize(70, 56)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.setEnabled(True)
        self.setAccessibleName(self.title)
        self.setAccessibleDescription("热力图单元格：未配置")

    @property
    def delta_value(self) -> Optional[float]:
        """兼容旧属性名：语义为 attenuation_value（衰减量）。"""
        return self.attenuation_value

    def set_position_label(self, position: str) -> None:
        self.position_label = position
        self._update_accessible_description()

    def set_data(
        self,
        attenuation: Optional[float],
        status: ChannelStatus,
        enabled: bool,
        extras: Optional[dict] = None,
    ) -> None:
        """设置单元格数据。

        attenuation：相对无钢板基线的衰减量（mV，来自统一测量快照；
        None = 未标定/无效/未知，显示 "—" 而非正常零值）。
        """
        self.attenuation_value = attenuation
        self.status = status
        self.enabled = enabled
        extras = extras or {}
        # mV 键优先；兼容旧 raw/filtered/baseline 键（数值亦为 mV）
        self.sample_mv = extras.get("sample_mv", extras.get("raw"))
        self.filtered = extras.get("filtered_mv", extras.get("filtered"))
        self.baseline = extras.get("baseline_mv", extras.get("baseline"))
        self.drop_pct = extras.get("drop_pct")
        self.vadc_mv = extras.get("vadc_mv")
        self.detector_mv = extras.get("detector_mv")
        self.valid_in_frame = extras.get("valid", False)
        if extras.get("last_update_monotonic"):
            self.last_update_monotonic = extras["last_update_monotonic"]
        self._refresh_presentation()

    def set_selected(self, sel: bool) -> None:
        self.selected = sel
        self.update()

    def set_coverage(self, enabled: bool, value: Optional[float]) -> None:
        """兼容旧单元格接口，覆盖率开关对应覆盖率/衰减量模式。"""
        self.set_display_mode("coverage" if enabled else "attenuation")
        self.set_coverage_value(value)

    def set_display_mode(self, mode: str) -> None:
        if mode not in _DISPLAY_MODES:
            raise ValueError(f"未知热力图显示量: {mode}")
        if mode == self.display_mode:
            return
        self.display_mode = mode
        self.coverage_mode = mode == "coverage"
        self.corrected_mode = mode == "corrected"
        self.coverage_value = None
        self.corrected_value = None
        self._refresh_presentation()

    def set_coverage_value(self, value: Optional[float]) -> None:
        self.coverage_value = value
        self._refresh_presentation()

    def set_corrected_value(self, value: Optional[float]) -> None:
        self.corrected_value = value
        self._refresh_presentation()

    def _refresh_presentation(self) -> None:
        self._update_text()
        self._refresh_tooltip()
        self._update_accessible_description()
        self.update()

    def set_range(self, range_max: float) -> None:
        self.range_max = max(1e-6, float(range_max))
        self.update()

    def focusInEvent(self, event) -> None:
        self.has_focus = True
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self.has_focus = False
        self.update()
        super().focusOutEvent(event)

    def _update_text(self) -> None:
        if self.coverage_mode:
            self.value_text = (
                "未知" if self.coverage_value is None
                else f"{self.coverage_value * 100:.1f}%"
            )
        elif self.corrected_mode:
            self.value_text = (
                "未知" if self.corrected_value is None
                else f"{self.corrected_value:+.1f}"
            )
        elif not self.enabled:
            self.value_text = "未启用"
        elif self.status == ChannelStatus.NO_DATA:
            self.value_text = "—"
        elif self.status == ChannelStatus.NOT_CALIBRATED:
            self.value_text = "未标定"
        elif self.status == ChannelStatus.INVALID:
            self.value_text = "无效"
        elif self.status == ChannelStatus.SATURATED:
            self.value_text = "饱和"
        elif self.status == ChannelStatus.WARMING_UP:
            self.value_text = "预热中"
        elif self.attenuation_value is None:
            self.value_text = "—"
        else:
            sign = "+" if self.attenuation_value >= 0 else ""
            self.value_text = f"{sign}{self.attenuation_value:.1f}"

    def _refresh_tooltip(self) -> None:
        if self.corrected_mode:
            value = (
                "未知" if self.corrected_value is None
                else f"{self.corrected_value:+.3f} mV"
            )
            lines = [
                f"{self.title} ({self.position_label})",
                f"补偿后响应: {value}",
                "负值蓝色；零点中性；正值红色",
            ]
            if self.valid_in_frame and self.sample_mv is not None:
                lines.append(f"ADC 原始: {self.sample_mv:.3f} mV")
            self.setToolTip("\n".join(lines))
            return
        if not self.enabled and not self.coverage_mode:
            tip = f"{self.title} ({self.position_label}) (未启用)"
        else:
            status_text = _STATUS_TEXT.get(self.status, self.status.name)
            status_line = (
                f"ADC 通道状态: {status_text or '正常'}" if self.coverage_mode
                else f"状态: {status_text}"
            )
            lines = [
                self.title,
                f"位置: {self.position_label}",
                # 与串口助手逐项对账：线圈标签 ↔ 第几路电压
                f"串口电压序号: 第 {self.channel + 1} 个"
                f"（idx {self.channel}）",
                status_line,
            ]
            if self.coverage_mode:
                coverage = (
                    "未知" if self.coverage_value is None
                    else f"{self.coverage_value * 100:.1f} %"
                )
                lines.insert(2, f"估计格子覆盖率: {coverage}")
                if not self.enabled:
                    lines.append("该通道未启用主界面的滤波处理")
            if self.attenuation_value is not None:
                lines.append(f"衰减量: {self.attenuation_value:+.3f} mV")
            else:
                lines.append("衰减量: —")
            if self.drop_pct is not None:
                lines.append(f"下降比例: {self.drop_pct:+.2f} %")
            if self.baseline is not None:
                lines.append(f"无钢板基线: {self.baseline:.3f} mV")
            if self.filtered is not None:
                lines.append(f"滤波后: {self.filtered:.3f} mV")
            if self.sample_mv is not None:
                lines.append(f"ADC 原始: {self.sample_mv:.3f} mV")
            if self.detector_mv is not None:
                lines.append(f"VOUT(分压后): {self.detector_mv:.3f} mV")
            lines.append("衰减量=基线−滤波；正值: 信号下降；负值: 信号升高")
            bit = 1 if self.valid_in_frame else 0
            lines.append(
                f"本帧有效: {'是' if bit else '否'}"
                f"（valid_mask bit {self.channel} = {bit}）"
            )
            if self.last_update_monotonic:
                age = time.monotonic() - self.last_update_monotonic
                lines.append(f"更新: {age:.1f}s 前")
            tip = "\n".join(lines)
        self.setToolTip(tip)

    def _update_accessible_description(self) -> None:
        if self.coverage_mode:
            coverage = (
                "未知" if self.coverage_value is None
                else f"{self.coverage_value * 100:.1f}%"
            )
            desc = f"位置 {self.position_label}，估计格子覆盖率 {coverage}"
        elif self.corrected_mode:
            value = (
                "未知" if self.corrected_value is None
                else f"{self.corrected_value:+.1f} mV"
            )
            desc = f"位置 {self.position_label}，补偿后响应 {value}"
        elif not self.enabled:
            desc = "未启用"
        else:
            status_text = _STATUS_TEXT.get(self.status, self.status.name) or "正常"
            if self.attenuation_value is not None:
                desc = (
                    f"位置 {self.position_label}，状态 {status_text}，"
                    f"衰减量 {self.attenuation_value:+.1f} mV"
                )
            else:
                desc = f"位置 {self.position_label}，状态 {status_text}"
        self.setAccessibleDescription(desc)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        rect = self.rect()

        if self.coverage_mode:
            if self.coverage_value is None:
                bg = QColor(DATA_VIZ.no_data_bg)
            else:
                bg = _coverage_color(self.coverage_value)
            text_color = _coverage_text_color(bg)
        elif self.corrected_mode:
            if self.corrected_value is None:
                bg = QColor(DATA_VIZ.no_data_bg)
            else:
                bg = _lerp_color(self.corrected_value / self.range_max)
            text_color = _coverage_text_color(bg)
        elif not self.enabled:
            bg = QColor(DATA_VIZ.disabled_bg)
            text_color = _text_color_for(bg)
        elif self.status == ChannelStatus.OK and self.attenuation_value is not None:
            # 发散色阶：正衰减（信号下降）与负衰减（信号升高）分色，零点中性
            t = self.attenuation_value / self.range_max
            bg = _lerp_color(t)
            text_color = _text_color_for(bg)
        else:
            # 未标定/无效/饱和/无数据：状态底色，绝不显示成正常零值色
            bg = _STATUS_COLORS.get(self.status, QColor(PALETTE.surface))
            text_color = QColor(DATA_VIZ.text_on_status)

        p.fillRect(rect, bg)

        if self.status == ChannelStatus.SATURATED and self.display_mode == "attenuation":
            p.save()
            p.setOpacity(0.5)
            pen = QPen(QColor(PALETTE.danger), 1)
            p.setPen(pen)
            step = 6
            x_end = self.width() + self.height()
            x = -self.height()
            while x < x_end:
                p.drawLine(int(x), 0, int(x + self.height()), self.height())
                x += step
            p.restore()

        if self.selected:
            outer = QPen(QColor(PALETTE.accent), 3)
            p.setPen(outer)
            p.setBrush(Qt.NoBrush)
            p.drawRect(rect.adjusted(1, 1, -2, -2))
            inner = QPen(QColor(255, 255, 255, 200), 1)
            p.setPen(inner)
            p.drawRect(rect.adjusted(4, 4, -5, -5))

        if self.has_focus and not self.selected:
            pen = QPen(QColor(PALETTE.focus), 2, Qt.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRect(rect.adjusted(2, 2, -3, -3))

        border = QPen(QColor(PALETTE.border), 1)
        p.setPen(border)
        p.drawRect(rect.adjusted(0, 0, -1, -1))

        p.setPen(text_color)
        title_font = QFont()
        title_font.setFamily(font_family_ui().split(",")[0].strip())
        title_font.setPointSize(8)
        title_font.setBold(True)
        p.setFont(title_font)
        p.drawText(
            rect.adjusted(6, 4, -6, 0),
            Qt.AlignLeft | Qt.AlignTop,
            self.title,
        )
        p.drawText(
            rect.adjusted(0, 4, -6, 0),
            Qt.AlignRight | Qt.AlignTop,
            self.position_label,
        )

        value_font = QFont()
        value_font.setFamily(font_family_mono().split(",")[0].strip())
        value_font.setPointSize(15)
        value_font.setBold(True)
        p.setFont(value_font)
        # 窗口收窄时完整显示覆盖率和带正负号的补偿后读数。
        if self.coverage_mode or self.corrected_mode:
            text_width = p.fontMetrics().horizontalAdvance(self.value_text)
            available_width = max(1, rect.width() - 12)
            if text_width > available_width:
                value_font.setPointSizeF(max(
                    8.0, value_font.pointSizeF() * available_width / text_width,
                ))
                p.setFont(value_font)
        p.setPen(text_color)
        p.drawText(
            rect.adjusted(6, 24, -6, -6),
            Qt.AlignCenter,
            self.value_text,
        )

        p.end()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and (
            self.enabled or self.coverage_mode or self.corrected_mode
        ):
            self.clicked.emit(self.channel)
            self.setFocus(Qt.OtherFocusReason)
        super().mousePressEvent(event)

    def event(self, event) -> bool:  # noqa: N802
        # 全局 Space 快捷键（暂停曲线）注册为 ApplicationShortcut；
        # 单元格聚焦时 Space/Enter 应用于"选中该通道"（键盘可达性）。
        # 通过 ShortcutOverride 声明占用，避免被全局快捷键抢占。
        if event.type() == QEvent.ShortcutOverride and event.key() in (
            Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter,
        ):
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.selection_requested.emit(self.channel)
            event.accept()
            return
        if event.key() in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
            parent = self.parent()
            if parent is not None and hasattr(parent, "keyPressEvent"):
                parent.keyPressEvent(event)
                return
        super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# 色阶图例
# ---------------------------------------------------------------------------
class HeatmapLegend(QWidget):
    """衰减量、覆盖率和补偿后响应的水平色阶图例。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._range_max = 250.0
        self._coverage_mode = False
        self._display_mode = "attenuation"
        self.setMinimumHeight(28)
        self.setMaximumHeight(36)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_range(self, range_max: float) -> None:
        self._range_max = max(1e-6, float(range_max))
        self.update()

    def set_coverage_mode(self, enabled: bool) -> None:
        """兼容覆盖率/衰减量两种显示的旧开关。"""
        self.set_display_mode("coverage" if enabled else "attenuation")

    def set_display_mode(self, mode: str) -> None:
        if mode not in _DISPLAY_MODES:
            raise ValueError(f"未知热力图显示量: {mode}")
        self._display_mode = mode
        self._coverage_mode = mode == "coverage"
        if mode == "coverage":
            name = "格子覆盖率图例"
            description = "覆盖率 0% 至 100%，浅蓝至深蓝，灰色表示未知"
        elif mode == "corrected":
            name = "补偿后响应图例"
            description = "补偿后响应，单位 mV；负值蓝色，零点中性，正值红色；灰色表示未知"
        else:
            name = "衰减量图例"
            description = "衰减量，单位毫伏，负值蓝色，零点中性，正值红色"
        self.setAccessibleName(name)
        self.setAccessibleDescription(description)
        self.setToolTip(description)
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        rect = self.rect()
        grad_rect = rect.adjusted(0, 4, 0, -16)
        steps = max(40, rect.width())
        for i in range(steps):
            fraction = i / max(1, steps - 1)
            color = (
                _coverage_color(fraction) if self._coverage_mode
                else _lerp_color(-1.0 + 2.0 * fraction)
            )
            p.fillRect(
                grad_rect.left() + i * grad_rect.width() // steps,
                grad_rect.top(),
                max(1, grad_rect.width() // steps) + 1,
                grad_rect.height(),
                color,
            )
        p.setPen(QColor(PALETTE.border))
        p.setBrush(Qt.NoBrush)
        p.drawRect(grad_rect)

        font = QFont()
        font.setFamily(font_family_mono().split(",")[0].strip())
        font.setPointSize(8)
        p.setFont(font)
        p.setPen(QColor(PALETTE.text_secondary))
        label_y = grad_rect.bottom() + 14
        left_text = "0%" if self._coverage_mode else f"−{self._range_max:.0f}"
        middle_text = "50% 覆盖率" if self._coverage_mode else "0 mV"
        if self._display_mode == "corrected":
            middle_text = "0 mV · 补偿后"
        right_text = "100%" if self._coverage_mode else f"+{self._range_max:.0f}"
        fm = p.fontMetrics()
        p.drawText(rect.left(), label_y, left_text)
        p.drawText(
            rect.center().x() - fm.horizontalAdvance(middle_text) // 2,
            label_y, middle_text,
        )
        text_w = fm.horizontalAdvance(right_text)
        p.drawText(rect.right() - text_w, label_y, right_text)
        p.end()


# ---------------------------------------------------------------------------
# 主控件
# ---------------------------------------------------------------------------
class HeatmapWidget(QWidget):
    """4×4 热力图容器。"""

    channel_clicked = pyqtSignal(int)
    channel_selected = pyqtSignal(int)
    navigation_requested = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows = 4
        self._cols = 4
        self._cells: Dict[Tuple[int, int], HeatmapCell] = {}
        self._cell_to_channel: Dict[int, Tuple[int, int]] = {}
        self._selected: Optional[int] = None
        self._range_max = 250.0
        self._coverage_mode = False
        self._display_mode = "attenuation"
        self._channel_map: Dict[int, Tuple[int, int]] = {}
        #: {数据通道: 格子标签}；空 = 按 CH(通道+1)
        self._labels: Dict[int, str] = {}
        self._build_grid()
        # 默认布局统一引用 core.channel_layout 的硬件实际接线映射，
        # 不再使用独立的行优先默认。
        self.set_channel_map(dict(HARDWARE_CHANNEL_MAP))

    def _build_grid(self) -> None:
        layout = QGridLayout()
        layout.setSpacing(4)
        layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(layout)
        self._layout = layout

    def _clear_cells(self) -> None:
        if hasattr(self, "_layout") and self._layout is not None:
            for i in reversed(range(self._layout.count())):
                item = self._layout.takeAt(i)
                if item is None:
                    continue
                w = item.widget()
                if w is not None:
                    w.setParent(None)
                    w.deleteLater()
        self._cells.clear()
        self._cell_to_channel.clear()

    def set_channel_map(
        self,
        channel_map: Dict[int, Tuple[int, int]],
        labels: Optional[Dict[int, str]] = None,
    ) -> None:
        """按给定映射重新放置通道单元格。

        channel_map：{数据通道: (row, col)}——键始终是固件数据通道。
        labels：{数据通道: "CHxx"}——格子标签；缺省按 CH(通道+1)。
        位置与标签可以和数据通道号不同（换板绑定），但数据仍按通道号
        索引：数值/颜色/有效状态/提示框都走同一个 channel。

        只接受完整合法映射（16 通道、16 个唯一在界坐标，见
        core.channel_layout.validate_channel_map）；非法输入显式拒绝
        并回退硬件默认映射——禁止静默 clamp、静默丢弃重复项或
        静默补全缺失项（那会产生重叠/错位的单元格）。
        """
        normalized = normalize_channel_map(channel_map)
        ok, reason = validate_channel_map(normalized)
        if not ok:
            logger.warning(
                "HeatmapWidget.set_channel_map: 非法映射（%s），"
                "回退硬件默认布局", reason,
            )
            normalized = dict(HARDWARE_CHANNEL_MAP)
            labels = None
        self._channel_map = normalized
        self._labels = dict(labels or {})
        self._rebuild_layout()

    def _rebuild_layout(self) -> None:
        self._clear_cells()
        prev_selected = self._selected
        self._selected = None
        layout = self._layout
        for ch, (r, c) in self._channel_map.items():
            cell = HeatmapCell(ch, title=self._labels.get(ch, ""))
            cell.set_position_label(f"R{r + 1}C{c + 1}")
            cell.set_range(self._range_max)
            cell.set_display_mode(self._display_mode)
            cell.clicked.connect(self._on_cell_clicked)
            cell.selection_requested.connect(self._on_cell_clicked)
            self._cells[(r, c)] = cell
            self._cell_to_channel[ch] = (r, c)
            layout.addWidget(cell, r, c)
        if prev_selected is not None and prev_selected in self._cell_to_channel:
            self.select_channel(prev_selected)

    def channel_position(self, channel: int) -> Optional[Tuple[int, int]]:
        return self._cell_to_channel.get(channel)

    def channel_map(self) -> Dict[int, Tuple[int, int]]:
        return dict(self._channel_map)

    def set_layout(self, rows: int, cols: int) -> None:
        if rows * cols != self._rows * self._cols:
            return
        self._rows = rows
        self._cols = cols

    def set_range(self, range_max: float) -> None:
        self._range_max = max(1e-6, float(range_max))
        for cell in self._cells.values():
            cell.set_range(self._range_max)

    def range_max(self) -> float:
        return self._range_max

    def set_coverage_mode(self, enabled: bool) -> None:
        """兼容覆盖率/衰减量两种显示的旧开关。"""
        self.set_display_mode("coverage" if enabled else "attenuation")

    def set_display_mode(self, mode: str) -> None:
        """选择主显示量；切换时清派生结果，等待当前兼容帧。"""
        if mode not in _DISPLAY_MODES:
            raise ValueError(f"未知热力图显示量: {mode}")
        if mode == self._display_mode:
            return
        self._display_mode = mode
        self._coverage_mode = mode == "coverage"
        for cell in self._cells.values():
            cell.set_display_mode(mode)

    def set_coverage_values(
        self, grid: Optional[Sequence[Sequence[Optional[float]]]],
    ) -> None:
        """设置物理 4×4 行优先覆盖率，和原始数据通道顺序无关。

        每格为 [0, 1] 面积比例；None、非有限值或越界值显示未知。
        整个 grid 为 None 或尺寸错误时清除全部覆盖率，避免残留旧结果。
        数据来源、新鲜度和标定适用性由上层判断后传入。
        """
        self._set_grid_values(grid, "coverage")

    def set_corrected_values(
        self, grid: Optional[Sequence[Sequence[Optional[float]]]],
    ) -> None:
        """设置物理 4×4 行优先的有符号补偿后响应（ADC mV）。

        None/非有限值显示灰色未知；切换数据源、失效帧及补偿不可用时
        上层传入 None。色阶使用 set_range() 的对称范围，保留数值符号。
        """
        self._set_grid_values(grid, "corrected")

    def _set_grid_values(
        self, grid: Optional[Sequence[Sequence[Optional[float]]]], mode: str,
    ) -> None:
        if grid is not None:
            try:
                valid_shape = len(grid) == 4 and all(len(row) == 4 for row in grid)
            except TypeError:
                valid_shape = False
            if not valid_shape:
                logger.warning("%s 尺寸错误，需要物理位置 4×4 格子", mode)
                grid = None
        for (r, c), cell in self._cells.items():
            value = None
            if grid is not None and grid[r][c] is not None:
                try:
                    candidate = float(grid[r][c])
                except (TypeError, ValueError, OverflowError):
                    candidate = float("nan")
                if math.isfinite(candidate) and (
                    mode == "corrected" or 0.0 <= candidate <= 1.0
                ):
                    value = candidate
            if mode == "coverage":
                cell.set_coverage_value(value)
            else:
                cell.set_corrected_value(value)

    def update_cell(
        self,
        channel: int,
        attenuation: Optional[float],
        status: ChannelStatus,
        enabled: bool,
        extras: Optional[dict] = None,
    ) -> None:
        """更新单元格：attenuation 为相对无钢板基线的衰减量（mV）。"""
        if channel not in self._cell_to_channel:
            return
        r, c = self._cell_to_channel[channel]
        cell = self._cells.get((r, c))
        if cell is None:
            return
        cell.set_data(attenuation, status, enabled, extras)
        cell.set_range(self._range_max)

    def select_channel(self, channel: int) -> None:
        if self._selected is not None and self._selected in self._cell_to_channel:
            r, c = self._cell_to_channel[self._selected]
            prev = self._cells.get((r, c))
            if prev is not None:
                prev.set_selected(False)
        self._selected = channel
        if channel in self._cell_to_channel:
            r, c = self._cell_to_channel[channel]
            cell = self._cells.get((r, c))
            if cell is not None:
                cell.set_selected(True)
                cell.setFocus(Qt.OtherFocusReason)
                self.channel_selected.emit(channel)

    def selected(self) -> Optional[int]:
        return self._selected

    def _on_cell_clicked(self, channel: int) -> None:
        self.select_channel(channel)
        self.channel_clicked.emit(channel)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if self._selected is None or self._selected not in self._cell_to_channel:
            return super().keyPressEvent(event)
        r, c = self._cell_to_channel[self._selected]
        key = event.key()
        if key == Qt.Key_Left:
            self._move_selection(r, c - 1)
            event.accept()
        elif key == Qt.Key_Right:
            self._move_selection(r, c + 1)
            event.accept()
        elif key == Qt.Key_Up:
            self._move_selection(r - 1, c)
            event.accept()
        elif key == Qt.Key_Down:
            self._move_selection(r + 1, c)
            event.accept()
        elif key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.channel_selected.emit(self._selected)
            event.accept()
        else:
            super().keyPressEvent(event)

    def _move_selection(self, r: int, c: int) -> None:
        if r < 0 or c < 0 or r >= self._rows or c >= self._cols:
            return
        cell = self._cells.get((r, c))
        if cell is None:
            return
        self.select_channel(cell.channel)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._reflow_cells()

    def _reflow_cells(self) -> None:
        """根据可用空间重新计算每个 cell 的尺寸，保持宽高比 ≤ 1.3。"""
        if not self._cells:
            return
        layout = self._layout
        if layout is None:
            return
        m = layout.contentsMargins()
        avail_w = max(0, self.width() - m.left() - m.right()
                      - layout.horizontalSpacing() * (self._cols - 1))
        avail_h = max(0, self.height() - m.top() - m.bottom()
                      - layout.verticalSpacing() * (self._rows - 1))
        if avail_w <= 0 or avail_h <= 0:
            return
        cell_w = avail_w // self._cols
        cell_h = avail_h // self._rows
        side = min(cell_w, cell_h)
        if side < 56:
            side = 56
        for cell in self._cells.values():
            cell.setFixedSize(side, side)
