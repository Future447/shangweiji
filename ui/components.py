"""
ui/components.py — 可复用小组件。

只放实际被多处引用的 UI 片段：
- StatusBadge: 圆角小徽标（用于状态指示）
- ElidingLabel: 中间省略号 Label
- SectionHeader: 紧凑分区标题
- KpiMetric: 等宽数字指标
"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QFontMetrics
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from .theme import font_family_mono, set_status_role


class StatusBadge(QLabel):
    """圆角小徽标：用 statusRole 切换语义色。"""

    def __init__(self, text: str = "", role: str = "neutral", parent=None) -> None:
        super().__init__(text, parent)
        self.setObjectName("StatusBadge")
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumHeight(20)
        self.setMaximumHeight(24)
        self.set_size_compact()
        set_status_role(self, role)

    def set_status(self, text: str, role: str) -> None:
        self.setText(text)
        set_status_role(self, role)

    def set_size_compact(self) -> None:
        # 仅由调用方主动触发，紧凑模式仍由 QSS 控制
        self.setProperty("size", "compact")
        self.style().unpolish(self)
        self.style().polish(self)


class ElidingLabel(QLabel):
    """路径等长字符串中间省略号。"""

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self._full_text = text
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setMinimumWidth(0)

    def set_full_text(self, text: str) -> None:
        self._full_text = text
        self.setText(text)
        self.setToolTip(text)

    def full_text(self) -> str:
        return self._full_text

    def elide_to_width(self, max_width: int) -> str:
        fm = QFontMetrics(self.font())
        return fm.elidedText(self._full_text, Qt.ElideMiddle, max_width)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._full_text:
            elided = self.elide_to_width(self.width())
            super().setText(elided)


class SectionHeader(QFrame):
    """紧凑分区标题：左侧标题，右侧可选 caption。"""

    def __init__(
        self,
        title: str,
        caption: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("SectionHeader")
        self._title = QLabel(title)
        self._title.setObjectName("SectionTitle")
        self._caption = QLabel(caption) if caption else None
        if self._caption is not None:
            self._caption.setObjectName("Caption")
            self._caption.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        h = QHBoxLayout()
        h.setContentsMargins(0, 6, 0, 6)
        h.setSpacing(8)
        h.addWidget(self._title)
        if self._caption is not None:
            h.addWidget(self._caption, 1)
        self.setLayout(h)

    def set_caption(self, text: str) -> None:
        if self._caption is None:
            self._caption = QLabel(text)
            self._caption.setObjectName("Caption")
            self._caption.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.layout().addWidget(self._caption, 1)
        else:
            self._caption.setText(text)

    def title(self) -> str:
        return self._title.text()

    def set_title(self, text: str) -> None:
        self._title.setText(text)


class KpiMetric(QWidget):
    """紧凑两列指标：标题 + 等宽数字。"""

    def __init__(
        self,
        label: str,
        value: str = "—",
        role: str = "numeric",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._label = QLabel(label)
        self._label.setObjectName("Caption")
        self._value = QLabel(value)
        self._value.setProperty("role", role)
        self._value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        h.addWidget(self._label, 1)
        h.addWidget(self._value, 0)
        self.setLayout(h)

    def set_value(self, text: str) -> None:
        self._value.setText(text)

    def value(self) -> str:
        return self._value.text()


class SurfaceCard(QFrame):
    """统一表面卡片：圆角 + 浅边框。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("SurfaceCard")
        v = QVBoxLayout()
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(8)
        self.setLayout(v)

    def add_widget(self, w: QWidget, stretch: int = 0) -> None:
        self.layout().addWidget(w, stretch)

    def add_layout(self, layout) -> None:
        self.layout().addLayout(layout)
