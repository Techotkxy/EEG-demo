# OpenBCI_LSL Setup Notes

OpenBCI_LSL has been set up in this folder. The following was done:

## What's Installed

- **Repository**: Cloned from [openbci-archive/OpenBCI_LSL](https://github.com/openbci-archive/OpenBCI_LSL)
- **Dependencies**: Updated `requirements.txt` for modern Python 3 compatibility and installed:
  - numpy, pylsl, pyqtgraph, scipy, pyserial

## Usage

### Command Line Interface (recommended)

1. Plug in your OpenBCI dongle and power on the board.
2. Run:
   ```bash
   python openbci_lsl.py --stream
   ```
3. Use `/start` to begin streaming, `/stop` to pause, `/exit` to disconnect.

**Windows serial port**: If auto-detection fails, specify the port:
```bash
python openbci_lsl.py COM3 --stream
```
(Replace `COM3` with your actual COM port.)

### GUI

The GUI requires **PyQt4**, which is deprecated and may be hard to install on newer systems. Options:

- Install PyQt4 if available for your platform (e.g. [conda install pyqt](https://anaconda.org/anaconda/pyqt4))
- Or use the CLI (`--stream` mode) for streaming

## Hardware

- Ensure the OpenBCI dongle is plugged in and the board is powered on before running.
- If you see "Cannot find OpenBCI port", this usually means no board is detected; restart the program and board.

## Reference

See [readme.md](readme.md) for full documentation, board configuration, and troubleshooting.
