#!/usr/bin/env python
"""
Visualize two CSV recordings (HB1 + HB2) in one GUI.

Usage:
    python visualize_two_csv.py "HB1.csv" "HB2.csv"
"""

import csv
import sys
from pathlib import Path

import numpy as np
from scipy import signal as scipy_signal

try:
    import pyqtgraph as pg
    from pyqtgraph.Qt import QtWidgets
except ImportError as exc:
    raise SystemExit("Missing GUI dependencies. Install pyqtgraph + PyQt5.") from exc


CHANNELS = ["AF7", "FP1", "FP2", "AF8"]
BANDS = [("Theta", 4, 8), ("Alpha", 8, 13), ("Beta", 13, 30)]
DEFAULT_FS = 250.0
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


def load_csv(path):
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    ts = []
    t_rel = []
    data = {c: [] for c in CHANNELS}
    for r in rows:
        try:
            ts.append(float(r.get("timestamp_unix", r.get("timestamp", "nan"))))
        except Exception:
            ts.append(np.nan)
        try:
            t_rel.append(float(r.get("t_rel_sec", "nan")))
        except Exception:
            t_rel.append(np.nan)
        for c in CHANNELS:
            try:
                data[c].append(float(r.get(c, "nan")))
            except Exception:
                data[c].append(np.nan)
    arr = np.column_stack([np.asarray(data[c], dtype=np.float64) for c in CHANNELS])
    return np.asarray(ts, dtype=np.float64), np.asarray(t_rel, dtype=np.float64), arr


def estimate_fs_from_timestamps(ts):
    ts = ts[np.isfinite(ts)]
    if len(ts) < 2:
        return None
    d = np.diff(ts)
    d = d[d > 0]
    if len(d) < 10:
        return None
    fs = 1.0 / np.median(d)
    return fs


def choose_fs(ts1, ts2):
    fs1 = estimate_fs_from_timestamps(ts1)
    fs2 = estimate_fs_from_timestamps(ts2)
    candidates = [x for x in [fs1, fs2] if x is not None]
    if not candidates:
        return DEFAULT_FS
    fs = float(np.median(candidates))
    # Host timestamps in this project can be noisy; clamp to expected board range.
    if fs < 150 or fs > 350:
        return DEFAULT_FS
    return fs


def build_curves(widget, names):
    curves = []
    colors = [pg.intColor(i) for i in range(len(names))]
    for i, name in enumerate(names):
        row, col = divmod(i, 2)
        p = widget.addPlot(row=row, col=col, title=name)
        p.setLabel("bottom", "Time", "s")
        p.setLabel("left", "z-score")
        p.showGrid(x=True, y=True, alpha=0.25)
        p.getViewBox().enableAutoRange(axis="y")
        curves.append(p.plot(pen=pg.mkPen(colors[i], width=1.4)))
    return curves


def main():
    if len(sys.argv) >= 3:
        hb1_path = Path(sys.argv[1])
        hb2_path = Path(sys.argv[2])
    else:
        hb1_path = Path(r"D:\Cogwear demo\test\test7_HB1_COM3_20260227_151335.csv")
        hb2_path = Path(r"D:\Cogwear demo\test\test7_HB2_COM4_20260227_151335.csv")

    if not hb1_path.exists() or not hb2_path.exists():
        raise SystemExit("CSV path not found. Pass two valid CSV files.")

    ts1, tr1, d1 = load_csv(hb1_path)
    ts2, tr2, d2 = load_csv(hb2_path)
    n = min(len(d1), len(d2))
    d1, d2 = d1[:n], d2[:n]
    ts1, ts2 = ts1[:n], ts2[:n]
    tr1, tr2 = tr1[:n], tr2[:n]
    data = np.column_stack([d1, d2])  # n x 8
    data = np.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)

    fs = choose_fs(tr1, tr2)
    if fs == DEFAULT_FS:
        fs = choose_fs(ts1, ts2)
    t = np.arange(n, dtype=np.float64) / fs
    td = t[::DISPLAY_DOWNSAMPLE]
    channel_names = [f"HB1_{c}" for c in CHANNELS] + [f"HB2_{c}" for c in CHANNELS]

    app = QtWidgets.QApplication(sys.argv)
    tabs = QtWidgets.QTabWidget()
    tabs.setWindowTitle(f"CSV Viewer | fs={fs:.1f} Hz | {hb1_path.name} + {hb2_path.name}")
    tabs.resize(1400, 900)

    raw_w = pg.GraphicsLayoutWidget()
    raw_curves = build_curves(raw_w, channel_names)
    tabs.addTab(raw_w, "Raw")

    band_curves = {}
    for name, _, _ in BANDS:
        w = pg.GraphicsLayoutWidget()
        band_curves[name] = build_curves(w, channel_names)
        tabs.addTab(w, name)

    for ch in range(data.shape[1]):
        y = zscore(data[:, ch])[::DISPLAY_DOWNSAMPLE]
        raw_curves[ch].setData(td, y)

    for name, lo, hi in BANDS:
        b, a = make_bandpass_filter(lo, hi, fs)
        for ch in range(data.shape[1]):
            y = scipy_signal.filtfilt(b, a, data[:, ch])
            y = zscore(y)[::DISPLAY_DOWNSAMPLE]
            band_curves[name][ch].setData(td, y)

    tabs.show()
    if hasattr(app, "exec"):
        sys.exit(app.exec())
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
