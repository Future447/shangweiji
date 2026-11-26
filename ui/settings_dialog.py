"""
ui/settings_dialog.py — 设置对话框：四个页签。

UI 重构要点：
- 改为四页签：采集 / 通道 / 处理与显示 / 实验信息
- 通道页采用与主界面一致的 4×4 网格 + 快捷选择
- 字段错误就地提示
- 底部固定"保存"和"取消"
- result_values 键名与旧实现保持兼容
"""

from __future__ import annotations

from typing import Dict, List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from models.adc_profile import AdcProfile, build_default_profiles
from core.channel_layout import (
    BINDING_DESIGN,
    BINDING_IDENTITY,
    BINDING_LEGACY_V341,
    BINDING_NAMES,
    HARDWARE_CHANNEL_MAP,
    LAYOUT_ACTIVE_PCB,
    LAYOUT_BREADBOARD,
    LAYOUT_CUSTOM,
    binding_name,
    data_label_map,
    data_position_map,
    default_binding,
    is_valid_map,
    layout_id_for_map,
    layout_map,
    layout_name,
    maps_equal,
    normalize_channel_map,
)
from ui.theme import font_family_ui


# 通道网格中的"快捷选择"
# [0,1,4,5] = CH01/CH02/CH05/CH06 = 实际接线的左下 2×2 区域
PRESET_4CH = [0, 1, 4, 5]
PRESET_16CH = list(range(16))


class _ChannelChip(QPushButton):
    """通道页网格中的单个 chip。

    chip 代表**固件原始数据通道**（channel = 0..15），显示文本是
    热力图上的线圈标签（有源板绑定后可能不是 CH(channel+1)）。
    """

    def __init__(self, channel: int, label: str = "", parent=None) -> None:
        super().__init__(label or f"CH{channel + 1:02d}", parent)
        self.channel = channel
        self.setCheckable(True)
        self.setMinimumSize(60, 40)
        self.setMaximumSize(80, 44)
        self.setProperty("role", "chip")
        self.set_label(label)

    def set_label(self, label: str) -> None:
        shown = label or f"CH{self.channel + 1:02d}"
        self.text_label = shown
        self.setText(shown)
        self.setToolTip(
            f"{shown}（线圈标签）\n"
            f"串口电压序号：第 {self.channel + 1} 个（idx {self.channel}）"
        )


class SettingsDialog(QDialog):
    """应用设置对话框。返回值通过 properties 传递。"""

    def __init__(
        self,
        current_profile: AdcProfile,
        enabled_channels: List[int],
        ema_alpha: float,
        median_window: int,
        heatmap_min: float,
        heatmap_max: float,
        heatmap_auto: bool,
        curve_window_s: float,
        invert_sign: bool,
        experiment_params: dict,
        channel_map: Optional[Dict[int, tuple]] = None,
        layout_id: Optional[str] = None,
        cell_binding: Optional[str] = None,
        binding_confirmed: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("系统设置")
        self.setMinimumSize(640, 560)
        self.resize(680, 620)

        self._profiles = build_default_profiles()
        self.result_values: dict = {}
        # 当前生效的通道物理映射（非法/缺失 → 硬件默认），
        # 用于通道启用网格按实际物理位置摆放
        norm_map = normalize_channel_map(channel_map)
        self._channel_map: Dict[int, tuple] = (
            norm_map if is_valid_map(norm_map)
            else dict(HARDWARE_CHANNEL_MAP)
        )
        self._is_hw_layout = maps_equal(
            self._channel_map, HARDWARE_CHANNEL_MAP
        )
        self._restore_hw_layout = False
        # 布局标识：显式传入优先，否则按映射反推（旧配置无此字段）
        self._layout_id: str = (
            str(layout_id) if layout_id else layout_id_for_map(self._channel_map)
        )
        # 线圈绑定：显式传入 → 按映射反查 → 布局默认。
        # 有源板的两张表存在冲突，绝不在用户未确认时静默套用旧表。
        from core.channel_layout import binding_for_map

        detected = binding_for_map(self._channel_map)
        if cell_binding in (BINDING_DESIGN, BINDING_LEGACY_V341):
            self._cell_binding: str = str(cell_binding)
        elif detected:
            self._cell_binding = detected
        else:
            self._cell_binding = default_binding(self._layout_id)
        self._binding_confirmed: bool = bool(binding_confirmed)

        outer = QVBoxLayout()
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(10)

        # 顶部标题
        title = QLabel("系统设置")
        title_font = title.font()
        title_font.setPointSize(13)
        title_font.setBold(True)
        title.setFont(title_font)
        outer.addWidget(title)

        # 页签
        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_acquisition_tab(current_profile), "采集")
        self._tabs.addTab(
            self._build_channels_tab(enabled_channels), "通道"
        )
        self._tabs.addTab(
            self._build_processing_tab(
                ema_alpha, median_window, heatmap_min,
                heatmap_max, heatmap_auto, curve_window_s, invert_sign,
            ),
            "处理与显示",
        )
        self._tabs.addTab(
            self._build_experiment_tab(experiment_params), "实验信息"
        )
        outer.addWidget(self._tabs, 1)

        # 底部固定按钮
        btns = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        # 给 OK 应用 primary 视觉
        ok_btn = btns.button(QDialogButtonBox.Ok)
        ok_btn.setText("保存")
        ok_btn.setProperty("role", "primary")
        cancel_btn = btns.button(QDialogButtonBox.Cancel)
        cancel_btn.setText("取消")
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        outer.addWidget(btns)

        self.setLayout(outer)

    # ------------------------------------------------------------------
    def _build_acquisition_tab(self, current_profile: AdcProfile) -> QWidget:
        page = QWidget()
        v = QVBoxLayout()
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(10)

        info = QLabel(
            "选择 ADC Profile。Profile 决定量程（mV）与前端分压补偿；\n"
            "如切换，现有无钢板基线可能失效，建议重新进行无钢板基线标定。\n"
            "有源线圈 PCB 若无 1:2 分压，请选"
            "「ADS1115 / ±4.096V / 无分压（有源线圈 PCB）」。\n"
            "通道布局（旧面包板 / 有源线圈 PCB）在「通道」页单独设置，"
            "二者互不影响。"
        )
        info.setObjectName("Caption")
        info.setWordWrap(True)
        v.addWidget(info)

        gb = QFrame()
        gb.setObjectName("SurfaceCard")
        form = QFormLayout()
        form.setSpacing(8)
        self._cmb_profile = QComboBox()
        for name, p in self._profiles.items():
            self._cmb_profile.addItem(p.display_name, name)
        idx = self._cmb_profile.findData(current_profile.name)
        if idx >= 0:
            self._cmb_profile.setCurrentIndex(idx)
        self._lbl_profile_desc = QLabel(current_profile.description)
        self._lbl_profile_desc.setWordWrap(True)
        self._lbl_profile_desc.setObjectName("Caption")
        self._cmb_profile.currentIndexChanged.connect(self._on_profile_changed)
        form.addRow("选择 Profile:", self._cmb_profile)
        form.addRow("说明:", self._lbl_profile_desc)
        gb.setLayout(form)
        v.addWidget(gb)

        # 参考电压（与 Profile 关联；允许只读展示）
        gb_vref = QFrame()
        gb_vref.setObjectName("SurfaceCard")
        vref_form = QFormLayout()
        self._lbl_vref = QLabel(
            f"{current_profile.vref_mv} mV (来自所选 Profile)"
        )
        self._lbl_vref.setProperty("role", "numeric")
        vref_form.addRow("Vref:", self._lbl_vref)
        gb_vref.setLayout(vref_form)
        v.addWidget(gb_vref)

        v.addStretch(1)
        page.setLayout(v)
        return page

    def _build_channels_tab(self, enabled_channels: List[int]) -> QWidget:
        page = QWidget()
        v = QVBoxLayout()
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(10)

        info = QLabel(
            "启用通道会参与热力图、曲线、标定和 CSV 记录。\n"
            "未启用通道的 wire mV 仍会写入 CSV（仅在 valid_mask=1 时）。"
        )
        info.setObjectName("Caption")
        info.setWordWrap(True)
        v.addWidget(info)

        # 快捷选择
        preset_row = QHBoxLayout()
        preset_row.setSpacing(8)
        btn_4ch = QPushButton("左下 2×2")
        btn_4ch.setProperty("role", "secondary")
        btn_4ch.setToolTip(
            "按线圈标签选择 CH01/CH02/CH05/CH06（左下 2×2 区域），"
            "保存时自动换算成对应的串口电压序号。"
        )
        btn_4ch.clicked.connect(
            lambda: self._apply_preset(self._raw_for_labels([1, 2, 5, 6]))
        )
        btn_16ch = QPushButton("全部通道")
        btn_16ch.setProperty("role", "secondary")
        btn_16ch.clicked.connect(lambda: self._apply_preset(PRESET_16CH))
        btn_none = QPushButton("清空")
        btn_none.setProperty("role", "ghost")
        btn_none.clicked.connect(lambda: self._apply_preset([]))
        preset_row.addWidget(btn_4ch)
        preset_row.addWidget(btn_16ch)
        preset_row.addWidget(btn_none)
        preset_row.addStretch(1)
        v.addLayout(preset_row)

        # 4×4 网格：按当前生效的物理映射摆放（与热力图一致），
        # 不再是独立的行优先排布
        grid_frame = QFrame()
        grid_frame.setObjectName("SurfaceCard")
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        self._chips_grid = grid
        self._chips: List[_ChannelChip] = []
        labels = data_label_map(self._layout_id, self._cell_binding)
        for ch in range(16):
            r, c = self._channel_map[ch]
            # chip 代表原始数据通道，文本用热力图同款线圈标签
            chip = _ChannelChip(ch, labels.get(ch, ""))
            chip.setChecked(ch in enabled_channels)
            self._chips.append(chip)
            grid.addWidget(chip, r, c)
        grid_frame.setLayout(grid)
        v.addWidget(grid_frame)

        # 通道物理布局：显式布局选择（旧面包板 / 有源线圈 PCB / 自定义）
        # 只改变"通道号 → 热力图位置"的显示映射，数据/基线/CSV 列不变。
        lay_frame = QFrame()
        lay_frame.setObjectName("SurfaceCard")
        lay_h = QHBoxLayout()
        lay_h.setSpacing(8)
        lay_h.addWidget(QLabel("布局:"))
        self._cmb_layout = QComboBox()
        for lid in (LAYOUT_BREADBOARD, LAYOUT_ACTIVE_PCB):
            self._cmb_layout.addItem(layout_name(lid), lid)
        if self._layout_id == LAYOUT_CUSTOM:
            self._cmb_layout.addItem(layout_name(LAYOUT_CUSTOM), LAYOUT_CUSTOM)
        cur_idx = self._cmb_layout.findData(self._layout_id)
        self._cmb_layout.setCurrentIndex(cur_idx if cur_idx >= 0 else 0)
        self._cmb_layout.setMinimumWidth(240)
        self._cmb_layout.currentIndexChanged.connect(self._on_layout_changed)
        lay_h.addWidget(self._cmb_layout)

        lay_h.addWidget(QLabel(""))
        lay_text = self._layout_text()
        self._lbl_layout = QLabel(lay_text)
        self._lbl_layout.setObjectName("Caption")
        self._lbl_layout.setWordWrap(True)
        lay_h.addWidget(self._lbl_layout, 1)
        self._btn_restore_layout = QPushButton("恢复旧面包板布局")
        self._btn_restore_layout.setProperty("role", "secondary")
        self._btn_restore_layout.setToolTip(
            "把热力图/通道网格的物理位置恢复为旧面包板布局。\n"
            "仅显示位置变化；通道数据、基线、CSV 列均不受影响。"
        )
        self._btn_restore_layout.setEnabled(not self._is_hw_layout)
        self._btn_restore_layout.clicked.connect(self._on_restore_hw_layout)
        lay_h.addWidget(self._btn_restore_layout)
        lay_frame.setLayout(lay_h)
        v.addWidget(lay_frame)

        # 线圈绑定：有源板存在两张互相冲突的表（PCB 设计对应 vs
        # v3.4.1 旧映射），必须由用户逐点实测后显式选择并勾选确认。
        bind_frame = QFrame()
        bind_frame.setObjectName("SurfaceCard")
        bind_h = QHBoxLayout()
        bind_h.setSpacing(8)
        bind_h.addWidget(QLabel("线圈绑定:"))
        self._cmb_binding = QComboBox()
        for bid in (BINDING_DESIGN, BINDING_LEGACY_V341, BINDING_IDENTITY):
            self._cmb_binding.addItem(BINDING_NAMES[bid], bid)
        self._cmb_binding.setMinimumWidth(300)
        self._cmb_binding.setToolTip(
            "决定「线圈 CH 标签」读取哪一路串口电压。\n"
            "CHn → 第 n 路：PCB 设计对应（前提：实物接线与设计一致）。\n"
            "CH01←9 …：v3.4.1 旧映射（上一版硬编码，需实测确认）。"
        )
        self._cmb_binding.currentIndexChanged.connect(
            self._on_binding_changed
        )
        bind_h.addWidget(self._cmb_binding, 1)
        self._chk_binding_confirmed = QCheckBox("已逐点实测确认")
        self._chk_binding_confirmed.setToolTip(
            "勾选表示你已用 16 个线圈逐点核对该绑定；\n"
            "未勾选时主界面会持续提示“绑定未实测确认”。"
        )
        bind_h.addWidget(self._chk_binding_confirmed)
        bind_frame.setLayout(bind_h)
        v.addWidget(bind_frame)
        self._sync_binding_ui()

        # 校验提示
        self._lbl_ch_warn = QLabel("")
        self._lbl_ch_warn.setObjectName("Caption")
        self._lbl_ch_warn.setVisible(False)
        v.addWidget(self._lbl_ch_warn)
        for chip in self._chips:
            chip.toggled.connect(self._refresh_channel_warn)
        self._refresh_channel_warn()

        v.addStretch(1)
        page.setLayout(v)
        return page

    def _build_processing_tab(
        self,
        ema_alpha: float,
        median_window: int,
        heatmap_min: float,
        heatmap_max: float,
        heatmap_auto: bool,
        curve_window_s: float,
        invert_sign: bool,
    ) -> QWidget:
        page = QWidget()
        v = QVBoxLayout()
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(10)

        # 滤波
        gb = QFrame()
        gb.setObjectName("SurfaceCard")
        filt_form = QFormLayout()
        filt_form.setSpacing(8)
        self._sp_ema = QDoubleSpinBox()
        self._sp_ema.setRange(0.0, 1.0)
        self._sp_ema.setSingleStep(0.05)
        self._sp_ema.setDecimals(2)
        self._sp_ema.setValue(ema_alpha)
        self._sp_med = QSpinBox()
        self._sp_med.setRange(1, 9)
        self._sp_med.setValue(median_window)
        self._chk_invert = QCheckBox(
            "旧诊断 Δ 符号反转（仅旧诊断量；不影响衰减量/下降比例）"
        )
        self._chk_invert.setToolTip(
            "仅翻转旧语义 Δ = 滤波 − 基线 及其旧百分比（诊断/兼容用途）。\n"
            "衰减量 = 无钢板基线 − 滤波 与检波电压下降比例不受影响，\n"
            "始终按真实方向显示（正=信号下降，负=信号升高）。"
        )
        self._chk_invert.setChecked(invert_sign)
        filt_form.addRow("EMA α:", self._sp_ema)
        filt_form.addRow("中值窗口:", self._sp_med)
        filt_form.addRow("", self._chk_invert)
        gb.setLayout(filt_form)
        v.addWidget(gb)

        # 热力图
        gb_hm = QFrame()
        gb_hm.setObjectName("SurfaceCard")
        hm_form = QFormLayout()
        hm_form.setSpacing(8)
        self._chk_hm_auto = QCheckBox("自动范围（防尖峰）")
        self._chk_hm_auto.setChecked(heatmap_auto)
        self._sp_hm_min = QDoubleSpinBox()
        self._sp_hm_min.setRange(-1e9, 0.0)
        self._sp_hm_min.setDecimals(1)
        self._sp_hm_min.setValue(heatmap_min)
        self._sp_hm_max = QDoubleSpinBox()
        self._sp_hm_max.setRange(0.0, 1e9)
        self._sp_hm_max.setDecimals(1)
        self._sp_hm_max.setValue(heatmap_max)
        self._chk_hm_auto.setToolTip(
            "自动按当前数据调整色阶（便于观察相对变化）；"
            "取消勾选后用固定色阶，便于标定前后/不同实验之间对比"
        )
        hm_form.addRow(self._chk_hm_auto)
        hm_form.addRow("色阶下界 (衰减量 mV):", self._sp_hm_min)
        hm_form.addRow("色阶上界 (衰减量 mV):", self._sp_hm_max)
        gb_hm.setLayout(hm_form)
        v.addWidget(gb_hm)

        # 曲线
        gb_plot = QFrame()
        gb_plot.setObjectName("SurfaceCard")
        plot_form = QFormLayout()
        self._sp_window = QDoubleSpinBox()
        self._sp_window.setRange(5.0, 600.0)
        self._sp_window.setValue(curve_window_s)
        self._sp_window.setSuffix(" s")
        plot_form.addRow("时间窗口:", self._sp_window)
        gb_plot.setLayout(plot_form)
        v.addWidget(gb_plot)

        v.addStretch(1)
        page.setLayout(v)
        return page

    def _build_experiment_tab(self, experiment_params: dict) -> QWidget:
        page = QWidget()
        v = QVBoxLayout()
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(10)

        info = QLabel("实验参数会写入 CSV 文件名与每行尾部。")
        info.setObjectName("Caption")
        v.addWidget(info)

        gb = QFrame()
        gb.setObjectName("SurfaceCard")
        form = QFormLayout()
        form.setSpacing(8)
        self._ed_freq = QLineEdit(str(experiment_params.get("frequency", "")))
        self._ed_freq_unit = QLineEdit(str(experiment_params.get("freq_unit", "")))
        self._ed_gain = QLineEdit(str(experiment_params.get("gain", "")))
        self._ed_coil = QLineEdit(str(experiment_params.get("coil", "")))
        self._ed_plate = QLineEdit(str(experiment_params.get("plate", "")))
        self._ed_note = QLineEdit(str(experiment_params.get("note", "")))
        form.addRow("激励频率:", self._ed_freq)
        form.addRow("频率单位:", self._ed_freq_unit)
        form.addRow("增益:", self._ed_gain)
        form.addRow("线圈:", self._ed_coil)
        form.addRow("铁板:", self._ed_plate)
        form.addRow("备注:", self._ed_note)
        gb.setLayout(form)
        v.addWidget(gb)

        v.addStretch(1)
        page.setLayout(v)
        return page

    # ------------------------------------------------------------------
    def _on_profile_changed(self, idx: int) -> None:
        name = self._cmb_profile.itemData(idx)
        p = self._profiles.get(name)
        if p is not None:
            self._lbl_profile_desc.setText(p.description)
            self._lbl_vref.setText(f"{p.vref_mv} mV (来自所选 Profile)")

    def _apply_preset(self, channels: List[int]) -> None:
        for chip in self._chips:
            chip.setChecked(chip.channel in set(channels))
        self._refresh_channel_warn()

    def _layout_text(self) -> str:
        if self._layout_id == LAYOUT_CUSTOM:
            return (
                "当前物理布局：自定义（与任何预设都不同）\n"
                "仅影响显示位置；通道数据、基线、CSV 列顺序不受影响。"
            )
        if self._layout_id == LAYOUT_ACTIVE_PCB:
            return (
                "当前物理布局：有源线圈 PCB（元件面朝观察者、FPC 朝左）\n"
                "自上而下：CH09-12 / CH13-16 / CH08-05 / CH04-01\n"
                f"线圈绑定：{binding_name(self._cell_binding)}"
            )
        return "当前物理布局：旧面包板（4 列，每列自下而上）"

    def _apply_layout_map(self, mapping, labels=None) -> None:
        """按映射重排启用通道网格，并把 chip 文本换成线圈标签。"""
        self._channel_map = dict(mapping)
        if labels is None:
            labels = data_label_map(self._layout_id, self._cell_binding)
        for chip in self._chips:
            pos = self._channel_map.get(chip.channel)
            if pos is not None:
                self._chips_grid.addWidget(chip, pos[0], pos[1])
            chip.set_label(labels.get(chip.channel, ""))
        self._is_hw_layout = maps_equal(
            self._channel_map, HARDWARE_CHANNEL_MAP
        )
        self._btn_restore_layout.setEnabled(not self._is_hw_layout)

    def _raw_for_labels(self, label_numbers: List[int]) -> List[int]:
        """按线圈标签号（1 基）反查原始数据通道列表。"""
        labels = data_label_map(self._layout_id, self._cell_binding)
        inv = {v: k for k, v in labels.items()}
        return [
            inv[f"CH{n:02d}"] for n in label_numbers
            if f"CH{n:02d}" in inv
        ]

    def _sync_binding_ui(self) -> None:
        """绑定下拉/确认框的可用性随布局同步（仅对视图，不写数据）。"""
        if not hasattr(self, "_cmb_binding"):
            return
        active = self._layout_id == LAYOUT_ACTIVE_PCB
        self._cmb_binding.setEnabled(active)
        self._chk_binding_confirmed.setEnabled(active)
        self._chk_binding_confirmed.setChecked(
            bool(self._binding_confirmed) if active else True
        )
        idx = self._cmb_binding.findData(self._cell_binding)
        if idx >= 0:
            self._cmb_binding.blockSignals(True)
            self._cmb_binding.setCurrentIndex(idx)
            self._cmb_binding.blockSignals(False)

    def _on_layout_changed(self, _idx: int) -> None:
        """布局下拉切换：只改变显示位置与绑定可选性，不改数据。"""
        lid = self._cmb_layout.currentData() or LAYOUT_BREADBOARD
        self._layout_id = str(lid)
        if lid in (LAYOUT_BREADBOARD, LAYOUT_ACTIVE_PCB):
            if lid == LAYOUT_BREADBOARD:
                self._cell_binding = BINDING_IDENTITY
            elif self._cell_binding not in (
                BINDING_DESIGN, BINDING_LEGACY_V341
            ):
                # 切入有源板：默认 PCB 设计对应，绝不静默用旧表
                self._cell_binding = default_binding(lid)
            self._apply_layout_map(
                data_position_map(self._layout_id, self._cell_binding)
            )
            # 下拉切换本身不置"恢复"标志：由 result_values["layout_id"]
            # 决定最终布局；"恢复"按钮仍保留独立语义
            self._restore_hw_layout = False
        self._lbl_layout.setText(self._layout_text())
        self._sync_binding_ui()

    def _on_binding_changed(self, _idx: int) -> None:
        """切换线圈绑定：映射与 chip 标签同时更新（所见即所得）。"""
        bid = self._cmb_binding.currentData() or BINDING_DESIGN
        self._cell_binding = str(bid)
        # 改绑必须重新实测确认
        self._binding_confirmed = False
        self._chk_binding_confirmed.setChecked(False)
        if self._layout_id == LAYOUT_ACTIVE_PCB:
            self._apply_layout_map(
                data_position_map(self._layout_id, self._cell_binding)
            )
        self._lbl_layout.setText(self._layout_text())

    def _on_restore_hw_layout(self) -> None:
        """显式恢复旧面包板布局（确认后随"保存"生效）。"""
        r = QMessageBox.question(
            self,
            "恢复旧面包板布局",
            "将把通道物理布局恢复为旧面包板（4 列，每列自下而上）。\n"
            "仅显示位置变化；通道数据、基线、CSV 列不受影响。\n"
            '点击"保存"后生效。继续？',
        )
        if r != QMessageBox.Yes:
            return
        self._restore_hw_layout = True
        self._layout_id = LAYOUT_BREADBOARD
        if hasattr(self, "_cmb_layout"):
            idx = self._cmb_layout.findData(LAYOUT_BREADBOARD)
            if idx >= 0:
                self._cmb_layout.blockSignals(True)
                self._cmb_layout.setCurrentIndex(idx)
                self._cmb_layout.blockSignals(False)
        self._cell_binding = BINDING_IDENTITY
        self._binding_confirmed = True
        self._apply_layout_map(HARDWARE_CHANNEL_MAP)
        self._sync_binding_ui()
        self._lbl_layout.setText("当前物理布局：将在保存后恢复为旧面包板")

    def _refresh_channel_warn(self) -> None:
        if not any(c.isChecked() for c in self._chips):
            self._lbl_ch_warn.setText(
                "请至少启用一个通道；如全部取消将自动使用 CH01。"
            )
            self._lbl_ch_warn.setVisible(True)
        else:
            self._lbl_ch_warn.setVisible(False)

    def _on_accept(self) -> None:
        enabled = [c.channel for c in self._chips if c.isChecked()]
        if not enabled:
            enabled = [0]
        self.result_values = {
            "profile_name": self._cmb_profile.currentData(),
            "enabled_channels": enabled,
            "ema_alpha": self._sp_ema.value(),
            "median_window": self._sp_med.value(),
            "heatmap_min": self._sp_hm_min.value(),
            "heatmap_max": self._sp_hm_max.value(),
            "heatmap_auto": self._chk_hm_auto.isChecked(),
            "curve_window_s": self._sp_window.value(),
            "invert_sign": self._chk_invert.isChecked(),
            "restore_hw_layout": self._restore_hw_layout,
            # 最终布局标识（预设或自定义）；主窗口据此同步 channel_map
            "layout_id": self._layout_id,
            # 线圈标签 ↔ 串口电压序号绑定（有源板必须显式确认）
            "cell_binding": self._cell_binding,
            "binding_confirmed": bool(
                self._chk_binding_confirmed.isChecked()
            ),
            "experiment_params": {
                "frequency": self._ed_freq.text(),
                "freq_unit": self._ed_freq_unit.text(),
                "gain": self._ed_gain.text(),
                "coil": self._ed_coil.text(),
                "plate": self._ed_plate.text(),
                "note": self._ed_note.text(),
            },
        }
        self.accept()
