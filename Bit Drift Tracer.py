#!/usr/bin/env python3
import sys; sys.dont_write_bytecode = True
"""
Bit Drift Tracer  v1.1.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Measures host-visible file read speed against file age (filesystem timestamps).

Read speed that stays flat across age buckets is the expected result. A drop for
older files is a prompt to investigate; this tool cannot see charge loss or ECC
effort, so it cannot say why.

Requirements:  pip install matplotlib numpy
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import ctypes
import os
import sys
import time
import threading
import random
import csv
import queue
from pathlib import Path
from datetime import datetime
from typing import Callable, List, Optional, Tuple

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import numpy as np

import drift_fit

__version__ = "1.1.0"

# ─── Palette ──────────────────────────────────────────────────────────────────
BG    = "#0d1117"
BG2   = "#161b22"
BG3   = "#21262d"
BORD  = "#30363d"
TEXT  = "#e6edf3"
MUTED = "#8b949e"
ACNT  = "#58a6ff"

# Each bracket: (age_lo_days, age_hi_days, hex_colour, label)
AGE_BRACKETS: List[Tuple[float, float, str, str]] = [
    (0,    90,   "#3fb950", "< 3 months"),
    (90,   365,  "#58a6ff", "3 mo – 1 yr"),
    (365,  730,  "#d29922", "1 – 2 yrs"),
    (730,  1460, "#db6d28", "2 – 4 yrs"),
    (1460, 1e9,  "#f85149", "4 + years"),
]

# Log-axis tick positions: (age in days, label)
LOG_TICKS: List[Tuple[float, str]] = [
    (0, "0"), (7, "1 wk"), (30, "1 mo"), (90, "3 mo"), (365, "1 yr"),
    (730, "2 yr"), (1460, "4 yr"), (2920, "8 yr"),
]

# ─── Win32 unbuffered reader ───────────────────────────────────────────────────
SECTOR   = 4096
CHUNK    = 4 * 1024 * 1024      # bytes to read per file (4 MB)
SPEED_CAP = 30_000              # MB/s sanity limit

_ON_WIN = sys.platform == "win32"
_k32    = ctypes.windll.kernel32 if _ON_WIN else None

_GENERIC_READ    = 0x80000000
_SHARE_RW        = 0x00000003
_OPEN_EXISTING   = 3
_NO_BUF          = 0x20000000   # FILE_FLAG_NO_BUFFERING  — bypass OS page cache
_RAND_ACCESS     = 0x10000000   # FILE_FLAG_RANDOM_ACCESS — suppresses prefetch hint
_MEM_CR          = 0x3000       # MEM_COMMIT | MEM_RESERVE
_PAGE_RW         = 0x04
_MEM_REL         = 0x8000
_FILE_BEGIN      = 0            # move method for SetFilePointerEx
# INVALID_HANDLE_VALUE = 0xFFFF...FFFF on both 32-bit and 64-bit Windows
_INVALID_HANDLE  = ctypes.c_size_t(-1).value

# Declare argtypes + restypes for every Win32 call we make.
# Without argtypes, ctypes defaults arguments to c_int (32-bit), which
# overflows when passing a 64-bit pointer returned by VirtualAlloc.
if _k32:
    _VP  = ctypes.c_void_p
    _UL  = ctypes.c_ulong
    _SZ  = ctypes.c_size_t
    _PUL = ctypes.POINTER(ctypes.c_ulong)

    _k32.VirtualAlloc.restype  = _VP
    _k32.VirtualAlloc.argtypes = [_VP, _SZ, _UL, _UL]

    _k32.VirtualFree.restype   = ctypes.c_bool
    _k32.VirtualFree.argtypes  = [_VP, _SZ, _UL]

    _k32.CreateFileW.restype   = _VP   # HANDLE

    _k32.ReadFile.restype      = ctypes.c_bool
    _k32.ReadFile.argtypes     = [_VP, _VP, _UL, _PUL, _VP]

    _k32.CloseHandle.restype   = ctypes.c_bool
    _k32.CloseHandle.argtypes  = [_VP]

    # SetFilePointerEx — used to seek to a random offset before each read
    _k32.SetFilePointerEx.restype  = ctypes.c_bool
    _k32.SetFilePointerEx.argtypes = [_VP, ctypes.c_int64, _VP, _UL]


class _AlignedBuf:
    """Page-aligned memory for FILE_FLAG_NO_BUFFERING reads (Windows)."""

    def __init__(self, size: int):
        self.size = size
        self.ptr  = None
        if _k32:
            p = _k32.VirtualAlloc(None, size, _MEM_CR, _PAGE_RW)
            self.ptr = p if p else None

    def __del__(self):
        if self.ptr and _k32:
            _k32.VirtualFree(self.ptr, 0, _MEM_REL)

    @property
    def ok(self) -> bool:
        return self.ptr is not None


def _random_offset(fsize: int, read_sz: int) -> int:
    """Return a sector-aligned random offset that leaves room for read_sz bytes."""
    if fsize <= read_sz:
        return 0
    # max start position, aligned down to sector boundary
    max_start = ((fsize - read_sz) // SECTOR) * SECTOR
    if max_start <= 0:
        return 0
    # pick a random sector index and convert back to bytes
    return random.randint(0, max_start // SECTOR) * SECTOR


def _win_read(path: str, buf: "_AlignedBuf") -> Optional[float]:
    """
    Unbuffered read from a random offset — bypasses OS page cache.

    Reading from a random position (not offset 0) avoids the drive's DRAM cache
    because file headers and the first few MB are the most-likely-cached regions
    (touched by Explorer, media players, indexers, etc.).
    FILE_FLAG_RANDOM_ACCESS suppresses the storage-driver prefetch hint so the
    drive doesn't pre-load adjacent sectors into its cache before we measure.
    """
    try:
        fsize = os.path.getsize(path)
    except OSError:
        return None
    if fsize < 1:
        return None

    # For files larger than CHUNK, pick a random aligned offset.
    # For smaller files, read the whole file (rounded up to sector).
    if fsize >= CHUNK:
        read_sz = CHUNK
        offset  = _random_offset(fsize, read_sz)
    else:
        read_sz = ((fsize + SECTOR - 1) // SECTOR) * SECTOR
        offset  = 0

    h = _k32.CreateFileW(
        path, _GENERIC_READ, _SHARE_RW, None,
        _OPEN_EXISTING, _NO_BUF | _RAND_ACCESS, None,
    )
    if h is None or h == _INVALID_HANDLE:
        return None

    try:
        if offset:
            if not _k32.SetFilePointerEx(h, ctypes.c_int64(offset), None, _FILE_BEGIN):
                return None

        n  = ctypes.c_ulong(0)
        t0 = time.perf_counter()
        ok = _k32.ReadFile(h, buf.ptr, read_sz, ctypes.byref(n), None)
        t1 = time.perf_counter()
    finally:
        _k32.CloseHandle(h)

    if not ok or n.value == 0:
        return None
    dt = t1 - t0
    if dt < 5e-5:
        return None                     # suspiciously fast → discard
    spd = n.value / dt / 1_048_576
    return spd if spd <= SPEED_CAP else None


def _py_read(path: str) -> Optional[float]:
    """Buffered fallback (non-Windows or if VirtualAlloc fails).
    Also reads from a random offset to avoid cached file-header data."""
    try:
        fsize = os.path.getsize(path)
        if fsize < 1:
            return None
        offset = _random_offset(fsize, CHUNK) if fsize >= CHUNK else 0
        t0, total = time.perf_counter(), 0
        with open(path, "rb") as f:
            if offset:
                f.seek(offset)
            while total < CHUNK:
                blk = f.read(65536)
                if not blk:
                    break
                total += len(blk)
        dt = time.perf_counter() - t0
        if dt < 1e-5 or total == 0:
            return None
        spd = total / dt / 1_048_576
        return spd if spd <= SPEED_CAP else None
    except OSError:
        return None


def measure_speed(path: str, buf: "_AlignedBuf") -> Optional[float]:
    if _ON_WIN and buf.ok:
        return _win_read(path, buf)
    return _py_read(path)


# ─── Data model ───────────────────────────────────────────────────────────────
class Result:
    __slots__ = ("path", "age_days", "speed_mbs", "size_mb")

    def __init__(self, path: str, age_days: float, speed_mbs: float, size_mb: float):
        self.path      = path
        self.age_days  = age_days
        self.speed_mbs = speed_mbs
        self.size_mb   = size_mb


# ─── Scanner thread ────────────────────────────────────────────────────────────
class Scanner:
    """
    Walks root and measures each qualifying file immediately as it is found.
    All results are posted to a queue.Queue; the UI polls that queue on the
    main thread so tkinter is never touched from the worker thread.

    Queue message format:  (tag, payload)
      ('result', Result)
      ('skip',   (path, reason))
      ('status', str)          — informational text (e.g. gathering progress)
      ('done',   None)
    """

    def __init__(self, root: str, min_bytes: int, randomize: bool,
                 out_queue: "queue.Queue"):
        self._root      = root
        self._min_bytes = min_bytes
        self._randomize = randomize
        self._q         = out_queue
        self._stop      = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    # ── internals ──────────────────────────────────────────────────────────────
    def _put(self, tag, payload=None):
        self._q.put((tag, payload))

    def _measure_one(self, fp: str, now: float, buf: _AlignedBuf):
        try:
            st      = os.stat(fp)
            wt      = min(st.st_mtime, st.st_ctime)
            age     = (now - wt) / 86400.0
            size_mb = st.st_size / 1_048_576
            speed   = measure_speed(fp, buf)
            if speed is None or speed <= 0:
                self._put('skip', (fp, "read failed"))
            else:
                self._put('result', Result(fp, age, speed, size_mb))
        except OSError as exc:
            self._put('skip', (fp, str(exc)))

    def _run(self):
        buf = _AlignedBuf(CHUNK)
        now = time.time()

        if self._randomize:
            # ── gather first, then shuffle, then measure ───────────────────
            pool: List[str] = []
            try:
                for dirpath, _, fnames in os.walk(self._root):
                    if self._stop.is_set():
                        break
                    for fn in fnames:
                        if self._stop.is_set():
                            break
                        fp = os.path.join(dirpath, fn)
                        try:
                            if os.path.getsize(fp) >= self._min_bytes:
                                pool.append(fp)
                                if len(pool) % 200 == 0:
                                    self._put('status',
                                              f"Gathering…  {len(pool):,} files found")
                        except OSError:
                            pass
            except (PermissionError, OSError):
                pass

            if not self._stop.is_set():
                random.shuffle(pool)
                self._put('status', f"Measuring {len(pool):,} files…")
                for fp in pool:
                    if self._stop.is_set():
                        break
                    self._measure_one(fp, now, buf)
        else:
            # ── walk and measure immediately — true real-time ──────────────
            try:
                for dirpath, _, fnames in os.walk(self._root):
                    if self._stop.is_set():
                        break
                    for fn in fnames:
                        if self._stop.is_set():
                            break
                        fp = os.path.join(dirpath, fn)
                        try:
                            if os.path.getsize(fp) >= self._min_bytes:
                                self._measure_one(fp, now, buf)
                        except OSError:
                            pass
            except (PermissionError, OSError):
                pass

        self._put('done')


# ─── Application ──────────────────────────────────────────────────────────────
class App:
    _POLL_MS      = 60      # how often to drain the result queue (ms)
    _GRAPH_MS     = 2000    # how often to redraw the graph while scanning
    _GRAPH_MIN    = 20      # also redraw after this many new results

    def __init__(self, root: tk.Tk):
        self.root     = root
        self.results: List[Result]          = []
        self.skipped: List[Tuple[str, str]] = []
        self._lock    = threading.Lock()
        self._scanner: Optional[Scanner]    = None
        self._queue:   queue.Queue          = queue.Queue()
        self._poll_job:  Optional[str]      = None
        self._graph_job: Optional[str]      = None
        self._graph_timer: Optional[str]    = None
        self._t_start  = 0.0
        self._n        = 0       # result count (fast path, avoids len())
        self._sum_spd  = 0.0    # running speed sum for avg
        self._pending  = 0       # new results since last graph draw

        self._build_ui()
        self._init_graph()
        self._set_status("Ready.  Choose a drive or folder and press  ▶ Scan.")

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        r = self.root
        r.title("Bit Drift Tracer")
        r.configure(bg=BG)
        r.minsize(960, 640)
        r.geometry("1200x760")

        st = ttk.Style()
        st.theme_use("clam")
        st.configure("TFrame",      background=BG)
        st.configure("TLabel",      background=BG, foreground=TEXT,
                     font=("Segoe UI", 10))
        st.configure("TButton",     background=BG3, foreground=TEXT,
                     relief="flat", padding=(10, 5), font=("Segoe UI", 10))
        st.configure("Accent.TButton", background=ACNT, foreground="#ffffff",
                     relief="flat", padding=(12, 6), font=("Segoe UI", 10, "bold"))
        st.configure("Stop.TButton", background="#f85149", foreground="#ffffff",
                     relief="flat", padding=(12, 6), font=("Segoe UI", 10, "bold"))
        st.configure("TEntry",       fieldbackground=BG3, foreground=TEXT,
                     insertcolor=TEXT, relief="flat", font=("Segoe UI", 10))
        st.configure("TCheckbutton", background=BG, foreground=TEXT,
                     font=("Segoe UI", 10))
        st.configure("Horizontal.TProgressbar", troughcolor=BG3,
                     background=ACNT, thickness=5, relief="flat")

        # ── header ────────────────────────────────────────────────────────────
        hdr = ttk.Frame(r)
        hdr.pack(fill="x", padx=16, pady=(14, 0))

        tk.Label(hdr, text="Bit Drift Tracer", bg=BG, fg=TEXT,
                 font=("Segoe UI", 17, "bold")).pack(side="left")
        tk.Label(hdr, text="  read speed vs. data age",
                 bg=BG, fg=MUTED, font=("Segoe UI", 10)).pack(side="left")

        # ── controls row ──────────────────────────────────────────────────────
        ctrl = ttk.Frame(r)
        ctrl.pack(fill="x", padx=16, pady=(10, 6))

        tk.Label(ctrl, text="Target:", bg=BG, fg=MUTED,
                 font=("Segoe UI", 10)).pack(side="left")

        self._target_var = tk.StringVar()
        ttk.Entry(ctrl, textvariable=self._target_var, width=46
                  ).pack(side="left", padx=(6, 4))

        ttk.Button(ctrl, text="Browse…", command=self._browse
                   ).pack(side="left", padx=(0, 20))

        tk.Label(ctrl, text="Min size (MB):", bg=BG, fg=MUTED,
                 font=("Segoe UI", 10)).pack(side="left")
        self._min_mb_var = tk.StringVar(value="0.5")
        ttk.Entry(ctrl, textvariable=self._min_mb_var, width=6
                  ).pack(side="left", padx=(4, 20))

        self._rand_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctrl, text="Randomize order",
                        variable=self._rand_var).pack(side="left", padx=(0, 20))

        self._log_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctrl, text="Log age axis and fit",
                        variable=self._log_var,
                        command=self._on_axis_toggle).pack(side="left",
                                                           padx=(0, 20))

        self._scan_btn = ttk.Button(ctrl, text="▶  Scan",
                                    style="Accent.TButton",
                                    command=self._start_scan)
        self._scan_btn.pack(side="left", padx=(0, 6))

        self._stop_btn = ttk.Button(ctrl, text="■  Stop",
                                    style="Stop.TButton",
                                    command=self._stop_scan, state="disabled")
        self._stop_btn.pack(side="left")

        # ── progress ──────────────────────────────────────────────────────────
        pf = ttk.Frame(r)
        pf.pack(fill="x", padx=16, pady=(0, 0))
        self._progress = ttk.Progressbar(pf, orient="horizontal",
                                         mode="indeterminate",
                                         style="Horizontal.TProgressbar")
        self._progress.pack(fill="x")

        # ── separator ─────────────────────────────────────────────────────────
        tk.Frame(r, bg=BORD, height=1).pack(fill="x", pady=(6, 0))

        # ── fit summary + caveat ──────────────────────────────────────────────
        self._fit_var = tk.StringVar(value=drift_fit.CONFOUND_NOTE)
        tk.Label(r, textvariable=self._fit_var, bg=BG, fg=MUTED,
                 font=("Segoe UI", 8), anchor="w", justify="left",
                 wraplength=1120).pack(fill="x", padx=16, pady=(4, 4))
        tk.Frame(r, bg=BORD, height=1).pack(fill="x")

        # ── main split: sidebar | graph ────────────────────────────────────────
        main = tk.Frame(r, bg=BG)
        main.pack(fill="both", expand=True)

        # sidebar
        side = tk.Frame(main, bg=BG2, width=216)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        self._build_sidebar(side)

        tk.Frame(main, bg=BORD, width=1).pack(side="left", fill="y")

        # graph
        self._graph_host = tk.Frame(main, bg=BG)
        self._graph_host.pack(side="left", fill="both", expand=True)

        # ── status bar ────────────────────────────────────────────────────────
        tk.Frame(r, bg=BORD, height=1).pack(fill="x")
        self._status_var = tk.StringVar()
        tk.Label(r, textvariable=self._status_var, bg=BG2, fg=MUTED,
                 font=("Segoe UI", 9), anchor="w",
                 padx=12, pady=5).pack(fill="x", side="bottom")

    def _build_sidebar(self, parent: tk.Frame):
        def block(label: str) -> tk.StringVar:
            f = tk.Frame(parent, bg=BG2)
            f.pack(fill="x", padx=14, pady=(10, 0))
            tk.Label(f, text=label, fg=MUTED, bg=BG2,
                     font=("Segoe UI", 7, "bold")).pack(anchor="w")
            var = tk.StringVar(value="—")
            tk.Label(f, textvariable=var, fg=TEXT, bg=BG2,
                     font=("Segoe UI", 14, "bold")).pack(anchor="w")
            return var

        tk.Label(parent, text="Live Stats", fg=TEXT, bg=BG2,
                 font=("Segoe UI", 11, "bold"), pady=10).pack(fill="x", padx=14)
        tk.Frame(parent, bg=BORD, height=1).pack(fill="x")

        self._sv_measured = block("FILES MEASURED")
        self._sv_skipped  = block("FILES SKIPPED")
        self._sv_avg      = block("AVG SPEED  MB/s")
        self._sv_last     = block("LAST SPEED  MB/s")
        self._sv_elapsed  = block("ELAPSED")

        tk.Frame(parent, bg=BORD, height=1).pack(fill="x", pady=(16, 0))

        tk.Label(parent, text="Current file", fg=MUTED, bg=BG2,
                 font=("Segoe UI", 7, "bold"), pady=4
                 ).pack(fill="x", padx=14, anchor="w")
        self._sv_curfile = tk.StringVar(value="—")
        tk.Label(parent, textvariable=self._sv_curfile,
                 fg=MUTED, bg=BG2, font=("Segoe UI", 8),
                 wraplength=188, justify="left"
                 ).pack(fill="x", padx=14, anchor="w")

        # export buttons
        tk.Frame(parent, bg=BORD, height=1).pack(fill="x", pady=(20, 0))
        for txt, cmd in [("Export TSV", self._export_tsv),
                         ("Export PNG", self._export_png)]:
            tk.Button(parent, text=txt, command=cmd,
                      bg=BG3, fg=TEXT, relief="flat",
                      font=("Segoe UI", 10), pady=8,
                      activebackground=ACNT, activeforeground="#fff",
                      cursor="hand2").pack(fill="x", padx=14, pady=(8, 0))

    # ── Graph ─────────────────────────────────────────────────────────────────
    def _init_graph(self):
        self._fig = Figure(facecolor=BG)
        gs = self._fig.add_gridspec(
            2, 1, height_ratios=[3, 1],
            hspace=0.50, left=0.08, right=0.97, top=0.92, bottom=0.10,
        )
        self._ax_sc  = self._fig.add_subplot(gs[0])
        self._ax_bar = self._fig.add_subplot(gs[1])
        self._style_axes()

        self._canvas = FigureCanvasTkAgg(self._fig, master=self._graph_host)
        self._canvas.get_tk_widget().pack(fill="both", expand=True)
        self._canvas.draw()

    def _style_axes(self):
        for ax in (self._ax_sc, self._ax_bar):
            ax.set_facecolor(BG2)
            ax.tick_params(colors=MUTED, labelsize=8)
            ax.grid(True, color=BORD, linewidth=0.6, alpha=0.9)
            for sp in ax.spines.values():
                sp.set_color(BORD)

    def _refresh_graph(self):
        self._graph_job = None
        with self._lock:
            data = list(self.results)
        self._pending = 0

        sc, bar = self._ax_sc, self._ax_bar
        sc.cla(); bar.cla()
        self._style_axes()

        sc.set_title("Read Speed  vs.  Data Age",
                     color=TEXT, fontsize=12, fontweight="bold", pad=8)
        log_mode = self._log_var.get()
        sc.set_xlabel("Data Age  (log scale)" if log_mode
                      else "Data Age  (weeks)",
                      color=MUTED, fontsize=9, labelpad=4)
        sc.set_ylabel("Read Speed  (MB/s)", color=MUTED, fontsize=9, labelpad=4)
        bar.set_xlabel("Age bracket", color=MUTED, fontsize=9, labelpad=3)
        bar.set_ylabel("Avg  MB/s", color=MUTED, fontsize=9, labelpad=3)

        if not data:
            self._canvas.draw_idle()
            return

        ages_d  = np.array([r.age_days  for r in data])
        ages_wk = ages_d / 7.0
        speeds  = np.array([r.speed_mbs for r in data])

        # ── X axis: linear timeline in weeks from 0 ────────────────────────
        max_wk = float(ages_wk.max()) if len(ages_wk) else 1.0
        x_max  = max(max_wk * 1.06, 4.0)   # small right margin, min 4 weeks

        import matplotlib.ticker as mticker
        # Log mode plots x = 1 + age in days so zero-day files stay on the axis.
        x_plot = (1.0 + ages_d) if log_mode else ages_wk
        if x_max <= 52:
            major_step, minor_step = 4,  1
        elif x_max <= 156:
            major_step, minor_step = 13, 4
        elif x_max <= 312:
            major_step, minor_step = 26, 13
        else:
            major_step, minor_step = 52, 13

        if log_mode:
            sc.set_xscale("log")
            top = max(float(ages_d.max()) * 1.15, 14.0)
            sc.set_xlim(1, 1.0 + top)
            ticks = [(d, t) for d, t in LOG_TICKS if d <= top]
            sc.xaxis.set_major_locator(
                mticker.FixedLocator([1.0 + d for d, _ in ticks]))
            sc.xaxis.set_major_formatter(
                mticker.FixedFormatter([t for _, t in ticks]))
            sc.xaxis.set_minor_locator(mticker.NullLocator())
        else:
            sc.set_xlim(0, x_max)
            sc.xaxis.set_major_locator(mticker.MultipleLocator(major_step))
            sc.xaxis.set_minor_locator(mticker.MultipleLocator(minor_step))
            sc.grid(True, which="minor", color=BORD, linewidth=0.3, alpha=0.5)
        sc.tick_params(axis="x", which="major", colors=MUTED, labelsize=8)
        sc.tick_params(axis="x", which="minor", colors=BORD,  length=3)

        # ── scatter by age bracket ─────────────────────────────────────────
        for lo, hi, color, label in AGE_BRACKETS:
            m = (ages_d >= lo) & (ages_d < hi)
            if m.any():
                sc.scatter(x_plot[m], speeds[m],
                           c=color, s=5, alpha=0.72,
                           linewidths=0, label=label, rasterized=True)

        # ── trend line: linear in weeks or linear in log age ──────────────
        lin_fit = drift_fit.fit_linear_age(ages_wk, speeds)
        log_fit = drift_fit.fit_log_age(ages_d, speeds)
        self._fit_var.set(self._fit_summary(lin_fit, log_fit))
        if log_mode and log_fit is not None:
            xr = np.geomspace(1.0, x_plot.max() * 1.15, 300)
            sc.plot(xr, log_fit.slope * np.log10(xr) + log_fit.intercept,
                    "--", color="#ffffff", alpha=0.30, linewidth=1.5,
                    label=f"Log fit  ({log_fit.slope:+.1f} MB/s / decade, "
                          f"R² {log_fit.r2:.2f})")
        elif not log_mode and lin_fit is not None:
            xr = np.linspace(0, x_max, 300)
            sc.plot(xr, lin_fit.slope * xr + lin_fit.intercept,
                    "--", color="#ffffff", alpha=0.30, linewidth=1.5,
                    label=f"Trend  ({lin_fit.slope:+.2f} MB/s / wk, "
                          f"R² {lin_fit.r2:.2f})")

        # ── rolling average ────────────────────────────────────────────────
        if len(ages_wk) >= 12:
            order  = np.argsort(ages_wk)
            xo, yo = ages_wk[order], speeds[order]
            win    = max(10, len(xo) // 25)
            kern   = np.ones(win) / win
            ys     = np.convolve(yo, kern, mode="valid")
            xs     = xo[win // 2: win // 2 + len(ys)]
            if log_mode:
                xs = 1.0 + xs * 7.0
            sc.plot(xs, ys, color=ACNT, linewidth=2.2, alpha=0.80,
                    label="Rolling avg", zorder=5)

        # Y axis: pin bottom to 0 AFTER data is plotted so top can autoscale
        sc.set_ylim(bottom=0)

        # legend
        leg = sc.legend(fontsize=7.5, framealpha=0.18,
                        facecolor=BG3, edgecolor=BORD,
                        labelcolor=TEXT, loc="upper right",
                        markerscale=2)

        # ── bar chart: avg speed per age bracket ───────────────────────────
        buckets: dict = {lbl: [] for *_, lbl in AGE_BRACKETS}
        for r in data:
            for lo, hi, _, lbl in AGE_BRACKETS:
                if lo <= r.age_days < hi:
                    buckets[lbl].append(r.speed_mbs)
                    break

        active = [(lo, hi, c, lbl) for (lo, hi, c, lbl) in AGE_BRACKETS
                  if buckets[lbl]]
        if active:
            bl  = [lbl for *_, lbl in active]
            bc  = [c   for _, _, c, _ in active]
            bh  = [float(np.mean(buckets[lbl])) for *_, lbl in active]
            bars = bar.bar(bl, bh, color=bc, alpha=0.85,
                           edgecolor=BORD, linewidth=0.5)
            bar.tick_params(axis="x", labelrotation=0, labelsize=7.5,
                            colors=MUTED)
            for b, h in zip(bars, bh):
                bar.text(b.get_x() + b.get_width() / 2,
                         h * 0.5, f"{h:.0f}",
                         ha="center", va="center",
                         color=TEXT, fontsize=8, fontweight="bold")

        self._canvas.draw_idle()

    def _on_axis_toggle(self):
        self._refresh_graph()

    @staticmethod
    def _fit_summary(lin, log) -> str:
        if lin is None or log is None:
            return drift_fit.CONFOUND_NOTE
        return (f"Fit R²: linear in weeks {lin.r2:.2f}, linear in log age "
                f"{log.r2:.2f}. {drift_fit.SOURCE_NOTE} "
                f"{drift_fit.CONFOUND_NOTE}")

    # ── Scan control ──────────────────────────────────────────────────────────
    def _start_scan(self):
        target = self._target_var.get().strip()
        if not target or not os.path.isdir(target):
            messagebox.showerror("Invalid target",
                                 "Please select a valid drive or folder.")
            return
        try:
            min_mb = float(self._min_mb_var.get())
        except ValueError:
            messagebox.showerror("Settings error",
                                 "Min size must be a number.")
            return

        # reset state
        with self._lock:
            self.results.clear()
            self.skipped.clear()
        # flush any leftover queue messages from a previous scan
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._n       = 0
        self._sum_spd = 0.0
        self._pending = 0
        self._t_start = time.time()

        self._refresh_graph()
        self._reset_stats()
        self._scan_btn.configure(state="disabled")
        self._stop_btn.configure(state="normal")
        self._progress.start(10)
        self._set_status(f"Scanning  {target}  …")

        self._scanner = Scanner(
            root      = target,
            min_bytes = int(min_mb * 1_048_576),
            randomize = self._rand_var.get(),
            out_queue = self._queue,
        )
        self._scanner.start()

        # start polling the queue and the periodic graph timer
        self._poll_job    = self.root.after(self._POLL_MS,  self._poll)
        self._graph_timer = self.root.after(self._GRAPH_MS, self._graph_tick)

    def _stop_scan(self):
        if self._scanner:
            self._scanner.stop()
        self._set_status("Stopping…")

    # ── Queue polling — runs entirely on the main thread ──────────────────────
    def _poll(self):
        """Drain all pending queue messages on the main thread."""
        self._poll_job = None
        try:
            while True:
                tag, data = self._queue.get_nowait()

                if tag == 'result':
                    r = data
                    with self._lock:
                        self.results.append(r)
                    self._n       += 1
                    self._sum_spd += r.speed_mbs
                    self._pending += 1
                    # live stat update (cheap — no iteration)
                    avg     = self._sum_spd / self._n
                    elapsed = time.time() - self._t_start
                    self._sv_measured.set(f"{self._n:,}")
                    self._sv_avg.set(f"{avg:,.1f}")
                    self._sv_last.set(f"{r.speed_mbs:,.1f}")
                    self._sv_elapsed.set(self._fmt(elapsed))
                    nm = Path(r.path).name
                    self._sv_curfile.set(nm[:36] + "…" if len(nm) > 36 else nm)
                    # graph refresh when enough new data accumulated
                    if self._pending >= self._GRAPH_MIN and self._graph_job is None:
                        self._graph_job = self.root.after(0, self._refresh_graph)

                elif tag == 'skip':
                    with self._lock:
                        self.skipped.append(data)
                    self._sv_skipped.set(f"{len(self.skipped):,}")

                elif tag == 'status':
                    self._set_status(data)

                elif tag == 'done':
                    self._scan_done()
                    return   # stop polling — done

        except queue.Empty:
            pass

        # reschedule unless scan is already finished
        self._poll_job = self.root.after(self._POLL_MS, self._poll)

    def _graph_tick(self):
        """Periodic graph redraw while scanning."""
        self._graph_timer = None
        if self._pending > 0 and self._graph_job is None:
            self._graph_job = self.root.after(0, self._refresh_graph)
        # keep ticking while scanner is alive
        if self._scanner is not None:
            self._graph_timer = self.root.after(self._GRAPH_MS, self._graph_tick)

    def _reset_stats(self):
        for sv in (self._sv_measured, self._sv_skipped,
                   self._sv_avg, self._sv_last, self._sv_elapsed):
            sv.set("0")
        self._sv_curfile.set("—")

    def _scan_done(self):
        # cancel any pending timers
        for attr in ('_poll_job', '_graph_job', '_graph_timer'):
            job = getattr(self, attr)
            if job:
                self.root.after_cancel(job)
                setattr(self, attr, None)

        self._scanner = None
        with self._lock:
            n = len(self.results)
            s = len(self.skipped)
        self._progress.stop()
        self._scan_btn.configure(state="normal")
        self._stop_btn.configure(state="disabled")
        elapsed = time.time() - self._t_start
        self._set_status(
            f"Done — {n:,} files measured,  {s:,} skipped  ·  {self._fmt(elapsed)}"
        )
        self._refresh_graph()

    @staticmethod
    def _fmt(sec: float) -> str:
        m, s = divmod(int(sec), 60)
        return f"{m}m {s:02d}s" if m else f"{s}s"

    def _set_status(self, msg: str):
        self._status_var.set(f"  {msg}")

    # ── Browse ─────────────────────────────────────────────────────────────────
    def _browse(self):
        p = filedialog.askdirectory(title="Select drive or folder to scan")
        if p:
            self._target_var.set(p.replace("/", "\\"))

    # ── Export ─────────────────────────────────────────────────────────────────
    def _export_tsv(self):
        with self._lock:
            data = list(self.results)
        if not data:
            messagebox.showinfo("Nothing to export", "Run a scan first.")
            return
        ts   = datetime.now().strftime("%Y-%m-%d %H.%M.%S")
        path = filedialog.asksaveasfilename(
            defaultextension=".tsv",
            filetypes=[("Tab-separated", "*.tsv"), ("All files", "*.*")],
            initialfile=f"{ts} Results.tsv",
        )
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter="\t")
            w.writerow(["Age (days)", "Rate (MB/s)", "Size (MB)", "Path"])
            for r in sorted(data, key=lambda x: x.speed_mbs):
                w.writerow([f"{r.age_days:.0f}", f"{r.speed_mbs:.1f}",
                             f"{r.size_mb:.1f}", r.path])
        self._set_status(f"Exported TSV → {path}")

    def _export_png(self):
        with self._lock:
            if not self.results:
                messagebox.showinfo("Nothing to export", "Run a scan first.")
                return
        ts   = datetime.now().strftime("%Y-%m-%d %H.%M.%S")
        path = filedialog.asksaveasfilename(
            defaultextension=".png",
            filetypes=[("PNG image", "*.png"), ("All files", "*.*")],
            initialfile=f"{ts} Chart.png",
        )
        if not path:
            return
        self._fig.savefig(path, dpi=150, bbox_inches="tight", facecolor=BG)
        self._set_status(f"Exported PNG → {path}")


# ─── Entry point ──────────────────────────────────────────────────────────────
def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
