"""Saved array calibration, session rebase and derived main heatmap views."""

import json
import time

import numpy as np
import pytest
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QComboBox, QFileDialog

from core.array_analysis import ArrayAnalysis, coverage_grid
from core.channel_layout import (
    ACTIVE_PCB_CHANNEL_MAP,
    BINDING_DESIGN,
    LAYOUT_ACTIVE_PCB,
    LAYOUT_BREADBOARD,
)
from core.config_manager import ConfigManager, SCOPE_SERIAL
from ui.main_window import DATA_TIMEOUT_S, SourceState


SAVED_DEVICE = "USB VID:PID=0483:5740 SER=ARRAY_TEST_123 LOCATION=1-8.3"
MOVED_DEVICE = "USB VID:PID=0483:5740 SER=ARRAY_TEST_123 LOCATION=2-4.1"
OTHER_DEVICE = "USB VID:PID=0483:5740 SER=OTHER_TEST_456 LOCATION=1-8.3"
BASELINE = np.arange(1000.0, 1016.0)


def values_at(position):
    return BASELINE - 8.0 * position


@pytest.fixture
def window(monkeypatch, tmp_path, qapp):
    """Avoid the user's config, stored calibration, CSV folder and source workers."""
    import app_paths
    import ui.main_window as main_module

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    config_path = tmp_path / "config.json"
    config = ConfigManager(config_path=str(config_path))
    config.layout_id = LAYOUT_ACTIVE_PCB
    config.cell_binding = BINDING_DESIGN
    config.enabled_channels = list(range(16))
    config.save()
    csv_directory = tmp_path / "csv"
    csv_directory.mkdir()
    monkeypatch.setattr(main_module, "ConfigManager", lambda: ConfigManager(config_path=str(config_path)))
    monkeypatch.setattr(app_paths, "get_default_csv_dir", lambda: csv_directory)
    win = main_module.MainWindow()
    win._refresh_timer.stop()
    win._processor.set_ema_alpha(1.0)
    win._processor.set_median_window(1)
    win.show()
    qapp.processEvents()
    try:
        yield win
    finally:
        win._refresh_timer.stop()
        win._stop_all_sources()
        win._recorder.stop_recording()
        win.close()
        qapp.processEvents()


@pytest.fixture
def saved_profile(tmp_path):
    """Use the existing version-1 profile schema saved by v3.5.0."""
    engine = ArrayAnalysis()
    engine.set_baseline([BASELINE] * 3)
    for position in (0.0, 1.0, 2.0, 3.0, 4.0):
        engine.add_template(position, [values_at(position)] * 3)
    profile = {
        "format": "array-analysis-calibration",
        "version": 1,
        "context": [SCOPE_SERIAL, SAVED_DEVICE, [
            ["adc_profile", "ads1115_4v096_div2"],
            ["coil", "默认"], ["freq_unit", "kHz"],
            ["frequency", 1000.0], ["gain", "1"], ["plate", ""],
        ]],
        "description": "串口实时 · ADC 原始毫伏值",
        "direction": "bottom",
        "threshold_enabled": False,
        "threshold": 3.0,
        "channel_map": {str(channel): list(position) for channel, position in ACTIVE_PCB_CHANNEL_MAP.items()},
        "analysis": engine.to_dict(),
    }
    path = tmp_path / "阵列标定-v350.json"
    path.write_text(json.dumps(profile, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    return path


def open_dialog(win):
    win._on_array_analysis_clicked()
    return win._array_analysis


def connect_source(win, device=SAVED_DEVICE):
    """Deliver the normal connected event without opening a hardware port."""
    win._start_new_session(SCOPE_SERIAL)
    win._pending_device_id = device
    win._on_serial_connected("COM_TEST")
    win._update_all_displays()


def feed(win, position, scan=1, status=0, valid_mask=0xFFFF, offset=0.0):
    numbers = ",".join(f"{value:.3f}" for value in values_at(position) + offset)
    win._on_serial_data(f"D,{scan},{status},{valid_mask},0,mV,{numbers}\r\n".encode("ascii"))
    if status == 0 and valid_mask == 0xFFFF:
        QTest.qWait(160)
    win._update_all_displays()


def heatmap_mode(win):
    combo = win.findChild(QComboBox, "arrayHeatmapMode")
    assert combo is not None
    return combo


def assert_coverage(win, position):
    assert heatmap_mode(win).currentData() == "coverage"
    expected = coverage_grid((position, position), "bottom")
    for location, cell in win._heatmap._cells.items():
        assert cell.coverage_mode
        assert cell.coverage_value == pytest.approx(expected[location])
        assert cell.value_text.endswith("%")
        assert float(cell.value_text.rstrip("%")) == pytest.approx(100 * expected[location])


def assert_unknown(win):
    assert heatmap_mode(win).currentData() == "coverage"
    for cell in win._heatmap._cells.values():
        assert cell.coverage_mode
        assert cell.coverage_value is None
        assert "%" not in cell.value_text


def assert_corrected_unknown(win):
    assert heatmap_mode(win).currentData() == "corrected"
    for cell in win._heatmap._cells.values():
        assert cell.corrected_mode
        assert not cell.coverage_mode
        assert cell.corrected_value is None
        assert cell.value_text == "未知"


def load_staged(win, path, monkeypatch):
    dialog = open_dialog(win)
    assert win._source_state == SourceState.DISCONNECTED
    assert dialog.load_button.isEnabled()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args, **kwargs: (str(path), "JSON"))
    dialog.load_button.click()
    snapshot = dialog.coverage_snapshot()
    assert snapshot["pending"]
    assert not snapshot["fresh"]
    assert np.isnan(snapshot["coverage"]).all()
    return dialog


def test_v350_manual_load_while_disconnected_applies_after_valid_source(window, saved_profile, monkeypatch):
    dialog = load_staged(window, saved_profile, monkeypatch)
    connect_source(window)
    assert not dialog.coverage_snapshot()["fresh"]
    assert all(cell.coverage_value is None for cell in window._heatmap._cells.values())
    for scan, position in enumerate((0.0, 0.5, 4.0), start=1):
        feed(window, position, scan)
        snapshot = dialog.coverage_snapshot()
        assert snapshot["has_model"]
        assert not snapshot["pending"]
        assert snapshot["fresh"]
        assert snapshot["position"] == pytest.approx(position)
        assert_coverage(window, position)
    assert dialog._analysis.positions == (0.0, 1.0, 2.0, 3.0, 4.0)


def test_saved_usb_identity_survives_location_change(window, saved_profile, monkeypatch):
    dialog = load_staged(window, saved_profile, monkeypatch)
    connect_source(window, MOVED_DEVICE)
    feed(window, 0.5)
    assert not dialog.coverage_snapshot()["pending"]
    assert_coverage(window, 0.5)


def test_coverage_toggle_preserves_cells_labels_and_signed_attenuation(window, saved_profile, monkeypatch):
    load_staged(window, saved_profile, monkeypatch)
    connect_source(window)
    for channel, state in enumerate(window._processor.all_channels()):
        state.baseline_mv = float(BASELINE[channel])
    feed(window, 0.5)
    cells = dict(window._heatmap._cells)
    identity = {location: (cell.channel, cell.title, cell.position_label) for location, cell in cells.items()}
    assert_coverage(window, 0.5)
    combo = heatmap_mode(window)
    combo.setCurrentIndex(combo.findData("attenuation"))
    window._update_all_displays()
    for location, cell in window._heatmap._cells.items():
        assert cell is cells[location]
        assert (cell.channel, cell.title, cell.position_label) == identity[location]
        assert not cell.coverage_mode
        assert cell.attenuation_value == pytest.approx(4.0)
        assert cell.value_text.startswith("+")
        assert "%" not in cell.value_text
    feed(window, -0.25, scan=2)
    assert all(cell.value_text.startswith("-") for cell in window._heatmap._cells.values())
    combo.setCurrentIndex(combo.findData("coverage"))
    feed(window, 0.5, scan=3)
    assert_coverage(window, 0.5)


def test_closing_dialog_keeps_loaded_main_heatmap_model_active(window, saved_profile, monkeypatch):
    dialog = load_staged(window, saved_profile, monkeypatch)
    connect_source(window)
    feed(window, 0.5)
    dialog.close()
    assert not dialog.isVisible()
    feed(window, 1.5, scan=2)
    assert dialog.coverage_snapshot()["fresh"]
    assert_coverage(window, 1.5)


@pytest.mark.parametrize("failure", ["stale", "invalid"])
def test_stale_or_invalid_data_clears_percentages_then_recovers(window, saved_profile, monkeypatch, failure):
    dialog = load_staged(window, saved_profile, monkeypatch)
    connect_source(window)
    feed(window, 0.5)
    assert_coverage(window, 0.5)
    if failure == "stale":
        window._segment_last_dframe_time = time.monotonic() - DATA_TIMEOUT_S - 1
        window._on_refresh()
        assert window._data_timeout_reported
    else:
        feed(window, 0.5, scan=2, status=1, valid_mask=0x7FFF)
    assert not dialog.coverage_snapshot()["fresh"]
    assert_unknown(window)
    feed(window, 1.5, scan=3)
    assert_coverage(window, 1.5)


@pytest.mark.parametrize("mismatch", ["device", "gain", "mapping"])
def test_incompatible_source_keeps_saved_profile_staged_and_heatmap_unknown(window, saved_profile, mismatch):
    dialog = open_dialog(window)
    if mismatch == "gain":
        parameters = window._config.experiment_params
        parameters["gain"] = "2"
        window._config.experiment_params = parameters
    elif mismatch == "mapping":
        window._config.layout_id = LAYOUT_BREADBOARD
        window._apply_channel_map_to_heatmap()
    connect_source(window, OTHER_DEVICE if mismatch == "device" else SAVED_DEVICE)
    assert dialog.load_calibration(saved_profile) is False
    combo = heatmap_mode(window)
    combo.setCurrentIndex(combo.findData("coverage"))
    feed(window, 0.5)
    snapshot = dialog.coverage_snapshot()
    assert snapshot["pending"]
    assert not snapshot["fresh"]
    assert np.isnan(snapshot["coverage"]).all()
    assert snapshot["status"]
    assert_unknown(window)


def test_changed_conditions_clear_previous_coverage_until_matching_new_frame(window, saved_profile, monkeypatch):
    dialog = load_staged(window, saved_profile, monkeypatch)
    connect_source(window)
    feed(window, 0.5)
    assert_coverage(window, 0.5)
    parameters = window._config.experiment_params
    parameters["gain"] = "2"
    window._config.experiment_params = parameters
    window._on_refresh()
    assert not dialog.coverage_snapshot()["fresh"]
    assert_unknown(window)
    feed(window, 0.5, scan=2)
    assert_unknown(window)
    parameters["gain"] = "1"
    window._config.experiment_params = parameters
    window._on_refresh()
    assert not dialog.coverage_snapshot()["fresh"]
    assert_unknown(window)
    feed(window, 1.5, scan=3)
    assert_coverage(window, 1.5)


@pytest.mark.parametrize("enabled", [False, True])
def test_old_profile_preserves_threshold_preference_baseline_and_templates(
    window, saved_profile, monkeypatch, enabled,
):
    saved = json.loads(saved_profile.read_text(encoding="utf-8"))
    saved["threshold_enabled"] = enabled
    saved_profile.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    original_file = saved_profile.read_bytes()
    dialog = load_staged(window, saved_profile, monkeypatch)
    connect_source(window)
    feed(window, 0.5)

    snapshot = dialog.coverage_snapshot()
    assert dialog.threshold_enabled.isChecked() is enabled
    assert snapshot["threshold_enabled"] is enabled
    assert snapshot["fresh"]
    assert ("位置估计" if enabled else "位置试算") in snapshot["status"]
    assert dialog._analysis.session_baseline is None
    assert dialog._analysis.to_dict() == saved["analysis"]
    np.testing.assert_array_equal(dialog._analysis.baseline, BASELINE)
    assert saved_profile.read_bytes() == original_file


@pytest.mark.parametrize("capture_count", [3, 10])
def test_rebase_button_recovers_offset_without_replacing_saved_templates(
    window, saved_profile, monkeypatch, capture_count,
):
    dialog = load_staged(window, saved_profile, monkeypatch)
    connect_source(window)
    # A first valid frame activates the staged file and restores its preference.
    feed(window, 0.0, scan=1)
    original_model = dialog._analysis.to_dict()
    original_positions = dialog._analysis.positions
    original_file = saved_profile.read_bytes()
    original_processor_baselines = np.arange(2000.0, 2016.0)
    for channel, state in enumerate(window._processor.all_channels()):
        state.baseline_mv = float(original_processor_baselines[channel])

    # The saved user's disabled threshold is respected until explicitly enabled.
    assert not dialog.threshold_enabled.isChecked()
    dialog.threshold_enabled.setChecked(True)
    dialog.threshold_spin.setValue(3.0)
    feed(window, 0.0, scan=2, offset=40.0)
    rejected = dialog.coverage_snapshot()
    assert rejected["state"] == "rejected"
    assert rejected["range"] is None
    assert np.isnan(rejected["coverage"]).all()
    assert_unknown(window)

    dialog.frame_count.setValue(capture_count)
    assert dialog.rebase_button.isEnabled()
    dialog.rebase_button.click()
    assert dialog._capture["kind"] == "rebase"
    assert not dialog._capture["frames"]
    assert dialog._analysis.session_baseline is None

    # A partial frame must neither advance the capture nor reuse the last vector.
    feed(window, 0.0, scan=3, status=1, valid_mask=0x7FFF, offset=40.0)
    assert not dialog._capture["frames"]
    for index in range(capture_count - 1):
        feed(window, 0.0, scan=index + 4, offset=40.0)
        assert len(dialog._capture["frames"]) == index + 1
        assert dialog._analysis.session_baseline is None
    feed(window, 0.0, scan=capture_count + 3, offset=40.0)

    assert dialog._capture is None
    np.testing.assert_array_equal(dialog._analysis.session_baseline, BASELINE + 40.0)
    np.testing.assert_array_equal(dialog._analysis.baseline, BASELINE)
    assert dialog._analysis.positions == original_positions
    assert dialog._analysis.to_dict() == original_model
    assert dialog.template_list.count() == len(original_positions)
    assert saved_profile.read_bytes() == original_file
    np.testing.assert_array_equal(
        [state.baseline_mv for state in window._processor.all_channels()],
        original_processor_baselines,
    )

    feed(window, 1.5, scan=capture_count + 4, offset=40.0)
    recovered = dialog.coverage_snapshot()
    assert recovered["fresh"]
    assert recovered["state"] == "candidate"
    assert recovered["position"] == pytest.approx(1.5)
    assert recovered["score"] == pytest.approx(0.0)
    assert_coverage(window, 1.5)


def test_corrected_view_preserves_sign_clears_bad_frames_and_marks_missing_references(
    window, saved_profile, monkeypatch,
):
    dialog = load_staged(window, saved_profile, monkeypatch)
    connect_source(window)
    offset = np.zeros(16)
    # These two data channels belong to the partially covered bottom row.
    assert ACTIVE_PCB_CHANNEL_MAP[0][0] == ACTIVE_PCB_CHANNEL_MAP[1][0] == 3
    offset[0] = 2.0
    offset[1] = -2.0
    feed(window, 0.5, scan=1, offset=offset)
    assert dialog.coverage_snapshot()["position"] == pytest.approx(0.5)
    combo = heatmap_mode(window)
    combo.setCurrentIndex(combo.findData("corrected"))
    cells = dict(window._heatmap._cells)
    expected = np.zeros((4, 4))
    expected[ACTIVE_PCB_CHANNEL_MAP[0]] = -2.0
    expected[ACTIVE_PCB_CHANNEL_MAP[1]] = 2.0
    np.testing.assert_array_equal(dialog.coverage_snapshot()["corrected"], expected)
    for location, cell in cells.items():
        assert cell.corrected_mode
        assert not cell.coverage_mode
        assert cell.corrected_value == pytest.approx(expected[location])
        assert cell.value_text == f"{expected[location]:+.1f}"
        assert "补偿后响应" in cell.toolTip()
        assert "%" not in cell.value_text
    assert "补偿后响应" in window._legend.accessibleName()

    feed(window, 0.5, scan=2, status=1, valid_mask=0x7FFF, offset=offset)
    assert not dialog.coverage_snapshot()["fresh"]
    assert_corrected_unknown(window)
    feed(window, 0.5, scan=3, offset=offset)
    assert dialog.coverage_snapshot()["fresh"]
    for location, cell in window._heatmap._cells.items():
        assert cell is cells[location]
        assert cell.corrected_value == pytest.approx(expected[location])

    # All-covered geometry supplies no known-uncovered reference for compensation.
    feed(window, 4.0, scan=4)
    full_cover = dialog.coverage_snapshot()
    assert full_cover["fresh"]
    assert full_cover["position"] == pytest.approx(4.0)
    assert full_cover["reference_count"] == 0
    assert full_cover["common_mv"] is None
    assert np.isnan(full_cover["corrected"]).all()
    assert_corrected_unknown(window)
    combo.setCurrentIndex(combo.findData("coverage"))
    assert_coverage(window, 4.0)
