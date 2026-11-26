"""
tools/screenshot_plot.py — 截取 1000x720 与 1360x840 全屏 + 曲线区域。
"""
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QSize
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import QApplication

from ui.main_window import MainWindow


def take(w: int, h: int):
    out_dir = os.path.join(ROOT_DIR, "artifacts", "ui_review")
    os.makedirs(out_dir, exist_ok=True)
    win = MainWindow()
    win.show()
    QApplication.processEvents()
    win.resize(w, h)
    for _ in range(8):
        QApplication.processEvents()
    try:
        win._plot.add_point(0, 12300, 12300.0)
        win._plot.add_point(0, 12310, 12305.0)
        win._plot.add_point(0, 12290, 12295.0)
        win._plot.add_point(0, 12305, 12300.0)
        win._plot.add_point(0, 12300, 12302.0)
    except Exception:
        pass
    for _ in range(4):
        QApplication.processEvents()

    full = win.grab()
    full_path = os.path.join(out_dir, f"window_{w}x{h}.png")
    full.save(full_path, "PNG")
    print(f"  full: {full_path}  ({full.width()}x{full.height()})")

    if hasattr(win, "_plot") and win._plot is not None:
        crop = win._plot.grab()
        crop_path = os.path.join(out_dir, f"plot_{w}x{h}.png")
        crop.save(crop_path, "PNG")
        print(f"  plot: {crop_path}  ({crop.width()}x{crop.height()})")

    win.close()
    win.deleteLater()
    QApplication.processEvents()


if __name__ == "__main__":
    app = QApplication.instance() or QApplication(sys.argv)
    for w, h in [(1000, 720), (1360, 840)]:
        print(f"=== {w}x{h} ===")
        take(w, h)
    print("done")
