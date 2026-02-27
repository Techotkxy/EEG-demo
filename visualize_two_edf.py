#!/usr/bin/env python
"""
Visualize two EDF recordings (HB1 + HB2) in one GUI.

Usage:
    python visualize_two_edf.py "HB1.edf" "HB2.edf"
"""

import sys
from pathlib import Path

import numpy as np
from scipy import signal as scipy_signal

try:
    import pyedflib
except ImportError as exc:
    raise SystemExit("Missing dependency: pyEDFlib. Install with `pip install pyEDFlib`.") from exc

try:
    import pyqtgraph as pg
    from pyqtgraph.Qt import QtWidgets
except ImportError as exc:
    raise SystemExit("Missing GUI dependencies. Install pyqtgraph + PyQt5.") from exc


BANDS = [
    ("Theta", 4, 8),
    ("Alpha", 8, 13),
    ("Beta", 13, 30),
]

DISPLAY_DOWNSAMPLE = 3


def zscore(x):
    x = np.asarray(x, dtype=np.float32)
    s = np.std(x)
    if s < 1e-8:
        return x - np.mean(x)
    return (x - np.mean(x)) / s


def make_bandpass_filter(low_hz, high_hz, fs, order=4):
    nyq = fs / 2
    low = max(low_hz / nyq, 0.001)
    high = min(high_hz / nyq, 0.99)
    return scipy_signal.butter(order, [low, high], btype="band")


def load_edf(path):
    rdr = pyedflib.EdfReader(str(path))
    try:
        n = rdr.signals_in_file
        labels = rdr.getSignalLabels()
        fs = float(rdr.getSampleFrequency(0))
        data = np.column_stack([rdr.readSignal(i) for i in range(n)]).astype(np.float64)
        return data, labels, fs
    finally:
        rdr.close()


def ensure_same_length(a, b):
    n = min(a.shape[0], b.shape[0])
    return a[:n], b[:n]


def build_curves(widget, channel_names):
    curves = []
    n_ch = len(channel_names)
    n_cols = 2
    colors = [pg.intColor(i) for i in range(n_ch)]
    for ch in range(n_ch):
        row = ch // n_cols
        col = ch % n_cols
        p = widget.addPlot(row=row, col=col, title=channel_names[ch])
        p.setLabel("bottom", "Time", "s")
        p.setLabel("left", "z-score")
        p.showGrid(x=True, y=True, alpha=0.25)
        p.getViewBox().enableAutoRange(axis="y")
        c = p.plot(pen=pg.mkPen(color=colors[ch], width=1.4))
        curves.append(c)
    return curves


def main():
    if len(sys.argv) >= 3:
        hb1_path = Path(sys.argv[1])
        hb2_path = Path(sys.argv[2])
    else:
        hb1_path = Path(r"D:\Cogwear demo\test\test1_HB1_COM3_20260227_124306.edf")
        hb2_path = Path(r"D:\Cogwear demo\test\test1_HB2_COM4_20260227_124306.edf")

    if not hb1_path.exists() or not hb2_path.exists():
        raise SystemExit("EDF path not found. Pass two valid EDF files.")

    hb1_data, hb1_labels, fs1 = load_edf(hb1_path)
    hb2_data, hb2_labels, fs2 = load_edf(hb2_path)
    if abs(fs1 - fs2) > 1e-6:
        raise SystemExit(f"Sample-rate mismatch: HB1={fs1}, HB2={fs2}")

    hb1_data, hb2_data = ensure_same_length(hb1_data, hb2_data)
    fs = fs1
    data = np.column_stack([hb1_data, hb2_data])
    channel_names = [f"HB1_{x}" for x in hb1_labels] + [f"HB2_{x}" for x in hb2_labels]

    t = np.arange(data.shape[0], dtype=np.float64) / fs
    ds = DISPLAY_DOWNSAMPLE
    td = t[::ds]

    app = QtWidgets.QApplication(sys.argv)
    tabs = QtWidgets.QTabWidget()
    tabs.setWindowTitle(f"EDF Viewer | fs={fs:.1f} Hz | {hb1_path.name} + {hb2_path.name}")
    tabs.resize(1400, 900)

    raw_widget = pg.GraphicsLayoutWidget()
    raw_curves = build_curves(raw_widget, channel_names)
    tabs.addTab(raw_widget, "Raw")

    band_widgets = {}
    band_curves = {}
    for name, _, _ in BANDS:
        w = pg.GraphicsLayoutWidget()
        band_widgets[name] = w
        band_curves[name] = build_curves(w, channel_names)
        tabs.addTab(w, name)

    # Raw (z-score)
    for ch in range(data.shape[1]):
        y = zscore(data[:, ch])[::ds]
        raw_curves[ch].setData(td, y)

    # Bands
    for name, lo, hi in BANDS:
        b, a = make_bandpass_filter(lo, hi, fs)
        for ch in range(data.shape[1]):
            y = scipy_signal.filtfilt(b, a, data[:, ch])
            y = zscore(y)[::ds]
            band_curves[name][ch].setData(td, y)

    tabs.show()
    if hasattr(app, "exec"):
        sys.exit(app.exec())
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
