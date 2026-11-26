"""
配置管理：保存 ADC Profile、参考电压、激活通道、通道映射、基线、滤波参数等。
版本号 + 数据校验。损坏配置走安全默认值。

v4：基线按数据来源分作用域保存（baseline_sets），演示标定不得覆盖
真实硬件基线，回放也不得把历史基线写成当前硬件基线：
- "serial" 作用域：USB CDC 串口链路的无钢板基线
- "tcp"    作用域：Hi3863 无线 TCP 链路的无钢板基线（与串口隔离，
  切换传输方式绝不静默套用另一条链路的基线）
- "demo"   作用域：演示（模拟数据源）的无钢板基线
每套基线附带 meta：来源、设备标识（USB VID:PID+序列号，不只看
COM 口号）、单位、通道、标定时间、ADC/检波配置、激励频率与增益、
baseline_id、衰减公式版本。切换来源时由 UI 按作用域取用。

旧配置（v3 及更早的扁平 "baselines" 字段，无来源信息）加载时备份到
legacy_baselines_backup 并作废，标记"待确认/需重新标定"——绝不静默
把无来源的旧基线当成当前硬件基线。
"""

from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from models.adc_profile import AdcProfile, build_default_profiles, profile_by_name

from core.channel_layout import (
    BINDING_DESIGN,
    BINDING_LEGACY_V341,
    CELL_BINDINGS,
    HARDWARE_CHANNEL_MAP,
    LAYOUT_ACTIVE_PCB,
    LAYOUT_BREADBOARD,
    LAYOUT_CUSTOM,
    LAYOUT_PRESETS,
    LEGACY_ACTIVE_PCB_MAP,
    LEGACY_ROW_MAJOR_MAP,
    MAPPING_VERSION_CURRENT,
    binding_for_map,
    binding_name,
    data_position_map,
    default_binding,
    is_valid_map,
    layout_id_for_map,
    layout_map,
    maps_equal,
    normalize_channel_map,
)


CONFIG_VERSION = 4

# v3 起测量与基线单位固定为 mV
MEASUREMENT_UNIT = "mV"
BASELINE_UNIT = "mV"

# 基线作用域
# serial：USB CDC 串口链路；tcp：Hi3863 无线 TCP 链路；demo：演示数据源。
# USB 与无线链路各自持有独立基线作用域——切换传输方式时绝不静默套用
# 另一条链路的基线（同一次标定只对当次链路有效）。
SCOPE_SERIAL = "serial"
SCOPE_TCP = "tcp"
SCOPE_DEMO = "demo"
BASELINE_SCOPES = (SCOPE_SERIAL, SCOPE_TCP, SCOPE_DEMO)

# 传输方式（数据源接入方式）
TRANSPORT_SERIAL = "serial"
TRANSPORT_TCP = "tcp"
TRANSPORTS = (TRANSPORT_SERIAL, TRANSPORT_TCP)

# TCP 监听默认值（上位机监听，Hi3863 作为客户端连入）
DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = 8080
MAX_DEVICE_ID_LEN = 64


# 通道到物理位置 (row, col) 的映射。键：channel (int)；值：[row, col]
# 默认：实际接线布局（唯一定义在 core.channel_layout，此处仅做类型
# 转换引用，禁止再写一套行优先默认）。
DEFAULT_CHANNEL_MAP: Dict[int, List[int]] = {
    ch: [r, c] for ch, (r, c) in HARDWARE_CHANNEL_MAP.items()
}


def _empty_baseline_set() -> dict:
    return {
        "meta": None,
        "baselines": {str(c): None for c in range(16)},
    }


def default_config(profiles: Dict[str, AdcProfile]) -> dict:
    return {
        "version": CONFIG_VERSION,
        "adc_profile": "ads1115_4v096_div2",
        "vref_mv": 4096,
        "enabled_channels": list(range(16)),
        "channel_map": DEFAULT_CHANNEL_MAP,
        # 布局标识：只影响"通道号 → 热力图位置"的显示映射，
        # 与 ADC 分压比（adc_profile）完全独立管理。
        # 旧配置无此字段时由 channel_map 反推（默认旧面包板）。
        "layout_id": LAYOUT_BREADBOARD,
        # 线圈标签 ↔ 串口电压序号的绑定（有源板有两张冲突表，必须显式选择）
        "cell_binding": "",
        # 绑定是否已用逐点实测确认（未确认时界面持续提示）
        "binding_confirmed": False,
        # 映射 schema 标记（独立于 version：bump version 会作废 v4 基线）
        "mapping_version": MAPPING_VERSION_CURRENT,
        "measurement_unit": MEASUREMENT_UNIT,
        "baseline_unit": BASELINE_UNIT,
        "baseline_sets": {s: _empty_baseline_set() for s in BASELINE_SCOPES},
        # 基线历史库：同一来源下按
        #   （设备标识 + 通道布局 + 测量条件：ADC Profile/频率/增益/线圈）
        # 分别保存，换板/换实验条件互不覆盖。
        # 旧配置只有 baseline_sets（每来源一个槽），加载时自动迁移进来。
        "baseline_entries": [],
        "ema_alpha": 0.2,
        "median_window": 3,
        "heatmap_min": -250.0,
        "heatmap_max": 250.0,
        "heatmap_auto": True,
        "curve_window_s": 30.0,
        "curve_max_channels": 4,
        "experiment_params": {
            "frequency": 1000,
            "freq_unit": "kHz",
            "gain": "1",
            "coil": "默认",
            "plate": "",
            "note": "",
        },
        "last_csv_dir": "",
        "last_serial": {
            "port": "",
            "baudrate": 115200,
        },
        # 传输方式与 TCP 监听参数（与 last_serial 并存，互不覆盖：
        # 旧配置只有 last_serial 时 transport 保持 "serial"，兼容旧版本）
        "transport": TRANSPORT_SERIAL,
        "tcp": {
            "listen_host": DEFAULT_LISTEN_HOST,
            "listen_port": DEFAULT_LISTEN_PORT,
            # TCP keepalive：用于发现"客户端突然掉电（半开连接）"。
            # 只是链路层探活，绝不会因短暂无业务数据而断开客户端；
            # 探活失败也只是提示/可手动清除，不冒充确定的业务断线。
            "keepalive_enabled": True,
            "keepalive_idle_s": 60,
            "keepalive_interval_s": 10,
            "keepalive_count": 3,
        },
        # 采集设备编号：持久化、可配置的硬件身份，用于基线匹配。
        # 绝不使用客户端动态 IP / 源端口 / PC 监听地址作为身份。
        "device_id": "",
        "invert_sign": False,
    }


def _safe_float(v: Any, default: float) -> float:
    try:
        x = float(v)
        if not math.isfinite(x):
            return default
        return x
    except (TypeError, ValueError):
        return default


def _safe_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _safe_bool(v: Any, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if v in (0, "0", "false", "False", "no"):
        return False
    if v in (1, "1", "true", "True", "yes"):
        return True
    return default


def _validate_baseline_map(raw: Any) -> Dict[int, Optional[float]]:
    """校验 {ch: mV|None} 映射；非法项置 None。"""
    out: Dict[int, Optional[float]] = {}
    if not isinstance(raw, dict):
        return {i: None for i in range(16)}
    for k, v in raw.items():
        try:
            ch = int(k)
        except (TypeError, ValueError):
            continue
        if not (0 <= ch < 16):
            continue
        if v is None:
            out[ch] = None
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            out[ch] = None
            continue
        if not math.isfinite(fv):
            out[ch] = None
            continue
        if abs(fv) > 1e6:  # 合理范围之外视为无效
            out[ch] = None
            continue
        out[ch] = fv
    for i in range(16):
        out.setdefault(i, None)
    return out


def _validate_meta(raw: Any) -> Optional[dict]:
    """校验基线 meta；结构不合格返回 None（视为无基线）。"""
    if not isinstance(raw, dict):
        return None
    source = str(raw.get("source", ""))
    if source not in BASELINE_SCOPES:
        return None
    unit = str(raw.get("unit", ""))
    if unit != BASELINE_UNIT:
        return None  # 非 mV 基线不得进入 mV 空间
    channels = raw.get("channels", [])
    if not isinstance(channels, list):
        channels = []
    channels = sorted({
        int(c) for c in channels
        if isinstance(c, (int, float)) and 0 <= int(c) < 16
    })
    return {
        "source": source,
        "device_id": str(raw.get("device_id", "") or ""),
        "unit": unit,
        # 标定时的通道布局（新有源板与旧面包板校准必须隔离）。
        # 旧基线无此字段 → 视为旧面包板布局（当时的实际布局）。
        "layout": str(raw.get("layout", "") or ""),
        "channels": channels,
        "calibrated_at": str(raw.get("calibrated_at", "") or ""),
        "adc_profile": str(raw.get("adc_profile", "") or ""),
        "detector_divider": _safe_float(
            raw.get("detector_divider", 1.0), 1.0),
        "frequency": _safe_float(raw.get("frequency", 0.0), 0.0),
        "freq_unit": str(raw.get("freq_unit", "") or ""),
        "gain": str(raw.get("gain", "") or ""),
        "coil": str(raw.get("coil", "") or ""),
        "baseline_id": str(raw.get("baseline_id", "") or ""),
        "formula": str(raw.get("formula", "") or ""),
    }


class ConfigManager:
    """配置文件管理器。"""

    def __init__(self, config_path: Optional[str] = None) -> None:
        self._path = config_path or self._default_path()
        self._profiles = build_default_profiles()
        self._data: dict = default_config(self._profiles)
        # 旧基线被作废时为 True（UI 据此提示重新标定）
        self._baselines_invalidated: bool = False
        # 通道映射迁移说明（本次加载发生过迁移/回退时非空，UI 据此提示）
        self._map_migration_notice: str = ""
        # 有源板"格子→数据通道"绑定是否在本次加载中被修正
        self._binding_migration_notice: str = ""
        self._load()

    @staticmethod
    def _default_path() -> str:
        from app_paths import get_config_path
        return str(get_config_path())

    @property
    def path(self) -> str:
        return self._path

    @property
    def data(self) -> dict:
        return self._data

    @property
    def adc_profile(self) -> AdcProfile:
        name = self._data.get("adc_profile", "ads1115_4v096_div2")
        return profile_by_name(name, self._profiles)

    @adc_profile.setter
    def adc_profile(self, profile: AdcProfile) -> None:
        self._data["adc_profile"] = profile.name

    @property
    def vref_mv(self) -> int:
        return _safe_int(self._data.get("vref_mv", 4096), 4096)

    @vref_mv.setter
    def vref_mv(self, v: int) -> None:
        self._data["vref_mv"] = int(v)

    @property
    def enabled_channels(self) -> List[int]:
        v = self._data.get("enabled_channels", [0, 1, 4, 5])
        if not isinstance(v, list):
            return [0, 1, 4, 5]
        out = []
        for x in v:
            try:
                ci = int(x)
            except (TypeError, ValueError):
                continue
            if 0 <= ci < 16 and ci not in out:
                out.append(ci)
        return sorted(out)

    @enabled_channels.setter
    def enabled_channels(self, channels: List[int]) -> None:
        clean = sorted({int(c) for c in channels if 0 <= int(c) < 16})
        self._data["enabled_channels"] = clean

    @property
    def channel_map(self) -> Dict[int, tuple]:
        """生效的通道物理映射。

        只接受完整合法映射（16 通道、16 个唯一坐标，见
        core.channel_layout.validate_channel_map）；缺失/部分/重叠/
        越界的存贮值一律回退硬件默认映射——禁止静默补全或产生重叠。
        """
        norm = normalize_channel_map(self._data.get("channel_map"))
        if is_valid_map(norm):
            return norm
        return dict(HARDWARE_CHANNEL_MAP)

    @channel_map.setter
    def channel_map(self, mapping: Dict[int, tuple]) -> None:
        self._data["channel_map"] = {
            str(int(k)): [int(v[0]), int(v[1])] for k, v in mapping.items()
        }
        # 映射变化时同步布局标识（预设可识别，否则为自定义）——
        # 保证 UI 显示的布局与真正生效的映射永远一致
        self._data["layout_id"] = layout_id_for_map(mapping)

    @property
    def layout_id(self) -> str:
        """当前通道布局标识（breadboard_hw_v1 / active_pcb_v1 / custom）。

        **映射是唯一事实来源**：始终由 channel_map 反推。
        这样旧配置（没有 layout_id 字段）、手工改过映射、或文件里
        layout_id 与映射不一致时，UI 显示的布局永远等于实际生效的
        布局，不会出现"标签说有源板、实际按面包板画"的错位。
        """
        return layout_id_for_map(self.channel_map)

    @layout_id.setter
    def layout_id(self, layout_id: str) -> None:
        lid = str(layout_id or "")
        if lid == LAYOUT_CUSTOM:
            # 自定义：保留现有映射，仅标记
            self._data["layout_id"] = LAYOUT_CUSTOM
            return
        if lid not in LAYOUT_PRESETS:
            # 未知标识：保持现状，按映射反推（不静默改数据）
            self._data["layout_id"] = layout_id_for_map(self.channel_map)
            return
        if lid == LAYOUT_ACTIVE_PCB:
            # 切到有源板：只有"已显式保存过有源板绑定"才沿用，
            # 否则用设计对应（绝不静默套用 v3.4.1 旧表）
            stored = str(self._data.get("cell_binding", "") or "")
            binding = (
                stored
                if stored in (BINDING_DESIGN, BINDING_LEGACY_V341)
                else default_binding(lid)
            )
        else:
            binding = default_binding(lid)
        self._data["cell_binding"] = binding
        self._data["channel_map"] = {
            str(int(k)): [int(v[0]), int(v[1])]
            for k, v in data_position_map(lid, binding).items()
        }
        self._data["layout_id"] = lid

    @property
    def cell_binding(self) -> str:
        """线圈标签 ↔ 串口电压序号的绑定。

        存储值优先；旧配置没有该字段时按 channel_map 反查；都无法确定
        时用布局默认值（有源板 = PCB 设计对应）。
        """
        stored = str(self._data.get("cell_binding", "") or "")
        if stored in CELL_BINDINGS:
            return stored
        derived = binding_for_map(self.channel_map)
        if derived:
            return derived
        return default_binding(self.layout_id)

    @cell_binding.setter
    def cell_binding(self, binding_id: str) -> None:
        bid = str(binding_id or "")
        if bid not in CELL_BINDINGS:
            return
        lid = self.layout_id
        self._data["cell_binding"] = bid
        if lid in LAYOUT_PRESETS:
            self._data["channel_map"] = {
                str(int(k)): [int(v[0]), int(v[1])]
                for k, v in data_position_map(lid, bid).items()
            }

    @property
    def binding_display_name(self) -> str:
        return binding_name(self.cell_binding)

    @property
    def binding_confirmed(self) -> bool:
        """绑定是否已用逐点实测确认（仅提示用，不改变数据）。"""
        return _safe_bool(
            self._data.get("binding_confirmed", False), False
        )

    @binding_confirmed.setter
    def binding_confirmed(self, value: bool) -> None:
        self._data["binding_confirmed"] = bool(value)

    @property
    def layout_display_name(self) -> str:
        from core.channel_layout import layout_name
        return layout_name(self.layout_id)

    def data_labels(self) -> Dict[int, str]:
        """当前布局+绑定下，数据通道 → 线圈 CH 标签。"""
        from core.channel_layout import data_label_map
        return data_label_map(self.layout_id, self.cell_binding)

    @property
    def measurement_unit(self) -> str:
        return str(self._data.get("measurement_unit", MEASUREMENT_UNIT))

    @property
    def baseline_unit(self) -> str:
        return str(self._data.get("baseline_unit", BASELINE_UNIT))

    @property
    def baselines_invalidated(self) -> bool:
        """旧基线是否已在加载时作废（待确认/需重新标定）。"""
        return self._baselines_invalidated

    @property
    def baseline_notice(self) -> str:
        """基线作废时的用户提示（未作废为空串）。"""
        if self._baselines_invalidated:
            return (
                "旧配置中的基线缺少来源信息，已备份并标记为待确认；"
                "请重新进行无钢板基线标定。"
            )
        return ""

    @property
    def map_migration_notice(self) -> str:
        """通道物理映射迁移/回退说明（本次加载无变化为空串）。"""
        return self._map_migration_notice

    # ------------------------------------------------------------------
    # 基线（v4：按来源作用域 + meta）
    # ------------------------------------------------------------------
    def _baseline_set(self, scope: str) -> dict:
        sets = self._data.get("baseline_sets")
        if not isinstance(sets, dict):
            sets = {}
            self._data["baseline_sets"] = sets
        s = sets.get(scope)
        if not isinstance(s, dict):
            s = _empty_baseline_set()
            sets[scope] = s
        return s

    # -- 基线历史库（按来源 + 设备 + 布局 + 测量条件）------------------
    @staticmethod
    def _entry_identity(meta: dict) -> tuple:
        """基线条目的身份键（同键视为同一次标定，可覆盖）。"""
        return (
            str(meta.get("source", "")),
            str(meta.get("device_id", "") or ""),
            str(meta.get("layout", "") or LAYOUT_BREADBOARD),
            str(meta.get("adc_profile", "") or ""),
            float(meta.get("frequency", 0.0) or 0.0),
            str(meta.get("freq_unit", "") or ""),
            str(meta.get("gain", "") or ""),
            str(meta.get("coil", "") or ""),
        )

    def _entries(self) -> List[dict]:
        raw = self._data.get("baseline_entries")
        if not isinstance(raw, list):
            raw = []
            self._data["baseline_entries"] = raw
        clean: List[dict] = []
        for e in raw:
            if not isinstance(e, dict):
                continue
            meta = _validate_meta(e.get("meta"))
            baselines = _validate_baseline_map(e.get("baselines"))
            if meta is None or not any(v is not None for v in baselines.values()):
                continue
            clean.append({"meta": meta, "baselines": baselines})
        return clean

    @property
    def binding_migration_notice(self) -> str:
        """本次加载是否修正了有源板"格子→数据通道"绑定。"""
        return self._binding_migration_notice

    def _migrate_active_pcb_binding(self) -> bool:
        """识别旧配置使用的绑定并补齐字段（**不改写已有映射**）。

        - v3.4.0 的映射 = PCB 设计对应 → cell_binding = design_v1
        - v3.4.1 的映射 = 旧表 → cell_binding = legacy_v341，并标记
          "未实测确认"，提示用户逐点核对
        绝不把用户文件里的映射悄悄换成另一张表（避免静默改数据）。
        """
        norm = normalize_channel_map(self._data.get("channel_map"))
        if not is_valid_map(norm):
            return False
        binding = binding_for_map(norm)
        if binding is None:
            return False
        stored = str(self._data.get("cell_binding", "") or "")
        if stored == binding:
            return False
        self._data["cell_binding"] = binding
        if binding == BINDING_LEGACY_V341:
            self._data["binding_confirmed"] = False
            self._binding_migration_notice = (
                "检测到 v3.4.1 旧有源板映射（CH01←9 …）：已保留原映射并"
                "标记为“旧映射（需实测确认）”。请逐点核对 16 只线圈后，"
                "在“设置 → 通道 → 线圈绑定”里确认或切换到 PCB 设计对应。"
            )
            return True
        if binding == BINDING_DESIGN and self._data.get("layout_id") == \
                LAYOUT_ACTIVE_PCB:
            self._binding_migration_notice = (
                "有源板布局使用 PCB 设计对应（CHn → 第 n 路电压）；"
                "该绑定需用逐点实测确认。"
            )
        return True

    def _migrate_baselines_to_entries(self) -> bool:
        """把 baseline_sets 中的既有基线并入历史库（幂等）。

        旧配置每个来源只有一个基线槽；迁移后它成为一条历史条目，
        之后对新板做标定只会新增条目，不会覆盖旧板基线。
        返回是否发生了迁移。
        """
        existing = {self._entry_identity(e["meta"]) for e in self._entries()}
        changed = False
        sets = self._data.get("baseline_sets")
        if not isinstance(sets, dict):
            return False
        for scope in BASELINE_SCOPES:
            s = sets.get(scope)
            if not isinstance(s, dict):
                continue
            meta = _validate_meta(s.get("meta"))
            baselines = _validate_baseline_map(s.get("baselines"))
            if meta is None or not any(
                v is not None for v in baselines.values()
            ):
                continue
            ident = self._entry_identity(meta)
            if ident in existing:
                continue
            self._upsert_entry(meta, baselines)
            existing.add(ident)
            changed = True
        return changed

    def _upsert_entry(
        self,
        meta: dict,
        baselines: Dict[int, Optional[float]],
    ) -> None:
        entries = self._entries()
        ident = self._entry_identity(meta)
        kept = [
            e for e in entries if self._entry_identity(e["meta"]) != ident
        ]
        kept.append({"meta": meta, "baselines": dict(baselines)})
        self._data["baseline_entries"] = kept

    @staticmethod
    def _entry_match_score(
        meta: dict,
        scope: str,
        device_id: Optional[str],
        layout_id: Optional[str],
        conditions: Optional[dict],
    ) -> int:
        """匹配得分；-1 表示不匹配。

        缺失的元数据（旧基线没有该字段）视为"未记录 = 兼容"，
        避免升级后历史基线全部失效；一旦记录过就严格比较。
        """
        if str(meta.get("source", "")) != scope:
            return -1
        score = 0
        saved_dev = str(meta.get("device_id", "") or "")
        cur_dev = str(device_id or "")
        if scope == SCOPE_TCP:
            # TCP 必须人工编号且一致
            if not saved_dev or not cur_dev or saved_dev != cur_dev:
                return -1
            score += 1
        elif saved_dev or cur_dev:
            if saved_dev != cur_dev:
                return -1
            score += 1
        if layout_id:
            saved_layout = str(meta.get("layout", "") or LAYOUT_BREADBOARD)
            if saved_layout != layout_id:
                return -1
            score += 1
        if conditions:
            for key in ("adc_profile", "frequency", "freq_unit", "gain",
                        "coil"):
                if key not in conditions:
                    continue
                cur = conditions.get(key)
                saved = meta.get(key)
                if cur in (None, "") or saved in (None, ""):
                    continue
                if key == "frequency":
                    try:
                        saved_f = float(saved)
                        cur_f = float(cur)
                    except (TypeError, ValueError):
                        return -1
                    # 旧基线没有频率记录时频率为 0.0 → 视为未记录（兼容），
                    # 不能当成"标定时是 0 Hz"而拒绝全部历史基线
                    if saved_f == 0.0:
                        continue
                    if abs(saved_f - cur_f) > 1e-6:
                        return -1
                elif str(saved) != str(cur):
                    return -1
                score += 1
        return score

    def _find_entry(
        self,
        scope: str,
        device_id: Optional[str],
        layout_id: Optional[str],
        conditions: Optional[dict],
    ) -> Optional[dict]:
        best = None
        best_score = -1
        for e in self._entries():
            s = self._entry_match_score(
                e["meta"], scope, device_id, layout_id, conditions
            )
            if s > best_score:
                best, best_score = e, s
        return best

    def baseline_conditions_signature(self, conditions: Optional[dict]) -> str:
        """测量条件的人类可读摘要（日志/提示用）。"""
        if not conditions:
            return ""
        parts = []
        for key, label in (
            ("adc_profile", "Profile"),
            ("frequency", "频率"),
            ("freq_unit", ""),
            ("gain", "增益"),
            ("coil", "线圈"),
        ):
            v = conditions.get(key)
            if v in (None, ""):
                continue
            parts.append(f"{label}{v}" if label else str(v))
        return " ".join(parts)

    def get_baselines(
        self,
        scope: str,
        device_id: Optional[str] = None,
        layout_id: Optional[str] = None,
        conditions: Optional[dict] = None,
    ) -> Dict[int, Optional[float]]:
        """取某作用域的基线映射。

        serial / tcp 作用域且保存了设备标识时：仅当与当前设备标识一致才
        返回（不只看 COM 口号，也不看 TCP 对端 IP/端口）；设备标识缺失
        （老设备无序列号、未配置编号）时放行。两条链路的基线互不通用。

        layout_id 非空时还校验标定时使用的通道布局：换板（旧面包板 ↔
        有源线圈 PCB）后旧标定结果不得自动套用。旧基线没有 layout 字段
        → 视为旧面包板布局（当时唯一存在的布局）。

        conditions（ADC Profile / 频率 / 增益 / 线圈）非空时，从基线
        历史库中按"来源 + 设备 + 布局 + 测量条件"精确匹配；匹配不到
        返回空，绝不套用其他实验条件的标定结果（旧基线缺该字段视为
        兼容，避免升级后历史基线全部失效）。
        """
        if conditions is not None or layout_id is not None:
            entry = self._find_entry(scope, device_id, layout_id, conditions)
            if entry is None:
                return {i: None for i in range(16)}
            return _validate_baseline_map(entry["baselines"])
        s = self._baseline_set(scope)
        meta = _validate_meta(s.get("meta"))
        if meta is None:
            return {i: None for i in range(16)}
        if layout_id:
            saved_layout = meta.get("layout") or LAYOUT_BREADBOARD
            if saved_layout != layout_id:
                return {i: None for i in range(16)}
        saved_dev = meta.get("device_id") or ""
        cur_dev = device_id or ""
        if scope == SCOPE_TCP:
            # TCP：手动填写的设备编号**不是硬件认证**，只是人工约定。
            # 因此身份为空、基线未记录编号（来源不可确认）、或编号不一致
            # 时，一律不自动应用历史基线——宁可要求重新标定，也不套用
            # 可能属于另一台设备的基线。
            if not cur_dev or not saved_dev or saved_dev != cur_dev:
                return {i: None for i in range(16)}
        elif scope == SCOPE_SERIAL:
            # 串口：保持既有兼容策略（老设备可能没有序列号）
            if saved_dev and cur_dev and saved_dev != cur_dev:
                return {i: None for i in range(16)}
        return _validate_baseline_map(s.get("baselines"))

    def get_baseline_meta(self, scope: str) -> Optional[dict]:
        """某作用域"当前槽"的基线 meta（无有效基线时为 None）。"""
        s = self._baseline_set(scope)
        meta = _validate_meta(s.get("meta"))
        if meta is None:
            return None
        baselines = _validate_baseline_map(s.get("baselines"))
        if not any(v is not None for v in baselines.values()):
            return None
        return meta

    def get_baseline_meta_for(
        self,
        scope: str,
        device_id: Optional[str] = None,
        layout_id: Optional[str] = None,
        conditions: Optional[dict] = None,
    ) -> Optional[dict]:
        """按上下文匹配到的基线 meta（供提示/CSV 元数据使用）。"""
        if conditions is None and layout_id is None:
            return self.get_baseline_meta(scope)
        entry = self._find_entry(scope, device_id, layout_id, conditions)
        return None if entry is None else dict(entry["meta"])

    def list_baseline_entries(self) -> List[dict]:
        """基线历史库摘要（诊断/测试用）：[(scope, device, layout, profile)]"""
        return [
            {
                "source": e["meta"].get("source", ""),
                "device_id": e["meta"].get("device_id", ""),
                "layout": e["meta"].get("layout", "")
                          or LAYOUT_BREADBOARD,
                "adc_profile": e["meta"].get("adc_profile", ""),
                "frequency": e["meta"].get("frequency", 0.0),
                "gain": e["meta"].get("gain", ""),
                "baseline_id": e["meta"].get("baseline_id", ""),
            }
            for e in self._entries()
        ]

    def set_baselines(
        self,
        scope: str,
        baselines: Dict[int, Optional[float]],
        meta: Optional[dict] = None,
    ) -> None:
        """写入某作用域的基线（仅该作用域，不影响其他来源）。"""
        if scope not in BASELINE_SCOPES:
            raise ValueError(f"未知基线作用域: {scope}")
        m = dict(meta or {})
        m.setdefault("source", scope)
        m.setdefault("unit", BASELINE_UNIT)
        m.setdefault("calibrated_at", datetime.now().isoformat(
            timespec="seconds"))
        m.setdefault("baseline_id", "bl-" + uuid.uuid4().hex[:12])
        m.setdefault("channels", sorted(
            c for c, v in baselines.items() if v is not None))
        self._data["baseline_sets"][scope] = {
            "meta": m,
            "baselines": {
                str(int(c)): (None if v is None else float(v))
                for c, v in baselines.items()
            },
        }
        # 同时写入历史库：同一来源下不同设备/布局/实验条件各占一条，
        # 新板标定不会覆盖旧板基线（baseline_sets 仅作"当前槽"兼容）
        self._upsert_entry(_validate_meta(m) or m, baselines)

    def clear_baselines(self, scope: str) -> None:
        """清空某作用域基线（其他作用域与历史库不动）。"""
        if scope in BASELINE_SCOPES:
            self._data["baseline_sets"][scope] = _empty_baseline_set()

    @property
    def legacy_baselines_backup(self) -> Optional[dict]:
        """旧配置备份下来的无来源基线（只读展示用）。"""
        v = self._data.get("legacy_baselines_backup")
        return v if isinstance(v, dict) else None

    # 兼容旧接口：默认读写 serial 作用域（新代码请用 get/set_baselines）
    @property
    def baselines(self) -> Dict[int, Optional[float]]:
        return self.get_baselines(SCOPE_SERIAL)

    @baselines.setter
    def baselines(self, baselines: Dict[int, Optional[float]]) -> None:
        self.set_baselines(SCOPE_SERIAL, baselines)

    # ------------------------------------------------------------------
    @property
    def ema_alpha(self) -> float:
        return _safe_float(self._data.get("ema_alpha", 0.2), 0.2)

    @ema_alpha.setter
    def ema_alpha(self, v: float) -> None:
        self._data["ema_alpha"] = max(0.0, min(1.0, float(v)))

    @property
    def median_window(self) -> int:
        return _safe_int(self._data.get("median_window", 3), 3)

    @median_window.setter
    def median_window(self, n: int) -> None:
        self._data["median_window"] = max(1, int(n))

    @property
    def heatmap_min(self) -> float:
        return _safe_float(self._data.get("heatmap_min", -2000.0), -2000.0)

    @heatmap_min.setter
    def heatmap_min(self, v: float) -> None:
        self._data["heatmap_min"] = float(v)

    @property
    def heatmap_max(self) -> float:
        return _safe_float(self._data.get("heatmap_max", 2000.0), 2000.0)

    @heatmap_max.setter
    def heatmap_max(self, v: float) -> None:
        self._data["heatmap_max"] = float(v)

    @property
    def heatmap_auto(self) -> bool:
        return _safe_bool(self._data.get("heatmap_auto", True), True)

    @heatmap_auto.setter
    def heatmap_auto(self, v: bool) -> None:
        self._data["heatmap_auto"] = bool(v)

    @property
    def curve_window_s(self) -> float:
        return _safe_float(self._data.get("curve_window_s", 30.0), 30.0)

    @curve_window_s.setter
    def curve_window_s(self, v: float) -> None:
        self._data["curve_window_s"] = max(5.0, float(v))

    @property
    def curve_max_channels(self) -> int:
        return _safe_int(self._data.get("curve_max_channels", 4), 4)

    @curve_max_channels.setter
    def curve_max_channels(self, n: int) -> None:
        self._data["curve_max_channels"] = max(1, min(16, int(n)))

    @property
    def experiment_params(self) -> dict:
        v = self._data.get("experiment_params", {})
        if not isinstance(v, dict):
            return default_config(self._profiles)["experiment_params"]
        return dict(v)

    @experiment_params.setter
    def experiment_params(self, params: dict) -> None:
        if isinstance(params, dict):
            self._data["experiment_params"] = dict(params)

    @property
    def last_csv_dir(self) -> str:
        return str(self._data.get("last_csv_dir", "") or "")

    @last_csv_dir.setter
    def last_csv_dir(self, p: str) -> None:
        self._data["last_csv_dir"] = str(p or "")

    @property
    def last_serial(self) -> dict:
        v = self._data.get("last_serial", {})
        if not isinstance(v, dict):
            return {"port": "", "baudrate": 115200}
        return {
            "port": str(v.get("port", "")),
            "baudrate": _safe_int(v.get("baudrate", 115200), 115200),
        }

    @last_serial.setter
    def last_serial(self, s: dict) -> None:
        if isinstance(s, dict):
            self._data["last_serial"] = {
                "port": str(s.get("port", "")),
                "baudrate": _safe_int(s.get("baudrate", 115200), 115200),
            }

    # ------------------------------------------------------------------
    # 传输方式 / TCP 监听 / 采集设备编号
    # ------------------------------------------------------------------
    @property
    def transport(self) -> str:
        """当前数据源传输方式："serial" 或 "tcp"。"""
        v = str(self._data.get("transport", TRANSPORT_SERIAL) or "")
        return v if v in TRANSPORTS else TRANSPORT_SERIAL

    @transport.setter
    def transport(self, v: str) -> None:
        s = str(v or "")
        self._data["transport"] = s if s in TRANSPORTS else TRANSPORT_SERIAL

    def _tcp_dict(self) -> dict:
        v = self._data.get("tcp")
        if not isinstance(v, dict):
            v = {}
            self._data["tcp"] = v
        return v

    @property
    def tcp_listen_host(self) -> str:
        """TCP 监听地址（默认 0.0.0.0 表示全部网卡）。"""
        h = str(self._tcp_dict().get("listen_host", DEFAULT_LISTEN_HOST) or "")
        return h.strip() or DEFAULT_LISTEN_HOST

    @tcp_listen_host.setter
    def tcp_listen_host(self, host: str) -> None:
        self._tcp_dict()["listen_host"] = (
            str(host or "").strip() or DEFAULT_LISTEN_HOST
        )

    @property
    def tcp_listen_port(self) -> int:
        """TCP 监听端口（1..65535，默认 8080）。"""
        p = _safe_int(
            self._tcp_dict().get("listen_port", DEFAULT_LISTEN_PORT),
            DEFAULT_LISTEN_PORT,
        )
        if not (1 <= p <= 65535):
            return DEFAULT_LISTEN_PORT
        return p

    @tcp_listen_port.setter
    def tcp_listen_port(self, port: int) -> None:
        p = _safe_int(port, DEFAULT_LISTEN_PORT)
        if not (1 <= p <= 65535):
            p = DEFAULT_LISTEN_PORT
        self._tcp_dict()["listen_port"] = p

    @property
    def tcp_keepalive_enabled(self) -> bool:
        """是否启用 TCP keepalive（链路层探活，不会踢掉短暂无数据的设备）。"""
        return _safe_bool(
            self._tcp_dict().get("keepalive_enabled", True), True
        )

    @tcp_keepalive_enabled.setter
    def tcp_keepalive_enabled(self, v: bool) -> None:
        self._tcp_dict()["keepalive_enabled"] = bool(v)

    @property
    def tcp_keepalive_idle_s(self) -> int:
        """空闲多少秒后开始探活（默认 60；过小会误伤正常短时空闲）。"""
        v = _safe_int(self._tcp_dict().get("keepalive_idle_s", 60), 60)
        return max(10, min(3600, v))

    @tcp_keepalive_idle_s.setter
    def tcp_keepalive_idle_s(self, v: int) -> None:
        self._tcp_dict()["keepalive_idle_s"] = max(
            10, min(3600, _safe_int(v, 60))
        )

    @property
    def tcp_keepalive_interval_s(self) -> int:
        v = _safe_int(
            self._tcp_dict().get("keepalive_interval_s", 10), 10
        )
        return max(1, min(60, v))

    @tcp_keepalive_interval_s.setter
    def tcp_keepalive_interval_s(self, v: int) -> None:
        self._tcp_dict()["keepalive_interval_s"] = max(
            1, min(60, _safe_int(v, 10))
        )

    @property
    def tcp_keepalive_count(self) -> int:
        v = _safe_int(self._tcp_dict().get("keepalive_count", 3), 3)
        return max(1, min(10, v))

    @tcp_keepalive_count.setter
    def tcp_keepalive_count(self, v: int) -> None:
        self._tcp_dict()["keepalive_count"] = max(
            1, min(10, _safe_int(v, 3))
        )

    @property
    def device_id(self) -> str:
        """采集设备编号（持久化硬件身份，基线匹配用）。

        空串表示未配置：此时 TCP 基线仍按 tcp 作用域隔离，
        但无法区分"同一台电脑换了一台采集设备"。
        """
        v = str(self._data.get("device_id", "") or "").strip()
        return v[:MAX_DEVICE_ID_LEN]

    @device_id.setter
    def device_id(self, v: str) -> None:
        self._data["device_id"] = str(v or "").strip()[:MAX_DEVICE_ID_LEN]

    @property
    def invert_sign(self) -> bool:
        return _safe_bool(self._data.get("invert_sign", False), False)

    @invert_sign.setter
    def invert_sign(self, v: bool) -> None:
        self._data["invert_sign"] = bool(v)

    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not os.path.isfile(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(data, dict):
            return
        # 合并：缺失字段走 default_config（其他用户设置全部保留）
        d = default_config(self._profiles)
        d.update({k: v for k, v in data.items() if k in d})
        # channel_map 单独处理
        if "channel_map" in data and isinstance(data["channel_map"], dict):
            d["channel_map"] = data["channel_map"]

        saved_unit = data.get("baseline_unit")
        saved_ver = data.get("version")

        # ---- v4 基线作用域 ----
        if saved_ver == CONFIG_VERSION and saved_unit == BASELINE_UNIT:
            # 已是 v4：逐作用域校验加载
            sets = data.get("baseline_sets")
            if isinstance(sets, dict):
                for scope in BASELINE_SCOPES:
                    s = sets.get(scope)
                    if not isinstance(s, dict):
                        continue
                    meta = _validate_meta(s.get("meta"))
                    baselines = _validate_baseline_map(s.get("baselines"))
                    if meta is None or not any(
                            v is not None for v in baselines.values()):
                        continue
                    d["baseline_sets"][scope] = {
                        "meta": meta,
                        "baselines": {
                            str(c): v for c, v in baselines.items()
                        },
                    }
            # v4 文件里仍残留无来源的扁平 baselines：同样备份作废，
            # 绝不静默当成当前硬件基线
            flat_v4 = data.get("baselines")
            if (isinstance(flat_v4, dict)
                    and any(v is not None for v in flat_v4.values())):
                d["legacy_baselines_backup"] = {
                    "version": saved_ver,
                    "baseline_unit": saved_unit,
                    "baselines": flat_v4,
                }
                self._baselines_invalidated = True
        else:
            # 旧版本（v3 及更早）或单位标记不符：扁平 baselines 无来源
            # 信息，备份后作废，标记"待确认/需重新标定"；绝不静默沿用。
            flat = data.get("baselines")
            had_baselines = (
                isinstance(flat, dict)
                and any(v is not None for v in flat.values())
            )
            # 版本/单位不符时，即使文件里带了 v4 作用域基线也不能沿用
            # （d.update 已把 baseline_sets 拷入 d，这里必须显式清空，
            # 否则"作废"被绕过）；其中的值同样备份后作废。
            stale_sets = data.get("baseline_sets")
            had_sets = False
            if isinstance(stale_sets, dict):
                for sc in BASELINE_SCOPES:
                    s = stale_sets.get(sc)
                    if not isinstance(s, dict):
                        continue
                    bl = s.get("baselines")
                    if isinstance(bl, dict) and any(
                            v is not None for v in bl.values()):
                        had_sets = True
                        break
            if had_baselines or had_sets:
                backup = {
                    "version": saved_ver,
                    "baseline_unit": saved_unit,
                    "baselines": flat if had_baselines else {},
                }
                if had_sets:
                    backup["baseline_sets"] = stale_sets
                d["legacy_baselines_backup"] = backup
                self._baselines_invalidated = True
            d["baseline_sets"] = {
                s: _empty_baseline_set() for s in BASELINE_SCOPES
            }
            # v3 之前可能没有 mV 单位标记的旧 raw count 基线同样在此作废

        # 已保存的旧基线备份跨加载保留：备份一旦产生，在后续
        # 保存/加载周期中不得被静默丢弃（本次新产生的备份优先）
        if "legacy_baselines_backup" not in d:
            prev_backup = data.get("legacy_baselines_backup")
            if isinstance(prev_backup, dict):
                d["legacy_baselines_backup"] = prev_backup

        # 强制写入当前单位标记
        d["measurement_unit"] = MEASUREMENT_UNIT
        d["baseline_unit"] = BASELINE_UNIT
        d["version"] = CONFIG_VERSION
        self._data = d

        # ---- 有源板"格子→数据通道"绑定迁移 ----
        # 旧版本把有源板的 CHxx 标签直接当成数据通道号；新版本标签位置
        # 不变，只改每格读取的数据通道。若文件里是旧映射，一次性换成
        # 修正后的映射（标签与坐标不变，数据来源被纠正）。
        self._migrate_active_pcb_binding()

        # ---- 基线历史库迁移（保留旧配置中可识别的基线）----
        # 旧配置每个来源只有一个槽（baseline_sets[scope]）：把它作为一条
        # 历史条目写入 baseline_entries，这样之后标定新板不会覆盖它。
        self._migrate_baselines_to_entries()

        # ---- 通道物理映射迁移（独立 mapping_version，不触碰 version）----
        # 旧配置（无 mapping_version 标记或标记过时）：
        #   - 映射完全匹配旧行优先默认 → 备份原配置后一次性迁移到实际
        #     接线布局，写入迁移标记并保存；重启后布局保持、不重复迁移
        #   - 缺失映射 → 新默认（default_config 已是硬件布局）
        #   - 合法且不同于旧默认的自定义映射 → 保留，绝不静默覆盖
        #     （设置对话框提供显式"恢复硬件默认布局"入口）
        #   - 非法映射（缺失/重叠/越界/格式错误）→ 回退硬件默认
        # 其余设置（串口/滤波/实验参数/真实基线/基线来源信息）已在上方
        # 正常加载，随本次保存原样写回，不受映射迁移影响。
        saved_map_ver = data.get("mapping_version")
        if saved_map_ver != MAPPING_VERSION_CURRENT:
            raw_map = data.get("channel_map")
            norm_map = normalize_channel_map(raw_map)
            hw_lists = {
                str(ch): [r, c]
                for ch, (r, c) in HARDWARE_CHANNEL_MAP.items()
            }
            if "channel_map" not in data:
                # 映射键缺失 → 新默认（d 默认已是硬件布局），仅补标记
                self._map_migration_notice = (
                    "配置缺少通道物理映射，已使用当前硬件默认布局。"
                )
            elif not isinstance(raw_map, dict) or not raw_map:
                # 键存在但值畸形（null/空表/字符串等）：d.update 已把
                # 畸形值拷入 self._data，必须显式写回硬件默认——否则
                # 畸形值随 mapping_version 标记永久落盘且不再提示
                self._backup_file("premap_v2")
                self._data["channel_map"] = hw_lists
                self._map_migration_notice = (
                    "配置中的通道物理映射格式非法，"
                    "已回退为当前硬件默认布局。"
                )
            elif is_valid_map(norm_map) and maps_equal(
                    norm_map, LEGACY_ROW_MAJOR_MAP):
                # 旧行优先默认 → 一次性迁移；先备份原配置文件
                self._backup_file("premap_v2")
                self._data["channel_map"] = hw_lists
                self._map_migration_notice = (
                    "通道物理映射已按实际接线布局（4 列、每列自下而上 "
                    "CH01→CH16）一次性迁移，原配置已备份。"
                )
            elif is_valid_map(norm_map):
                # 合法自定义映射：保留，不覆盖、不清空
                pass
            else:
                # 非法映射：回退硬件默认（显式提示，非静默重叠）
                self._backup_file("premap_v2")
                self._data["channel_map"] = hw_lists
                self._map_migration_notice = (
                    "配置中的通道物理映射非法（缺失/重叠/越界），"
                    "已回退为当前硬件默认布局。"
                )
            self._data["mapping_version"] = MAPPING_VERSION_CURRENT
            self.save()

    def _backup_file(self, tag: str) -> None:
        """迁移前备份当前配置文件（失败不阻塞迁移，仅尽力而为）。"""
        try:
            if os.path.isfile(self._path):
                shutil.copy2(self._path, self._path + f".{tag}.bak")
        except OSError:
            pass

    def save(self) -> tuple:
        tmp_path = None
        try:
            dir_name = os.path.dirname(self._path) or "."
            fd, tmp_path = tempfile.mkstemp(
                suffix=".tmp", dir=dir_name, prefix="config_"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(
                    self._data, f, ensure_ascii=False, indent=2
                )
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._path)
            return True, ""
        except OSError as e:
            if tmp_path is not None:
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except OSError:
                    pass
            return False, str(e)
