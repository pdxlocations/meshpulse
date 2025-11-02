# MeshPulse

A lightweight Meshtastic packet‑rate monitor for detecting when a radio appears "deaf" (receiving few or no packets).

## Usage

```bash
python3 meshpulse.py
python3 meshpulse.py --window 300 --interval 5
```
Options:
- `--window` — rolling window size in seconds (default 30)
- `--interval` — print/report interval in seconds (default 1)
- `--dead-seconds` — mark DEAF if no packets for this long
- `--csv` — optional CSV log file path
- `--no-plot` — disable live Matplotlib chart

Connection type and BLE address are configured at the top of `meshpulse.py`.

## Requirements

Python 3.10+  
Libraries: `meshtastic`, `matplotlib`

