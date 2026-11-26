"""
涡流传感器采集系统 v3 (毫伏协议) - 上位机程序入口

启动方式:
    python main.py              正常启动（连接真实串口）
    python main.py --demo       演示模式（内置模拟数据，无需硬件）
"""

import os
import sys

# 确保当前目录在路径中（模块导入需要）
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir:
    os.chdir(_script_dir)
    if _script_dir not in sys.path:
        sys.path.insert(0, _script_dir)

from app_paths import ensure_paths
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QApplication

from ui.main_window import MainWindow
from ui.theme import PALETTE, build_stylesheet, font_family_ui


def main():
    paths_info = ensure_paths()
    if paths_info["error"]:
        print(f"路径警告: {paths_info['error']}")
    if paths_info["migrated"]:
        print("已从旧路径迁移 config.json")

    app = QApplication(sys.argv)
    app.setApplicationName("涡流阵列采集系统 v3")
    # 应用统一主题
    app.setStyleSheet(build_stylesheet())
    # 统一字体
    app.setFont(QFont(font_family_ui().split(",")[0].strip(), 10))

    window = MainWindow()
    window.show()

    if "--demo" in sys.argv:
        window.setWindowTitle("涡流阵列采集系统 v3 [演示模式]")
        window._start_demo()

    # 打包 EXE 自验收钩子（隐藏参数，正常用户不使用）：
    #   --autotest-exit-after=秒 --autotest-report=路径
    # 到时间后把运行状态（帧计数/源状态/标定/记录）写入报告文件并退出，
    # 用于不依赖源码运行的 EXE 冒烟验收。
    def _arg_value(prefix: str) -> str:
        for a in sys.argv:
            if a.startswith(prefix):
                return a[len(prefix):]
        return ""

    try:
        _autotest_secs = float(_arg_value("--autotest-exit-after=") or 0)
    except ValueError:
        _autotest_secs = 0
    _autotest_report = _arg_value("--autotest-report=")
    _autotest_shot = _arg_value("--autotest-screenshot=")
    # EXE 验收钩子：在报告前切换布局/Profile，证明"有源板布局可选、
    # 分压可独立选择"在打包程序里确实生效（不影响正常启动）
    _autotest_layout = _arg_value("--autotest-layout=")
    _autotest_profile = _arg_value("--autotest-profile=")
    _autotest_binding = _arg_value("--autotest-binding=")
    if _autotest_layout:
        window._config.layout_id = _autotest_layout
        if _autotest_binding:
            window._config.cell_binding = _autotest_binding
        window._apply_channel_map_to_heatmap()
        window._apply_scope_baselines()
        window._update_all_displays()
    if _autotest_profile:
        from models.adc_profile import build_default_profiles, profile_by_name
        _prof = profile_by_name(
            _autotest_profile, build_default_profiles()
        )
        window._config.adc_profile = _prof
        window._processor.set_adc_profile(_prof)
        window._update_all_displays()
    if _autotest_secs > 0 and _autotest_report:
        from PyQt5.QtCore import QTimer

        def _write_autotest_report():
            try:
                frames = int(getattr(window, "_frame_count", 0))
                demo = getattr(window, "_demo", None)
                from core.recorder import APP_VERSION
                lines = [
                    "AUTOTEST_REPORT",
                    f"app_version={APP_VERSION}",
                    f"frames={frames}",
                    f"demo_running={bool(demo is not None and demo.is_running())}",
                    f"source_state={getattr(window, '_source_state', None)}",
                    f"recording={window._recorder.is_recording}",
                    f"layout={window._config.layout_id}",
                    f"binding={window._config.cell_binding}",
                    f"binding_confirmed={window._config.binding_confirmed}",
                    f"profile={window._config.adc_profile.name}",
                    "divider="
                    f"{window._config.adc_profile.detector_divider:g}",
                ]
                # 热力图实际排布（channel -> row,col 与单元格标签），
                # 供 EXE 验收核对 16 通道物理位置映射
                try:
                    heat = getattr(window, "_heatmap", None)
                    if heat is not None:
                        for ch in range(16):
                            pos = heat.channel_position(ch)
                            cell = heat._cells.get(pos) if pos else None
                            label = (
                                f"{cell.title}/{cell.position_label}"
                                f"/data{cell.channel + 1}"
                                if cell is not None else ""
                            )
                            lines.append(
                                f"map_ch{ch:02d}={pos[0]},{pos[1]}|{label}"
                                if pos else f"map_ch{ch:02d}=MISSING"
                            )
                except Exception as e:
                    lines.append(f"map_dump_error={e}")
                lines.append(
                    "result=OK" if frames > 0 else "result=NO_FRAMES"
                )
                with open(_autotest_report, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
                # 界面截图（可选）：核对热力图 16 通道实际排布
                if _autotest_shot:
                    try:
                        window.grab().save(_autotest_shot)
                    except Exception:
                        pass
            except Exception as e:  # 报告失败也要退出，避免验收脚本悬挂
                try:
                    with open(_autotest_report, "w", encoding="utf-8") as f:
                        f.write(f"AUTOTEST_REPORT\nresult=ERROR\nerror={e}\n")
                except Exception:
                    pass
            app.quit()

        QTimer.singleShot(int(_autotest_secs * 1000), _write_autotest_report)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
