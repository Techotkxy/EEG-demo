# Current Setup Tutorial (2-Headband Recording + CSV)

This tutorial is for the current Cogwear setup using two OpenBCI headbands and the latest recorder GUI.

## 1) Prerequisites

- Python 3.8+
- Two OpenBCI dongles + two powered headbands
- Dependencies installed:

```bash
pip install -r requirements.txt
```

## 2) Hardware setup

1. Plug in both dongles.
2. Power on both headbands.
3. Set different radio channels for each headband (see `RADIO_SETUP.md`).
4. Identify COM ports in Device Manager (Windows).

## 3) Launch the recording GUI

```bash
python dual_headband_recorder_gui.py COM3 COM4
```

If ports are different, replace `COM3` and `COM4`.

## 4) Verify dongle-port mapping

In the GUI:
- Click **Show Dongle-Port Mapping**.
- Confirm:
  - HB1 -> expected COM
  - HB2 -> expected COM
- The mapping shows COM, serial number, manufacturer, and description.

## 5) Start recording

1. Click **Start Recording**.
2. Enter recording name.
3. Choose output folder.
4. Recording starts for both headbands simultaneously.

You will see live metrics:
- elapsed time
- sample counters (HB1/HB2)
- packet loss per headband

## 6) Stop recording

1. Click **Stop Recording**.
2. GUI state changes to **Recording: OFF**.
3. Recording files are finalized and closed.
4. A popup confirms completion and shows audio path (if microphone recording is enabled).

## 7) Output files

For each session, files are written separately for each headband:

- CSV:
  - `<name>_HB1_COMx_<timestamp>.csv`
  - `<name>_HB2_COMy_<timestamp>.csv`
- Optional microphone:
  - `<name>_AUDIO_<timestamp>.wav`

CSV signals are already **preprocessed raw EEG**:
- bandpass (0.3-50 Hz)
- notch (50/60 Hz, depending config)
- CAR per headband

## 8) Visualize two CSV files

```bash
python visualize_two_csv.py "D:\path\file_HB1.csv" "D:\path\file_HB2.csv"
```

This opens one GUI with:
- Raw
- Theta
- Alpha
- Beta

for all 8 channels (`HB1_*` + `HB2_*`).

Alternative simple plotting:

```bash
python plot_preprocessed_csv.py "D:\path\file_HB1.csv" "D:\path\file_HB2.csv"
```

## 9) Troubleshooting

- **Port busy / cannot connect**
  - Close other scripts using the same COM.
  - Replug dongle and retry.
- **High packet loss**
  - Use different radio channels.
  - Separate dongles physically.
  - Try different USB ports or powered hub.
- **Unexpected noise**
  - Check electrode contact and impedance.
  - Keep away from strong power-line interference.
