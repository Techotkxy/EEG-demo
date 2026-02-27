# EEG-demo / Cogwear Demo

Real-time EEG streaming and recording for OpenBCI Cyton headbands.

## Current workflow (latest)

- Live multi-headband viewer: `view_raw_signal.py`
- Dedicated 2-headband recorder GUI: `dual_headband_recorder_gui.py`
- CSV plotting tools:
  - `plot_preprocessed_csv.py` (simple matplotlib)
  - `visualize_two_csv.py` (tabbed pyqtgraph)

> Current recorder output is **CSV + WAV** (no EDF export in this version).

## Install

```bash
pip install -r requirements.txt
```

## Hardware notes

- One dongle per headband (separate COM ports).
- Use different radio channels per headband to reduce interference.
- See `RADIO_SETUP.md` for channel setup details.

## Run scripts

### 1) Multi-headband viewer (1-4 headbands)

```bash
python view_raw_signal.py COM3
python view_raw_signal.py COM3 COM4
python view_raw_signal.py COM3 COM4 COM5 COM6
```

### 2) 2-headband recorder GUI (recommended)

```bash
python dual_headband_recorder_gui.py COM3 COM4
```

Features:
- Raw/Theta/Alpha/Beta real-time plots
- Start/Stop recording controls
- Dongle-to-port mapping helper (COM + USB details)
- Live metrics (elapsed time, samples, packet loss)
- Optional microphone capture synchronized to EEG session

## What is written to disk

For session name `test8` at timestamp `20260227_152855`:

- `test8_HB1_COM3_20260227_152855.csv`
- `test8_HB2_COM4_20260227_152855.csv`
- `test8_AUDIO_20260227_152855.wav` (if microphone enabled)

### CSV content

The recorder writes **preprocessed raw EEG** per headband channel:
- Bandpass: 0.3-50 Hz
- Notch: 50 Hz (configurable)
- CAR: per-headband common average reference

Columns:
- `timestamp_unix`
- `t_rel_sec`
- `sample_id`
- `AF7`, `FP1`, `FP2`, `AF8`
- `headband`
- `port`
- `processing`

## Plot recorded CSV files

### Simple static plot

```bash
python plot_preprocessed_csv.py "D:\Cogwear demo\test\test8_HB1_COM3_20260227_152855.csv" "D:\Cogwear demo\test\test8_HB2_COM4_20260227_152855.csv"
```

### Tabbed plot (Raw/Theta/Alpha/Beta)

```bash
python visualize_two_csv.py "D:\Cogwear demo\test\test8_HB1_COM3_20260227_152855.csv" "D:\Cogwear demo\test\test8_HB2_COM4_20260227_152855.csv"
```

## Additional docs

- `CURRENT_SETUP_TUTORIAL.md`: step-by-step usage tutorial
- `RADIO_SETUP.md`: radio channel recommendations
- `SETUP_NOTES.md`: setup summary and notes
