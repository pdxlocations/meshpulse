"""
Meshtastic packet-rate monitor

Purpose:
  Track how many packets are received over a rolling time window to help
  detect times when the radio appears "deaf" (i.e., few or no packets).

Usage examples:
  python3 deafness-study.py                           # defaults
  python3 deafness-study.py --window 300 --interval 5 # 5‑min window, report every 5s
  python3 deafness-study.py --csv packets.csv         # also log to CSV
  python3 deafness-study.py --dead-seconds 60         # mark DEAF if no packets >=60s

Notes:
  - Packet content/type is ignored; only arrival is counted.
  - Works with a local SerialInterface.
"""

# -----------------------
# Connection settings
# -----------------------
CONNECTION_TYPE = "ble"  # Options: "ble" or "serial"
BLE_ADDRESS = "x2_ea17"  # Only used if CONNECTION_TYPE == "ble"

# -----------------------
# User configuration defaults
# -----------------------

DEFAULT_WINDOW = 60  # Rolling window in seconds (e.g. 300 = 5 minutes)
DEFAULT_INTERVAL = 10  # How often to print/report stats (seconds)
DEFAULT_DEAD_SECONDS = 30  # Consider radio deaf if no packets for this long
DEFAULT_CSV_PATH = None  # Optional CSV file path (e.g. 'packets.csv')
DEFAULT_VERBOSE = False  # Print every received packet if True
DEFAULT_NO_PLOT = False  # Disable live Matplotlib chart if True
DEFAULT_CHART_WINDOW = 1800  # Seconds the chart should display (30 minutes)

import argparse
import csv
import queue
import signal
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.animation import FuncAnimation

try:
    plt.switch_backend("MacOSX")
except Exception:
    try:
        plt.switch_backend("TkAgg")
    except Exception:
        pass

from pubsub import pub
import meshtastic.serial_interface
import meshtastic.ble_interface

# Helpers


def id_to_hex(node_id: int | None) -> str:
    if node_id is None:
        return "!unknown"
    try:
        return "!" + hex(int(node_id))[2:]
    except Exception:
        return str(node_id)


# -----------------------
# Arguments
# -----------------------


def parse_args():
    p = argparse.ArgumentParser(description="Meshtastic packet-rate monitor")
    p.add_argument(
        "--window",
        type=int,
        default=DEFAULT_WINDOW,
        help="Rolling window in seconds for rate calculation (default: 300)",
    )
    p.add_argument(
        "--interval", type=int, default=DEFAULT_INTERVAL, help="How often to print stats, in seconds (default: 5)"
    )
    p.add_argument(
        "--dead-seconds",
        type=int,
        default=DEFAULT_DEAD_SECONDS,
        help="Mark DEAF if no packets have been seen for this many seconds (default: 60)",
    )
    p.add_argument(
        "--csv",
        type=str,
        default=DEFAULT_CSV_PATH,
        help="Optional path to write CSV logs: time_iso,count_in_window,rate_per_min,last_packet_ago",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        default=DEFAULT_VERBOSE,
        help="Print a line for each received packet",
    )
    p.add_argument(
        "--no-plot", action="store_true", default=DEFAULT_NO_PLOT, help="Do not open the live Matplotlib chart"
    )
    p.add_argument(
        "--chart-window",
        type=int,
        default=DEFAULT_CHART_WINDOW,
        help="Chart history window in seconds (default: 1800 = 30 minutes)",
    )
    return p.parse_args()


# -----------------------
# Core monitor
# -----------------------


class PacketRateMonitor:
    def __init__(
        self,
        window_seconds: int,
        report_interval: int,
        dead_seconds: int,
        csv_path: str | None,
        verbose: bool,
        chart_window_seconds: int,
    ):
        self.window_seconds = window_seconds
        self.report_interval = report_interval
        self.dead_seconds = dead_seconds
        self.csv_path = csv_path
        self.verbose = verbose
        self.chart_window_seconds = chart_window_seconds

        # Deque of packet arrival timestamps (float epoch seconds)
        self.arrivals = deque()
        self.total_packets = 0
        self.last_packet_time: float | None = None
        self._stop = threading.Event()
        self._csv_lock = threading.Lock()
        self._csv_file = None
        self._csv_writer = None

        # Interval-scoped counts for nodes active between reporter updates
        from collections import defaultdict

        self._interval_counts = defaultdict(int)  # node_key -> count since last report

        # History of (timestamp, rate_per_min) for web chart (kept to window size)
        self.rate_history = deque()
        self._hist_lock = threading.Lock()

        # Map of node_id string (e.g., "!abcd") -> deque of arrival timestamps
        self.node_windows: dict[str, deque] = {}

        self.ingest_q = queue.Queue()

        if self.csv_path:
            # open on first write to avoid creating empty files if nothing arrives
            pass

    # PubSub callback from meshtastic
    def on_receive(self, packet, interface):
        # Ignore messages originating from self as early as possible
        try:
            my_id = getattr(interface.myInfo, "my_node_num", None)
            if my_id is not None and packet.get("from") == my_id:
                return
        except Exception:
            pass

        # Extract a minimal tuple and enqueue for the background ingestor thread
        now = time.time()
        raw_from = packet.get("from")
        if raw_from is None:
            raw_from = packet.get("fromId")
        try:
            self.ingest_q.put_nowait((now, raw_from))
        except Exception:
            # As a last resort, drop silently to avoid blocking callback
            return

    def ingest_loop(self):
        """Background thread that processes packets placed on the ingest queue.
        Keeps work out of the PubSub callback to avoid drops due to slow handlers.
        """
        while not self._stop.is_set():
            try:
                now, raw_from = self.ingest_q.get(timeout=0.5)
            except queue.Empty:
                continue

            # Update rolling global arrivals window
            self.arrivals.append(now)
            self.total_packets += 1
            self.last_packet_time = now

            # Determine node key and per-node windows
            node_key = id_to_hex(raw_from) if isinstance(raw_from, (int, str)) else "!unknown"
            if node_key not in self.node_windows:
                self.node_windows[node_key] = deque()
            self.node_windows[node_key].append(now)

            # Count for this reporting interval
            self._interval_counts[node_key] += 1

            # Optional per-packet print (keep tiny to avoid blocking)
            if self.verbose:
                try:
                    # use sys.stdout.write to avoid print() overhead/locks
                    sys.stdout.write(f"[{datetime.now(timezone.utc).isoformat()}] packet\n")
                except Exception:
                    pass

    def prune_old(self, now: float):
        cutoff = now - self.window_seconds
        dq = self.arrivals
        while dq and dq[0] < cutoff:
            dq.popleft()
        # Prune per-node windows
        for node in list(self.node_windows.keys()):
            ndq = self.node_windows[node]
            while ndq and ndq[0] < cutoff:
                ndq.popleft()
            if not ndq:
                del self.node_windows[node]

    def compute_stats(self):
        now = time.time()
        self.prune_old(now)
        count = len(self.arrivals)
        rate_per_min = (count / self.window_seconds) * 60.0 if self.window_seconds > 0 else 0.0
        last_ago = (now - self.last_packet_time) if self.last_packet_time else float("inf")
        return now, count, rate_per_min, last_ago

    def ensure_csv(self):
        if self.csv_path and self._csv_writer is None:
            self._csv_file = open(self.csv_path, "a", newline="", encoding="utf-8")
            self._csv_writer = csv.writer(self._csv_file)
            # Write header only if file is empty
            try:
                if self._csv_file.tell() == 0:
                    self._csv_writer.writerow(["time_iso", "count_in_window", "rate_per_min", "last_packet_ago"])
                    self._csv_file.flush()
            except Exception:
                pass

    def log_csv(self, ts_iso: str, count: int, rate_per_min: float, last_ago: float):
        if not self.csv_path:
            return
        self.ensure_csv()
        with self._csv_lock:
            self._csv_writer.writerow([ts_iso, count, f"{rate_per_min:.3f}", f"{last_ago:.1f}"])
            self._csv_file.flush()

    def get_history_points(self):
        """Return a list of {"t": iso8601, "v": rate_per_min} for the last window."""
        with self._hist_lock:
            points = [
                {"t": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(), "v": r} for ts, r in self.rate_history
            ]
        return points

    def get_history_points_ts(self):
        """Return list of (ts_float, rate_per_min)."""
        with self._hist_lock:
            return list(self.rate_history)

    def reporter_loop(self):
        while not self._stop.is_set():
            now, count, rate_per_min, last_ago = self.compute_stats()
            ts_iso = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()

            status = "OK"
            if last_ago >= self.dead_seconds:
                status = "DEAF"

            # Record history for the web chart (keep last window seconds)
            with self._hist_lock:
                self.rate_history.append((now, rate_per_min))
                cutoff_hist = now - self.chart_window_seconds
                while self.rate_history and self.rate_history[0][0] < cutoff_hist:
                    self.rate_history.popleft()

            # Simple text bar for rate visualization (scaled to window)
            # Draw up to 50 chars based on rate per minute (cap to avoid overflows)
            bar_len = min(int(rate_per_min), 50)
            bar = "#" * bar_len

            print(
                f"[{ts_iso}] window={self.window_seconds}s count={count} rate/min={rate_per_min:.2f} last_packet_ago={last_ago:.0f}s {status} {bar}"
            )

            # Print ONLY nodes that were active since last report (interval counts)
            interval_counts = dict(self._interval_counts)
            self._interval_counts.clear()
            if interval_counts:
                summary = ", ".join(f"{node[-4:]}({cnt})" for node, cnt in sorted(interval_counts.items()))
                print(f"Active nodes: {summary}")
            else:
                print("Active nodes: None")

            self.log_csv(ts_iso, count, rate_per_min, last_ago)

            self._stop.wait(self.report_interval)

    def start(self):
        # Subscribe to receive notifications
        pub.subscribe(self.on_receive, "meshtastic.receive")

        # Start reporter and ingestor threads
        t = threading.Thread(target=self.reporter_loop, name="reporter", daemon=True)
        t.start()
        self._reporter_thread = t

        ti = threading.Thread(target=self.ingest_loop, name="ingest", daemon=True)
        ti.start()
        self._ingest_thread = ti
        return t

    def stop(self):
        self._stop.set()
        try:
            if self._csv_file:
                self._csv_file.flush()
                self._csv_file.close()
        except Exception:
            pass


def run_matplotlib_chart(monitor: PacketRateMonitor):
    fig, ax = plt.subplots()
    (line,) = ax.plot([], [], linewidth=1.5)
    ax.set_xlabel("Time")
    ax.set_ylabel("Packets / minute")
    ax.grid(True, which="both", linestyle=":")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))

    def refresh(_frame=None):
        pts = monitor.get_history_points_ts()
        if not pts:
            return
        xs = [datetime.fromtimestamp(t, tz=timezone.utc) for t, _ in pts]
        ys = [v for _, v in pts]
        line.set_data(xs, ys)
        # Keep last window in view
        xmax = xs[-1]
        xmin = xmax - timedelta(seconds=monitor.chart_window_seconds)
        ax.set_xlim(xmin, xmax)
        # Y autoscale with a little headroom
        ymin = 0
        ymax = max(1.0, max(ys) * 1.2)
        ax.set_ylim(ymin, ymax)
        fig.autofmt_xdate()
        fig.canvas.draw_idle()

    # Tie animation interval to reporter interval
    interval_ms = max(200, int(monitor.report_interval * 1000))
    from datetime import timedelta

    anim = FuncAnimation(fig, refresh, interval=interval_ms)
    # Keep references to avoid garbage collection
    monitor._fig = fig
    monitor._ax = ax
    monitor._line = line
    monitor._anim = anim
    plt.show(block=False)
    return anim


# -----------------------
# Main
# -----------------------


def main():
    args = parse_args()

    if CONNECTION_TYPE == "ble":
        print(f"Connecting to Meshtastic via BLE ({BLE_ADDRESS})…")
        interface = meshtastic.ble_interface.BLEInterface(address=BLE_ADDRESS)
    else:
        print("Connecting to Meshtastic via SerialInterface…")
        interface = meshtastic.serial_interface.SerialInterface()

    monitor = PacketRateMonitor(
        window_seconds=args.window,
        report_interval=args.interval,
        dead_seconds=args.dead_seconds,
        csv_path=args.csv,
        verbose=args.verbose,
        chart_window_seconds=args.chart_window,
    )

    have_plot = False
    if not args.no_plot:
        monitor._anim_ref = run_matplotlib_chart(monitor)
        have_plot = True

    reporter_thread = monitor.start()

    def _graceful_exit(signum, frame):  # noqa: ARG001
        print("\nStopping…")
        monitor.stop()
        # allow reporter thread to exit cleanly
        time.sleep(0.1)
        sys.exit(0)

    signal.signal(signal.SIGINT, _graceful_exit)
    signal.signal(signal.SIGTERM, _graceful_exit)

    # Keep the main thread alive while the reporter runs
    try:
        while True:
            if have_plot:
                # Allow Matplotlib GUI to process events
                plt.pause(0.01)
                time.sleep(0.1)
            else:
                time.sleep(1)
    except KeyboardInterrupt:
        _graceful_exit(None, None)


if __name__ == "__main__":
    main()
