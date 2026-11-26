"""Cover the app's serial parser and CSV replay paths through the native dialog."""

from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QFileDialog, QLabel, QPushButton, QSpinBox

from core.config_manager import SCOPE_SERIAL
from core.recorder import Recorder
from models.frame import DFrame, MV_MAX
from ui.main_window import MainWindow, SourceState


EXAMPLE = (
    b'D,58,0,65535,0,mV,309.875,309.625,309.875,309.875,309.750,309.625,'
    b'309.750,309.875,309.750,309.625,309.750,309.875,309.625,309.625,309.625,309.625\n'
)


def test_native_dialog_receives_live_valid_frames_and_uses_physical_map(monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'appdata'))
    win = MainWindow()
    try:
        win._start_new_session(SCOPE_SERIAL)
        win._enter_live_scope(SCOPE_SERIAL, 'array-test-device')
        win._set_source_state(SourceState.SERIAL_CONNECTED)
        win.findChild(QPushButton, 'arrayAnalysisButton').click()
        dialog = win._array_analysis
        assert dialog.isVisible()
        win._ingest_bytes(EXAMPLE)
        account = dialog.findChild(QPushButton, 'arrayCaptureBaseline')
        count = dialog.findChild(QSpinBox, 'arrayCaptureFrames')
        status = dialog.findChild(QLabel, 'arrayStatus')
        assert account is not None and count is not None and status is not None

        feed = []
        original = dialog.on_frame

        def collect(frame):
            feed.append(frame)
            original(frame)

        monkeypatch.setattr(dialog, 'on_frame', collect)
        win._ingest_bytes(EXAMPLE)
        assert feed == []  # Duplicate scan number.
        win._ingest_bytes(EXAMPLE.replace(b'D,58,0,65535,', b'D,59,1,32767,'))
        assert feed == [None]
        win._ingest_bytes(EXAMPLE.replace(b'D,58,', b'D,60,').replace(b'309.875', b'4095.875', 1))
        assert feed == [None, None]
        win._ingest_bytes(EXAMPLE.replace(b'D,58,', b'D,61,'))
        assert len(feed[-1]) == 16 and feed[-1][0] == 309.875 and feed[-1][-1] == 309.625
        QTest.qWait(150)
        assert account.isEnabled()
        before = len(feed)
        account.click()
        win.findChild(QPushButton, 'arrayAnalysisButton').click()
        assert len(feed) == before  # Reopening visible dialog cannot contribute a frame.
        win._set_source_state(SourceState.DISCONNECTED)
        QTest.qWait(170)
        assert not account.isEnabled()
    finally:
        win.close()


def test_native_dialog_captures_and_matches_live_positions(monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'appdata'))
    win = MainWindow()
    try:
        win._start_new_session(SCOPE_SERIAL)
        win._enter_live_scope(SCOPE_SERIAL, 'array-test-device')
        win._set_source_state(SourceState.SERIAL_CONNECTED)
        win._on_array_analysis_clicked()
        dialog = win._array_analysis
        dialog.frame_count.setValue(3)
        counter = 0

        def feed(delta, count=1):
            nonlocal counter
            for _ in range(count):
                numbers = ','.join(f'{300 + channel + delta:.3f}' for channel in range(16))
                win._ingest_bytes(f'D,{counter},0,65535,0,mV,{numbers}\n'.encode())
                counter += 1

        feed(0)
        dialog.baseline_button.click()
        feed(0, 3)
        assert list(dialog._analysis.baseline) == list(range(300, 316))

        dialog.position_spin.setValue(-0.5)
        dialog.template_button.click()
        feed(-2, 3)
        dialog.position_spin.setValue(1.5)
        dialog.template_button.click()
        feed(-10, 3)
        assert dialog._analysis.positions == (-0.5, 1.5)

        feed(-6)
        QTest.qWait(150)
        assert '0.500 p' in dialog.estimate_label.text()
        assert [dialog.coverage_table.item(0, col).text() for col in range(4)] == [
            '50%', '0%', '0%', '0%',
        ]
        cell = win._heatmap.channel_map()[0]
        assert '-6.000' in dialog.delta_table.item(*cell).text()

        saved = tmp_path / 'steel-plate.json'
        dialog.save_calibration(saved)
        dialog.load_calibration(saved)
        assert dialog._analysis.positions == (-0.5, 1.5)
    finally:
        win.close()


def test_csv_coverage_uses_recorded_context_and_rejects_bad_frame(monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'appdata'))
    win = MainWindow()
    try:
        # Fixture data uses the application's own record and load interfaces.
        rec = Recorder()
        path = rec.start_recording(
            win._config.experiment_params, str(tmp_path), win._config.adc_profile,
            session_meta={'source': 'demo'},
        )
        valid = DFrame(0, 0, 0xffff, 0, (309.625,) * 16)
        saturated = DFrame(1, 0, 0xffff, 0, (MV_MAX,) + (309.625,) * 15)
        try:
            for frame in (valid, saturated):
                win._processor.ingest(frame)
                rec.write_dframe(frame, win._processor, channel_map=win._heatmap.channel_map())
        finally:
            rec.stop_recording()

        monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *args: (str(path), ''))
        win._set_app_mode('replay')
        win._on_replay_open()
        assert win._replay.total_rows == 2
        assert win._replay.recorded_source_scope == 'demo'
        win._on_array_analysis_clicked()
        values = []
        monkeypatch.setattr(win._array_analysis, 'on_frame', values.append)
        win._replay.play()
        QTest.qWait(180)
        assert values == [[309.625] * 16, None]
    finally:
        win.close()
