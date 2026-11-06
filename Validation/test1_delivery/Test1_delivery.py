# -*- coding: utf-8 -*-
"""
YeLink Test 1 - GUI for Spyder (open the file and press F5, or run: python Test1.py)

Tab 1, "Run test"
  Enter the TCP host and port of YeLink_AdapterUI, choose the command CSV from
  make_test1_commands.py, set the pacing, and press Run. Each command is sent over
  its own TCP connection, the same way YeLink_Controller.exe sends it. The adapter
  returns the firmware's own reply (OK ... or ERR ...). Results are saved to a CSV
  and a summary text file in the output folder. The results file is then loaded
  into Tab 2 automatically.

Tab 2, "Compare"
  Load the run CSV from Tab 1 and the log CSV exported from YeLink_AdapterUI.
  The tab reconciles the two logs part by part, reports missing or extra entries,
  and plots round-trip time, the round-trip distribution, pacing, and the
  host-side delay from send to adapter receipt.

Requirements: Python 3.8 or newer, numpy, matplotlib (both included with Anaconda,
which Spyder comes with). Tkinter is part of the Python standard library.

Before running: start YeLink_AdapterUI.exe, select the COM port, set 115200, click
Connect, and close the Arduino IDE Serial Monitor.

Change log
  2026-10-29  send_one: use a blocking connect() instead of socket.create_connection(timeout=...).
              On Windows a socket with a timeout performs a non-blocking connect and waits in
              select(), which added a ~10 ms step to about half the connections (measured with
              loopback_probe2.py). YeLink_Controller.exe uses a blocking connect, so the test
              client now matches it. The timeout is applied after connecting, for the receive only.
"""

import csv
import os
import queue
import socket
import threading
import time
from datetime import datetime
from difflib import SequenceMatcher

import numpy as np
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk


# ----------------------------------------------------------------------
# Core helpers
# ----------------------------------------------------------------------

RUN_FIELDS = ["index", "category", "command", "expected_firmware", "send_time",
              "send_offset_ms", "connected", "connect_ms", "reply", "reply_ms", "error"]
CATEGORY_COLORS = {"single": "#1f77b4", "batch": "#2ca02c", "malformed": "#d62728"}


def send_one(host, port, command, timeout_s):
    """One TCP connection per command, as YeLink_Controller.exe does.

    The connect is a plain blocking connect(), like the C controller. Do not use
    socket.create_connection(timeout=...) or settimeout() before connecting: on Windows
    that switches to a non-blocking connect + select(), which adds a ~10 ms wake-up
    delay to most connections and would be reported as adapter latency.
    The timeout protects only the receive, in case the adapter never replies.
    """
    res = {"connected": False, "reply": "", "connect_ms": "", "reply_ms": "", "error": ""}
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    t0 = time.perf_counter()
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
        sock.settimeout(timeout_s)   # receive-only timeout; set after the blocking connect
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


def expand_parts(command):
    """A batch 'a;b' is logged by the adapter as two rows, one per part."""
    return [p.strip() for p in command.split(";") if p.strip()]


def fw_matches(reply, expected):
    """True if every part of the firmware reply starts with the expected result (OK or ERR)."""
    parts = [p for p in reply.split(";") if p.strip()]
    return bool(parts) and all(p.strip().startswith(expected) for p in parts)


def parse_ts(text):
    """Parse 'yyyy-MM-dd HH:mm:ss.ffffff' or '.fff' (local time) to epoch seconds."""
    return datetime.strptime(text.strip(), "%Y-%m-%d %H:%M:%S.%f").timestamp()


def parse_bool(text):
    return str(text).strip().lower() == "true"


def to_float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return float("nan")


def finite(values):
    arr = np.asarray(values, dtype=float)
    return arr[np.isfinite(arr)]


def stat_line(name, values, unit="ms"):
    a = finite(values)
    if a.size == 0:
        return f"{name}: no data"
    return (f"{name}: n={a.size}, median {np.median(a):.3f}, p95 {np.percentile(a, 95):.3f}, "
            f"min {a.min():.3f}, max {a.max():.3f} {unit}")


def align(expected, received):
    """Match two sequences in order. Returns matched index pairs plus missing and extra counts."""
    sm = SequenceMatcher(None, expected, received, autojunk=False)
    pairs, missing, extra = [], 0, 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            pairs.extend((i1 + k, j1 + k) for k in range(i2 - i1))
        elif tag == "delete":
            missing += i2 - i1
        elif tag == "insert":
            extra += j2 - j1
        elif tag == "replace":
            missing += i2 - i1
            extra += j2 - j1
    return pairs, missing, extra


def load_run(path):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                send_ts = parse_ts(r["send_time"])
            except ValueError:
                send_ts = float("nan")
            rows.append({
                "index": int(r["index"]),
                "category": r["category"],
                "command": r["command"],
                "expected_firmware": r.get("expected_firmware", ""),
                "send_ts": send_ts,
                "send_offset_ms": to_float(r["send_offset_ms"]),
                "connected": parse_bool(r["connected"]),
                "connect_ms": to_float(r.get("connect_ms", "")),
                "reply": r["reply"],
                "reply_ms": to_float(r["reply_ms"]),
            })
    return rows


def load_adapter(path):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                ts = parse_ts(r["Time"])
            except ValueError:
                ts = float("nan")
            rows.append({"ts": ts, "source": r["Source"].strip(),
                         "received": r["Received"].strip(), "sent": r["SentToArduino"].strip()})
    return rows


def analyze_compare(runs, adapter):
    """Reconcile the run with the adapter log and build the numbers for the summary and plots."""
    expected, owners = [], []
    for run_row in runs:
        if run_row["connected"]:
            for part in expand_parts(run_row["command"]):
                expected.append(part)
                owners.append(run_row)

    tcp_rows = [a for a in adapter if a["source"].upper() == "TCP"]
    received = [a["received"] for a in tcp_rows]
    pairs, missing, extra = align(expected, received)

    delays_ms = []
    for i, j in pairs:
        d = (tcp_rows[j]["ts"] - owners[i]["send_ts"]) * 1000.0
        if np.isfinite(d):
            delays_ms.append(d)

    matched_rows = [r for r in runs if r["connected"] and fw_matches(r["reply"], r["expected_firmware"])]
    valid = [r for r in runs if r["category"] != "malformed"]
    malformed = [r for r in runs if r["category"] == "malformed"]
    valid_ok = [r for r in valid if r["connected"] and fw_matches(r["reply"], r["expected_firmware"])]
    malformed_err = [r for r in malformed if r["connected"] and fw_matches(r["reply"], r["expected_firmware"])]

    rt = [r["reply_ms"] for r in matched_rows]
    connect = [r["connect_ms"] for r in runs if r["connected"]]
    offsets = [r["send_offset_ms"] for r in runs]
    intervals = np.diff(finite(offsets)) if len(finite(offsets)) > 1 else np.array([])

    match = (missing == 0 and extra == 0 and len(expected) == len(received))
    lines = [
        f"Commands in run file:            {len(runs)}",
        f"Connections succeeded:           {sum(r['connected'] for r in runs)}",
        f"Firmware replies as expected:    {len(matched_rows)} of {len(runs)}",
        f"Valid commands returning OK:     {len(valid_ok)} of {len(valid)}",
        f"Malformed commands rejected (ERR): {len(malformed_err)} of {len(malformed)}",
        "",
        "Reconciliation with adapter log",
        f"  command parts sent:            {len(expected)}",
        f"  adapter TCP rows:              {len(received)}",
        f"  matched in order:              {len(pairs)}",
        f"  missing at adapter:            {missing}",
        f"  extra at adapter:              {extra}",
        f"  result:                        {'MATCH' if match else 'MISMATCH'}",
        "",
        "Timing",
        "  " + stat_line("Round trip (connect to firmware reply)", rt),
        "  " + stat_line("Connection setup (connect only)", connect),
        "  " + stat_line("Interval between command starts", intervals),
    ]
    if intervals.size:
        lines.append(f"    target interval median {np.median(intervals):.3f} ms, SD {np.std(intervals):.3f} ms")
    lines += [
        "  " + stat_line("Adapter receipt minus send time", delays_ms),
        "    (host-side only: time from the send to the adapter receiving the command.",
        "     This is not the hardware latency, which Test 2 measures with the edge logger.",
        "     Values near zero or slightly negative reflect clock agreement at sub-millisecond scale.)",
    ]
    return {
        "text": "\n".join(lines),
        "rt": rt,
        "intervals": intervals,
        "delays": delays_ms,
        "matched_rows": matched_rows,
    }


def build_run_text(rows, cfg, stopped):
    connected = sum(1 for r in rows if r["connected"])
    matched = sum(1 for r in rows if r["connected"] and fw_matches(r["reply"], r["expected_firmware"]))
    valid = [r for r in rows if r["category"] != "malformed"]
    valid_ok = sum(1 for r in valid if r["connected"] and fw_matches(r["reply"], r["expected_firmware"]))
    malformed = [r for r in rows if r["category"] == "malformed"]
    malformed_err = sum(1 for r in malformed if r["connected"] and fw_matches(r["reply"], r["expected_firmware"]))
    failures = [r for r in rows if not (r["connected"] and fw_matches(r["reply"], r["expected_firmware"]))
                and r["category"] != "malformed"]
    rt = [to_float(r["reply_ms"]) for r in rows if r["connected"] and fw_matches(r["reply"], r["expected_firmware"])]
    connect = [to_float(r["connect_ms"]) for r in rows if r["connected"]]
    lines = [
        f"YeLink Test 1 - run {cfg['label']}" + (" (STOPPED EARLY)" if stopped else ""),
        f"Date:      {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Target:    {cfg['host']}:{cfg['port']}",
        f"Commands:  {cfg['commands']}",
        f"Interval:  {cfg['interval_ms']:.0f} ms",
        "",
        f"Commands sent:                     {len(rows)} of {cfg['n']}",
        f"Connections succeeded:             {connected}",
        f"Firmware replies as expected:      {matched} of {len(rows)}",
        f"Valid commands returning OK:       {valid_ok} of {len(valid)}",
        f"Malformed commands rejected (ERR): {malformed_err} of {len(malformed)}",
        "",
        stat_line("Round trip (connect to firmware reply)", rt),
        stat_line("Connection setup (connect only)", connect),
    ]
    if failures:
        lines.append(f"\nValid commands that failed: {len(failures)}")
        for r in failures[:20]:
            lines.append(f"  index {r['index']}: connected={r['connected']} reply='{r['reply']}' {r['error']}")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("YeLink Test 1 - Command delivery")
        self.geometry("1250x820")
        self.q = queue.Queue()
        self.stop_evt = threading.Event()
        self.worker = None
        self.cfg = None
        self.run_rows = []

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)
        self.tab_run = ttk.Frame(nb)
        self.tab_cmp = ttk.Frame(nb)
        nb.add(self.tab_run, text="1. Run test")
        nb.add(self.tab_cmp, text="2. Compare")

        self._build_run_tab()
        self._build_compare_tab()
        self.after(100, self._poll_queue)

    # ---------- Tab 1: run ----------
    def _build_run_tab(self):
        f = self.tab_run

        conn = ttk.LabelFrame(f, text="TCP connection to YeLink adapter")
        conn.pack(fill="x", padx=10, pady=6)
        self.var_host = tk.StringVar(value="127.0.0.1")
        self.var_port = tk.StringVar(value="13000")
        self.var_timeout = tk.StringVar(value="1.0")
        ttk.Label(conn, text="Host").grid(row=0, column=0, padx=6, pady=6, sticky="e")
        ttk.Entry(conn, textvariable=self.var_host, width=16).grid(row=0, column=1, sticky="w")
        ttk.Label(conn, text="Port").grid(row=0, column=2, padx=6, sticky="e")
        ttk.Entry(conn, textvariable=self.var_port, width=8).grid(row=0, column=3, sticky="w")
        ttk.Label(conn, text="Timeout (s)").grid(row=0, column=4, padx=6, sticky="e")
        ttk.Entry(conn, textvariable=self.var_timeout, width=6).grid(row=0, column=5, sticky="w")
        ttk.Button(conn, text="Test connection", command=self.test_connection).grid(row=0, column=6, padx=10)
        self.lbl_conn = ttk.Label(conn, text="not tested", foreground="gray")
        self.lbl_conn.grid(row=0, column=7, padx=6, sticky="w")

        settings = ttk.LabelFrame(f, text="Test settings")
        settings.pack(fill="x", padx=10, pady=6)
        self.var_cmdfile = tk.StringVar()
        self.var_outdir = tk.StringVar(value=os.getcwd())
        self.var_interval = tk.StringVar(value="100")
        self.var_limit = tk.StringVar(value="0")

        ttk.Label(settings, text="Command CSV").grid(row=0, column=0, padx=6, pady=4, sticky="e")
        ttk.Entry(settings, textvariable=self.var_cmdfile, width=80).grid(row=0, column=1, columnspan=4, sticky="we")
        ttk.Button(settings, text="Browse...", command=self.browse_cmd).grid(row=0, column=5, padx=6)

        ttk.Label(settings, text="Output folder").grid(row=1, column=0, padx=6, pady=4, sticky="e")
        ttk.Entry(settings, textvariable=self.var_outdir, width=80).grid(row=1, column=1, columnspan=4, sticky="we")
        ttk.Button(settings, text="Browse...", command=self.browse_out).grid(row=1, column=5, padx=6)

        ttk.Label(settings, text="Interval (ms)").grid(row=2, column=0, padx=6, pady=4, sticky="e")
        ttk.Entry(settings, textvariable=self.var_interval, width=8).grid(row=2, column=1, sticky="w")
        ttk.Label(settings, text="Limit (0 = all)").grid(row=2, column=2, padx=6, sticky="e")
        ttk.Entry(settings, textvariable=self.var_limit, width=8).grid(row=2, column=3, sticky="w")
        ttk.Label(settings, text="Output file names use the date and time of the run.",
                  foreground="gray").grid(row=2, column=4, columnspan=2, padx=6, sticky="w")
        settings.columnconfigure(1, weight=1)

        buttons = ttk.Frame(f)
        buttons.pack(fill="x", padx=10, pady=6)
        self.btn_run = ttk.Button(buttons, text="Run", command=self.start_run)
        self.btn_run.pack(side="left", padx=4)
        self.btn_stop = ttk.Button(buttons, text="Stop", command=self.stop_run, state="disabled")
        self.btn_stop.pack(side="left", padx=4)
        self.lbl_progress = ttk.Label(buttons, text="idle")
        self.lbl_progress.pack(side="left", padx=12)
        self.pb = ttk.Progressbar(f, maximum=100)
        self.pb.pack(fill="x", padx=10, pady=4)

        logframe = ttk.Frame(f)
        logframe.pack(fill="both", expand=True, padx=10, pady=6)
        self.txt_log = tk.Text(logframe, height=20, wrap="word")
        scroll = ttk.Scrollbar(logframe, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=scroll.set)
        self.txt_log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def log(self, msg):
        self.txt_log.insert("end", msg + "\n")
        self.txt_log.see("end")

    def browse_cmd(self):
        path = filedialog.askopenfilename(filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if path:
            self.var_cmdfile.set(path)

    def browse_out(self):
        path = filedialog.askdirectory()
        if path:
            self.var_outdir.set(path)

    def _conn_params(self):
        host = self.var_host.get().strip()
        port = int(self.var_port.get())
        timeout = float(self.var_timeout.get())
        if not 1 <= port <= 65535:
            raise ValueError("port must be 1 to 65535")
        if timeout <= 0:
            raise ValueError("timeout must be greater than 0")
        return host, port, timeout

    def test_connection(self):
        """Open and close a TCP connection without sending any command."""
        try:
            host, port, timeout = self._conn_params()
        except ValueError as e:
            messagebox.showerror("Input error", str(e))
            return
        try:
            socket.create_connection((host, port), timeout=timeout).close()
            self.lbl_conn.config(text=f"connected to {host}:{port}", foreground="green")
            self.log(f"Connection test OK: {host}:{port}")
        except OSError as e:
            self.lbl_conn.config(text="failed", foreground="red")
            self.log(f"Connection test FAILED: {e}")

    def start_run(self):
        if self.worker and self.worker.is_alive():
            return
        try:
            host, port, timeout = self._conn_params()
            interval_ms = float(self.var_interval.get())
            limit = int(self.var_limit.get())
            if interval_ms < 0 or limit < 0:
                raise ValueError("interval and limit must not be negative")
        except ValueError as e:
            messagebox.showerror("Input error", str(e))
            return

        cmd_path = self.var_cmdfile.get().strip()
        if not os.path.isfile(cmd_path):
            messagebox.showerror("Missing file", "Choose a command CSV file first.")
            return
        with open(cmd_path, newline="", encoding="utf-8-sig") as f:
            cmds = list(csv.DictReader(f))
        if limit > 0:
            cmds = cmds[:limit]
        if not cmds:
            messagebox.showerror("Empty file", "The command CSV has no commands.")
            return

        label = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        outdir = self.var_outdir.get().strip() or os.getcwd()
        os.makedirs(outdir, exist_ok=True)
        out_csv = os.path.join(outdir, f"test1_{label}_results.csv")
        if os.path.exists(out_csv):
            out_csv = os.path.join(outdir, f"test1_{label}_results_{datetime.now():%H%M%S}.csv")

        self.cfg = {"host": host, "port": port, "timeout": timeout, "interval_ms": interval_ms,
                    "label": label, "commands": cmd_path, "out_csv": out_csv, "n": len(cmds)}
        self.run_rows = []
        self.stop_evt.clear()
        self.pb.configure(maximum=len(cmds), value=0)
        self.btn_run.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.log(f"Starting run {label}: {len(cmds)} commands, every {interval_ms:.0f} ms, "
                 f"to {host}:{port}")
        self.worker = threading.Thread(target=self._worker, args=(cmds,), daemon=True)
        self.worker.start()

    def stop_run(self):
        self.stop_evt.set()
        self.log("Stop requested. Finishing the current command, then saving.")

    def _worker(self, cmds):
        """Runs in a background thread so the window stays responsive."""
        try:
            cfg = self.cfg
            interval = cfg["interval_ms"] / 1000.0
            start = time.perf_counter()
            for i, row in enumerate(cmds):
                if self.stop_evt.is_set():
                    break
                target = start + i * interval
                while time.perf_counter() < target and not self.stop_evt.is_set():
                    time.sleep(0.002)
                send_wall = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
                send_perf = time.perf_counter()
                res = send_one(cfg["host"], cfg["port"], row["command"], cfg["timeout"])
                rec = {
                    "index": row["index"], "category": row["category"], "command": row["command"],
                    "expected_firmware": row["expected_firmware"], "send_time": send_wall,
                    "send_offset_ms": f"{(send_perf - start) * 1000:.3f}",
                    "connected": res["connected"], "connect_ms": res["connect_ms"],
                    "reply": res["reply"], "reply_ms": res["reply_ms"], "error": res["error"],
                }
                self.q.put(("row", i + 1, rec))
            self.q.put(("done", self.stop_evt.is_set()))
        except Exception as e:
            self.q.put(("error", str(e)))

    def _poll_queue(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "row":
                    _, count, rec = item
                    self.run_rows.append(rec)
                    self.pb["value"] = count
                    self.lbl_progress.config(text=f"{count} / {self.cfg['n']} sent")
                    expected_ok = fw_matches(rec["reply"], rec["expected_firmware"])
                    if not (rec["connected"] and expected_ok) and rec["category"] != "malformed":
                        self.log(f"  index {rec['index']}: connected={rec['connected']} "
                                 f"reply='{rec['reply']}' {rec['error']}")
                elif kind == "done":
                    self._finish_run(stopped=item[1])
                elif kind == "error":
                    self.log(f"Worker error: {item[1]}")
                    self._reset_buttons()
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _reset_buttons(self):
        self.btn_run.config(state="normal")
        self.btn_stop.config(state="disabled")

    def _finish_run(self, stopped):
        self._reset_buttons()
        cfg = self.cfg
        with open(cfg["out_csv"], "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=RUN_FIELDS)
            w.writeheader()
            w.writerows(self.run_rows)
        summary_path = cfg["out_csv"][:-4] + "_summary.txt"
        text = build_run_text(self.run_rows, cfg, stopped)
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        if hasattr(self, "var_runcsv"):
            self.var_runcsv.set(cfg["out_csv"])
        self.lbl_progress.config(text=f"finished: {len(self.run_rows)} sent")
        self.log("\n" + text)
        self.log(f"\nSaved: {cfg['out_csv']}\nSaved: {summary_path}")
        self.log("Export the adapter log from YeLink_AdapterUI and load both files in tab 2.")

    # ---------- Tab 2: compare ----------
    def _build_compare_tab(self):
        f = self.tab_cmp

        inputs = ttk.LabelFrame(f, text="Input files")
        inputs.pack(fill="x", padx=10, pady=6)
        self.var_runcsv = tk.StringVar()
        self.var_adcsv = tk.StringVar()
        ttk.Label(inputs, text="Run CSV (tab 1)").grid(row=0, column=0, padx=6, pady=4, sticky="e")
        ttk.Entry(inputs, textvariable=self.var_runcsv, width=90).grid(row=0, column=1, sticky="we")
        ttk.Button(inputs, text="Browse...", command=lambda: self._browse_into(self.var_runcsv)).grid(row=0, column=2, padx=6)
        ttk.Label(inputs, text="Adapter log CSV").grid(row=1, column=0, padx=6, pady=4, sticky="e")
        ttk.Entry(inputs, textvariable=self.var_adcsv, width=90).grid(row=1, column=1, sticky="we")
        ttk.Button(inputs, text="Browse...", command=lambda: self._browse_into(self.var_adcsv)).grid(row=1, column=2, padx=6)
        inputs.columnconfigure(1, weight=1)

        bar = ttk.Frame(f)
        bar.pack(fill="x", padx=10, pady=4)
        ttk.Button(bar, text="Compare and plot", command=self.compare).pack(side="left", padx=4)
        ttk.Button(bar, text="Save figure (PNG)", command=self.save_figure).pack(side="left", padx=4)

        body = ttk.Frame(f)
        body.pack(fill="both", expand=True, padx=10, pady=6)
        left = ttk.Frame(body)
        left.pack(side="left", fill="y")
        self.txt_summary = tk.Text(left, width=66, wrap="word", font=("Consolas", 9))
        self.txt_summary.pack(fill="both", expand=True)

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self.fig = Figure(figsize=(9, 6.5), dpi=100)
        self.canvas = FigureCanvasTkAgg(self.fig, master=right)
        toolbar = NavigationToolbar2Tk(self.canvas, right)
        toolbar.update()
        toolbar.pack(side="top", fill="x")
        self.canvas.get_tk_widget().pack(side="top", fill="both", expand=True)

    def _browse_into(self, var):
        path = filedialog.askopenfilename(filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if path:
            var.set(path)

    def compare(self):
        run_path = self.var_runcsv.get().strip()
        ad_path = self.var_adcsv.get().strip()
        if not (os.path.isfile(run_path) and os.path.isfile(ad_path)):
            messagebox.showerror("Missing file", "Choose both the run CSV and the adapter log CSV.")
            return
        try:
            runs = load_run(run_path)
            adapter = load_adapter(ad_path)
        except (KeyError, ValueError) as e:
            messagebox.showerror("File format", f"Could not read the files: {e}")
            return

        result = analyze_compare(runs, adapter)
        self.txt_summary.delete("1.0", "end")
        self.txt_summary.insert("1.0", result["text"])
        self._plot(runs, result)

    def _plot(self, runs, result):
        self.fig.clf()
        axes = self.fig.subplots(2, 2)

        # (a) round trip by command index, colored by category
        ax = axes[0, 0]
        for cat, color in CATEGORY_COLORS.items():
            pts = [(r["index"], r["reply_ms"]) for r in result["matched_rows"]
                   if r["category"] == cat and np.isfinite(r["reply_ms"])]
            if pts:
                xs, ys = zip(*pts)
                ax.scatter(xs, ys, s=8, color=color, label=f"{cat} ({len(xs)})", alpha=0.7)
        ax.set_title("Round-trip time by command")
        ax.set_xlabel("command index")
        ax.set_ylabel("ms")
        ax.legend(fontsize=8)

        # (b) round-trip distribution
        ax = axes[0, 1]
        rt = finite(result["rt"])
        if rt.size:
            ax.hist(rt, bins=40, color="#1f77b4")
        ax.set_title("Round-trip time distribution")
        ax.set_xlabel("ms")
        ax.set_ylabel("count")

        # (c) pacing
        ax = axes[1, 0]
        iv = finite(result["intervals"])
        if iv.size:
            ax.hist(iv, bins=40, color="#9467bd")
            ax.axvline(np.median(iv), color="k", ls="--", lw=1, label=f"median {np.median(iv):.2f} ms")
            ax.legend(fontsize=8)
        ax.set_title("Interval between command starts")
        ax.set_xlabel("ms")
        ax.set_ylabel("count")

        # (d) host-side delay: adapter receipt minus send
        ax = axes[1, 1]
        dl = finite(result["delays"])
        if dl.size:
            ax.hist(dl, bins=40, color="#ff7f0e")
            ax.axvline(np.median(dl), color="k", ls="--", lw=1, label=f"median {np.median(dl):.2f} ms")
            ax.legend(fontsize=8)
        else:
            ax.text(0.5, 0.5, "no matched rows", ha="center", va="center", transform=ax.transAxes)
        ax.set_title("Adapter receipt minus send time (host-side)")
        ax.set_xlabel("ms")
        ax.set_ylabel("count")

        self.fig.tight_layout()
        self.canvas.draw()

    def save_figure(self):
        path = filedialog.asksaveasfilename(defaultextension=".png",
                                            filetypes=[("PNG image", "*.png")],
                                            initialfile="test1_comparison.png")
        if path:
            self.fig.savefig(path, dpi=200)
            self.log(f"Figure saved: {path}") if hasattr(self, "log") else None


if __name__ == "__main__":
    App().mainloop()