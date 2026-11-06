# -*- coding: utf-8 -*-
"""
Validation4_multiarena_gui.py  --  YeLink validation 4: four EthoVision arenas driving four YeLink pins (GUI)

NEW FILE 2026-11-04. Recorder + analyser. Reuses the Teensy link, clock fit and GUI style of
test3_closedloop_gui.py. The Teensy sketch YeLink_EdgeLogger_Teensy.ino is used UNCHANGED.

Design of the test
  One 10 min / 30 fps video shows four 40 x 40 cm chambers (2 x 2) with one mouse each. In EthoVision XT
  four arenas are defined, one zone per arena at the same place (zone names differ: Zone A in arena 1 ...
  Zone D in arena 4). Trial control fires an external command (YeLink_Controller.exe) per arena:
      enter Zone A (arena 1) -> p=2 v=1 d=0 ; leave Zone A -> p=2 v=0
      ...                                     p=3 for Zone B, p=4 for Zone C, p=5 for Zone D
  (latch on entry, off on exit, as in validation 3).  Uno pins 2-5 are recorded on Teensy pins 2-5.
  Then the EthoVision raw-data export (one file / sheet per arena, with the "In zone" variables) is loaded
  and every arena's zone entries / exits are compared with the edges on its own pin.

Hardware (as in validation 2/3)
  Teensy 4.0 + YeLink_EdgeLogger_Teensy.ino (monitors Teensy pins 2,3,4,5).
  Each line: Uno pin N -[10k]- Teensy pin N -[20k]- GND, grounds shared (Teensy pins are NOT 5 V tolerant).

Tab 1 "Record"
  Choose the COM port; tick the pins to analyse and give each a name (e.g. "Arena 1 / Zone A").
  Start recording BEFORE starting acquisition in EthoVision, press Stop after the trial ended.
  "Mark" stamps a labelled time into marks.csv. Live plot is off by default (cleanest timing).
  Files: edges.csv (all four pins), sync.csv, marks.csv, meta.json in results_multiarena\multiarena_<timestamp>\.

Tab 2 "Analyze"
  Results folder + EthoVision export file(s) (one workbook with one sheet per arena, or one file per arena;
  .xlsx or text). The GUI proposes which "In zone" column belongs to which pin (from "Arena n" / "Zone X" in
  the channel name); correct it in the drop-downs if needed. Optional: adapter log CSV (command count per pin).

  Alignment (no shared clock exists between EthoVision and the Teensy):
    1. first-onset anchor: for every pin, first ON edge minus first zone entry; the median is the starting offset,
       and the four per-pin values are reported (they should agree within a few frames);
    2. all entries/exits are paired with the nearest edge of the same type on the SAME pin within the tolerance
       and  edge time = a + b x EthoVision time  is fitted on all pairs of all arenas (offset a, rate b; the
       pairing is repeated after each fit). b must be 1.000 (video processed in real time);
    3. per pin: counts, unpaired events, offset SD; a pin-vs-zone cross-talk matrix (does an edge on pin p
       follow zone events of arena c != p?) with coincident events of two arenas excluded.
  The offset a absorbs the mean EthoVision-to-pin lag, so ABSOLUTE latency is not measured, as in validation 3.

Requirements: Python 3.8+, pyserial, numpy, matplotlib; openpyxl only for .xlsx exports (all in Anaconda).
"""
import csv
import json
import os
import queue
import re
import statistics
import sys
import threading
import time
from datetime import datetime

import numpy as np

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
    HAVE_TK = True
except Exception:                      # headless (tests): analysis functions still work
    tk = None
    HAVE_TK = False
from matplotlib.figure import Figure

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None

sys.setswitchinterval(0.001)

PINS = [2, 3, 4, 5]
PIN_COLORS = {2: "#1f77b4", 3: "#2ca02c", 4: "#ff7f0e", 5: "#9467bd"}
DEFAULT_NAMES = {2: "Arena 1 / Zone A", 3: "Arena 2 / Zone B", 4: "Arena 3 / Zone C", 5: "Arena 4 / Zone D"}
SYNC_EVERY_S = 10.0
DEFAULT_TOL_MS = 150.0       # first-pass pairing tolerance around the predicted edge time
FRAME_MS = 1000.0 / 30.0     # one video frame at 30 fps (33.3 ms): events further than this from the line are "late" and do not enter the alignment fit
TRIM_MS = FRAME_MS


# ----------------------------------------------------------------------
# Teensy link (same as test3_closedloop_gui.py)
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
# Edge / pulse helpers
# ----------------------------------------------------------------------

def stat(name, vals, unit="ms"):
    a = np.asarray([v for v in vals if v is not None and np.isfinite(v)], dtype=float)
    if a.size == 0:
        return f"{name}: no data"
    q1, q3 = np.percentile(a, [25, 75])
    return (f"{name}: n={a.size}, median {np.median(a):.2f}, IQR {q1:.2f} to {q3:.2f}, "
            f"min {a.min():.2f}, max {a.max():.2f} {unit}")


def pulses_from_edges(edges):
    """edges: sorted list of (t, state). Returns (pulses [dict rise, fall|None], notes)."""
    notes = []
    pulses = []
    cur = None
    for t, s in edges:
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
        notes.append(f"pin still high at end of recording (rise {cur['rise']:.3f})")
        pulses.append(cur)
    return pulses, notes


def load_adapter_log_counts(path):
    """Count commands per pin and value in an adapter CSV export (TCP rows only; batches split on ';')."""
    counts = {}
    n_rows = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("Source", "").strip().upper() != "TCP":
                continue
            n_rows += 1
            for part in r.get("Received", "").replace("\n", ";").split(";"):
                p = re.search(r"\bp\s*=\s*(\d+)", part)
                v = re.search(r"\bv\s*=\s*(\d+)", part)
                if p and v:
                    key = (int(p.group(1)), int(v.group(1)))
                    counts[key] = counts.get(key, 0) + 1
    return counts, n_rows


# ----------------------------------------------------------------------
# EthoVision export loading (several arenas)
# ----------------------------------------------------------------------

def _cell_str(v):
    if v is None:
        return ""
    return str(v).strip()


def _rows_to_source(rows, label):
    hdr_i = None
    for i, r in enumerate(rows[:300]):
        low = [c.lower() for c in r[:4]]
        if any(c in ("trial time", "recording time") for c in low):
            hdr_i = i
            break
    if hdr_i is None:
        return None
    names = rows[hdr_i]
    data_i = hdr_i + 1
    if data_i < len(rows) and rows[data_i] and rows[data_i][0].lower() in ("s", "sec", ""):
        data_i += 1
    header_info = rows[:hdr_i]
    cols = {}
    for n in names:
        if n and n not in cols:
            cols[n] = []
    for r in rows[data_i:]:
        if not r or not r[0]:
            continue
        seen = set()
        for k, n in enumerate(names):
            if n and n not in seen:
                seen.add(n)
                cols[n].append(r[k] if k < len(r) else "")
    tname = "Trial time" if "Trial time" in cols else "Recording time"
    t = []
    for v in cols[tname]:
        try:
            t.append(float(v.replace(",", ".")))
        except ValueError:
            t.append(np.nan)
    arena_text = ""
    for r in header_info:
        if r and r[0].lower().startswith("arena name") and len(r) > 1:
            arena_text = r[1]
            break
    m = re.search(r"(\d+)\s*$", arena_text) or re.search(r"arena\s*[-_ ]?(\d+)", label, re.I)
    return {"label": label, "time": np.array(t, dtype=float), "time_name": tname, "columns": cols,
            "header_info": header_info, "arena_text": arena_text,
            "arena_num": int(m.group(1)) if m else None,
            "zone_columns": [n for n in cols if n.lower().startswith("in zone")]}


def load_etho_sources(path):
    """Read an EthoVision raw-data export (.xlsx with one or several sheets, or a text export).
    Returns a list of sources (one per sheet that contains a 'Trial time' table)."""
    ext = os.path.splitext(path)[1].lower()
    base = os.path.basename(path)
    out = []
    if ext in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError:
            raise RuntimeError("openpyxl is needed to read .xlsx exports (or export from EthoVision as text).")
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        for sn in wb.sheetnames:
            rows = [[_cell_str(v) for v in r] for r in wb[sn].iter_rows(values_only=True)]
            src = _rows_to_source(rows, f"{base} [{sn}]")
            if src is not None:
                out.append(src)
        wb.close()
    else:
        rows = []
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
            sample = f.read(4096)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
            except csv.Error:
                dialect = csv.excel
            for r in csv.reader(f, dialect):
                rows.append([_cell_str(v) for v in r])
        src = _rows_to_source(rows, base)
        if src is not None:
            out.append(src)
    if not out:
        raise RuntimeError(f"No 'Trial time' / 'Recording time' table found in {base}.")
    return out


def zone_events(src, col):
    """Entry / exit events from an 'In zone' column. Missing samples keep the previous state.
    A mouse already in the zone at the first valid sample is counted as an entry at that sample (initial=True).
    Returns (events [(t, 1 entry | 0 exit)], initial_in, frame_dt)."""
    t = src["time"]
    state = None
    initial_in = False
    events = []
    for i, v in enumerate(src["columns"][col]):
        s = v.strip()
        if s in ("", "-"):
            continue
        try:
            z = 1 if float(s.replace(",", ".")) >= 0.5 else 0
        except ValueError:
            continue
        if state is None:
            state = z
            initial_in = bool(z)
            if z:                      # EthoVision fires the 'enter zone' rule at track start (seen in the first real run)
                events.append((float(t[i]), 1))
            continue
        if z != state:
            events.append((float(t[i]), z))
            state = z
    frame_dt = float(np.nanmedian(np.diff(t))) if len(t) > 2 else float("nan")
    return events, initial_in, frame_dt


def auto_assign(channels, sources):
    """channels: {pin: name}; returns {pin: (src_index, column) or None} from 'Arena n' / 'Zone X' in the names."""
    out = {}
    for pin, name in channels.items():
        ar = re.search(r"arena\s*[-_ ]?(\d+)", name, re.I)
        zn = re.search(r"zone\s*[-_ ]?([A-Za-z0-9]+)", name, re.I)
        best, best_score = None, 0
        for si, s in enumerate(sources):
            for col in s["zone_columns"]:
                score = 0
                # a column that is ever 'in zone' wins a tie: with identical or unnamed zones in all arenas, the active column is the arena's own zone
                if any(v.strip() in ("1", "1.0") for v in s["columns"][col]):
                    score += 1
                if zn and re.search(r"zone\s*[-_ ]?" + re.escape(zn.group(1)) + r"(?![A-Za-z0-9])", col, re.I):
                    score += 2
                if ar and s["arena_num"] is not None and s["arena_num"] == int(ar.group(1)):
                    score += 2
                if score > best_score:
                    best, best_score = (si, col), score
        out[pin] = best
    return out


# ----------------------------------------------------------------------
# Alignment and analysis
# ----------------------------------------------------------------------

def match_nearest(pred, cand, tol):
    """For each predicted time take the nearest unused candidate within tol (s). Returns list of index|None."""
    used = [False] * len(cand)
    out = []
    for p in pred:
        bj, bd = None, None
        for j, c in enumerate(cand):
            if used[j]:
                continue
            d = abs(c - p)
            if d <= tol and (bd is None or d < bd):
                bj, bd = j, d
        if bj is not None:
            used[bj] = True
        out.append(bj)
    return out


def match_in_order(ex, edges, a, b, early=0.1, late=0.4):
    """Order-preserving pairing for one pin. ex: [(t_export, 'ON'|'OFF')]; edges: [(t, 'ON'|'OFF', index)].
    EthoVision launches one process per command and the commands reach the pin in order, but a burst of events
    (zone flicker over a few frames) can be delayed by 100-200 ms, so the window is asymmetric: an edge may come
    `early` s before and `late` s after the predicted time (default 0.1 / 1.0 s; offsets above 0.4 s are flagged in the summary). Each export event takes the FIRST unused edge of the
    same kind inside the window that is later than the previously matched edge.
    Returns [(t_export, kind, t_edge|None, index|None)]."""
    edges = sorted(edges)
    used = [False] * len(edges)
    last_t = -1e18
    out = []
    for t_ex, kind in sorted(ex):
        pred = a + b * t_ex
        hit = None
        for k, (te, kd, idx) in enumerate(edges):
            if used[k] or kd != kind or te <= last_t:
                continue
            if te < pred - early:
                continue
            if te > pred + late:
                break
            hit = k
            break
        if hit is None:
            out.append((t_ex, kind, None, None))
        else:
            used[hit] = True
            last_t = edges[hit][0]
            out.append((t_ex, kind, edges[hit][0], edges[hit][2]))
    return out


def load_recording(folder):
    with open(os.path.join(folder, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
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
    return meta, edges, marks


def channels_from_meta(meta):
    ch = meta.get("channels")
    if ch:
        return {int(p): c["name"] for p, c in ch.items() if c.get("enabled", True)}
    return dict(DEFAULT_NAMES)


def analyze_folder(folder, sources=None, mapping=None, adapter_log=None, tol_ms=DEFAULT_TOL_MS, names=None):
    """sources: list from load_etho_sources (several files concatenated); mapping {pin: (src_idx, col)|None}."""
    meta, edges, marks = load_recording(folder)
    chans = dict(names) if names else channels_from_meta(meta)
    pins = sorted(chans)
    rec_start = meta.get("start_perf", edges[0][0] if edges else 0.0)
    rec_end = meta.get("stop_perf", edges[-1][0] if edges else rec_start + 1.0)
    pin_edges = {p: [(t, s) for t, pp, s in edges if pp == p] for p in pins}
    pulses, notes = {}, []
    for p in pins:
        pulses[p], nn = pulses_from_edges(pin_edges[p])
        notes += [f"{chans[p]} (pin {p}): {n}" for n in nn]

    clk = meta["clock"]
    lines = [f"YeLink validation 4 (four arenas) - recording {meta['label']}",
             f"Recording length: {rec_end - rec_start:.1f} s   edges on all pins: {len(edges)}   marks: {len(marks)}",
             f"Clock sync: {clk['n']} samples, RTT median {clk['rtt_median_ms']:.2f} ms, "
             f"fit residual RMS {clk['resid_rms_us']:.0f} us, drift {clk['drift_ppm']:.1f} ppm",
             f"Note: {meta.get('note', '')}" if meta.get("note") else "Note: -", "",
             "Channels (Teensy pin = Uno pin): " + "; ".join(f"pin {p} = {chans[p]}" for p in pins),
             "Output pulses detected (ON edge to OFF edge): " +
             ", ".join(f"pin {p}: {len(pulses[p])}" for p in pins)]
    lv = meta.get("levels_start", {})
    if any(str(v) == "1" for v in lv.values()):
        lines.append(f"WARNING: pins were already high at the start of the recording: {lv}")

    res = {"folder": folder, "meta": meta, "chans": chans, "pins": pins, "pin_edges": pin_edges,
           "pulses": pulses, "rec_start": rec_start, "rec_end": rec_end, "aligned": False, "marks": marks,
           "pairs": [], "dur_pairs": [], "channel_rows": [], "xtalk": None, "frame_dt": None}

    # ---- adapter log (optional) ----
    if adapter_log:
        counts, n_rows = load_adapter_log_counts(adapter_log)
        lines += ["", f"Adapter log: {n_rows} TCP rows. Commands received vs edges recorded per pin:"]
        for p in pins:
            n_on = counts.get((p, 1), 0)
            n_off = counts.get((p, 0), 0)
            e_on = sum(1 for _, s in pin_edges[p] if s == 1)
            e_off = sum(1 for _, s in pin_edges[p] if s == 0)
            lines.append(f"  pin {p}: v=1 commands {n_on} vs rising edges {e_on};  v=0 commands {n_off} vs falling edges {e_off}")

    if not sources or not mapping:
        lines += ["", "No EthoVision export loaded: edge-only summary. Load the export on tab 2 for the alignment."]
        res["text"] = "\n".join(lines + _notes_marks(notes, marks, rec_start))
        return res

    # ---- zone events per mapped channel ----
    data = {}
    frame_dts = []
    for p in pins:
        m = mapping.get(p)
        if not m:
            lines.append(f"pin {p} ({chans[p]}): no In-zone column assigned - skipped")
            continue
        src = sources[m[0]]
        ev, init_in, fdt = zone_events(src, m[1])
        frame_dts.append(fdt)
        data[p] = {"src": src, "col": m[1], "ev": ev, "init_in": init_in,
                   "ent": [t for t, z in ev if z == 1], "ext": [t for t, z in ev if z == 0],
                   "on": [pu["rise"] for pu in pulses[p]],
                   "off": [pu["fall"] for pu in pulses[p] if pu["fall"] is not None]}
    if not data:
        res["text"] = "\n".join(lines + ["", "No channel has an assigned EthoVision column."])
        return res
    dpins = sorted(data)
    frame_dt = float(np.nanmedian(frame_dts)) if frame_dts else float("nan")
    res["frame_dt"] = frame_dt
    tol0 = tol_ms / 1000.0

    # ---- 1. starting offset: histogram vote over all same-pin time differences ----
    diffs = []
    for p in dpins:
        d = data[p]
        for ex, pn in ((d["ent"], d["on"]), (d["ext"], d["off"])):
            if ex and pn:
                diffs.append((np.asarray(pn)[None, :] - np.asarray(ex)[:, None]).ravel())
    if not diffs:
        res["text"] = "\n".join(lines + ["", "Alignment failed: no channel has both an export event and an edge."])
        return res
    diffs = np.concatenate(diffs)
    bw = 0.025
    lo = float(diffs.min())
    nb = int((diffs.max() - lo) / bw) + 3
    h = np.bincount(((diffs - lo) / bw).astype(int), minlength=nb).astype(float)
    h3 = h + np.r_[h[1:], 0] + np.r_[0, h[:-1]]
    k = int(np.argmax(h3))
    near = diffs[np.abs(diffs - (lo + (k + 0.5) * bw)) <= 1.5 * bw]
    a = float(np.median(near))
    b = 1.0
    res["anchor_votes"] = int(h3[k])

    # ---- 2. iterative pairing and global fit ----
    def pair_all(a, b, tol):
        pr = []          # dicts
        for p in dpins:
            d = data[p]
            for kind, ex, pn in (("ON", d["ent"], d["on"]), ("OFF", d["ext"], d["off"])):
                idx = match_nearest([a + b * t for t in ex], pn, tol)
                for t_ex, j in zip(ex, idx):
                    pr.append({"pin": p, "event": kind, "t_ex": t_ex, "t_pin": pn[j] if j is not None else None,
                               "j": j})
        return pr

    tols = [tol0, max(tol0 / 2, 0.05), max(tol0 / 2, 0.05), max(tol0 / 2, 0.05)]
    pr = []
    for tol in tols:
        pr = pair_all(a, b, tol)
        ok = [q for q in pr if q["t_pin"] is not None]
        if len(ok) >= 3 and (max(q["t_ex"] for q in ok) - min(q["t_ex"] for q in ok)) > 1.0:
            bb, aa = np.polyfit([q["t_ex"] for q in ok], [q["t_pin"] for q in ok], 1)
            a, b = float(aa), float(bb)
        elif ok:
            a = float(np.median([q["t_pin"] - q["t_ex"] for q in ok]))
    def final_pairing(a, b):
        """Order-preserving pairing, asymmetric window around the fit."""
        out = []
        for p in dpins:
            d = data[p]
            ed = []
            n_off = 0
            for ip, pu in enumerate(pulses[p]):
                ed.append((pu["rise"], "ON", ip))
                if pu["fall"] is not None:
                    ed.append((pu["fall"], "OFF", n_off))
                    n_off += 1
            exl = [(t, "ON") for t in d["ent"]] + [(t, "OFF") for t in d["ext"]]
            for t_ex, kind, t_pin, j in match_in_order(exl, ed, a, b, early=0.1, late=max(1.0, tol0 * 2)):
                out.append({"pin": p, "event": kind, "t_ex": t_ex, "t_pin": t_pin, "j": j,
                            "initial": bool(kind == "ON" and data[p]["init_in"] and t_ex == data[p]["ev"][0][0])})
        return out

    # robust alignment: events that are not triggered by a zone crossing (initial in-zone state at track start) and
    # events more than TRIM_MS from the line (late commands) do not enter the fit; the zero is set so that the MEDIAN
    # offset of the events used is 0 (the offset carries the mean EthoVision-to-pin delay and is not a latency)
    n_fit = 0
    for _ in range(3):
        pr = final_pairing(a, b)
        use = [q for q in pr if q["t_pin"] is not None and not q["initial"]
               and abs(q["t_pin"] - (a + b * q["t_ex"])) * 1000 <= TRIM_MS]
        if len(use) < 3:
            break
        bb, aa = np.polyfit([q["t_ex"] for q in use], [q["t_pin"] for q in use], 1)
        r_ = np.array([q["t_pin"] - (aa + bb * q["t_ex"]) for q in use])
        a, b = float(aa + np.median(r_)), float(bb)
        n_fit = len(use)
    pr = final_pairing(a, b)
    ok = [q for q in pr if q["t_pin"] is not None]
    for q in pr:
        q["off_ms"] = (q["t_pin"] - (a + b * q["t_ex"])) * 1000 if q["t_pin"] is not None else None
    t_first = min(float(np.nanmin(data[p]["src"]["time"])) for p in dpins)
    t_last = max(float(np.nanmax(data[p]["src"]["time"])) for p in dpins)
    t_ref = a + b * t_first
    res.update({"aligned": True, "a": a, "b": b, "t_ref": t_ref, "t_first": t_first, "t_last": t_last,
                "t_end": a + b * t_last, "data": data, "dpins": dpins})

    # ---- text: alignment ----
    lines += ["", "EthoVision export sources: " + "; ".join(
        f"pin {p} <- {data[p]['src']['label']} :: {data[p]['col']}" for p in dpins),
        f"Sample interval {frame_dt * 1000:.2f} ms"]
    lines += ["", f"Starting offset from a vote over all same-pin edge/event time differences: {res['anchor_votes']} of "
                  f"{len(diffs)} differences agree within 75 ms.",
              f"Alignment: edge time = {b:.5f} x EthoVision time + const, fitted on {n_fit} of {len(ok)} paired events "
              f"(initial in-zone entries and events more than one frame ({FRAME_MS:.1f} ms) from the line are left out of the fit); "
              "the constant is set so that the median offset of the events used is 0.",
              f"  slope 1.000 = processed in real time; {(b - 1) * 100:+.3f} % here"]
    if abs(b - 1) > 0.001:
        lines.append("  WARNING: slope differs from 1 by more than 0.1 % - the video was not processed in real time "
                     "(or events were mis-paired); check the pairing counts.")
    lines += ["", "First genuine zone transition of each arena (an initial in-zone state at track start is not a transition),"
                  " offset from the alignment:"]
    for p in dpins:
        qs = sorted((q for q in pr if q["pin"] == p and not q["initial"]), key=lambda q: q["t_ex"])
        if qs and qs[0]["t_pin"] is not None:
            lines.append(f"  pin {p} ({chans[p]}): {'entry' if qs[0]['event'] == 'ON' else 'exit'} at export t = "
                         f"{qs[0]['t_ex']:.3f} s -> {qs[0]['off_ms']:+.1f} ms")
        else:
            lines.append(f"  pin {p} ({chans[p]}): first transition not paired")

    # ---- per channel results ----
    all_ok = True
    rows = []
    for p in dpins:
        d = data[p]
        mine = [q for q in pr if q["pin"] == p]
        on_q = [q for q in mine if q["event"] == "ON"]
        off_q = [q for q in mine if q["event"] == "OFF"]
        un_ex_on = sum(1 for q in on_q if q["t_pin"] is None)
        un_ex_off = sum(1 for q in off_q if q["t_pin"] is None)
        used_on = {q["j"] for q in on_q if q["j"] is not None}
        used_off = {q["j"] for q in off_q if q["j"] is not None}
        extra_on = len(d["on"]) - len(used_on)
        # OFF edges without an export event at/after the last exported sample = the end-of-session all-off
        # (EthoVision 'turn off at the end'); reported separately, not as errors
        end_off = [j for j, t in enumerate(d["off"]) if j not in used_off and t >= res["t_end"] - 0.1]
        extra_off = len(d["off"]) - len(used_off) - len(end_off)
        n_ent, n_ext = len(d["ent"]), len(d["ext"])
        complete = (n_ent == len(used_on) and n_ext == len(used_off) and extra_on == 0 and extra_off == 0)
        all_ok &= complete
        off_on = [q["off_ms"] for q in on_q if q["off_ms"] is not None and not q["initial"]]
        off_off = [q["off_ms"] for q in off_q if q["off_ms"] is not None]
        rows.append({"pin": p, "name": chans[p], "n_ent": n_ent, "n_on": len(d["on"]), "n_ext": n_ext,
                     "n_off": len(d["off"]), "un_ex_on": un_ex_on, "un_ex_off": un_ex_off,
                     "extra_on": extra_on, "extra_off": extra_off, "complete": complete,
                     "mean_on": float(np.median(off_on)) if off_on else float("nan"),
                     "mean_off": float(np.median(off_off)) if off_off else float("nan"),
                     "off_on": off_on, "off_off": off_off})
        lines += ["", f"Pin {p} - {chans[p]}  [{d['src']['label']} :: {d['col']}]",
                  f"  zone entries {n_ent}, exits {n_ext}{'  (mouse in zone at the first sample: counted as an entry)' if d['init_in'] else ''};"
                  f" ON edges {len(d['on'])}, OFF edges {len(d['off'])}",
                  f"  unpaired export events: entries {un_ex_on}, exits {un_ex_off};"
                  f" edges without an export event: ON {extra_on}, OFF {extra_off}"
                  + (f"; plus {len(end_off)} OFF edge(s) at the end of the session (all-off)" if end_off else ""),
                  "  " + stat("ON  offset from aligned export time" + (" (initial entry excluded)" if d["init_in"] else ""), off_on),
                  "  " + stat("OFF offset from aligned export time", off_off),
                  f"  RESULT: {'complete one-to-one pairing' if complete else 'INCOMPLETE - see counts above'}"]
    res["channel_rows"] = rows
    res["pairs"] = pr

    # ---- late / early outliers ----
    init_q = [q for q in ok if q["initial"]]
    big = [q for q in ok if abs(q["off_ms"]) > FRAME_MS and not q["initial"]]
    if init_q:
        lines += ["", "Initial in-zone entries at track start (EthoVision fires the rule when tracking starts, not at a zone crossing;"
                      " excluded from the alignment, the offset statistics, the figure and the duration analysis): " +
                  "; ".join(f"pin {q['pin']} {q['off_ms']:+.0f} ms" for q in init_q)]
    lines += ["", f"Events more than one frame ({FRAME_MS:.1f} ms) from the aligned export time"
                  f" (initial entries excluded): {len(big)} of {len([q for q in ok if not q['initial']])}"]
    for q in sorted(big, key=lambda q: q["t_ex"])[:15]:
        init = ""
        lines.append(f"  pin {q['pin']} {q['event']:3s} export t = {q['t_ex']:8.3f} s: {q['off_ms']:+.1f} ms{init}")

    # events delayed together: late edges of different arenas that leave within 30 ms of each other were most
    # probably held back by the same upstream delay (EthoVision processing or process launch), not by YeLink
    lt = sorted((q["t_pin"], q["pin"], q["event"], q["off_ms"]) for q in ok if q["off_ms"] > FRAME_MS)
    groups, cur = [], []
    for item in lt:
        if cur and item[0] - cur[-1][0] <= 0.03:
            cur.append(item)
        else:
            if len(cur) > 1 and len({x[1] for x in cur}) > 1:
                groups.append(cur)
            cur = [item]
    if len(cur) > 1 and len({x[1] for x in cur}) > 1:
        groups.append(cur)
    very = [q for q in ok if q["off_ms"] > 400]
    if very:
        lines.append(f"  {len(very)} event(s) later than 400 ms (matched because the pairing window is 1 s)")
    for g in groups:
        lines.append("  late edges of different arenas leaving together (within 30 ms): " + "; ".join(
            f"pin {x[1]} {x[2]} +{x[3]:.0f} ms" for x in g) + f" at video t = {g[0][0] - t_ref:.2f} s")

    # ---- cross-arena skew (mean offset per pin; common clock) ----
    lines += ["", "Median timing offset of each pin from the globally aligned export time (same clock map for all pins):"]
    means = []
    for r in rows:
        v = [x for x in (r["mean_on"], r["mean_off"]) if np.isfinite(x)]
        if v:
            means.append(float(np.mean(v)))
            lines.append(f"  pin {r['pin']}: ON {r['mean_on']:+.1f} ms, OFF {r['mean_off']:+.1f} ms")
    if len(means) >= 2:
        lines.append(f"  spread of the per-pin medians: {max(means) - min(means):.1f} ms")

    # ---- leakage between pins: every edge must be explained by an event of its own arena (see counts per pin) ----
    total_extra = sum(r["extra_on"] + r["extra_off"] for r in rows)
    res["xtalk"] = None
    lines += ["", f"Edges on a pin without a zone event of its own arena (leakage / mis-routing): {total_extra}"
                  "  (events of different arenas that happen close together are legitimate and are not cross-talk)"]

    # ---- simultaneous events (info) ----
    allev = sorted((t, p, k) for p in dpins for k, ts in (("ON", data[p]["ent"]), ("OFF", data[p]["ext"])) for t in ts)
    n_same_frame = 0
    for (t1, p1, k1), (t2, p2, k2) in zip(allev, allev[1:]):
        if p1 != p2 and abs(t2 - t1) < max(frame_dt, 1e-3) * 0.99:
            n_same_frame += 1
    lines += ["", f"Events of different arenas in the same video frame: {n_same_frame}"]

    # ---- durations ----
    dur = []
    for p in dpins:
        d = data[p]
        for q_i, te in enumerate(d["ent"]):
            nxt = [t for t in d["ext"] if t > te]
            if not nxt:
                continue
            t_exit = nxt[0]
            q_on = next((q for q in res["pairs"] if q["pin"] == p and q["event"] == "ON" and q["t_ex"] == te), None)
            if q_on is None or q_on["j"] is None or q_on["initial"]:
                continue
            pu = pulses[p][q_on["j"]] if q_on["j"] < len(pulses[p]) else None
            if pu is not None and pu["fall"] is not None:
                dur.append((p, t_exit - te, pu["fall"] - pu["rise"]))
    res["dur_pairs"] = dur
    if dur:
        dd = [(py - ex) * 1000 for _, ex, py in dur]
        lines += ["", "Pulse duration minus EthoVision in-zone duration (all arenas):", "  " + stat("difference", dd)]
        ex_a = np.array([x[1] for x in dur])
        py_a = np.array([x[2] for x in dur])
        if ex_a.size >= 2 and ex_a.std() > 0 and py_a.std() > 0:
            lines.append(f"  R^2 = {np.corrcoef(ex_a, py_a)[0, 1] ** 2:.5f} (n = {ex_a.size})")
        for p in dpins:
            pts = [(ex, py) for pp, ex, py in dur if pp == p]
            if len(pts) >= 2:
                xa, ya = np.array([x[0] for x in pts]), np.array([x[1] for x in pts])
                sl = np.polyfit(xa, ya, 1)[0]
                lines.append(f"  pin {p} ({chans[p]}): " + stat("difference", [(y - x) * 1000 for x, y in pts]) +
                             f"; R^2 {np.corrcoef(xa, ya)[0, 1] ** 2:.5f}, slope {sl:.5f}")

    # ---- verdict ----
    lines += ["", "OVERALL: " + ("every arena/zone pairs one-to-one with its own pin, no edge without a zone event of its own arena"
                                  if (all_ok and total_extra == 0 and len(dpins) == len(pins)) else
                                  "NOT clean - check the per-pin counts above")]
    lines += ["NOTE: the export has no shared clock with the Teensy. The alignment checks event counts, order, durations,"
              " per-pair edge timing and playback rate; the offset a absorbs the typical EthoVision-to-pin delay, so it does not"
              " measure absolute latency."]
    res["text"] = "\n".join(lines + _notes_marks(notes, marks, rec_start))
    return res


def _notes_marks(notes, marks, t0):
    out = []
    if notes:
        out += ["", "Edge-sequence notes:"] + ["  " + n for n in notes[:30]]
    if marks:
        out += ["", "Marks:"] + [f"  {t - t0:9.3f} s  {lab}" for t, lab in marks]
    return out


# ----------------------------------------------------------------------
# Plotting
# ----------------------------------------------------------------------

def draw_timeline(ax, res, xmin=None, xmax=None, compact=False):
    """Pin traces (colour) and, if aligned, EthoVision in-zone traces (black) per arena on one time axis.
    Time zero = first exported video sample when aligned, else recording start."""
    pins = res["dpins"] if res.get("aligned") else res["pins"]
    chans = res["chans"]
    aligned = res.get("aligned", False)
    t0 = res["t_ref"] if aligned else res["rec_start"]
    t_end = res["t_end"] if aligned else res["rec_end"]
    step = 3.0 if aligned else 1.5
    yt, yl = [], []
    for i, p in enumerate(pins):
        base = (len(pins) - 1 - i) * step          # arena 1 at the top; the y axis is NOT inverted
        ts, vs, state = [res["rec_start"] - t0], [0], 0
        for t, s in res["pin_edges"][p]:
            ts.append(t - t0)
            vs.append(s)
            state = s
        ts.append(res["rec_end"] - t0)
        vs.append(state)
        ax.step(ts, [base + v for v in vs], where="post", color=PIN_COLORS.get(p, "k"), lw=1.0 if compact else 1.3)
        yt.append(base + 0.5)
        arena_lab = re.split(r"\s*/\s*", chans[p])[0]
        yl.append(f"{arena_lab}\nYeLink pin {p}" if not compact else arena_lab)
        if aligned:
            d = res["data"][p]
            ts, vs, state = [0.0], [1 if d["init_in"] else 0], (1 if d["init_in"] else 0)
            for te, z in d["ev"]:
                ts.append(res["b"] * (te - res["t_first"]))
                vs.append(z)
                state = z
            ts.append(t_end - t0)
            vs.append(state)
            ax.step(ts, [base + 1.3 + v * 0.8 for v in vs], where="post", color="k", lw=0.8)
            yt.append(base + 1.7)
            zl = re.search(r"zone\s*[-_ ]?([A-Za-z0-9]+)", d["col"].split("(", 1)[-1], re.I)
            zl = ("in zone " + zl.group(1)) if zl else "in zone"
            yl.append(("EthoVision " + zl) if not compact else zl)
    ax.set_yticks(yt)
    ax.set_yticklabels(yl, fontsize=7 if compact else 8)
    ax.set_ylim(-0.5, len(pins) * step)
    ax.set_xlabel("Time since video start (s)" if aligned else "Time since recording start (s)")
    if xmin is None:
        xmin = -5.0
    if xmax is None:
        xmax = (t_end - t0) + 5.0
    if xmax <= xmin:
        xmax = xmin + 1.0
    ax.set_xlim(xmin, xmax)
    if aligned:
        ax.axvline(0, color="gray", lw=0.8, ls=":")
        ax.axvline(t_end - t0, color="gray", lw=0.8, ls=":")
    ax.grid(True, axis="x", alpha=0.3)
    return f"Timeline: {os.path.basename(res['folder'])}"


def plt_rect(x, y, w, h, col, filled):
    from matplotlib.patches import Rectangle
    if filled:
        return Rectangle((x, y), w, h, facecolor=col, alpha=0.35, edgecolor=col, lw=1.0, zorder=3)
    return Rectangle((x, y), w, h, facecolor="none", edgecolor=col, lw=1.2, zorder=3)


def plot_results(fig, res, xmin=None, xmax=None):
    """A timeline (full width); B offset per event (full width); C edge alignment per zone-pin pair; D durations."""
    fig.clf()
    try:
        fig.set_layout_engine("constrained")
    except Exception:
        fig.set_constrained_layout(True)
    if not res.get("aligned"):
        ax = fig.subplots(1, 1)
        draw_timeline(ax, res, xmin, xmax, compact=False)
        ax.set_title("Output timeline (no EthoVision export loaded)")
        return
    gs = fig.add_gridspec(3, 2, height_ratios=[1.5, 1.0, 1.1])
    a = fig.add_subplot(gs[0, :])
    draw_timeline(a, res, xmin, xmax, compact=True)
    a.set_title("Output timeline: YeLink pin (colour) and EthoVision in-zone state (black)", fontsize=10)

    # B. offset per event (axis limited to -40..+150 ms; larger offsets are drawn at the top edge, stacked so that
    # simultaneous ones stay visible, and labelled with their value)
    a = fig.add_subplot(gs[1, :])
    t_ref = res["t_ref"]
    B_LO, B_HI = -40.0, 150.0
    clipped = sorted((x["t_pin"] for x in res["pairs"] if x["t_pin"] is not None and not x.get("initial")
                      and x["off_ms"] > B_HI))
    y_of = {}
    last_t, k_stack = None, 0
    for t_ in clipped:
        k_stack = k_stack + 1 if (last_t is not None and t_ - last_t < 3.0) else 0
        y_of[t_] = B_HI - 16 * k_stack
        last_t = t_
    for p in res["dpins"]:
        for kind, mk, filled in (("ON", "o", True), ("OFF", "^", False)):
            q = [x for x in res["pairs"] if x["pin"] == p and x["event"] == kind and x["t_pin"] is not None
                 and not x.get("initial")]
            if not q:
                continue
            xs = [x["t_pin"] - t_ref for x in q]
            ys = [y_of.get(x["t_pin"], x["off_ms"]) if x["off_ms"] > B_HI else x["off_ms"] for x in q]
            lab = f"{res['chans'][p].split(' / ')[0]} {kind} (n = {len(q)})"
            if filled:
                a.scatter(xs, ys, s=12, marker=mk, color=PIN_COLORS[p], label=lab, clip_on=False)
            else:
                a.scatter(xs, ys, s=14, marker=mk, facecolors="none", edgecolors=PIN_COLORS[p], label=lab, clip_on=False)
            for x_, y_, q_ in zip(xs, ys, q):
                if q_["off_ms"] > B_HI:
                    a.text(x_ - 6, y_, f"+{q_['off_ms']:.0f} ms", fontsize=6.5, ha="right", va="center", color=PIN_COLORS[p])
    a.axhline(0, color="k", lw=0.8, ls="--")
    a.set_ylim(B_LO, B_HI + 75)
    a.set_title("Pin-Edge Offset from Aligned Export Time", fontsize=10)
    a.set_xlabel("Time since video start (s)")
    a.set_ylabel("Edge − Export (ms)")
    a.set_xlim(xmin, xmax)
    a.grid(True, alpha=0.3)
    a.legend(fontsize=6.5, ncol=4, loc="upper right", frameon=True, framealpha=0.9, columnspacing=1.0)

    # C. edge alignment inside each zone-pin pair (ON = entry, OFF = exit)
    a = fig.add_subplot(gs[2, 0])
    rng = np.random.RandomState(1)
    lim_lo, lim_hi = -40.0, 75.0
    pins = res["dpins"]
    for i, p in enumerate(pins):
        for kind, off, filled in (("ON", -0.2, True), ("OFF", 0.2, False)):
            v = np.array([q["off_ms"] for q in res["pairs"] if q["pin"] == p and q["event"] == kind
                          and q["off_ms"] is not None and not q.get("initial")])
            if v.size == 0:
                continue
            col = PIN_COLORS[p]
            xs = i + off + rng.uniform(-0.07, 0.07, v.size)
            vin = np.clip(v, lim_lo, lim_hi)
            if filled:
                a.scatter(xs, vin, s=5, color=col, alpha=0.35, linewidths=0)
            else:
                a.scatter(xs, vin, s=6, facecolors="none", edgecolors=col, alpha=0.5, linewidths=0.5)
            q1, med, q3 = np.percentile(v, [25, 50, 75])
            a.add_patch(plt_rect(i + off - 0.14, q1, 0.28, q3 - q1, col, filled))
            a.plot([i + off - 0.14, i + off + 0.14], [med, med], color="k", lw=1.2)
            nout = int(np.sum(v > lim_hi))
            if nout:
                a.text(i + off, lim_hi - 1, f"▲{nout}", ha="center", va="top", fontsize=6.5, color="k")
    a.axhline(0, color="k", lw=0.8, ls="--")
    a.set_ylim(lim_lo, lim_hi)
    a.set_xlim(-0.6, len(pins) - 0.4)
    a.set_xticks(range(len(pins)))
    a.set_xticklabels([re.split(r"\s*/\s*", res["chans"][p])[0] for p in pins], fontsize=8)
    a.set_ylabel("Edge − Export (ms)")
    a.set_title("Edge Alignment per Zone–Pin Pair", fontsize=10)
    a.grid(True, axis="y", alpha=0.3)
    from matplotlib.lines import Line2D
    a.legend(handles=[Line2D([], [], marker="o", ls="", color="gray", markersize=4, label="ON (entry)"),
                      Line2D([], [], marker="o", ls="", markerfacecolor="none", color="gray", markersize=4, label="OFF (exit)")],
             fontsize=7, loc="lower center", ncol=2, framealpha=0.9)

    # D. pulse duration
    a = fig.add_subplot(gs[2, 1])
    allx, ally = [], []
    for p in res["dpins"]:
        pts = [(ex, py) for pp, ex, py in res["dur_pairs"] if pp == p]
        if not pts:
            continue
        xs = np.array([x[0] for x in pts])
        ys = np.array([x[1] for x in pts])
        allx += list(xs)
        ally += list(ys)
        a.scatter(xs, ys, s=12, color=PIN_COLORS[p], label=f"{res['chans'][p]} (n = {xs.size})")
    if allx:
        mx = max(max(allx), max(ally))
        lab = "y = x"
        if len(allx) >= 2 and np.std(allx) > 0 and np.std(ally) > 0:
            lab = f"R$^2$ = {np.corrcoef(allx, ally)[0, 1] ** 2:.2f}"
        a.plot([0, mx], [0, mx], "k--", lw=0.8, label=lab)
        a.legend(fontsize=7, loc="lower right", framealpha=0.9)
    a.set_title("Pulse vs. In-Zone Duration", fontsize=10)
    a.set_xlabel("EthoVision in-zone duration (s)")
    a.set_ylabel("YeLink pulse duration (s)")


def save_publication_figure(res, path, xmin=None, xmax=None):
    """Re-draw on a fresh figure of fixed size so the saved PNG does not depend on the window size."""
    f = Figure(figsize=(10, 11))
    plot_results(f, res, xmin, xmax)
    f.savefig(path, dpi=200)


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------

if HAVE_TK:
    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("YeLink validation 4 - four EthoVision arenas, four pins")
            self.geometry("1380x900")
            self.q = queue.Queue()
            self.stop_evt = threading.Event()
            self.worker = None
            self.tz = None
            self.live_clock = None
            self.run_t0 = None
            self.marks = []
            self.mon = None
            self.mon_seen = 0
            self.last_res = None
            self.sources = []          # all sources from all loaded files
            self.map_vars = {}         # pin -> StringVar of the mapping combobox
            self.map_cbos = {}
            self.rec_enabled = {p: tk.BooleanVar(value=True) for p in PINS}
            self.rec_names = {p: tk.StringVar(value=DEFAULT_NAMES[p]) for p in PINS}
            self.var_xmin = tk.StringVar(value="-5")
            self.var_xmax = tk.StringVar(value="")

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

            ch = ttk.LabelFrame(f, text="Channels: tick the pins to record/analyse and name the events "
                                        "(each Uno pin N -> 10k / 20k divider -> Teensy pin N)")
            ch.pack(fill="x", padx=10, pady=6)
            ttk.Label(ch, text="Use").grid(row=0, column=0, padx=6)
            ttk.Label(ch, text="Teensy pin").grid(row=0, column=1, padx=6)
            ttk.Label(ch, text="Event name (arena / zone)").grid(row=0, column=2, padx=6, sticky="w")
            for i, p in enumerate(PINS):
                ttk.Checkbutton(ch, variable=self.rec_enabled[p]).grid(row=i + 1, column=0, padx=6)
                tk.Label(ch, text=f"  pin {p}  ", bg=PIN_COLORS[p], fg="white").grid(row=i + 1, column=1, padx=6, pady=2)
                ttk.Entry(ch, textvariable=self.rec_names[p], width=34).grid(row=i + 1, column=2, padx=6, sticky="w")

            st = ttk.LabelFrame(f, text="Recording")
            st.pack(fill="x", padx=10, pady=6)
            self.var_out = tk.StringVar(value=os.path.join(os.getcwd(), "results_multiarena"))
            self.var_note = tk.StringVar(value="")
            self.var_live = tk.BooleanVar(value=False)
            ttk.Label(st, text="Output folder").grid(row=0, column=0, padx=6, pady=4, sticky="e")
            ttk.Entry(st, textvariable=self.var_out, width=80).grid(row=0, column=1, columnspan=3, sticky="we")
            ttk.Button(st, text="Browse...", command=self._browse_out).grid(row=0, column=4, padx=4)
            ttk.Label(st, text="Note (video, rule, build)").grid(row=1, column=0, padx=6, pady=4, sticky="e")
            ttk.Entry(st, textvariable=self.var_note, width=80).grid(row=1, column=1, columnspan=3, sticky="we")
            ttk.Checkbutton(st, text="Live plot during recording (off = cleanest timing)",
                            variable=self.var_live).grid(row=1, column=4, padx=4)
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
            self.txt_log = tk.Text(left, width=58, wrap="word", font=("Consolas", 9))
            sb = ttk.Scrollbar(left, command=self.txt_log.yview)
            self.txt_log.configure(yscrollcommand=sb.set)
            self.txt_log.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")

            right = ttk.Frame(body)
            right.pack(side="left", fill="both", expand=True, padx=(10, 0))
            pbar = ttk.Frame(right)
            pbar.pack(side="top", fill="x")
            ttk.Button(pbar, text="Plot timeline (after the recording)", command=self.plot_timeline).pack(side="left", padx=4)
            ttk.Label(pbar, text="  x from").pack(side="left")
            ttk.Entry(pbar, textvariable=self.var_xmin, width=7).pack(side="left", padx=2)
            ttk.Label(pbar, text="to").pack(side="left")
            ttk.Entry(pbar, textvariable=self.var_xmax, width=7).pack(side="left", padx=2)
            ttk.Label(pbar, text="s (blank = end + 5 s)", foreground="gray").pack(side="left")
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
                     "trial runs -> Stop and save -> export the adapter log and the EthoVision raw data "
                     "(all four arenas, with the In-zone variables) -> tab 2.")

        def _channels(self):
            return {p: (self.rec_names[p].get().strip() or f"pin {p}") for p in PINS if self.rec_enabled[p].get()}

        def _init_live_axes(self):
            a = self.ax_live
            a.clear()
            self.fig_live.suptitle("")
            a.set_title("Live: pin levels from the Teensy")
            a.set_xlabel("seconds since recording start")
            a.set_yticks([i * 1.5 + 0.5 for i in range(len(PINS))])
            a.set_yticklabels([f"pin {p}  {self.rec_names[p].get()}" for p in PINS], fontsize=8)
            a.set_ylim(-0.5, len(PINS) * 1.5)
            a.grid(True, axis="x", alpha=0.3)
            self.canvas_live.draw_idle()

        def log(self, msg):
            self.txt_log.insert("end", msg + "\n")
            self.txt_log.see("end")

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
                    self.log("  Note: some pins are high. Turn the outputs off (adapter: all-off) before starting.")
            except Exception as e:
                self.lbl_teensy.config(text="failed", foreground="red")
                self.log(f"Teensy test FAILED: {e}")

        # ---------------- live monitor ----------------
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
            self.log("Monitor: live pin levels. Drive each pin by hand (adapter: p=2 v=1 d=0 / p=2 v=0 ...) "
                     "and check that the matching Teensy pin follows.")
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
            txt = "  ".join(f"pin{p}={lv.get(p, '?')}" for p in PINS)
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
            chans = self._channels()
            if not chans:
                messagebox.showerror("Input error", "Tick at least one pin.")
                return
            label = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            outdir = os.path.join(self.var_out.get().strip() or os.getcwd(), f"multiarena_{label}")
            os.makedirs(outdir, exist_ok=True)
            cfg = {"com": com, "label": label, "outdir": outdir, "note": self.var_note.get().strip(),
                   "channels": {str(p): {"name": self.rec_names[p].get().strip() or f"pin {p}",
                                         "enabled": bool(self.rec_enabled[p].get())} for p in PINS}}
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
                while not self.stop_evt.is_set():
                    time.sleep(0.05)
                    now = time.perf_counter()
                    if now - last_sync >= SYNC_EVERY_S:
                        sync_samples += tz.sync(5)
                        last_sync = now
                        self.q.put(("progress", now - start_perf, len(tz.edges)))
                time.sleep(0.3)
                sync_samples += tz.sync(30)
                stop_perf = time.perf_counter()
                levels_end = tz.query_levels()
                edges = list(tz.edges)
                other = tz.other[:50]
                tz.close()
                self.tz = None
                clk = fit_clock(sync_samples)
                outdir = cfg["outdir"]
                meta = {"label": cfg["label"], "teensy": cfg["com"], "channels": cfg["channels"],
                        "note": cfg["note"], "wall_minus_perf_s": w_off, "clock": clk, "start_perf": start_perf,
                        "stop_perf": stop_perf, "start_wall": datetime.fromtimestamp(start_perf + w_off).isoformat(sep=" "),
                        "levels_start": levels, "levels_end": levels_end, "teensy_other": other}
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
                            self.log("Saved. Showing the edge-only summary; load the EthoVision export on tab 2 for the alignment.")
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
                    a.set_yticklabels([f"pin {p}  {self.rec_names[p].get()}" for p in PINS], fontsize=8)
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

        def _xrange(self):
            def parse(var):
                s = var.get().strip()
                if not s:
                    return None
                try:
                    return float(s)
                except ValueError:
                    return None
            return parse(self.var_xmin), parse(self.var_xmax)

        def plot_timeline(self):
            folder = self.var_folder.get().strip()
            if not os.path.isfile(os.path.join(folder, "edges.csv")):
                folder = filedialog.askdirectory(title="Choose a results folder (contains edges.csv and meta.json)")
                if not folder or not os.path.isfile(os.path.join(folder, "edges.csv")):
                    return
                self.var_folder.set(folder)
            if self.last_res is None or self.last_res.get("folder") != folder:
                try:
                    self.last_res = analyze_folder(folder)
                except Exception as e:
                    self.log(f"Timeline: {e}")
                    return
            xmin, xmax = self._xrange()
            a = self.ax_live
            a.clear()
            title = draw_timeline(a, self.last_res, xmin, xmax, compact=False)
            self.fig_live.suptitle(title, fontsize=9)
            self.fig_live.tight_layout(rect=(0, 0, 1, 0.95))
            self.canvas_live.draw()
            try:
                self.fig_live.savefig(os.path.join(folder, "multiarena_timeline.png"), dpi=200)
            except Exception:
                pass

        # ---------------- Tab 2 ----------------
        def _build_an_tab(self):
            f = self.tab_an
            inp = ttk.LabelFrame(f, text="Input")
            inp.pack(fill="x", padx=10, pady=6)
            self.var_folder = tk.StringVar()
            self.var_adlog = tk.StringVar()
            self.var_tol = tk.StringVar(value=f"{DEFAULT_TOL_MS:.0f}")
            ttk.Label(inp, text="Results folder").grid(row=0, column=0, padx=6, pady=4, sticky="e")
            ttk.Entry(inp, textvariable=self.var_folder, width=90).grid(row=0, column=1, sticky="we")
            ttk.Button(inp, text="Browse...", command=self._browse_folder).grid(row=0, column=2, padx=6)
            ttk.Label(inp, text="EthoVision export file(s)").grid(row=1, column=0, padx=6, pady=4, sticky="ne")
            fr = ttk.Frame(inp)
            fr.grid(row=1, column=1, sticky="we")
            self.lst_files = tk.Listbox(fr, height=3)
            self.lst_files.pack(side="left", fill="x", expand=True)
            fb = ttk.Frame(inp)
            fb.grid(row=1, column=2, padx=6, sticky="n")
            ttk.Button(fb, text="Add file(s)...", command=self._add_etho).pack(fill="x")
            ttk.Button(fb, text="Clear", command=self._clear_etho).pack(fill="x", pady=2)
            ttk.Label(inp, text="Adapter log CSV (optional)").grid(row=2, column=0, padx=6, pady=4, sticky="e")
            ttk.Entry(inp, textvariable=self.var_adlog, width=90).grid(row=2, column=1, sticky="we")
            ttk.Button(inp, text="Browse...", command=lambda: self._browse_file(
                self.var_adlog, [("CSV files", "*.csv"), ("All files", "*.*")])).grid(row=2, column=2, padx=6)
            self.frm_map = ttk.LabelFrame(inp, text="Which EthoVision 'In zone' column belongs to which pin")
            self.frm_map.grid(row=3, column=0, columnspan=3, sticky="we", padx=6, pady=4)
            opt = ttk.Frame(inp)
            opt.grid(row=4, column=1, sticky="w")
            ttk.Label(opt, text="Pairing tolerance (ms, first pass):").pack(side="left")
            ttk.Entry(opt, textvariable=self.var_tol, width=6).pack(side="left", padx=4)
            ttk.Label(opt, text="  timeline x from").pack(side="left")
            ttk.Entry(opt, textvariable=self.var_xmin, width=6).pack(side="left", padx=2)
            ttk.Label(opt, text="to").pack(side="left")
            ttk.Entry(opt, textvariable=self.var_xmax, width=6).pack(side="left", padx=2)
            ttk.Label(opt, text="s", foreground="gray").pack(side="left")
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
            self.txt_summary = tk.Text(left, width=78, wrap="none", font=("Consolas", 9))
            self.txt_summary.pack(fill="both", expand=True)
            right = ttk.Frame(body)
            right.pack(side="left", fill="both", expand=True, padx=(10, 0))
            self.fig = Figure(figsize=(8, 9), dpi=100)
            self.canvas = FigureCanvasTkAgg(self.fig, master=right)
            tb = NavigationToolbar2Tk(self.canvas, right)
            tb.update()
            tb.pack(side="top", fill="x")
            self.canvas.get_tk_widget().pack(side="top", fill="both", expand=True)

        def _browse_file(self, var, types):
            p = filedialog.askopenfilename(filetypes=types)
            if p:
                var.set(p)

        def _browse_folder(self):
            p = filedialog.askdirectory()
            if p:
                self.var_folder.set(p)
                self._rebuild_mapping()

        def _add_etho(self):
            paths = filedialog.askopenfilenames(filetypes=[("EthoVision export", "*.xlsx *.txt *.csv"), ("All files", "*.*")])
            for p in paths:
                try:
                    srcs = load_etho_sources(p)
                except Exception as e:
                    messagebox.showerror("Export read error", f"{os.path.basename(p)}: {e}")
                    continue
                self.sources += srcs
                self.lst_files.insert("end", f"{os.path.basename(p)}  ({len(srcs)} table(s): " +
                                      ", ".join(s['arena_text'] or '?' for s in srcs) + ")")
            self._rebuild_mapping()

        def _clear_etho(self):
            self.sources = []
            self.lst_files.delete(0, "end")
            self._rebuild_mapping()

        def _current_channels(self):
            folder = self.var_folder.get().strip()
            if os.path.isfile(os.path.join(folder, "meta.json")):
                try:
                    with open(os.path.join(folder, "meta.json"), encoding="utf-8") as f:
                        return channels_from_meta(json.load(f))
                except Exception:
                    pass
            return self._channels()

        def _rebuild_mapping(self):
            for w in self.frm_map.winfo_children():
                w.destroy()
            self.map_vars, self.map_cbos = {}, {}
            chans = self._current_channels()
            opts = ["(none)"] + [f"{si}: {s['label']} :: {c}" for si, s in enumerate(self.sources) for c in s["zone_columns"]]
            auto = auto_assign(chans, self.sources) if self.sources else {}
            for i, p in enumerate(sorted(chans)):
                tk.Label(self.frm_map, text=f" pin {p} ", bg=PIN_COLORS.get(p, "gray"), fg="white").grid(row=i, column=0, padx=4, pady=1)
                ttk.Label(self.frm_map, text=chans[p], width=24).grid(row=i, column=1, sticky="w")
                v = tk.StringVar(value="(none)")
                m = auto.get(p)
                if m is not None:
                    v.set(f"{m[0]}: {self.sources[m[0]]['label']} :: {m[1]}")
                cb = ttk.Combobox(self.frm_map, textvariable=v, values=opts, width=95, state="readonly")
                cb.grid(row=i, column=2, sticky="we", padx=4)
                self.map_vars[p] = v
                self.map_cbos[p] = cb

        def _mapping(self):
            out = {}
            for p, v in self.map_vars.items():
                s = v.get()
                if s == "(none)" or ":" not in s:
                    out[p] = None
                    continue
                si = int(s.split(":", 1)[0])
                col = s.split(" :: ", 1)[1]
                out[p] = (si, col)
            return out

        def analyze(self):
            folder = self.var_folder.get().strip()
            if not os.path.isfile(os.path.join(folder, "edges.csv")):
                messagebox.showerror("Missing folder", "Choose a results folder that contains edges.csv and meta.json.")
                return
            if not self.map_vars:
                self._rebuild_mapping()
            try:
                tol = float(self.var_tol.get())
            except ValueError:
                tol = DEFAULT_TOL_MS
            try:
                res = analyze_folder(folder, sources=self.sources or None, mapping=self._mapping() if self.sources else None,
                                     adapter_log=self.var_adlog.get().strip() or None, tol_ms=tol)
            except Exception as e:
                import traceback
                messagebox.showerror("Analysis error", "".join(traceback.format_exception(type(e), e, e.__traceback__))[-1500:])
                return
            self.last_res = res
            self.txt_summary.delete("1.0", "end")
            self.txt_summary.insert("1.0", res["text"])
            xmin, xmax = self._xrange()
            plot_results(self.fig, res, xmin, xmax)
            self.canvas.draw()
            try:
                with open(os.path.join(folder, "summary.txt"), "w", encoding="utf-8") as f:
                    f.write(res["text"] + "\n")
                if res.get("aligned"):
                    with open(os.path.join(folder, "pairs.csv"), "w", newline="", encoding="utf-8") as f:
                        w = csv.writer(f)
                        w.writerow(["pin", "name", "event", "export_time_s", "pin_time_video_s", "offset_ms"])
                        for q in res["pairs"]:
                            w.writerow([q["pin"], res["chans"][q["pin"]], q["event"], f"{q['t_ex']:.4f}",
                                        "" if q["t_pin"] is None else f"{q['t_pin'] - res['t_ref']:.4f}",
                                        "" if q["off_ms"] is None else f"{q['off_ms']:.3f}"])
                    save_publication_figure(res, os.path.join(folder, "multiarena_figure.png"), xmin, xmax)
            except Exception:
                pass

        def save_figure(self):
            p = filedialog.asksaveasfilename(defaultextension=".png", filetypes=[("PNG image", "*.png")],
                                             initialfile="multiarena_figure.png")
            if p:
                if self.last_res is not None:
                    xmin, xmax = self._xrange()
                    save_publication_figure(self.last_res, p, xmin, xmax)
                else:
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
            for t in (self.mon, self.tz):
                if t is not None:
                    try:
                        t.close()
                    except Exception:
                        pass
            self.destroy()


if __name__ == "__main__":
    if not HAVE_TK:
        raise SystemExit("tkinter is not available in this Python.")
    App().mainloop()
