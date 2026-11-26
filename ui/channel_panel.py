"""
ui/channel_panel.py — 选中通道详情面板（v3 毫伏协议，衰减实验语义）。

UI 要点：
- 数据一律来自 core.measurement 的统一测量快照（MeasurementSnapshot），
  与热力图/tooltip/曲线/CSV 同源同公式；面板不再自行计算 delta/百分比
- 主显示（实验核心量）：
    相对基线衰减量 attenuation_mv = 无钢板基线 − 当前检波电压（滤波）
    检波电压下降比例 detector_drop_pct（基线为正且达有效下限才给出）
  衰减量 >0 信号下降、≈0 接近基线、<0 信号高于基线（负值如实显示，
  不取绝对值、不截零、不翻转）
- 次级详情：无钢板基线（历史标定参数，当前帧无效时仍显示）、
  当前检波电压（滤波）、串口采样（ADS1115 输入端）、
  VOUT 估算（AD8362 输出端 = 采样 × 分压比）、旧 Δ（诊断）、更新时间
- 当前帧无效/饱和/断流时，当前测量与派生值显示"—"，不留残留值
- 没有真实数据时显示"—"，不显示看似真实的 0
"""

from __future__ import annotations

import time
from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.measurement import MeasurementSnapshot, build_snapshot
from models.adc_profile import AdcProfile
from models.channel_state import ChannelState, ChannelStatus
from ui.components import StatusBadge
from ui.theme import font_family_mono


_STATUS_TEXT = {
    ChannelStatus.NO_DATA: "无数据",
    ChannelStatus.UNKNOWN: "未知",
    ChannelStatus.INVALID: "本帧无效",
    ChannelStatus.NOT_CALIBRATED: "未标定",
    ChannelStatus.SATURATED: "饱和",
    ChannelStatus.OK: "正常",
    ChannelStatus.WARMING_UP: "预热中",
}

_STATUS_ROLE = {
    ChannelStatus.NO_DATA: "neutral",
    ChannelStatus.UNKNOWN: "neutral",
    ChannelStatus.INVALID: "warning",
    ChannelStatus.NOT_CALIBRATED: "neutral",
    ChannelStatus.SATURATED: "danger",
    ChannelStatus.OK: "ok",
    ChannelStatus.WARMING_UP: "warning",
}


def _signed(v: float, digits: int = 3) -> str:
    return f"{'+' if v >= 0 else ''}{v:.{digits}f}"


class _VerticalMetric(QFrame):
    """单个纵向指标：标签在上，值在下。"""

    def __init__(
        self, label: str, parent=None, tooltip: str = ""
    ) -> None:
        super().__init__(parent)
        self.setObjectName("VerticalMetric")
        v = QVBoxLayout()
        v.setContentsMargins(6, 4, 6, 4)
        v.setSpacing(2)
        self._lbl = QLabel(label)
        self._lbl.setObjectName("Caption")
        if tooltip:
            self._lbl.setToolTip(tooltip)
            self.setToolTip(tooltip)
        self._val = QLabel("—")
        self._val.setProperty("role", "numeric")
        f = self._val.font()
        f.setFamily(font_family_mono().split(",")[0].strip())
        f.setPointSize(11)
        f.setBold(False)
        self._val.setFont(f)
        self._val.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        v.addWidget(self._lbl)
        v.addWidget(self._val)
        self.setLayout(v)

    def set_value(self, text: str) -> None:
        self._val.setText(text)


class ChannelPanel(QWidget):
    """选中通道的详细信息（统一测量快照驱动）。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._profile: Optional[AdcProfile] = None
        self._channel: Optional[int] = None
        self._last_snapshot: Optional[MeasurementSnapshot] = None
        self._last_update_monotonic: Optional[float] = None
        self._position: str = "—"
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        # 头部：通道号 + 物理位置 + 状态徽标
        header = QHBoxLayout()
        header.setSpacing(8)
        self._lbl_title = QLabel("—")
        f_title = self._lbl_title.font()
        f_title.setPointSize(13)
        f_title.setBold(True)
        self._lbl_title.setFont(f_title)
        self._lbl_position = QLabel("—")
        self._lbl_position.setObjectName("Caption")
        self._badge = StatusBadge("—", "neutral")
        header.addWidget(self._lbl_title)
        header.addWidget(self._lbl_position, 1)
        header.addWidget(self._badge)
        layout.addLayout(header)

        # 来源标注：当前生效的"线圈标签 ↔ 串口电压序号"绑定
        self._lbl_binding = QLabel("")
        self._lbl_binding.setObjectName("Caption")
        self._lbl_binding.setWordWrap(True)
        layout.addWidget(self._lbl_binding)

        # 主数值（横向）：相对基线衰减量 + 检波电压下降比例
        primary = QHBoxLayout()
        primary.setSpacing(8)
        att_box = QFrame()
        att_box.setObjectName("VerticalMetric")
        att_v = QVBoxLayout()
        att_v.setContentsMargins(10, 6, 10, 6)
        att_v.setSpacing(2)
        att_cap = QLabel("相对基线衰减量 (mV)")
        att_cap.setObjectName("Caption")
        att_tip = (
            "相对无钢板基线的衰减量 = 无钢板基线 − 当前检波电压（滤波）。\n"
            "正值：信号下降；负值：信号高于基线（如实显示）。"
        )
        att_cap.setToolTip(att_tip)
        att_box.setToolTip(att_tip)
        self._lbl_attenuation = QLabel("—")
        self._lbl_attenuation.setObjectName("ChannelDelta")
        f_att = self._lbl_attenuation.font()
        f_att.setFamily(font_family_mono().split(",")[0].strip())
        f_att.setPointSize(26)
        f_att.setBold(True)
        self._lbl_attenuation.setFont(f_att)
        self._lbl_attenuation.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        att_v.addWidget(att_cap)
        att_v.addWidget(self._lbl_attenuation)
        att_box.setLayout(att_v)
        att_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        pct_box = QFrame()
        pct_box.setObjectName("VerticalMetric")
        pct_v = QVBoxLayout()
        pct_v.setContentsMargins(10, 6, 10, 6)
        pct_v.setSpacing(2)
        pct_cap = QLabel("检波电压下降比例")
        pct_cap.setObjectName("Caption")
        pct_tip = (
            "检波电压下降比例 = 衰减量 / 无钢板基线 × 100%。\n"
            "仅在基线为正且不低于有效下限时给出，否则显示 —。"
        )
        pct_cap.setToolTip(pct_tip)
        pct_box.setToolTip(pct_tip)
        self._lbl_percent = QLabel("—")
        self._lbl_percent.setProperty("role", "numeric")
        f_pct = self._lbl_percent.font()
        f_pct.setFamily(font_family_mono().split(",")[0].strip())
        f_pct.setPointSize(20)
        self._lbl_percent.setFont(f_pct)
        self._lbl_percent.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        pct_v.addWidget(pct_cap)
        pct_v.addWidget(self._lbl_percent)
        pct_box.setLayout(pct_v)
        pct_box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        pct_box.setMinimumWidth(140)

        primary.addWidget(att_box, 1)
        primary.addWidget(pct_box)
        layout.addLayout(primary)

        # 紧凑指标 3×2 网格（两行三列，更适合右栏窄宽）
        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)
        self._m_baseline = _VerticalMetric(
            "无钢板基线 mV",
            tooltip="无钢板基线（ADC mV，历史标定参数；当前帧无效时仍显示）",
        )
        self._m_filtered = _VerticalMetric(
            "滤波后 mV",
            tooltip="滤波后的 ADC 电压（中值+EMA），单位 mV",
        )
        self._m_raw = _VerticalMetric(
            "ADC 原始 mV",
            tooltip="D 帧直接给出的原始电压（mV，物理节点：ADS1115 输入端）；"
                    "未经滤波、未乘分压比",
        )
        self._m_vout = _VerticalMetric(
            "VOUT(分压后) mV",
            tooltip="检波器 VOUT 估计值（mV）= ADC 原始 mV × 分压比；"
                    "物理节点：AD8362 输出端。与上面的 ADC 原始值不是同一个量",
        )
        self._m_delta = _VerticalMetric(
            "旧Δ(诊断) mV",
            tooltip="旧语义 Δ = 滤波 − 基线（诊断量，与衰减量符号相反；"
                    "仅 invert_sign 影响它）",
        )
        self._m_age = _VerticalMetric(
            "更新", tooltip="最后更新时间"
        )
        self._m_scan = _VerticalMetric(
            "帧号",
            tooltip="D 帧的扫描编号（scan_number）；设备重启会归零",
        )
        self._m_index = _VerticalMetric(
            "串口电压序号",
            tooltip="该线圈读取的数据通道：D 帧 mV 之后的第 n 个值"
                    "（1 起，与 CSV 列 chNN 对应）",
        )
        self._m_valid = _VerticalMetric(
            "有效位 (valid_mask)",
            tooltip="本帧 valid_mask 中该数据通道对应的 bit 值：1=有效，0=无效",
        )
        grid.addWidget(self._m_baseline, 0, 0)
        grid.addWidget(self._m_filtered, 0, 1)
        grid.addWidget(self._m_raw, 0, 2)
        grid.addWidget(self._m_vout, 1, 0)
        grid.addWidget(self._m_delta, 1, 1)
        grid.addWidget(self._m_age, 1, 2)
        grid.addWidget(self._m_scan, 2, 0)
        grid.addWidget(self._m_index, 2, 1)
        grid.addWidget(self._m_valid, 2, 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        layout.addLayout(grid)
        self.setLayout(layout)

    def set_profile(self, profile: AdcProfile) -> None:
        self._profile = profile
        if self._channel is not None and self._last_snapshot is not None:
            self._refresh_numbers(
                self._last_snapshot, self._last_update_monotonic
            )

    def set_position(self, position_str: str) -> None:
        self._position = position_str
        self._lbl_position.setText(position_str)

    # ------------------------------------------------------------------
    def update_snapshot(
        self,
        channel: int,
        snapshot: MeasurementSnapshot,
        position: str = "",
        last_update_monotonic: Optional[float] = None,
        label: str = "",
        scan_number: Optional[int] = None,
        valid_bit: Optional[int] = None,
        binding_label: str = "",
    ) -> None:
        """用统一测量快照刷新（主入口；与热力图/CSV 同源）。

        channel 始终是**数据通道（串口电压序号-1）**；label 是该数据
        通道对应的**线圈 CH 标签**。两者在有源板换绑后可能不同，
        详情必须同时给出，便于与串口助手逐项对账。
        """
        self._channel = channel
        self._last_snapshot = snapshot
        self._last_update_monotonic = last_update_monotonic
        if position:
            self._position = position
        shown = label or f"CH{channel + 1:02d}"
        self._lbl_title.setText(
            f"{shown} · 第 {channel + 1} 路电压"
        )
        if binding_label:
            self._lbl_binding.setText(f"绑定：{binding_label}")
        self._lbl_position.setText(self._position or "—")
        self._badge.set_status(
            _STATUS_TEXT.get(snapshot.status, "—"),
            _STATUS_ROLE.get(snapshot.status, "neutral"),
        )
        self._m_index.set_value(
            f"第 {channel + 1} 个（idx {channel}）"
        )
        self._m_scan.set_value(
            f"{int(scan_number)}" if scan_number is not None else "—"
        )
        if valid_bit is None:
            self._m_valid.set_value("—")
        else:
            self._m_valid.set_value(
                f"bit {channel} = {int(valid_bit)}"
            )
        self._refresh_numbers(snapshot, last_update_monotonic)

    def update_channel(
        self, channel: int, state: ChannelState, position: str = ""
    ) -> None:
        """兼容旧调用：由 ChannelState 现场构建快照。

        frame_valid 无法从 state 单独判断时按状态近似（实时链路请走
        update_snapshot 传 processor.measurement() 的权威快照）。
        """
        frame_valid = state.last_status not in (
            ChannelStatus.NO_DATA,
            ChannelStatus.UNKNOWN,
            ChannelStatus.INVALID,
            ChannelStatus.SATURATED,
        )
        divider = self._profile.detector_divider if self._profile else 1.0
        snap = build_snapshot(
            state, frame_valid=frame_valid, detector_divider=divider
        )
        self.update_snapshot(
            channel, snap, position, state.last_update_monotonic
        )

    # ------------------------------------------------------------------
    def _refresh_numbers(
        self,
        snap: MeasurementSnapshot,
        last_update_monotonic: Optional[float],
    ) -> None:
        # 主量：衰减量 / 下降比例（缺失、无效帧 → "—"）
        if snap.attenuation_mv is not None:
            self._lbl_attenuation.setText(_signed(snap.attenuation_mv))
        else:
            self._lbl_attenuation.setText("—")
        if snap.detector_drop_pct is not None:
            self._lbl_percent.setText(
                f"{_signed(snap.detector_drop_pct, 2)}%"
            )
        else:
            self._lbl_percent.setText("—")

        # 次级：基线为历史标定参数，始终显示；其余各字段直接采用
        # 快照值——快照已按统一有效性门槛判定（无效帧 None、饱和
        # 帧保留 rail 诊断采样、预热帧有真实采样但无滤波值），
        # 不再叠加第二道门槛（否则与热力图 tooltip 同源不同显示：
        # 预热/饱和时 tooltip 有真实采样而面板显示 "—"）
        self._m_baseline.set_value(
            f"{snap.baseline_mv:.3f}" if snap.baseline_mv is not None else "—"
        )
        self._m_filtered.set_value(
            f"{snap.filtered_mv:.3f}"
            if snap.filtered_mv is not None
            else "—"
        )
        self._m_raw.set_value(
            f"{snap.sample_mv:.3f}"
            if snap.sample_mv is not None
            else "—"
        )
        self._m_vout.set_value(
            f"{snap.detector_vout_mv:.3f}"
            if snap.detector_vout_mv is not None
            else "—"
        )
        self._m_delta.set_value(
            _signed(snap.delta_legacy_mv)
            if snap.delta_legacy_mv is not None
            else "—"
        )
        if last_update_monotonic is not None:
            age = time.monotonic() - last_update_monotonic
            if 0 <= age < 600:
                self._m_age.set_value(f"{age:.1f} s 前")
            else:
                self._m_age.set_value("—")
        else:
            self._m_age.set_value("—")
