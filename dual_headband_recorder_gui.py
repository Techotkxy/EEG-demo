#!/usr/bin/env python
"""
Prototype GUI for simultaneous 2-headband EEG viewing + recording.

Features:
- Real-time plots for Raw, Theta, Alpha, Beta bands
- 2 headbands (8 channels total): HB1/HB2 x [AF7, FP1, FP2, AF8]
- Start/Pause recording button:
  - When starting (or resuming), asks for recording name and save directory
  - Creates two CSV files simultaneously (one per headband)
- Stop Recording button:
  - Stops current recording session and closes files
"""

import csv
import sys
import time
import threading
import collections
import re
from pathlib import Path

import numpy as np
from scipy import signal as scipy_signal
import serial.tools.list_ports

try:
    import pyqtgraph as pg
    from pyqtgraph.Qt import QtCore, QtWidgets
except ImportError as exc:
    raise SystemExit("Missing GUI dependencies. Install pyqtgraph + PyQt5.") from exc

try:
    import sounddevice as sd
    import soundfile as sf
except ImportError:
    sd = None
    sf = None

sys.path.insert(0, ".")
import lib.open_bci_v3 as bci


CHANNELS_PER_HEADBAND = 4
ELECTRODE_NAMES = ["AF7", "FP1", "FP2", "AF8"]

# Viewer/processing settings
WINDOW_SEC = 5
PLOT_REFRESH_MS = 50
DISPLAY_DOWNSAMPLE = 3
LOW_CUTOFF = 0.3
HIGH_CUTOFF = 50.0
NOTCH_HZ = 50.0
NOTCH_Q = 30
PLOT_AUTO_RANGE_Y = True
BOARD_RECONNECT_MAX_RETRIES = 5

BANDS = [
    ("Theta", 4, 8),
    ("Alpha", 8, 13),
    ("Beta", 13, 30),
]


def make_bandpass_filter(low_hz, high_hz, fs, order=4):
    nyq = fs / 2
    low = max(low_hz / nyq, 0.001)
    high = min(high_hz / nyq, 0.99)
    return scipy_signal.butter(order, [low, high], btype="band")


def make_notch_filter(notch_hz, fs, q=30):
    nyq = fs / 2
    return scipy_signal.iirnotch(notch_hz / nyq, q)


def zscore(x):
    x = np.asarray(x, dtype=np.float32)
    if len(x) < 8:
        return x
    s = np.std(x)
    if s < 1e-8:
        return x - np.mean(x)
    return (x - np.mean(x)) / s


def channel_label(ch_idx):
    hb = ch_idx // CHANNELS_PER_HEADBAND + 1
    elec = ELECTRODE_NAMES[ch_idx % CHANNELS_PER_HEADBAND]
    return f"HB{hb}_{elec}"


def safe_token(text):
    """Convert text into a filesystem-safe token for filenames."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(text)).strip("_")


class DualHeadbandRecorder(QtWidgets.QMainWindow):
    def __init__(self, ports):
        super().__init__()
        if len(ports) != 2:
            raise ValueError("Exactly 2 ports are required.")
        self.ports = ports
        self.n_headbands = 2
        self.n_channels = self.n_headbands * CHANNELS_PER_HEADBAND

        self.lock = threading.Lock()
        self.stream_running = True
        self.recording = False
        self.last_session_name = None
        self.record_files = [None, None]
        self.record_writers = [None, None]
        self.pending_rows = [[], []]
        self.total_written = [0, 0]
        self.total_samples = [0, 0]
        self.sample_index = [0, 0]
        self.current_out_dir = None
        self.current_stamp = None
        self.current_csv_paths = [None, None]
        self.record_started_at = None
        self.current_audio_path = None
        self.audio_stream = None
        self.audio_chunks = []
        self.audio_fs = 16000
        self.audio_channels = 1

        self.boards = []
        self._connect_boards()
        self.sample_rate = self.boards[0].getSampleRate()
        self.buffer_size = int(WINDOW_SEC * self.sample_rate)
        self.time_axis = np.linspace(-WINDOW_SEC, 0, self.buffer_size)
        self.buffers = [
            collections.deque([0.0] * self.buffer_size, maxlen=self.buffer_size)
            for _ in range(self.n_channels)
        ]

        self.bp_b, self.bp_a = make_bandpass_filter(LOW_CUTOFF, HIGH_CUTOFF, self.sample_rate)
        self.bp_state = [
            [np.zeros(max(len(self.bp_a), len(self.bp_b)) - 1, dtype=np.float64) for _ in range(CHANNELS_PER_HEADBAND)]
            for _ in range(self.n_headbands)
        ]
        if NOTCH_HZ > 0:
            self.notch_b, self.notch_a = make_notch_filter(NOTCH_HZ, self.sample_rate, q=NOTCH_Q)
            self.notch_state = [
                [np.zeros(max(len(self.notch_a), len(self.notch_b)) - 1, dtype=np.float64) for _ in range(CHANNELS_PER_HEADBAND)]
                for _ in range(self.n_headbands)
            ]
        else:
            self.notch_b, self.notch_a = None, None
            self.notch_state = None
        self.band_filters = {name: make_bandpass_filter(lo, hi, self.sample_rate) for name, lo, hi in BANDS}

        self.raw_curves = []
        self.band_curves = {name: [] for name, _, _ in BANDS}
        self._build_ui()
        self._start_stream_threads()

        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self._update_plots)
        self.timer.start(PLOT_REFRESH_MS)

    def _connect_boards(self):
        for port in self.ports:
            board = None
            for attempt in range(5):
                try:
                    board = bci.OpenBCIBoard(port=port, timeout=1)
                    break
                except Exception:
                    time.sleep(1.5)
            if board is None:
                raise RuntimeError(f"Failed to connect to {port}")
            self.boards.append(board)

    def _build_ui(self):
        self.setWindowTitle("2-Headband EEG Prototype (Raw + Theta/Alpha/Beta)")
        self.resize(1400, 900)

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)

        controls = QtWidgets.QHBoxLayout()
        self.start_pause_btn = QtWidgets.QPushButton("Start Recording")
        self.start_pause_btn.clicked.connect(self._on_start_recording)
        self.stop_btn = QtWidgets.QPushButton("Stop Recording")
        self.stop_btn.clicked.connect(self._on_stop_recording)
        self.port_info_btn = QtWidgets.QPushButton("Show Dongle-Port Mapping")
        self.port_info_btn.clicked.connect(self._show_port_info_dialog)
        self.status_label = QtWidgets.QLabel(
            f"Connected: HB1={self.ports[0]} | HB2={self.ports[1]} | Recording: OFF"
        )
        self.metrics_label = QtWidgets.QLabel("Elapsed: 00:00 | Samples: HB1=0 HB2=0 | Packet loss: HB1=0.00% HB2=0.00%")
        controls.addWidget(self.start_pause_btn)
        controls.addWidget(self.stop_btn)
        controls.addWidget(self.port_info_btn)
        controls.addWidget(self.status_label, 1)
        root.addLayout(controls)
        root.addWidget(self.metrics_label)

        self.tabs = QtWidgets.QTabWidget()
        root.addWidget(self.tabs, 1)

        self.raw_widget = self._create_plot_tab("Raw EEG (z-score)", self.raw_curves)
        self.tabs.addTab(self.raw_widget, "Raw")
        for band_name, _, _ in BANDS:
            curves = self.band_curves[band_name]
            w = self._create_plot_tab(f"{band_name} Band (z-score)", curves)
            self.tabs.addTab(w, band_name)

        mapping = QtWidgets.QLabel(
            f"Recording mapping: HB1 -> {self.ports[0]}, HB2 -> {self.ports[1]}"
        )
        root.addWidget(mapping)
        port_details = self._get_port_mapping_text()
        self.port_mapping_label = QtWidgets.QLabel(port_details)
        self.port_mapping_label.setWordWrap(True)
        root.addWidget(self.port_mapping_label)

    def _set_status(self, state_text):
        self.status_label.setText(
            f"Connected: HB1={self.ports[0]} | HB2={self.ports[1]} | Recording: {state_text}"
        )

    def _get_port_mapping_text(self):
        port_by_name = {p.device.upper(): p for p in serial.tools.list_ports.comports()}
        lines = []
        for hb_idx, port in enumerate(self.ports):
            rec = port_by_name.get(port.upper())
            hb = hb_idx + 1
            if rec is None:
                lines.append(f"HB{hb}: {port} | details unavailable")
                continue
            serial_no = rec.serial_number or "N/A"
            mfg = rec.manufacturer or "N/A"
            desc = rec.description or "N/A"
            lines.append(f"HB{hb}: {port} | SN={serial_no} | MFG={mfg} | DESC={desc}")
        lines.append("Tip: If unsure, unplug/replug one dongle and reopen this mapping to see which COM/SN changes.")
        return "Dongle-Port mapping:\n" + "\n".join(lines)

    def _show_port_info_dialog(self):
        QtWidgets.QMessageBox.information(self, "Dongle-Port Mapping", self._get_port_mapping_text())

    def _audio_callback(self, indata, frames, time_info, status):
        if status:
            # Keep running; we only log audio callback warnings to console.
            print(f"Audio callback warning: {status}")
        self.audio_chunks.append(indata.copy())

    def _preprocess_hb_sample(self, hb_idx, sample_vals):
        """
        Per-sample preprocessing for recording:
        1) Bandpass 0.3-50 Hz
        2) Notch 50/60 Hz
        3) CAR within the 4 channels of this headband
        """
        x = np.asarray(sample_vals, dtype=np.float64)
        y = np.zeros(CHANNELS_PER_HEADBAND, dtype=np.float64)

        # Bandpass each channel with per-channel filter state.
        for ch in range(CHANNELS_PER_HEADBAND):
            yi, self.bp_state[hb_idx][ch] = scipy_signal.lfilter(
                self.bp_b, self.bp_a, [x[ch]], zi=self.bp_state[hb_idx][ch]
            )
            y[ch] = yi[0]

        # Notch each channel with per-channel filter state.
        if self.notch_b is not None:
            for ch in range(CHANNELS_PER_HEADBAND):
                yi, self.notch_state[hb_idx][ch] = scipy_signal.lfilter(
                    self.notch_b, self.notch_a, [y[ch]], zi=self.notch_state[hb_idx][ch]
                )
                y[ch] = yi[0]

        # CAR per headband.
        y = y - np.mean(y)
        return y.tolist()

    def _create_plot_tab(self, title, curve_store):
        widget = pg.GraphicsLayoutWidget()
        widget.setBackground("#0b0b0b")
        widget.addLabel(title, row=0, col=0, colspan=2)
        colors = [pg.intColor(i) for i in range(self.n_channels)]
        for ch in range(self.n_channels):
            row = ch // 2 + 1
            col = ch % 2
            p = widget.addPlot(row=row, col=col, title=channel_label(ch))
            p.setLabel("bottom", "Time", "s")
            p.setLabel("left", "z-score")
            p.setXRange(-WINDOW_SEC, 0)
            if PLOT_AUTO_RANGE_Y:
                p.getViewBox().enableAutoRange(axis="y")
            else:
                p.setYRange(-3, 3)
            p.showGrid(x=True, y=True, alpha=0.25)
            c = p.plot(pen=pg.mkPen(color=colors[ch], width=1.4))
            curve_store.append(c)
        return widget

    def _make_callback(self, hb_idx):
        ch_offset = hb_idx * CHANNELS_PER_HEADBAND

        def on_sample(sample):
            now = time.time()
            with self.lock:
                if len(sample.channel_data) < CHANNELS_PER_HEADBAND:
                    return
                pre = self._preprocess_hb_sample(hb_idx, sample.channel_data[:CHANNELS_PER_HEADBAND])
                for i in range(CHANNELS_PER_HEADBAND):
                    self.buffers[ch_offset + i].append(pre[i])

                if self.recording and self.record_writers[hb_idx] is not None:
                    self.sample_index[hb_idx] += 1
                    t_rel = self.sample_index[hb_idx] / float(self.sample_rate)
                    row = [now, t_rel, sample.id]
                    sample_vals = []
                    for i in range(CHANNELS_PER_HEADBAND):
                        val = pre[i]
                        row.append(val)
                        sample_vals.append(val)
                    row.extend([f"HB{hb_idx + 1}", self.ports[hb_idx]])
                    self.pending_rows[hb_idx].append(row)
                self.total_samples[hb_idx] += 1

        return on_sample

    def _stream_thread(self, board, hb_idx):
        retries = 0
        while self.stream_running:
            try:
                board.start_streaming(self._make_callback(hb_idx), lapse=-1)
                break
            except Exception as e:
                msg = str(e)
                print(f"Stream error HB{hb_idx + 1}: {msg}")
                if "stalled" in msg.lower() and retries < BOARD_RECONNECT_MAX_RETRIES:
                    retries += 1
                    print(f"HB{hb_idx + 1}: attempting reconnect ({retries}/{BOARD_RECONNECT_MAX_RETRIES})...")
                    try:
                        board.stop()
                    except Exception:
                        pass
                    try:
                        board.disconnect()
                    except Exception:
                        pass
                    # Re-create board on the same COM port and continue streaming.
                    try:
                        board = bci.OpenBCIBoard(port=self.ports[hb_idx], timeout=1)
                        self.boards[hb_idx] = board
                        time.sleep(1.0)
                        continue
                    except Exception as conn_err:
                        print(f"HB{hb_idx + 1}: reconnect failed: {conn_err}")
                # Unrecoverable stream failure for this run.
                self.stream_running = False
                break

    def _start_stream_threads(self):
        for hb_idx, board in enumerate(self.boards):
            t = threading.Thread(target=self._stream_thread, args=(board, hb_idx), daemon=True)
            t.start()

    def _flush_record_buffers(self):
        for hb_idx in range(self.n_headbands):
            rows = self.pending_rows[hb_idx]
            w = self.record_writers[hb_idx]
            f = self.record_files[hb_idx]
            if rows and w is not None and f is not None:
                w.writerows(rows)
                self.total_written[hb_idx] += len(rows)
                self.pending_rows[hb_idx] = []
                f.flush()

    def _open_record_files(self):
        # Ask user each time recording starts/resumes
        name, ok = QtWidgets.QInputDialog.getText(
            self,
            "Recording Name",
            "Enter a recording name:",
        )
        if not ok or not name.strip():
            return False
        out_dir = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Choose folder to store recordings",
            str(Path.cwd()),
        )
        if not out_dir:
            return False

        self.last_session_name = name.strip()
        self.current_stamp = time.strftime("%Y%m%d_%H%M%S")
        self.current_out_dir = Path(out_dir)
        self.current_csv_paths = [None, None]
        self.current_audio_path = self.current_out_dir / f"{self.last_session_name}_AUDIO_{self.current_stamp}.wav"
        self.audio_chunks = []
        for hb_idx in range(self.n_headbands):
            hb = hb_idx + 1
            port = self.ports[hb_idx]
            session_token = safe_token(self.last_session_name)
            port_token = safe_token(port)
            out_path = self.current_out_dir / f"{session_token}_HB{hb}_{port_token}_{self.current_stamp}.csv"
            try:
                self.current_out_dir.mkdir(parents=True, exist_ok=True)
                f = out_path.open("w", newline="", encoding="utf-8")
            except Exception as e:
                QtWidgets.QMessageBox.critical(
                    self,
                    "Cannot Create Recording File",
                    f"Failed to create:\n{out_path}\n\nReason:\n{e}",
                )
                return False
            w = csv.writer(f)
            w.writerow(
                [
                    "timestamp_unix",
                    "t_rel_sec",
                    "sample_id",
                    "AF7",
                    "FP1",
                    "FP2",
                    "AF8",
                    "headband",
                    "port",
                    "processing",
                ]
            )
            w.writerow(["", "", "", "", "", "", "", "", "", "bandpass_0.3_50_notch_car"])
            self.record_files[hb_idx] = f
            self.record_writers[hb_idx] = w
            self.total_written[hb_idx] = 0
            self.current_csv_paths[hb_idx] = out_path

        self.record_started_at = time.time()
        QtWidgets.QMessageBox.information(
            self,
            "Recording Started",
            (
                f"Recording: {self.last_session_name}\n"
                f"HB1 -> {self.ports[0]}\n"
                f"HB2 -> {self.ports[1]}\n\n"
                f"CSV files:\n"
                f"{self.current_csv_paths[0]}\n"
                f"{self.current_csv_paths[1]}\n\n"
                f"Audio file:\n"
                f"{self.current_audio_path}"
            ),
        )
        return True

    def _start_audio_recording(self):
        if sd is None or sf is None:
            QtWidgets.QMessageBox.warning(
                self,
                "Microphone Disabled",
                "sounddevice/soundfile not installed. EEG recording will continue without microphone audio.",
            )
            return False
        try:
            self.audio_stream = sd.InputStream(
                samplerate=self.audio_fs,
                channels=self.audio_channels,
                dtype="float32",
                callback=self._audio_callback,
            )
            self.audio_stream.start()
            return True
        except Exception as e:
            self.audio_stream = None
            QtWidgets.QMessageBox.warning(
                self,
                "Microphone Start Failed",
                f"Could not start microphone recording.\nReason: {e}\n\nEEG recording will continue.",
            )
            return False

    def _stop_audio_recording_and_save(self):
        if self.audio_stream is not None:
            try:
                self.audio_stream.stop()
                self.audio_stream.close()
            except Exception:
                pass
            self.audio_stream = None

        if self.current_audio_path is None:
            return None
        if sd is None or sf is None:
            return None
        if not self.audio_chunks:
            return None

        try:
            audio = np.concatenate(self.audio_chunks, axis=0)
            sf.write(str(self.current_audio_path), audio, self.audio_fs)
            return str(self.current_audio_path)
        except Exception as e:
            print(f"Audio save error: {e}")
            return None

    def _close_record_files(self, reset_session=False):
        self._flush_record_buffers()
        for i in range(self.n_headbands):
            if self.record_files[i] is not None:
                self.record_files[i].close()
            self.record_files[i] = None
            self.record_writers[i] = None
            self.pending_rows[i] = []
        if reset_session:
            self.last_session_name = None
            self.current_out_dir = None
            self.current_stamp = None
            self.current_csv_paths = [None, None]
            self.record_started_at = None
            self.current_audio_path = None
            self.audio_chunks = []
            self.sample_index = [0, 0]

    def _on_start_recording(self):
        if self.recording:
            QtWidgets.QMessageBox.information(
                self,
                "Recording Already Running",
                "Recording is already ON. Use Stop Recording to finish the current session.",
            )
            return
        started = self._open_record_files()
        if not started:
            return
        self.recording = True
        self.start_pause_btn.setText("Start Recording")
        self._set_status(f"ON ({self.last_session_name})")
        self._start_audio_recording()

    def _on_stop_recording(self):
        self.recording = False
        self._flush_record_buffers()
        audio_path = self._stop_audio_recording_and_save()
        self._close_record_files(reset_session=True)
        self.start_pause_btn.setText("Start Recording")
        self._set_status("OFF")
        msg = "Recording state: OFF\nCSV export completed."
        if audio_path is not None:
            msg += "\n\nMicrophone audio saved:\n" + audio_path
        QtWidgets.QMessageBox.information(
            self,
            "Recording Stopped",
            msg,
        )

    def _preprocess(self, data):
        # Buffers already store per-sample preprocessed raw data.
        return np.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)

    def _update_plots(self):
        if not self.stream_running:
            return

        with self.lock:
            min_len = min(len(self.buffers[ch]) for ch in range(self.n_channels))
            if min_len < 300:
                return
            data = np.column_stack(
                [np.asarray(list(self.buffers[ch])[-min_len:], dtype=np.float32) for ch in range(self.n_channels)]
            )

        try:
            data_clean = self._preprocess(data)
            t = self.time_axis[::DISPLAY_DOWNSAMPLE]

            # Raw
            for ch in range(self.n_channels):
                vals = zscore(data_clean[:, ch])[::DISPLAY_DOWNSAMPLE]
                self.raw_curves[ch].setData(t[-len(vals):], vals)

            # Bands
            for band_name, _, _ in BANDS:
                b, a = self.band_filters[band_name]
                curves = self.band_curves[band_name]
                for ch in range(self.n_channels):
                    vals = scipy_signal.filtfilt(b, a, data_clean[:, ch])
                    vals = zscore(vals)[::DISPLAY_DOWNSAMPLE]
                    curves[ch].setData(t[-len(vals):], vals)

            self._flush_record_buffers()
            self._update_metrics_label()
        except Exception as e:
            print(f"Plot/update error: {e}")

    def _update_metrics_label(self):
        elapsed_sec = 0
        if self.recording and self.record_started_at is not None:
            elapsed_sec = max(0, int(time.time() - self.record_started_at))
        mm = elapsed_sec // 60
        ss = elapsed_sec % 60

        loss_parts = []
        for hb_idx, board in enumerate(self.boards):
            ok, dropped = board.get_packet_stats()
            total = ok + dropped
            pct = (100.0 * dropped / total) if total > 0 else 0.0
            loss_parts.append(f"HB{hb_idx + 1}={pct:.2f}%")

        self.metrics_label.setText(
            f"Elapsed: {mm:02d}:{ss:02d} | Samples: HB1={self.total_samples[0]} HB2={self.total_samples[1]} | "
            f"Packet loss: {' '.join(loss_parts)}"
        )

    def closeEvent(self, event):
        self.stream_running = False
        self.recording = False
        self._stop_audio_recording_and_save()
        self._close_record_files(reset_session=True)
        for b in self.boards:
            try:
                b.stop()
                b.disconnect()
            except Exception:
                pass
        event.accept()


def main():
    ports = [a for a in sys.argv[1:] if a.upper().startswith("COM") or "tty" in a or "/" in a]
    if not ports:
        ports = ["COM3", "COM4"]
    if len(ports) != 2:
        print("Usage: python dual_headband_recorder_gui.py COM3 COM4")
        sys.exit(1)

    app = QtWidgets.QApplication(sys.argv)
    win = DualHeadbandRecorder(ports)
    win.show()
    if hasattr(app, "exec"):
        sys.exit(app.exec())
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
