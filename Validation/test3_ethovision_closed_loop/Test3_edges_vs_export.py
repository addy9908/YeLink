# -*- coding: utf-8 -*-
"""
Test3_edges_vs_export.py  --  Test 3: edge time minus exported zone event, for the IO box and for YeLink

Companion to test3_closedloop_gui.py (same folder). It reuses that program's analysis to pair every IO-box and
YeLink edge with the EthoVision zone entry / exit it belongs to, fits edge time as a straight line of export time
for each line separately, removes the median offset of the line, and reports the spread and the events that are
more than one video frame (33.3 ms at 30 fps) from the line.

It also asks whether the two lines are late together: Pearson and Spearman correlation of the IO-box offset with the
YeLink offset for the same zone event, with and without events more than one frame late, and a scatter plot.
A shared delay (EthoVision video tracking and rule evaluation) makes the two offsets correlate; delays that arise
separately in each path (launch of the external program, USB path of the IO box) do not.

Usage:  python Test3_edges_vs_export.py <results folder of a Test 3 run> [--io-pin 2] [--yl-pin 3]
Output: test3_edges_vs_export.txt, .csv and test3_IObox_vs_YeLink_offsets.png in the results folder.
Needs: numpy, matplotlib (and the files written by test3_closedloop_gui.py: edges.csv, meta.json, the EthoVision export .xlsx).
"""
import argparse
import csv
import glob
import importlib.util
import os
import sys

import numpy as np

FRAME_MS = 1000.0 / 30.0          # one video frame at 30 fps


def load_t3():
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        sys.path.insert(0, here)
        import test3_closedloop_gui as T3
        return T3
    except Exception:             # tkinter not available (headless): stub it, the analysis does not need it
        from unittest.mock import MagicMock
        for n in ("tkinter", "tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox", "matplotlib.backends.backend_tkagg"):
            sys.modules[n] = MagicMock()
        spec = importlib.util.spec_from_file_location("t3", os.path.join(here, "test3_closedloop_gui.py"))
        T3 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(T3)
        return T3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--io-pin", type=int, default=2)
    ap.add_argument("--yl-pin", type=int, default=3)
    a = ap.parse_args()
    T3 = load_t3()
    xl = glob.glob(os.path.join(a.folder, "Raw data*.xlsx"))[0]
    res = T3.analyze_folder(a.folder, io_pin=a.io_pin, yl_pin=a.yl_pin, invert_io=False, etho_path=xl)
    ev = res["etho"]["events"]
    ent = [t for t, z, i in ev if z == 1]
    ext = [t for t, z, i in ev if z == 0]
    lines = [f"Test 3: edge time minus exported zone event (median of each line removed); one frame = {FRAME_MS:.1f} ms",
             f"Entries {len(ent)}, exits {len(ext)} in the EthoVision export", ""]
    rows = []
    for lab, pulses in (("IO box", res["io_pulses"]), ("YeLink", res["yl_pulses"])):
        allr = []
        for kind, ex, key in (("entry", ent, "rise"), ("exit", ext, "fall")):
            e = np.array([p[key] for p in pulses])
            x = np.array(ex)
            b, c = np.polyfit(x, e, 1)
            r = (e - (c + b * x)) * 1000.0
            r = r - np.median(r)
            for k, (t, rr) in enumerate(zip(x, r)):
                allr.append((kind, k + 1, float(t), float(rr)))
                rows.append((lab, kind, k + 1, f"{t:.3f}", f"{rr:.2f}"))
        v = np.array([x[3] for x in allr])
        late = [x for x in allr if x[3] > FRAME_MS]
        lines.append(f"{lab}: n = {v.size}, range {v.min():+.1f} to {v.max():+.1f} ms, "
                     f"IQR {np.percentile(v, 25):+.1f} to {np.percentile(v, 75):+.1f} ms; "
                     f"events more than one frame late: {len(late)}" +
                     ("".join(f"  [{x[0]} {x[1]} at export t = {x[2]:.1f} s: {x[3]:+.1f} ms]" for x in late)))
    # ---- are the two lines late together?  (same zone event, IO box vs YeLink)
    io_rows = [r for r in rows if r[0] == "IO box"]
    yl_rows = [r for r in rows if r[0] == "YeLink"]
    X = np.array([float(r[4]) for r in io_rows]); Y = np.array([float(r[4]) for r in yl_rows])
    kind = np.array([r[1] for r in io_rows])

    def rank(v):
        o = v.argsort(); out_ = np.empty(len(v)); out_[o] = np.arange(len(v)); return out_

    def corr(x, y, label):
        r = np.corrcoef(x, y)[0, 1]; rho = np.corrcoef(rank(x), rank(y))[0, 1]; cov = np.cov(x, y)[0, 1]
        rng = np.random.RandomState(0)
        perm = np.array([np.corrcoef(x, rng.permutation(y))[0, 1] for _ in range(20000)])
        return (f"{label}: n = {len(x)}, Pearson r = {r:.2f} (R2 = {r * r:.2f}), Spearman rho = {rho:.2f}, permutation p = {np.mean(np.abs(perm) >= abs(r)):.4f}; "
                f"shared variance {100 * cov / x.var(ddof=1):.0f}% (IO box) and {100 * cov / y.var(ddof=1):.0f}% (YeLink); SD of the YeLink minus IO-box difference {np.std(y - x, ddof=1):.1f} ms"), r
    lines += ["", "Correlation of the two lines across zone events (offsets with the median of each line and edge type removed):"]
    t_all, r_all = corr(X, Y, "all events"); lines.append("  " + t_all)
    keep = (np.abs(X) <= FRAME_MS) & (np.abs(Y) <= FRAME_MS)
    t_nl, r_nl = corr(X[keep], Y[keep], "without events more than one frame from the line"); lines.append("  " + t_nl)
    for k in ("entry", "exit"):
        t_k, _ = corr(X[kind == k], Y[kind == k], k + "s"); lines.append("  " + t_k)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(4.6, 4.4), dpi=200)
        ax.axvspan(-FRAME_MS, FRAME_MS, color="0.93", zorder=0); ax.axhspan(-FRAME_MS, FRAME_MS, color="0.93", zorder=0)
        lim = 45
        ax.plot([-lim, lim], [-lim, lim], "k--", lw=0.8, label="y = x", zorder=1)
        for k, col, mk, lab in (("entry", "#1f77b4", "o", "Entries (ON)"), ("exit", "#ff7f0e", "^", "Exits (OFF)")):
            m = kind == k
            ax.scatter(X[m], Y[m], s=22, color=col, marker=mk, alpha=0.85, label=f"{lab}, n = {int(m.sum())}", zorder=3)
        for xi, yi in zip(X, Y):
            if xi > FRAME_MS and yi > FRAME_MS:
                ax.annotate("same exit late\non both lines", (xi, yi), xytext=(-8, -30), textcoords="offset points", ha="right", fontsize=7,
                            arrowprops=dict(arrowstyle="-", lw=0.6))
        ax.set_xlim(-lim, lim); ax.set_ylim(-lim, lim); ax.set_aspect("equal")
        ax.set_xlabel("IO box edge − exported zone event (ms)"); ax.set_ylabel("YeLink edge − exported zone event (ms)")
        ax.text(0.03, 0.97, f"r = {r_all:.2f} (all 70 events)\nr = {r_nl:.2f} (without the late exit)", transform=ax.transAxes, va="top", fontsize=8)
        ax.text(0.97, 0.03, "shaded: within one frame (33.3 ms)", transform=ax.transAxes, ha="right", fontsize=6.5, color="0.35")
        ax.legend(fontsize=7, loc="lower right", bbox_to_anchor=(1.0, 0.07), frameon=False)
        fig.tight_layout()
        fig.savefig(os.path.join(a.folder, "test3_IObox_vs_YeLink_offsets.png"), dpi=300)
    except Exception as e:                                   # plotting is optional
        lines.append(f"(plot not written: {e})")
    out = os.path.join(a.folder, "test3_edges_vs_export")
    with open(out + ".txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(out + ".csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["line", "event", "index", "export_time_s", "edge_minus_export_ms"])
        w.writerows(rows)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
