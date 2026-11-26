"""
tools/verify_plot_deep.py — PlotWidget 深度布局验证（独立模块）。
"""


PLOT_TOOL_NAMES = {
    "_btn_pause", "_btn_clear",
    "_btn_autorange", "_btn_restore",
    "_chk_raw", "_chk_filt",
}

CANVAS_TARGET_H = {
    1000: 180, 1024: 190, 1280: 210, 1360: 230, 1440: 260,
}

SIZES_FULL = [
    (1000, 720), (1024, 768), (1280, 800), (1360, 840), (1440, 900),
]


def _right_column_width(window):
    sp = getattr(window, "_splitter", None)
    if sp is None:
        return 0
    sizes = sp.sizes()
    if len(sizes) < 2:
        return 0
    return sizes[1]


def _check_plot_deep(window, w, h):
    from typing import Any, Dict, List
    from PyQt5.QtWidgets import QLabel

    out = {
        "size": [w, h],
        "right_col_w": _right_column_width(window),
        "plot_visible_w": window._plot.width(),
        "plot_visible_h": window._plot.height(),
        "plot_min_hint_w": window._plot.minimumSizeHint().width(),
        "plot_min_hint_h": window._plot.minimumSizeHint().height(),
        "canvas_w": window._plot._plot.width(),
        "canvas_h": window._plot._plot.height(),
    }
    out["canvas_h_target"] = CANVAS_TARGET_H.get(w, 200)
    out["canvas_h_ok"] = out["canvas_h"] >= out["canvas_h_target"]

    # 1) 唯一"实时曲线"标题
    titles = []
    for lab in window._plot.findChildren(QLabel):
        if lab.text() == "实时曲线" and lab.isVisible():
            g = lab.geometry()
            titles.append((lab.objectName() or "?", g.x(), g.y()))
    out["title_count"] = len(titles)
    out["title_unique"] = len(titles) == 1

    # 2) 每个工具栏控件必须落在 PlotWidget 可见区域内
    #    btn.geometry() 是相对父控件 (PlotWidget) 的坐标；与 (0,0,W,H) 比较
    plot_w = window._plot.width()
    plot_h = window._plot.height()
    controls_out = []
    overflow = []
    for name in PLOT_TOOL_NAMES:
        btn = getattr(window._plot, name, None)
        if btn is None:
            controls_out.append({"name": name, "missing": True})
            continue
        gb = btn.geometry()  # 相对 PlotWidget
        inside = (
            0 <= gb.left() and 0 <= gb.top()
            and gb.right() <= plot_w and gb.bottom() <= plot_h
        )
        controls_out.append({
            "name": name,
            "x": gb.x(), "y": gb.y(),
            "w": gb.width(), "h": gb.height(),
            "text": btn.text() if hasattr(btn, "text") else "",
            "visible": btn.isVisible(),
            "inside_plot": inside,
        })
        if not inside:
            overflow.append(name)
    out["controls"] = controls_out
    out["overflow"] = overflow

    # 3) 同行的控件几何不应相交
    visible = [c for c in controls_out if c.get("visible") and "x" in c]
    rows = {}
    for c in visible:
        cy = c["y"] + c["h"] / 2.0
        matched = None
        for ry in list(rows.keys()):
            if abs(ry - cy) < 14:
                matched = ry
                break
        key = matched if matched is not None else cy
        rows.setdefault(key, []).append(c)
    row_conflicts = []
    for ry, lst in rows.items():
        for i in range(len(lst)):
            for j in range(i + 1, len(lst)):
                a, b = lst[i], lst[j]
                ax2 = a["x"] + a["w"]
                bx2 = b["x"] + b["w"]
                ay2 = a["y"] + a["h"]
                by2 = b["y"] + b["h"]
                if not (ax2 <= b["x"] or bx2 <= a["x"]
                        or ay2 <= b["y"] or by2 <= a["y"]):
                    row_conflicts.append({
                        "a": a["name"], "b": b["name"],
                        "y_center": ry,
                    })
    out["row_conflicts"] = row_conflicts

    # 4) 工具栏宽度不应超过右列
    #    所有 c["x"]/c["w"] 已是 PlotWidget 局部坐标；plot_w == 右列宽减去小边距
    rt = _right_column_width(window)
    if visible:
        toolbar_right = max(c["x"] + c["w"] for c in visible)
        out["toolbar_right_in_plot"] = toolbar_right
        out["plot_width"] = plot_w
        out["toolbar_fits_in_right_col"] = (
            toolbar_right <= rt if rt else True
        )
    else:
        out["toolbar_fits_in_right_col"] = True

    out["min_hint_fits_right_col"] = (
        out["plot_min_hint_w"] <= rt if rt else True
    )

    # 5) 功能性
    func = {}
    try:
        before_pause = window._plot._btn_pause.isChecked()
        window._plot._btn_pause.toggle()
        after_pause = window._plot._btn_pause.isChecked()
        func["pause_toggle_changes_state"] = before_pause != after_pause
        window._plot._btn_pause.setChecked(before_pause)

        window._plot.add_point(0, 12345, 12340.5)
        window._plot.add_point(1, 8000, 7998.0)
        func["raw_curve_present"] = 0 in window._plot._curves_raw
        func["filt_curve_present"] = 0 in window._plot._curves_filt

        window._plot._chk_raw.setChecked(False)
        window._plot._chk_filt.setChecked(False)
        window._plot._on_visibility_toggled()
        func["raw_hidden_when_unchecked"] = (
            not window._plot._curves_raw[0].isVisible()
        )
        func["filt_hidden_when_unchecked"] = (
            not window._plot._curves_filt[0].isVisible()
        )
        window._plot._chk_raw.setChecked(True)
        window._plot._chk_filt.setChecked(True)
        window._plot._on_visibility_toggled()

        window._plot.clear()
        func["clear_empties_curves"] = (
            len(window._plot._curves_raw) == 0
            and len(window._plot._curves_filt) == 0
        )

        try:
            window._plot._on_autorange()
            window._plot._on_restore()
            func["autorange_restore_no_error"] = True
        except Exception:
            func["autorange_restore_no_error"] = False
    except Exception as exc:  # noqa: BLE001
        func["error"] = str(exc)
    out["functional"] = func

    # 6) ChannelPanel 紧凑 3x2
    panel = window._channel_panel
    metrics = []
    for attr in ("_m_raw", "_m_filtered", "_m_vadc",
                 "_m_baseline", "_m_vout", "_m_age"):
        m = getattr(panel, attr, None)
        if m is None:
            metrics.append({"name": attr, "missing": True})
            continue
        metrics.append({
            "name": attr, "w": m.width(), "h": m.height(),
            "label": m._lbl.text(),
            "tooltip": (m._lbl.toolTip() or "")[:24],
        })
    out["channel_panel_metrics"] = metrics

    return out


def _check_short_label_collisions(win):
    out = []
    for name in PLOT_TOOL_NAMES:
        btn = getattr(win._plot, name, None)
        if btn is None or not btn.isVisible():
            continue
        fm = btn.fontMetrics()
        text_w = fm.horizontalAdvance(btn.text())
        out.append({
            "name": name, "text": btn.text(),
            "btn_w": btn.width(), "text_w": text_w,
            "fits": text_w + 16 <= btn.width(),
        })
    return {"items": out, "all_fit": all(x.get("fits", True) for x in out)}
