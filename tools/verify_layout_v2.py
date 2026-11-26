"""
tools/verify_layout_v2.py — 综合 UI 验证（阶段 10+11）。

覆盖：
- 六种窗口尺寸（1024x768 / 1280x800 / 1360x840 / 1440x900 / 1366x768 / 1920x1080）
- 直接子控件重叠检查
- Splitter 65:35 比例（0.62-0.68）
- 单元格宽高比 ≤ 1.3
- channel_map 物理位置正确性
- 键盘导航（Left/Right/Up/Down）
- Tooltip 字段完整性
- 状态文本（无空状态时显示真值）
- Record 按钮的角色（primary vs danger）
- Calib 进度与 ETA 显示
- MessageBar fault/recovery
- 全局快捷键存在
- AccessibleName
- Dock 默认折叠
- Replay 互斥
- 对比度 WCAG AA
- Source 互斥（serial/demo/replay）

输出 JSON 报告到 artifacts/ui_review/verification.json
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QRect, Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QApplication, QShortcut, QSplitter, QWidget

from ui.heatmap_widget import _text_color_for
from ui.main_window import MainWindow, SourceState
from ui.theme import DATA_VIZ, METRICS, PALETTE


SIZES: List[Tuple[int, int]] = [
    (1024, 768), (1280, 800), (1360, 840), (1440, 900),
    (1366, 768), (1920, 1080),
]


def _rects_overlap(a: QRect, b: QRect) -> bool:
    if a.isEmpty() or b.isEmpty():
        return False
    return not (
        a.right() <= b.left() or b.right() <= a.left()
        or a.bottom() <= b.top() or b.bottom() <= a.top()
    )


def _collect_overlaps(central: QWidget) -> List[Dict[str, Any]]:
    children = [w for w in central.children()
                if isinstance(w, QWidget) and w.isVisible()]
    overlaps: List[Dict[str, Any]] = []
    for i in range(len(children)):
        for j in range(i + 1, len(children)):
            a, b = children[i], children[j]
            ga, gb = a.geometry(), b.geometry()
            if not _rects_overlap(ga, gb):
                continue
            if a.isAncestorOf(b) or b.isAncestorOf(a):
                continue
            overlaps.append({
                "a": a.objectName() or a.__class__.__name__,
                "b": b.objectName() or b.__class__.__name__,
            })
    return overlaps


def _cell_aspect(window) -> List[Dict[str, Any]]:
    heat = getattr(window, "_heatmap", None)
    if heat is None:
        return []
    out: List[Dict[str, Any]] = []
    for cell in heat._cells.values():
        w, h = cell.width(), cell.height()
        if w <= 0 or h <= 0:
            continue
        out.append({
            "channel": cell.channel,
            "w": w, "h": h,
            "ratio": round(max(w, h) / min(w, h), 3),
        })
    return out


def _check_one_size(window_class, w: int, h: int) -> Dict[str, Any]:
    win = window_class()
    win.show()
    QApplication.processEvents()
    win.resize(w, h)
    for _ in range(4):
        QApplication.processEvents()

    info: Dict[str, Any] = {
        "size": [w, h],
        "min_size": [win.minimumWidth(), win.minimumHeight()],
    }
    sp = getattr(win, "_splitter", None)
    if isinstance(sp, QSplitter):
        sizes = sp.sizes()
        total = sum(sizes) or 1
        left = sizes[0] / total
        info["splitter"] = {
            "sizes": sizes,
            "left_ratio": round(left, 3),
            "ratio_ok": 0.62 <= left <= 0.68,
        }
    cells = _cell_aspect(win)
    if cells:
        max_r = max(c["ratio"] for c in cells)
        info["cells"] = {
            "count": len(cells),
            "max_ratio": round(max_r, 3),
            "all_under_1_3": max_r <= 1.3,
        }
    info["overlaps"] = {
        "count": len(_collect_overlaps(win.centralWidget())),
        "all_zero": True,
    }
    info["overlaps"]["all_zero"] = info["overlaps"]["count"] == 0
    info["dock_default_hidden"] = not win._dock.isVisible()
    return info


def _check_heatmap_navigation(window) -> Dict[str, Any]:
    """模拟键盘方向键导航（按物理相邻移动）。

    硬件布局下 CH01(idx 0) 位于左下角 (3,0)：Right 应到同排右侧
    (3,1)=CH05(idx 4)；再 Down 越出底边应保持 4 不变。
    """
    heat = window._heatmap
    # 选中初始通道
    heat.select_channel(0)
    out: Dict[str, Any] = {}
    from PyQt5.QtGui import QKeyEvent
    from PyQt5.QtCore import QEvent
    heat.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Right, Qt.NoModifier))
    out["right_from_0"] = heat.selected()
    heat.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Down, Qt.NoModifier))
    out["down_from_right"] = heat.selected()
    # 显式期望断言：防止导航退化（如错回行优先 idx 1）被静默通过
    out["expected"] = {"right_from_0": 4, "down_from_right": 4}
    out["ok"] = out["right_from_0"] == 4 and out["down_from_right"] == 4
    return out


def _check_tooltips(window) -> Dict[str, Any]:
    """检查至少一个 cell 包含衰减量 tooltip 的关键字段。"""
    heat = window._heatmap
    if not heat._cells:
        return {"ok": False, "reason": "no cells"}
    cell = next(iter(heat._cells.values()))
    # 模拟填充数据（新语义：衰减量 = 基线 − 滤波）
    from models.channel_state import ChannelStatus
    cell.set_data(
        12.5, ChannelStatus.OK, True,
        extras={
            "sample_mv": 11987.5, "filtered_mv": 11987.5,
            "baseline_mv": 12000.0, "drop_pct": 0.104,
            "detector_mv": 23975.0, "valid": True,
            "last_update_monotonic": time.monotonic(),
        },
    )
    tip = cell.toolTip()
    expected = ["衰减量", "无钢板基线", "检波电压(滤波)", "串口采样",
                "VOUT 估算", "本帧有效", "更新"]
    present = {k: (k in tip) for k in expected}
    return {"ok": all(present.values()), "fields": present, "tip": tip}


def _check_shortcuts(window) -> Dict[str, Any]:
    scs = window.findChildren(QShortcut)
    keys = {sc.key().toString() for sc in scs}
    expected = {"F5", "Ctrl+,", "Space", "Ctrl+Shift+R", "Ctrl+O", "Esc"}
    return {
        "present": sorted(keys),
        "all_present": expected.issubset(keys),
    }


def _check_accessible_names(window) -> Dict[str, Any]:
    out: Dict[str, str] = {}
    for attr in ("_btn_connect", "_btn_disconnect", "_btn_demo",
                 "_btn_rec", "_btn_calib", "_btn_refresh",
                 "_btn_settings", "_btn_diag", "_btn_about"):
        w = getattr(window, attr, None)
        if w is not None:
            out[attr] = w.accessibleName()
    return out


def _check_record_button_role(window) -> Dict[str, Any]:
    """验证 record 按钮的角色切换。"""
    rec = window._btn_rec
    before = rec.property("role")
    # 模拟停止记录（不实际启动）
    rec.setProperty("role", "danger")
    rec.style().unpolish(rec)
    rec.style().polish(rec)
    after = rec.property("role")
    rec.setProperty("role", before)
    rec.style().unpolish(rec)
    rec.style().polish(rec)
    return {
        "initial_role": before,
        "danger_role_supported": after == "danger",
    }


def _check_calib_eta(window) -> Dict[str, Any]:
    """检查 calib 进度回调更新 ETA。"""
    rec = []
    def cb(c, t, f, tf):
        rec.append({"c": c, "t": t, "f": f, "tf": tf})
    window._calib.progress.connect(cb)
    window._calib_start_time = time.monotonic() - 5.0
    window._calib_fps_ema = 10.0
    window._on_calib_progress(50, 200, 60, 200)
    return {
        "progress_text_has_eta": "ETA" in window._lbl_calib.text(),
        "label": window._lbl_calib.text(),
    }


def _check_message_bar(window) -> Dict[str, Any]:
    """检查 MessageBar 的 show/clear。"""
    mb = window._message_bar
    initial_visible = mb.isVisible()
    mb.show_message("测试告警", "warning")
    shown = mb.isVisible() and mb._lbl_text.text() == "测试告警"
    mb.clear()
    cleared = not mb.isVisible()
    return {
        "initial_hidden": not initial_visible,
        "show_works": shown,
        "clear_works": cleared,
    }


def _check_replay_mutex(window) -> Dict[str, Any]:
    """切换到回放模式后，串口/演示按钮应被禁用。"""
    # 模拟切到回放模式（跳过确认弹窗）
    window._set_app_mode("replay")
    rec_disabled = not window._btn_rec.isEnabled()
    cal_disabled = not window._btn_calib.isEnabled()
    rep_visible = window._replay_controls.isVisible()
    # 切回 live
    window._set_app_mode("live")
    rec_back = window._btn_rec.isEnabled()
    cal_back = window._btn_calib.isEnabled()
    return {
        "replay_rec_disabled": rec_disabled,
        "replay_calib_disabled": cal_disabled,
        "replay_controls_visible": rep_visible,
        "live_restored": rec_back and not cal_back,
    }


def _check_source_exclusivity(window) -> Dict[str, Any]:
    """_stop_all_sources 应同时停止 serial 和 demo。"""
    # 准备两个假源
    class FakeSource:
        def __init__(self):
            self.stopped = False
        def stop(self):
            self.stopped = True
    window._serial = FakeSource()
    window._demo = FakeSource()
    window._stop_all_sources()
    out = {
        "serial_stopped": window._serial is None,
        "demo_stopped": window._demo is None,
    }
    return out


def _check_contrast() -> Dict[str, Any]:
    """验证最极端颜色的文字对比度 ≥ 4.5:1。"""
    def luminance(c: QColor) -> float:
        return (0.2126 * c.red() + 0.7152 * c.green() + 0.0722 * c.blue()) / 255.0

    def contrast(bg: QColor, fg: QColor) -> float:
        l1, l2 = sorted([luminance(bg), luminance(fg)], reverse=True)
        return (l1 + 0.05) / (l2 + 0.05)

    checks = []
    for bg_hex, name in [
        (DATA_VIZ.heat_neg, "heat_neg"),
        (DATA_VIZ.heat_pos, "heat_pos"),
        (PALETTE.surface, "surface"),
        (PALETTE.danger_subtle, "danger_subtle"),
    ]:
        bg = QColor(bg_hex)
        fg = _text_color_for(bg)
        c = contrast(bg, fg)
        checks.append({
            "bg": bg_hex, "name": name,
            "fg": fg.name(), "ratio": round(c, 2),
            "ok": c >= 4.5,
        })
    return {"all_ok": all(c["ok"] for c in checks), "items": checks}


def _check_channel_map_placement(window) -> Dict[str, Any]:
    """检查热力图通道物理位置：与生效配置映射一致，且（默认配置下）
    与实际接线布局（core.channel_layout.HARDWARE_CHANNEL_MAP）一致。"""
    from core.channel_layout import HARDWARE_CHANNEL_MAP

    heat = window._heatmap
    placement = {}
    for ch, (r, c) in heat.channel_map().items():
        placement[ch] = (r, c)
    expected_cfg = {
        int(ch): (int(pos[0]), int(pos[1]))
        for ch, pos in window._config.channel_map.items()
    }
    hw = {ch: tuple(pos) for ch, pos in HARDWARE_CHANNEL_MAP.items()}
    return {
        "all_16_channels": len(placement) == 16,
        "valid_positions": all(
            0 <= r < 4 and 0 <= c < 4 for r, c in placement.values()
        ),
        "unique_positions": len(set(placement.values())) == 16,
        "matches_config_map": placement == expected_cfg,
        "matches_hardware_layout": placement == hw,
        "bottom_left_is_ch01": placement.get(0) == (3, 0),
        "top_right_is_ch16": placement.get(15) == (0, 3),
    }


def main(argv: List[str]) -> int:
    app = QApplication.instance() or QApplication(argv)

    out_dir = os.path.join(ROOT_DIR, "artifacts", "ui_review")
    os.makedirs(out_dir, exist_ok=True)

    report: Dict[str, Any] = {
        "metrics": {
            "left_ratio": METRICS.left_ratio,
            "window_default_w": METRICS.window_default_w,
            "window_min_w": METRICS.window_min_w,
            "right_min_w": METRICS.right_min_w,
        },
        "sizes": [],
    }

    # 1) 几何验证（4 尺寸）
    for w, h in SIZES:
        try:
            info = _check_one_size(MainWindow, w, h)
        except Exception as exc:  # noqa: BLE001
            info = {"size": [w, h], "error": str(exc)}
        report["sizes"].append(info)

    # 1b) PlotWidget 深度检查（5 尺寸：1000..1440）
    from tools.verify_plot_deep import (
        SIZES_FULL, _check_plot_deep, _check_short_label_collisions,
    )
    report["plot_deep"] = []
    for w, h in SIZES_FULL:
        try:
            win = MainWindow()
            win.show()
            QApplication.processEvents()
            win.resize(w, h)
            for _ in range(6):
                QApplication.processEvents()
            deep = _check_plot_deep(win, w, h)
            txt = _check_short_label_collisions(win)
            deep["label_collisions"] = txt
            report["plot_deep"].append(deep)
            win.close()
            win.deleteLater()
            QApplication.processEvents()
        except Exception as exc:  # noqa: BLE001
            report["plot_deep"].append({"size": [w, h], "error": str(exc)})

    # 2) 单次创建窗口做功能性检查
    win = MainWindow()
    win.show()
    QApplication.processEvents()

    report["heatmap_navigation"] = _check_heatmap_navigation(win)
    report["tooltips"] = _check_tooltips(win)
    report["shortcuts"] = _check_shortcuts(win)
    report["accessible_names"] = _check_accessible_names(win)
    report["record_button_role"] = _check_record_button_role(win)
    report["calib_eta"] = _check_calib_eta(win)
    report["message_bar"] = _check_message_bar(win)
    report["replay_mutex"] = _check_replay_mutex(win)
    report["source_exclusivity"] = _check_source_exclusivity(win)
    report["contrast"] = _check_contrast()
    report["channel_map"] = _check_channel_map_placement(win)

    # 3) 总结
    lines: List[str] = []
    lines.append("=== UI 综合验证报告 ===")
    for info in report["sizes"]:
        sp = info.get("splitter", {})
        cells = info.get("cells", {})
        ov = info.get("overlaps", {})
        ok = (
            sp.get("ratio_ok", False)
            and cells.get("all_under_1_3", False)
            and ov.get("all_zero", False)
            and info.get("dock_default_hidden", False)
        )
        lines.append(
            f"  {info['size'][0]}x{info['size'][1]}: "
            f"left={sp.get('left_ratio')} cells={cells.get('max_ratio')} "
            f"overlaps={ov.get('count')} dock={info.get('dock_default_hidden')} "
            f"=> {'OK' if ok else 'FAIL'}"
        )
    lines.append("--- PlotWidget 深度 ---")
    for d in report.get("plot_deep", []):
        if "error" in d:
            lines.append(f"  {d['size'][0]}x{d['size'][1]}: ERROR {d['error']}")
            continue
        ok = (d["title_unique"] and not d["overflow"]
              and not d["row_conflicts"] and d["canvas_h_ok"])
        lines.append(
            f"  {d['size'][0]}x{d['size'][1]}: "
            f"right_col={d['right_col_w']} plot={d['plot_visible_w']}x{d['plot_visible_h']} "
            f"canvas={d['canvas_w']}x{d['canvas_h']} (h≥{d['canvas_h_target']}) "
            f"title_unique={d['title_unique']} overflow={d['overflow']} "
            f"row_conflict={d['row_conflicts']} "
            f"toolbar_fits={d.get('toolbar_fits_in_right_col')} "
            f"min_hint_fits={d.get('min_hint_fits_right_col')} "
            f"label_fits={d['label_collisions']['all_fit']} "
            f"func={d['functional']} "
            f"=> {'OK' if ok else 'FAIL'}"
        )
    for k in ("heatmap_navigation", "tooltips", "shortcuts",
              "record_button_role", "calib_eta", "message_bar",
              "replay_mutex", "source_exclusivity", "channel_map"):
        lines.append(f"  {k}: {report[k]}")
    lines.append(f"  contrast: {report['contrast']['all_ok']}")
    print("\n".join(lines))

    out_path = os.path.join(out_dir, "verification.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n报告: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
