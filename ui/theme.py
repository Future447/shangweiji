"""
ui/theme.py — 集中式视觉令牌与 QSS 生成器。

设计参数：
- DESIGN_VARIANCE = 3（参考 Fluent 2 层级与状态规范）
- MOTION_INTENSITY = 2（仅状态切换、悬停、按下）
- VISUAL_DENSITY = 8（高信息密度、单层浅色）
- 单一冷蓝强调色 #2F6FCE
- 数字与日志使用 Cascadia Mono / Consolas
- 面板圆角 6 px，按钮/输入框 4 px
- 控件高度 32-34 px，主要动作 ≥ 36 px
- 不使用玻璃拟态、外发光、装饰纹理、网页式动画
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple


# ---------------------------------------------------------------------------
# 颜色令牌
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Palette:
    """语义化颜色令牌。所有 UI 模块应只引用此处定义的名称。"""

    # 表面
    canvas: str = "#F2F5F8"
    surface: str = "#F8FAFC"
    surface_raised: str = "#EEF2F6"

    # 文本
    text_primary: str = "#17212B"
    text_secondary: str = "#52606D"
    text_muted: str = "#7B8794"
    text_inverse: str = "#FFFFFF"

    # 边界
    border: str = "#D3DBE3"
    border_strong: str = "#AEB9C4"

    # 语义状态
    accent: str = "#2F6FCE"
    accent_pressed: str = "#245AAD"
    accent_hover: str = "#3D7DDB"
    accent_subtle: str = "#DDE7F5"

    success: str = "#247A52"
    success_subtle: str = "#DDEEE6"
    warning: str = "#9A5B00"
    warning_subtle: str = "#F5E7D1"
    danger: str = "#B43A32"
    danger_subtle: str = "#F4DCD9"
    demo: str = "#5967A8"
    demo_subtle: str = "#E0E3F0"

    # 焦点
    focus: str = "#2F6FCE"


# ---------------------------------------------------------------------------
# 数据可视化令牌（独立于界面强调色）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DataViz:
    """数据可视化色阶。热力图与曲线都应使用这里的色，避免与界面强调色混淆。

    对比度经过手工校验：白色文本可在最深两端背景上保持 ≥ 4.5:1。
    """

    # 对称发散色阶：负端蓝 → 中心白 → 正端红
    heat_neg: str = "#0F2862"   # 深蓝（最大负 delta，对比白文字 ≥ 4.5:1）
    heat_zero: str = "#F4F6FA"  # 中心
    heat_pos: str = "#7A1212"   # 深红（最大正 delta，对比白文字 ≥ 4.5:1）

    # 状态色（用于未标定/无效/禁用等）
    no_data_bg: str = "#EEF0F4"      # 无数据：很浅灰
    invalid_bg: str = "#D8DEE6"      # 本帧无效：中灰
    not_calibrated_bg: str = "#E4E6EA"  # 未标定：浅灰
    disabled_bg: str = "#E0E4EA"     # 禁用
    saturated_bg: str = "#FCE0DC"    # 饱和：浅红
    warming_up_bg: str = "#FDF3D7"   # 预热中：浅琥珀
    text_on_status: str = "#52606D"  # 状态文字色

    # 曲线：raw 与 filtered
    curve_raw: str = "#7B8794"        # 低对比细线
    curve_filtered: str = "#2F6FCE"   # 冷蓝粗线
    curve_pinned: Tuple[str, ...] = (
        "#2F6FCE",  # accent
        "#9A5B00",  # warning
        "#247A52",  # success
        "#B43A32",  # danger
    )
    cursor: str = "#2F6FCE"


# ---------------------------------------------------------------------------
# 字体令牌
# ---------------------------------------------------------------------------
FONT_FAMILY = "Segoe UI"
FONT_FAMILY_FALLBACK = "Microsoft YaHei UI"
FONT_FAMILY_MONO = "Cascadia Mono"


def font_family_ui() -> str:
    return f"{FONT_FAMILY}, {FONT_FAMILY_FALLBACK}"


def font_family_mono() -> str:
    return f"{FONT_FAMILY_MONO}, Consolas"


# ---------------------------------------------------------------------------
# 尺寸令牌
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Metrics:
    radius_panel: int = 6
    radius_control: int = 4
    control_height: int = 32
    control_height_main: int = 36
    gutter: int = 8
    gutter_lg: int = 12
    split_handle: int = 4

    # 主窗口几何
    window_default_w: int = 1360
    window_default_h: int = 840
    window_min_w: int = 1000
    window_min_h: int = 720
    left_ratio: float = 0.65
    right_min_w: int = 360
    right_min_h: int = 220


# ---------------------------------------------------------------------------
# 角色
# ---------------------------------------------------------------------------
# 状态角色（通过 objectName 切换）：
#   "primary"   - 主操作（连接、开始标定、开始记录）
#   "secondary" - 次级操作（演示、刷新端口、选择目录）
#   "danger"    - 危险操作（断开、停止记录、取消标定）
#   "ghost"     - 透明背景按钮（次要工具栏）
#
# 状态语义（通过 dynamic property "statusRole" 切换）：
#   "ok" / "warning" / "danger" / "neutral" / "demo" / "accent"

# ---------------------------------------------------------------------------
# 调色板与指标单例
# ---------------------------------------------------------------------------
PALETTE = Palette()
DATA_VIZ = DataViz()
METRICS = Metrics()


# ---------------------------------------------------------------------------
# QSS 生成
# ---------------------------------------------------------------------------
def build_stylesheet(extra: str = "") -> str:
    """返回完整的应用 QSS。"""

    p = PALETTE
    m = METRICS
    ui_font = font_family_ui()
    mono_font = font_family_mono()

    qss = f"""
    /* ----- 基础窗口 ----- */
    QMainWindow, QDialog {{
        background-color: {p.canvas};
        color: {p.text_primary};
        font-family: "{ui_font}";
        font-size: 10pt;
    }}

    QWidget {{
        font-family: "{ui_font}";
        color: {p.text_primary};
    }}

    /* ----- 表面 ----- */
    QFrame#SurfaceCard {{
        background-color: {p.surface};
        border: 1px solid {p.border};
        border-radius: {m.radius_panel}px;
    }}

    QFrame#SectionHeader {{
        background: transparent;
        border: none;
        border-bottom: 1px solid {p.border};
    }}

    QLabel#SectionTitle {{
        color: {p.text_primary};
        font-size: 11pt;
        font-weight: 600;
        padding: 2px 0;
    }}

    QLabel#SectionSubtitle {{
        color: {p.text_secondary};
        font-size: 9pt;
        padding: 0;
    }}

    QLabel#Muted {{
        color: {p.text_muted};
    }}

    QLabel#Caption {{
        color: {p.text_secondary};
        font-size: 9pt;
    }}

    /* 数字使用等宽字体 */
    QLabel[role="numeric"], QLabel[role="mono"] {{
        font-family: "{mono_font}";
    }}

    /* ----- 状态徽标 ----- */
    QLabel#StatusBadge {{
        border-radius: 10px;
        padding: 2px 10px;
        font-size: 9pt;
        font-weight: 600;
    }}
    QLabel#StatusBadge[statusRole="ok"] {{
        background-color: {p.success_subtle};
        color: {p.success};
    }}
    QLabel#StatusBadge[statusRole="warning"] {{
        background-color: {p.warning_subtle};
        color: {p.warning};
    }}
    QLabel#StatusBadge[statusRole="danger"] {{
        background-color: {p.danger_subtle};
        color: {p.danger};
    }}
    QLabel#StatusBadge[statusRole="neutral"] {{
        background-color: {p.surface_raised};
        color: {p.text_secondary};
    }}
    QLabel#StatusBadge[statusRole="demo"] {{
        background-color: {p.demo_subtle};
        color: {p.demo};
    }}
    QLabel#StatusBadge[statusRole="accent"] {{
        background-color: {p.accent_subtle};
        color: {p.accent};
    }}

    /* ----- 按钮 ----- */
    QPushButton {{
        min-height: {m.control_height}px;
        max-height: {m.control_height}px;
        padding: 4px 14px;
        border: 1px solid {p.border};
        border-radius: {m.radius_control}px;
        background-color: {p.surface};
        color: {p.text_primary};
        text-align: center;
    }}
    QPushButton:hover {{
        background-color: {p.surface_raised};
        border-color: {p.border_strong};
    }}
    QPushButton:pressed {{
        background-color: {p.border};
    }}
    QPushButton:focus {{
        border: 2px solid {p.focus};
        padding: 3px 13px;
    }}
    QPushButton:disabled {{
        color: {p.text_muted};
        background-color: {p.canvas};
        border-color: {p.border};
    }}
    QPushButton[role="primary"] {{
        background-color: {p.accent};
        color: {p.text_inverse};
        border: 1px solid {p.accent};
        min-height: {m.control_height_main}px;
        max-height: {m.control_height_main}px;
        font-weight: 600;
    }}
    QPushButton[role="primary"]:hover {{
        background-color: {p.accent_hover};
        border-color: {p.accent_hover};
    }}
    QPushButton[role="primary"]:pressed {{
        background-color: {p.accent_pressed};
    }}
    QPushButton[role="primary"]:disabled {{
        background-color: {p.border};
        color: {p.text_muted};
        border-color: {p.border};
    }}
    QPushButton[role="danger"] {{
        background-color: {p.surface};
        color: {p.danger};
        border: 1px solid {p.danger};
    }}
    QPushButton[role="danger"]:hover {{
        background-color: {p.danger_subtle};
    }}
    QPushButton[role="danger"]:pressed {{
        background-color: {p.danger};
        color: {p.text_inverse};
    }}
    QPushButton[role="ghost"] {{
        background: transparent;
        border: 1px solid transparent;
    }}
    QPushButton[role="ghost"]:hover {{
        background-color: {p.surface_raised};
    }}
    QPushButton:checked, QPushButton[role="primary"]:checked {{
        background-color: {p.accent_pressed};
        color: {p.text_inverse};
    }}

    /* ----- 紧凑工具按钮（仅 PlotWidget 工具栏使用） ----- */
    QToolButton[role="plotTool"] {{
        background-color: {p.surface_raised};
        color: {p.text_primary};
        border: 1px solid {p.border};
        border-radius: 4px;
        padding: 2px 8px;
        font-size: 9pt;
    }}
    QToolButton[role="plotTool"]:hover {{
        background-color: {p.surface};
        border-color: {p.accent};
    }}
    QToolButton[role="plotTool"]:pressed,
    QToolButton[role="plotTool"]:checked {{
        background-color: {p.accent};
        color: {p.text_inverse};
        border-color: {p.accent_pressed};
    }}

    /* ----- 输入控件 ----- */
    QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {{
        min-height: {m.control_height}px;
        max-height: {m.control_height}px;
        padding: 2px 8px;
        border: 1px solid {p.border};
        border-radius: {m.radius_control}px;
        background-color: {p.surface};
        color: {p.text_primary};
        selection-background-color: {p.accent_subtle};
    }}
    QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus {{
        border: 2px solid {p.focus};
    }}
    QComboBox::drop-down {{
        border: none;
        width: 18px;
    }}

    /* ----- 复选框 ----- */
    QCheckBox {{
        spacing: 6px;
    }}
    QCheckBox::indicator {{
        width: 16px;
        height: 16px;
        border: 1px solid {p.border_strong};
        border-radius: 3px;
        background: {p.surface};
    }}
    QCheckBox::indicator:checked {{
        background: {p.accent};
        border-color: {p.accent};
    }}

    /* ----- 分组 ----- */
    QGroupBox {{
        font-weight: 600;
        border: 1px solid {p.border};
        border-radius: {m.radius_panel}px;
        margin-top: 14px;
        padding: 8px 8px 8px 8px;
        background-color: {p.surface};
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        left: 10px;
        padding: 0 6px;
        color: {p.text_secondary};
    }}

    /* ----- 状态栏 ----- */
    QStatusBar {{
        background: {p.surface_raised};
        color: {p.text_secondary};
        border-top: 1px solid {p.border};
    }}

    /* ----- 标签页 ----- */
    QTabWidget::pane {{
        border: 1px solid {p.border};
        border-radius: {m.radius_panel}px;
        background: {p.surface};
        top: -1px;
    }}
    QTabBar::tab {{
        min-height: 28px;
        padding: 4px 14px;
        border: 1px solid transparent;
        border-bottom: none;
        background: transparent;
        color: {p.text_secondary};
    }}
    QTabBar::tab:selected {{
        background: {p.surface};
        color: {p.text_primary};
        border: 1px solid {p.border};
        border-bottom: 1px solid {p.surface};
        border-radius: {m.radius_control}px {m.radius_control}px 0 0;
    }}
    QTabBar::tab:!selected:hover {{
        color: {p.text_primary};
        background: {p.surface_raised};
    }}

    /* ----- 工具栏 ----- */
    QToolBar {{
        background: {p.canvas};
        border: none;
        padding: 4px;
        spacing: 4px;
    }}
    QToolBar::separator {{
        background: {p.border};
        width: 1px;
        margin: 4px;
    }}

    /* ----- 分割器 ----- */
    QSplitter::handle {{
        background: {p.border};
    }}
    QSplitter::handle:horizontal {{
        width: {m.split_handle}px;
    }}
    QSplitter::handle:vertical {{
        height: {m.split_handle}px;
    }}

    /* ----- 滚动条 ----- */
    QScrollBar:vertical {{
        background: {p.canvas};
        width: 10px;
        margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background: {p.border_strong};
        border-radius: 4px;
        min-height: 24px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {p.text_muted};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0;
    }}
    QScrollBar:horizontal {{
        background: {p.canvas};
        height: 10px;
        margin: 0;
    }}
    QScrollBar::handle:horizontal {{
        background: {p.border_strong};
        border-radius: 4px;
        min-width: 24px;
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0;
    }}

    /* ----- Dock ----- */
    QDockWidget {{
        color: {p.text_primary};
        titlebar-close-icon: none;
    }}
    QDockWidget::title {{
        background: {p.surface_raised};
        padding: 4px 8px;
        text-align: left;
        border-bottom: 1px solid {p.border};
    }}

    /* ----- 列表 / 树 ----- */
    QListWidget, QTreeView, QPlainTextEdit, QTextEdit {{
        background-color: {p.surface};
        border: 1px solid {p.border};
        border-radius: {m.radius_control}px;
        selection-background-color: {p.accent_subtle};
        selection-color: {p.text_primary};
    }}
    QPlainTextEdit, QTextEdit {{
        font-family: "{mono_font}";
    }}

    /* ----- 工具提示 ----- */
    QToolTip {{
        background-color: {p.text_primary};
        color: {p.text_inverse};
        border: 1px solid {p.text_primary};
        border-radius: 3px;
        padding: 4px 6px;
        font-size: 9pt;
    }}

    /* ----- 消息框 ----- */
    QMessageBox {{
        background: {p.surface};
    }}

    /* ----- 覆盖层 ----- */
    QLabel#PlotPausedOverlay {{
        background-color: rgba(255, 255, 255, 220);
        color: {p.text_secondary};
        font-weight: 600;
        padding: 6px 14px;
        border: 1px solid {p.border};
        border-radius: {m.radius_control}px;
    }}

    QLabel#PlotCursorReadout {{
        background-color: rgba(255, 255, 255, 220);
        color: {p.text_secondary};
        padding: 4px 8px;
        border: 1px solid {p.border};
        border-radius: {m.radius_control}px;
        font-family: "{mono_font}";
        font-size: 9pt;
    }}

    /* 主 delta 数值 */
    QLabel#ChannelDelta {{
        font-family: "{mono_font}";
        font-size: 26pt;
        font-weight: 700;
        color: {p.text_primary};
    }}

    /* ChannelChip（设置对话框的通道复选） */
    QPushButton[role="chip"] {{
        min-height: 32px;
        padding: 4px 10px;
        background-color: {p.surface};
        color: {p.text_primary};
        border: 1px solid {p.border};
    }}
    QPushButton[role="chip"]:checked {{
        background-color: {p.accent};
        color: {p.text_inverse};
        border: 1px solid {p.accent};
        font-weight: 600;
    }}

    /* 纵向指标卡（channel_panel.py） */
    QFrame#VerticalMetric {{
        background-color: {p.surface_raised};
        border: 1px solid {p.border};
        border-radius: {m.radius_control}px;
    }}
    """

    return qss + extra


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def set_status_role(widget, role: str) -> None:
    """为 QLabel 设置 statusRole 动态属性并刷新样式。"""
    widget.setProperty("statusRole", role)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def set_button_role(button, role: str) -> None:
    """为 QPushButton 设置 role 动态属性。"""
    button.setProperty("role", role)
    button.style().unpolish(button)
    button.style().polish(button)


# ---------------------------------------------------------------------------
# 状态语义辅助
# ---------------------------------------------------------------------------
SOURCE_STATUS_MAP: Dict[str, tuple] = {
    # source_state -> (badge_text, role, status_bar_text)
    "DISCONNECTED": ("未连接", "neutral", "请连接串口或开启演示模式"),
    "CONNECTING": ("连接中", "accent", "正在打开串口…"),
    "SERIAL_CONNECTED": ("已连接", "ok", ""),
    "DEMO": ("演示模式", "demo", ""),
}

FRESHNESS_STATUS_MAP: Dict[str, tuple] = {
    # (timeout_reason or "ok") -> (badge_text, role)
    "ok": ("接收中", "ok"),
    "no_bytes": ("无数据", "danger"),
    "parse": ("协议不匹配", "warning"),
    "stale": ("数据陈旧", "warning"),
    "device_fault": ("设备故障", "danger"),
}
