#!/usr/bin/env python
"""
OpenBCI EEG Viewer - 4/8/12/16 Channels (1-4 headbands)

Each headband: Ch1=AF7, Ch2=FP1, Ch3=FP2, Ch4=AF8
Each headband needs its own USB dongle -> separate COM port.
Set headbands on different radio channels to avoid interference.

Usage:
    python view_raw_signal.py COM3                    # 1 headband
    python view_raw_signal.py COM3 COM4               # 2 headbands
    python view_raw_signal.py COM3 COM4 COM5 COM6     # 4 headbands (with EEG synchrony)
"""

import sys
import warnings
import threading
import collections
import numpy as np

warnings.filterwarnings('ignore', category=UserWarning, module='sklearn.decomposition._fastica')
warnings.filterwarnings('ignore', category=RuntimeWarning, module='sklearn.decomposition._fastica')
warnings.filterwarnings('ignore', category=RuntimeWarning, module='numpy')
from scipy import signal as scipy_signal
from scipy.stats import kurtosis

sys.path.insert(0, '.')

try:
    import pyqtgraph as pg
    from pyqtgraph.Qt import QtCore, QtGui, QtWidgets
    pg.setConfigOptions(antialias=True)
    # useOpenGL=True can cause stalls on some Windows systems
    USE_PYQTGRAPH = True
except ImportError:
    USE_PYQTGRAPH = False

try:
    from sklearn.decomposition import FastICA
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False

# Per-headband electrode order: Ch1=AF7, Ch2=FP1, Ch3=FP2, Ch4=AF8
CHANNELS_PER_HEADBAND = 4
ELECTRODE_NAMES = ['AF7', 'FP1', 'FP2', 'AF8']

# Preprocessing
LOW_CUTOFF = 0.3   # Hz
HIGH_CUTOFF = 50.0  # Hz
NOTCH_HZ = 50.0    # Power line notch: 50 (Europe/Asia), 60 (Americas), 0 to disable
NOTCH_Q = 30       # Notch sharpness (original)
Z_THRESHOLD = 1.96  # Exclude ICA components with |kurtosis_z| > this
USE_CAR = True     # Common Average Reference per headband (reduces common-mode noise)
DISPLAY_SMOOTH = 15  # Moving average for display (1=off, 15=strong smoothing)
OUTLIER_REJECT_Z = 2.0  # Replace spikes beyond ±this with median (2.0=aggressive)
DISPLAY_CLIP_Z = 2.5    # Cap display at ±this (tighter=less spike dominance)
DISPLAY_MEDIAN_FILTER = 5  # Median filter before smoothing (0=off, 5=removes spikes)

BANDS = [
    ('Theta', 4, 8),
    ('Alpha', 8, 13),
    ('Beta', 13, 30),
    ('Gamma', 30, 60),
]
# Band for EEG synchrony (Phase Locking Value)
SYNC_BAND_LO, SYNC_BAND_HI = 8, 13  # Alpha band

# Display: True = auto-scale Y to data (helps when signal is small); False = fixed [-4, 4] z-score
PLOT_AUTO_RANGE_Y = True
# Set True to bypass ICA (useful if ICA over-rejects and flattens signal when electrodes on scalp)
ICA_DISABLED = False
# Set True to show raw µV in Raw window (helps verify signal amplitude; typical EEG 10-100 µV on scalp)
SHOW_RAW_UV = False


def make_bandpass_filter(low_hz, high_hz, fs, order=4):
    nyq = fs / 2
    low = max(low_hz / nyq, 0.001)
    high = min(high_hz / nyq, 0.99)
    b, a = scipy_signal.butter(order, [low, high], btype='band')
    return b, a


def make_notch_filter(notch_hz, fs, Q=50):
    """Notch filter to remove power line noise (50 or 60 Hz). Higher Q = narrower notch."""
    nyq = fs / 2
    w0 = notch_hz / nyq
    b, a = scipy_signal.iirnotch(w0, Q)
    return b, a


def smooth_display(y, window):
    """Light moving average for display (reduces visual jitter)."""
    if window < 2 or len(y) < window:
        return y
    return np.convolve(y, np.ones(window) / window, mode='same')


def reject_outliers_display(y, z_thresh):
    """Replace spike artifacts (beyond ±z_thresh) with local median for cleaner display."""
    if z_thresh <= 0 or len(y) < 10:
        return y
    out = np.array(y, dtype=np.float32)
    med = np.median(out)
    mask = np.abs(out - med) > z_thresh
    if np.any(mask):
        k = min(5, len(y) | 1)  # odd kernel, max 5
        if k >= 3:
            filtered = scipy_signal.medfilt(out, kernel_size=k)
            out[mask] = filtered[mask]
        else:
            out[mask] = med
    return out


def zscore_normalize(data):
    """Normalize to zero mean, unit std. For near-flat signals, return mean-centered (auto-range will scale)."""
    arr = np.asarray(data, dtype=np.float32)
    if len(arr) < 10:
        return arr
    mean = np.mean(arr)
    std = np.std(arr)
    if std < 1e-6:
        return (arr - mean)  # show raw deviation; Y auto-range will scale
    return (arr - mean) / std


# ICA runs every ICA_UPDATE_INTERVAL_MS (not every frame)
ICA_UPDATE_INTERVAL_MS = 2500  # 2.5 seconds
PLOT_REFRESH_MS = 33  # ~30 Hz (synchronous processing)
DISPLAY_DOWNSAMPLE = 4  # plot every Nth point (1250->312) for faster GPU rendering

def fit_ica_and_reject(data, z_threshold=1.96):
    """
    Fit ICA on data, return (ica, keep_mask) or (None, None) on failure.
    """
    if not HAS_SKLEARN or data.shape[1] < 2 or data.shape[0] < 500:
        return None, None
    try:
        # Clip and normalize to prevent overflow/divide-by-zero with noisy data
        data = np.asarray(data, dtype=np.float64)
        if np.any(np.isnan(data)) or np.any(np.isinf(data)):
            return None, None
        std = np.std(data)
        if std < 1e-6:
            return None, None
        data = np.clip(data, -10 * std, 10 * std)  # clip outliers
        ica = FastICA(n_components=data.shape[1], random_state=0, max_iter=1000, tol=0.01)
        components = ica.fit_transform(data)
        kur = kurtosis(components, axis=0, fisher=True)
        kur_mean, kur_std = np.mean(kur), np.std(kur)
        if kur_std < 1e-10:
            return ica, np.ones(data.shape[1], dtype=bool)  # keep all
        kur_z = np.abs((kur - kur_mean) / kur_std)
        keep = kur_z <= z_threshold
        if np.sum(keep) == 0:
            keep = np.ones(data.shape[1], dtype=bool)
        return ica, keep
    except Exception:
        return None, None


def apply_car_per_headband(data, channels_per_hb, n_headbands):
    """Common Average Reference: subtract mean of each headband's channels from that headband."""
    out = data.copy()
    for i in range(n_headbands):
        ch_start = i * channels_per_hb
        ch_end = ch_start + channels_per_hb
        mean_sig = np.mean(data[:, ch_start:ch_end], axis=1, keepdims=True)
        out[:, ch_start:ch_end] = data[:, ch_start:ch_end] - mean_sig
    return out


def apply_ica_transform(data, ica, keep_mask):
    """Apply existing ICA and mask to new data. Returns data unchanged if ica is None."""
    if ica is None or keep_mask is None:
        return data
    try:
        components = ica.transform(data)
        components_clean = components.copy()
        components_clean[:, ~keep_mask] = 0
        return ica.inverse_transform(components_clean)
    except Exception:
        return data


def compute_plv(sig1, sig2, band_lo, band_hi, fs, order=4):
    """
    Phase Locking Value (PLV) between two signals in a frequency band.
    PLV = |mean(exp(1j * (phase1 - phase2)))| in [0, 1].
    """
    if len(sig1) < 100 or len(sig2) < 100:
        return 0.0
    try:
        nyq = fs / 2
        b, a = scipy_signal.butter(order, [band_lo / nyq, band_hi / nyq], btype='band')
        s1 = scipy_signal.filtfilt(b, a, np.asarray(sig1, dtype=np.float64))
        s2 = scipy_signal.filtfilt(b, a, np.asarray(sig2, dtype=np.float64))
        h1 = scipy_signal.hilbert(s1)
        h2 = scipy_signal.hilbert(s2)
        phase1 = np.angle(h1)
        phase2 = np.angle(h2)
        plv = np.abs(np.mean(np.exp(1j * (phase1 - phase2))))
        return float(np.clip(plv, 0, 1))
    except Exception:
        return 0.0


def compute_pairwise_plv(data_clean, n_headbands, channels_per_hb, band_lo, band_hi, fs):
    """
    Compute PLV for all pairs of headbands. Uses mean of channels per headband as subject signal.
    Returns list of (label, plv) for each pair.
    """
    pairs = []
    subject_signals = []
    for i in range(n_headbands):
        ch_start = i * channels_per_hb
        ch_end = ch_start + channels_per_hb
        sig = np.mean(data_clean[:, ch_start:ch_end], axis=1)
        subject_signals.append(sig)
    for i in range(n_headbands):
        for j in range(i + 1, n_headbands):
            plv = compute_plv(subject_signals[i], subject_signals[j], band_lo, band_hi, fs)
            pairs.append((f"HB{i+1}-HB{j+1}", plv))
    return pairs


def main():
    # Parse ports: python view_raw_signal.py COM3 COM4 COM5 COM6
    ports = [a for a in sys.argv[1:] if a.upper().startswith('COM') or '/' in a or 'tty' in a]
    if not ports:
        # Default to 4 headbands (COM3-COM6) for multi-subject synchrony
        ports = ['COM3', 'COM4', 'COM5', 'COM6']
        print("No ports specified, using COM3 COM4 COM5 COM6 (4 headbands).")

    if not USE_PYQTGRAPH:
        print("Error: pyqtgraph and Qt required. Install: pip install pyqtgraph PyQt5")
        sys.exit(1)
    if not HAS_SKLEARN:
        print("Error: scikit-learn required for ICA. Install: pip install scikit-learn")
        sys.exit(1)

    import lib.open_bci_v3 as bci
    import time

    N_CHANNELS = len(ports) * CHANNELS_PER_HEADBAND
    CHANNEL_LABELS = [f"HB{i+1}_{e}" for i in range(len(ports)) for e in ELECTRODE_NAMES]

    print(f"Connecting to {len(ports)} headband(s)...")
    boards = []
    for port in ports:
        for attempt in range(5):
            try:
                b = bci.OpenBCIBoard(port=port) if port else bci.OpenBCIBoard()
                boards.append(b)
                print(f"  {port or 'auto'}: OK")
                break
            except Exception as e:
                err_msg = str(e).encode('ascii', errors='replace').decode('ascii')
                if 'PermissionError' in err_msg or 'could not open port' in err_msg or 'Serial' in err_msg:
                    if attempt < 4:
                        print(f"  {port}: busy or not found, retrying...")
                        time.sleep(2)
                    else:
                        # When defaulting to 4 ports, skip unavailable ones
                        if len(ports) == 4 and ports == ['COM3', 'COM4', 'COM5', 'COM6']:
                            print(f"  {port}: skipped (not available)")
                            break
                        else:
                            print(f"Error: {err_msg}")
                            print("Tip: Unplug dongles, wait 5s, plug back in. Use different USB ports for each.")
                            sys.exit(1)
                else:
                    print(f"Error: {err_msg}")
                    sys.exit(1)
    if not boards:
        print("Error: No headbands connected. Specify ports: python view_raw_signal.py COM3 COM4 ...")
        sys.exit(1)

    sample_rate = boards[0].getSampleRate()
    print(f"Connected: {N_CHANNELS} channels @ {sample_rate} Hz")
    print(f"Channels: {CHANNEL_LABELS}")
    if len(boards) > 1:
        print("*** RADIO CHANNELS: Each headband MUST use a different radio channel to avoid interference. ***")
        print("    Change via: OpenBCI Hub GUI -> Radio Config, or Arduino (docs.openbci.com/Cyton/CytonRadios/)")
        print("    Suggested channels (avoid WiFi 2.4GHz overlap): 11, 12, 13, 14 or 15, 16, 17, 18")
        print(f"EEG synchrony (PLV, {SYNC_BAND_LO}-{SYNC_BAND_HI} Hz): all pairwise pairs in dedicated window.")
    notch_str = f", {NOTCH_HZ:.0f}Hz notch" if NOTCH_HZ > 0 else ""
    ica_str = "disabled" if ICA_DISABLED else f"z-threshold={Z_THRESHOLD}"
    print(f"Preprocessing: {LOW_CUTOFF}-{HIGH_CUTOFF} Hz{notch_str}, ICA {ica_str}")
    print(f"ICA every {ICA_UPDATE_INTERVAL_MS/1000}s, display ~{1000/PLOT_REFRESH_MS:.0f} Hz")

    window_sec = 5
    buffer_size = int(window_sec * sample_rate)
    time_axis = np.linspace(-window_sec, 0, buffer_size)

    channel_buffers = [
        collections.deque([0.0] * buffer_size, maxlen=buffer_size)
        for _ in range(N_CHANNELS)
    ]

    # Bandpass 0.3-50 Hz for preprocessing
    bp_b, bp_a = make_bandpass_filter(LOW_CUTOFF, HIGH_CUTOFF, sample_rate)
    notch_b, notch_a = (make_notch_filter(NOTCH_HZ, sample_rate, Q=NOTCH_Q) if NOTCH_HZ > 0 else (None, None))
    band_filters = {
        name: make_bandpass_filter(lo, hi, sample_rate)
        for name, lo, hi in BANDS
    }

    app = QtWidgets.QApplication(sys.argv)
    colors = [pg.intColor(i) for i in range(N_CHANNELS)]

    def create_plot_window(window_title, ylabel="z-score"):
        """Create window with plots in a grid (2x2, 2x4, or 4x4) to reduce vertical stacking."""
        win = pg.GraphicsLayoutWidget()
        win.setWindowTitle(window_title)
        n_cols = 4 if N_CHANNELS >= 8 else 2
        n_rows = (N_CHANNELS + n_cols - 1) // n_cols
        win.resize(min(900, 280 * n_cols), max(350, 160 * n_rows))
        curves = []
        for ch in range(N_CHANNELS):
            row, col = ch // n_cols, ch % n_cols
            plot = win.addPlot(row=row, col=col, title=CHANNEL_LABELS[ch])
            plot.setLabel("bottom", "Time", "s")
            plot.setLabel("left", ylabel)
            plot.setXRange(-window_sec, 0)
            if PLOT_AUTO_RANGE_Y:
                plot.getViewBox().enableAutoRange(axis='y')
            else:
                plot.setYRange(-4, 4)
            plot.showGrid(x=True, y=True, alpha=0.3)
            c = plot.plot(pen=pg.mkPen(color=colors[ch], width=1.5))
            c.setClipToView(True)  # only render visible region (GPU-friendly)
            curves.append(c)
        return win, curves

    # Windows with channel names in titles
    raw_ylabel = "µV" if SHOW_RAW_UV else "z-score"
    raw_title = f"Raw EEG - {', '.join(CHANNEL_LABELS)} (0.3-50 Hz, {raw_ylabel})"
    if not ICA_DISABLED:
        raw_title += ", ICA"
    raw_win, raw_curves = create_plot_window(raw_title, ylabel=raw_ylabel)
    # Packet loss status bar at bottom of raw window
    n_cols = 4 if N_CHANNELS >= 8 else 2
    n_rows = (N_CHANNELS + n_cols - 1) // n_cols
    packet_loss_label = raw_win.addLabel("Packet loss: --", row=n_rows, col=0, colspan=n_cols)
    theta_win, theta_curves = create_plot_window(
        f"Theta (4-8 Hz) - {', '.join(CHANNEL_LABELS)}", "z-score"
    )
    alpha_win, alpha_curves = create_plot_window(
        f"Alpha (8-13 Hz) - {', '.join(CHANNEL_LABELS)}", "z-score"
    )
    beta_win, beta_curves = create_plot_window(
        f"Beta (13-30 Hz) - {', '.join(CHANNEL_LABELS)}", "z-score"
    )
    gamma_win, gamma_curves = create_plot_window(
        f"Gamma (30-60 Hz) - {', '.join(CHANNEL_LABELS)}", "z-score"
    )

    all_windows = [raw_win, theta_win, alpha_win, beta_win, gamma_win]
    band_curves = [theta_curves, alpha_curves, beta_curves, gamma_curves]

    # EEG synchrony window (pairwise PLV) - only when 2+ headbands
    n_headbands = len(boards)
    sync_win = None
    sync_bars = None
    if n_headbands >= 2:
        sync_win = pg.GraphicsLayoutWidget()
        sync_win.setWindowTitle(f"EEG Synchrony (PLV, {SYNC_BAND_LO}-{SYNC_BAND_HI} Hz) - All pairs")
        sync_win.resize(500, 350)
        sync_plot = sync_win.addPlot(title="Pairwise phase locking value")
        sync_plot.setLabel("bottom", "Subject pair")
        sync_plot.setLabel("left", "PLV")
        sync_plot.setYRange(0, 1)
        sync_plot.showGrid(x=True, y=True, alpha=0.3)
        pair_labels = [f"HB{i+1}-HB{j+1}" for i in range(n_headbands) for j in range(i + 1, n_headbands)]
        sync_bars = pg.BarGraphItem(x=range(len(pair_labels)), height=[0] * len(pair_labels), width=0.6)
        sync_plot.addItem(sync_bars)
        sync_plot.getAxis("bottom").setTicks([[(i, pair_labels[i]) for i in range(len(pair_labels))]])
        all_windows.append(sync_win)

    lock = threading.Lock()
    stream_running = True
    ica_state = [None, None]
    last_ica_time = [0.0]
    crosstalk_check_done = [False]  # run once after ~5s of data

    def make_callback(ch_offset):
        def on_sample(sample):
            with lock:
                for i in range(min(CHANNELS_PER_HEADBAND, len(sample.channel_data))):
                    channel_buffers[ch_offset + i].append(sample.channel_data[i])
        return on_sample

    def update_plots():
        if not stream_running:
            return
        # Copy data quickly - don't hold lock during heavy processing (avoids blocking stream)
        with lock:
            min_len = min(len(channel_buffers[ch]) for ch in range(N_CHANNELS))
            if min_len < 500:
                app.processEvents()
                return
            data = np.column_stack([
                np.array(list(channel_buffers[ch])[-min_len:], dtype=np.float32)
                for ch in range(N_CHANNELS)
            ])

        # Sanitize: replace NaN/Inf (can occur with packet loss) to avoid scipy byref() errors
        if np.any(~np.isfinite(data)):
            data = np.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)

        try:
            # Bandpass 0.3-50 Hz
            data_bp = np.empty_like(data)
            for ch in range(N_CHANNELS):
                data_bp[:, ch] = scipy_signal.filtfilt(bp_b, bp_a, data[:, ch])
            # Notch filter for power line (50/60 Hz) - reduces noise when electrodes off-scalp
            if notch_b is not None:
                for ch in range(N_CHANNELS):
                    data_bp[:, ch] = scipy_signal.filtfilt(notch_b, notch_a, data_bp[:, ch])

            # Common Average Reference per headband (reduces common-mode noise)
            if USE_CAR:
                data_bp = apply_car_per_headband(data_bp, CHANNELS_PER_HEADBAND, n_headbands)

            # ICA: run periodically
            now = time.time()
            if now - last_ica_time[0] >= ICA_UPDATE_INTERVAL_MS / 1000.0:
                ica, keep = fit_ica_and_reject(data_bp, z_threshold=Z_THRESHOLD)
                if ica is not None:
                    ica_state[0], ica_state[1] = ica, keep
                    if last_ica_time[0] == 0:
                        print(f"ICA fitted (rejected {np.sum(~keep)} components)")
                last_ica_time[0] = now

            if ICA_DISABLED:
                data_clean = data_bp
            elif ica_state[0] is not None:
                data_clean = apply_ica_transform(data_bp, ica_state[0], ica_state[1])
            else:
                data_clean = data_bp

            # Cross-talk check (once): warn if headbands appear on same radio channel
            if not crosstalk_check_done[0] and n_headbands >= 2 and data_clean.shape[0] >= 1000:
                crosstalk_check_done[0] = True
                for i in range(n_headbands):
                    for j in range(i + 1, n_headbands):
                        s1 = data_clean[:, i * CHANNELS_PER_HEADBAND]
                        s2 = data_clean[:, j * CHANNELS_PER_HEADBAND]
                        corr = np.corrcoef(s1, s2)[0, 1] if np.std(s1) > 1e-6 and np.std(s2) > 1e-6 else 0
                        if not np.isnan(corr) and abs(corr) > 0.92:
                            print(f"*** CROSSTALK WARNING: HB{i+1} and HB{j+1} correlation={corr:.2f} - may be on same radio channel! ***")

            ds = DISPLAY_DOWNSAMPLE
            t = time_axis[::ds]
            smooth = DISPLAY_SMOOTH
            clip_z = DISPLAY_CLIP_Z
            out_z = OUTLIER_REJECT_Z
            med_k = DISPLAY_MEDIAN_FILTER
            for ch in range(N_CHANNELS):
                raw_vals = (data_clean[:, ch] if SHOW_RAW_UV else zscore_normalize(data_clean[:, ch]))[::ds]
                if med_k >= 3 and len(raw_vals) >= med_k and not SHOW_RAW_UV:
                    raw_vals = scipy_signal.medfilt(raw_vals, kernel_size=med_k)
                if out_z > 0 and not SHOW_RAW_UV:
                    raw_vals = reject_outliers_display(raw_vals, out_z)
                if smooth > 1:
                    raw_vals = smooth_display(raw_vals, smooth)
                if clip_z > 0 and not SHOW_RAW_UV:
                    raw_vals = np.clip(raw_vals, -clip_z, clip_z)
                raw_curves[ch].setData(t[-len(raw_vals):], raw_vals)
                for band_idx, (name, _, _) in enumerate(BANDS):
                    b, a = band_filters[name]
                    band_z = zscore_normalize(scipy_signal.filtfilt(b, a, data_clean[:, ch]))[::ds]
                    if med_k >= 3 and len(band_z) >= med_k:
                        band_z = scipy_signal.medfilt(band_z, kernel_size=med_k)
                    if out_z > 0:
                        band_z = reject_outliers_display(band_z, out_z)
                    if smooth > 1:
                        band_z = smooth_display(band_z, smooth)
                    if clip_z > 0:
                        band_z = np.clip(band_z, -clip_z, clip_z)
                    band_curves[band_idx][ch].setData(t[-len(band_z):], band_z)

            # Packet loss and flat-signal display
            parts = []
            hb_stds = [np.std(data_clean[:, i*CHANNELS_PER_HEADBAND:(i+1)*CHANNELS_PER_HEADBAND]) for i in range(n_headbands)]
            ref_std = np.median(hb_stds) if hb_stds else 1.0
            for i, b in enumerate(boards):
                ok, dropped = b.get_packet_stats()
                total = ok + dropped
                pct = (100.0 * dropped / total) if total > 0 else 0
                name = f"HB{i+1}" if n_headbands > 1 else "Board"
                flat = " ⚠flat" if ref_std > 1e-6 and hb_stds[i] < ref_std * 0.15 else ""
                parts.append(f"{name}: {pct:.2f}% ({dropped}/{total}){flat}")
            packet_loss_label.setText("Packet loss: " + "  |  ".join(parts))

            # Real-time EEG synchrony (pairwise PLV) for 2+ headbands
            if sync_bars is not None and n_headbands >= 2:
                try:
                    pairs = compute_pairwise_plv(
                        data_clean, n_headbands, CHANNELS_PER_HEADBAND,
                        SYNC_BAND_LO, SYNC_BAND_HI, sample_rate
                    )
                    if pairs:
                        heights = [p[1] for p in pairs]
                        sync_bars.setOpts(height=heights)
                except Exception:
                    pass

            app.processEvents()
        except Exception as e:
            # Catch scipy byref() and other processing errors (e.g. from corrupted data)
            if "byref" in str(e) or "ctypes" in str(e):
                pass  # skip this frame
            else:
                print(f"Plot error: {e}")

    def stream_thread(board, ch_offset):
        nonlocal stream_running
        try:
            board.start_streaming(make_callback(ch_offset), lapse=-1)
        except Exception as e:
            print(f"Stream error: {e}")
        finally:
            stream_running = False

    for i, board in enumerate(boards):
        ch_offset = i * CHANNELS_PER_HEADBAND
        t = threading.Thread(target=stream_thread, args=(board, ch_offset), daemon=True)
        t.start()

    closed = False

    def on_close(e):
        nonlocal stream_running, closed
        if closed:
            e.accept()
            return
        closed = True
        stream_running = False
        for b in boards:
            try:
                b.stop()
                b.disconnect()
            except Exception:
                pass
        print("Disconnected.")
        e.accept()
        app.quit()

    for w in all_windows:
        w.closeEvent = on_close

    timer = QtCore.QTimer()
    timer.timeout.connect(update_plots)
    timer.start(PLOT_REFRESH_MS)

    # Position windows in a grid (3 cols; sync window added when 2+ headbands)
    try:
        screen = app.primaryScreen().availableGeometry()
        sw, sh = screen.width(), screen.height()
        n_wins = len(all_windows)
        ww, wh = min(550, sw // 3), min(450, sh // 2)
        positions = [(i % 3 * ww, i // 3 * wh) for i in range(n_wins)]
        for i, w in enumerate(all_windows):
            w.resize(ww, wh)
            x = screen.x() + positions[i][0]
            y = screen.y() + positions[i][1]
            w.setGeometry(x, y, ww, wh)
    except Exception:
        pass  # fallback to default positions

    for w in all_windows:
        w.show()

    n_wins = len(all_windows)
    print(f"Streaming. {n_wins} windows tiled on screen. Close any to stop.")
    if hasattr(app, 'exec'):
        sys.exit(app.exec())
    else:
        sys.exit(app.exec_())


if __name__ == "__main__":
    main()
