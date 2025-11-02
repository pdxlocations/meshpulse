# MeshPulse

A lightweight Meshtastic packet‑rate monitor for detecting when a radio appears "deaf" (receiving few or no packets).
<img width="1358" height="521" alt="Screenshot 2025-11-01 at 9 39 38 PM" src="https://github.com/user-attachments/assets/7b8390e2-400e-4885-ae0d-0ebb6d496d4d" />

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

