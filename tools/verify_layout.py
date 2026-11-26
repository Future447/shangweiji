"""
tools/verify_layout.py — 在四种窗口尺寸下验证主窗口布局。

读取真实几何（splitter.sizes、cell 宽高比、child overlap）。
不修改源码；只做"读取"并把结果以 JSON 写到 stdout。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Tuple

# 确保项目根目录在 sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# 使用 offscreen 平台避免实际显示窗口
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QRect
from PyQt5.QtWidgets import QApplication, QSplitter, QWidget

from ui.main_window import MainWindow
from ui.theme import METRICS


SIZES: List[Tuple[int, int]] = [
    (1024, 768),
    (1280, 800),
    (1360, 840),
    (1440, 900),
]


def rects_overlap(a: QRect, b: QRect) -> bool:
    if a.isEmpty() or b.isEmpty():
        return False
    return not (
        a.right() <= b.left()
        or b.right() <= a.left()
        or a.bottom() <= b.top()
        or b.bottom() <= a.top()
    )


def collect_overlaps(central: QWidget) -> List[Dict[str, Any]]:
    """收集直接子控件之间的非父子重叠。"""
    children = [w for w in central.children() if isinstance(w, QWidget) and w.isVisible()]
    overlaps: List[Dict[str, Any]] = []
    for i in range(len(children)):
        for j in range(i + 1, len(children)):
            a = children[i]
            b = children[j]
            ga = a.geometry()
            gb = b.geometry()
            if not rects_overlap(ga, gb):
                continue
            if a.isAncestorOf(b) or b.isAncestorOf(a):
                continue
            overlaps.append({
                "a": a.objectName() or a.__class__.__name__,
                "b": b.objectName() or b.__class__.__name__,
                "a_rect": [ga.x(), ga.y(), ga.width(), ga.height()],
                "b_rect": [gb.x(), gb.y(), gb.width(), gb.height()],
            })
    return overlaps


def collect_cell_aspect(window) -> List[Dict[str, Any]]:
    """收集热力图所有单元格的宽高比。"""
    heat = getattr(window, "_heatmap", None)
    if heat is None:
        return []
    out: List[Dict[str, Any]] = []
    for ch, cell in heat._cells.items():
        w = cell.width()
        h = cell.height()
        if w <= 0 or h <= 0:
            continue
        ratio = max(w, h) / min(w, h)
        out.append({
            "channel": ch,
            "w": w,
            "h": h,
            "ratio": round(ratio, 3),
        })
    return out


def run_size(window_class, w: int, h: int) -> Dict[str, Any]:
    window = window_class()
    window.show()
    QApplication.processEvents()
    window.resize(w, h)
    for _ in range(3):
        QApplication.processEvents()

    info: Dict[str, Any] = {
        "size": [w, h],
        "min_size": [window.minimumWidth(), window.minimumHeight()],
        "central_size": [window.centralWidget().width(), window.centralWidget().height()],
    }

    splitter = getattr(window, "_splitter", None)
    if isinstance(splitter, QSplitter):
        sizes = splitter.sizes()
        total = sum(sizes) or 1
        info["splitter"] = {
            "sizes": sizes,
            "left_ratio": round(sizes[0] / total, 3) if total else None,
        }

    op_bar = getattr(window, "_operation_bar", None)
    if op_bar is not None:
        g = op_bar.geometry()
        info["operation_bar"] = {"y": g.y(), "h": g.height()}

    cmd_bar = getattr(window, "_command_bar", None)
    if cmd_bar is not None:
        g = cmd_bar.geometry()
        info["command_bar"] = {"y": g.y(), "h": g.height()}

    status_bar = getattr(window, "_global_status_bar", None)
    if status_bar is not None:
        g = status_bar.geometry()
        info["global_status"] = {"y": g.y(), "h": g.height()}

    info["cells"] = collect_cell_aspect(window)
    info["overlaps"] = collect_overlaps(window.centralWidget())

    return info


def main(argv: List[str]) -> int:
    app = QApplication.instance() or QApplication(argv)
    report: Dict[str, Any] = {
        "metrics": {
            "left_ratio": METRICS.left_ratio,
            "window_default_w": METRICS.window_default_w,
            "window_min_w": METRICS.window_min_w,
            "right_min_w": METRICS.right_min_w,
        },
        "sizes": [],
    }

    for w, h in SIZES:
        try:
            info = run_size(MainWindow, w, h)
        except Exception as exc:  # noqa: BLE001
            info = {"size": [w, h], "error": str(exc)}
        report["sizes"].append(info)

    summary: List[str] = []
    for info in report["sizes"]:
        size = info.get("size", [0, 0])
        min_w = info.get("min_size", [0, 0])[0]
        if "error" in info:
            summary.append(f"  {size[0]}x{size[1]}: ERROR {info['error']}")
            continue
        sp = info.get("splitter", {})
        left_ratio = sp.get("left_ratio")
        cell_count = len(info.get("cells", []))
        cell_max_ratio = max((c["ratio"] for c in info.get("cells", [])), default=0)
        overlap_count = len(info.get("overlaps", []))
        summary.append(
            f"  {size[0]}x{size[1]}: min_w={min_w} left_ratio={left_ratio} "
            f"cells={cell_count} max_cell_ratio={cell_max_ratio} overlaps={overlap_count}"
        )

    print("=== Layout verification report ===")
    print("\n".join(summary))
    print()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
