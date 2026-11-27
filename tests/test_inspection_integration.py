"""Native inspection workflows through parser, widgets, reference JSON and CSV replay."""

import csv
import json
import os
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QFileDialog, QPushButton

from core.channel_layout import BINDING_DESIGN, LAYOUT_ACTIVE_PCB
from core.config_manager import ConfigManager, SCOPE_SERIAL
from core.recorder import Recorder
from models.frame import DFrame


BASE = np.arange(300.0, 316.0)


@pytest.fixture
def window(monkeypatch, tmp_path, qapp):
    """Keep all config/recordings in temporary storage and never open a worker."""
    import app_paths
    import ui.main_window as main_module

    config_path = tmp_path / "config.json"
    config = ConfigManager(config_path=str(config_path))
    config.layout_id = LAYOUT_ACTIVE_PCB
    config.cell_binding = BINDING_DESIGN
    config.enabled_channels = list(range(16))
    config.save()
    monkeypatch.setattr(main_module, "ConfigManager", lambda: ConfigManager(config_path=str(config_path)))
    monkeypatch.setattr(main_module, "list_serial_ports", lambda: [])
    monkeypatch.setattr(app_paths, "get_default_csv_dir", lambda: tmp_path / "recordings")
    win = main_module.MainWindow()
    win._refresh_timer.stop()
    win._processor.set_ema_alpha(1.0)
    win._processor.set_median_window(1)
    try:
        yield win
    finally:
        win._refresh_timer.stop()
        win._stop_all_sources()
        win._recorder.stop_recording()
        win.close()
        qapp.processEvents()


def connect_live(win):
    win._start_new_session(SCOPE_SERIAL)
    win._pending_device_id = "USB VID:PID=0483:5740 SER=INSPECTION_TEST LOCATION=1-2"
    win._on_serial_connected("COM_TEST")
    counter = 0

    def feed(values=BASE, *, mask=0xFFFF, status=0):
        nonlocal counter
        counter += 1
        numbers = ",".join(f"{value:.3f}" for value in values)
        win._on_serial_data(f"D,{counter},{status},{mask},0,mV,{numbers}\r\n".encode("ascii"))
        win._update_all_displays()

    return feed


def mode_button(win, mode):
    return win.findChild(QPushButton, f"inspectionMode_{mode}")


def capture_compensation(win, feed):
    mode_button(win, "compensation").click()
    panel = win._compensation
    panel.frame_count.setValue(3)
    feed()
    assert panel.baseline_button.isEnabled()
    panel.baseline_button.click()
    for _ in range(3):
        feed()
    assert panel.active_profile is not None
    mode_button(win, "fatigue").click()
    assert win._task_mode == "fatigue"


def capture_reference(win, feed, values=BASE):
    panel = win._inspection_panel
    panel.frame_count.setValue(3)
    feed(values)
    assert panel.capture_button.isEnabled()
    panel.capture_button.click()
    assert win._inspection.capture is not None
    for _ in range(3):
        feed(values)
    assert win._inspection.capture is None
    return win._inspection.references[win._inspection.mode]


def test_live_mode_buttons_capture_isolation_signed_mv_and_invalid_mask(window):
    win = window
    feed = connect_live(win)
    capture_compensation(win, feed)
    panel, controller = win._inspection_panel, win._inspection
    panel.frame_count.setValue(3)
    feed()
    panel.capture_button.click()
    feed()
    assert len(controller.capture[0]) == 1  # The pre-click frame was not captured.
    mode_button(win, "weld").click()
    assert controller.capture is None
    assert not controller.references
    assert controller.mode == "weld"
    weld_reference = capture_reference(win, feed, BASE + 10)

    mode_button(win, "fatigue").click()
    fatigue_reference = capture_reference(win, feed)
    assert controller.references["weld"] is weld_reference
    assert fatigue_reference is not weld_reference
    anomaly = BASE.copy()
    anomaly[0] -= 1.0
    anomaly[1] += 1.0
    feed(anomaly)
    _, raw, residual, scores = panel._frames[-1]
    np.testing.assert_allclose(raw, anomaly)
    np.testing.assert_allclose(residual[:2], [-1, 1])
    np.testing.assert_allclose(scores[:2], [8, 8])
    assert "疑似异常" in panel.status_label.text()
    assert controller.event_count == 1
    assert "mV" in panel.summary_label.text()
    for channel, expected in ((0, -1.0), (1, 1.0)):
        cell = win._heatmap._cells[win._heatmap.channel_map()[channel]]
        assert cell.display_mode == "residual"
        assert cell.corrected_value == pytest.approx(expected)

    feed(anomaly, mask=0xFFFE)
    assert np.isnan(panel._frames[-1][2][0])
    assert np.isnan(panel._frames[-1][3][0])
    assert not np.isfinite(panel._frames[-1][3]).any()
    assert "16 路" in panel.status_label.text()
    assert win._heatmap._cells[win._heatmap.channel_map()[0]].corrected_value is None
    feed(anomaly, status=1)
    assert not np.isfinite(panel._frames[-1][3]).any()
    assert not panel.capture_button.isEnabled()

    panel.threshold_spin.setValue(9)
    mode_button(win, "weld").click()
    assert panel.threshold_spin.value() == 5
    feed(BASE + 10)
    np.testing.assert_allclose(panel._frames[-1][2], 0)
    assert controller.event_count == 0
    mode_button(win, "fatigue").click()
    assert panel.threshold_spin.value() == 9
    assert controller.references["fatigue"] is fatigue_reference


def test_reference_json_buttons_roundtrip_and_wrong_mode(window, monkeypatch, tmp_path):
    win = window
    feed = connect_live(win)
    capture_compensation(win, feed)
    capture_reference(win, feed)
    panel, controller = win._inspection_panel, win._inspection
    panel.threshold_spin.setValue(7.5)
    path = tmp_path / "fatigue-reference.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(path), "JSON"))
    panel.save_button.click()
    assert json.loads(path.read_text(encoding="utf-8"))["mode"] == "fatigue"
    panel.clear_button.click()
    assert "fatigue" not in controller.references
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), "JSON"))
    panel.load_button.click()
    assert panel.threshold_spin.value() == 7.5
    sample = BASE + 3
    sample[4] += 0.5
    feed(sample)
    expected = np.zeros(16)
    expected[4] = 0.5
    np.testing.assert_allclose(panel._frames[-1][2], expected)
    np.testing.assert_allclose(panel._frames[-1][3], expected / 0.125)

    mode_button(win, "weld").click()
    panel.load_button.click()
    assert "另一检测模式" in panel.status_label.text()
    assert "weld" not in controller.references
    assert "fatigue" in controller.references


@pytest.mark.parametrize("mismatch,reason", [("map", "物理布局"), ("gain", "增益")])
def test_loaded_reference_context_mismatch_never_scores(window, tmp_path, mismatch, reason):
    win = window
    feed = connect_live(win)
    capture_compensation(win, feed)
    capture_reference(win, feed)
    path = tmp_path / "reference.json"
    win._inspection.save_reference(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if mismatch == "map":
        mapping = data["context"]["map"]
        mapping["0"], mapping["1"] = mapping["1"], mapping["0"]
    else:
        data["context"]["gain"] = "different-gain"
    path.write_text(json.dumps(data), encoding="utf-8")
    win._inspection.load_reference(path)
    feed(BASE + 10)
    assert reason in win._inspection_panel.status_label.text()
    assert not np.isfinite(win._inspection_panel._frames[-1][2]).any()
    assert not np.isfinite(win._inspection_panel._frames[-1][3]).any()
    assert win._inspection.event_count == 0


def record_csv(win, directory, frames, positions=None):
    """Real recorder schema; add only explicit fixture positions and known times."""
    recorder = Recorder()
    path = Path(recorder.start_recording(
        win._config.experiment_params, str(directory), win._config.adc_profile,
        session_meta={"source": "serial", "baseline_device_id": "UNRELATED_BASELINE_BOARD"},
    ))
    try:
        for index, values in enumerate(frames):
            frame = DFrame(index, 0, 0xFFFF, 0, tuple(values))
            win._processor.ingest(frame)
            ok, error = recorder.write_dframe(frame, win._processor, channel_map=win._heatmap.channel_map())
            assert ok, error
    finally:
        recorder.stop_recording()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields, rows = list(reader.fieldnames), list(reader)
    if positions is not None:
        fields.append("position_mm")
    for index, row in enumerate(rows):
        row["相对时间/s"] = str(index * 0.29)
        if positions is not None:
            row["position_mm"] = str(positions[index])
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


@pytest.mark.parametrize("located", [False, True])
def test_recorder_csv_import_and_eager_mainwindow_replay(window, monkeypatch, tmp_path, located):
    win = window
    feed = connect_live(win)
    capture_compensation(win, feed)
    calibration_path = tmp_path / "common.json"
    win._compensation.save_calibration(calibration_path)
    reference_positions = [0, 0, 0, 10, 10, 10] if located else None
    reference_frames = [BASE + position * 0.5 for position in reference_positions] if located else [BASE] * 3
    reference_path = record_csv(win, tmp_path / "good", reference_frames, reference_positions)
    normal = BASE + 2.5 if located else BASE.copy()
    anomaly = normal.copy()
    anomaly[3] += 1
    test_frames = [normal, anomaly, normal]
    test_positions = [5, 5, 20] if located else None
    test_path = record_csv(win, tmp_path / "test", test_frames, test_positions)

    win._set_app_mode("replay")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(test_path), "CSV"))
    win._on_replay_open()
    win._compensation.load_calibration(calibration_path)
    assert win._compensation.active_profile is not None
    controller, panel = win._inspection, win._inspection_panel
    controller.import_reference_csv(reference_path)
    assert controller.references["fatigue"].reference.has_positions == located
    assert controller.references["fatigue"].context["device"] == ""
    win._on_replay_play()
    win._replay._start_monotonic -= 10  # Make every recorded timestamp due in one tick.
    win._replay._on_tick()
    assert win._replay.state == "ended"
    assert [frame[0] for frame in panel._frames] == [0, 0.29, 0.58]
    assert not panel.capture_button.isEnabled()
    np.testing.assert_allclose(panel._frames[0][2], 0)
    assert panel._frames[1][2][3] == pytest.approx(1)
    assert panel._frames[1][3][3] == pytest.approx(8)
    assert controller.event_count == 1
    if located:
        assert not np.isfinite(panel._frames[2][3]).any()
        assert "超出良品参考范围" in panel.status_label.text()
    else:
        np.testing.assert_allclose(panel._frames[2][2], 0)



def test_common_workflow_load_applies_to_both_modes_and_compares_heatmap(window, tmp_path):
    win = window
    feed = connect_live(win)
    mode_button(win, "weld").click()
    assert win._task_mode == "compensation"

    capture_compensation(win, feed)
    mode_button(win, "compensation").click()
    panel = win._compensation
    for channel in range(16):
        panel.channel_picker.setCurrentIndex(panel.channel_picker.findData(channel))
        panel.template_button.click()
        sample = BASE.copy()
        sample[channel] += 4
        sample[(channel + 1) % 16] += 1
        for _ in range(3):
            feed(sample)
    profile = panel.active_profile
    assert profile.correction_mode == "coupling"
    path = tmp_path / "calibration.json"
    panel.save_calibration(path)
    panel.load_calibration(path)
    assert panel.active_profile.fingerprint == profile.fingerprint

    frame = BASE + 5
    frame[5] += 4
    frame[6] += 1
    feed(frame)
    cells = [win._heatmap._cells[win._heatmap.channel_map()[channel]] for channel in range(16)]
    assert cells[5].corrected_value == pytest.approx(4, abs=0.005)
    assert abs(cells[6].corrected_value) < 0.005
    scale = win._heatmap.range_max
    win._before_compensation.setChecked(True)
    assert cells[6].corrected_value == pytest.approx(6)
    assert win._heatmap.range_max == scale

    for mode in ("fatigue", "weld"):
        mode_button(win, mode).click()
        saved = capture_reference(win, feed)
        assert saved.context["compensation_id"] == profile.fingerprint
        feed(frame)
        residual = win._inspection_panel._frames[-1][2]
        assert residual[5] == pytest.approx(4, abs=0.005)
        assert abs(residual[6]) < 0.005
        assert "疑似异常" in win._workflow_status.text()


def test_auxiliary_workspace_expands_and_restores_heatmap(window, qapp):
    win = window
    win.resize(1000, 720)
    win.show()
    qapp.processEvents()
    assert win._heatmap.isVisible() and not win._workspace.isVisible()
    mode_button(win, "compensation").click()
    qapp.processEvents()
    assert win._heatmap.isVisible() and win._compensation.isVisible()
    assert all(win._heatmap.rect().contains(cell.geometry()) for cell in win._heatmap._cells.values())
    win._btn_expand_workspace.click()
    qapp.processEvents()
    assert not win._heatmap.isVisible()
    assert win._workspace.width() >= 950
    mode_button(win, "compensation").click()
    qapp.processEvents()
    assert win._heatmap.isVisible() and not win._workspace.isVisible()
