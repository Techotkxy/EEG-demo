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
import datetime
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
    import pyedflib
except ImportError:
    pyedflib = None

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
        self.edf_samples = [[], []]  # in-memory samples for EDF+ export per headband
        self.current_out_dir = None
        self.current_stamp = None
        self.current_csv_paths = [None, None]
        self.current_edf_paths = [None, None]
        self.record_started_at = None

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
        if NOTCH_HZ > 0:
            self.notch_b, self.notch_a = make_notch_filter(NOTCH_HZ, self.sample_rate, q=NOTCH_Q)
        else:
            self.notch_b, self.notch_a = None, None
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
                    board = bci.OpenBCIBoard(port=port)
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
                for i in range(min(CHANNELS_PER_HEADBAND, len(sample.channel_data))):
                    self.buffers[ch_offset + i].append(sample.channel_data[i])

                if self.recording and self.record_writers[hb_idx] is not None:
                    row = [now, sample.id]
                    sample_vals = []
                    for i in range(CHANNELS_PER_HEADBAND):
                        val = sample.channel_data[i] if i < len(sample.channel_data) else 0.0
                        row.append(val)
                        sample_vals.append(val)
                    row.extend([f"HB{hb_idx + 1}", self.ports[hb_idx]])
                    self.pending_rows[hb_idx].append(row)
                    self.edf_samples[hb_idx].append(sample_vals)
                self.total_samples[hb_idx] += 1

        return on_sample

    def _stream_thread(self, board, hb_idx):
        try:
            board.start_streaming(self._make_callback(hb_idx), lapse=-1)
        except Exception as e:
            print(f"Stream error HB{hb_idx + 1}: {e}")
        finally:
            self.stream_running = False

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
        self.edf_samples = [[], []]
        self.current_csv_paths = [None, None]
        self.current_edf_paths = [None, None]
        for hb_idx in range(self.n_headbands):
            hb = hb_idx + 1
            port = self.ports[hb_idx]
            out_path = self.current_out_dir / f"{self.last_session_name}_HB{hb}_{port}_{self.current_stamp}.csv"
            f = out_path.open("w", newline="", encoding="utf-8")
            w = csv.writer(f)
            w.writerow(["timestamp", "sample_id", "AF7", "FP1", "FP2", "AF8", "headband", "port"])
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
                f"{self.current_csv_paths[1]}"
            ),
        )
        return True

    def _export_edf_files(self):
        if pyedflib is None:
            QtWidgets.QMessageBox.warning(
                self,
                "EDF Export Skipped",
                "pyEDFlib is not installed. Install it to enable EDF+ export.",
            )
            return
        if not self.last_session_name or self.current_out_dir is None or self.current_stamp is None:
            return

        for hb_idx in range(self.n_headbands):
            samples = self.edf_samples[hb_idx]
            if not samples:
                continue

            hb = hb_idx + 1
            port = self.ports[hb_idx]
            edf_path = self.current_out_dir / f"{self.last_session_name}_HB{hb}_{port}_{self.current_stamp}.edf"
            self.current_edf_paths[hb_idx] = edf_path

            arr = np.asarray(samples, dtype=np.float64)  # shape: n_samples x 4
            if arr.ndim != 2 or arr.shape[1] != CHANNELS_PER_HEADBAND:
                continue
            signals = [arr[:, i] for i in range(CHANNELS_PER_HEADBAND)]

            signal_headers = []
            for i, ch_name in enumerate(ELECTRODE_NAMES):
                ch = signals[i]
                pmin = float(np.min(ch))
                pmax = float(np.max(ch))
                if abs(pmax - pmin) < 1e-6:
                    pmin, pmax = -1.0, 1.0
                signal_headers.append(
                    {
                        "label": ch_name,
                        "dimension": "uV",
                        "sample_frequency": self.sample_rate,
                        "physical_min": pmin,
                        "physical_max": pmax,
                        "digital_min": -32768,
                        "digital_max": 32767,
                        "transducer": "OpenBCI Cyton",
                        "prefilter": f"BP:{LOW_CUTOFF}-{HIGH_CUTOFF}Hz; Notch:{NOTCH_HZ}Hz Q{NOTCH_Q}",
                    }
                )

            header = {
                "technician": "Cogwear Prototype",
                "recording_additional": (
                    f"session={self.last_session_name}; headband=HB{hb}; port={port}; "
                    f"sample_rate_hz={self.sample_rate}; channels=AF7,FP1,FP2,AF8; units=uV"
                ),
                "patientname": f"HB{hb}",
                "patient_additional": f"port={port};session={self.last_session_name}",
                "patientcode": f"HB{hb}_{port}",
                "equipment": "OpenBCI Cyton",
                "admincode": "Cogwear",
                "sex": "",
                "startdate": datetime.datetime.now(),
                "birthdate": "",
            }

            with pyedflib.EdfWriter(str(edf_path), CHANNELS_PER_HEADBAND, file_type=pyedflib.FILETYPE_EDFPLUS) as writer:
                writer.setHeader(header)
                writer.setSignalHeaders(signal_headers)
                writer.writeSamples(signals)

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
            self.current_edf_paths = [None, None]
            self.edf_samples = [[], []]
            self.record_started_at = None

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

    def _on_stop_recording(self):
        self.recording = False
        self._flush_record_buffers()
        has_data = any(len(x) > 0 for x in self.edf_samples)
        exported_paths = []
        if has_data:
            self._export_edf_files()
            exported_paths = [str(p) for p in self.current_edf_paths if p is not None]
        self._close_record_files(reset_session=True)
        self.start_pause_btn.setText("Start Recording")
        self._set_status("OFF")
        if has_data and exported_paths:
            QtWidgets.QMessageBox.information(
                self,
                "Recording Stopped",
                "Recording state: OFF\nEDF export completed:\n" + "\n".join(exported_paths),
            )

    def _preprocess(self, data):
        data = np.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)
        bp = np.empty_like(data)
        for ch in range(self.n_channels):
            bp[:, ch] = scipy_signal.filtfilt(self.bp_b, self.bp_a, data[:, ch])
        if self.notch_b is not None:
            for ch in range(self.n_channels):
                bp[:, ch] = scipy_signal.filtfilt(self.notch_b, self.notch_a, bp[:, ch])
        # CAR per headband
        for hb_idx in range(self.n_headbands):
            s = hb_idx * CHANNELS_PER_HEADBAND
            e = s + CHANNELS_PER_HEADBAND
            m = np.mean(bp[:, s:e], axis=1, keepdims=True)
            bp[:, s:e] = bp[:, s:e] - m
        return bp

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
