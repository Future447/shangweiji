"""
主窗口：涡流阵列采集系统 v3（mV 协议，无钢板标定 → 放钢板衰减实验）。

P0 整改保持：
- P0-1 标定帧驱动（calibration_manager.on_frame）
- P0-2 CSV 写 DFrame 直采（recorder.write_dframe）
- P0-3 串口/演示严格互斥
- P0-4 故障判定：协议不匹配可从未收到 D 帧时触发
- P0-5 invert_sign 仅作用于旧诊断 Δ

实验流程整改：
- 统一测量快照（core.measurement）：详情/热力图/tooltip/曲线/CSV 同源
- 主测量量：衰减量 = 无钢板基线 − 当前检波电压（滤波）；
  检波电压下降比例仅在基线为正且达有效下限时给出
- 无钢板基线标定：开始前需用户确认已移除钢板；按作用域（serial/demo）
  分别保存基线，演示标定不覆盖真实硬件基线
- 切换数据源：清理滤波/当前帧有效性/曲线会话数据，按设备标识
  （USB VID:PID+序列号，不只看 COM 口号）匹配已保存基线；
  忽略旧来源延迟到达的回调

UI 重构要点（来自 UI_DESIGN_ADVICE_AND_CLAUDE_PROMPT.md）：
- 默认 1360×840 窗口
- 顶部状态条：全局状态、FPS、帧、丢帧
- 操作带：端口 + 连接 + 演示 + 记录
- 4×4 热力图优先，辅助面板按需展开
- QDockWidget 容纳"日志 / 原始数据"，默认折叠
- 全部用主题令牌和 QStyle.standardIcon
"""

from __future__ import annotations

import math
import os
import time
import uuid

import numpy as np
from collections import deque
from enum import IntEnum
from typing import Dict, List, Optional

from PyQt5.QtCore import QSize, Qt, QTimer
from PyQt5.QtGui import QFont, QKeySequence
from PyQt5.QtWidgets import (
    QAction,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QStyle,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.calibration_manager import CalibrationManager
from core.channel_layout import (
    BINDING_DESIGN,
    BINDING_IDENTITY,
    BINDING_LEGACY_V341,
    HARDWARE_CHANNEL_MAP,
    LAYOUT_ACTIVE_PCB,
    LAYOUT_BREADBOARD,
    binding_for_map,
    binding_name,
    is_valid_map,
    labels_for_map,
    layout_map,
    layout_name,
    map_to_grid_list,
    maps_equal,
    normalize_channel_map,
)
from core.channel_processor import ChannelProcessor
from core.config_manager import (
    SCOPE_DEMO,
    SCOPE_SERIAL,
    SCOPE_TCP,
    TRANSPORT_SERIAL,
    TRANSPORT_TCP,
    ConfigManager,
)
from core.demo_source import DemoSource
from core.measurement import ATTENUATION_FORMULA_ID
from core.protocol_parser import FrameKind, FrameParser, ParsedFrame
from core.recorder import Recorder
from core.replay_source import CsvSchemaError, ReplaySource
from core.serial_worker import (
    SerialWorker,
    device_id_for_port,
    list_serial_ports,
)
from core.tcp_worker import TcpServerWorker
from models.adc_profile import AdcProfile, build_default_profiles, profile_by_name
from models.channel_state import ChannelStatus
from models.frame import DFrame, EFrame, MAX_CHANNEL_COUNT, MV_MAX, MV_MIN
from ui.array_analysis_dialog import ArrayAnalysisDialog
from ui.channel_panel import ChannelPanel
from ui.compensation_panel import CompensationPanel
from ui.components import ElidingLabel, SectionHeader, StatusBadge, SurfaceCard
from ui.heatmap_widget import HeatmapLegend, HeatmapWidget
from ui.inspection_controller import InspectionController, MODE_NAMES, reference_context, replay_context, usable_samples
from ui.inspection_panel import InspectionPanel
from ui.plot_widget import PlotWidget
from ui.replay_controls import ReplayControls
from ui.settings_dialog import SettingsDialog
from ui.theme import METRICS, PALETTE, build_stylesheet, set_status_role


# ---------------------------------------------------------------------
# 常量与枚举
# ---------------------------------------------------------------------
class SourceState(IntEnum):
    DISCONNECTED = 0
    CONNECTING = 1
    #: 数据源已连接（串口或 TCP）。TCP 沿用同一数值语义，便于
    #: 既有"已连接才判新鲜度/才可标定"的判断无需逐处改写。
    SERIAL_CONNECTED = 2
    DEMO = 3
    REPLAY = 4
    #: TCP 已开始监听但设备尚未连入——绝不能被当成"已连接"
    LISTENING = 5


#: TCP 已连接状态的可读别名（与 SERIAL_CONNECTED 同值）
TCP_CONNECTED = SourceState.SERIAL_CONNECTED


DATA_TIMEOUT_S = 5.0
NO_BYTES_TIMEOUT_S = 5.0
PROTOCOL_MISMATCH_TIMEOUT_S = 5.0
#: 扫描号小幅回退（≤ 该值）视为乱序/延迟帧，而非设备重启
SCAN_REORDER_WINDOW = 8
#: 扫描号缺口日志的每段条数上限（防止刷屏）
SCAN_GAP_LOG_LIMIT = 20
REFRESH_INTERVAL_MS = 80
HEATMAP_RANGE_HOLD_S = 1.5
FPS_WINDOW_S = 10.0


# ---------------------------------------------------------------------
# 消息栏
# ---------------------------------------------------------------------
class MessageBar(QFrame):
    """顶部消息栏：故障原因 + 建议操作。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("MessageBar")
        self.setProperty("statusRole", "warning")
        self.setVisible(False)
        self.setMinimumHeight(36)
        self.setMaximumHeight(48)
        h = QHBoxLayout()
        h.setContentsMargins(12, 6, 12, 6)
        h.setSpacing(12)
        self._lbl_icon = QLabel("⚠")
        self._lbl_icon.setObjectName("MessageBarIcon")
        self._lbl_text = QLabel("")
        self._lbl_text.setObjectName("MessageBarText")
        self._btn_action1 = QPushButton()
        self._btn_action1.setProperty("role", "secondary")
        self._btn_action1.setVisible(False)
        self._btn_action2 = QPushButton()
        self._btn_action2.setProperty("role", "ghost")
        self._btn_action2.setVisible(False)
        h.addWidget(self._lbl_icon)
        h.addWidget(self._lbl_text, 1)
        h.addWidget(self._btn_action1)
        h.addWidget(self._btn_action2)
        self.setLayout(h)

    def show_message(
        self,
        text: str,
        role: str = "warning",
        actions: Optional[List[tuple]] = None,
    ) -> None:
        self._lbl_text.setText(text)
        set_status_role(self, role)
        for btn in (self._btn_action1, self._btn_action2):
            btn.setVisible(False)
            try:
                btn.clicked.disconnect()
            except TypeError:
                pass
        if actions:
            for i, (label, _role, callback) in enumerate(actions[:2]):
                btn = self._btn_action1 if i == 0 else self._btn_action2
                btn.setText(label)
                btn.setVisible(True)
                btn.clicked.connect(callback)
        self.setVisible(True)

    def clear(self) -> None:
        self._lbl_text.setText("")
        for btn in (self._btn_action1, self._btn_action2):
            btn.setVisible(False)
        self.setVisible(False)


# ---------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------
class MainWindow(QMainWindow):
    """主窗口。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("涡流传感器采集系统 v3 (mV)")
        self.resize(METRICS.window_default_w, METRICS.window_default_h)
        self.setMinimumSize(METRICS.window_min_w, METRICS.window_min_h)
        self.setStyleSheet(build_stylesheet())

        # --- 核心对象 ---
        self._profiles = build_default_profiles()
        self._config = ConfigManager()
        self._processor = ChannelProcessor(
            adc_profile=self._config.adc_profile,
            enabled_channels=self._config.enabled_channels,
        )
        self._parser = FrameParser()
        self._calib = CalibrationManager(self._processor, self)
        self._recorder = Recorder()
        self._demo: Optional[DemoSource] = None
        self._serial: Optional[SerialWorker] = None
        # TCP 监听线程（Hi3863 作为客户端连入）；与串口同一时刻只允许一个
        self._tcp: Optional[TcpServerWorker] = None
        self._transport: str = self._config.transport
        self._tcp_endpoint: str = ""
        self._tcp_peer: str = ""
        self._tcp_idle_timeout_reported: bool = False
        #: 设备成功连入次数（诊断/验收用：验证重连确实发生）
        self._tcp_connect_count: int = 0

        # --- 三层状态分离：应用会话 / 连接段 / 源设备运行周期 ---
        # 连接段（segment）：一次 TCP 连接（或一次串口会话）内的收数统计。
        # 数据健康判断只用段内计数，绝不用跨段的累计值。
        self._segment_index: int = 0
        self._segment_byte_count: int = 0
        self._segment_dframe_count: int = 0
        self._segment_eframe_count: int = 0
        self._segment_start_time: float = 0.0
        self._segment_last_byte_time: float = 0.0
        self._segment_last_dframe_time: float = 0.0
        #: 进程级累计 D 帧数（跨会话/跨连接段，仅诊断与验收使用）
        self._total_dframe_count: int = 0
        # 源设备运行周期（epoch）：设备重启/扫描号归零即进入新周期
        self._scan_epoch: int = 0
        # 扫描号统计：缺口 ≠ 无线丢包率，只是"未观测到的扫描次数"
        self._scan_gaps: int = 0
        self._scan_wraps: int = 0
        self._scan_restarts: int = 0
        self._scan_duplicates: int = 0
        self._scan_reorders: int = 0
        self._replay: Optional[ReplaySource] = ReplaySource(self)
        self._array_analysis: Optional[ArrayAnalysisDialog] = None
        self._array_last_live_scan: Optional[int] = None
        self._array_replay_revision = 0
        self._task_mode = "compensation"
        self._workspace_tool = None
        self._latest_workflow_packet = None
        self._latest_compensated = None
        self._last_replay_task_row = None
        # 回放临时映射：当前热力图是否正按"记录时布局"显示。
        # 只改显示位置，绝不写入实时配置；退出回放时恢复当前硬件布局。
        self._replay_using_recorded_map: bool = False

        # --- 状态 ---
        self._source_state: SourceState = SourceState.DISCONNECTED
        self._selected_channel: int = (
            self._config.enabled_channels[0]
            if self._config.enabled_channels
            else 0
        )
        self._session_id: str = ""
        self._last_dframe: Optional[DFrame] = None
        self._last_eframe: Optional[EFrame] = None
        self._last_rx_byte_time: float = 0.0
        self._last_valid_data_time: float = 0.0
        self._session_start_time: float = 0.0
        self._data_timeout_reported: bool = False
        self._data_timeout_reason: str = ""
        self._frame_count: int = 0
        self._error_count: int = 0
        self._recv_byte_count: int = 0
        self._expected_scan_number: Optional[int] = None
        self._dropped_frames: int = 0
        self._last_diag_lines = deque(maxlen=200)
        self._heatmap_range_max = max(
            abs(self._config.heatmap_min),
            abs(self._config.heatmap_max),
            1.0,
        )
        self._heatmap_auto = self._config.heatmap_auto
        self._heatmap_manual_min = self._config.heatmap_min
        self._heatmap_manual_max = self._config.heatmap_max
        self._heatmap_auto_max = 100.0
        self._heatmap_auto_max_set_time = time.monotonic()
        self._frame_timestamps = deque(maxlen=64)
        # E 帧设备故障
        self._device_init_status: int = 0
        self._device_init_attempts: int = 0
        self._device_recover_attempts: int = 0
        self._device_total_errors: int = 0
        self._device_fault: bool = False
        self._rec_start_time: float = 0.0
        self._rec_points: int = 0
        self._app_mode: str = "live"
        self._calib_start_time: float = 0.0
        self._calib_eta_s: float = -1.0
        self._calib_fps_ema: float = 0.0
        # 基线作用域状态：当前来源（serial/demo）与设备标识
        self._current_scope: str = SCOPE_SERIAL
        self._current_device_id: str = ""
        self._pending_device_id: str = ""
        # 当前作用域基线是否已实际应用到 processor（设备标识不匹配时
        # 不应用，CSV 元数据也不得声称该基线来源）
        self._scope_baselines_applied: bool = False
        # 回放最近一行的 raw_valid（供当前帧有效性判断）
        self._last_replay_raw_valid: List[int] = [0] * 16
        # 数据通道 → 线圈标签（换板绑定后 CHxx 不再等于 data+1）；
        # 热力图/详情/曲线共用这一份
        self._channel_labels: Dict[int, str] = {}
        #: 当前生效绑定的可读名（详情/提示用）
        self._binding_label: str = ""

        # 启动即通过统一入口加载基线：来源 + 设备 + 布局 + 测量条件
        # 全部参与匹配（不再直接读串口槽，绕过布局/条件检查）。
        self._apply_scope_baselines()

        self._apply_processor_settings_from_config()

        # --- UI ---
        self._build_ui()
        self._build_dock()

        # --- 定时器 ---
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(REFRESH_INTERVAL_MS)
        self._refresh_timer.timeout.connect(self._on_refresh)
        self._refresh_timer.start()

        # 标定回调
        self._calib.progress.connect(self._on_calib_progress)
        self._calib.finished.connect(self._on_calib_finished)
        self._calib.cancelled.connect(self._on_calib_cancelled)
        self._calib.failed.connect(self._on_calib_failed)
        self._calib.quality_report.connect(self._on_calib_quality)

        # 回放源回调
        if self._replay is not None:
            self._replay.frame_ready.connect(self._on_replay_frame)
            self._replay.state_changed.connect(self._on_replay_state)
            self._replay.error_occurred.connect(
                lambda msg: self._log(f"[REPLAY ERR] {msg}")
            )

        # 全局快捷键
        self._install_global_shortcuts()

        # 初始刷新
        self._update_port_list()
        self._select_channel(self._selected_channel)
        # 应用配置的 channel_map（物理位置），否则热力图始终按默认
        # 行优先排布、详情面板"位置"恒为 —
        self._apply_channel_map_to_heatmap()
        self._update_all_displays()

        # v3 协议切换：旧 raw count 基线已作废时提示重新标定
        notice = self._config.baseline_notice
        if notice:
            self._log(f"[CONFIG] {notice}")
            if hasattr(self, "_message_bar"):
                self._message_bar.show_message(notice, "warning")

        # 通道物理映射迁移/回退提示（一次性，见 config_manager 迁移逻辑）
        map_notice = self._config.map_migration_notice
        if map_notice:
            self._log(f"[CONFIG] {map_notice}")
            if hasattr(self, "_message_bar"):
                self._message_bar.show_message(map_notice, "warning")

    # ==================================================================
    # UI 构建
    # ==================================================================
    def _build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        self._command_bar = self._build_command_bar()
        layout.addWidget(self._command_bar)
        self._command_bar.hide()
        self._global_status_bar = self._build_global_status_bar()
        layout.addWidget(self._global_status_bar)
        self._message_bar = MessageBar()
        layout.addWidget(self._message_bar)
        layout.addWidget(self._build_task_bar())
        self._op_bar_full = self._build_operation_bar()
        self._op_bar_inline = self._build_operation_bar_compact()
        layout.addWidget(self._op_bar_inline)
        layout.addWidget(self._op_bar_full)
        layout.addWidget(self._build_replay_bar())

        self._splitter = QSplitter(Qt.Horizontal)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.setHandleWidth(6)
        self._heatmap_workspace = self._build_left_panel()
        self._heatmap_workspace.setMinimumWidth(320)
        self._splitter.addWidget(self._heatmap_workspace)
        self._splitter.addWidget(self._build_workspace())
        self._splitter.setStretchFactor(0, 3)
        self._splitter.setStretchFactor(1, 2)
        layout.addWidget(self._splitter, 1)
        self._update_compact_mode()
        self.setCentralWidget(central)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("就绪")
        self._set_workspace_tool(None)

    def _update_compact_mode(self) -> None:
        if not hasattr(self, "_replay_controls"):
            return
        replay = self._app_mode == "replay"
        self._op_bar_full.setVisible(not replay and self._btn_connection_details.isChecked())
        self._op_bar_inline.setVisible(not replay)
        self._replay_controls.setVisible(replay)

    def _build_workspace(self) -> QWidget:
        workspace = QWidget()
        workspace.setMinimumWidth(300)
        layout = QVBoxLayout(workspace)
        layout.setContentsMargins(2, 0, 0, 0)
        layout.setSpacing(6)
        toolbar = QHBoxLayout()
        self._workspace_title = QLabel()
        self._workspace_title.setObjectName("AppName")
        toolbar.addWidget(self._workspace_title, 1)
        self._btn_expand_workspace = QPushButton("放大")
        self._btn_expand_workspace.setProperty("role", "secondary")
        self._btn_expand_workspace.setCheckable(True)
        self._btn_expand_workspace.toggled.connect(self._expand_workspace)
        toolbar.addWidget(self._btn_expand_workspace)
        close = QPushButton("收起")
        close.setProperty("role", "ghost")
        close.clicked.connect(lambda: self._set_workspace_tool(None))
        toolbar.addWidget(close)
        layout.addLayout(toolbar)

        self._workspace_stack = QStackedWidget()
        self._workspace_stack.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        layout.addWidget(self._workspace_stack, 1)

        board = QWidget()
        board_layout = QVBoxLayout(board)
        board_layout.setContentsMargins(0, 0, 0, 0)
        board_layout.addWidget(self._btn_plate, 0, Qt.AlignLeft)
        self._btn_plate.hide()
        board_layout.addWidget(self._legacy_calib_card)
        board_layout.addWidget(self._board_controls, 1)

        detail = self._build_right_panel()
        self._channel_panel.set_legacy_experiment(False)
        self._plot.set_legacy_experiment(False)
        self._compensation = CompensationPanel()
        self._compensation.profile_changed.connect(self._on_compensation_changed)
        self._inspection_panel = InspectionPanel()
        self._inspection = InspectionController(
            self._inspection_panel, self, self._show_inspection_grid, require_compensation=True,
        )
        self._workspace_pages = {}
        for name, widget in (
            ("compensation", self._compensation), ("inspection", self._inspection_panel),
            ("plate", board), ("channels", detail),
        ):
            if name == "plate":
                page = widget
            else:
                page = QScrollArea()
                page.setFrameShape(QFrame.NoFrame)
                page.setWidgetResizable(True)
                page.setWidget(widget)
            self._workspace_pages[name] = page
            self._workspace_stack.addWidget(page)
        self._workspace = workspace
        return workspace

    def _set_workspace_tool(self, tool) -> None:
        self._workspace_tool = tool
        self._btn_expand_workspace.setChecked(False)
        self._workspace.setVisible(tool is not None)
        self._heatmap_workspace.show()
        self._btn_channels.setChecked(tool == "channels")
        if tool is not None:
            titles = {
                "compensation": "第 1 步 · 公共补偿", "inspection": "第 2 步 · 检测",
                "plate": "板边覆盖实验", "channels": "通道详情和曲线",
            }
            self._workspace_title.setText(titles[tool])
            self._workspace_stack.setCurrentWidget(self._workspace_pages[tool])
            self._splitter.setSizes([54, 46])

    def _expand_workspace(self, expanded: bool) -> None:
        self._heatmap_workspace.setVisible(not expanded)
        self._btn_expand_workspace.setText("并排查看" if expanded else "放大")
        if not expanded:
            self._splitter.setSizes([54, 46])

    def _toggle_workspace_tool(self, tool) -> None:
        self._set_workspace_tool(None if self._workspace_tool == tool else tool)

    def _build_status_bar_strip(self) -> QWidget:
        """已迁移到 _build_global_status_bar；保留以避免破坏外部调用。"""
        return self._build_global_status_bar()

    def _build_command_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("CommandBar")
        h = QHBoxLayout()
        h.setContentsMargins(12, 6, 12, 6)
        h.setSpacing(12)
        style = self.style()

        self._lbl_app_name = QLabel("涡流阵列采集系统 v3")
        self._lbl_app_name.setObjectName("AppName")
        app_font = self._lbl_app_name.font()
        app_font.setPointSize(12)
        app_font.setBold(True)
        self._lbl_app_name.setFont(app_font)
        h.addWidget(self._lbl_app_name)

        self._btn_mode_live = QPushButton("实时")
        self._btn_mode_live.setCheckable(True)
        self._btn_mode_live.setChecked(True)
        self._btn_mode_live.setProperty("role", "secondary")
        self._btn_mode_live.setMaximumWidth(80)
        self._btn_mode_replay = QPushButton("回放")
        self._btn_mode_replay.setCheckable(True)
        self._btn_mode_replay.setProperty("role", "secondary")
        self._btn_mode_replay.setMaximumWidth(80)
        self._source_buttons = QButtonGroup(bar)
        self._source_buttons.addButton(self._btn_mode_live)
        self._source_buttons.addButton(self._btn_mode_replay)
        self._btn_mode_live.clicked.connect(lambda: self._select_data_source("live"))
        self._btn_mode_replay.clicked.connect(lambda: self._select_data_source("replay"))
        mode_holder = QFrame()
        mode_holder.setObjectName("ModeGroup")
        mg = QHBoxLayout(mode_holder)
        mg.setContentsMargins(0, 0, 0, 0)
        mg.setSpacing(0)
        mg.addWidget(self._btn_mode_live)
        mg.addWidget(self._btn_mode_replay)
        self._source_mode_group = mode_holder

        h.addSpacing(8)
        self._lbl_profile_tb = ElidingLabel(
            f"Profile: {self._config.adc_profile.display_name}"
        )
        self._lbl_profile_tb.setObjectName("Caption")
        h.addStretch(1)

        self._btn_settings = QPushButton()
        self._btn_settings.setIcon(style.standardIcon(QStyle.SP_FileDialogDetailedView))
        self._btn_settings.setText("设置")
        self._btn_settings.setProperty("role", "ghost")
        self._btn_settings.clicked.connect(self._on_open_settings)

        self._btn_diag = QPushButton()
        self._btn_diag.setIcon(style.standardIcon(QStyle.SP_MessageBoxInformation))
        self._btn_diag.setText("诊断")
        self._btn_diag.setCheckable(True)
        self._btn_diag.setProperty("role", "ghost")
        self._btn_diag.toggled.connect(self._on_toggle_diagnostics)
        bar.setLayout(h)
        return bar

    def _build_task_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("InspectionModeBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(2, 0, 2, 0)
        self._task_buttons = QButtonGroup(bar)
        self._task_button_by_mode = {}
        tasks = {"compensation": "1  公共补偿", "fatigue": "2  疲劳裂纹",
                 "weld": "2  焊缝检测", "plate": "板边实验"}
        for mode, label in tasks.items():
            button = QPushButton(label)
            button.setObjectName(f"inspectionMode_{mode}")
            button.setCheckable(True)
            button.setChecked(mode == self._task_mode)
            button.setProperty("role", "secondary")
            button.clicked.connect(lambda _checked, mode=mode: self._select_task(mode))
            self._task_buttons.addButton(button)
            self._task_button_by_mode[mode] = button
            row.addWidget(button)
        self._btn_channels = QPushButton("通道 / 曲线")
        self._btn_channels.setProperty("role", "ghost")
        self._btn_channels.setCheckable(True)
        self._btn_channels.clicked.connect(lambda: self._toggle_workspace_tool("channels"))
        row.addWidget(self._btn_channels)
        row.addStretch(1)
        row.addWidget(self._source_mode_group)
        row.addWidget(self._btn_settings)
        row.addWidget(self._btn_diag)
        return bar

    def _select_task(self, mode: str) -> None:
        if mode != "compensation" and self._compensation.is_capturing:
            self._task_button_by_mode[self._task_mode].setChecked(True)
            self._set_workspace_tool("compensation")
            self.statusBar().showMessage("请等待公共补偿采集完成，或先取消采集。", 6000)
            return
        if mode == "plate":
            self._on_array_analysis_clicked()
            return
        tool = "compensation" if mode == "compensation" else "inspection"
        self._sync_common_context()
        if mode in MODE_NAMES and self._compensation.active_profile is None:
            if self._task_mode == "plate":
                self._select_task("compensation")
            else:
                self._task_button_by_mode[self._task_mode].setChecked(True)
                self._set_workspace_tool("compensation")
            self.statusBar().showMessage("请先完成或加载当前工况的公共补偿标定。", 8000)
            return
        if mode == self._task_mode:
            self._toggle_workspace_tool(tool)
            return
        self._task_mode = mode
        self._task_button_by_mode[mode].setChecked(True)
        if self._array_analysis is not None:
            self._array_analysis.set_workspace_active(False)
            self._array_analysis.clear_frame()
        if self._calib.is_running():
            self._calib.cancel()
        self._board_controls.hide()
        self._legacy_calib_card.hide()
        self._legacy_range_controls.hide()
        self._lbl_array_coverage_status.hide()
        self._channel_panel.set_legacy_experiment(False)
        self._plot.set_legacy_experiment(False)
        self._btn_plate.hide()
        self._set_workspace_tool(tool)
        self._before_compensation.setVisible(mode == "compensation")
        self._workflow_status.show()
        self._inspection.reset_stream()
        if mode in MODE_NAMES:
            self._inspection.set_mode(mode)
        self._sync_inspection_context()
        self._render_compensation_grid()
        if mode in MODE_NAMES and self._latest_workflow_packet is not None:
            self._inspection.on_frame(*self._latest_workflow_packet)

    def _sync_active_task_context(self) -> None:
        if self._task_mode == "plate":
            self._sync_array_analysis_context()
        self._sync_inspection_context()

    def _workflow_context(self):
        live = self._app_mode == "live" and self._source_state in (SourceState.SERIAL_CONNECTED, SourceState.DEMO)
        if live:
            conditions = self._baseline_conditions()
            profile = conditions.pop("adc_profile")
            if not conditions["frequency"]:
                conditions.pop("frequency")
            unit = conditions["freq_unit"]
            conditions["freq_unit"] = {"hz": "Hz", "khz": "kHz", "mhz": "MHz", "ghz": "GHz"}.get(unit.lower(), unit)
            conditions["plate"] = str(self._config.experiment_params.get("plate") or "")
            conditions["enabled_channels"] = sorted(self._processor.enabled_channels)
            context = reference_context(
                self._heatmap.channel_map(), self._session_source_name(), profile,
                conditions, self._current_device_id or "",
            )
            return context, True
        if self._app_mode == "replay":
            return replay_context(self._replay), False
        return None, False

    def _sync_common_context(self):
        context, live = self._workflow_context()
        previous = self._compensation.active_profile
        self._compensation.set_context(context, can_capture=live)
        active = self._compensation.active_profile
        if previous is not active:
            self._latest_workflow_packet = None
            self._latest_compensated = None
            if self._task_mode != "plate":
                self._render_compensation_grid()
        return context, live

    def _sync_inspection_context(self):
        context, live = self._sync_common_context()
        if self._task_mode not in MODE_NAMES:
            return None
        self._inspection.set_context(
            context, can_capture=live, compensation=self._compensation.active_profile,
        )
        return self._inspection

    def _on_compensation_changed(self):
        self._latest_compensated = None
        if self._before_compensation.isChecked():
            self._before_compensation.blockSignals(True)
            self._before_compensation.setChecked(False)
            self._before_compensation.blockSignals(False)
        self._sync_inspection_context()
        if self._latest_workflow_packet is not None:
            packet = self._latest_workflow_packet
            self._latest_workflow_packet = None
            self._dispatch_workflow_frame(*packet, collect=False)
        else:
            self._render_compensation_grid()

    def _dispatch_workflow_frame(self, time_s, raw, position_mm=None, *, collect=True):
        inspection = self._sync_inspection_context()
        if collect:
            self._compensation.on_frame(raw)
        active = self._compensation.active_profile
        self._latest_workflow_packet = (time_s, raw, position_mm)
        self._latest_compensated = active.apply(raw) if active is not None else None
        if inspection is not None:
            inspection.on_frame(time_s, raw, position_mm)
        elif self._task_mode == "compensation":
            self._render_compensation_grid()

    def _render_compensation_grid(self, residual=None):
        if self._task_mode == "plate":
            return
        profile = self._compensation.active_profile
        if profile is None:
            self._write_workflow_grid(None, "compensated", "等待第 1 步公共补偿标定",
                                      "连接设备后采集完好参考，或加载同工况标定。")
            return

        caption = "公共补偿后响应（相对完好参考，mV）"
        status = ("耦合修正已启用" if profile.correction_mode == "coupling"
                  else f"公共补偿已启用 · 耦合标定 {profile.template_count}/16 格")
        values = self._latest_compensated
        display = "compensated"
        if self._task_mode == "compensation" and self._before_compensation.isChecked():
            raw = self._latest_workflow_packet[1] if self._latest_workflow_packet is not None else None
            values = raw - profile.baseline if raw is not None and np.isfinite(raw).all() else None
            display = "before_compensation"
            caption = "补偿前响应（相对同一完好参考，mV）"
        if values is None:
            status += " · 等待新数据"
        elif not np.isfinite(values).any():
            status += " · 本帧需要完整 16 路"
        assessment = self._task_mode in MODE_NAMES
        if assessment:
            if residual is not None:
                values, display = residual, "residual"
                caption = "补偿后良品残差（当前 − 良品，mV）"
            status = f"{status} · {self._inspection_panel.indicator.text()}"
        self._write_workflow_grid(values, display, caption, status)

    def _show_inspection_grid(self, residual):
        if self._task_mode in MODE_NAMES:
            self._render_compensation_grid(residual)

    def _write_workflow_grid(self, values, display, caption, status):
        self._heatmap.set_display_mode(display)
        self._legend.set_display_mode(display)
        self._heatmap_header.set_caption(caption)
        if self._workflow_status.text() != status:
            self._workflow_status.setText(status)
        grid = None
        if values is not None:
            grid = [[None] * 4 for _ in range(4)]
            for channel, (row, col) in self._heatmap.channel_map().items():
                grid[row][col] = float(values[channel])
        if self._task_mode == "compensation" and self._latest_workflow_packet is not None:
            profile = self._compensation.active_profile
            if profile is not None:
                raw = self._latest_workflow_packet[1]
                if np.isfinite(raw).all():
                    # Same color scale for the before/after comparison.
                    values = np.r_[raw - profile.baseline, self._latest_compensated]
        peak = max((abs(v) for v in values if math.isfinite(v)), default=0.0) if values is not None else 0.0
        scale = max(0.125, float(peak))
        self._heatmap.set_range(scale)
        self._legend.set_range(scale)
        self._heatmap.set_corrected_values(grid)

    def _on_array_analysis_clicked(self) -> None:
        if self._task_mode == "plate":
            self._toggle_workspace_tool("plate")
            return
        self._task_mode = "plate"
        self._inspection.reset_stream()
        self._before_compensation.hide()
        self._workflow_status.hide()
        self._set_workspace_tool("plate")
        self._task_button_by_mode["plate"].setChecked(True)
        if self._array_analysis is None:
            self._array_analysis = ArrayAnalysisDialog(self._board_controls, embedded=True)
            self._board_controls.setWidget(self._array_analysis)
            self._array_analysis.display_result_changed.connect(self._show_array_coverage)
            self._array_analysis.calibration_applied.connect(self._on_array_calibration_applied)
        self._legacy_range_controls.show()
        self._btn_plate.setVisible(self._source_state == SourceState.DEMO)
        self._on_array_heatmap_mode_changed(self._array_heatmap_mode.currentIndex())
        dialog = self._sync_array_analysis_context()
        if self._last_replay_task_row is not None and self._app_mode == "replay":
            self._show_replay_task_frame(self._last_replay_task_row)
            if self._replay is not None and self._replay.state in ("paused", "ended"):
                self._array_analysis.hold_frame()
        elif (
            dialog is not None
            and self._app_mode == "live"
            and self._source_state in (SourceState.SERIAL_CONNECTED, SourceState.DEMO)
            and self._last_dframe is not None
            and self._last_dframe.scan_number == self._array_last_live_scan
            and self._segment_last_dframe_time > 0
            and time.monotonic() - self._segment_last_dframe_time <= DATA_TIMEOUT_S
        ):
            frame = self._last_dframe
            self._send_array_analysis_frame(frame.samples_mv, frame.scan_status, frame.valid_mask)

    def _sync_array_analysis_context(self) -> Optional[ArrayAnalysisDialog]:
        dialog = self._array_analysis
        if dialog is None:
            return None

        channel_map = self._heatmap.channel_map()
        if self._app_mode == "replay":
            replay = self._replay
            if replay is None or not replay.total_rows:
                dialog.set_context(None, channel_map, "请先打开回放 CSV。")
                return None
            if (replay.layout_conflict or replay.profile_conflict or replay.source_conflict
                    or replay.inspection_conditions_conflict):
                dialog.set_context(None, channel_map, "记录混用了采集条件，请选用同一工况的 CSV。")
                return None
            conditions = replay.inspection_conditions
            if not conditions.get("frequency"):
                conditions.pop("frequency", None)
            conditions.update(
                adc_profile=replay.recorded_profile_name,
                recorded_source_scope=replay.recorded_source_scope,
                load_revision=self._array_replay_revision,
            )
            key = ("replay", replay.csv_path, tuple(sorted(conditions.items())))
            description = "CSV 回放 · ADC 原始毫伏值"
            if replay.recorded_source_scope == SCOPE_DEMO:
                description += " · 演示记录"
            if replay.recorded_channel_map is None:
                description += " · 文件缺少布局，按当前阵列排列"
        elif self._source_state in (SourceState.SERIAL_CONNECTED, SourceState.DEMO):
            source = self._session_source_name()
            conditions = self._baseline_conditions()
            conditions["plate"] = str(self._config.experiment_params.get("plate") or "")
            if not self._current_device_id and source in (SCOPE_SERIAL, SCOPE_TCP):
                conditions["connection"] = f"{self._session_id}/{self._segment_index}"
            key = (source, self._current_device_id, tuple(sorted(conditions.items())))
            description = {
                SCOPE_SERIAL: "串口实时 · ADC 原始毫伏值",
                SCOPE_TCP: "TCP 实时 · ADC 原始毫伏值",
                SCOPE_DEMO: "演示数据 · ADC 毫伏值",
            }[source]
        else:
            dialog.set_context(None, channel_map, "等待连接设备或打开回放。")
            return None

        dialog.set_context(key, channel_map, description)
        return dialog

    def _send_array_analysis_frame(self, values, status: int, valid_mask: int) -> None:
        if self._task_mode != "plate":
            return
        dialog = self._sync_array_analysis_context()
        if dialog is None:
            return
        usable = (
            status == 0
            and valid_mask == (1 << MAX_CHANNEL_COUNT) - 1
            and len(values) == MAX_CHANNEL_COUNT
            and all(value is not None and MV_MIN < value < MV_MAX for value in values)
        )
        dialog.on_frame(values if usable else None)

    def _clear_array_analysis_frame(self) -> None:
        self._array_last_live_scan = None
        self._last_replay_task_row = None
        self._latest_workflow_packet = None
        self._latest_compensated = None
        if hasattr(self, "_compensation"):
            self._compensation.reset_stream()
        if self._array_analysis is not None:
            self._array_analysis.clear_frame()
        if hasattr(self, "_inspection"):
            self._inspection.reset_stream()
            self._render_compensation_grid()

    def _on_array_calibration_applied(self) -> None:
        if self._task_mode == "plate":
            self._array_heatmap_mode.setCurrentIndex(self._array_heatmap_mode.findData("coverage"))

    def _on_array_heatmap_mode_changed(self, _index: int) -> None:
        if self._task_mode != "plate":
            return
        mode = self._array_heatmap_mode.currentData()
        independent = mode == "attenuation"
        self._board_controls.setVisible(not independent)
        self._legacy_calib_card.setVisible(independent)
        self._channel_panel.set_legacy_experiment(independent)
        self._plot.set_legacy_experiment(independent)
        self._array_analysis.set_workspace_active(not independent)
        self._heatmap.set_display_mode(mode)
        self._legend.set_display_mode(mode)
        captions = {
            "coverage": "直板边几何覆盖率估计（%）",
            "corrected": "空场衰减扣除公共响应（mV）",
            "board_raw": "板边标定空场 − 当前采样（mV）",
            "attenuation": "独立无钢板基线 − 当前滤波（mV）",
        }
        self._heatmap_header.set_caption(captions[mode])
        for control in (self._lbl_range, self._chk_auto, self._lbl_manual_range, self._sp_range):
            control.setVisible(mode != "coverage")
        self._lbl_array_coverage_status.setVisible(not independent)
        self._apply_range_to_ui()
        if independent:
            self._array_analysis.clear_frame()
        else:
            if self._calib.is_running():
                self._calib.cancel()
            self._sync_array_analysis_context()
            self._show_array_coverage(self._array_analysis.coverage_snapshot())

    def _show_array_coverage(self, snapshot: dict) -> None:
        if self._task_mode != "plate":
            return
        mode = self._array_heatmap_mode.currentData()
        if mode == "attenuation":
            return
        matching_map = snapshot.get("channel_map") == self._heatmap.channel_map()
        usable = matching_map and (snapshot.get("fresh") or snapshot.get("state") == "held")
        status = snapshot.get("status") or "等待与标定匹配的完整 16 路数据。"
        if mode == "coverage":
            self._heatmap.set_coverage_values(snapshot.get("coverage") if usable else None)
            self._lbl_array_coverage_status.hide()
            return
        if mode == "board_raw":
            values = snapshot.get("attenuation") if matching_map else None
            status = "使用当前板边标定的空场参考。" if values is not None else status
        else:
            values = snapshot.get("corrected") if usable else None
            if usable and snapshot.get("range") is not None:
                common = snapshot.get("common_mv")
                count = snapshot["reference_count"]
                if common is None:
                    status = f"只有 {count} 个明确未覆盖的参考格，公共补偿需要至少 4 个。"
                else:
                    status = f"已扣除公共响应 {common:+.2f} mV，使用 {count} 个未覆盖参考格。"
        self._heatmap.set_corrected_values(values)
        if self._heatmap_auto and values is not None:
            peak = max((abs(float(value)) for row in values for value in row
                        if math.isfinite(value)), default=0.0)
            scale = max(1.0, peak)
            self._heatmap.set_range(scale)
            self._legend.set_range(scale)
            self._lbl_range.setText(f"色阶 ±{scale:.1f}")
        self._lbl_array_coverage_status.show()
        if self._lbl_array_coverage_status.text() != status:
            self._lbl_array_coverage_status.setText(status)
            self._lbl_array_coverage_status.setToolTip(status)

    def _select_data_source(self, mode: str) -> None:
        if mode == self._app_mode:
            return
        if mode == "replay" and not self._confirm_switch_to_replay():
            self._btn_mode_live.setChecked(True)
            return
        self._set_app_mode(mode)

    def _set_app_mode(self, mode: str) -> None:
        self._clear_array_analysis_frame()
        self._app_mode = mode
        replay = mode == "replay"
        (self._btn_mode_replay if replay else self._btn_mode_live).setChecked(True)
        self._update_compact_mode()
        for attr in (
            "_btn_connect", "_btn_demo", "_btn_calib", "_btn_rec",
            "_btn_connect_inline", "_btn_demo_inline", "_btn_rec_inline",
        ):
            w = getattr(self, attr, None)
            if w is not None:
                w.setEnabled(not replay)
        if replay:
            self._stop_all_sources()
            if self._calib.is_running():
                self._calib.cancel()
            if self._recorder.is_recording:
                self._btn_rec.setChecked(False)
            # 进入回放：复位数据源状态与实时会话计数器——
            # 否则 _on_refresh 会按滞留的 SERIAL_CONNECTED 重新启用
            # "开始标定"（标定永远收不到帧、误报断流），且
            # "有效/无效/丢帧"跨会话混杂
            self._set_source_state(SourceState.REPLAY)
            self._frame_count = 0
            self._error_count = 0
            self._dropped_frames = 0
            self._frame_timestamps.clear()
            # 清理实时会话数据（滤波/帧有效性/曲线），
            # 回放帧将逐行重写状态（含回放派生量覆盖）
            self._processor.clear_session_data()
            self._last_dframe = None
            self._last_replay_raw_valid = [0] * 16
            if hasattr(self, "_plot"):
                self._plot.clear()
            self._clear_plot_baselines()
            # 重进回放且文件仍加载：恢复该文件的"记录时布局"显示
            # （上次离开回放时已恢复硬件布局），否则空间排布会静默
            # 变成当前接线布局而非记录时布局
            if self._replay is not None and self._replay.total_rows > 0:
                self._apply_replay_recorded_map()
                self._apply_replay_experiment_context()
                self._lbl_state.setText("回放（已加载，待播放）")
            else:
                self._lbl_state.setText("回放（待加载 CSV）")
            set_status_role(self._lbl_state, "neutral")
        else:
            if self._replay is not None and self._replay.is_running():
                self._replay.stop()
            if self._source_state == SourceState.REPLAY:
                self._set_source_state(SourceState.DISCONNECTED)
            # 离开回放：清除回放写入的状态与派生覆盖，
            # 恢复当前作用域的硬件基线（回放基线绝不写成硬件基线）
            self._processor.clear_session_data()
            # 恢复实时配置的分压比（回放的记录时 Profile 只对回放生效）
            self._processor.clear_detector_divider_override()
            self._last_replay_raw_valid = [0] * 16
            if hasattr(self, "_plot"):
                self._plot.clear()
            # 恢复当前硬件布局：回放的"记录时布局"只是临时显示，
            # 绝不带进实时模式（实时配置从未被它修改）
            if self._replay_using_recorded_map:
                self._replay_using_recorded_map = False
                self._apply_channel_map_to_heatmap()
                self._log("[REPLAY] 已恢复当前硬件布局显示")
                # 清除"按记录时布局显示"标注——回到实时模式后该
                # 现在时断言已为假，不得残留
                if hasattr(self, "_message_bar"):
                    self._message_bar.clear()
            self._apply_scope_baselines()
            # 立即按当前源状态校正标定按钮（不等下一个刷新周期）
            self._btn_calib.setEnabled(
                self._source_state
                in (SourceState.SERIAL_CONNECTED, SourceState.DEMO)
            )
        self._sync_active_task_context()

    def _confirm_switch_to_replay(self) -> bool:
        board_capturing = self._array_analysis is not None and self._array_analysis.is_capturing()
        if (self._recorder.is_recording or self._calib.is_running() or self._inspection.capture
                or self._compensation.is_capturing or board_capturing):
            r = QMessageBox.question(
                self, "切换到回放",
                "当前正在记录或标定，切到回放会停止这些操作。继续？",
            )
            if r != QMessageBox.Yes:
                return False
        return True

    def _on_toggle_diagnostics(self, checked: bool) -> None:
        if hasattr(self, "_dock"):
            self._dock.setVisible(checked)

    def _build_global_status_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("GlobalStatusBar")
        h = QHBoxLayout()
        h.setContentsMargins(12, 6, 12, 6)
        h.setSpacing(14)

        self._lbl_state = QLabel("未连接")
        self._lbl_state.setObjectName("StatusBadge")
        set_status_role(self._lbl_state, "neutral")
        self._lbl_state.setMinimumWidth(110)
        h.addWidget(QLabel("数据源:"))
        h.addWidget(self._lbl_state)

        self._lbl_fresh = QLabel("—")
        self._lbl_fresh.setObjectName("StatusBadge")
        set_status_role(self._lbl_fresh, "neutral")
        self._lbl_fresh.setMinimumWidth(100)
        h.addWidget(QLabel("新鲜度:"))
        h.addWidget(self._lbl_fresh)

        self._lbl_init = QLabel("INIT=0")
        self._lbl_init.setObjectName("StatusBadge")
        set_status_role(self._lbl_init, "ok")
        h.addWidget(self._lbl_init)

        h.addSpacing(8)
        self._lbl_fps = QLabel("— fps")
        self._lbl_valid = QLabel("有效 0")
        self._lbl_invalid = QLabel("无效 0")
        self._lbl_dropped = QLabel("扫描缺口 0")
        self._lbl_dropped.setToolTip(
            "本连接段内扫描号连续性：缺口只表示这些扫描没有被观测到"
            "（触发式采集/跳过/链路中断/设备重启均可能），\n"
            "不能等同于无线丢包率。回绕、重启、重复、倒序分别单独统计。"
        )
        self._lbl_age = QLabel("最新 0.0s")
        for w in (self._lbl_fps, self._lbl_valid,
                  self._lbl_invalid, self._lbl_dropped, self._lbl_age):
            w.setProperty("role", "numeric")
        for w in (self._lbl_fps, self._lbl_valid,
                  self._lbl_invalid, self._lbl_dropped, self._lbl_age):
            h.addWidget(w)

        h.addStretch(1)
        self._badge_rec = StatusBadge("记录: 停止", "neutral")
        h.addWidget(self._badge_rec)
        bar.setLayout(h)
        return bar

    def _build_operation_bar_compact(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("CompactOpBar")
        h = QHBoxLayout()
        h.setContentsMargins(8, 4, 8, 4)
        h.setSpacing(6)

        self._btn_connection_details = QPushButton("连接设置")
        self._btn_connection_details.setCheckable(True)
        self._btn_connection_details.setProperty("role", "ghost")
        self._btn_connection_details.toggled.connect(self._update_compact_mode)
        h.addWidget(self._btn_connection_details)
        self._cmb_port_inline = QComboBox()
        self._cmb_port_inline.setMinimumWidth(120)
        for dev, desc in list_serial_ports():
            self._cmb_port_inline.addItem(f"{dev} — {desc}", dev)
        h.addWidget(self._cmb_port_inline, 1)

        # TCP 模式下用只读标签替代串口下拉（避免"端口"语义混淆）
        self._lbl_tcp_inline = QLabel("")
        self._lbl_tcp_inline.setObjectName("Caption")
        self._lbl_tcp_inline.setVisible(False)
        h.addWidget(self._lbl_tcp_inline, 1)

        self._btn_connect_inline = QPushButton("连接")
        self._btn_connect_inline.setProperty("role", "primary")
        self._btn_connect_inline.setMaximumWidth(72)
        self._btn_connect_inline.clicked.connect(self._on_connect_clicked)
        h.addWidget(self._btn_connect_inline)
        self._btn_disconnect_inline = QPushButton("断开")
        self._btn_disconnect_inline.setProperty("role", "secondary")
        self._btn_disconnect_inline.clicked.connect(self._on_disconnect_clicked)
        h.addWidget(self._btn_disconnect_inline)

        self._btn_demo_inline = QPushButton("演示")
        self._btn_demo_inline.setProperty("role", "secondary")
        self._btn_demo_inline.setMaximumWidth(72)
        self._btn_demo_inline.clicked.connect(self._on_demo_clicked)
        h.addWidget(self._btn_demo_inline)

        self._btn_rec_inline = QPushButton("记录")
        self._btn_rec_inline.setProperty("role", "primary")
        self._btn_rec_inline.setCheckable(True)
        self._btn_rec_inline.setMaximumWidth(72)
        self._btn_rec_inline.toggled.connect(self._on_rec_toggled)
        h.addWidget(self._btn_rec_inline)
        self._lbl_profile_tb.setMaximumWidth(340)
        h.addWidget(self._lbl_profile_tb, 1)

        bar.setLayout(h)
        return bar

    def _install_global_shortcuts(self) -> None:
        from PyQt5.QtWidgets import QShortcut

        def _make(seq, slot):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ApplicationShortcut)
            sc.activated.connect(slot)
            return sc

        _make("F5", self._on_shortcut_refresh)
        _make("Ctrl+,", self._on_open_settings)
        _make("Space", self._on_shortcut_pause)
        _make("Ctrl+Shift+R", self._on_shortcut_record_toggle)
        _make("Ctrl+O", self._on_shortcut_open_csv)
        _make("Escape", self._on_shortcut_cancel)

    def _on_shortcut_refresh(self) -> None:
        if self._transport != TRANSPORT_SERIAL:
            return
        if self._source_state in (SourceState.DISCONNECTED, SourceState.CONNECTING):
            self._update_port_list()

    def _on_shortcut_pause(self) -> None:
        self._plot.set_paused(not self._plot.is_paused())

    def _on_shortcut_record_toggle(self) -> None:
        self._btn_rec.toggle()

    def _on_shortcut_open_csv(self) -> None:
        # 回放模式下 Ctrl+O 打开回放 CSV；实时模式下打开保存目录
        if self._app_mode == "replay":
            self._on_replay_open()
            return
        d = QFileDialog.getExistingDirectory(
            self, "选择保存目录", self._config.last_csv_dir or ""
        )
        if d:
            self._config.last_csv_dir = d
            self._config.save()
            if hasattr(self, "_lbl_rec_file"):
                self._lbl_rec_file.set_full_text(d)

    def _on_shortcut_cancel(self) -> None:
        if self._compensation.is_capturing:
            self._compensation.reset_stream()
            return
        if self._calib.is_running():
            self._on_calib_cancel()
            return
        if hasattr(self, "_message_bar") and self._message_bar.isVisible():
            self._message_bar.clear()
            return

    def _channel_label(self, channel: int) -> str:
        """数据通道的显示标签（缺省 CH(通道+1)）。"""
        return self._channel_labels.get(channel, f"CH{channel + 1:02d}")

    def _apply_channel_map_to_heatmap(self) -> None:
        if not hasattr(self, "_heatmap"):
            return
        try:
            norm = normalize_channel_map(self._config.channel_map)
        except Exception:
            norm = {}
        if not is_valid_map(norm):
            # 兜底统一引用硬件默认布局（config getter 已保证合法，
            # 正常不会走到；禁止再写一行优先兜底）
            norm = dict(HARDWARE_CHANNEL_MAP)
        # 标签由"数据通道→坐标"映射反推：命中布局预设用绑定标签
        # （有源板 CH04 可能读 data 11），否则按历史约定 CH(data+1)
        # （例如旧 CSV 记录时就是这种标签）。
        labels = labels_for_map(norm)
        self._channel_labels = dict(labels)
        # 绑定的可读名：优先按映射反查（回放时也要按记录时的表标注），
        # 无法识别才回落到配置值
        detected = binding_for_map(norm)
        self._binding_label = binding_name(
            detected or self._config.cell_binding
        )
        self._heatmap.set_channel_map(norm, labels=labels)
        if hasattr(self, "_inspection_panel"):
            self._inspection_panel.set_channel_labels(labels)
            self._compensation.set_channels(norm, labels)
        if hasattr(self, "_plot"):
            self._plot.set_channel_labels(labels)
        self._sync_selected_channel_position()

    def _sync_selected_channel_position(self) -> None:
        """把当前选中通道的物理位置标签同步到详情面板。"""
        if not hasattr(self, "_heatmap"):
            return
        pos = self._heatmap.channel_position(self._selected_channel)
        if pos is not None and hasattr(self, "_channel_panel"):
            self._channel_panel.set_position(f"R{pos[0] + 1}C{pos[1] + 1}")

    def _build_operation_bar(self) -> QWidget:
        """操作带：端口 + 波特率 + 连接 + 演示 + 断开 + 记录 + 选目录。"""
        bar = QFrame()
        bar.setObjectName("SurfaceCard")
        h = QHBoxLayout()
        h.setContentsMargins(12, 6, 12, 6)
        h.setSpacing(8)

        style = self.style()

        h.addWidget(QLabel("数据源:"))
        self._cmb_transport = QComboBox()
        self._cmb_transport.addItem("串口", TRANSPORT_SERIAL)
        self._cmb_transport.addItem("TCP 网络", TRANSPORT_TCP)
        idx = self._cmb_transport.findData(self._transport)
        self._cmb_transport.setCurrentIndex(idx if idx >= 0 else 0)
        self._cmb_transport.setMinimumWidth(96)
        self._cmb_transport.currentIndexChanged.connect(
            self._on_transport_changed
        )
        h.addWidget(self._cmb_transport)

        # ---- 串口参数组 ----
        self._serial_group = QWidget()
        sg = QHBoxLayout(self._serial_group)
        sg.setContentsMargins(0, 0, 0, 0)
        sg.setSpacing(8)
        sg.addWidget(QLabel("端口:"))
        self._cmb_port = QComboBox()
        self._cmb_port.setMinimumWidth(180)
        sg.addWidget(self._cmb_port, 1)

        self._btn_refresh = QPushButton()
        self._btn_refresh.setIcon(style.standardIcon(QStyle.SP_BrowserReload))
        self._btn_refresh.setText("刷新")
        self._btn_refresh.setAccessibleName("刷新串口列表")
        self._btn_refresh.clicked.connect(self._update_port_list)
        sg.addWidget(self._btn_refresh)

        sg.addSpacing(4)
        sg.addWidget(QLabel("波特率:"))
        self._cmb_baud = QComboBox()
        self._cmb_baud.addItems(["115200", "230400", "460800", "921600"])
        self._cmb_baud.setCurrentText("115200")
        sg.addWidget(self._cmb_baud)
        h.addWidget(self._serial_group, 1)

        # ---- TCP 监听参数组（上位机监听，Hi3863 作为客户端连入）----
        # 监听地址 = 本机绑定的网卡地址；设备地址由客户端连入时确定，
        # 两者严格区分，绝不把对端 IP 当作设备身份。
        self._tcp_group = QWidget()
        tcp_layout = QVBoxLayout(self._tcp_group)
        tcp_layout.setContentsMargins(0, 0, 0, 0)
        tcp_layout.setSpacing(4)
        tg = QHBoxLayout()
        tg.setSpacing(8)
        tcp_layout.addLayout(tg)
        tg.addWidget(QLabel("监听地址:"))
        self._edt_listen_host = QLineEdit(self._config.tcp_listen_host)
        self._edt_listen_host.setMinimumWidth(110)
        self._edt_listen_host.setMaximumWidth(150)
        self._edt_listen_host.setToolTip(
            "本机监听地址（0.0.0.0 = 全部网卡，推荐）；"
            "这是 PC 的地址，不是采集设备的地址"
        )
        tg.addWidget(self._edt_listen_host)

        tg.addWidget(QLabel("端口:"))
        self._sp_listen_port = QSpinBox()
        self._sp_listen_port.setRange(1, 65535)
        self._sp_listen_port.setValue(self._config.tcp_listen_port)
        self._sp_listen_port.setMaximumWidth(90)
        tg.addWidget(self._sp_listen_port)

        tg.addStretch(1)
        tg = QHBoxLayout()
        tg.setSpacing(8)
        tcp_layout.addLayout(tg)
        tg.addWidget(QLabel("设备编号:"))
        self._edt_device_id = QLineEdit(self._config.device_id)
        self._edt_device_id.setMinimumWidth(100)
        self._edt_device_id.setMaximumWidth(160)
        self._edt_device_id.setPlaceholderText("如 EDDY-01")
        self._edt_device_id.setToolTip(
            "采集设备的持久化编号，用于基线匹配；\n"
            "不使用设备 IP / 端口作为身份。"
        )
        self._edt_device_id.editingFinished.connect(
            self._on_device_id_edited
        )
        tg.addWidget(self._edt_device_id)

        # TCP keepalive：只做链路探活（发现对端掉电导致的半开连接），
        # 不产生任何业务数据，也不会因短暂无数据而断开设备
        tg.addSpacing(6)
        self._chk_keepalive = QCheckBox("保活")
        self._chk_keepalive.setChecked(self._config.tcp_keepalive_enabled)
        self._chk_keepalive.setToolTip(
            "开启 TCP keepalive：对端突然掉电时能较快发现半开连接。\n"
            "仅链路层探活，不会因为正常短暂无数据而断开设备。"
        )
        tg.addWidget(self._chk_keepalive)
        self._sp_keepalive = QSpinBox()
        self._sp_keepalive.setRange(10, 600)
        self._sp_keepalive.setSingleStep(10)
        self._sp_keepalive.setValue(self._config.tcp_keepalive_idle_s)
        self._sp_keepalive.setSuffix(" s")
        self._sp_keepalive.setMaximumWidth(84)
        self._sp_keepalive.setToolTip("空闲多少秒后开始 keepalive 探活")
        tg.addWidget(self._sp_keepalive)

        # 数据超时可能只是"对端掉电但连接未正常关闭"（半开）。
        # 提供显式清除操作：断开当前设备但继续监听，绝不自动踢人。
        self._btn_kick = QPushButton("清除设备")
        self._btn_kick.setProperty("role", "secondary")
        self._btn_kick.setEnabled(False)
        self._btn_kick.setToolTip(
            "断开当前设备连接但继续监听（用于客户端掉电/半开连接）。\n"
            "不会自动触发，也不会让第二台设备顶替第一台。"
        )
        self._btn_kick.clicked.connect(self._on_kick_client_clicked)
        tg.addWidget(self._btn_kick)
        tg.addStretch(1)
        h.addWidget(self._tcp_group, 1)
        h.addStretch(1)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self._btn_connect = QPushButton()
        self._btn_connect.setIcon(style.standardIcon(QStyle.SP_DialogYesButton))
        self._btn_connect.setText("连接")
        self._btn_connect.setProperty("role", "primary")
        self._btn_connect.setAccessibleName("连接串口")
        self._btn_connect.clicked.connect(self._on_connect_clicked)
        actions.addWidget(self._btn_connect)

        self._btn_disconnect = QPushButton()
        self._btn_disconnect.setIcon(
            style.standardIcon(QStyle.SP_DialogNoButton)
        )
        self._btn_disconnect.setText("断开")
        self._btn_disconnect.setProperty("role", "secondary")
        self._btn_disconnect.setAccessibleName("断开串口")
        self._btn_disconnect.clicked.connect(self._on_disconnect_clicked)
        actions.addWidget(self._btn_disconnect)

        self._btn_demo = QPushButton()
        self._btn_demo.setIcon(style.standardIcon(QStyle.SP_MediaPlay))
        self._btn_demo.setText("演示模式")
        self._btn_demo.setProperty("role", "secondary")
        self._btn_demo.setAccessibleName("开启演示模式")
        self._btn_demo.clicked.connect(self._on_demo_clicked)
        actions.addWidget(self._btn_demo)

        # 演示专用：模拟"放入/移除钢板"（仅演示模式可用；模拟数据，
        # 不代表真实串口连接）。默认无钢板，可先演示真实标定顺序。
        self._btn_plate = QPushButton()
        self._btn_plate.setIcon(style.standardIcon(QStyle.SP_ArrowDown))
        self._btn_plate.setText("放入钢板")
        self._btn_plate.setCheckable(True)
        self._btn_plate.setEnabled(False)
        self._btn_plate.setProperty("role", "secondary")
        self._btn_plate.setAccessibleName("模拟放入或移除钢板")
        self._btn_plate.setToolTip(
            "仅演示模式：模拟放入钢板（检波电压下降、衰减量增大）；\n"
            "再次点击移除钢板，信号回到基线附近。模拟数据。"
        )
        self._btn_plate.toggled.connect(self._on_plate_toggled)

        actions.addSpacing(12)
        self._btn_rec = QPushButton()
        self._btn_rec.setIcon(style.standardIcon(QStyle.SP_DialogSaveButton))
        self._btn_rec.setText("开始记录")
        self._btn_rec.setCheckable(True)
        self._btn_rec.setProperty("role", "primary")
        self._btn_rec.setAccessibleName("开始或停止记录")
        self._btn_rec.toggled.connect(self._on_rec_toggled)
        actions.addWidget(self._btn_rec)

        self._btn_choose_dir = QPushButton()
        self._btn_choose_dir.setIcon(
            style.standardIcon(QStyle.SP_DirOpenIcon)
        )
        self._btn_choose_dir.setText("选目录")
        self._btn_choose_dir.setProperty("role", "ghost")
        self._btn_choose_dir.clicked.connect(self._on_choose_dir)
        actions.addWidget(self._btn_choose_dir)

        self._lbl_rec_file = ElidingLabel("未选择")
        self._lbl_rec_file.setMinimumWidth(80)
        actions.addWidget(self._lbl_rec_file, 1)

        self._lbl_rec = QLabel("停止")
        actions.addWidget(self._lbl_rec)

        layout = QVBoxLayout(bar)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(4)
        h.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(h)
        layout.addLayout(actions)
        # 显式设置 Tab 顺序：端口 → 刷新 → 波特率 → 连接 → 断开 → 演示 → 记录
        self.setTabOrder(self._cmb_port, self._btn_refresh)
        self.setTabOrder(self._btn_refresh, self._cmb_baud)
        self.setTabOrder(self._cmb_baud, self._btn_connect)
        self.setTabOrder(self._btn_connect, self._btn_disconnect)
        self.setTabOrder(self._btn_disconnect, self._btn_demo)
        self.setTabOrder(self._btn_demo, self._btn_rec)
        self.setTabOrder(self._btn_rec, self._btn_choose_dir)
        self._update_transport_ui()
        return bar

    # ==================================================================
    # 传输方式（串口 / TCP 网络）
    # ==================================================================
    def _update_transport_ui(self) -> None:
        """按当前传输方式切换参数区与按钮文案。"""
        is_tcp = self._transport == TRANSPORT_TCP
        group_serial = getattr(self, "_serial_group", None)
        group_tcp = getattr(self, "_tcp_group", None)
        if group_serial is not None:
            group_serial.setVisible(not is_tcp)
        if group_tcp is not None:
            group_tcp.setVisible(is_tcp)

        endpoint = (
            f"监听 {self._config.tcp_listen_host}:"
            f"{self._config.tcp_listen_port}"
            if is_tcp else ""
        )
        cmb_inline = getattr(self, "_cmb_port_inline", None)
        if cmb_inline is not None:
            cmb_inline.setVisible(not is_tcp)
        lbl_inline = getattr(self, "_lbl_tcp_inline", None)
        if lbl_inline is not None:
            lbl_inline.setText(endpoint)
            lbl_inline.setVisible(is_tcp)

        for attr, text in (
            ("_btn_connect", "开始监听" if is_tcp else "连接"),
            ("_btn_connect_inline", "监听" if is_tcp else "连接"),
        ):
            btn = getattr(self, attr, None)
            if btn is not None:
                btn.setText(text)
                btn.setAccessibleName(
                    "开始 TCP 监听，等待采集设备连入" if is_tcp else "连接串口"
                )
        btn_refresh = getattr(self, "_btn_refresh", None)
        if btn_refresh is not None:
            btn_refresh.setEnabled(not is_tcp)
        btn_disc = getattr(self, "_btn_disconnect", None)
        if btn_disc is not None:
            btn_disc.setAccessibleName(
                "停止 TCP 监听" if is_tcp else "断开串口"
            )
        self._set_identity_editable()
        self._refresh_kick_enabled()

    def _refresh_kick_enabled(self) -> None:
        btn = getattr(self, "_btn_kick", None)
        if btn is None:
            return
        btn.setEnabled(
            self._transport == TRANSPORT_TCP
            and self._source_state == SourceState.SERIAL_CONNECTED
        )

    def _on_kick_client_clicked(self) -> None:
        """清除当前设备连接并继续监听（不发送任何业务数据）。"""
        if self._tcp is None:
            self.statusBar().showMessage("当前未在监听")
            return
        if self._tcp.disconnect_client():
            self._log("[TCP] 已请求清除当前设备连接（继续监听）")
            self.statusBar().showMessage("已清除当前设备连接，继续监听")
        else:
            self.statusBar().showMessage("当前没有已连接的设备")

    def _on_transport_changed(self, _index: int) -> None:
        """切换串口 / TCP：先停掉当前数据源，绝不让两种来源混在一个会话。"""
        combo = getattr(self, "_cmb_transport", None)
        transport = (
            combo.currentData() if combo is not None else TRANSPORT_SERIAL
        )
        transport = transport or TRANSPORT_SERIAL
        if transport == self._transport:
            return
        was_active = self._source_state in (
            SourceState.SERIAL_CONNECTED,
            SourceState.LISTENING,
            SourceState.CONNECTING,
        )
        self._stop_all_sources()
        if self._calib.is_running():
            self._calib.cancel()
        self._set_source_state(SourceState.DISCONNECTED)
        self._transport = transport
        self._config.transport = transport
        self._config.save()
        self._update_transport_ui()
        self._log(
            f"[SOURCE] 数据传输方式切换为 "
            f"{'TCP 网络' if transport == TRANSPORT_TCP else '串口'}"
            + ("（原数据源已停止）" if was_active else "")
        )

    def _identity_edit_locked(self) -> bool:
        """监听/连接期间锁定设备编号编辑（避免同一会话混用两个身份）。"""
        return self._source_state in (
            SourceState.LISTENING,
            SourceState.CONNECTING,
            SourceState.SERIAL_CONNECTED,
        )

    def _set_identity_editable(self) -> None:
        if not hasattr(self, "_edt_device_id"):
            return
        locked = self._identity_edit_locked()
        self._edt_device_id.setReadOnly(locked)
        self._edt_device_id.setToolTip(
            "监听/连接期间不能修改采集设备编号；请先停止监听。"
            if locked else
            "采集设备的持久化编号，用于基线匹配（人工填写，"
            "非硬件认证）；\n不使用设备 IP / 端口作为身份。"
        )

    def _on_device_id_edited(self) -> None:
        """采集设备编号变更：持久化（基线身份用）。

        监听/连接期间禁止修改：同一会话内换身份会让"当前会话基线"
        失去含义；必须先停止监听再改，改动在下次连接生效。
        """
        if not hasattr(self, "_edt_device_id"):
            return
        new_id = self._edt_device_id.text().strip()
        if self._identity_edit_locked():
            if new_id != self._config.device_id:
                self._edt_device_id.setText(self._config.device_id)
                msg = (
                    "正在监听/连接，设备编号不可修改；"
                    "请先停止监听，再修改编号并重新连接。"
                )
                self._log(f"[CONFIG] {msg}")
                if hasattr(self, "_message_bar"):
                    self._message_bar.show_message(msg, "warning")
            return
        if new_id == self._config.device_id:
            return
        self._config.device_id = new_id
        self._config.save()
        self._log(
            f"[CONFIG] 采集设备编号设置为 {new_id or '（空）'}"
            "（人工编号，非硬件认证；下次连接生效）"
        )
        if not new_id and self._transport == TRANSPORT_TCP:
            if hasattr(self, "_message_bar"):
                self._message_bar.show_message(
                    "未填写采集设备编号：TCP 历史基线将不会自动应用。"
                    "建议填写编号（如 EDDY-01）或重新标定。",
                    "warning",
                )

    def _device_identity(self, transport: str) -> str:
        """当前链路的设备身份（用于基线匹配）。

        - TCP：只用配置里的持久化采集设备编号，绝不用对端 IP/端口；
        - 串口：优先用配置编号，缺省回退 USB hwid（VID:PID+序列号）。
        """
        configured = self._config.device_id
        if transport == TRANSPORT_TCP:
            return configured
        if configured:
            return configured
        return self._pending_device_id or ""

    def _save_config_quiet(self) -> None:
        ok, err = self._config.save()
        if not ok:
            self._log(f"[CONFIG] 保存失败: {err}")

    def _build_left_panel(self) -> QWidget:
        """左侧：4×4 热力图 + 范围控制 + 标定 + 色阶图例。"""
        left = QWidget()
        v = QVBoxLayout()
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        heat_card = SurfaceCard()
        self._heatmap_header = SectionHeader("4×4 阵列热力图", "等待第 1 步公共补偿标定")
        header = QHBoxLayout()
        header.addWidget(self._heatmap_header, 1)
        self._before_compensation = QCheckBox("查看补偿前")
        self._before_compensation.setToolTip("使用同一完好参考和同一色阶对照补偿前后。")
        self._before_compensation.toggled.connect(lambda: self._render_compensation_grid())
        header.addWidget(self._before_compensation, 0, Qt.AlignVCenter)
        heat_card.add_layout(header)
        self._workflow_status = QLabel("连接设备后采集完好参考，或加载同工况标定。")
        self._workflow_status.setObjectName("Caption")
        heat_card.add_widget(self._workflow_status)
        self._heatmap = HeatmapWidget()
        self._heatmap.channel_clicked.connect(self._on_heatmap_cell_clicked)
        heat_card.add_widget(self._heatmap, 1)

        range_row = QHBoxLayout()
        range_row.setSpacing(8)
        self._legacy_range_controls = QWidget()
        range_row.setContentsMargins(0, 0, 0, 0)
        self._legacy_range_controls.setLayout(range_row)
        self._legacy_range_controls.hide()
        heatmap_mode_label = QLabel("显示")
        self._array_heatmap_mode = QComboBox()
        self._array_heatmap_mode.setObjectName("arrayHeatmapMode")
        self._array_heatmap_mode.setAccessibleName("热力图显示量")
        self._array_heatmap_mode.addItem("覆盖率（%）", "coverage")
        self._array_heatmap_mode.addItem("空场衰减（mV）", "board_raw")
        self._array_heatmap_mode.addItem("公共补偿（mV）", "corrected")
        self._array_heatmap_mode.addItem("独立衰减实验（mV）", "attenuation")
        heatmap_mode_label.setBuddy(self._array_heatmap_mode)
        range_row.addWidget(heatmap_mode_label)
        range_row.addWidget(self._array_heatmap_mode)
        self._lbl_range = QLabel(
            f"色阶 ±{self._heatmap_range_max:.0f}"
        )
        self._lbl_range.setProperty("role", "numeric")
        range_row.addWidget(self._lbl_range)
        self._chk_auto = QPushButton("自动范围: 开")
        self._chk_auto.setCheckable(True)
        self._chk_auto.setChecked(self._heatmap_auto)
        self._chk_auto.setProperty("role", "ghost")
        self._chk_auto.toggled.connect(self._on_auto_range_toggled)
        range_row.addWidget(self._chk_auto)
        self._lbl_manual_range = QLabel("手动:")
        range_row.addWidget(self._lbl_manual_range)
        self._sp_range = QSpinBox()
        self._sp_range.setRange(1, 100000)
        self._sp_range.setValue(int(self._heatmap_range_max))
        self._sp_range.valueChanged.connect(self._on_manual_range_changed)
        range_row.addWidget(self._sp_range)
        range_row.addStretch(1)
        heat_card.add_widget(self._legacy_range_controls)
        self._lbl_array_coverage_status = QLabel()
        self._lbl_array_coverage_status.setObjectName("arrayCoverageHeatmapStatus")
        self._lbl_array_coverage_status.setWordWrap(True)
        self._lbl_array_coverage_status.setTextFormat(Qt.PlainText)
        self._lbl_array_coverage_status.hide()
        heat_card.add_widget(self._lbl_array_coverage_status)
        v.addWidget(heat_card, 1)

        self._legend = HeatmapLegend()
        self._legend.set_range(self._heatmap_range_max)
        v.addWidget(self._legend)
        self._array_heatmap_mode.currentIndexChanged.connect(self._on_array_heatmap_mode_changed)

        self._board_controls = QScrollArea()
        self._board_controls.setWidgetResizable(True)
        self._board_controls.setFrameShape(QFrame.NoFrame)
        self._board_controls.hide()

        calib_card = SurfaceCard()
        self._legacy_calib_card = calib_card
        calib_card.add_widget(SectionHeader(
            "无钢板基线标定",
            "移除钢板后标定；基线固定，放入钢板后观察衰减量",
        ))
        calib_row = QHBoxLayout()
        calib_row.setSpacing(8)
        self._sp_calib = QSpinBox()
        self._sp_calib.setRange(2, 5000)
        self._sp_calib.setValue(200)
        self._sp_calib.setSuffix(" 样本/通道")
        calib_row.addWidget(self._sp_calib)
        self._btn_calib = QPushButton()
        self._btn_calib.setIcon(
            self.style().standardIcon(QStyle.SP_MediaSkipForward)
        )
        self._btn_calib.setText("开始标定")
        self._btn_calib.setProperty("role", "primary")
        self._btn_calib.setAccessibleName("开始无钢板基线标定")
        self._btn_calib.setToolTip(
            "无钢板基线标定：开始前需确认已移除钢板，\n"
            "保持线圈位置、激励频率和增益不变"
        )
        self._btn_calib.clicked.connect(self._on_calib_clicked)
        calib_row.addWidget(self._btn_calib)
        self._btn_calib_cancel = QPushButton("取消")
        self._btn_calib_cancel.setProperty("role", "ghost")
        self._btn_calib_cancel.setEnabled(False)
        self._btn_calib_cancel.clicked.connect(self._on_calib_cancel)
        calib_row.addWidget(self._btn_calib_cancel)
        self._lbl_calib = QLabel("空闲")
        self._lbl_calib.setObjectName("Caption")
        calib_row.addWidget(self._lbl_calib, 1)
        calib_card.add_layout(calib_row)
        calib_card.hide()

        self._heatmap.set_display_mode("compensated")
        self._legend.set_display_mode("compensated")
        left.setLayout(v)
        return left

    def _build_right_panel(self) -> QWidget:
        """右侧：通道详情 + 曲线（垂直堆叠，无标签）。"""
        right = QWidget()
        v = QVBoxLayout()
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        ch_card = SurfaceCard()
        ch_card.add_widget(SectionHeader("选中通道"))
        self._channel_panel = ChannelPanel()
        self._channel_panel.set_profile(self._config.adc_profile)
        ch_card.add_widget(self._channel_panel)
        v.addWidget(ch_card, 1)

        # PlotWidget 自带"实时曲线"标题——不要在外层再加 SectionHeader
        # 也不要在外层套 SurfaceCard，避免双重内边距浪费高度
        self._plot = PlotWidget()
        self._plot.set_window(self._config.curve_window_s)
        self._plot.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        v.addWidget(self._plot, 4)

        right.setLayout(v)
        return right

    def _build_replay_bar(self) -> QWidget:
        self._replay_controls = ReplayControls()
        self._replay_controls.open_csv_requested.connect(self._on_replay_open)
        self._replay_controls.play_requested.connect(self._on_replay_play)
        self._replay_controls.pause_requested.connect(self._on_replay_pause)
        self._replay_controls.stop_requested.connect(self._on_replay_stop)
        self._replay_controls.seek_requested.connect(self._on_replay_seek)
        self._replay_controls.speed_requested.connect(self._on_replay_speed)
        return self._replay_controls

    def _build_dock(self) -> None:
        """底部 QDockWidget：日志 + 原始数据。"""
        dock = QDockWidget("诊断", self)
        dock.setObjectName("DiagnosticsDock")
        dock.setAllowedAreas(
            Qt.BottomDockWidgetArea | Qt.TopDockWidgetArea
        )
        dock.setFeatures(
            QDockWidget.DockWidgetClosable
            | QDockWidget.DockWidgetMovable
            | QDockWidget.DockWidgetFloatable
        )
        tabs = QTabWidget()
        log_tab = QWidget()
        lv = QVBoxLayout()
        lv.setContentsMargins(6, 4, 6, 4)
        lv.setSpacing(4)
        log_tools = QHBoxLayout()
        log_tools.setSpacing(4)
        style = self.style()
        self._btn_log_clear = QPushButton()
        self._btn_log_clear.setIcon(
            style.standardIcon(QStyle.SP_DialogResetButton)
        )
        self._btn_log_clear.setText("清空")
        self._btn_log_clear.setProperty("role", "ghost")
        self._btn_log_clear.setAccessibleName("清空日志")
        self._btn_log_clear.clicked.connect(self._on_log_clear)
        log_tools.addWidget(self._btn_log_clear)
        self._btn_log_copy = QPushButton()
        self._btn_log_copy.setIcon(
            style.standardIcon(QStyle.SP_DialogSaveButton)
        )
        self._btn_log_copy.setText("复制全部")
        self._btn_log_copy.setProperty("role", "ghost")
        self._btn_log_copy.setAccessibleName("复制全部日志")
        self._btn_log_copy.clicked.connect(self._on_log_copy)
        log_tools.addWidget(self._btn_log_copy)
        self._chk_log_autoscroll = QPushButton("自动滚动: 开")
        self._chk_log_autoscroll.setCheckable(True)
        self._chk_log_autoscroll.setChecked(True)
        self._chk_log_autoscroll.setProperty("role", "ghost")
        self._chk_log_autoscroll.toggled.connect(
            self._on_log_autoscroll_toggled
        )
        log_tools.addWidget(self._chk_log_autoscroll)
        self._btn_log_save = QPushButton()
        self._btn_log_save.setIcon(
            style.standardIcon(QStyle.SP_DialogOpenButton)
        )
        self._btn_log_save.setText("保存日志")
        self._btn_log_save.setProperty("role", "ghost")
        self._btn_log_save.setAccessibleName("保存日志到文件")
        self._btn_log_save.clicked.connect(self._on_log_save)
        log_tools.addWidget(self._btn_log_save)
        log_tools.addStretch(1)
        lv.addLayout(log_tools)
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(500)
        self._log_view.setFont(QFont("Consolas", 9))
        lv.addWidget(self._log_view)
        log_tab.setLayout(lv)
        tabs.addTab(log_tab, "日志")
        raw_tab = QWidget()
        rv = QVBoxLayout()
        rv.setContentsMargins(6, 4, 6, 4)
        self._raw_view = QPlainTextEdit()
        self._raw_view.setReadOnly(True)
        self._raw_view.setMaximumBlockCount(500)
        self._raw_view.setFont(QFont("Consolas", 9))
        rv.addWidget(self._raw_view)
        raw_tab.setLayout(rv)
        tabs.addTab(raw_tab, "原始数据")

        dock.setWidget(tabs)
        dock.setVisible(False)
        try:
            dock.resize(dock.width(), 220)
        except Exception:
            pass
        self._dock = dock
        self.addDockWidget(Qt.BottomDockWidgetArea, dock)
        dock.visibilityChanged.connect(self._on_dock_visibility_changed)

    def _on_dock_visibility_changed(self, visible: bool) -> None:
        if hasattr(self, "_btn_diag"):
            self._btn_diag.blockSignals(True)
            self._btn_diag.setChecked(visible)
            self._btn_diag.blockSignals(False)

    def _on_log_clear(self) -> None:
        if hasattr(self, "_log_view"):
            self._log_view.clear()

    def _on_log_copy(self) -> None:
        from PyQt5.QtWidgets import QApplication
        if hasattr(self, "_log_view"):
            QApplication.clipboard().setText(self._log_view.toPlainText())

    def _on_log_autoscroll_toggled(self, on: bool) -> None:
        if hasattr(self, "_chk_log_autoscroll"):
            self._chk_log_autoscroll.setText(
                f"自动滚动: {'开' if on else '关'}"
            )

    def _on_log_save(self) -> None:
        if not hasattr(self, "_log_view"):
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "保存日志", "diagnostic_log.txt",
            "文本文件 (*.txt);;所有文件 (*)",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._log_view.toPlainText())
        except OSError as e:
            QMessageBox.warning(self, "保存日志失败", str(e))

    # ==================================================================
    # 配置/处理器同步
    # ==================================================================
    def _apply_processor_settings_from_config(self) -> None:
        self._processor.set_ema_alpha(self._config.ema_alpha)
        self._processor.set_median_window(self._config.median_window)
        self._processor.set_invert_sign(self._config.invert_sign)

    def _apply_config_to_ui(self) -> None:
        if hasattr(self, "_lbl_profile_tb"):
            unconfirmed = (
                "（绑定未实测确认）"
                if self._config.layout_id == LAYOUT_ACTIVE_PCB
                and not self._config.binding_confirmed
                else ""
            )
            self._lbl_profile_tb.set_full_text(
                f"Profile: {self._config.adc_profile.display_name}"
                f" | 布局: {self._config.layout_display_name}"
                f"{unconfirmed}"
            )
            self._lbl_profile_tb.setToolTip(
                "Profile 决定电压量程与分压补偿（与布局无关）；\n"
                "布局决定 CH01–CH16 在 4×4 热力图中的显示位置；\n"
                "线圈绑定决定每个格子读取第几路串口电压"
                f"（当前：{self._config.binding_display_name}）。\n"
                "三者独立设置，换布局/换绑定不会改变电压换算与 CSV 列序。"
            )
        self._channel_panel.set_profile(self._config.adc_profile)
        self._heatmap.set_range(self._heatmap_range_max)
        if hasattr(self, "_legend"):
            self._legend.set_range(self._heatmap_range_max)
        self._sp_range.blockSignals(True)
        self._sp_range.setValue(int(self._heatmap_range_max))
        self._sp_range.blockSignals(False)
        self._lbl_range.setText(
            f"色阶 ±{self._heatmap_range_max:.0f}"
        )
        self._chk_auto.blockSignals(True)
        self._chk_auto.setChecked(self._heatmap_auto)
        self._chk_auto.setText(
            "自动范围: 开" if self._heatmap_auto else "自动范围: 关"
        )
        self._chk_auto.blockSignals(False)
        # 传输方式相关的控件可见性/按钮文案随配置同步
        self._update_transport_ui()

    # ==================================================================
    # 会话管理（P0-3）
    # ==================================================================
    def _start_new_session(self, source: str) -> str:
        # 切换数据源前若仍在记录，先停止：同一 CSV 不得混入
        # 不同来源/会话元数据的帧（否则衰减量的基线标识失真）
        if self._recorder.is_recording:
            self._log(
                "[REC] 切换数据源：停止当前记录（来源/会话元数据不可混用）"
            )
            self._btn_rec.setChecked(False)  # 触发 _on_rec_toggled(False)
        self._session_id = f"{source}-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        self._parser.reset()
        self._frame_count = 0
        self._error_count = 0
        self._recv_byte_count = 0
        self._expected_scan_number = None
        self._dropped_frames = 0
        self._frame_timestamps.clear()
        self._data_timeout_reported = False
        self._data_timeout_reason = ""
        self._last_valid_data_time = 0.0
        self._last_rx_byte_time = time.monotonic()
        self._session_start_time = time.monotonic()
        self._device_fault = False
        self._device_init_status = 0
        self._reset_segment_counters()
        self._clear_array_analysis_frame()
        return self._session_id

    def _reset_segment_counters(self, new_segment: bool = False) -> None:
        """重置"连接段"级统计与源设备运行周期。

        应用会话（session_id / 记录文件）可以跨连接段，但数据健康判断
        必须按段重置——否则"先正常收帧、断线、重连后持续乱码"会被历史
        累计计数掩盖。
        """
        now = time.monotonic()
        if new_segment:
            self._segment_index += 1
        self._segment_byte_count = 0
        self._segment_dframe_count = 0
        self._segment_eframe_count = 0
        self._segment_start_time = now
        self._segment_last_byte_time = 0.0
        self._segment_last_dframe_time = 0.0
        # 新段重新建立扫描号预期：不沿用上一段（可能已重启/回绕）
        self._expected_scan_number = None
        self._scan_epoch += 1
        self._scan_gaps = 0
        self._scan_wraps = 0
        self._scan_restarts = 0
        self._scan_duplicates = 0
        self._scan_reorders = 0
        self._dropped_frames = 0
        self._tcp_idle_timeout_reported = False

    def _stop_all_sources(self) -> None:
        self._stop_serial()
        self._stop_tcp()
        self._stop_demo()

    def _session_source_name(self) -> str:
        """CSV 会话来源名：serial / tcp / demo / none（回放不写新行）。"""
        if self._source_state == SourceState.DEMO:
            return SCOPE_DEMO
        if self._source_state == SourceState.SERIAL_CONNECTED:
            if self._current_scope in (SCOPE_SERIAL, SCOPE_TCP):
                return self._current_scope
            return (
                SCOPE_TCP if self._transport == TRANSPORT_TCP
                else SCOPE_SERIAL
            )
        return "none"

    # ------------------------------------------------------------------
    # 基线作用域管理（serial/demo 隔离）
    # ------------------------------------------------------------------
    def _enter_live_scope(self, scope: str, device_id: str) -> None:
        """切换到某数据来源作用域。

        - 清理滤波、当前帧有效性与曲线会话数据（旧来源残留不得混入）
        - 回放覆盖量随 clear_session_data 一并清除
        - 按作用域（+设备标识）重新应用已保存基线；设备不匹配时不应用
        """
        self._clear_array_analysis_frame()
        self._current_scope = scope
        self._current_device_id = device_id or ""
        self._processor.clear_session_data()
        self._last_dframe = None
        self._last_replay_raw_valid = [0] * 16
        if hasattr(self, "_plot"):
            self._plot.clear()
        self._apply_scope_baselines()

    def _baseline_conditions(self) -> dict:
        """当前"实际测量条件"（基线匹配用，与布局/设备正交）。"""
        exp = self._config.experiment_params
        try:
            freq = float(exp.get("frequency", 0) or 0)
        except (TypeError, ValueError):
            freq = 0.0
        return {
            "adc_profile": self._config.adc_profile.name,
            "frequency": freq,
            "freq_unit": str(exp.get("freq_unit", "") or ""),
            "gain": str(exp.get("gain", "") or ""),
            "coil": str(exp.get("coil", "") or ""),
        }

    def _apply_scope_baselines(self) -> None:
        """统一基线匹配入口（启动 / 连接 / 换布局 / 改条件都走这里）。

        匹配维度：数据来源 + 采集设备 + 通道布局 + 测量条件
        （ADC Profile / 频率 / 增益 / 线圈）。任一项不匹配则不应用，
        并给出可见原因；绝不静默套用别板/别实验的标定结果。
        """
        layout = self._config.layout_id
        conditions = self._baseline_conditions()
        baselines = self._config.get_baselines(
            self._current_scope,
            self._current_device_id or None,
            layout,
            conditions,
        )
        applied = False
        for ch in range(16):
            st = self._processor.get(ch)
            v = baselines.get(ch)
            st.baseline_mv = float(v) if v is not None else None
            if v is not None:
                applied = True
                if st.last_status == ChannelStatus.NO_DATA:
                    st.set_status(ChannelStatus.OK)
        if not applied and self._current_scope in (SCOPE_SERIAL, SCOPE_TCP):
            msg = self._explain_baseline_mismatch(layout, conditions)
            if msg:
                self._log(f"[CONFIG] {msg}")
                if hasattr(self, "_message_bar"):
                    self._message_bar.show_message(msg, "warning")
        # 记录"当前会话的基线确实已应用"——不匹配未应用时为 False，
        # CSV 元数据不得声称使用了该基线来源
        self._scope_baselines_applied = applied
        self._sync_plot_baseline_lines()

    def _explain_baseline_mismatch(
        self, layout: str, conditions: dict
    ) -> str:
        """基线未应用的原因说明（无历史基线时返回空串，不吓用户）。"""
        scope = self._current_scope
        dev = self._current_device_id or None
        # 同设备/同布局但条件不同？
        meta_cond = self._config.get_baseline_meta_for(
            scope, dev, layout, None
        )
        if meta_cond is not None:
            saved_cond = {
                "adc_profile": meta_cond.get("adc_profile", ""),
                "frequency": meta_cond.get("frequency", 0.0),
                "freq_unit": meta_cond.get("freq_unit", ""),
                "gain": meta_cond.get("gain", ""),
                "coil": meta_cond.get("coil", ""),
            }
            return (
                "已保存基线的测量条件（"
                f"{self._config.baseline_conditions_signature(saved_cond)}"
                "）与当前条件（"
                f"{self._config.baseline_conditions_signature(conditions)}"
                "）不一致，该基线未应用；请按当前条件重新进行无钢板基线标定。"
            )
        meta = self._config.get_baseline_meta(scope)
        if meta is None:
            return ""
        saved_dev = meta.get("device_id") or ""
        cur_dev = self._current_device_id or ""
        saved_layout = meta.get("layout") or LAYOUT_BREADBOARD
        if saved_layout != layout:
            return (
                f"已保存基线的布局（{layout_name(saved_layout)}）与"
                f"当前布局（{layout_name(layout)}）不一致，"
                "该基线未应用；请在新布局下重新进行无钢板基线标定。"
            )
        if scope == SCOPE_TCP:
            if not cur_dev:
                return (
                    "未填写采集设备编号：已保存的 TCP 基线未自动应用"
                    "（无法确认基线属于哪台设备）。请填写编号后重新连接，"
                    "或重新进行无钢板基线标定。"
                )
            if not saved_dev:
                return (
                    "已保存的 TCP 基线未记录采集设备编号（来源不可确认），"
                    "未自动应用；请重新进行无钢板基线标定。"
                )
            if saved_dev != cur_dev:
                return (
                    f"当前采集设备编号「{cur_dev}」与已保存基线"
                    f"（「{saved_dev}」）不一致，该基线未应用。"
                    "请重新标定或确认编号；编号为人工填写，"
                    "不代表系统自动识别设备。"
                )
        elif saved_dev and cur_dev and saved_dev != cur_dev:
            return (
                "当前设备与已保存基线的设备标识不一致，"
                "该基线未应用；请重新进行无钢板基线标定。"
            )
        return ""

    def _clear_plot_baselines(self) -> None:
        """移除所有曲线基线参考线。

        进入/重新加载回放时调用：回放行的基线随帧到达后由
        _sync_plot_baseline_lines 重建；此间不得显示上一来源
        （实时会话/上一个回放文件）残留的旧基线参考线。
        """
        if not hasattr(self, "_plot"):
            return
        for ch in range(16):
            self._plot.set_baseline(ch, None)

    def _sync_plot_baseline_lines(self) -> None:
        """按当前基线更新曲线参考线（仅当前可见通道）。"""
        if not hasattr(self, "_plot"):
            return
        visible = set(self._plot.visible_channels())
        for ch in range(16):
            st = self._processor.get(ch)
            if ch in visible and st.baseline_mv is not None:
                self._plot.set_baseline(ch, st.baseline_mv)
            else:
                self._plot.set_baseline(ch, None)

    def _baseline_session_meta(self) -> dict:
        """当前作用域基线的来源信息（写入 CSV 元数据）。

        基线未实际应用到当前会话（无基线 / 设备标识不匹配未应用）时
        返回空串——CSV 不得声称使用了并未生效的基线来源。
        """
        meta = self._config.get_baseline_meta_for(
            self._current_scope,
            self._current_device_id or None,
            self._config.layout_id,
            self._baseline_conditions(),
        )
        if meta is None or not self._scope_baselines_applied:
            return {
                "baseline_source": "",
                "baseline_id": "",
                "baseline_device_id": "",
            }
        return {
            "baseline_source": meta.get("source", ""),
            "baseline_id": meta.get("baseline_id", ""),
            "baseline_device_id": meta.get("device_id", ""),
        }

    # ==================================================================
    # 串口控制
    # ==================================================================
    def _update_port_list(self) -> None:
        # 完整/紧凑两条操作栏各有一个端口下拉，必须同步刷新，
        # 否则曲线全宽布局下紧凑栏里的端口列表为空/过期
        for cmb in (
            getattr(self, "_cmb_port", None),
            getattr(self, "_cmb_port_inline", None),
        ):
            if cmb is None:
                continue
            current = cmb.currentText()
            cmb.blockSignals(True)
            cmb.clear()
            for dev, desc in list_serial_ports():
                cmb.addItem(f"{dev} — {desc}", dev)
            if current:
                idx = cmb.findText(current)
                if idx >= 0:
                    cmb.setCurrentIndex(idx)
            cmb.blockSignals(False)

    def _on_connect_clicked(self) -> None:
        if self._transport == TRANSPORT_TCP:
            self._start_tcp_listening()
            return
        # 紧凑操作栏可见时，以紧凑栏的端口选择为准（用户实际看到的那个）
        cmb = (
            self._cmb_port_inline
            if self._op_bar_inline.isVisible()
            else self._cmb_port
        )
        port = cmb.currentData()
        if not port:
            QMessageBox.warning(self, "未选择端口", "请先选择串口")
            return
        try:
            baud = int(self._cmb_baud.currentText())
        except ValueError:
            baud = 115200

        self._stop_all_sources()
        if self._calib.is_running():
            self._calib.cancel()

        # 设备标识：优先用配置里持久化的采集设备编号（USB/TCP 一致），
        # 未配置时回退 USB VID:PID+序列号（不只看 COM 口号）
        self._pending_device_id = (
            self._config.device_id or device_id_for_port(port)
        )
        self._set_source_state(SourceState.CONNECTING)
        self._start_new_session("serial")
        self._serial = SerialWorker(port=port, baudrate=baud)
        self._serial.connected.connect(self._on_serial_connected)
        self._serial.data_received.connect(self._on_serial_data)
        self._serial.error_occurred.connect(self._on_serial_error)
        self._serial.connection_lost.connect(self._on_serial_lost)
        self._serial.stopped.connect(self._on_serial_stopped)
        self._serial.start()
        self._config.last_serial = {"port": port, "baudrate": baud}
        self._config.save()

    def _start_tcp_listening(self) -> None:
        """开始 TCP 监听（Hi3863 作为客户端主动连入）。"""
        host = (
            self._edt_listen_host.text().strip()
            if hasattr(self, "_edt_listen_host") else ""
        ) or self._config.tcp_listen_host
        try:
            port = int(self._sp_listen_port.value())
        except (AttributeError, ValueError):
            port = self._config.tcp_listen_port
        if not (1 <= port <= 65535):
            QMessageBox.warning(self, "端口非法", "监听端口必须在 1..65535")
            return

        # 持久化监听参数与设备编号（兼容旧配置：只增字段，不动 last_serial）
        self._config.tcp_listen_host = host
        self._config.tcp_listen_port = port
        self._config.transport = TRANSPORT_TCP
        if hasattr(self, "_edt_device_id"):
            self._config.device_id = self._edt_device_id.text().strip()
        keepalive_on = (
            self._chk_keepalive.isChecked()
            if hasattr(self, "_chk_keepalive")
            else self._config.tcp_keepalive_enabled
        )
        keepalive_idle = (
            int(self._sp_keepalive.value())
            if hasattr(self, "_sp_keepalive")
            else self._config.tcp_keepalive_idle_s
        )
        self._config.tcp_keepalive_enabled = keepalive_on
        self._config.tcp_keepalive_idle_s = keepalive_idle
        self._save_config_quiet()

        self._stop_all_sources()
        if self._calib.is_running():
            self._calib.cancel()

        self._pending_device_id = self._device_identity(TRANSPORT_TCP)
        self._tcp_endpoint = f"{host}:{port}"
        self._tcp_peer = ""
        self._tcp_idle_timeout_reported = False
        self._tcp_connect_count = 0
        self._set_source_state(SourceState.LISTENING)
        self._start_new_session("tcp")

        self._tcp = TcpServerWorker(
            host=host,
            port=port,
            data_timeout_s=DATA_TIMEOUT_S,
            keepalive_enabled=keepalive_on,
            keepalive_idle_s=keepalive_idle,
            keepalive_interval_s=self._config.tcp_keepalive_interval_s,
            keepalive_count=self._config.tcp_keepalive_count,
        )
        self._tcp.listening.connect(self._on_tcp_listening)
        self._tcp.connected.connect(self._on_tcp_connected)
        self._tcp.data_received.connect(self._on_tcp_data)
        self._tcp.error_occurred.connect(self._on_tcp_error)
        self._tcp.connection_lost.connect(self._on_tcp_lost)
        self._tcp.client_rejected.connect(self._on_tcp_rejected)
        self._tcp.data_timeout.connect(self._on_tcp_timeout)
        self._tcp.data_resumed.connect(self._on_tcp_resumed)
        self._tcp.stopped.connect(self._on_tcp_stopped)
        self._tcp.start()

        self._log(
            f"[TCP] 正在监听 {self._tcp.endpoint}，等待采集设备连入"
        )
        self.statusBar().showMessage(
            f"正在监听 {self._tcp.endpoint}，等待设备连接"
        )

    def _on_disconnect_clicked(self) -> None:
        self._stop_all_sources()
        if self._calib.is_running():
            self._calib.cancel()
        self._set_source_state(SourceState.DISCONNECTED)
        if self._recorder.is_recording:
            self._log("[REC] 数据源已停止；记录仍继续（无新帧将停止）")

    def _on_demo_clicked(self) -> None:
        self._stop_all_sources()
        if self._calib.is_running():
            self._calib.cancel()
        self._start_demo()

    def _stop_serial(self) -> None:
        if self._serial is not None:
            try:
                self._serial.stop()
            except Exception:
                pass
            self._serial = None

    def _stop_tcp(self) -> None:
        """停止 TCP 监听并释放 socket（关窗/切换模式/断开都必须走这里）。"""
        if self._tcp is not None:
            try:
                self._tcp.stop()
            except Exception:
                pass
            self._tcp = None
        self._tcp_peer = ""
        self._tcp_idle_timeout_reported = False

    def _stop_demo(self) -> None:
        if self._demo is not None:
            try:
                self._demo.stop()
            except Exception:
                pass
            self._demo = None

    def _start_demo(self) -> None:
        self._start_new_session("demo")
        self._enter_live_scope(SCOPE_DEMO, "")
        # 演示的钢板空间响应按当前生效的物理映射计算——
        # 创建与复用都必须传入（按内部 channel 0..15 顺序构造），
        # 否则演示目标位置与实际接线布局不一致
        demo_map = map_to_grid_list(self._config.channel_map)
        if self._demo is None:
            self._demo = DemoSource(
                processor=self._processor,
                enabled_channels=self._processor.enabled_channels,
                map_to_grid=demo_map,
            )
            self._demo.frame_ready.connect(self._on_demo_frame)
        else:
            self._demo.configure(
                enabled_channels=self._processor.enabled_channels,
                map_to_grid=demo_map,
            )
        # 演示默认无钢板：可先演示"先标定、后放板"的真实操作顺序
        self._demo.set_plate_inserted(False)
        self._btn_plate.blockSignals(True)
        self._btn_plate.setChecked(False)
        self._btn_plate.setText("放入钢板")
        self._btn_plate.blockSignals(False)
        self._demo.start(interval_ms=200)
        self._set_source_state(SourceState.DEMO)
        self._log("[DEMO] 演示模式已启动（模拟数据，当前：无钢板）")

    def _on_demo_frame(self, df: DFrame) -> None:
        """演示帧入口：忽略旧演示源延迟到达的回调。"""
        sender = self.sender()
        if sender is not None and sender is not self._demo:
            return
        if self._source_state != SourceState.DEMO:
            return
        self._on_dframe(df)

    def _on_plate_toggled(self, checked: bool) -> None:
        if self._demo is None:
            return
        self._demo.set_plate_inserted(checked)
        self._btn_plate.setText("移除钢板" if checked else "放入钢板")
        self._log(
            "[DEMO] 模拟放入钢板（信号衰减）" if checked
            else "[DEMO] 模拟移除钢板（回到基线附近）"
        )

    def _set_source_state(self, state: SourceState) -> None:
        if self._source_state != state:
            self._clear_array_analysis_frame()
        self._source_state = state
        if state == SourceState.DISCONNECTED:
            self._lbl_state.setText("未连接")
            set_status_role(self._lbl_state, "neutral")
            self._btn_connect.setEnabled(True)
            self._btn_disconnect.setEnabled(False)
            self._btn_demo.setEnabled(True)
        elif state == SourceState.CONNECTING:
            self._lbl_state.setText("连接中…")
            set_status_role(self._lbl_state, "warning")
            self._btn_connect.setEnabled(False)
            self._btn_disconnect.setEnabled(True)
            self._btn_demo.setEnabled(True)
        elif state == SourceState.SERIAL_CONNECTED:
            if self._transport == TRANSPORT_TCP:
                peer = self._tcp_peer or "设备"
                self._lbl_state.setText(f"设备已连接 {peer}")
            else:
                self._lbl_state.setText("串口已连接")
            set_status_role(self._lbl_state, "ok")
            self._btn_connect.setEnabled(False)
            self._btn_disconnect.setEnabled(True)
            self._btn_demo.setEnabled(True)
        elif state == SourceState.LISTENING:
            # 开始监听 ≠ 设备已连接：明确区分，避免误判成"已连上"
            endpoint = self._tcp_endpoint or ""
            self._lbl_state.setText(
                f"监听中 {endpoint}（等待设备）" if endpoint else "监听中"
            )
            set_status_role(self._lbl_state, "warning")
            self._btn_connect.setEnabled(False)
            self._btn_disconnect.setEnabled(True)
            self._btn_demo.setEnabled(True)
        elif state == SourceState.DEMO:
            self._lbl_state.setText("演示模式（模拟数据）")
            set_status_role(self._lbl_state, "demo")
            self._btn_connect.setEnabled(True)
            self._btn_disconnect.setEnabled(True)
            self._btn_demo.setEnabled(False)
        # 钢板切换按钮仅演示模式可用
        if hasattr(self, "_btn_plate"):
            self._btn_plate.setEnabled(state == SourceState.DEMO)
            self._btn_plate.setVisible(state == SourceState.DEMO and self._task_mode == "plate")
            if state != SourceState.DEMO:
                self._btn_plate.blockSignals(True)
                self._btn_plate.setChecked(False)
                self._btn_plate.setText("放入钢板")
                self._btn_plate.blockSignals(False)
        # 紧凑操作栏按钮与完整操作栏保持同一状态源，
        # 否则曲线全宽布局下按钮可用性与实际数据源状态脱节
        for full, inline in (
            ("_btn_connect", "_btn_connect_inline"),
            ("_btn_disconnect", "_btn_disconnect_inline"),
            ("_btn_demo", "_btn_demo_inline"),
            ("_btn_rec", "_btn_rec_inline"),
        ):
            wf = getattr(self, full, None)
            wi = getattr(self, inline, None)
            if wf is not None and wi is not None:
                wi.setEnabled(wf.isEnabled())
        # 监听/连接期间锁定设备编号；"清除设备"仅在 TCP 已连接时可用
        self._set_identity_editable()
        self._refresh_kick_enabled()
        if state != SourceState.REPLAY:
            self._sync_active_task_context()

    # ==================================================================
    # 串口事件
    # ==================================================================
    def _on_serial_connected(self, port: str) -> None:
        # 忽略旧 SerialWorker 延迟到达的信号（重连/切换来源后）
        sender = self.sender()
        if sender is not None and sender is not self._serial:
            return
        self._log(f"[SERIAL] 已连接: {port}")
        self._parser.reset()
        self._reset_segment_counters(new_segment=True)
        self._last_rx_byte_time = time.monotonic()
        self._last_valid_data_time = 0.0
        self._data_timeout_reported = False
        self._data_timeout_reason = ""
        self._session_start_time = time.monotonic()
        self._enter_live_scope(SCOPE_SERIAL, self._pending_device_id)
        self._set_source_state(SourceState.SERIAL_CONNECTED)
        self.statusBar().showMessage(f"已连接 {port}")

    def _on_serial_error(self, msg: str) -> None:
        sender = self.sender()
        if sender is not None and sender is not self._serial:
            return
        self._log(f"[SERIAL ERROR] {msg}")
        if self._source_state == SourceState.CONNECTING:
            self._set_source_state(SourceState.DISCONNECTED)
        if self._calib.is_running():
            self._calib.cancel()
        if self._recorder.is_recording:
            self._btn_rec.setChecked(False)
        QMessageBox.warning(self, "串口错误", msg)

    def _on_serial_lost(self) -> None:
        sender = self.sender()
        if sender is not None and sender is not self._serial:
            return
        self._log("[SERIAL] 连接丢失")
        if self._calib.is_running():
            self._calib.cancel()
        if self._recorder.is_recording:
            self._btn_rec.setChecked(False)
        if self._source_state == SourceState.SERIAL_CONNECTED:
            self._set_source_state(SourceState.DISCONNECTED)
            self.statusBar().showMessage("连接丢失")

    def _on_serial_stopped(self) -> None:
        sender = self.sender()
        if sender is not None and sender is not self._serial:
            return
        if self._source_state == SourceState.CONNECTING:
            self._set_source_state(SourceState.DISCONNECTED)

    def _on_serial_data(self, data: bytes) -> None:
        # 忽略旧串口 worker 延迟到达的回调（切换来源后）
        sender = self.sender()
        if sender is not None and sender is not self._serial:
            return
        self._ingest_bytes(data)

    def _ingest_bytes(self, data: bytes) -> None:
        """串口 / TCP 共用的原始字节入口：直送 FrameParser（不改报文格式）。"""
        self._recv_byte_count += len(data)
        now = time.monotonic()
        self._last_rx_byte_time = now
        self._segment_byte_count += len(data)
        self._segment_last_byte_time = now
        try:
            txt = data.decode("utf-8", errors="replace")
            self._raw_view.appendPlainText(txt.rstrip("\r\n"))
        except Exception:
            pass
        for pf in self._parser.feed(data):
            self._handle_parsed(pf)

    # ==================================================================
    # TCP 事件（上位机监听，设备为客户端）
    # ==================================================================
    def _tcp_signal_is_current(self) -> bool:
        """忽略旧 TcpServerWorker 延迟到达的信号。"""
        sender = self.sender()
        return sender is None or sender is self._tcp

    def _on_tcp_listening(self, endpoint: str) -> None:
        if not self._tcp_signal_is_current():
            return
        self._tcp_endpoint = endpoint
        self._log(f"[TCP] 监听已就绪: {endpoint}")
        if self._source_state == SourceState.LISTENING:
            self._lbl_state.setText(f"监听中 {endpoint}（等待设备）")
            set_status_role(self._lbl_state, "warning")
        self.statusBar().showMessage(f"正在监听 {endpoint}，等待设备连接")

    def _on_tcp_connected(self, peer: str) -> None:
        if not self._tcp_signal_is_current():
            return
        reconnect = self._tcp_connect_count > 0
        self._tcp_connect_count += 1

        # 一次标定不得跨连接段累计：新段开始前先终止
        if self._calib.is_running():
            self._calib.cancel()
            self._log("[CALIB] 新连接段开始：已终止进行中的标定")

        if reconnect:
            # 新连接段按"新应用会话"处理：新 session_id、新曲线时间原点、
            # 新记录文件（_start_new_session 内部先停止旧记录）。
            # 这样 CSV 元数据/时间轴可追溯到具体连接段，且同段内不倒退。
            self._log(
                "[TCP] 新连接段：新建应用会话（曲线与记录文件按段隔离）"
            )
            self._start_new_session("tcp")
        else:
            # 首次连接沿用监听时建立的会话，只重置段级统计与解析缓冲
            self._parser.reset()
            self._reset_segment_counters(new_segment=True)
            self._session_start_time = time.monotonic()

        self._tcp_peer = peer
        self._tcp_idle_timeout_reported = False
        self._last_rx_byte_time = time.monotonic()
        self._last_valid_data_time = 0.0
        self._data_timeout_reported = False
        self._data_timeout_reason = ""
        # 清空上一条提示（必须在应用基线之前：基线不匹配的警告
        # 由 _apply_scope_baselines 写入，不能被随后的清空抹掉）
        if hasattr(self, "_message_bar"):
            self._message_bar.clear()
        # 基线按 tcp 作用域 + 持久化设备编号应用（与串口作用域隔离）
        identity = self._device_identity(TRANSPORT_TCP)
        self._enter_live_scope(SCOPE_TCP, identity)
        self._set_source_state(SourceState.SERIAL_CONNECTED)
        self._log(
            f"[TCP] 设备已连接: {peer}（第 {self._tcp_connect_count} 次连入，"
            f"连接段 {self._segment_index}，会话 {self._session_id}）"
        )
        self.statusBar().showMessage(f"设备已连接 {peer}")
        if not identity:
            msg = (
                "未填写采集设备编号：历史 TCP 基线不会自动应用"
                "（无法确认属于哪台设备）。请填写编号后重新连接，"
                "或重新进行无钢板基线标定。"
            )
            self._log(f"[CONFIG] {msg}")
            if hasattr(self, "_message_bar"):
                self._message_bar.show_message(msg, "warning")

    def _on_tcp_lost(self) -> None:
        if not self._tcp_signal_is_current():
            return
        peer = self._tcp_peer or "设备"
        self._log(f"[TCP] {peer} 连接断开，继续监听等待重连")
        self._tcp_peer = ""

        # 断线立即终止标定：标定结果不得跨连接段累计
        if self._calib.is_running():
            self._calib.cancel()
            self._log(
                "[CALIB] TCP 设备断开：已终止进行中的标定"
                "（一次标定不可跨连接段）"
            )
        # 记录按连接段分文件：断线即停止，避免同一 CSV 内时间轴跨段倒退
        if self._recorder.is_recording:
            self._log(
                "[REC] 设备断开：停止当前记录（每个连接段单独成文件，"
                "元数据含 session_id 可追溯）"
            )
            self._btn_rec.setChecked(False)

        # 段结束：半帧、段计数、扫描号预期全部作废
        self._parser.reset()
        self._segment_byte_count = 0
        self._segment_dframe_count = 0
        self._segment_last_dframe_time = 0.0
        self._segment_last_byte_time = 0.0
        self._expected_scan_number = None
        self._last_valid_data_time = 0.0
        self._data_timeout_reported = False
        self._data_timeout_reason = ""
        self._tcp_idle_timeout_reported = False
        if self._source_state in (
            SourceState.SERIAL_CONNECTED, SourceState.LISTENING
        ):
            self._set_source_state(SourceState.LISTENING)
        self.statusBar().showMessage(f"{peer} 已断开，继续监听")
        if hasattr(self, "_message_bar"):
            self._message_bar.show_message(
                f"采集设备 {peer} 已断开；仍在监听，等待设备自动重连。",
                "warning",
            )

    def _on_tcp_rejected(self, peer: str) -> None:
        if not self._tcp_signal_is_current():
            return
        msg = (
            f"已有采集设备在线，已拒绝新的连接 {peer}（单设备模式，"
            "不会混合两台设备的数据）"
        )
        self._log(f"[TCP] {msg}")
        self.statusBar().showMessage(f"已拒绝 {peer}（已有设备在线）")
        if hasattr(self, "_message_bar"):
            self._message_bar.show_message(msg, "warning")

    def _on_tcp_timeout(self, idle_s: float) -> None:
        """链路仍在线但长时间无字节：提示数据超时（不判为断线）。"""
        if not self._tcp_signal_is_current():
            return
        self._tcp_idle_timeout_reported = True
        msg = (
            f"TCP 链路仍连接，但已 {idle_s:.0f}s 未收到数据"
            f"（阈值 {DATA_TIMEOUT_S:.0f}s）；连接未断开，等待设备恢复发送"
        )
        self._log(f"[TIMEOUT] {msg}")
        self._lbl_state.setText("数据超时（连接保持）")
        set_status_role(self._lbl_state, "warning")
        if hasattr(self, "_message_bar"):
            self._message_bar.show_message(msg, "warning")

    def _on_tcp_resumed(self) -> None:
        if not self._tcp_signal_is_current():
            return
        self._tcp_idle_timeout_reported = False
        self._log("[TCP] 数据恢复接收")
        if hasattr(self, "_message_bar"):
            self._message_bar.clear()
        if self._source_state == SourceState.SERIAL_CONNECTED:
            self._lbl_state.setText("接收中")
            set_status_role(self._lbl_state, "ok")

    def _on_tcp_error(self, msg: str) -> None:
        if not self._tcp_signal_is_current():
            return
        self._log(f"[TCP ERROR] {msg}")
        if self._calib.is_running():
            self._calib.cancel()
        if self._source_state in (
            SourceState.LISTENING, SourceState.CONNECTING
        ):
            self._set_source_state(SourceState.DISCONNECTED)
        QMessageBox.warning(self, "TCP 监听错误", msg)

    def _on_tcp_stopped(self) -> None:
        if not self._tcp_signal_is_current():
            return
        if self._source_state in (SourceState.LISTENING,):
            self._set_source_state(SourceState.DISCONNECTED)

    def _on_tcp_data(self, data: bytes) -> None:
        if not self._tcp_signal_is_current():
            return
        self._ingest_bytes(data)

    def _handle_parsed(self, pf: ParsedFrame) -> None:
        if pf.kind == FrameKind.D and pf.d_frame is not None:
            self._on_dframe(pf.d_frame)
        elif pf.kind == FrameKind.E and pf.e_frame is not None:
            self._on_eframe(pf.e_frame)
        elif pf.kind == FrameKind.DIAG:
            self._log(f"[DIAG] {pf.raw_line.strip()}")
        elif pf.kind == FrameKind.UNKNOWN:
            self._error_count += 1
            if pf.error:
                self._log(f"[PARSE ERR] {pf.raw_line.strip()} -> {pf.error}")

    def _on_eframe(self, ef: EFrame) -> None:
        self._last_eframe = ef
        self._segment_eframe_count += 1
        self._device_init_status = ef.init_status
        self._device_init_attempts = ef.init_attempts
        self._device_recover_attempts = ef.recover_attempts
        self._device_total_errors = ef.total_errors
        if ef.init_status != 0:
            self._device_fault = True
        else:
            self._device_fault = False
        self._log(
            f"[E] INIT status={ef.init_status} "
            f"inits={ef.init_attempts} recovers={ef.recover_attempts} "
            f"errs={ef.total_errors}"
        )

    def _classify_scan_number(self, scan_number: int) -> None:
        """分类扫描号连续性。

        缺口**不等于**无线丢包率：它只说明"这些扫描没被观测到"，
        原因可能是按键触发式采集、STM32 侧跳过、链路中断、设备重启等。
        分类：正常 / 回绕 / 重启（新运行周期）/ 重复 / 倒序（延迟帧）/ 跳号。
        """
        snap = int(scan_number)
        expected = self._expected_scan_number
        self._expected_scan_number = (snap + 1) & 0xFFFFFFFF
        if expected is None:
            return
        if snap == expected:
            return

        prev = (expected - 1) & 0xFFFFFFFF
        if snap == prev:
            self._scan_duplicates += 1
            self._log(f"[SCAN] 扫描号重复: {snap}（同一扫描重复到达）")
            return
        if snap > expected:
            gap = (snap - expected) & 0xFFFFFFFF
            self._scan_gaps += gap
            self._dropped_frames = self._scan_gaps
            if self._scan_gaps <= SCAN_GAP_LOG_LIMIT or gap >= 10:
                self._log(
                    f"[SCAN] 扫描号跳号: {prev} → {snap}，"
                    f"跳过 {gap} 次扫描（未观测，不能等同于无线丢包）"
                )
            return
        # snap < expected
        if prev - snap > 0x7FFFFFFF:
            # prev 接近 2^32、snap 接近 0：正常的 32 位回绕
            self._scan_wraps += 1
            self._log(f"[SCAN] 扫描号回绕（2^32）: {prev} → {snap}")
            return
        back = prev - snap
        if snap == 0 or back > SCAN_REORDER_WINDOW:
            # 归零或大幅回退：设备重新上电/复位，进入新的运行周期
            self._scan_epoch += 1
            self._scan_restarts += 1
            self._log(
                f"[SCAN] 扫描号回退: {prev} → {snap}，判定为设备重启"
                f"（源设备运行周期 {self._scan_epoch}）"
            )
            return
        self._scan_reorders += 1
        self._log(f"[SCAN] 扫描号倒序/延迟帧: {prev} → {snap}")

    def _on_dframe(self, df: DFrame) -> None:
        self._last_dframe = df
        self._frame_count += 1
        self._total_dframe_count += 1
        now = time.monotonic()
        self._frame_timestamps.append(now)
        self._segment_dframe_count += 1
        self._segment_last_dframe_time = now
        previous_discarded = self._scan_duplicates + self._scan_reorders
        previous_epoch = self._scan_epoch
        self._classify_scan_number(df.scan_number)

        self._processor.ingest(df)
        if previous_epoch != self._scan_epoch:
            self._clear_array_analysis_frame()
        if self._app_mode == "live":
            previous_scan = self._array_last_live_scan
            is_forward = previous_scan is None or (
                0 < ((df.scan_number - previous_scan) & 0xFFFFFFFF) < 0x80000000
            )
            if is_forward and previous_discarded == self._scan_duplicates + self._scan_reorders:
                self._array_last_live_scan = df.scan_number
                self._send_array_analysis_frame(df.samples_mv, df.scan_status, df.valid_mask)
                t_rel = max(0.0, now - (self._session_start_time or now))
                enabled_mask = sum(1 << channel for channel in self._processor.enabled_channels)
                raw = usable_samples(df.samples_mv, df.scan_status, df.valid_mask & enabled_mask)
                self._dispatch_workflow_frame(t_rel, raw)
        self._last_valid_data_time = now
        # 合法 D 帧到达：解除"无字节/协议失配/数据陈旧"一切超时告警
        self._data_timeout_reported = False
        self._data_timeout_reason = ""
        self._tcp_idle_timeout_reported = False
        if self._device_fault and df.scan_status == 0:
            self._device_fault = False

        if self._calib.is_running():
            # 只累计当前会话的帧；会话不匹配时在 manager 内剔除
            self._calib.on_frame(df, session_id=self._session_id)

        if self._selected_channel is not None:
            c = self._selected_channel
            st = self._processor.get(c)
            # 本帧所有曲线共用同一时间戳：会话相对接收时间（秒）
            t0 = getattr(self, "_session_start_time", 0.0)
            t_frame = (time.monotonic() - t0) if t0 > 0 else None
            snap = self._processor.measurement(c, df.is_valid(c))
            # 启用通道 + 本帧有状态活动才画点（未启用/从未有帧的
            # 通道 sample_mv 恒为默认 0.0，直接画会伪造一条 0 mV
            # 直线）。SATURATED/INVALID 也算活动：饱和帧的 rail
            # 诊断值经 sample_for_state 保留（首帧即饱和的通道
            # sample_count=0，不能因此整条曲线空白）；无效帧写
            # NaN 断点。数值走统一快照门槛：无效/饱和/预热帧给出
            # None → NaN 断点——不跳过（跳过会把无效段连成看似
            # 连续的线），也绝不画残留值
            if (
                c in self._processor.enabled_channels
                and (
                    st.sample_count > 0
                    or st.last_status
                    in (ChannelStatus.SATURATED, ChannelStatus.INVALID)
                )
            ):
                self._plot.add_point(
                    c, snap.sample_mv, snap.filtered_mv, t=t_frame,
                )
            # 衰减量视图：逐点记录当时基线下的衰减值（None → 断点）
            self._plot.add_attenuation_point(
                c, snap.attenuation_mv, t=t_frame,
            )

        if self._recorder.is_recording:
            self._write_csv_dframe(df)

        self._update_heatmap_auto_range()

    # ==================================================================
    # 标定
    # ==================================================================
    def _confirm_plate_removed(self) -> bool:
        """无钢板确认对话框（独立方法，便于测试/工具 monkeypatch）。"""
        r = QMessageBox.question(
            self,
            "无钢板基线标定",
            "请移除钢板，保持线圈位置、激励频率和增益不变，"
            "待信号稳定后开始标定。\n\n已确认现场无钢板？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return r == QMessageBox.Yes

    def _on_calib_clicked(self) -> None:
        # 回放模式不得标定：无实时帧，start() 只会清掉回放滤波
        # 并以"无新 D 帧"误报失败（纵深防御——按钮在回放中本已
        # 禁用，但禁用态可能被标定收尾槽恢复，见 _on_calib_cancelled）
        if self._app_mode == "replay":
            return
        if self._source_state == SourceState.DISCONNECTED:
            QMessageBox.warning(
                self, "未连接", "请先连接串口或开启演示模式"
            )
            return
        if self._source_state == SourceState.CONNECTING:
            QMessageBox.warning(
                self, "正在连接", "请等待连接完成"
            )
            return
        if self._calib.is_running():
            return
        # 演示模式已放入钢板时不得标定"无钢板基线"——
        # 那会把衰减后的信号误当成空气基线
        if (
            self._source_state == SourceState.DEMO
            and self._demo is not None
            and self._demo.plate_inserted
        ):
            QMessageBox.warning(
                self, "请先移除钢板",
                "演示模式当前处于【已放入钢板】状态。\n"
                "无钢板基线标定必须先移除钢板。",
            )
            return
        # 开始前必须确认无钢板（实验流程核心前提）
        if not self._confirm_plate_removed():
            self._lbl_calib.setText("已取消（未确认无钢板）")
            return
        target = self._sp_calib.value()
        self._calib_start_time = time.monotonic()
        self._calib_eta_s = -1.0
        self._calib_fps_ema = 0.0
        # 只累计当前会话的新 D 帧（不用 UI 刷新次数充当采样）
        self._calib.start(
            target_samples=target, session_id=self._session_id
        )
        self._btn_calib.setEnabled(False)
        self._btn_calib_cancel.setEnabled(True)
        # 标定期间禁止切换钢板状态（防止采样中途混入放板帧）
        self._btn_plate.setEnabled(False)
        self._lbl_calib.setText(f"标定中 0 / {target}")
        self.statusBar().showMessage("正在无钢板基线标定…")

    def _on_calib_cancel(self) -> None:
        self._calib.cancel()
        self._btn_calib.setEnabled(True)
        self._btn_calib_cancel.setEnabled(False)
        self._btn_plate.setEnabled(self._source_state == SourceState.DEMO)
        self._lbl_calib.setText("已取消")

    def _on_calib_progress(
        self, current: int, target: int, frames: int, target_frames: int
    ) -> None:
        eta_text = ""
        if self._calib_start_time > 0 and current > 0:
            elapsed = time.monotonic() - self._calib_start_time
            if elapsed > 0.2:
                rate = current / elapsed
                if rate > 0.01:
                    self._calib_fps_ema = (
                        0.7 * self._calib_fps_ema + 0.3 * rate
                        if self._calib_fps_ema > 0 else rate
                    )
                remaining = max(0, target - current)
                if self._calib_fps_ema > 0.01:
                    eta = remaining / self._calib_fps_ema
                    self._calib_eta_s = eta
                    if eta < 60:
                        eta_text = f"  ETA {eta:.1f}s"
                    else:
                        eta_text = f"  ETA {eta / 60:.1f}min"
        pct = int(current * 100 / max(1, target))
        self._lbl_calib.setText(
            f"标定中 {current}/{target} ({pct}%)  帧 {frames}/{target_frames}{eta_text}"
        )
        self.statusBar().showMessage(
            f"标定中 样本 {current}/{target}  帧 {frames}/{target_frames}{eta_text}"
        )

    def _on_calib_quality(self, quality: dict) -> None:
        for c, q in quality.items():
            self._log(
                f"[CALIB] CH{c + 1:02d} n={q['n']} mean={q['mean']:.2f} "
                f"median={q['median']:.2f} std={q['std']:.2f} "
                f"mad={q['mad']:.2f} cv_mad={q['cv_mad']:.3f} "
                f"drift={q.get('drift_mv', 0.0):+.2f}mV "
                f"outliers={q.get('outliers', 0)}"
            )

    def _on_calib_finished(self, baselines: dict) -> None:
        # 原子提交：任一通道无效则整体放弃，保留旧基线
        ok = self._processor.apply_calibration(baselines)
        if ok:
            # 按当前来源作用域保存（演示标定不覆盖真实硬件基线）
            scope = self._current_scope
            exp = self._config.experiment_params
            meta = {
                "source": scope,
                "device_id": self._current_device_id,
                # 记录标定时的通道布局：换板（旧面包板 ↔ 有源 PCB）后
                # 该基线不会被自动套用
                "layout": self._config.layout_id,
                "channels": sorted(baselines.keys()),
                "adc_profile": self._config.adc_profile.name,
                "detector_divider":
                    self._config.adc_profile.detector_divider,
                "frequency": exp.get("frequency", 0),
                "freq_unit": exp.get("freq_unit", ""),
                "gain": str(exp.get("gain", "")),
                "coil": str(exp.get("coil", "") or ""),
                "formula": ATTENUATION_FORMULA_ID,
            }
            # 只保存本次参与标定的通道：未参与通道若一并写入，
            # 其旧基线会被打上新 baseline_id，语义造假
            # （缺失通道由 config 校验层补 None，安全）
            self._config.set_baselines(
                scope,
                {c: self._processor.get(c).baseline_mv for c in baselines},
                meta,
            )
            ok_save, err_save = self._config.save()
            self._scope_baselines_applied = True
            if not ok_save:
                # 基线已应用到当前会话（真实），但落盘失败：
                # 告知用户重启后需重新标定；CSV 会话元数据不更新为
                # 一个磁盘上并不存在的 baseline_id
                self._log(f"[CALIB] 基线保存失败: {err_save}")
                # 记录中的 CSV 仍以旧 baseline_id 标注——而新基线
                # 已经生效，继续写会产出基线标识造假的行：明示并
                # 停止记录（已写行保持旧标识，真实无造假）
                was_recording = self._recorder.is_recording
                if was_recording:
                    self._log(
                        "[REC] 基线落盘失败：停止记录（避免用旧 "
                        "baseline_id 标注新基线下的数据）"
                    )
                    self._btn_rec.setChecked(False)
                QMessageBox.warning(
                    self, "基线保存失败",
                    "基线已应用到当前会话，但写入配置文件失败：\n"
                    f"{err_save}\n"
                    + (
                        "进行中的记录已停止（避免用旧基线标识标注"
                        "新基线下的数据），可重新开始记录。\n"
                        if was_recording else ""
                    )
                    + "重启后可能需要重新标定。",
                )
            # 基线固定：不加自动追踪；派生衰减曲线按新基线重新开始
            self._plot.notify_baseline_changed()
            self._sync_plot_baseline_lines()
            if self._recorder.is_recording and ok_save:
                new_meta = self._config.get_baseline_meta(scope) or {}
                self._recorder.update_session_meta(
                    baseline_source=new_meta.get("source", scope),
                    baseline_id=new_meta.get("baseline_id", ""),
                    baseline_device_id=new_meta.get("device_id", ""),
                )
            self._lbl_calib.setText(
                f"无钢板基线已建立（{len(baselines)} 通道）"
            )
            self._log(
                f"[CALIB] {scope} 无钢板基线已建立: "
                f"{sorted(baselines.keys())}；请放入钢板测量"
            )
            self.statusBar().showMessage(
                "无钢板基线已建立，请放入钢板测量"
            )
            if hasattr(self, "_message_bar"):
                self._message_bar.show_message(
                    "无钢板基线已建立，请放入钢板测量。", "ok"
                )
        else:
            self._lbl_calib.setText("标定失败（保留旧基线）")
            self._log("[CALIB] 失败: 基线校验不通过，保留旧基线")
            self.statusBar().showMessage("标定失败")
        # 回放模式下标定按钮保持禁用（切回放时会同步 cancel
        # 进行中的标定，收尾槽不得把它重新启用）
        self._btn_calib.setEnabled(self._app_mode != "replay")
        self._btn_calib_cancel.setEnabled(False)
        self._btn_plate.setEnabled(self._source_state == SourceState.DEMO)
        self._update_all_displays()

    def _on_calib_cancelled(self) -> None:
        self._lbl_calib.setText("已取消")
        self._log("[CALIB] 已取消，保留旧基线")
        self._btn_calib.setEnabled(self._app_mode != "replay")
        self._btn_calib_cancel.setEnabled(False)
        self._btn_plate.setEnabled(self._source_state == SourceState.DEMO)
        self.statusBar().showMessage("标定取消")

    def _on_calib_failed(self, reason: str) -> None:
        self._lbl_calib.setText(f"失败: {reason}")
        self._log(f"[CALIB] 失败: {reason}")
        self._btn_calib.setEnabled(self._app_mode != "replay")
        self._btn_calib_cancel.setEnabled(False)
        self._btn_plate.setEnabled(self._source_state == SourceState.DEMO)
        self.statusBar().showMessage("标定失败")

    # ==================================================================
    # 热力图
    # ==================================================================
    def _on_auto_range_toggled(self, checked: bool) -> None:
        self._heatmap_auto = bool(checked)
        self._config.heatmap_auto = self._heatmap_auto
        self._chk_auto.setText("自动范围: 开" if checked else "自动范围: 关")
        if not checked:
            self._heatmap_range_max = max(
                abs(self._heatmap_manual_min),
                abs(self._heatmap_manual_max),
                1.0,
            )
        else:
            self._heatmap_range_max = max(self._heatmap_auto_max, 1.0)
        self._apply_range_to_ui()

    def _on_manual_range_changed(self, v: int) -> None:
        self._heatmap_range_max = max(1, int(v))
        self._heatmap_manual_max = float(self._heatmap_range_max)
        self._heatmap_manual_min = -float(self._heatmap_range_max)
        self._config.heatmap_min = self._heatmap_manual_min
        self._config.heatmap_max = self._heatmap_manual_max
        if not self._heatmap_auto:
            self._apply_range_to_ui()

    def _apply_range_to_ui(self) -> None:
        self._heatmap.set_range(self._heatmap_range_max)
        if hasattr(self, "_legend"):
            self._legend.set_range(self._heatmap_range_max)
        self._lbl_range.setText(f"色阶 ±{self._heatmap_range_max:.0f}")
        self._sp_range.blockSignals(True)
        self._sp_range.setValue(int(self._heatmap_range_max))
        self._sp_range.blockSignals(False)

    def _update_heatmap_auto_range(self) -> None:
        if not self._heatmap_auto:
            return
        max_abs = 0.0
        for c in self._processor.enabled_channels:
            # 用回放感知的统一有效性判定（_frame_valid_for）：缺省
            # frame_valid 取实时 D 帧缓存，回放模式下恒为 False，
            # 衰减量恒 None，自动色阶会冻结在进入回放前的值
            snap = self._processor.measurement(c, self._frame_valid_for(c))
            if snap.attenuation_mv is None:
                continue
            max_abs = max(max_abs, abs(snap.attenuation_mv))
        if max_abs <= 0:
            return
        candidate = max(max_abs * 1.1, 10.0)
        if candidate > self._heatmap_auto_max * 1.5:
            if (
                time.monotonic() - self._heatmap_auto_max_set_time
                < HEATMAP_RANGE_HOLD_S
            ):
                return
            self._heatmap_auto_max_set_time = time.monotonic()
        if candidate > self._heatmap_auto_max:
            self._heatmap_auto_max = candidate
        else:
            self._heatmap_auto_max = (
                self._heatmap_auto_max * 0.9 + candidate * 0.1
            )
        self._heatmap_range_max = max(self._heatmap_auto_max, 1.0)
        if self._task_mode == "plate" and self._array_heatmap_mode.currentData() == "attenuation":
            self._apply_range_to_ui()

    # ==================================================================
    # 选中通道
    # ==================================================================
    def _on_heatmap_cell_clicked(self, channel: int) -> None:
        self._select_channel(channel)
        if self._workspace_tool is None:
            self._set_workspace_tool("channels")

    def _select_channel(self, channel: int) -> None:
        if not (0 <= channel < 16):
            return
        self._selected_channel = channel
        self._heatmap.select_channel(channel)
        self._plot.set_selected_channel(channel)
        # 详情面板同步显示该通道在阵列中的物理位置（随 channel_map）
        pos = self._heatmap.channel_position(channel)
        if pos is not None and hasattr(self._channel_panel, "set_position"):
            self._channel_panel.set_position(f"R{pos[0] + 1}C{pos[1] + 1}")
        st = self._processor.get(channel)
        self._channel_panel.update_snapshot(
            channel,
            self._processor.measurement(
                channel, self._frame_valid_for(channel)
            ),
            last_update_monotonic=st.last_update_monotonic,
            label=self._channel_label(channel),
            scan_number=self._current_scan_number(),
            valid_bit=self._frame_valid_bit(channel),
            binding_label=self._binding_label,
        )
        self._sync_plot_baseline_lines()

    def _current_scan_number(self) -> Optional[int]:
        """当前显示帧的扫描号（回放/实时同源；无帧 → None）。"""
        df = self._last_dframe
        if df is None:
            return None
        try:
            return int(df.scan_number)
        except (TypeError, ValueError):
            return None

    def _frame_valid_bit(self, channel: int) -> Optional[int]:
        """当前帧 valid_mask 中该数据通道的位值（1/0；无帧 → None）。"""
        if self._app_mode == "replay":
            return 1 if self._last_replay_raw_valid[channel] else 0
        df = self._last_dframe
        if df is None:
            return None
        return (int(df.valid_mask) >> channel) & 1

    def _frame_valid_for(self, channel: int) -> bool:
        """当前帧有效性（供统一快照）：无效/饱和/断流/陈旧 → False。"""
        if self._app_mode == "replay":
            return bool(self._last_replay_raw_valid[channel])
        if self._source_state == SourceState.SERIAL_CONNECTED:
            if self._data_timeout_reported:
                return False  # 断流/陈旧：当前测量不得显示为正常
        elif self._source_state != SourceState.DEMO:
            return False
        return self._processor.last_wire_valid(channel)

    # ==================================================================
    # 记录
    # ==================================================================
    def _sync_rec_buttons(self, on: bool) -> None:
        """完整/紧凑两个记录按钮状态同步（二者都连到 _on_rec_toggled，
        只同步其中一个会让另一个停留在旧状态，再点击时状态翻转错乱）。
        """
        for attr in ("_btn_rec", "_btn_rec_inline"):
            w = getattr(self, attr, None)
            if w is not None:
                w.blockSignals(True)
                w.setChecked(on)
                w.setText("停止记录" if on and attr == "_btn_rec"
                          else "停止" if on
                          else "开始记录" if attr == "_btn_rec"
                          else "记录")
                w.blockSignals(False)

    def _on_rec_toggled(self, on: bool) -> None:
        self._sync_rec_buttons(on)
        if on:
            if self._source_state == SourceState.DISCONNECTED:
                r = QMessageBox.warning(
                    self, "无数据源",
                    "当前未连接串口或演示模式。\n"
                    "记录将只写入表头/元信息，没有 D 帧数据。\n"
                    "确定要开始吗？",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if r != QMessageBox.Yes:
                    self._sync_rec_buttons(False)
                    return
                if hasattr(self, "_message_bar"):
                    self._message_bar.show_message(
                        "记录中但无数据源：CSV 只会写入表头/元信息。",
                        "warning",
                    )
            params = self._config.experiment_params
            save_dir = self._config.last_csv_dir
            if not save_dir or not os.path.isdir(save_dir):
                from app_paths import get_default_csv_dir
                save_dir = str(get_default_csv_dir())
            session_meta = {
                "source": self._session_source_name(),
                "session_id": self._session_id,
                # 基线来源与标识（随每行写入，便于追溯衰减量出处）
                **self._baseline_session_meta(),
            }
            try:
                self._recorder.start_recording(
                    params, save_dir, self._config.adc_profile,
                    session_meta=session_meta,
                )
                self._config.last_csv_dir = save_dir
                self._config.save()
                self._lbl_rec.setText("记录中")
                self._badge_rec.set_status("记录: 进行中", "ok")
                self._lbl_rec_file.set_full_text(
                    self._recorder.file_path or ""
                )
                self._log(f"[REC] 开始记录 -> {self._recorder.file_path}")
                self.statusBar().showMessage("记录中…")
                self._btn_rec.setText("停止记录")
                # 危险角色：让按钮视觉上变成"停止"色
                self._btn_rec.setProperty("role", "danger")
                self.style().unpolish(self._btn_rec)
                self.style().polish(self._btn_rec)
                self._btn_rec.update()
                self._rec_start_time = time.monotonic()
            except (OSError, RuntimeError) as e:
                # RuntimeError：底层文件句柄已关闭等运行时失败，
                # 与 OSError 一样必须复位按钮状态，否则两个记录
                # 按钮停留在"记录中"假象
                QMessageBox.critical(self, "记录失败", str(e))
                self._sync_rec_buttons(False)
        else:
            ok, err = self._recorder.stop_recording()
            if not ok:
                self._log(f"[REC] 关闭异常: {err}")
            self._lbl_rec.setText("停止")
            self._badge_rec.set_status("记录: 停止", "neutral")
            self._lbl_rec_file.set_full_text(
                self._recorder.file_path or "未选择"
            )
            self._btn_rec.setText("开始记录")
            self._btn_rec.setProperty("role", "primary")
            self.style().unpolish(self._btn_rec)
            self.style().polish(self._btn_rec)
            self._btn_rec.update()
            if hasattr(self, "_message_bar"):
                self._message_bar.clear()
            self._rec_start_time = 0.0
            self._log("[REC] 停止记录")
            self.statusBar().showMessage("记录已停止")

    def _on_choose_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "选择保存目录", self._config.last_csv_dir or ""
        )
        if d:
            self._config.last_csv_dir = d
            self._config.save()
            self._lbl_rec_file.set_full_text(d)

    def _write_csv_dframe(self, df: DFrame) -> None:
        ok, err = self._recorder.write_dframe(
            df, self._processor,
            invert_sign=self._processor.invert_sign,
            enabled_channels=self._processor.enabled_channels,
            channel_map=self._config.channel_map,
        )
        if not ok:
            self._log(f"[REC] 写失败: {err}")
            self._btn_rec.setChecked(False)
            QMessageBox.critical(
                self, "记录失败", f"无法写入 CSV：{err}\n记录已停止。"
            )

    # ==================================================================
    # 设置
    # ==================================================================
    def _on_open_settings(self) -> None:
        # 标定进行中不得修改通道/滤波/Profile 等设置——
        # 那会改变正在累计样本的语义，污染基线
        if self._calib.is_running():
            QMessageBox.information(
                self, "标定进行中",
                "无钢板基线标定进行中，请先完成或取消标定，再修改设置。",
            )
            return
        # 录制期间锁定会改变测量语义的设置：单个 CSV 必须整份实验条件
        # 一致（布局/Profile/频率/增益/线圈）。显示类参数也一并锁定，
        # 避免"改了但文件里没变"的困惑。
        if self._recorder.is_recording:
            QMessageBox.information(
                self, "记录中，设置已锁定",
                "正在记录数据。为保证同一个 CSV 内实验条件一致，\n"
                "录制期间不允许修改：通道布局、ADC Profile、\n"
                "激励频率/增益/线圈等实验条件，以及处理参数。\n\n"
                "请先停止记录（再次点击“停止记录”）后再修改设置。",
            )
            return
        dlg = SettingsDialog(
            current_profile=self._config.adc_profile,
            enabled_channels=self._config.enabled_channels,
            ema_alpha=self._config.ema_alpha,
            median_window=self._config.median_window,
            heatmap_min=self._config.heatmap_min,
            heatmap_max=self._config.heatmap_max,
            heatmap_auto=self._config.heatmap_auto,
            curve_window_s=self._config.curve_window_s,
            invert_sign=self._config.invert_sign,
            experiment_params=self._config.experiment_params,
            channel_map=self._config.channel_map,
            layout_id=self._config.layout_id,
            cell_binding=self._config.cell_binding,
            binding_confirmed=self._config.binding_confirmed,
            parent=self,
        )
        if dlg.exec_() != dlg.Accepted:
            return
        rv = dlg.result_values
        new_profile = profile_by_name(rv["profile_name"], self._profiles)
        if new_profile.name != self._config.adc_profile.name:
            QMessageBox.information(
                self, "Profile 已切换",
                f"已切换到 {new_profile.display_name}。\n"
                "不同 Profile 的量程/分压比不同，建议重新做无钢板基线标定。"
            )
        # 激励频率/增益等影响测量的实验条件变化 → 提示基线可能失效
        old_exp = self._config.experiment_params
        new_exp = rv["experiment_params"]
        cond_changed = any(
            str(old_exp.get(k, "")) != str(new_exp.get(k, ""))
            for k in ("frequency", "freq_unit", "gain", "coil")
        )
        if cond_changed:
            QMessageBox.information(
                self, "实验条件已修改",
                "激励频率/增益/线圈等实验条件已修改，"
                "现有无钢板基线可能失效；\n"
                "建议移除钢板后重新进行无钢板基线标定。",
            )
        old_profile_name = self._config.adc_profile.name
        self._config.adc_profile = new_profile
        self._config.enabled_channels = rv["enabled_channels"]
        self._config.ema_alpha = rv["ema_alpha"]
        self._config.median_window = rv["median_window"]
        self._config.heatmap_min = rv["heatmap_min"]
        self._config.heatmap_max = rv["heatmap_max"]
        self._config.heatmap_auto = rv["heatmap_auto"]
        self._config.curve_window_s = rv["curve_window_s"]
        self._config.invert_sign = rv["invert_sign"]
        self._config.experiment_params = rv["experiment_params"]

        # 先让 processor 采用新的 Profile/滤波参数，再做基线匹配——
        # 否则匹配用的是旧 Profile，条件判断会错位
        self._processor.set_adc_profile(new_profile)
        self._processor.set_enabled_channels(rv["enabled_channels"])
        self._processor.set_ema_alpha(rv["ema_alpha"])
        self._processor.set_median_window(rv["median_window"])
        self._processor.set_invert_sign(rv["invert_sign"])

        # 布局切换：预设布局由 layout_id 驱动（其 setter 同步 channel_map），
        # "恢复旧面包板布局"按钮作为显式兜底入口。
        # 仅改变显示位置；数据列顺序、基线、CSV 列一律不变。
        old_layout = self._config.layout_id
        old_binding = self._config.cell_binding
        new_layout = str(rv.get("layout_id") or old_layout)
        if rv.get("restore_hw_layout"):
            new_layout = LAYOUT_BREADBOARD
        new_binding = str(rv.get("cell_binding") or old_binding)
        layout_changed = new_layout != old_layout
        if layout_changed:
            self._config.layout_id = new_layout
            self._log(
                f"[CFG] 通道布局切换：{layout_name(old_layout)} → "
                f"{layout_name(self._config.layout_id)}"
                "（仅显示位置；数据列顺序不变，旧基线不再自动套用）"
            )
        elif rv.get("restore_hw_layout"):
            self._config.channel_map = dict(HARDWARE_CHANNEL_MAP)
            self._log("[CFG] 通道物理布局已恢复为旧面包板布局")
        # 线圈绑定：显式保存（有源板两张表冲突，必须由用户确认）
        if new_binding in (BINDING_DESIGN, BINDING_LEGACY_V341,
                           BINDING_IDENTITY):
            self._config.cell_binding = new_binding
        binding_changed = self._config.cell_binding != old_binding
        if new_layout == LAYOUT_ACTIVE_PCB:
            self._config.binding_confirmed = bool(
                rv.get("binding_confirmed")
            )
        else:
            self._config.binding_confirmed = True
        if binding_changed:
            self._log(
                f"[CFG] 线圈绑定切换：{self._config.binding_display_name}"
                f"（{'已实测确认' if self._config.binding_confirmed else '未实测确认'}；"
                "仅影响标签与串口电压序号的对应，数据列顺序不变）"
            )

        # 换板/换绑定/换 Profile 即换实验：清掉上一实验的滤波、当前帧、
        # 曲线与故障状态，避免残留值被误读成新实验数据
        if layout_changed or binding_changed or \
                new_profile.name != old_profile_name:
            self._processor.clear_session_data()
            self._last_dframe = None
            self._device_fault = False
            self._device_init_status = 0
            self._data_timeout_reported = False
            self._data_timeout_reason = ""
            if hasattr(self, "_plot"):
                self._plot.clear()
            self._clear_plot_baselines()
        self._apply_channel_map_to_heatmap()
        # 统一入口重新判定基线适用性（布局/设备/条件任一变化都会
        # 使旧基线不再自动套用，并在界面上给出原因）
        self._apply_scope_baselines()
        self._update_all_displays()
        self._config.save()
        # 演示源跟随新的启用通道集与物理映射（否则演示帧的
        # valid_mask 与处理器启用通道不一致，出现"有帧但不更新"
        # 的通道；钢板空间响应位置也会与实际接线布局脱节）
        if self._demo is not None:
            self._demo.configure(
                enabled_channels=self._processor.enabled_channels,
                map_to_grid=map_to_grid_list(self._config.channel_map),
            )

        self._heatmap_auto = rv["heatmap_auto"]
        self._heatmap_manual_min = rv["heatmap_min"]
        self._heatmap_manual_max = rv["heatmap_max"]
        self._heatmap_range_max = max(
            abs(self._heatmap_manual_min),
            abs(self._heatmap_manual_max),
            1.0,
        )
        self._plot.set_window(rv["curve_window_s"])
        self._apply_config_to_ui()
        self._apply_channel_map_to_heatmap()
        # 回放中应用设置不得丢弃"记录时布局"显示：上面已按实时
        # 配置重设热力图，需立即重放记录映射，否则布局显示与
        # _replay_using_recorded_map 标志及消息栏标注失同步
        if self._replay_using_recorded_map:
            self._apply_replay_recorded_map()
        self._select_channel(self._selected_channel)
        self._log(f"[CFG] 设置已应用: profile={new_profile.name}")
        self._update_all_displays()

    # ==================================================================
    # 回放
    # ==================================================================
    def _on_replay_open(self) -> None:
        if self._replay is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "选择回放 CSV",
            self._config.last_csv_dir or "",
            "CSV 文件 (*.csv);;所有文件 (*)",
        )
        if not path:
            return
        self._clear_array_analysis_frame()
        try:
            self._replay.load(path)
        except CsvSchemaError as e:
            QMessageBox.critical(self, "回放加载失败", str(e))
            return
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "回放加载失败", str(e))
            return
        self._array_replay_revision += 1
        self._replay_controls.set_loaded(self._replay.total_rows)
        self._replay_controls.set_open_path(path)
        self._log(
            f"[REPLAY] 已加载: {path} "
            f"({self._replay.total_rows} 行, schema=v{self._replay.schema_version})"
        )
        self._apply_replay_recorded_map()
        self._apply_replay_experiment_context()
        # （重新）加载 = 新回放会话：清理处理器会话数据
        # （滤波/回放派生覆盖/曲线）与帧计数——已在回放模式下
        # 再打开第二个 CSV 时不经过 _set_app_mode，必须在此复位，
        # 否则旧文件/旧来源的残留混入新回放
        self._processor.clear_session_data()
        self._last_replay_raw_valid = [0] * 16
        self._frame_count = 0
        self._error_count = 0
        self._dropped_frames = 0
        self._frame_timestamps.clear()
        self._plot.clear()
        self._clear_plot_baselines()
        self._update_all_displays()
        self._set_source_state(SourceState.REPLAY)
        self._sync_active_task_context()

    def _apply_replay_recorded_map(self) -> None:
        """回放布局：文件带有效记录映射 → 默认按"记录时布局"显示；
        缺失/非法 → 按当前硬件布局显示并明确告知（不假称已恢复
        历史物理位置）。临时映射只作用于热力图显示，绝不写入
        实时配置；退出回放时由 _set_app_mode 恢复当前硬件布局。
        """
        if self._replay is None:
            return
        # 先清除上一个文件可能留下的临时布局，回到当前硬件布局基准，
        # 否则"换文件后新文件无需临时映射"时旧布局会滞留；同时撤下
        # 旧的"记录时布局"标注（新文件若需要会在下方重新标注）
        if self._replay_using_recorded_map:
            self._replay_using_recorded_map = False
            self._apply_channel_map_to_heatmap()
            if hasattr(self, "_message_bar"):
                self._message_bar.clear()
        rec_map = getattr(self._replay, "recorded_channel_map", None)
        cur_map = self._config.channel_map
        if rec_map is not None:
            if maps_equal(rec_map, cur_map):
                # 记录布局与当前布局一致：无需临时切换
                self._log("[REPLAY] 记录时布局与当前硬件布局一致")
            else:
                # 回放必须同时还原"坐标 + 格子标签 + 详情/图例"：
                # 只换坐标会让记录时的 CH04 格子被按索引错标成 CH12
                rec_labels = labels_for_map(rec_map)
                self._channel_labels = dict(rec_labels)
                self._inspection_panel.set_channel_labels(rec_labels)
                self._compensation.set_channels(rec_map, rec_labels)
                detected = binding_for_map(rec_map)
                self._binding_label = binding_name(
                    detected or self._config.cell_binding
                )
                self._heatmap.set_channel_map(
                    dict(rec_map), labels=rec_labels
                )
                if hasattr(self, "_plot"):
                    self._plot.set_channel_labels(rec_labels)
                self._replay_using_recorded_map = True
                self._sync_selected_channel_position()
                msg = (
                    "热力图按【记录时布局】显示（仅单元格位置不同，"
                    "通道数据/基线/CSV 列均未重排）；退出回放后恢复"
                    "当前硬件布局。"
                )
                self._log(f"[REPLAY] {msg}")
                if hasattr(self, "_message_bar"):
                    self._message_bar.show_message(msg, "info")
        else:
            # 文件无（有效）映射信息：保持当前布局并明确告知
            msg = (
                "该文件不含有效的通道映射信息，热力图按当前硬件布局"
                "显示（未恢复历史物理位置）。"
            )
            self._log(f"[REPLAY] {msg}")
            if hasattr(self, "_message_bar"):
                self._message_bar.show_message(msg, "warning")

    def _apply_replay_experiment_context(self) -> None:
        """回放时按**记录时**的 ADC Profile 计算历史 VOUT。

        - 记录里有可识别 Profile → 用它的分压比（旧 1:2 文件里的
          ADC 1000 mV 应显示 VOUT 2000 mV，与当前实时直连配置无关）；
        - 记录缺失/Profile 不可识别 → VOUT 显示为未知，不按当前
          配置臆测；
        - ADC mV、已保存的衰减量/百分比一律保持记录值不变。
        同时提示文件内的布局/Profile 冲突（旧程序可能逐行改设置）。
        """
        if self._replay is None:
            return
        name = str(getattr(self._replay, "recorded_profile_name", "") or "")
        divider = None
        unknown = True
        if name and name in self._profiles:
            divider = float(self._profiles[name].detector_divider)
            unknown = False
        self._processor.set_detector_divider_override(divider, unknown=unknown)

        msgs = []
        if getattr(self._replay, "layout_conflict", False):
            msgs.append(
                "该 CSV 内混用了多种通道布局（录制中被改过布局）："
                "热力图按首行布局显示，通道数据仍按 CH01–CH16 列序解析；"
                "如需精确定位请按对应时段分段查看。"
            )
        if getattr(self._replay, "profile_conflict", False):
            msgs.append(
                "该 CSV 内 ADC Profile 前后不一致：VOUT 按首行 Profile 估算，"
                "其余时段可能不准，请核对原实验记录。"
            )
        if unknown:
            msgs.append(
                "该记录未标注可识别的 ADC Profile：VOUT 显示为未知"
                "（ADC mV 与已保存衰减量不受影响）；如需 VOUT 请核对"
                "原实验记录确认分压比。"
            )
        if name:
            self._log(
                f"[REPLAY] 记录时 ADC Profile: {name}"
                + (f"（分压比 {divider:g}×）" if divider is not None else
                   "（不可识别）")
            )
        if msgs:
            msg = " ".join(msgs)
            self._log(f"[REPLAY] {msg}")
            if hasattr(self, "_message_bar"):
                self._message_bar.show_message(msg, "warning")

    def _on_replay_play(self) -> None:
        if self._replay is None or self._replay.total_rows == 0:
            return
        if self._replay.state == "ended":
            self._replay.seek(0)
        self._replay.play()

    def _on_replay_pause(self) -> None:
        if self._replay is None:
            return
        self._replay.pause()

    def _on_replay_stop(self) -> None:
        if self._replay is None:
            return
        self._replay.stop()
        self._replay_controls.set_index(None)
        # 保持 REPLAY 源状态：停止回放 ≠ 退出回放模式——
        # 回放模式下连接/演示/记录按钮的禁用逻辑仍然成立，
        # 退出回放只能经由模式切换（_set_app_mode）

    def _on_replay_state(self, state: str) -> None:
        self._replay_controls.set_state(state)
        if state == "stopped":
            self._clear_array_analysis_frame()
        elif state in ("paused", "ended") and self._task_mode == "plate" and self._array_analysis is not None:
            self._array_analysis.hold_frame()
        if state == "playing":
            self._lbl_state.setText("回放中")
            set_status_role(self._lbl_state, "ok")
        elif state == "paused":
            self._lbl_state.setText("回放已暂停")
            set_status_role(self._lbl_state, "warning")
        elif state == "stopped":
            self._lbl_state.setText("回放已停止")
            set_status_role(self._lbl_state, "neutral")
        elif state == "ended":
            self._lbl_state.setText("回放结束")
            set_status_role(self._lbl_state, "neutral")

    def _on_replay_seek(self, index: int) -> None:
        if self._replay is None:
            return
        self._clear_array_analysis_frame()
        self._replay.seek(index)
        self._replay_controls.set_index(index)

    def _on_replay_speed(self, speed: float) -> None:
        if self._replay is None:
            return
        self._replay.set_speed(speed)

    def _show_replay_task_frame(self, row) -> None:
        if self._task_mode == "plate":
            all_valid = len(row.raw_valid) == MAX_CHANNEL_COUNT and all(valid == 1 for valid in row.raw_valid)
            self._send_array_analysis_frame(row.wire_mv if all_valid else (), row.status, row.valid_mask)
        raw = usable_samples(row.wire_mv, row.status, row.valid_mask, row.raw_valid)
        self._dispatch_workflow_frame(row.t_rel, raw, row.position_mm)

    def _on_replay_frame(self, row) -> None:
        """从 CSV 行直接写入 processor，不走协议解析路径。

        衰减量/下降比例直接使用文件中保存的值（use_replay_derived）：
        不重算、不做符号翻转；v3 文件没有这两列 → None（未知，显示"—"）。
        回放只读：不会把历史基线保存成当前硬件基线。
        """
        if self._app_mode != "replay":
            return
        self._last_replay_task_row = row
        self._show_replay_task_frame(row)
        n = self._replay.n_channels if self._replay else 16
        self._last_replay_raw_valid = [0] * 16
        for c in range(min(n, 16)):
            try:
                status = (
                    ChannelStatus(row.ch_status[c])
                    if 0 <= row.ch_status[c] < len(ChannelStatus.__members__)
                    else ChannelStatus.OK
                )
            except Exception:
                status = ChannelStatus.OK
            st = self._processor.get(c)
            # 回放数据单位与实时一致：mV（v3/v4 CSV）；
            # 空单元格 = 未知 → None（UI 显示"—"），绝不落成 0.0
            if c < len(row.wire_mv):
                v = row.wire_mv[c]
                st.sample_mv = float(v) if v is not None else None
            st.filtered_mv = row.filtered[c] if c < len(row.filtered) else None
            st.baseline_mv = row.baseline[c] if c < len(row.baseline) else None
            st.set_status(status)
            st.last_update_monotonic = time.monotonic()
            if c < len(row.raw_valid):
                self._last_replay_raw_valid[c] = int(row.raw_valid[c])
            # 回放派生量覆盖（v4 保存值 / v3 为 None=未知）
            att = row.attenuation[c] if c < len(row.attenuation) else None
            pct = row.drop_pct[c] if c < len(row.drop_pct) else None
            st.set_replay_derived(att, pct)
        # 选中通道的曲线：X 轴使用文件实验时间（t_rel）——回放速度
        # 只影响调度不影响 X 坐标；暂停不产生假实验时间；seek 回退
        # 由 PlotWidget 检测时间倒退并清窗重建（绝不倒序连线）。
        # 数值走统一快照门槛（与实时同一规则）：无效行的采样/滤波
        # 为 None → NaN 断点——旧 CSV 的历史残留值不得显示成当前
        # 有效值，也不跳过（跳过会把无效段连成看似连续的线）
        t_row = getattr(row, "t_rel", None)
        if self._selected_channel is not None:
            c = self._selected_channel
            snap = self._processor.measurement(c, bool(
                self._last_replay_raw_valid[c]
            ))
            self._plot.add_point(
                c, snap.sample_mv, snap.filtered_mv, t=t_row,
            )
            self._plot.add_attenuation_point(
                c, snap.attenuation_mv, t=t_row,
            )
        # 基线参考线跟随回放行写入的基线
        self._sync_plot_baseline_lines()
        # 帧计数
        self._frame_count += 1
        if self._replay is not None:
            self._replay_controls.set_index(self._replay.current_index)
        self._update_heatmap_auto_range()

    # ==================================================================
    # 周期刷新
    # ==================================================================
    def _compute_fps(self) -> float:
        if len(self._frame_timestamps) < 2:
            return 0.0
        ts = list(self._frame_timestamps)
        dt = ts[-1] - ts[0]
        if dt <= 0:
            return 0.0
        return (len(ts) - 1) / dt

    def _on_refresh(self) -> None:
        fps = self._compute_fps()
        # 记录徽标附带时长
        if self._recorder.is_recording and self._rec_start_time > 0:
            elapsed = int(time.monotonic() - self._rec_start_time)
            mm = elapsed // 60
            ss = elapsed % 60
            self._badge_rec.set_status(
                f"记录: {mm:02d}:{ss:02d}", "ok"
            )
        self._lbl_fps.setText(f"{fps:.2f} fps")
        self._lbl_valid.setText(f"有效 {self._frame_count}")
        self._lbl_invalid.setText(f"无效 {self._error_count}")
        self._lbl_dropped.setText(f"扫描缺口 {self._scan_gaps}")
        self._lbl_dropped.setToolTip(
            f"本连接段扫描号连续性：\n"
            f"未观测扫描 {self._scan_gaps} 次（≠ 无线丢包率）\n"
            f"回绕 {self._scan_wraps} 次 / 设备重启 {self._scan_restarts} 次 / "
            f"重复 {self._scan_duplicates} 次 / 倒序 {self._scan_reorders} 次"
        )

        self._check_data_freshness()
        dialog = self._sync_array_analysis_context() if self._task_mode == "plate" else None
        if dialog is not None and self._app_mode == "live" and self._data_timeout_reported:
            dialog.clear_frame()
        inspection = self._sync_inspection_context()
        if self._app_mode == "live" and self._data_timeout_reported and self._latest_workflow_packet is not None:
            self._latest_workflow_packet = None
            self._latest_compensated = None
            self._compensation.reset_stream()
            if inspection is not None:
                inspection.lose_frame()
            self._render_compensation_grid()
        if self._calib.is_running():
            self._btn_calib.setEnabled(False)
            self._btn_calib_cancel.setEnabled(True)
        else:
            # 回放模式下永远不可标定（标定永远收不到实时帧，
            # 只会误报断流）；即使源状态滞留也要排除
            self._btn_calib.setEnabled(
                self._app_mode != "replay"
                and self._source_state
                in (SourceState.SERIAL_CONNECTED, SourceState.DEMO)
            )
            self._btn_calib_cancel.setEnabled(False)

        self._update_all_displays()

    def _check_data_freshness(self) -> None:
        if self._source_state != SourceState.SERIAL_CONNECTED:
            self._data_timeout_reported = False
            self._data_timeout_reason = ""
            return
        now = time.monotonic()
        if self._segment_start_time <= 0:
            self._segment_start_time = now
        if self._session_start_time <= 0:
            self._session_start_time = now
        # 段起点取"连接段起点"与"会话起点"中较早者：正常两者一致；
        # 兼容既有按 _session_start_time 判断健康度的调用路径。
        since_segment = now - min(
            self._segment_start_time, self._session_start_time
        )
        link = "TCP 链路" if self._transport == TRANSPORT_TCP else "串口"

        # 1) 完全没有任何字节（连接在，但设备不发数据）
        if (
            self._segment_byte_count == 0
            and since_segment > NO_BYTES_TIMEOUT_S
            and not self._data_timeout_reported
        ):
            self._data_timeout_reported = True
            self._data_timeout_reason = "no_bytes"
            self._log(
                f"[TIMEOUT] {link}本连接段 {NO_BYTES_TIMEOUT_S:.0f}s "
                f"未收到任何字节"
            )
            return

        # 2) 有字节但本段从未出现合法 D 帧 → 协议失配
        #    段内计数：首次连接与"断线重连后持续乱码"都必须能触发
        if (
            self._segment_dframe_count == 0
            and self._segment_byte_count > 0
            and since_segment > PROTOCOL_MISMATCH_TIMEOUT_S
            and not self._data_timeout_reported
        ):
            self._data_timeout_reported = True
            self._data_timeout_reason = "parse"
            self._log(
                f"[TIMEOUT] 本连接段 {PROTOCOL_MISMATCH_TIMEOUT_S:.0f}s 内"
                f"收到 {self._segment_byte_count} 字节但无合法 D 帧"
                f"（可能协议不匹配或线路干扰）"
            )
            return

        # 3) 本段曾正常收帧、但最近停更 → 数据陈旧
        if (
            self._segment_dframe_count > 0
            and self._segment_last_dframe_time > 0
            and (now - self._segment_last_dframe_time) > DATA_TIMEOUT_S
            and not self._data_timeout_reported
        ):
            self._data_timeout_reported = True
            self._data_timeout_reason = "stale"
            self._log(
                f"[TIMEOUT] 本连接段已 {DATA_TIMEOUT_S:.0f}s 未收到新 D 帧"
                f"（E 心跳/错误帧不算数据）"
            )

    def _update_all_displays(self) -> None:
        # 统一测量快照：详情、热力图、tooltip、曲线、CSV 同源同公式
        for c in range(16):
            st = self._processor.get(c)
            enabled = c in self._processor.enabled_channels
            frame_valid = self._frame_valid_for(c)
            snap = self._processor.measurement(c, frame_valid)
            extras = {
                "sample_mv": snap.sample_mv,
                "filtered_mv": snap.filtered_mv,
                "baseline_mv": snap.baseline_mv,
                "drop_pct": snap.detector_drop_pct,
                "vadc_mv": snap.sample_mv,
                "detector_mv": snap.detector_vout_mv,
                "valid": frame_valid,
                "last_update_monotonic": st.last_update_monotonic,
            }
            self._heatmap.update_cell(
                c, snap.attenuation_mv, snap.status, enabled, extras
            )
        if self._selected_channel is not None:
            c = self._selected_channel
            st = self._processor.get(c)
            self._channel_panel.update_snapshot(
                c,
                self._processor.measurement(c, self._frame_valid_for(c)),
                last_update_monotonic=st.last_update_monotonic,
                label=self._channel_label(c),
                scan_number=self._current_scan_number(),
                valid_bit=self._frame_valid_bit(c),
                binding_label=self._binding_label,
            )
        if self._source_state == SourceState.SERIAL_CONNECTED:
            if self._tcp_idle_timeout_reported:
                # TCP 读取超时不等于断线：连接仍在，只是没有数据
                self._lbl_state.setText("数据超时（连接保持）")
                set_status_role(self._lbl_state, "warning")
            elif self._data_timeout_reported:
                if self._data_timeout_reason == "no_bytes":
                    self._lbl_state.setText("无数据")
                    set_status_role(self._lbl_state, "danger")
                elif self._data_timeout_reason == "parse":
                    self._lbl_state.setText("协议不匹配")
                    set_status_role(self._lbl_state, "warning")
                else:
                    self._lbl_state.setText("数据陈旧")
                    set_status_role(self._lbl_state, "warning")
            elif self._device_fault:
                self._lbl_state.setText(
                    f"设备故障 (INIT={self._device_init_status})"
                )
                set_status_role(self._lbl_state, "danger")
            elif self._segment_dframe_count == 0 \
                    and self._segment_eframe_count > 0:
                # 只有 E 心跳、没有一帧 D：设备在线但未就绪（区别于断线）
                self._lbl_state.setText("仅心跳（设备未就绪）")
                set_status_role(self._lbl_state, "warning")
            elif self._segment_dframe_count > 0:
                self._lbl_state.setText("接收中")
                set_status_role(self._lbl_state, "ok")

    def _log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self._last_diag_lines.append(msg)
        # 启动早期（UI 尚未构建）也会调用本方法：日志控件缺失时只打印，
        # 绝不能因为一条提示而让主窗口构造失败
        view = getattr(self, "_log_view", None)
        if view is None:
            try:
                print(f"{ts}  {msg}")
            except Exception:
                pass
            return
        view.appendPlainText(f"{ts}  {msg}")
        # 自动滚动
        if (not hasattr(self, "_chk_log_autoscroll")
                or self._chk_log_autoscroll.isChecked()):
            sb = view.verticalScrollBar()
            sb.setValue(sb.maximum())

    # ==================================================================
    # 关闭
    # ==================================================================
    def closeEvent(self, event) -> None:
        self._refresh_timer.stop()
        if self._array_analysis is not None:
            self._array_analysis.set_workspace_active(False)
            self._array_analysis.clear_frame()
        self._calib.cancel()
        self._stop_all_sources()
        if self._recorder.is_recording:
            self._recorder.stop_recording()
        self._config.save()
        super().closeEvent(event)
