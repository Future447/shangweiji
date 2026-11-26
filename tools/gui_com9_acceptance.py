# -*- coding: utf-8 -*-
"""
COM9 GUI 实机验收（offscreen 驱动完整 MainWindow，不依赖人工操作）。

步骤：
1. MainWindow 启动 → 选 COM9 → 连接
2. 等待 D 帧：状态必须从"协议不匹配/等待"变为"接收中"
3. 16 通道实时数据：sample_mv 在量程内且 0.125 步进
4. 热力图 / 通道面板数据更新（单元格衰减量文本、面板 mV 文本）
5. 无钢板基线标定（小 target，自动确认无钢板对话框）→ 基线写入（mV）
   → 衰减量 ≈ 0（空气中标定后）
6. CSV 记录 ~4s → 文件含 schema v4 / measurement_unit=mV /
   attenuation_formula / 衰减量列 / 3 位小数
7. 回放刚记录的 CSV → v4 加载成功、wire_mv 精度保持、
   回放衰减量直接取保存值（不二次翻转）

本脚本与上位机本身一样：绝不向 COM9 写入任何数据。
"""

import csv
import os
import sys
import time

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QCoreApplication
from PyQt5.QtWidgets import QApplication

from models.frame import is_valid_mv_value


def pump(ms: int = 30) -> None:
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.005)


def main() -> int:
    app = QApplication.instance() or QApplication([])
    from ui.main_window import MainWindow

    failures = []

    win = MainWindow()
    win.show()
    pump(100)
    # 自动确认"无钢板"标定前对话框（实机验收无人值守）
    win._confirm_plate_removed = lambda: True

    # --- 1. 选择 COM9 并连接 ---
    idx = win._cmb_port.findData("COM9")
    if idx < 0:
        win._update_port_list()
        pump(50)
        idx = win._cmb_port.findData("COM9")
    if idx < 0:
        print("FAIL: 端口列表中没有 COM9")
        return 1
    win._cmb_port.setCurrentIndex(idx)
    win._on_connect_clicked()
    pump(500)
    if win._serial is None:
        print("FAIL: 串口未能打开 COM9")
        return 1
    print("1) COM9 已连接")

    try:
        # --- 2. 等待 D 帧 → 状态应为"接收中" ---
        deadline = time.monotonic() + 8.0
        while win._frame_count < 5 and time.monotonic() < deadline:
            pump(200)
        win._on_refresh()
        state_text = win._lbl_state.text()
        print(f"2) 状态栏: {state_text!r}  帧数: {win._frame_count}")
        if win._frame_count < 5:
            failures.append(f"帧数不足: {win._frame_count}")
        if state_text != "接收中":
            failures.append(f"状态不是'接收中': {state_text!r}")

        # --- 3. 16 通道实时数据校验 ---
        df = win._last_dframe
        if df is None:
            failures.append("无 D 帧")
        else:
            bad = 0
            for ch in range(16):
                if df.is_valid(ch) and not is_valid_mv_value(
                    df.samples_mv[ch]
                ):
                    bad += 1
            if bad:
                failures.append(f"{bad} 个通道样本非法")
            st0 = win._processor.get(0)
            print(
                f"3) CH01 sample={st0.sample_mv:.3f} mV "
                f"filtered={st0.filtered_mv:.3f} mV "
                f"valid_mask={df.valid_mask}"
            )
            if st0.filtered_mv is None:
                failures.append("滤波值为 None")

        # --- 4. 热力图 / 通道面板更新 ---
        pump(300)
        win._update_all_displays()
        ch0_pos = win._config.channel_map.get(0, (0, 0))
        cell = win._heatmap._cells[ch0_pos]
        panel = win._channel_panel
        print(
            f"4) 热力图 cell0 文本: {cell.value_text!r}  "
            f"tooltip 含衰减量: {'衰减量' in cell.toolTip()}"
        )
        if "衰减量" not in cell.toolTip():
            failures.append("热力图 tooltip 不含衰减量")
        sample_text = panel._m_raw._val.text()
        print(
            f"   通道面板采样: {sample_text!r} "
            f"VOUT: {panel._m_vout._val.text()!r}"
        )
        if sample_text in ("—", ""):
            failures.append("通道面板采样无数据")

        # --- 5. 无钢板基线标定（target=8，约 3 秒）---
        win._sp_calib.setValue(8)
        win._on_calib_clicked()
        t_end = time.monotonic() + 20.0
        while win._calib.is_running() and time.monotonic() < t_end:
            pump(200)
        if win._calib.is_running():
            win._calib.cancel()
            failures.append("标定超时未完成")
        else:
            none_ch = [
                c for c in win._processor.enabled_channels
                if win._processor.get(c).baseline_mv is None
            ]
            b0 = win._processor.get(0).baseline_mv
            print(f"5) 标定完成  CH01 基线: {b0:.3f} mV  缺基线通道: {none_ch}")
            if none_ch:
                failures.append(f"标定后仍缺基线: {none_ch}")
            # 衰减量应接近 0（空气中刚标定完）
            snap0 = win._processor.measurement(0, True)
            print(
                f"   标定后 CH01 衰减量: {snap0.attenuation_mv} mV  "
                f"下降比例: {snap0.detector_drop_pct}"
            )
            if snap0.attenuation_mv is None:
                failures.append("标定后衰减量为 None")
            elif abs(snap0.attenuation_mv) > 50.0:
                failures.append(f"标定后衰减量异常: {snap0.attenuation_mv}")
            # 基线应保存到 serial 作用域且带来源 meta
            meta = win._config.get_baseline_meta("serial")
            if meta is None or not meta.get("baseline_id"):
                failures.append("serial 作用域基线 meta 缺失")

        # --- 6. CSV 记录 ~4s ---
        import tempfile
        csv_dir = tempfile.mkdtemp(prefix="com9_csv_")
        win._config.last_csv_dir = csv_dir
        win._btn_rec.setChecked(True)
        pump(4000)
        win._btn_rec.setChecked(False)
        pump(100)
        csvs = [f for f in os.listdir(csv_dir) if f.endswith(".csv")]
        if not csvs:
            failures.append("未生成 CSV")
        else:
            path = os.path.join(csv_dir, csvs[0])
            with open(path, encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            print(f"6) CSV: {csvs[0]}  数据行: {len(rows)}")
            if not rows:
                failures.append("CSV 无数据行")
            else:
                r0 = rows[0]
                if r0.get("schema_version") != "4":
                    failures.append(f"schema_version={r0.get('schema_version')}")
                if r0.get("measurement_unit") != "mV":
                    failures.append("measurement_unit != mV")
                if not r0.get("attenuation_formula", "").startswith(
                    "attenuation_mv=baseline_mv-filtered_mv"
                ):
                    failures.append(
                        f"attenuation_formula 异常: "
                        f"{r0.get('attenuation_formula')!r}"
                    )
                if "ch01_attenuation_mv" not in r0:
                    failures.append("缺少 ch01_attenuation_mv 列")
                wire = r0.get("ch01_wire_mv", "")
                try:
                    float(wire)
                    decimals = len(wire.split(".")[1])
                    if decimals != 3:
                        failures.append(f"wire_mv 小数位 {decimals} != 3")
                except (ValueError, IndexError):
                    failures.append(f"wire_mv 非数值: {wire!r}")
                print(f"   schema={r0.get('schema_version')} "
                      f"unit={r0.get('measurement_unit')} "
                      f"ch01_wire_mv={wire!r} "
                      f"ch01_baseline_mv={r0.get('ch01_baseline_mv')!r} "
                      f"ch01_attenuation_mv={r0.get('ch01_attenuation_mv')!r}")

                # --- 7. 回放刚记录的 CSV ---
                from core.replay_source import ReplaySource
                rp = ReplaySource()
                try:
                    rp.load(path)
                    print(
                        f"7) 回放加载 OK  rows={rp.total_rows} "
                        f"schema={rp.schema_version} ch={rp.n_channels}"
                    )
                    frames = []
                    rp.frame_ready.connect(lambda row: frames.append(row))
                    rp.play()
                    rp._on_tick()
                    rp.stop()
                    if not frames:
                        failures.append("回放无帧")
                    else:
                        w0 = frames[0].wire_mv[0]
                        # 回放首帧 = CSV 第 1 个数据行（首行不再被消费）
                        expect = float(rows[0]["ch01_wire_mv"])
                        print(f"   回放 ch01 wire_mv={w0:.3f} (期望 {expect:.3f})")
                        if abs(w0 - expect) > 1e-9:
                            failures.append(
                                f"回放精度丢失: {w0} vs {expect}"
                            )
                        # v4：回放衰减量直接取保存值（不二次翻转/重算）
                        saved_att = rows[0].get("ch01_attenuation_mv", "")
                        replay_att = frames[0].attenuation[0]
                        if saved_att == "":
                            if replay_att is not None:
                                failures.append(
                                    f"保存衰减为空但回放为 {replay_att}"
                                )
                        else:
                            if replay_att is None or abs(
                                replay_att - float(saved_att)
                            ) > 1e-9:
                                failures.append(
                                    f"回放衰减量被改变: {replay_att} "
                                    f"vs 保存 {saved_att}"
                                )
                except Exception as e:
                    failures.append(f"回放加载失败: {e}")
    finally:
        try:
            win._refresh_timer.stop()
        except Exception:
            pass
        try:
            win._calib.cancel()
        except Exception:
            pass
        try:
            win._recorder.stop_recording()
        except Exception:
            pass
        try:
            win._on_disconnect_clicked()
        except Exception:
            pass
        pump(300)
        win.close()
        win.deleteLater()
        pump(100)

    print("=" * 60)
    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("PASS: COM9 GUI 实机验收全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
