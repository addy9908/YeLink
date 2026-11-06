# -*- coding: utf-8 -*-
"""
test3_closedloop_gui.py  --  YeLink Test 3: EthoVision closed-loop comparison, YeLink vs Noldus IO box (GUI)

NEW FILE 2026-10-30. Recorder + analyser for the closed-loop test. Reuses the Teensy link, clock fit
and GUI style of Test23_gui.py. Teensy sketch YeLink_EdgeLogger_Teensy.ino is used UNCHANGED.
Rev 2 (2026-10-30): added "Monitor levels" button (live pin levels + edge count without recording) for
wiring / COM-port diagnosis.
Rev 3 (2026-10-30): after a tab-2 analysis with the EthoVision export loaded, the tab-1 timeline also shows
the export's in-zone state on the Teensy time axis (offset and rate from the fit, not from the Mark button).
Rev 4 (2026-10-30): timeline polished: only IO box and YeLink traces (pins 4/5 and Mark stamps no longer
drawn); with the export loaded, time zero = first exported video sample and the plot is trimmed to the video
span (CSV files untouched).
Rev 5 (2026-10-30): analysis panel B uses video time when the export fit exists; all panel titles / axis
labels reworded (pulse duration = ON edge to OFF edge; panel D x = EthoVision trial time of zone entry from
the track export); timeline x-range entries (default -5 s to video end + 5 s), dotted lines at video start/end.
Rev 6 (2026-10-30): panel B dashed y=0; panel C R^2 in legend; panel D now shows IO-box and YeLink ON/OFF
edges relative to the video event (same clock map for all series; legend gives per-series SD = jitter),
clock slope shown with 2 decimals in the title (full precision stays in the summary text).
Rev 7 (2026-10-30): panel D is now a paired plot (YeLink deviation vs IO-box deviation for the same event,
identity line) so common-mode jitter from EthoVision is separated from YeLink's own; shorter C/D titles.
Rev 8 (2026-10-30): figure re-ordered: A = output timeline (shared draw_timeline, same x-range boxes as tab 1),
B = relative latency per event, C = its distribution (same colours/legend as B, common bins), D = pulse
duration with dashed y = x line labelled by R^2. The video-time jitter panel was dropped from the figure;
its numbers remain in the summary text.

Open in Spyder and press F5, or run:  python test3_closedloop_gui.py

What it measures
  EthoVision trial control fires the same rule two ways when the "mouse" enters / leaves zone A:
    (a) a TTL output on the Noldus IO box, and
    (b) an external command (YeLink_Controller.exe -> YeLink_AdapterUI -> Uno) that latches pin 8 on
        (p=8 v=1 d=0) on entry and turns it off (p=8 v=0) on exit.
  Both lines are recorded on the Teensy edge logger. The primary metric is the YeLink edge time minus
  the IO-box edge time for the same event (ON and OFF separately). The EthoVision track export can be
  loaded afterwards to check that no event was missed, that pulse durations match in-zone durations,
  and that the video was processed in real time (slope of IO-box time vs EthoVision time must be 1.000).

Hardware
  Teensy 4.0 running YeLink_EdgeLogger_Teensy.ino (monitors Teensy pins 2,3,4,5).
  Every line into the Teensy goes through a divider: source -[10k]- Teensy pin -[20k]- GND, grounds shared.
  Teensy 4.0 pins are NOT 5 V tolerant; both the Uno output and the IO-box TTL are 5 V.
    IO box TTL output  -> divider -> Teensy pin 2   (default, configurable)
    Uno pin 8 (YeLink) -> divider -> Teensy pin 3   (default, configurable)
  Pins 4 and 5 are recorded too (spare / optional optical marker) but not analysed.

Tab 1 "Record"
  Select the Teensy COM port, assign which Teensy pin carries the IO-box signal and which carries YeLink,
  press Start before starting acquisition in EthoVision, press Stop after the trial ends. "Mark" stamps a
  labelled time (e.g. "pressed Start in EthoVision") into marks.csv. The live plot is off by default.
  Files: edges.csv, sync.csv, marks.csv, meta.json in results\test3_<timestamp>\.

Tab 2 "Analyze"
  Results folder (required); adapter log CSV exported from YeLink_AdapterUI (optional: splits the YeLink
  delay into "EthoVision -> adapter" and "adapter -> pin"); EthoVision track export (optional, .xlsx or
  text export with the "In zone" variable).

Requirements: Python 3.8+, pyserial, numpy, matplotlib; openpyxl only if you load an .xlsx export (all in Anaconda).
"""
import csv
import json
import os
import queue
import statistics
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

sys.setswitchinterval(0.001)

PINS = [2, 3, 4, 5]
PIN_COLORS = {2: "#1f77b4", 3: "#2ca02c", 4: "#ff7f0e", 5: "#9467bd"}
MATCH_LEAD = 0.05        # s: a YeLink edge may precede the IO-box edge by at most this much and still be paired
MATCH_WINDOW = 1.0       # s: a YeLink edge must follow the IO-box edge within this window to be paired
SYNC_EVERY_S = 10.0      # periodic clock-sync interval during recording


# ----------------------------------------------------------------------
# Teensy link (same as Test23_gui.py)
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
# Analysis helpers
# ----------------------------------------------------------------------

def stat(name, vals, unit="ms"):
    a = np.asarray([v for v in vals if v is not None and np.isfinite(v)], dtype=float)
    if a.size == 0:
        return f"{name}: no data"
    return (f"{name}: n={a.size}, median {np.median(a):.2f}, p95 {np.percentile(a, 95):.2f}, "
            f"min {a.min():.2f}, max {a.max():.2f} {unit}")


def pulses_from_edges(edges, invert=False):
    """edges: sorted list of (t, state). Returns list of dict(rise, fall or None) and notes."""
    notes = []
    seq = [(t, (1 - s) if invert else s) for t, s in edges]
    pulses = []
    cur = None
    for t, s in seq:
        if s == 1:
            if cur is not None:
                notes.append(f"two rising edges without a fall at {cur['rise']:.3f} and {t:.3f}")
            cur = {"rise": t, "fall": None}
        else:
            if cur is None:
                notes.append(f"falling edge without a preceding rise at {t:.3f} (ignored)")
                continue
            cur["fall"] = t
            pulses.append(cur)
            cur = None
    if cur is not None:
        notes.append(f"last pulse still high at end of recording (rise {cur['rise']:.3f})")
        pulses.append(cur)
    return pulses, notes


def load_adapter_log(path):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("Source", "").strip().upper() != "TCP":
                continue
            try:
                ts = datetime.strptime(r["Time"].strip(), "%Y-%m-%d %H:%M:%S.%f").timestamp()
            except (ValueError, KeyError):
                continue
            rows.append({"ts": ts, "received": r.get("Received", "").strip()})
    return rows


def _cell_str(v):
    if v is None:
        return ""
    return str(v).strip()


def load_etho_export(path):
    """Read an EthoVision track export (.xlsx or text). Returns dict(time, columns{name: values}, header_info)."""
    rows = []
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError:
            raise RuntimeError("openpyxl is needed to read .xlsx exports (or export from EthoVision as text).")
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb[wb.sheetnames[0]]
        for r in ws.iter_rows(values_only=True):
            rows.append([_cell_str(v) for v in r])
        wb.close()
    else:
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
            sample = f.read(4096)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
            except csv.Error:
                dialect = csv.excel
            for r in csv.reader(f, dialect):
                rows.append([_cell_str(v) for v in r])
    # find the column-name row
    hdr_i = None
    for i, r in enumerate(rows[:200]):
        low = [c.lower() for c in r[:4]]
        if any(c in ("trial time", "recording time") for c in low):
            hdr_i = i
            break
    if hdr_i is None:
        raise RuntimeError("Could not find the 'Trial time' / 'Recording time' column row in the export.")
    names = rows[hdr_i]
    data_i = hdr_i + 1
    if data_i < len(rows) and rows[data_i] and rows[data_i][0].lower() in ("s", "sec", ""):
        data_i += 1                      # units row
    header_info = rows[:hdr_i]
    cols = {n: [] for n in names if n}
    for r in rows[data_i:]:
        if not r or not r[0]:
            continue
        for k, n in enumerate(names):
            if n:
                cols[n].append(r[k] if k < len(r) else "")
    tname = "Trial time" if "Trial time" in cols else "Recording time"
    t = []
    for v in cols[tname]:
        try:
            t.append(float(v.replace(",", ".")))
        except ValueError:
            t.append(np.nan)
    return {"time": np.array(t, dtype=float), "time_name": tname, "columns": cols, "header_info": header_info,
            "zone_columns": [n for n in cols if n.lower().startswith("in zone")]}


def etho_zone_events(exp, col):
    """Entry/exit times from an 'In zone' column. Missing samples ('-') keep the previous state."""
    t = exp["time"]
    vals = exp["columns"][col]
    state = 0
    events = []           # (time, 1 entry / 0 exit, sample index)
    for i, v in enumerate(vals):
        s = v.strip()
        if s in ("", "-"):
            continue
        try:
            z = 1 if float(s.replace(",", ".")) >= 0.5 else 0
        except ValueError:
            continue
        if z != state:
            events.append((t[i], z, i))
            state = z
    frame_dt = float(np.nanmedian(np.diff(t))) if len(t) > 2 else float("nan")
    return events, frame_dt


def pair_sequences(ref_times, other_times, lead, window):
    """Greedy in-order pairing: each ref time takes the first unused 'other' time in [ref-lead, ref+window]."""
    other = sorted(other_times)
    used = [False] * len(other)
    pairs = []
    for rt in sorted(ref_times):
        best = None
        for j, ot in enumerate(other):
            if used[j]:
                continue
            if ot < rt - lead:
                continue
            if ot > rt + window:
                break
            best = j
            break
        if best is not None:
            used[best] = True
            pairs.append((rt, other[best]))
        else:
            pairs.append((rt, None))
    unmatched_other = [ot for j, ot in enumerate(other) if not used[j]]
    return pairs, unmatched_other


def analyze_folder(folder, io_pin=None, yl_pin=None, invert_io=False, adapter_log=None,
                   etho_path=None, etho_col=None):
    with open(os.path.join(folder, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    io_pin = int(io_pin if io_pin is not None else meta.get("io_pin", 2))
    yl_pin = int(yl_pin if yl_pin is not None else meta.get("yl_pin", 3))
    edges = []
    with open(os.path.join(folder, "edges.csv"), newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            edges.append((float(r["pc_perf_s"]), int(r["pin"]), int(r["state"])))
    edges.sort()
    marks = []
    mp = os.path.join(folder, "marks.csv")
    if os.path.isfile(mp):
        with open(mp, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                marks.append((float(r["pc_perf_s"]), r["label"]))
    t0 = meta.get("start_perf", edges[0][0] if edges else 0.0)

    io_edges = [(t, s) for t, p, s in edges if p == io_pin]
    yl_edges = [(t, s) for t, p, s in edges if p == yl_pin]
    io_pulses, io_notes = pulses_from_edges(io_edges, invert=invert_io)
    yl_pulses, yl_notes = pulses_from_edges(yl_edges)

    # --- pair ON edges and OFF edges, IO box as reference ---
    on_pairs, yl_on_unmatched = pair_sequences([p["rise"] for p in io_pulses], [p["rise"] for p in yl_pulses],
                                               MATCH_LEAD, MATCH_WINDOW)
    off_pairs, yl_off_unmatched = pair_sequences([p["fall"] for p in io_pulses if p["fall"] is not None],
                                                 [p["fall"] for p in yl_pulses if p["fall"] is not None],
                                                 MATCH_LEAD, MATCH_WINDOW)
    d_on = [(y - r) * 1000 for r, y in on_pairs if y is not None]
    on_ref_t = [r for r, y in on_pairs if y is not None]          # absolute (perf) times; made relative below
    d_off = [(y - r) * 1000 for r, y in off_pairs if y is not None]
    off_ref_t = [r for r, y in off_pairs if y is not None]
    io_unmatched_on = [r for r, y in on_pairs if y is None]
    io_unmatched_off = [r for r, y in off_pairs if y is None]

    # --- durations ---
    io_dur = [(p["fall"] - p["rise"]) for p in io_pulses if p["fall"] is not None]
    yl_dur = [(p["fall"] - p["rise"]) for p in yl_pulses if p["fall"] is not None]
    dur_pairs = []
    yl_by_rise = {p["rise"]: p for p in yl_pulses}
    for r, y in on_pairs:
        if y is None:
            continue
        iop = next(p for p in io_pulses if p["rise"] == r)
        ylp = yl_by_rise[y]
        if iop["fall"] is not None and ylp["fall"] is not None:
            dur_pairs.append((iop["fall"] - iop["rise"], ylp["fall"] - ylp["rise"]))

    # --- adapter log split (optional) ---
    ad = None
    if adapter_log:
        w_off = meta["wall_minus_perf_s"]
        rows = load_adapter_log(adapter_log)
        ad_on = [r["ts"] - w_off for r in rows if "v=1" in r["received"].replace(" ", "")]
        ad_off = [r["ts"] - w_off for r in rows if "v=0" in r["received"].replace(" ", "")]
        a_on, _ = pair_sequences([r for r, y in on_pairs if y is not None], ad_on, MATCH_LEAD, MATCH_WINDOW)
        a_off, _ = pair_sequences([r for r, y in off_pairs if y is not None], ad_off, MATCH_LEAD, MATCH_WINDOW)
        yl_on_of = {r: y for r, y in on_pairs if y is not None}
        yl_off_of = {r: y for r, y in off_pairs if y is not None}
        ad = {"io_to_adapter_on": [(a - r) * 1000 for r, a in a_on if a is not None],
              "adapter_to_pin_on": [(yl_on_of[r] - a) * 1000 for r, a in a_on if a is not None],
              "io_to_adapter_off": [(a - r) * 1000 for r, a in a_off if a is not None],
              "adapter_to_pin_off": [(yl_off_of[r] - a) * 1000 for r, a in a_off if a is not None],
              "n_rows": len(rows), "n_on": len(ad_on), "n_off": len(ad_off)}

    # --- EthoVision export (optional) ---
    et = None
    if etho_path:
        exp = load_etho_export(etho_path)
        col = etho_col or (exp["zone_columns"][0] if exp["zone_columns"] else None)
        if col is None:
            raise RuntimeError("The export has no 'In zone' column. Add it in the export settings (Analysis variables).")
        events, frame_dt = etho_zone_events(exp, col)
        e_in = [t for t, z, i in events if z == 1]
        e_out = [t for t, z, i in events if z == 0]
        et = {"col": col, "frame_dt": frame_dt, "n_entries": len(e_in), "n_exits": len(e_out),
              "time_name": exp["time_name"], "n_samples": int(np.sum(np.isfinite(exp["time"]))),
              "duration_s": float(np.nanmax(exp["time"]) - np.nanmin(exp["time"])) if len(exp["time"]) else 0.0,
              "events": events, "t_first": float(np.nanmin(exp["time"])), "t_last": float(np.nanmax(exp["time"]))}
        # in-zone durations from the export
        e_dur = []
        st = None
        for t, z, i in events:
            if z == 1:
                st = t
            elif st is not None:
                e_dur.append(t - st)
                st = None
        et["durations"] = e_dur
        io_rises = [p["rise"] for p in io_pulses]
        io_falls = [p["fall"] for p in io_pulses if p["fall"] is not None]
        yl_on_of = {r: y for r, y in on_pairs if y is not None}
        yl_off_of = {r: y for r, y in off_pairs if y is not None}
        if len(e_in) >= 2 and len(e_in) == len(io_rises):
            x = np.array(e_in)
            y = np.array(io_rises)
            b, a = np.polyfit(x, y, 1)
            resid = (y - (a + b * x)) * 1000
            et["fit"] = {"slope": float(b), "intercept": float(a), "resid_ms": resid.tolist(), "x": x.tolist(),
                         "method": "entries paired by order (equal counts)"}
            et["dur_diff_ms"] = [(d_io - d_e) * 1000 for d_io, d_e in zip(io_dur, e_dur)] if len(io_dur) == len(e_dur) else []
            # Jitter of each output relative to the video event. The same clock map (a, b from the IO-box ON
            # fit) is applied to every series, so IO-box ON scatters around 0 by construction and the others
            # show their offset relative to it; the SD of each series is its timing jitter vs the video.
            series = {"IO box ON": (x.tolist(), resid.tolist())}
            yl_on = [(ex, yl_on_of.get(r)) for ex, r in zip(e_in, io_rises)]
            yl_on = [(ex, yy) for ex, yy in yl_on if yy is not None]
            if yl_on:
                series["YeLink ON"] = ([p[0] for p in yl_on], [(p[1] - (a + b * p[0])) * 1000 for p in yl_on])
            if len(e_out) == len(io_falls) and io_falls:
                series["IO box OFF"] = (e_out, [(f - (a + b * ex)) * 1000 for ex, f in zip(e_out, io_falls)])
                yl_off = [(ex, yl_off_of.get(f)) for ex, f in zip(e_out, io_falls)]
                yl_off = [(ex, yy) for ex, yy in yl_off if yy is not None]
                if yl_off:
                    series["YeLink OFF"] = ([p[0] for p in yl_off], [(p[1] - (a + b * p[0])) * 1000 for p in yl_off])
            et["series"] = series
        elif len(e_in) >= 2 and len(io_rises) >= 2:
            # counts differ: anchor on the first event, then pair by nearest within half a second
            off0 = io_rises[0] - e_in[0]
            pr, _ = pair_sequences([t + off0 for t in e_in], io_rises, 0.5, 0.5)
            xy = [(ex - off0, iy) for ex, iy in pr if iy is not None]
            if len(xy) >= 2:
                x = np.array([p[0] for p in xy])
                y = np.array([p[1] for p in xy])
                b, a = np.polyfit(x, y, 1)
                resid = (y - (a + b * x)) * 1000
                et["fit"] = {"slope": float(b), "intercept": float(a), "resid_ms": resid.tolist(), "x": x.tolist(),
                             "method": f"counts differ ({len(e_in)} export entries vs {len(io_rises)} IO-box pulses); "
                                       f"{len(xy)} paired by nearest after anchoring on the first event"}
                et["series"] = {"IO box ON": (x.tolist(), resid.tolist())}
            et["dur_diff_ms"] = []

    # --- time axis for per-event plots: video start if the export fit exists, else recording start ---
    if et is not None and "fit" in et:
        t_ref = et["fit"]["intercept"] + et["fit"]["slope"] * et["t_first"]
        t_axis_label = "Time since video start (s)"
    else:
        t_ref = t0
        t_axis_label = "Time since recording start (s)"
    d_on_t = [r - t_ref for r in on_ref_t]
    d_off_t = [r - t_ref for r in off_ref_t]

    # --- text summary ---
    clk = meta["clock"]
    rec_len = meta.get("stop_perf", edges[-1][0] if edges else t0) - t0
    lines = [
        f"YeLink Test 3 (closed loop) - recording {meta['label']}",
        f"Recording length: {rec_len:.1f} s   edges on all pins: {len(edges)}   marks: {len(marks)}",
        f"Clock sync: {clk['n']} samples, RTT median {clk['rtt_median_ms']:.2f} ms, "
        f"fit residual RMS {clk['resid_rms_us']:.0f} us, drift {clk['drift_ppm']:.1f} ppm",
        f"Roles: IO box = Teensy pin {io_pin}{' (inverted)' if invert_io else ''}, YeLink = Teensy pin {yl_pin}",
        "",
        f"Output pulses detected: IO box {len(io_pulses)}, YeLink {len(yl_pulses)}",
        "",
        "Latency of YeLink relative to the IO box (YeLink edge minus IO-box edge; positive = YeLink later)",
        "  " + stat("ON  (zone entry)", d_on),
        "  " + stat("OFF (zone exit) ", d_off),
        f"  unpaired events: IO-box ON {len(io_unmatched_on)}, IO-box OFF {len(io_unmatched_off)}, "
        f"YeLink ON {len(yl_on_unmatched)}, YeLink OFF {len(yl_off_unmatched)}",
        "",
        "Output pulse duration (ON edge to OFF edge)",
        "  " + stat("IO box", [d * 1000 for d in io_dur]),
        "  " + stat("YeLink", [d * 1000 for d in yl_dur]),
        "  " + stat("YeLink minus IO box, paired pulses", [(y - i) * 1000 for i, y in dur_pairs]),
    ]
    if ad is not None:
        lines += ["", f"Adapter log: {ad['n_rows']} TCP rows ({ad['n_on']} v=1, {ad['n_off']} v=0)",
                  "  Decomposition of the YeLink latency:",
                  "  " + stat("IO-box edge -> command received by adapter, ON ", ad["io_to_adapter_on"]),
                  "  " + stat("command received by adapter -> YeLink pin, ON ", ad["adapter_to_pin_on"]),
                  "  " + stat("IO-box edge -> command received by adapter, OFF", ad["io_to_adapter_off"]),
                  "  " + stat("command received by adapter -> YeLink pin, OFF", ad["adapter_to_pin_off"])]
    if et is not None:
        lines += ["", f"EthoVision track export: column '{et['col']}', {et['n_samples']} samples, "
                      f"{et['duration_s']:.1f} s of {et['time_name']}, sample interval {et['frame_dt'] * 1000:.2f} ms",
                  f"  zone entries {et['n_entries']}, exits {et['n_exits']}  (IO-box pulses {len(io_pulses)})"]
        if "fit" in et:
            fit = et["fit"]
            r = np.array(fit["resid_ms"])
            lines += [f"  playback-rate check: IO-box edge time = {fit['slope']:.5f} x EthoVision entry time + const  "
                      f"({fit['method']})",
                      f"    slope 1.000 = processed in real time; {(fit['slope'] - 1) * 100:+.2f} % here",
                      f"    residual SD {r.std():.1f} ms, range {r.min():.1f}..{r.max():.1f} ms "
                      f"(sample quantisation alone gives SD about {et['frame_dt'] * 1000 / np.sqrt(12):.1f} ms)"]
            if et.get("series"):
                lines.append("  timing of each output relative to the video event (same clock map; offset vs IO-box ON, jitter = SD):")
                for name, (sx, sr) in et["series"].items():
                    arr = np.asarray(sr)
                    lines.append(f"    {name:11s} n={arr.size}, mean {arr.mean():+.1f} ms, SD {arr.std():.1f} ms")
        if et.get("dur_diff_ms"):
            lines.append("  " + stat("IO-box pulse duration minus EthoVision in-zone duration", et["dur_diff_ms"]))
        lines.append("  NOTE: the export has no shared clock with the Teensy; it checks event counts, durations and "
                     "playback rate, not absolute latency.")
    notes = io_notes + yl_notes
    if notes:
        lines += ["", "Edge-sequence notes:"] + ["  " + n for n in notes[:20]]
    if marks:
        lines += ["", "Marks:"] + [f"  {t - t0:9.3f} s  {lab}" for t, lab in marks]
    return {"text": "\n".join(lines), "d_on": d_on, "d_on_t": d_on_t, "d_off": d_off, "d_off_t": d_off_t,
            "t_axis_label": t_axis_label, "folder": folder,
            "dur_pairs": dur_pairs, "adapter": ad, "etho": et, "io_pulses": io_pulses, "yl_pulses": yl_pulses}


def draw_timeline(ax, folder, res=None, xmin=None, xmax=None, compact=False):
    """Draw the IO-box / YeLink timeline (pins 4/5 and Mark stamps are not drawn) on the given axes.
    If `res` is an analysis result of this folder with an EthoVision export fit, time zero is the first
    exported video sample (mapped onto the Teensy axis with the fitted offset and rate) and the export's
    in-zone state is drawn as a third trace. Only the plot is referenced to video time; CSV files are untouched.
    xmin / xmax: None = automatic (-5 s to video end + 5 s). Returns a title string."""
    with open(os.path.join(folder, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    edges = []
    with open(os.path.join(folder, "edges.csv"), newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            edges.append((float(r["pc_perf_s"]), int(r["pin"]), int(r["state"])))
    edges.sort()
    rec_start = meta.get("start_perf", edges[0][0] if edges else 0.0)
    rec_end = meta.get("stop_perf", (edges[-1][0] if edges else rec_start + 1.0))
    io, yl = meta.get("io_pin", 2), meta.get("yl_pin", 3)

    et = None
    if res is not None and res.get("etho") and "fit" in res["etho"]:
        et = res["etho"]
    if et is not None:
        fa, fb = et["fit"]["intercept"], et["fit"]["slope"]
        t0 = fa + fb * et["t_first"]            # Teensy-axis time of the first exported video sample
        t_end = fa + fb * et["t_last"]
        xlabel = "Time since video start (s)" if compact else \
            f"Time since video start (s); zero = first exported sample, {et['time_name']} = {et['t_first']:.3f} s"
    else:
        t0, t_end = rec_start, rec_end
        xlabel = "Time since recording start (s)" if compact else \
            "Time since recording start (s); analyse with the EthoVision export on tab 2 to use video time"

    rows = [(io, "IO box TTL", PIN_COLORS.get(io, "k")), (yl, "YeLink TTL", PIN_COLORS.get(yl, "k"))]
    for i, (p, lab, col) in enumerate(rows):
        base = i * 1.5
        ts, vs, state = [rec_start - t0], [0], 0
        for t, pin, st in edges:
            if pin != p:
                continue
            ts.append(t - t0)
            vs.append(st)
            state = st
        ts.append(rec_end - t0)
        vs.append(state)
        ax.step(ts, [base + v for v in vs], where="post", color=col, lw=1.0 if compact else 1.2)
    ylabels = [lab for _, lab, _ in rows]
    if et is not None:
        base = len(rows) * 1.5
        ts, vs, state = [0.0], [0], 0
        for te, z, i in et["events"]:
            ts.append(fb * (te - et["t_first"]))
            vs.append(z)
            state = z
        ts.append(t_end - t0)
        vs.append(state)
        ax.step(ts, [base + v for v in vs], where="post", color="#d62728", lw=1.0 if compact else 1.2)
        ylabels.append("EthoVision\nin zone" if compact else "EthoVision in zone\n(track export)")
    n_rows = len(ylabels)
    ax.set_xlabel(xlabel)
    ax.set_yticks([i * 1.5 + 0.5 for i in range(n_rows)])
    ax.set_yticklabels(ylabels, fontsize=8 if compact else None)
    ax.set_ylim(-0.5, n_rows * 1.5)
    if xmin is None:
        xmin = -5.0
    if xmax is None:
        xmax = (t_end - t0) + 5.0
    if xmax <= xmin:
        xmax = xmin + 1.0
    ax.set_xlim(xmin, xmax)
    if et is not None:
        ax.axvline(0, color="gray", lw=0.8, ls=":")
        ax.axvline(t_end - t0, color="gray", lw=0.8, ls=":")
    ax.grid(True, axis="x", alpha=0.3)
    title = f"Timeline: {os.path.basename(folder)}"
    if et is not None:
        title += f"   (export mapped with slope {fb:.5f}; mean IO-box lag removed by the fit)"
    return title


COL_ON, COL_OFF = "#1f77b4", "#ff7f0e"


def plot_results(fig, res, xmin=None, xmax=None):
    """Four-panel figure. A timeline; B relative latency over the session; C its distribution;
    D pulse duration YeLink vs IO box."""
    fig.clf()
    ax = fig.subplots(2, 2)
    lab_on = f"ON (Zone Entry, n = {len(res['d_on'])})"
    lab_off = f"OFF (Zone Exit, n = {len(res['d_off'])})"

    # A. timeline
    a = ax[0, 0]
    folder = res.get("folder")
    if folder and os.path.isfile(os.path.join(folder, "edges.csv")):
        draw_timeline(a, folder, res, xmin, xmax, compact=True)
    a.set_title("A. Output timeline")

    # B. relative latency across the session
    a = ax[0, 1]
    if res["d_on"]:
        a.scatter(res["d_on_t"], res["d_on"], s=10, color=COL_ON, label=lab_on)
    if res["d_off"]:
        a.scatter(res["d_off_t"], res["d_off"], s=10, color=COL_OFF, label=lab_off)
    a.axhline(0, color="k", lw=0.8, ls="--")
    a.set_title("YeLink–IO Box Offset by Event")
    a.set_xlabel(res.get("t_axis_label", "Time since recording start (s)"))
    a.set_ylabel("YeLink − IO Box Edge-Time Offset (ms)")
    a.set_xlim(xmin,xmax)
    if res["d_on"] or res["d_off"]:
        a.legend(fontsize=7)

    # C. distribution of the same quantity
    a = ax[1, 0]
    allv = res["d_on"] + res["d_off"]
    if allv:
        bins = np.linspace(min(allv), max(allv), 41) if max(allv) > min(allv) else 20
        if res["d_on"]:
            a.hist(res["d_on"], bins=bins, color=COL_ON, alpha=0.8, label=lab_on)
        if res["d_off"]:
            a.hist(res["d_off"], bins=bins, color=COL_OFF, alpha=0.6, label=lab_off)
        a.legend(fontsize=7)
    a.axvline(0, color="k", lw=0.8, ls="--")
    a.set_title("YeLink–IO Box Offset Distribution")
    a.set_xlabel("YeLink − IO Box Edge-Time Offset (ms)")
    a.set_ylabel("Number of events")

    # D. pulse duration
    a = ax[1, 1]
    if res["dur_pairs"]:
        xi = np.array([i for i, y in res["dur_pairs"]])
        yi = np.array([y for i, y in res["dur_pairs"]])
        a.scatter(xi, yi, s=12, color="#2ca02c", label=f"paired pulses (n = {xi.size})")
        m = max(xi.max(), yi.max())
        if xi.size >= 2 and xi.std() > 0 and yi.std() > 0:
            r2 = float(np.corrcoef(xi, yi)[0, 1] ** 2)
            line_lab = f"R$^2$ = {r2:.4f}"
        else:
            line_lab = "y = x"
        a.plot([0, m], [0, m], "k--", lw=0.8, label=line_lab)
        a.legend(fontsize=7)
    a.set_title("YeLink vs. IO Box Pulse Duration")
    a.set_xlabel("IO Box pulse duration (s)")
    a.set_ylabel("YeLink pulse duration (s)")
    fig.tight_layout()


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("YeLink Test 3 - EthoVision closed loop: YeLink vs IO box")
        self.geometry("1300x880")
        self.q = queue.Queue()
        self.stop_evt = threading.Event()
        self.worker = None
        self.tz = None
        self.live_clock = None
        self.run_t0 = None
        self.marks = []
        self.etho_cache = None
        self.mon = None
        self.mon_seen = 0
        self.last_res = None
        self.last_res_folder = None

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)
        self.tab_run = ttk.Frame(nb)
        self.tab_an = ttk.Frame(nb)
        nb.add(self.tab_run, text="1. Record")
        nb.add(self.tab_an, text="2. Analyze")
        self._build_run_tab()
        self._build_an_tab()
        self.after(100, self._poll_queue)
        self.after(500, self._update_live)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- Tab 1 ----------------
    def _build_run_tab(self):
        f = self.tab_run
        hw = ttk.LabelFrame(f, text="Teensy edge logger")
        hw.pack(fill="x", padx=10, pady=6)
        ttk.Label(hw, text="Teensy COM").grid(row=0, column=0, padx=6, pady=6, sticky="e")
        self.var_com = tk.StringVar()
        self.cbo_com = ttk.Combobox(hw, textvariable=self.var_com, width=28, state="readonly")
        self.cbo_com.grid(row=0, column=1, sticky="w")
        ttk.Button(hw, text="Refresh", command=self.refresh_ports).grid(row=0, column=2, padx=4)
        ttk.Button(hw, text="Test Teensy", command=self.test_teensy).grid(row=0, column=3, padx=4)
        self.btn_mon = ttk.Button(hw, text="Monitor levels", command=self.toggle_monitor)
        self.btn_mon.grid(row=0, column=4, padx=4)
        self.lbl_teensy = ttk.Label(hw, text="not tested", foreground="gray")
        self.lbl_teensy.grid(row=0, column=5, padx=6, sticky="w")

        ttk.Label(hw, text="IO box TTL on Teensy pin").grid(row=1, column=0, padx=6, pady=4, sticky="e")
        self.var_io_pin = tk.StringVar(value="2")
        self.var_yl_pin = tk.StringVar(value="3")
        rp = ttk.Frame(hw)
        rp.grid(row=1, column=1, columnspan=4, sticky="w")
        ttk.Combobox(rp, textvariable=self.var_io_pin, values=[str(p) for p in PINS], width=3, state="readonly").pack(side="left")
        ttk.Label(rp, text="    YeLink (Uno pin 8) on Teensy pin").pack(side="left")
        ttk.Combobox(rp, textvariable=self.var_yl_pin, values=[str(p) for p in PINS], width=3, state="readonly").pack(side="left", padx=4)
        ttk.Label(rp, text="   (each line through 10k series / 20k to GND; grounds shared)", foreground="gray").pack(side="left")

        st = ttk.LabelFrame(f, text="Recording")
        st.pack(fill="x", padx=10, pady=6)
        self.var_out = tk.StringVar(value=os.path.join(os.getcwd(), "results"))
        self.var_note = tk.StringVar(value="")
        self.var_live = tk.BooleanVar(value=False)
        ttk.Label(st, text="Output folder").grid(row=0, column=0, padx=6, pady=4, sticky="e")
        ttk.Entry(st, textvariable=self.var_out, width=80).grid(row=0, column=1, columnspan=3, sticky="we")
        ttk.Button(st, text="Browse...", command=self._browse_out).grid(row=0, column=4, padx=4)
        ttk.Label(st, text="Note (video, rule, build)").grid(row=1, column=0, padx=6, pady=4, sticky="e")
        ttk.Entry(st, textvariable=self.var_note, width=80).grid(row=1, column=1, columnspan=3, sticky="we")
        ttk.Checkbutton(st, text="Live plot during recording (off = cleanest timing)", variable=self.var_live).grid(row=1, column=4, padx=4)
        st.columnconfigure(1, weight=1)

        bar = ttk.Frame(f)
        bar.pack(fill="x", padx=10, pady=4)
        self.btn_run = ttk.Button(bar, text="Start recording", command=self.start_run)
        self.btn_run.pack(side="left", padx=4)
        self.btn_stop = ttk.Button(bar, text="Stop and save", command=self.stop_run, state="disabled")
        self.btn_stop.pack(side="left", padx=4)
        ttk.Label(bar, text="   Mark label:").pack(side="left")
        self.var_mark = tk.StringVar(value="EthoVision Start pressed")
        ttk.Entry(bar, textvariable=self.var_mark, width=30).pack(side="left", padx=4)
        self.btn_mark = ttk.Button(bar, text="Mark", command=self.add_mark, state="disabled")
        self.btn_mark.pack(side="left", padx=4)
        self.lbl_progress = ttk.Label(bar, text="idle")
        self.lbl_progress.pack(side="left", padx=12)

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
        ttk.Button(pbar, text="Plot full timeline (after the recording)", command=self.plot_timeline).pack(side="left", padx=4)
        ttk.Label(pbar, text="  x from").pack(side="left")
        self.var_xmin = tk.StringVar(value="-5")
        self.var_xmax = tk.StringVar(value="")
        ttk.Entry(pbar, textvariable=self.var_xmin, width=7).pack(side="left", padx=2)
        ttk.Label(pbar, text="to").pack(side="left")
        ttk.Entry(pbar, textvariable=self.var_xmax, width=7).pack(side="left", padx=2)
        ttk.Label(pbar, text="s  (blank = end + 5 s)", foreground="gray").pack(side="left")
        ttk.Label(pbar, text="   Toolbar below the plot: zoom / save.", foreground="gray").pack(side="left", padx=8)
        self.fig_live = Figure(figsize=(7, 5), dpi=100)
        self.ax_live = self.fig_live.add_subplot(111)
        self.canvas_live = FigureCanvasTkAgg(self.fig_live, master=right)
        tb_live = NavigationToolbar2Tk(self.canvas_live, right)
        tb_live.update()
        tb_live.pack(side="top", fill="x")
        self.canvas_live.get_tk_widget().pack(fill="both", expand=True)
        self._init_live_axes()
        self.refresh_ports()
        self.log("Sequence: Test Teensy -> Start recording -> (Mark) start acquisition in EthoVision -> "
                 "trial runs -> Stop and save -> export adapter log and EthoVision track -> tab 2.")

    def _pin_labels(self):
        io, yl = self.var_io_pin.get(), self.var_yl_pin.get()
        out = []
        for p in PINS:
            role = " IO box" if str(p) == io else (" YeLink" if str(p) == yl else "")
            out.append(f"pin {p}{role}")
        return out

    def _init_live_axes(self):
        a = self.ax_live
        a.clear()
        self.fig_live.suptitle("")
        a.set_title("Live: pin levels from the Teensy")
        a.set_xlabel("seconds since recording start")
        a.set_yticks([i * 1.5 + 0.5 for i in range(len(PINS))])
        a.set_yticklabels(self._pin_labels())
        a.set_ylim(-0.5, len(PINS) * 1.5)
        a.grid(True, axis="x", alpha=0.3)
        self.canvas_live.draw_idle()

    def log(self, msg):
        self.txt_log.insert("end", msg + "\n")
        self.txt_log.see("end")

    def _browse_file(self, var, types):
        p = filedialog.askopenfilename(filetypes=types)
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
                self.log("  Note: some pins are high. Fine if the IO box idles high (use 'invert IO box' on tab 2); "
                         "otherwise turn outputs off before starting.")
        except Exception as e:
            self.lbl_teensy.config(text="failed", foreground="red")
            self.log(f"Teensy test FAILED: {e}")

    # ---------------- live monitor (diagnostics, no recording) ----------------
    def toggle_monitor(self):
        if self.mon is not None:
            self._stop_monitor()
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Busy", "Stop the recording first.")
            return
        try:
            self.mon = Teensy(self._com())
        except Exception as e:
            self.log(f"Monitor: cannot open the Teensy: {e}")
            self.mon = None
            return
        self.mon_seen = 0
        self.btn_mon.config(text="Stop monitor")
        self.btn_run.config(state="disabled")
        self.log("Monitor: showing live pin levels. Drive each line by hand (adapter: p=8 v=1 d=0 / p=8 v=0; "
                 "EthoVision hardware test dialog for the IO box) and check the matching Teensy pin follows.")
        self._monitor_tick()

    def _stop_monitor(self):
        if self.mon is not None:
            try:
                self.mon.close()
            except Exception:
                pass
        self.mon = None
        self.btn_mon.config(text="Monitor levels")
        self.btn_run.config(state="normal")
        self.log("Monitor stopped.")

    def _monitor_tick(self):
        if self.mon is None:
            return
        try:
            self.mon.ser.write(b"Q")
        except Exception as e:
            self.log(f"Monitor: serial error: {e}")
            self._stop_monitor()
            return
        lv = dict(self.mon.levels)
        edges = self.mon.edges
        n = len(edges)
        if n > self.mon_seen:
            for us, pin, st, _ in edges[self.mon_seen:n][-10:]:
                self.log(f"  edge: Teensy pin {pin} -> {st}   (us={us})")
            self.mon_seen = n
        io, yl = self.var_io_pin.get(), self.var_yl_pin.get()
        txt = "  ".join(f"pin{p}={lv.get(p, '?')}" + ("(IO)" if str(p) == io else "(YL)" if str(p) == yl else "")
                        for p in PINS)
        self.lbl_teensy.config(text=f"LIVE  {txt}   edges {n}", foreground="blue")
        self.after(500, self._monitor_tick)

    # ---------------- recording control ----------------
    def start_run(self):
        if self.worker and self.worker.is_alive():
            return
        if self.mon is not None:
            self._stop_monitor()
        try:
            com = self._com()
        except ValueError as e:
            messagebox.showerror("Input error", str(e))
            return
        if self.var_io_pin.get() == self.var_yl_pin.get():
            messagebox.showerror("Input error", "IO box and YeLink must be on different Teensy pins.")
            return
        label = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        outdir = os.path.join(self.var_out.get().strip() or os.getcwd(), f"test3_{label}")
        os.makedirs(outdir, exist_ok=True)
        cfg = {"com": com, "label": label, "outdir": outdir, "io_pin": int(self.var_io_pin.get()),
               "yl_pin": int(self.var_yl_pin.get()), "note": self.var_note.get().strip()}
        self.stop_evt.clear()
        self.marks = []
        self.live_clock = None
        self.btn_run.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.btn_mark.config(state="normal")
        self._init_live_axes()
        self.log(f"Starting recording {label} -> {outdir}")
        self.worker = threading.Thread(target=self._worker, args=(cfg,), daemon=True)
        self.worker.start()

    def stop_run(self):
        self.stop_evt.set()
        self.btn_stop.config(state="disabled")
        self.btn_mark.config(state="disabled")
        self.log("Stop requested; final clock sync and file writing...")

    def add_mark(self):
        t = time.perf_counter()
        lab = self.var_mark.get().strip() or "mark"
        self.marks.append((t, lab))
        rel = (t - self.run_t0) if self.run_t0 else 0.0
        self.log(f"Mark at {rel:.3f} s: {lab}")

    def _worker(self, cfg):
        tz = None
        try:
            tz = Teensy(cfg["com"])
            self.tz = tz
            levels = tz.query_levels()
            self.q.put(("log", f"Teensy pin levels at start: {levels}"))
            w_off = wall_perf_offset()
            sync_samples = tz.sync(30)
            if len(sync_samples) < 5:
                self.q.put(("log", "ABORT: the Teensy did not answer clock-sync queries."))
                self.q.put(("done", None))
                return
            self.live_clock = (sync_samples[0][1], sync_samples[0][0])
            rtt = statistics.median(s[2] for s in sync_samples) * 1000
            self.q.put(("log", f"Initial clock sync: {len(sync_samples)} samples, RTT median {rtt:.2f} ms"))
            start_perf = time.perf_counter()
            self.run_t0 = start_perf
            self.q.put(("log", "RECORDING. Start acquisition in EthoVision now. Press 'Stop and save' after the trial."))
            last_sync = time.perf_counter()
            n_edges_seen = 0
            while not self.stop_evt.is_set():
                time.sleep(0.05)
                now = time.perf_counter()
                if now - last_sync >= SYNC_EVERY_S:
                    sync_samples += tz.sync(5)
                    last_sync = now
                    n = len(tz.edges)
                    if n != n_edges_seen:
                        n_edges_seen = n
                    self.q.put(("progress", now - start_perf, n))
            time.sleep(0.3)
            sync_samples += tz.sync(30)
            stop_perf = time.perf_counter()
            levels_end = tz.query_levels()
            edges = list(tz.edges)
            tz.close()
            self.tz = None
            clk = fit_clock(sync_samples)
            outdir = cfg["outdir"]
            meta = {"label": cfg["label"], "teensy": cfg["com"], "io_pin": cfg["io_pin"], "yl_pin": cfg["yl_pin"],
                    "note": cfg["note"], "wall_minus_perf_s": w_off, "clock": clk, "start_perf": start_perf,
                    "stop_perf": stop_perf, "start_wall": datetime.fromtimestamp(start_perf + w_off).isoformat(sep=" "),
                    "levels_start": levels, "levels_end": levels_end, "teensy_other": tz.other[:50],
                    "match_lead_s": MATCH_LEAD, "match_window_s": MATCH_WINDOW}
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
            with open(os.path.join(outdir, "marks.csv"), "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["pc_perf_s", "pc_wall", "label"])
                for t, lab in self.marks:
                    w.writerow([f"{t:.6f}", datetime.fromtimestamp(t + w_off).strftime("%Y-%m-%d %H:%M:%S.%f"), lab])
            with open(os.path.join(outdir, "meta.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
            self.q.put(("log", f"Clock fit: {clk['n']} samples, RTT median {clk['rtt_median_ms']:.2f} ms, "
                               f"residual RMS {clk['resid_rms_us']:.0f} us, drift {clk['drift_ppm']:.1f} ppm"))
            self.q.put(("log", f"Recorded {stop_perf - start_perf:.1f} s, {len(edges)} edges. Files written to {outdir}"))
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
                    _, secs, n = item
                    self.lbl_progress.config(text=f"recording {secs:6.0f} s, {n} edges")
                elif kind == "done":
                    self.btn_run.config(state="normal")
                    self.btn_stop.config(state="disabled")
                    self.btn_mark.config(state="disabled")
                    folder = item[1]
                    if folder:
                        self.lbl_progress.config(text="saved")
                        self.var_folder.set(folder)
                        self.log("Saved. Now: Export CSV in YeLink_AdapterUI (before closing it) and export the "
                                 "EthoVision track with 'In zone'; load both on tab 2. Showing the edge-only analysis.")
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
                a.set_title("Live: pin levels from the Teensy")
                a.set_xlabel("seconds since recording start")
                a.set_yticks([i * 1.5 + 0.5 for i in range(len(PINS))])
                a.set_yticklabels(self._pin_labels())
                a.set_ylim(-0.5, len(PINS) * 1.5)
                a.set_xlim(lo, max(now, lo + 1.0))
                a.grid(True, axis="x", alpha=0.3)
                edges = list(self.tz.edges)
                for i, p in enumerate(PINS):
                    base = i * 1.5
                    ts, vs, state = [lo], [0], 0
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
                for t, lab in self.marks:
                    if t - self.run_t0 >= lo:
                        a.axvline(t - self.run_t0, color="k", lw=0.8, ls="--", alpha=0.6)
                self.canvas_live.draw_idle()
        except Exception:
            pass
        self.after(500, self._update_live)

    def plot_timeline(self):
        """Standalone timeline on tab 1 (same drawing as panel A of the analysis figure)."""
        folder = self.var_folder.get().strip()
        if not os.path.isfile(os.path.join(folder, "edges.csv")):
            folder = filedialog.askdirectory(title="Choose a results folder (contains edges.csv and meta.json)")
            if not folder or not os.path.isfile(os.path.join(folder, "edges.csv")):
                return
            self.var_folder.set(folder)
        res = self.last_res if (self.last_res is not None and self.last_res_folder == folder) else None
        xmin, xmax = self._timeline_xrange()
        a = self.ax_live
        a.clear()
        title = draw_timeline(a, folder, res, xmin, xmax, compact=False)
        self.fig_live.suptitle(title, fontsize=9)
        self.fig_live.tight_layout(rect=(0, 0, 1, 0.95))
        self.canvas_live.draw()
        try:
            self.fig_live.savefig(os.path.join(folder, "test3_timeline.png"), dpi=200)
            self.log(f"Timeline saved: {os.path.join(folder, 'test3_timeline.png')}")
        except Exception:
            pass

    def _timeline_xrange(self):
        """(xmin, xmax) from the entry boxes; None = automatic."""
        def parse(var):
            s = var.get().strip()
            if not s:
                return None
            try:
                return float(s)
            except ValueError:
                return None
        return parse(self.var_xmin), parse(self.var_xmax)

    # ---------------- Tab 2 ----------------
    def _build_an_tab(self):
        f = self.tab_an
        inp = ttk.LabelFrame(f, text="Input")
        inp.pack(fill="x", padx=10, pady=6)
        self.var_folder = tk.StringVar()
        self.var_adlog = tk.StringVar()
        self.var_etho = tk.StringVar()
        self.var_etho_col = tk.StringVar()
        self.var_invert = tk.BooleanVar(value=False)
        ttk.Label(inp, text="Results folder").grid(row=0, column=0, padx=6, pady=4, sticky="e")
        ttk.Entry(inp, textvariable=self.var_folder, width=90).grid(row=0, column=1, sticky="we")
        ttk.Button(inp, text="Browse...", command=self._browse_folder).grid(row=0, column=2, padx=6)
        ttk.Label(inp, text="Adapter log CSV (optional)").grid(row=1, column=0, padx=6, pady=4, sticky="e")
        ttk.Entry(inp, textvariable=self.var_adlog, width=90).grid(row=1, column=1, sticky="we")
        ttk.Button(inp, text="Browse...", command=lambda: self._browse_file(
            self.var_adlog, [("CSV files", "*.csv"), ("All files", "*.*")])).grid(row=1, column=2, padx=6)
        ttk.Label(inp, text="EthoVision track export (optional)").grid(row=2, column=0, padx=6, pady=4, sticky="e")
        ttk.Entry(inp, textvariable=self.var_etho, width=90).grid(row=2, column=1, sticky="we")
        ttk.Button(inp, text="Browse...", command=self._browse_etho).grid(row=2, column=2, padx=6)
        opt = ttk.Frame(inp)
        opt.grid(row=3, column=1, sticky="w")
        ttk.Label(opt, text="In-zone column:").pack(side="left")
        self.cbo_etho_col = ttk.Combobox(opt, textvariable=self.var_etho_col, width=45, state="readonly")
        self.cbo_etho_col.pack(side="left", padx=4)
        ttk.Checkbutton(opt, text="IO box TTL is active-low (invert)", variable=self.var_invert).pack(side="left", padx=16)
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
        self.txt_summary = tk.Text(left, width=72, wrap="word", font=("Consolas", 9))
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

    def _browse_etho(self):
        p = filedialog.askopenfilename(filetypes=[("EthoVision export", "*.xlsx *.txt *.csv"), ("All files", "*.*")])
        if not p:
            return
        self.var_etho.set(p)
        try:
            exp = load_etho_export(p)
            self.cbo_etho_col["values"] = exp["zone_columns"]
            if exp["zone_columns"]:
                self.var_etho_col.set(exp["zone_columns"][0])
            else:
                messagebox.showwarning("No zone column", "No 'In zone' column found in this export.")
        except Exception as e:
            messagebox.showerror("Export read error", str(e))

    def analyze(self):
        folder = self.var_folder.get().strip()
        if not os.path.isfile(os.path.join(folder, "edges.csv")):
            messagebox.showerror("Missing folder", "Choose a results folder that contains edges.csv and meta.json.")
            return
        adlog = self.var_adlog.get().strip() or None
        etho = self.var_etho.get().strip() or None
        try:
            res = analyze_folder(folder, io_pin=int(self.var_io_pin.get()), yl_pin=int(self.var_yl_pin.get()),
                                 invert_io=self.var_invert.get(), adapter_log=adlog, etho_path=etho,
                                 etho_col=self.var_etho_col.get().strip() or None)
        except Exception as e:
            import traceback
            messagebox.showerror("Analysis error", "".join(traceback.format_exception_only(type(e), e)))
            return
        self.txt_summary.delete("1.0", "end")
        self.txt_summary.insert("1.0", res["text"])
        self.last_res = res
        self.last_res_folder = folder
        xmin, xmax = self._timeline_xrange()
        plot_results(self.fig, res, xmin, xmax)
        self.canvas.draw()
        with open(os.path.join(folder, "summary.txt"), "w", encoding="utf-8") as f:
            f.write(res["text"] + "\n")
        with open(os.path.join(folder, "pairs.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["event", "t_rel_s", "yelink_minus_iobox_ms"])
            for t, d in zip(res["d_on_t"], res["d_on"]):
                w.writerow(["ON", f"{t:.3f}", f"{d:.3f}"])
            for t, d in zip(res["d_off_t"], res["d_off"]):
                w.writerow(["OFF", f"{t:.3f}", f"{d:.3f}"])
        try:
            self.fig.savefig(os.path.join(folder, "test3_figure.png"), dpi=200)
        except Exception:
            pass

    def save_figure(self):
        p = filedialog.asksaveasfilename(defaultextension=".png", filetypes=[("PNG image", "*.png")],
                                         initialfile="test3_figure.png")
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
        if self.mon is not None:
            try:
                self.mon.close()
            except Exception:
                pass
        if self.tz is not None:
            try:
                self.tz.close()
            except Exception:
                pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()