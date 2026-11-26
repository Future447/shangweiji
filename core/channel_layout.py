"""
core/channel_layout.py — 通道物理位置映射的唯一来源（single source of truth）。

最终接线顺序（v3.3.0 起，与面包板实际接线一致）：
    4 列从左到右，每列自下而上编号：
      第 1 列：CH01–CH04（下 → 上）
      第 2 列：CH05–CH08（下 → 上）
      第 3 列：CH09–CH12（下 → 上）
      第 4 列：CH13–CH16（下 → 上）

热力图坐标约定：row=0 为最顶行，row=3 为最底行；col=0 为最左列。
内部通道号 channel 0 = CH01，channel 15 = CH16。

因此热力图自上而下显示为：
    CH04  CH08  CH12  CH16
    CH03  CH07  CH11  CH15
    CH02  CH06  CH10  CH14
    CH01  CH05  CH09  CH13

映射公式：
    row = 3 - channel % 4
    col = channel // 4

约束：
- 只改物理位置，不重排采集数据：samples_mv[i]、valid_mask bit i、
  处理器/滤波/基线/启用通道仍按内部通道号索引，与本模块无关。
- 映射必须完整覆盖 16 个通道和 16 个唯一坐标；非法映射（缺失/
  重复/越界/格式错误）一律显式拒绝，禁止静默产生重叠。
- config_manager / heatmap_widget / main_window / demo_source /
  recorder / replay_source 统一引用本模块，禁止各自再写一套默认。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

GRID_ROWS = 4
GRID_COLS = 4
CHANNEL_COUNT = GRID_ROWS * GRID_COLS  # 16

# mapping_version：独立于 CONFIG_VERSION 的映射 schema 标记。
#   缺失/1 = 旧行优先（row-major）布局
#   2      = 实际接线布局（列优先、每列自下而上）
# 绝不能为了映射升级去 bump CONFIG_VERSION——v4 基线加载依赖
# saved_ver == CONFIG_VERSION，bump 会把有效基线误判为旧版本作废。
MAPPING_VERSION_LEGACY = 1
MAPPING_VERSION_HARDWARE = 2
MAPPING_VERSION_CURRENT = MAPPING_VERSION_HARDWARE


def _hw_pos(channel: int) -> Tuple[int, int]:
    return (GRID_ROWS - 1 - channel % GRID_ROWS, channel // GRID_ROWS)


#: 当前硬件实际接线映射：{channel: (row, col)}
#: （= 旧面包板布局；作为默认与向后兼容基准，不得改写语义）
HARDWARE_CHANNEL_MAP: Dict[int, Tuple[int, int]] = {
    ch: _hw_pos(ch) for ch in range(CHANNEL_COUNT)
}


# ----------------------------------------------------------------------
# 布局预设（layout preset）
#
# 布局只决定"通道号 → 热力图 (row, col)"的显示位置，
# 绝不改变数据语义：samples_mv[i] / valid_mask bit i / enabled_channels
# 仍按通道号索引（CH01 = channel 0 … CH16 = channel 15）。
#
# 布局与 ADC 分压比是**两个独立设置**：换布局不改变任何电压换算。
# ----------------------------------------------------------------------

#: 旧面包板布局（4 列，每列自下而上，CH01 在左下）
LAYOUT_BREADBOARD = "breadboard_hw_v1"
#: 有源线圈 PCB 布局
LAYOUT_ACTIVE_PCB = "active_pcb_v1"
#: 用户自定义映射
LAYOUT_CUSTOM = "custom"


def _active_pcb_pos(channel: int) -> Tuple[int, int]:
    """有源线圈 PCB：以元件面朝向观察者、FPC 指端朝左为参考。

    热力图自上而下、每行自左向右应为：
        CH09 CH10 CH11 CH12
        CH13 CH14 CH15 CH16
        CH08 CH07 CH06 CH05
        CH04 CH03 CH02 CH01
    """
    if channel >= 8:
        # 上半区：CH09–CH12 在第一行，CH13–CH16 在第二行，均自左向右
        return (0 if channel < 12 else 1, channel % GRID_COLS)
    # 下半区：CH08–CH05 在第三行（自左向右递减），CH04–CH01 在第四行
    row = 2 if channel >= 4 else 3
    return (row, (GRID_COLS - 1) - (channel % GRID_COLS))


#: 有源线圈 PCB 的**格子标签位置**：{slot: (row, col)}
#: slot = 标签序号 - 1（slot 0 ↔ 标签 CH01），与数据通道无关。
#: 这张表保证"格子坐标 + CHxx 标签"与改动前完全一致。
ACTIVE_PCB_LABEL_POSITIONS: Dict[int, Tuple[int, int]] = {
    slot: _active_pcb_pos(slot) for slot in range(CHANNEL_COUNT)
}


# ----------------------------------------------------------------------
# 线圈标签 ↔ 串口电压序号（数据通道）绑定
#
# 术语（必须区分）：
#   - "线圈 CH 标签"：PCB 丝印 / 热力图格子上的 CH01…CH16（物理位置）
#   - "串口电压序号"：D 帧中 "mV" 之后的第 1…16 个电压值（数据通道）
#
# 存在两份互相冲突的表：
#   1) PCB 设计对应：CHn → 第 n 路电压（前提：实物接线与设计一致）
#   2) v3.4.1 旧映射：CH01←9 … CH16←1（上一版硬编码）
# 因此绑定必须由用户"逐点实测确认"后显式选择并存盘；仅选择
# "有源板布局"时**绝不**静默套用其中任何一张表。
# ----------------------------------------------------------------------

#: PCB 设计对应：CH01→第1路 … CH16→第16路
BINDING_DESIGN = "design_v1"
#: v3.4.1 旧映射：CH01←9, CH02←10 … CH08←16, CH09←8 … CH16←1
BINDING_LEGACY_V341 = "legacy_v341"
#: 恒等（旧面包板）：CHn → 第 n 路
BINDING_IDENTITY = "identity"

BINDING_NAMES: Dict[str, str] = {
    BINDING_DESIGN: "PCB 设计对应（CHn → 第 n 路电压）",
    BINDING_LEGACY_V341: "v3.4.1 旧映射（CH01←9 …，需实测确认）",
    BINDING_IDENTITY: "恒等（CHn → 第 n 路电压）",
}


def _binding_design() -> Dict[int, int]:
    """标签槽位 → 数据通道（恒等）。"""
    return {slot: slot for slot in range(CHANNEL_COUNT)}


def _binding_legacy_v341() -> Dict[int, int]:
    """v3.4.1 的旧表：CH01←9 … CH08←16，CH09←8 … CH16←1（0 基）。"""
    out = {}
    for slot in range(CHANNEL_COUNT):
        if slot < 8:
            out[slot] = slot + 8
        else:
            out[slot] = (CHANNEL_COUNT - 1) - slot
    return out


#: 全部绑定表：{binding_id: {标签槽位: 数据通道}}
CELL_BINDINGS: Dict[str, Dict[int, int]] = {
    BINDING_DESIGN: _binding_design(),
    BINDING_LEGACY_V341: _binding_legacy_v341(),
    BINDING_IDENTITY: _binding_design(),
}

#: 各布局的默认绑定：有源板默认"设计对应"（而非旧表）
DEFAULT_BINDING: Dict[str, str] = {
    LAYOUT_BREADBOARD: BINDING_IDENTITY,
    LAYOUT_ACTIVE_PCB: BINDING_DESIGN,
}


def default_binding(layout_id: Optional[str]) -> str:
    return DEFAULT_BINDING.get(str(layout_id or ""), BINDING_IDENTITY)


def binding_name(binding_id: Optional[str]) -> str:
    return BINDING_NAMES.get(
        str(binding_id or ""), BINDING_NAMES[BINDING_IDENTITY]
    )


def binding_cell_data(binding_id: Optional[str]) -> Dict[int, int]:
    """绑定表 {标签槽位: 数据通道}；未知绑定 → 恒等。"""
    return dict(
        CELL_BINDINGS.get(str(binding_id or ""), CELL_BINDINGS[BINDING_IDENTITY])
    )


#: 兼容别名：v3.4.1 旧表（{slot: 数据通道}），仅供历史代码/测试引用
ACTIVE_PCB_CELL_DATA_CHANNEL: Dict[int, int] = dict(
    CELL_BINDINGS[BINDING_LEGACY_V341]
)

#: v3.4.0 保存的 active_pcb 映射（当时"标签号 == 数据通道号"，
#: 即设计对应关系）。仅用于识别旧配置，不再做静默改写。
LEGACY_ACTIVE_PCB_MAP: Dict[int, Tuple[int, int]] = dict(
    ACTIVE_PCB_LABEL_POSITIONS
)


def cell_data_channel(
    layout_id: Optional[str],
    slot: int,
    binding_id: Optional[str] = None,
) -> int:
    """某布局/绑定下，第 slot 个格子（标签 CH(slot+1)）读取的数据通道。"""
    lid = str(layout_id or "")
    if lid != LAYOUT_ACTIVE_PCB:
        return slot
    table = binding_cell_data(binding_id or default_binding(lid))
    return table.get(slot, slot)


def data_position_map(
    layout_id: Optional[str],
    binding_id: Optional[str] = None,
) -> Dict[int, Tuple[int, int]]:
    """数据通道 → 热力图坐标（标签位置按绑定关系换到数据通道上）。

    数据仍按通道号索引，只有"显示到哪个格子"经过一次绑定。
    """
    lid = str(layout_id or "")
    if lid == LAYOUT_ACTIVE_PCB:
        table = binding_cell_data(binding_id or default_binding(lid))
        return {
            table[slot]: ACTIVE_PCB_LABEL_POSITIONS[slot]
            for slot in range(CHANNEL_COUNT)
        }
    if lid == LAYOUT_BREADBOARD:
        return dict(HARDWARE_CHANNEL_MAP)
    return {}


def data_label_map(
    layout_id: Optional[str],
    binding_id: Optional[str] = None,
) -> Dict[int, str]:
    """数据通道 → 显示的格子标签（如 data 11 → "CH04"）。"""
    lid = str(layout_id or "")
    if lid == LAYOUT_ACTIVE_PCB:
        table = binding_cell_data(binding_id or default_binding(lid))
        return {
            data: f"CH{slot + 1:02d}" for slot, data in table.items()
        }
    return {ch: f"CH{ch + 1:02d}" for ch in range(CHANNEL_COUNT)}


#: 可用于反查标签的 (布局, 绑定) 组合（含历史表）
_LABEL_CANDIDATES = (
    (LAYOUT_BREADBOARD, BINDING_IDENTITY),
    (LAYOUT_ACTIVE_PCB, BINDING_DESIGN),
    (LAYOUT_ACTIVE_PCB, BINDING_LEGACY_V341),
)


def labels_for_map(mapping) -> Dict[int, str]:
    """由"数据通道→坐标"映射反推标签。

    历史 CSV 只记录了映射，没有记录标签；只要映射与某张已知绑定表
    完全一致，就能还原当时的标签。无法识别时按历史约定 CH(data+1)。
    """
    norm = normalize_channel_map(mapping)
    for lid, bid in _LABEL_CANDIDATES:
        if norm == data_position_map(lid, bid):
            return data_label_map(lid, bid)
    return {ch: f"CH{ch + 1:02d}" for ch in range(CHANNEL_COUNT)}


def binding_for_map(mapping) -> Optional[str]:
    """由映射反查绑定标识（无法识别 → None）。"""
    norm = normalize_channel_map(mapping)
    for lid, bid in _LABEL_CANDIDATES:
        if norm == data_position_map(lid, bid):
            return bid
    return None


def layout_and_binding_for_map(mapping) -> Tuple[str, Optional[str]]:
    """由映射反查 (布局, 绑定)。"""
    norm = normalize_channel_map(mapping)
    if norm == dict(HARDWARE_CHANNEL_MAP):
        return LAYOUT_BREADBOARD, BINDING_IDENTITY
    for bid in (BINDING_DESIGN, BINDING_LEGACY_V341):
        if norm == data_position_map(LAYOUT_ACTIVE_PCB, bid):
            return LAYOUT_ACTIVE_PCB, bid
    return LAYOUT_CUSTOM, None


#: 有源板"数据通道 → 坐标"映射（默认绑定 = PCB 设计对应）
ACTIVE_PCB_CHANNEL_MAP: Dict[int, Tuple[int, int]] = data_position_map(
    LAYOUT_ACTIVE_PCB, BINDING_DESIGN
)

#: 布局预设表：{layout_id: {数据通道: (row, col)}}（配置/热力图用）
LAYOUT_PRESETS: Dict[str, Dict[int, Tuple[int, int]]] = {
    LAYOUT_BREADBOARD: HARDWARE_CHANNEL_MAP,
    LAYOUT_ACTIVE_PCB: ACTIVE_PCB_CHANNEL_MAP,
}

#: 布局显示名（UI/提示统一引用，禁止各处自写文案）
LAYOUT_NAMES: Dict[str, str] = {
    LAYOUT_BREADBOARD: "旧面包板（4 列，每列自下而上）",
    LAYOUT_ACTIVE_PCB: "有源线圈 PCB（元件面朝观察者，FPC 朝左）",
    LAYOUT_CUSTOM: "自定义布局",
}


def layout_name(layout_id: Optional[str]) -> str:
    """布局显示名；未知/空 → 自定义。"""
    return LAYOUT_NAMES.get(str(layout_id or ""), LAYOUT_NAMES[LAYOUT_CUSTOM])


def layout_map(
    layout_id: Optional[str],
    binding_id: Optional[str] = None,
) -> Dict[int, Tuple[int, int]]:
    """取布局+绑定对应的"数据通道→坐标"映射；未知 → 旧面包板。"""
    lid = str(layout_id or "")
    if lid == LAYOUT_ACTIVE_PCB:
        return data_position_map(lid, binding_id)
    preset = LAYOUT_PRESETS.get(lid)
    if preset is None:
        return dict(HARDWARE_CHANNEL_MAP)
    return dict(preset)


def layout_id_for_map(mapping) -> str:
    """由实际映射反推布局标识（预设优先；否则自定义）。

    用于加载旧配置（可能只有 channel_map 没有 layout_id）。
    """
    norm = normalize_channel_map(mapping)
    if not is_valid_map(norm):
        return LAYOUT_CUSTOM
    for lid, preset in LAYOUT_PRESETS.items():
        if norm == dict(preset):
            return lid
    # v3.4.1 旧绑定或有源板历史映射：仍识别为有源板布局，
    # 具体用哪张表由 ConfigManager 的 cell_binding 决定（不静默改写）。
    if norm in (
        data_position_map(LAYOUT_ACTIVE_PCB, BINDING_LEGACY_V341),
        dict(LEGACY_ACTIVE_PCB_MAP),
    ):
        return LAYOUT_ACTIVE_PCB
    return LAYOUT_CUSTOM

#: 旧默认（错误）行优先映射，仅用于识别并一次性迁移旧配置。
LEGACY_ROW_MAJOR_MAP: Dict[int, Tuple[int, int]] = {
    ch: (ch // GRID_COLS, ch % GRID_COLS) for ch in range(CHANNEL_COUNT)
}


def channel_label(channel: int) -> str:
    """内部通道号 → 显示标签（0 → 'CH01'）。标签永远按通道号生成。"""
    return f"CH{channel + 1:02d}"


def normalize_channel_map(raw) -> Dict[int, Tuple[int, int]]:
    """宽松解析 {ch: (row, col)}：丢弃无法解析的项，不补全、不去重、
    不 clamp。解析结果需再经 validate_channel_map 判定是否可用。"""
    out: Dict[int, Tuple[int, int]] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        try:
            ch = int(k)
        except (TypeError, ValueError, OverflowError):
            continue
        if not isinstance(v, (list, tuple)) or len(v) != 2:
            continue
        try:
            # int(float('inf')) 抛 OverflowError（json 可解析 1e999/
            # Infinity 为 inf），必须与 TypeError/ValueError 一并显式
            # 丢弃——否则损坏配置中的 inf 坐标会在启动迁移时崩溃
            r, c = int(v[0]), int(v[1])
        except (TypeError, ValueError, OverflowError):
            continue
        out[ch] = (r, c)
    return out


def validate_channel_map(mapping) -> Tuple[bool, str]:
    """严格校验：必须恰好覆盖通道 0..15，且 16 个坐标在界内且唯一。

    返回 (ok, reason)；ok=False 时 reason 说明失败类别
    （missing / extra / range / duplicate / format）。
    """
    if not isinstance(mapping, dict) or not mapping:
        return False, "format: 映射为空或不是字典"
    norm = normalize_channel_map(mapping)
    if len(norm) != len(mapping):
        return False, "format: 存在无法解析的键或坐标"
    expected = set(range(CHANNEL_COUNT))
    keys = set(norm.keys())
    missing = expected - keys
    if missing:
        return False, f"missing: 缺少通道 {sorted(missing)}"
    extra = keys - expected
    if extra:
        return False, f"extra: 含越界通道 {sorted(extra)}"
    for ch, (r, c) in norm.items():
        if not (0 <= r < GRID_ROWS and 0 <= c < GRID_COLS):
            return False, (
                f"range: 通道 {ch} 坐标 ({r},{c}) 越界 "
                f"(合法 0..{GRID_ROWS - 1}, 0..{GRID_COLS - 1})"
            )
    coords = list(norm.values())
    if len(set(coords)) != CHANNEL_COUNT:
        dup = sorted({p for p in coords if coords.count(p) > 1})
        return False, f"duplicate: 坐标重叠 {dup}"
    return True, ""


def is_valid_map(mapping) -> bool:
    return validate_channel_map(mapping)[0]


def maps_equal(a, b) -> bool:
    """两个映射解析后是否完全一致。"""
    na, nb = normalize_channel_map(a), normalize_channel_map(b)
    return bool(na) and na == nb


def map_to_grid_list(mapping=None) -> List[Tuple[int, int]]:
    """按内部 channel 0..15 顺序返回 [(row, col), ...]（DemoSource 用）。

    mapping 为 None 或非法时回退硬件默认映射（显式、可预测，
    绝不返回部分映射或重叠坐标）。
    """
    if mapping is None or not is_valid_map(mapping):
        norm = dict(HARDWARE_CHANNEL_MAP)
    else:
        norm = normalize_channel_map(mapping)
    return [norm[ch] for ch in range(CHANNEL_COUNT)]


# ----------------------------------------------------------------------
# CSV 紧凑序列化（recorder 写、replay 读；格式冻结，不得变更）：
#   "0:3x0;1:2x0;2:1x0;3:0x0;4:3x1;..."  即 "ch:row x col"，按通道升序
# ----------------------------------------------------------------------
def format_compact_map(mapping) -> str:
    norm = normalize_channel_map(mapping)
    return ";".join(
        f"{ch}:{r}x{c}" for ch, (r, c) in sorted(norm.items())
    )


def parse_compact_map(s) -> Optional[Dict[int, Tuple[int, int]]]:
    """解析 format_compact_map 的产物；无法解析或校验失败 → None。"""
    if not s or not isinstance(s, str):
        return None
    out: Dict[int, Tuple[int, int]] = {}
    for part in s.split(";"):
        part = part.strip()
        if not part:
            continue
        if ":" not in part or "x" not in part:
            return None
        ch_s, pos_s = part.split(":", 1)
        r_s, c_s = pos_s.split("x", 1)
        try:
            out[int(ch_s)] = (int(r_s), int(c_s))
        except ValueError:
            return None
    return out if is_valid_map(out) else None
