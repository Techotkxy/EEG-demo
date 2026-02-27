# OpenBCI Radio Channel Setup (Reduce Interference)

Packet errors (`Unexpected END_BYTE`) and cross-talk often indicate **radio channel overlap** or **2.4 GHz interference** (WiFi, Bluetooth).

## Change Radio Channel

**Radio channels cannot be changed from Python** – they require the OpenBCI Hub GUI or Arduino firmware.

### Option 1: OpenBCI Hub (easiest)

1. Download [OpenBCI Hub](https://github.com/OpenBCI/OpenBCI_GUI/releases)
2. Connect each dongle one at a time
3. Go to **Radio Config** or **System Status**
4. Set each headband/dongle pair to a **different channel** (e.g. 11, 12, 13, 14)

### Option 2: Arduino firmware

1. See [Cyton Radios Programming](https://docs.openbci.com/Cyton/CytonRadios/)
2. Edit `radio.begin(OPENBCI_MODE_DEVICE, CHANNEL);` – change `CHANNEL` (default 20)
3. Upload to both **Host** (dongle) and **Device** (board) for each headband
4. Use different channels: e.g. 15, 16, 17, 18 (or 21, 22, 23, 24 to avoid WiFi)

## Suggested channels (4 headbands)

| Headband | Channel | Notes |
|----------|---------|-------|
| HB1 | 15 | |
| HB2 | 16 | |
| HB3 | 17 | |
| HB4 | 18 | |

Or use 21–24 to reduce overlap with WiFi (ch 1–11).

## Other tips

- **Distance**: Keep dongles and headbands away from WiFi routers
- **USB**: Use different USB controllers (e.g. front + back ports) for multiple dongles
- **Power**: Use a powered USB hub if connecting 4 dongles
