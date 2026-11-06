# -*- coding: utf-8 -*-
"""
Test23_gui.py  --  YeLink Test 2/3: command-to-pin latency and output behaviour (GUI)

Open in Spyder and press F5, or run:  python Test23_gui.py

Hardware
  YeLink_AdapterUI connected to the Arduino Uno (YeLink firmware), pins 2-5 under test.
  Teensy 4.0 running YeLink_EdgeLogger_Teensy.ino. Each Uno pin goes through a divider
  (Uno pin -[10k]- Teensy pin -[20k]- GND), grounds shared. Teensy 4.0 pins are not 5 V tolerant.

Tab 1 "Run test"
  Choose the Teensy COM port (Test Teensy shows the current pin levels), the adapter host/port,
  the command CSV from make_test23_commands.py (or generate one), the output folder, and press Run.
  The timeline plot (pin levels recorded by the Teensy = outputs, commands sent = inputs) is drawn
  after the run with the "Plot full I/O timeline" button, and has a toolbar for zooming and saving.
  A live version during the run is available via the checkbox but is off by default.
  At the end the GUI asks you to close the YeLink_AdapterUI window and press a button, to verify
  that all outputs go low on close.

Tab 2 "Analyze"
  Load a results folder (and optionally the adapter log CSV exported from YeLink_AdapterUI)
  to see the summary and the four-panel figure; save the figure as PNG.

Timing design
  All edge times come from the Teensy's micros(), synchronised to the PC clock with repeated
  round-trip queries (drift-corrected fit). The client-side send stamp is taken immediately before
  connect(). The live plot is throttled and cannot affect Teensy or adapter timestamps; it can at
  most add ~1 ms of jitter to the client-side send stamp. Latency computed from the adapter's own
  receipt stamp (Tab 2, with the adapter log) is independent of the GUI entirely.

Requirements: Python 3.8+, pyserial, numpy, matplotlib (all in Anaconda).
"""
import csv
import json
import os
import queue
import socket
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime

import numpy as np
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None

sys.setswitchinterval(0.001)     # let the sender thread regain the GIL quickly during plot updates

PINS = [2, 3, 4, 5]
LAT_WINDOW = 0.25        # s after a command in which its edge must appear
FALL_SLACK = 0.25        # s after the nominal end of a timed pulse in which the fall must appear
PIN_COLORS = {2: "#1f77b4", 3: "#2ca02c", 4: "#ff7f0e", 5: "#9467bd"}
CAT_COLORS = {"timed": "#1f77b4", "latch_on": "#9467bd", "latch_off": "#9467bd", "cancel_on": "#d62728",
              "cancel_off": "#d62728", "batch": "#2ca02c", "malformed": "#7f7f7f", "final_latch": "#8c564b"}


# ----------------------------------------------------------------------
# TCP client (same behaviour as Test1.py; send stamp taken right before connect)
# ----------------------------------------------------------------------

def send_one(host, port, command, timeout_s):
    res = {"connected": False, "reply": "", "connect_ms": "", "reply_ms": "", "error": "",
           "send_perf": None, "send_wall": None}
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    res["send_wall"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    t0 = time.perf_counter()
    res["send_perf"] = t0
    try:
        sock.connect((host, port))
    except OSError as e:
        res["error"] = str(e)
        sock.close()
        return res
    try:
        res["connected"] = True
        res["connect_ms"] = f"{(time.perf_counter() - t0) * 1000:.3f}"
        sock.sendall((command + "\n").encode("ascii", errors="replace"))
        sock.settimeout(timeout_s)
        data = b""
        while True:
            try:
                chunk = sock.recv(256)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
        res["reply"] = data.decode("ascii", errors="replace").strip()
        res["reply_ms"] = f"{(time.perf_counter() - t0) * 1000:.3f}"
    except OSError as e:
        res["error"] = str(e)
    finally:
        sock.close()
    return res


# ----------------------------------------------------------------------
# Teensy link
# ----------------------------------------------------------------------

class Teensy:
    def __init__(self, port):
        if serial is None:
            raise RuntimeError("pyserial is not installed (conda install pyserial).")
        self.ser = serial.Serial(port, 115200, timeout=0.05)
        self.edges = []          # (teensy_us, pin, state, pc_perf_at_receipt)
        self.sync_q = queue.Queue()
        self.other = []
        self.levels = {}
        self.levels_evt = threading.Event()
        self.running = True
        self.ready = threading.Event()
        self.th = threading.Thread(target=self._reader, daemon=True)
        self.th.start()

    def _reader(self):
        buf = b""
        while self.running:
            try:
                n = self.ser.in_waiting
                chunk = self.ser.read(n if n else 1)
            except Exception:
                break
            if not chunk:
                continue
            t_rx = time.perf_counter()
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                s = line.decode("ascii", errors="replace").strip()
                if not s:
                    continue
                f = s.split(",")
                try:
                    if f[0] == "E" and len(f) >= 4:
                        self.edges.append((int(f[1]), int(f[2]), int(f[3]), t_rx))
                    elif f[0] == "S" and len(f) >= 2:
                        self.sync_q.put((int(f[1]), t_rx))
                    elif f[0] == "P" and len(f) >= 4:
                        self.levels[int(f[2])] = int(f[3])
                        self.levels_evt.set()
                    elif f[0] == "READY":
                        self.ready.set()
                        self.other.append(s)
                    else:
                        self.other.append(s)
                except ValueError:
                    self.other.append(s)

    def query_levels(self, wait=0.3):
        self.levels = {}
        self.levels_evt.clear()
        self.ser.write(b"Q")
        self.levels_evt.wait(wait)
        time.sleep(0.05)
        return dict(self.levels)

    def sync(self, n=5, pause=0.01):
        out = []
        for _ in range(n):
            while not self.sync_q.empty():
                self.sync_q.get_nowait()
            t0 = time.perf_counter()
            self.ser.write(b"T")
            try:
                us, t_rx = self.sync_q.get(timeout=0.5)
            except queue.Empty:
                continue
            out.append(((t0 + t_rx) / 2.0, us, t_rx - t0))
            time.sleep(pause)
        return out

    def close(self):
        self.running = False
        time.sleep(0.1)
        try:
            self.ser.close()
        except Exception:
            pass


def unwrap(us, base):
    d = (us - base) & 0xFFFFFFFF
    if d > 0x7FFFFFFF:
        d -= 0x100000000
    return base + d


def fit_clock(samples):
    """samples: (pc_mid_perf_s, teensy_us, rtt_s) -> teensy_us = a + b*pc_perf_s (low-RTT samples only)."""
    if len(samples) < 2:
        raise RuntimeError("Not enough clock-sync samples from the Teensy.")
    rtts = [s[2] for s in samples]
    cutoff = statistics.median(rtts) * 1.5
    good = [s for s in samples if s[2] <= cutoff] or samples
    base = good[0][1]
    pcs = np.array([s[0] for s in good])
    uss = np.array([unwrap(s[1], base) for s in good], dtype=float)
    b, a = np.polyfit(pcs, uss, 1)
    resid = uss - (a + b * pcs)
    return {"a": float(a), "b": float(b), "base_us": int(base), "n": len(good),
            "rtt_median_ms": float(statistics.median(rtts) * 1000),
            "resid_rms_us": float(np.sqrt(np.mean(resid ** 2))),
            "drift_ppm": float(b - 1e6)}


def teensy_to_perf(us, clk):
    return (unwrap(us, clk["base_us"]) - clk["a"]) / clk["b"]


def wall_perf_offset(n=50):
    offs = []
    for _ in range(n):
        p = time.perf_counter()
        w = datetime.now().timestamp()
        offs.append(w - p)
        time.sleep(0.002)
    return statistics.median(offs)


# ----------------------------------------------------------------------
# Analysis
# ----------------------------------------------------------------------

def parse_parts(command):
    out = []
    for part in command.split(";"):
        part = part.strip()
        if not part:
            continue
        pin, v, d = None, 0, 0.0
        for tok in part.replace(",", " ").split():
            if len(tok) >= 2 and tok[1] == "=":
                k, val = tok[0], tok[2:]
                try:
                    if k == "p":
                        pin = int(val)
                    elif k == "v":
                        v = 1 if int(val) != 0 else 0
                    elif k == "d":
                        d = float(val)
                except ValueError:
                    pass
        if pin is None or pin < 2 or pin > 13:
            pin = None
        out.append((pin, v, d))
    return out


def stat(name, vals, unit="ms"):
    a = np.asarray([v for v in vals if v is not None and np.isfinite(v)], dtype=float)
    if a.size == 0:
        return f"{name}: no data"
    return (f"{name}: n={a.size}, median {np.median(a):.3f}, p95 {np.percentile(a, 95):.3f}, "
            f"min {a.min():.3f}, max {a.max():.3f} {unit}")


def load_adapter_log(path):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["Source"].strip().upper() != "TCP":
                continue
            try:
                ts = datetime.strptime(r["Time"].strip(), "%Y-%m-%d %H:%M:%S.%f").timestamp()
            except ValueError:
                continue
            rows.append({"ts": ts, "received": r["Received"].strip()})
    return rows


def analyze_folder(folder, adapter_log=None):
    """Return dict with 'text' and the data series for plotting."""
    with open(os.path.join(folder, "run.csv"), newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    with open(os.path.join(folder, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    edges = []
    with open(os.path.join(folder, "edges.csv"), newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            edges.append({"t": float(r["pc_perf_s"]), "pin": int(r["pin"]), "state": int(r["state"]), "used": None})
    edges.sort(key=lambda e: e["t"])
    by_pin = {p: [e for e in edges if e["pin"] == p] for p in PINS}
    w_off = meta["wall_minus_perf_s"]
    adapter = load_adapter_log(adapter_log) if adapter_log else None
    ad_idx = 0

    def take(pin, state, t_from, t_to, tag):
        for e in by_pin.get(pin, []):
            if e["used"] is None and e["state"] == state and t_from <= e["t"] <= t_to:
                e["used"] = tag
                return e
        return None

    lat_on, lat_off, lat_on_adapter = [], [], []
    dur_err = {}
    batch_spread, batch_order_ok = [], 0
    cancel_results, cancel_extra_fall = [], 0
    latch_results = []
    malformed_sends, malformed_edges = [], 0
    missing = []
    pending_rise = {}
    final_pins = []

    for r in rows:
        if str(r["connected"]).lower() != "true":
            missing.append((r["index"], r["command"], "not connected"))
            continue
        send = float(r["send_perf"])
        cat = r["category"]
        parts = parse_parts(r["command"])
        ad_times = []
        if adapter is not None:
            for _ in parts:
                if ad_idx < len(adapter):
                    ad_times.append(adapter[ad_idx]["ts"] - w_off)
                    ad_idx += 1
        if cat == "malformed":
            malformed_sends.append(send)
            continue
        rises = []
        for k, (pin, v, d) in enumerate(parts):
            if pin is None:
                continue
            tag = f"{r['index']}:{k}"
            if v == 1:
                rise = take(pin, 1, send - 0.002, send + LAT_WINDOW, tag)
                if rise is None:
                    missing.append((r["index"], r["command"], f"no rising edge on pin {pin}"))
                    continue
                lat_on.append((rise["t"] - send) * 1000)
                if ad_times and k < len(ad_times):
                    lat_on_adapter.append((rise["t"] - ad_times[k]) * 1000)
                rises.append(rise["t"])
                if cat in ("timed", "batch"):
                    fall = take(pin, 0, rise["t"] + d * 0.5, rise["t"] + d + FALL_SLACK, tag)
                    if fall is None:
                        missing.append((r["index"], r["command"], f"no falling edge on pin {pin}"))
                    else:
                        dur_err.setdefault(d, []).append(((fall["t"] - rise["t"]) - d) * 1000)
                elif cat in ("latch_on", "cancel_on"):
                    pending_rise[r["group"]] = (rise["t"], send, pin, d)
                elif cat == "final_latch":
                    final_pins.append(pin)
            else:
                fall = take(pin, 0, send - 0.002, send + LAT_WINDOW, tag)
                if fall is None:
                    missing.append((r["index"], r["command"], f"no falling edge on pin {pin}"))
                    continue
                lat_off.append((fall["t"] - send) * 1000)
                if r["group"] in pending_rise:
                    rise_t, send_on, pin_on, d_on = pending_rise.pop(r["group"])
                    commanded = send - send_on
                    measured = fall["t"] - rise_t
                    if cat == "cancel_off":
                        cancel_results.append((commanded, measured))
                        extra = take(pin, 0, rise_t + d_on - 0.3, rise_t + d_on + 0.3, "cancel-extra")
                        if extra is not None:
                            cancel_extra_fall += 1
                    else:
                        latch_results.append((commanded, measured))
        if cat == "batch" and len(rises) >= 2:
            batch_spread.append((max(rises) - min(rises)) * 1000)
            if rises == sorted(rises):
                batch_order_ok += 1

    close_mark = meta.get("close_mark_perf")
    close_falls = {}
    if close_mark is not None:
        for p in final_pins:
            e = take(p, 0, close_mark - 30.0, close_mark + 25.0, "close")
            close_falls[p] = None if e is None else (e["t"] - close_mark) * 1000
    unexplained = [e for e in edges if e["used"] is None]
    for send in malformed_sends:
        malformed_edges += sum(1 for e in unexplained if send <= e["t"] <= send + 1.0)

    n_batch = sum(1 for r in rows if r["category"] == "batch")
    clk = meta["clock"]
    lines = [
        f"YeLink Test 2/3 - run {meta['label']}",
        f"Commands: {len(rows)}   connected: {sum(1 for r in rows if str(r['connected']).lower() == 'true')}",
        f"Edges recorded: {len(edges)}   unexplained edges: {len(unexplained)}",
        f"Clock sync: {clk['n']} samples, RTT median {clk['rtt_median_ms']:.2f} ms, "
        f"fit residual RMS {clk['resid_rms_us']:.0f} us, drift {clk['drift_ppm']:.1f} ppm",
        "",
        "Latency (command sent by client -> pin edge on logger)",
        "  " + stat("ON  (rising edge)", lat_on),
        "  " + stat("OFF (falling edge)", lat_off),
    ]
    if adapter is not None:
        lines.append("  " + stat("ON, measured from adapter receipt stamp", lat_on_adapter))
    else:
        lines.append("  (load the adapter log CSV to also get latency from the adapter's receipt stamp)")
    lines += ["", "Duration accuracy of timed pulses (measured high time minus commanded d)"]
    for d in sorted(dur_err):
        lines.append("  " + stat(f"d = {d:g} s", dur_err[d]))
    lines += ["", "Latch on/off (measured high time minus commanded hold)"]
    if latch_results:
        lines.append("  " + stat(f"n={len(latch_results)}", [(m - c) * 1000 for c, m in latch_results]))
    lines += ["", "Cancel (v=0 sent during a 5 s pulse)"]
    if cancel_results:
        lines.append("  " + stat(f"measured - commanded high time, n={len(cancel_results)}",
                                 [(m - c) * 1000 for c, m in cancel_results]))
        lines.append(f"  late falling edges from a surviving timer: {cancel_extra_fall} of {len(cancel_results)}")
    lines += ["", f"Batches: {n_batch} sent; parts switched in command order in {batch_order_ok}",
              "  " + stat("spread between first and last rising edge", batch_spread),
              "", f"Malformed commands: {len(malformed_sends)} sent; "
                  f"edges on any monitored pin within 1 s: {malformed_edges}",
              "", "All-off on closing the adapter window:"]
    for p in PINS:
        if p in close_falls:
            v = close_falls[p]
            lines.append(f"  pin {p}: " + ("NO falling edge" if v is None else f"fell {v:+.0f} ms relative to the button press"))
    lines.append(f"  pin levels after close (Teensy): {meta.get('levels_after_close')}")
    if missing:
        lines += ["", f"Commands with a missing expected edge: {len(missing)}"]
        for m in missing[:30]:
            lines.append(f"  index {m[0]}: {m[1]}  -> {m[2]}")
    if unexplained:
        lines += ["", f"Unexplained edges (first 20 of {len(unexplained)}):"]
        for e in unexplained[:20]:
            lines.append(f"  pin {e['pin']} state {e['state']} at perf {e['t']:.3f}")
    return {"text": "\n".join(lines), "lat_on": lat_on, "lat_off": lat_off, "lat_on_adapter": lat_on_adapter,
            "dur_err": dur_err, "batch_spread": batch_spread, "latch": latch_results, "cancel": cancel_results}


def plot_results(fig, res):
    fig.clf()
    ax = fig.subplots(2, 2)
    a = ax[0, 0]
    if res["lat_on"]:
        a.hist(res["lat_on"], bins=40, color="#1f77b4", label=f"ON from client send, n={len(res['lat_on'])}")
    if res["lat_off"]:
        a.hist(res["lat_off"], bins=40, color="#ff7f0e", alpha=0.7, label=f"OFF from client send, n={len(res['lat_off'])}")
    if res["lat_on_adapter"]:
        a.hist(res["lat_on_adapter"], bins=40, color="#2ca02c", alpha=0.6,
               label=f"ON from adapter receipt, n={len(res['lat_on_adapter'])}")
    a.set_title("Command-to-pin latency")
    a.set_xlabel("ms")
    a.set_ylabel("count")
    a.legend(fontsize=7)
    a = ax[0, 1]
    ds = sorted(res["dur_err"])
    if ds:
        a.boxplot([res["dur_err"][d] for d in ds], showfliers=False)
        a.set_xticks(range(1, len(ds) + 1))
        a.set_xticklabels([f"{d:g}" for d in ds])
        rng = np.random.default_rng(0)
        for i, d in enumerate(ds):
            v = res["dur_err"][d]
            a.scatter(np.full(len(v), i + 1) + rng.uniform(-0.08, 0.08, len(v)), v, s=8, color="#1f77b4", alpha=0.6)
    a.axhline(0, color="k", lw=0.8)
    a.set_title("Pulse duration error (measured - commanded)")
    a.set_xlabel("commanded d (s)")
    a.set_ylabel("ms")
    a = ax[1, 0]
    if res["batch_spread"]:
        a.bar(range(1, len(res["batch_spread"]) + 1), res["batch_spread"], color="#2ca02c")
    a.set_title("Batch: spread of rising edges across pins")
    a.set_xlabel("batch")
    a.set_ylabel("ms")
    a = ax[1, 1]
    if res["latch"]:
        a.scatter([c for c, m in res["latch"]], [(m - c) * 1000 for c, m in res["latch"]],
                  s=14, color="#9467bd", label=f"latch on/off, n={len(res['latch'])}")
    if res["cancel"]:
        a.scatter([c for c, m in res["cancel"]], [(m - c) * 1000 for c, m in res["cancel"]],
                  s=14, color="#d62728", label=f"cancel, n={len(res['cancel'])}")
    a.axhline(0, color="k", lw=0.8)
    if res["latch"] or res["cancel"]:
        a.legend(fontsize=8)
    a.set_title("Latch and cancel: high-time error")
    a.set_xlabel("commanded high time (s)")
    a.set_ylabel("measured - commanded (ms)")
    fig.tight_layout()


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("YeLink Test 2/3 - Latency and output behaviour")
        self.geometry("1300x860")
        self.q = queue.Queue()
        self.stop_evt = threading.Event()
        self.close_evt = threading.Event()
        self.close_mark = None
        self.worker = None
        self.tz = None
        self.live_clock = None          # simple (offset_us) for the live plot only
        self.sent = []                  # (perf, category, command) for the live plot
        self.run_t0 = None

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)
        self.tab_run = ttk.Frame(nb)
        self.tab_an = ttk.Frame(nb)
        nb.add(self.tab_run, text="1. Run test")
        nb.add(self.tab_an, text="2. Analyze")
        self._build_run_tab()
        self._build_an_tab()
        self.after(100, self._poll_queue)
        self.after(500, self._update_live)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- Tab 1 ----------------
    def _build_run_tab(self):
        f = self.tab_run
        hw = ttk.LabelFrame(f, text="Connections")
        hw.pack(fill="x", padx=10, pady=6)
        ttk.Label(hw, text="Teensy COM").grid(row=0, column=0, padx=6, pady=6, sticky="e")
        self.var_com = tk.StringVar()
        self.cbo_com = ttk.Combobox(hw, textvariable=self.var_com, width=28, state="readonly")
        self.cbo_com.grid(row=0, column=1, sticky="w")
        ttk.Button(hw, text="Refresh", command=self.refresh_ports).grid(row=0, column=2, padx=4)
        ttk.Button(hw, text="Test Teensy", command=self.test_teensy).grid(row=0, column=3, padx=4)
        self.lbl_teensy = ttk.Label(hw, text="not tested", foreground="gray")
        self.lbl_teensy.grid(row=0, column=4, padx=6, sticky="w")

        ttk.Label(hw, text="Adapter host").grid(row=1, column=0, padx=6, pady=4, sticky="e")
        self.var_host = tk.StringVar(value="127.0.0.1")
        self.var_port = tk.StringVar(value="13000")
        self.var_timeout = tk.StringVar(value="1.0")
        hp = ttk.Frame(hw)
        hp.grid(row=1, column=1, columnspan=3, sticky="w")
        ttk.Entry(hp, textvariable=self.var_host, width=14).pack(side="left")
        ttk.Label(hp, text="port").pack(side="left", padx=(8, 2))
        ttk.Entry(hp, textvariable=self.var_port, width=7).pack(side="left")
        ttk.Label(hp, text="timeout (s)").pack(side="left", padx=(8, 2))
        ttk.Entry(hp, textvariable=self.var_timeout, width=5).pack(side="left")
        ttk.Button(hp, text="Test adapter", command=self.test_adapter).pack(side="left", padx=8)
        self.lbl_adapter = ttk.Label(hw, text="not tested", foreground="gray")
        self.lbl_adapter.grid(row=1, column=4, padx=6, sticky="w")

        st = ttk.LabelFrame(f, text="Test settings")
        st.pack(fill="x", padx=10, pady=6)
        self.var_cmd = tk.StringVar()
        self.var_out = tk.StringVar(value=os.path.join(os.getcwd(), "results_test23"))
        self.var_seed = tk.StringVar(value="1")
        self.var_live = tk.BooleanVar(value=False)
        ttk.Label(st, text="Command CSV").grid(row=0, column=0, padx=6, pady=4, sticky="e")
        ttk.Entry(st, textvariable=self.var_cmd, width=80).grid(row=0, column=1, columnspan=3, sticky="we")
        ttk.Button(st, text="Browse...", command=lambda: self._browse_file(self.var_cmd)).grid(row=0, column=4, padx=4)
        gen = ttk.Frame(st)
        gen.grid(row=0, column=5, padx=4)
        ttk.Button(gen, text="Generate (seed)", command=self.generate_commands).pack(side="left")
        ttk.Entry(gen, textvariable=self.var_seed, width=4).pack(side="left", padx=2)
        ttk.Label(st, text="Output folder").grid(row=1, column=0, padx=6, pady=4, sticky="e")
        ttk.Entry(st, textvariable=self.var_out, width=80).grid(row=1, column=1, columnspan=3, sticky="we")
        ttk.Button(st, text="Browse...", command=self._browse_out).grid(row=1, column=4, padx=4)
        ttk.Checkbutton(st, text="Live plot during run (off = cleanest timing)", variable=self.var_live).grid(row=1, column=5, padx=4)
        st.columnconfigure(1, weight=1)

        bar = ttk.Frame(f)
        bar.pack(fill="x", padx=10, pady=4)
        self.btn_run = ttk.Button(bar, text="Run", command=self.start_run)
        self.btn_run.pack(side="left", padx=4)
        self.btn_stop = ttk.Button(bar, text="Stop", command=self.stop_run, state="disabled")
        self.btn_stop.pack(side="left", padx=4)
        self.btn_closed = ttk.Button(bar, text="I have closed the adapter window", command=self.adapter_closed,
                                     state="disabled")
        self.btn_closed.pack(side="left", padx=16)
        self.lbl_progress = ttk.Label(bar, text="idle")
        self.lbl_progress.pack(side="left", padx=12)
        self.pb = ttk.Progressbar(f, maximum=100)
        self.pb.pack(fill="x", padx=10, pady=2)

        body = ttk.Frame(f)
        body.pack(fill="both", expand=True, padx=10, pady=6)
        left = ttk.Frame(body)
        left.pack(side="left", fill="y")
        self.txt_log = tk.Text(left, width=62, wrap="word", font=("Consolas", 9))
        sb = ttk.Scrollbar(left, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=sb.set)
        self.txt_log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))
        pbar = ttk.Frame(right)
        pbar.pack(side="top", fill="x")
        ttk.Button(pbar, text="Plot full I/O timeline (after the run)", command=self.plot_timeline).pack(side="left", padx=4)
        ttk.Label(pbar, text="Use the toolbar below the plot to zoom and save.", foreground="gray").pack(side="left", padx=8)
        self.fig_live = Figure(figsize=(7, 5), dpi=100)
        self.ax_live = self.fig_live.add_subplot(111)
        self.canvas_live = FigureCanvasTkAgg(self.fig_live, master=right)
        tb_live = NavigationToolbar2Tk(self.canvas_live, right)
        tb_live.update()
        tb_live.pack(side="top", fill="x")
        self.canvas_live.get_tk_widget().pack(fill="both", expand=True)
        self._init_live_axes()
        self.refresh_ports()

    def _init_live_axes(self):
        a = self.ax_live
        a.clear()
        self.fig_live.suptitle("")
        a.set_title("Live: pin levels from the Teensy (outputs) and commands sent (inputs)")
        a.set_xlabel("seconds since run start")
        a.set_yticks([i * 1.5 + 0.5 for i in range(len(PINS))])
        a.set_yticklabels([f"pin {p}" for p in PINS])
        a.set_ylim(-0.5, len(PINS) * 1.5)
        a.grid(True, axis="x", alpha=0.3)
        self.canvas_live.draw_idle()

    def log(self, msg):
        self.txt_log.insert("end", msg + "\n")
        self.txt_log.see("end")

    def _browse_file(self, var):
        p = filedialog.askopenfilename(filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if p:
            var.set(p)

    def _browse_out(self):
        p = filedialog.askdirectory()
        if p:
            self.var_out.set(p)

    def refresh_ports(self):
        items = []
        if list_ports is not None:
            for p in list_ports.comports():
                items.append(f"{p.device}  ({p.description})")
        self.cbo_com["values"] = items
        teensy = [i for i in items if "Teensy" in i or "USB Serial Device" in i]
        if teensy:
            self.var_com.set(teensy[0])
        elif items:
            self.var_com.set(items[0])

    def _com(self):
        s = self.var_com.get().strip()
        if not s:
            raise ValueError("Select the Teensy COM port.")
        return s.split()[0]

    def test_teensy(self):
        try:
            tz = Teensy(self._com())
            lv = tz.query_levels()
            sy = tz.sync(10)
            tz.close()
            if not lv and not sy:
                raise RuntimeError("no reply (is YeLink_EdgeLogger_Teensy.ino loaded?)")
            rtt = statistics.median(s[2] for s in sy) * 1000 if sy else float("nan")
            self.lbl_teensy.config(text=f"levels {lv}, sync RTT {rtt:.2f} ms", foreground="green")
            self.log(f"Teensy OK: levels {lv}; sync round trip median {rtt:.2f} ms over {len(sy)} queries")
            if any(lv.values()):
                self.log("  WARNING: some pins are high. Press 'Turn all off' in the adapter before running.")
        except Exception as e:
            self.lbl_teensy.config(text="failed", foreground="red")
            self.log(f"Teensy test FAILED: {e}")

    def test_adapter(self):
        try:
            host, port, to = self.var_host.get().strip(), int(self.var_port.get()), float(self.var_timeout.get())
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(to)
            s.connect((host, port))
            s.close()
            self.lbl_adapter.config(text=f"connected to {host}:{port}", foreground="green")
            self.log(f"Adapter OK: {host}:{port}")
        except Exception as e:
            self.lbl_adapter.config(text="failed", foreground="red")
            self.log(f"Adapter test FAILED: {e}")

    def generate_commands(self):
        gen = os.path.join(os.path.dirname(os.path.abspath(__file__)), "make_test23_commands.py")
        if not os.path.isfile(gen):
            messagebox.showerror("Missing file", "make_test23_commands.py must be in the same folder as this script.")
            return
        seed = self.var_seed.get().strip() or "1"
        out = os.path.join(os.path.dirname(gen), f"test23_commands_seed{seed}.csv")
        try:
            r = subprocess.run([sys.executable, gen, seed, out], capture_output=True, text=True, timeout=60)
            self.log(r.stdout.strip() or r.stderr.strip())
            if r.returncode == 0:
                self.var_cmd.set(out)
        except Exception as e:
            self.log(f"Generation failed: {e}")

    # ---------------- run control ----------------
    def start_run(self):
        if self.worker and self.worker.is_alive():
            return
        try:
            com = self._com()
            host, port, to = self.var_host.get().strip(), int(self.var_port.get()), float(self.var_timeout.get())
        except ValueError as e:
            messagebox.showerror("Input error", str(e))
            return
        cmd_path = self.var_cmd.get().strip()
        if not os.path.isfile(cmd_path):
            messagebox.showerror("Missing file", "Choose or generate a command CSV first.")
            return
        with open(cmd_path, newline="", encoding="utf-8-sig") as f:
            cmds = list(csv.DictReader(f))
        if not cmds:
            messagebox.showerror("Empty file", "The command CSV has no commands.")
            return
        label = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        outdir = os.path.join(self.var_out.get().strip() or os.getcwd(), f"test23_{label}")
        os.makedirs(outdir, exist_ok=True)
        cfg = {"com": com, "host": host, "port": port, "timeout": to, "cmds": cmds, "label": label,
               "outdir": outdir, "cmd_path": cmd_path}
        self.stop_evt.clear()
        self.close_evt.clear()
        self.close_mark = None
        self.sent = []
        self.live_clock = None
        self.pb.configure(maximum=len(cmds), value=0)
        self.btn_run.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.btn_closed.config(state="disabled")
        self._init_live_axes()
        self.log(f"Starting run {label}: {len(cmds)} commands from {os.path.basename(cmd_path)} -> {outdir}")
        self.worker = threading.Thread(target=self._worker, args=(cfg,), daemon=True)
        self.worker.start()

    def stop_run(self):
        self.stop_evt.set()
        self.close_evt.set()
        self.log("Stop requested.")

    def adapter_closed(self):
        self.close_mark = time.perf_counter()
        self.btn_closed.config(state="disabled")
        self.close_evt.set()

    def _worker(self, cfg):
        tz = None
        try:
            tz = Teensy(cfg["com"])
            self.tz = tz
            levels = tz.query_levels()
            self.q.put(("log", f"Teensy pin levels before start: {levels}"))
            if any(levels.values()):
                self.q.put(("log", "ABORT: some pins are already high. Press 'Turn all off' in the adapter, then Run again."))
                self.q.put(("done", None))
                return
            w_off = wall_perf_offset()
            sync_samples = tz.sync(30)
            if len(sync_samples) < 5:
                self.q.put(("log", "ABORT: the Teensy did not answer clock-sync queries."))
                self.q.put(("done", None))
                return
            self.live_clock = (sync_samples[0][1], sync_samples[0][0])     # (us, perf) one-point map for display
            rtt = statistics.median(s[2] for s in sync_samples) * 1000
            self.q.put(("log", f"Initial clock sync: {len(sync_samples)} samples, RTT median {rtt:.2f} ms"))

            rows = []
            cmds = cfg["cmds"]
            t_start = time.perf_counter() + 1.0
            self.run_t0 = t_start
            n_since_sync = 0
            for i, c in enumerate(cmds):
                if self.stop_evt.is_set():
                    break
                target = t_start + float(c["t_offset_s"])
                if n_since_sync >= 20 and target - time.perf_counter() > 0.12:
                    sync_samples += tz.sync(5)
                    n_since_sync = 0
                while time.perf_counter() < target - 0.003 and not self.stop_evt.is_set():
                    time.sleep(0.001)
                while time.perf_counter() < target:
                    pass
                res = send_one(cfg["host"], cfg["port"], c["command"], cfg["timeout"])
                n_since_sync += 1
                self.sent.append((res["send_perf"], c["category"], c["command"]))
                rows.append({**c, "send_wall": res["send_wall"], "send_perf": f"{res['send_perf']:.6f}",
                             "connected": res["connected"], "connect_ms": res["connect_ms"],
                             "reply": res["reply"], "reply_ms": res["reply_ms"], "error": res["error"]})
                self.q.put(("progress", i + 1, len(cmds), c, res))

            if self.stop_evt.is_set():
                self.q.put(("log", "Stopped early: turning test pins off."))
                for p in PINS:
                    send_one(cfg["host"], cfg["port"], f"p={p},v=0", cfg["timeout"])
                close_mark, levels_after = None, None
            else:
                self.q.put(("log", "All commands sent. Waiting 6 s for the last timers..."))
                time.sleep(6.0)
                sync_samples += tz.sync(30)
                levels = tz.query_levels()
                self.q.put(("log", f"Pin levels after the final latch: {levels}  (expected: all 1)"))
                self.q.put(("await_close",))
                self.close_evt.wait()
                close_mark = self.close_mark
                # poll until every monitored pin has fallen (or 20 s), so the order of
                # "close window" and "press button" does not matter
                t_poll0 = time.perf_counter()
                levels_after = tz.query_levels()
                while any(levels_after.get(p, 0) for p in PINS) and time.perf_counter() - t_poll0 < 20.0 \
                        and not self.stop_evt.is_set():
                    time.sleep(0.5)
                    levels_after = tz.query_levels()
                waited = time.perf_counter() - t_poll0
                if any(levels_after.get(p, 0) for p in PINS):
                    self.q.put(("log", f"WARNING: pins still high {waited:.0f} s after the button press: {levels_after}. "
                                       f"Is the adapter window closed? All-off on close FAILED if it is."))
                else:
                    self.q.put(("log", f"All monitored pins low {waited:.1f} s after the button press: {levels_after}"))
                time.sleep(1.0)
                sync_samples += tz.sync(30)

            time.sleep(0.3)
            edges = list(tz.edges)
            tz.close()
            self.tz = None
            clk = fit_clock(sync_samples)
            outdir = cfg["outdir"]
            meta = {"label": cfg["label"], "commands_file": os.path.abspath(cfg["cmd_path"]), "host": cfg["host"],
                    "port": cfg["port"], "teensy": cfg["com"], "wall_minus_perf_s": w_off, "clock": clk,
                    "close_mark_perf": close_mark, "levels_after_close": levels_after,
                    "stopped_early": self.stop_evt.is_set(), "teensy_other": tz.other[:50]}
            with open(os.path.join(outdir, "run.csv"), "w", newline="", encoding="utf-8") as f:
                fields = ["index", "t_offset_s", "category", "command", "pins", "group", "d", "send_wall", "send_perf",
                          "connected", "connect_ms", "reply", "reply_ms", "error"]
                w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
            with open(os.path.join(outdir, "edges.csv"), "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["teensy_us", "pin", "state", "pc_perf_s", "pc_wall"])
                for us, pin, st, _ in edges:
                    p = teensy_to_perf(us, clk)
                    w.writerow([us, pin, st, f"{p:.6f}",
                                datetime.fromtimestamp(p + w_off).strftime("%Y-%m-%d %H:%M:%S.%f")])
            with open(os.path.join(outdir, "sync.csv"), "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["pc_mid_perf_s", "teensy_us", "rtt_ms"])
                for s in sync_samples:
                    w.writerow([f"{s[0]:.6f}", s[1], f"{s[2] * 1000:.3f}"])
            with open(os.path.join(outdir, "meta.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
            try:
                import shutil
                shutil.copy(cfg["cmd_path"], os.path.join(outdir, "commands_used.csv"))
            except Exception:
                pass
            self.q.put(("log", f"Clock fit: {clk['n']} samples, RTT median {clk['rtt_median_ms']:.2f} ms, "
                               f"residual RMS {clk['resid_rms_us']:.0f} us, drift {clk['drift_ppm']:.1f} ppm"))
            self.q.put(("log", f"Edges recorded: {len(edges)}.  Files written to {outdir}"))
            self.q.put(("done", outdir))
        except Exception as e:
            import traceback
            self.q.put(("log", "Worker error: " + "".join(traceback.format_exception_only(type(e), e)).strip()))
            self.q.put(("done", None))
        finally:
            if tz is not None and tz.running:
                tz.close()
            self.tz = None

    def _poll_queue(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self.log(item[1])
                elif kind == "progress":
                    _, n, total, c, res = item
                    self.pb["value"] = n
                    self.lbl_progress.config(text=f"{n} / {total} sent")
                    if (not res["connected"]) or (c["category"] != "malformed" and not res["reply"].startswith("OK")) \
                            or (c["category"] == "malformed" and not res["reply"].startswith("ERR")):
                        self.log(f"  index {c['index']} {c['category']} '{c['command']}' -> '{res['reply']}' {res['error']}")
                    elif n % 10 == 0:
                        self.log(f"  {n}/{total}  {c['category']:11s} {c['command']}")
                elif kind == "await_close":
                    self.btn_closed.config(state="normal")
                    self.lbl_progress.config(text="CLOSE the YeLink_AdapterUI window, then press the button")
                    self.log("\n>>> Close the YeLink_AdapterUI window now (click its X), then press "
                             "'I have closed the adapter window'.")
                    messagebox.showinfo("Export the adapter log, then close the adapter",
                                        "All four test pins are latched ON.\n\n"
                                        "1. In YeLink_AdapterUI click 'Export CSV' and save the log "
                                        "(you will load it on tab 2 for the adapter-stamp latency).\n"
                                        "2. Close the YeLink_AdapterUI window (click its X).\n"
                                        "3. Press 'I have closed the adapter window' in this program.\n\n"
                                        "(If you press the button early, the program keeps watching the pins for 20 s.)")
                elif kind == "done":
                    self.btn_run.config(state="normal")
                    self.btn_stop.config(state="disabled")
                    self.btn_closed.config(state="disabled")
                    folder = item[1]
                    if folder:
                        self.lbl_progress.config(text="finished")
                        self.var_folder.set(folder)
                        self.log("Run finished. Export the adapter log (Export CSV) if the adapter is still open, "
                                 "then use tab 2. Showing the analysis without the adapter log now.")
                        self.analyze()
                        self.plot_timeline()
                    else:
                        self.lbl_progress.config(text="stopped")
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _update_live(self):
        try:
            if self.var_live.get() and self.tz is not None and self.live_clock is not None and self.run_t0:
                us0, perf0 = self.live_clock
                now = time.perf_counter() - self.run_t0
                lo = max(0.0, now - 30.0)
                a = self.ax_live
                a.clear()
                a.set_title("Live: pin levels from the Teensy (outputs) and commands sent (inputs)")
                a.set_xlabel("seconds since run start")
                a.set_yticks([i * 1.5 + 0.5 for i in range(len(PINS))])
                a.set_yticklabels([f"pin {p}" for p in PINS])
                a.set_ylim(-0.5, len(PINS) * 1.5)
                a.set_xlim(lo, max(now, lo + 1.0))
                a.grid(True, axis="x", alpha=0.3)
                edges = list(self.tz.edges)
                for i, p in enumerate(PINS):
                    base = i * 1.5
                    ts, vs = [lo], [0]
                    state = 0
                    for us, pin, st, _ in edges:
                        if pin != p:
                            continue
                        t = perf0 + (unwrap(us, us0) - us0) / 1e6 - self.run_t0
                        if t < lo:
                            state = st
                            ts, vs = [lo], [st]
                            continue
                        ts.append(t)
                        vs.append(st)
                        state = st
                    ts.append(now)
                    vs.append(state)
                    a.step(ts, [base + v for v in vs], where="post", color=PIN_COLORS.get(p, "k"), lw=1.5)
                for perf, cat, cmd in self.sent:
                    t = perf - self.run_t0
                    if t >= lo:
                        a.axvline(t, color=CAT_COLORS.get(cat, "k"), lw=0.8, alpha=0.5, ls="--")
                a.text(0.01, 0.98, "dashed = command sent (colour by category); edges = Teensy", transform=a.transAxes,
                       fontsize=7, va="top", color="dimgray")
                self.canvas_live.draw_idle()
        except Exception:
            pass
        self.after(500, self._update_live)

    def plot_timeline(self):
        """Full input/output timeline from the saved files of a results folder (no effect on timing:
        it only runs when you press the button)."""
        folder = self.var_folder.get().strip()
        if not os.path.isfile(os.path.join(folder, "edges.csv")):
            folder = filedialog.askdirectory(title="Choose a results folder (contains run.csv and edges.csv)")
            if not folder or not os.path.isfile(os.path.join(folder, "edges.csv")):
                return
            self.var_folder.set(folder)
        sent = []
        with open(os.path.join(folder, "run.csv"), newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                try:
                    sent.append((float(r["send_perf"]), r["category"], r["command"]))
                except ValueError:
                    pass
        edges = []
        with open(os.path.join(folder, "edges.csv"), newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                edges.append((float(r["pc_perf_s"]), int(r["pin"]), int(r["state"])))
        if not sent:
            return
        t0 = min(s[0] for s in sent) - 1.0
        t_end = max([s[0] for s in sent] + [e[0] for e in edges]) + 1.0
        a = self.ax_live
        a.clear()
        for i, p in enumerate(PINS):
            base = i * 1.5
            ts, vs, state = [0.0], [0], 0
            for t, pin, st in sorted(edges):
                if pin != p:
                    continue
                ts.append(t - t0)
                vs.append(st)
                state = st
            ts.append(t_end - t0)
            vs.append(state)
            a.step(ts, [base + v for v in vs], where="post", color=PIN_COLORS.get(p, "k"), lw=1.2)
        seen = set()
        for perf, cat, cmd in sent:
            lab = cat if cat not in seen else None
            seen.add(cat)
            a.axvline(perf - t0, color=CAT_COLORS.get(cat, "k"), lw=0.7, alpha=0.5, ls="--", label=lab)
        self.fig_live.suptitle(f"I/O timeline: {os.path.basename(folder)}  (dashed = command sent, steps = pin level on Teensy)",
                               fontsize=9)
        a.set_xlabel("seconds since first command - 1 s")
        a.set_yticks([i * 1.5 + 0.5 for i in range(len(PINS))])
        a.set_yticklabels([f"pin {p}" for p in PINS])
        a.set_ylim(-0.5, len(PINS) * 1.5)
        a.set_xlim(0, t_end - t0)
        a.grid(True, axis="x", alpha=0.3)
        a.legend(fontsize=7, loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=4, frameon=False)
        self.fig_live.tight_layout(rect=(0, 0, 1, 0.95))
        self.canvas_live.draw()
        try:
            self.fig_live.savefig(os.path.join(folder, "test23_timeline.png"), dpi=200)
            self.log(f"Timeline saved: {os.path.join(folder, 'test23_timeline.png')}")
        except Exception:
            pass

    # ---------------- Tab 2 ----------------
    def _build_an_tab(self):
        f = self.tab_an
        inp = ttk.LabelFrame(f, text="Input")
        inp.pack(fill="x", padx=10, pady=6)
        self.var_folder = tk.StringVar()
        self.var_adlog = tk.StringVar()
        ttk.Label(inp, text="Results folder").grid(row=0, column=0, padx=6, pady=4, sticky="e")
        ttk.Entry(inp, textvariable=self.var_folder, width=90).grid(row=0, column=1, sticky="we")
        ttk.Button(inp, text="Browse...", command=self._browse_folder).grid(row=0, column=2, padx=6)
        ttk.Label(inp, text="Adapter log CSV (optional)").grid(row=1, column=0, padx=6, pady=4, sticky="e")
        ttk.Entry(inp, textvariable=self.var_adlog, width=90).grid(row=1, column=1, sticky="we")
        ttk.Button(inp, text="Browse...", command=lambda: self._browse_file(self.var_adlog)).grid(row=1, column=2, padx=6)
        inp.columnconfigure(1, weight=1)
        bar = ttk.Frame(f)
        bar.pack(fill="x", padx=10, pady=4)
        ttk.Button(bar, text="Analyze and plot", command=self.analyze).pack(side="left", padx=4)
        ttk.Button(bar, text="Save figure (PNG)", command=self.save_figure).pack(side="left", padx=4)
        ttk.Button(bar, text="Open results folder", command=self.open_folder).pack(side="left", padx=4)
        body = ttk.Frame(f)
        body.pack(fill="both", expand=True, padx=10, pady=6)
        left = ttk.Frame(body)
        left.pack(side="left", fill="y")
        self.txt_summary = tk.Text(left, width=70, wrap="word", font=("Consolas", 9))
        self.txt_summary.pack(fill="both", expand=True)
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self.fig = Figure(figsize=(9, 6.5), dpi=100)
        self.canvas = FigureCanvasTkAgg(self.fig, master=right)
        tb = NavigationToolbar2Tk(self.canvas, right)
        tb.update()
        tb.pack(side="top", fill="x")
        self.canvas.get_tk_widget().pack(side="top", fill="both", expand=True)

    def _browse_folder(self):
        p = filedialog.askdirectory()
        if p:
            self.var_folder.set(p)

    def analyze(self):
        folder = self.var_folder.get().strip()
        if not os.path.isfile(os.path.join(folder, "run.csv")):
            messagebox.showerror("Missing folder", "Choose a results folder that contains run.csv, edges.csv and meta.json.")
            return
        adlog = self.var_adlog.get().strip() or None
        try:
            res = analyze_folder(folder, adlog)
        except Exception as e:
            messagebox.showerror("Analysis error", str(e))
            return
        self.txt_summary.delete("1.0", "end")
        self.txt_summary.insert("1.0", res["text"])
        plot_results(self.fig, res)
        self.canvas.draw()
        with open(os.path.join(folder, "summary.txt"), "w", encoding="utf-8") as f:
            f.write(res["text"] + "\n")
        try:
            self.fig.savefig(os.path.join(folder, "test23_figure.png"), dpi=200)
        except Exception:
            pass

    def save_figure(self):
        p = filedialog.asksaveasfilename(defaultextension=".png", filetypes=[("PNG image", "*.png")],
                                         initialfile="test23_figure.png")
        if p:
            self.fig.savefig(p, dpi=200)

    def open_folder(self):
        folder = self.var_folder.get().strip()
        if os.path.isdir(folder):
            try:
                os.startfile(folder)
            except Exception:
                pass

    def _on_close(self):
        self.stop_evt.set()
        self.close_evt.set()
        if self.tz is not None:
            try:
                self.tz.close()
            except Exception:
                pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
