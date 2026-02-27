#!/usr/bin/env python
"""
Plot preprocessed raw EEG signals from two CSV files (HB1 + HB2).

Usage:
    python plot_preprocessed_csv.py "HB1.csv" "HB2.csv"
"""

import csv
import sys
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt


CHANNELS = ["AF7", "FP1", "FP2", "AF8"]


def load_csv(path):
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    t = []
    data = {c: [] for c in CHANNELS}
    for r in rows:
        try:
            t_val = float(r.get("t_rel_sec", "nan"))
        except Exception:
            t_val = np.nan
        t.append(t_val)
        for c in CHANNELS:
            try:
                data[c].append(float(r.get(c, "nan")))
            except Exception:
                data[c].append(np.nan)
    t = np.asarray(t, dtype=np.float64)
    arr = np.column_stack([np.asarray(data[c], dtype=np.float64) for c in CHANNELS])
    # Drop invalid rows (e.g. metadata rows)
    valid = np.isfinite(t)
    for i in range(arr.shape[1]):
        valid &= np.isfinite(arr[:, i])
    return t[valid], arr[valid]


def main():
    if len(sys.argv) >= 3:
        hb1_path = Path(sys.argv[1])
        hb2_path = Path(sys.argv[2])
    else:
        raise SystemExit("Usage: python plot_preprocessed_csv.py HB1.csv HB2.csv")

    if not hb1_path.exists() or not hb2_path.exists():
        raise SystemExit("CSV file not found.")

    t1, d1 = load_csv(hb1_path)
    t2, d2 = load_csv(hb2_path)
    n = min(len(t1), len(t2))
    t1, d1 = t1[:n], d1[:n]
    t2, d2 = t2[:n], d2[:n]

    fig, axes = plt.subplots(4, 2, figsize=(14, 10), sharex=True)
    fig.suptitle("Preprocessed Raw EEG from CSV (uV)")

    for i, ch in enumerate(CHANNELS):
        ax = axes[i, 0]
        ax.plot(t1, d1[:, i], linewidth=0.8)
        ax.set_title(f"HB1_{ch}")
        ax.grid(alpha=0.3)
        ax.set_ylabel("uV")

    for i, ch in enumerate(CHANNELS):
        ax = axes[i, 1]
        ax.plot(t2, d2[:, i], linewidth=0.8)
        ax.set_title(f"HB2_{ch}")
        ax.grid(alpha=0.3)

    axes[-1, 0].set_xlabel("Time (s)")
    axes[-1, 1].set_xlabel("Time (s)")
    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    main()
