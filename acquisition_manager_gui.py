#!/usr/bin/env python3
# Acquisition Manager - spectral acquisition suite for XIMEA cameras
# Copyright (C) 2023-2026  Monica Rainer and Alessio Turchi
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for details.
#
# You should have received a copy of the GNU General Public License along with
# this program.  If not, see <https://www.gnu.org/licenses/>.
"""
acquisition_manager_gui.py
---------------------
GUI front-end for acquisition_manager_lib.  Reproduces the original XIMEA manager
interface and adds a 'Loop control' panel with:
  - Number of loop iterations (N)
  - Wait time between iterations (seconds)
  - GO LOOP button  →  runs acquisition → [extraction] → [analysis] N times,
    calling exec_motor() and sleeping between iterations.
  - STOP button     →  interrupts the loop after the current phase, deletes
    the incomplete iteration directory, and disables the motor servo.
  - Enable checkboxes in Extraction and Analysis frames to make those phases
    optional.  Analysis can only be enabled when Extraction is enabled.
  - Camera preview panel (camera_view_lib): live pointing window shown while
    no acquisition is running.  The XIMEA device is exclusive, so the preview
    is always closed (and the handle released) before GO! or GO LOOP, and
    optionally reopened when the run terminates.

Written by Monica Rainer (2023).
Modified by Alessio Turchi (2026)
Loop extension: March 2026 - Alessio Turchi.
Camera preview integration: September 2026 - Alessio Turchi.
"""

import os
import sys
import gc
import time
import random
import json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import shutil

import lib.acquisition_manager_lib as alib

from lib.motor_client_lib import execute_move, motor_servo_enable, motor_servo_disable

# Camera preview is optional: the acquisition GUI must still start if the
# preview dependencies (opencv, Pillow, xiAPI) are not installed.
try:
    import lib.camera_view_lib as cvlib
    CAMERA_VIEW_OK  = True
    CAMERA_VIEW_ERR = ""
except Exception as _exc:          # ImportError, but also any module-level error
    cvlib = None
    CAMERA_VIEW_OK  = False
    CAMERA_VIEW_ERR = str(_exc)

# Default camera gain (dB) restored by the "Reset" button next to the gain field
DEFAULT_GAIN_DB = 0.0

# ==============================================================================
# MOTOR CONFIGURATION
#
# All motor parameters, the controller address included, live in motor.yml next
# to this file.  If the file (or PyYAML) is missing the motor is disabled in the
# GUI rather than silently driven with fallback values: moving a stage at a
# guessed address is worse than not moving it at all.
# ==============================================================================
MOTOR_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "motor.yml")

# Used only to fill keys a valid motor.yml happens to omit
_MOTOR_DEFAULTS = {
    "host":        "127.0.0.1",
    "port":        2002,
    "axis":        "1",
    "timeout":     10.0,
    "home_tol":    0.1,
    "move_tol":    0.1,
    "ont_retries": 100,
    "ont_delay":   0.5,
    "move_delay":  2.5,
}
_MOTOR_MAX_DEV_DEFAULT = 20.0     # maximum allowed deviation from zero


def load_motor_config(path: str = MOTOR_CONFIG_PATH):
    """
    Read the motor configuration from a YAML file.

    Returns
    -------
    (config, max_deviation, ok, message)
        config        : dict accepted by motor_client_lib
        max_deviation : bound of the random walk around the starting position
        ok            : False if the file could not be used at all
        message       : human-readable detail, empty when everything was found
    """
    fallback = (dict(_MOTOR_DEFAULTS), _MOTOR_MAX_DEV_DEFAULT)

    try:
        import yaml
    except ImportError as exc:
        return (*fallback, False, f"PyYAML is not installed ({exc})")

    if not os.path.isfile(path):
        return (*fallback, False, f"{os.path.basename(path)} not found")

    try:
        with open(path) as f:
            doc = yaml.safe_load(f) or {}
    except Exception as exc:
        return (*fallback, False, f"cannot parse {os.path.basename(path)}: {exc}")

    motor_section = doc.get("motor") or {}
    cfg = dict(_MOTOR_DEFAULTS)
    cfg.update(motor_section)
    # YAML turns an unquoted 1 into an int, but the GCS protocol wants the axis
    # identifier as a string; coerce it centrally instead of at each call site.
    cfg["axis"] = str(cfg["axis"])

    max_dev = float((doc.get("random_walk") or {}).get(
        "max_deviation", _MOTOR_MAX_DEV_DEFAULT))

    missing = [k for k in _MOTOR_DEFAULTS if k not in motor_section]
    msg = ("default used for: " + ", ".join(sorted(missing))) if missing else ""
    return cfg, max_dev, True, msg


_MOTOR_CONFIG, _MOTOR_MAX_DEV, MOTOR_CONFIG_OK, MOTOR_CONFIG_MSG = \
    load_motor_config()
if not MOTOR_CONFIG_OK:
    print(f"[motor] Configuration unusable ({MOTOR_CONFIG_MSG}); "
          "the motor switch is disabled")
elif MOTOR_CONFIG_MSG:
    print(f"[motor] {MOTOR_CONFIG_PATH}: {MOTOR_CONFIG_MSG}")

# ==============================================================================
# Motor command
# ==============================================================================
_motor_accum   = 0.0   # accumulated displacement from zero (signed)

def exec_motor(params: dict) -> bool:
    """
    Execute a motor move with bounded random-walk logic.

    On the first call, records the motor's returned position as absolute zero.
    On subsequent calls, chooses a random step that always reduces |_motor_accum|,
    ensuring the motor never deviates more than _MOTOR_MAX_DEV from zero.

    Returns True if the move succeeded, False otherwise.
    """
    global _motor_accum

    # --- Determine the move distance ---
    # Must move back toward zero: step is a random fraction of |accum|,
    # capped so the result stays within [-MAX_DEV, +MAX_DEV].
    if abs(_motor_accum) < 1e-6:
        # Already at zero: pick a small random excursion (will be corrected next call)
        dist = random.uniform(0.0, _MOTOR_MAX_DEV)
    else:
        # Move in the direction opposite to accum, random fraction in (0, 1]
        max_step = _MOTOR_MAX_DEV
        step = random.uniform(0.0, max_step)
        dist = -step if _motor_accum > 0 else step

    ok, info = execute_move(position=dist, config=_MOTOR_CONFIG)

    if not ok:
        print(f"[exec_motor] Move failed: {info}")
        return False

    pos_final = info.get("pos_final", dist)

    _motor_accum += (pos_final)
    # Use actual displacement reported by motor, not commanded dist
    print(f"[exec_motor] pos_final={pos_final:.4f}, "
            f"accum={_motor_accum:.4f}")

    return True

# ==============================================================================
# CLASS: Tooltip
# ==============================================================================
class Tooltip:
    def __init__(self, widget, bg='#FFFFEA', pad=(5, 3, 5, 3),
                 text='widget info', waittime=400, wraplength=250):
        self.waittime   = waittime
        self.wraplength = wraplength
        self.widget     = widget
        self.text       = text
        self.bg         = bg
        self.pad        = pad
        self.id         = None
        self.tw         = None
        widget.bind("<Enter>",       self.onEnter)
        widget.bind("<Leave>",       self.onLeave)
        widget.bind("<ButtonPress>", self.onLeave)

    def onEnter(self, event=None): self.schedule()
    def onLeave(self, event=None): self.unschedule(); self.hide()

    def schedule(self):
        self.unschedule()
        self.id = self.widget.after(self.waittime, self.show)

    def unschedule(self):
        id_ = self.id; self.id = None
        if id_: self.widget.after_cancel(id_)

    def show(self):
        def tip_pos(widget, label, delta=(10, 5), pad=(5, 3, 5, 3)):
            sw, sh = widget.winfo_screenwidth(), widget.winfo_screenheight()
            w = pad[0] + label.winfo_reqwidth()  + pad[2]
            h = pad[1] + label.winfo_reqheight() + pad[3]
            mx, my = widget.winfo_pointerxy()
            x1, y1 = mx + delta[0], my + delta[1]
            if x1 + w > sw: x1 = mx - delta[0] - w
            if y1 + h > sh: y1 = my - delta[1] - h
            return x1, max(0, y1)

        self.tw = tk.Toplevel(self.widget)
        self.tw.wm_overrideredirect(True)
        win = tk.Frame(self.tw, background=self.bg, borderwidth=0)
        label = tk.Label(win, text=self.text, justify=tk.LEFT,
                         background=self.bg, relief=tk.SOLID, borderwidth=0,
                         wraplength=self.wraplength)
        label.grid(padx=(self.pad[0], self.pad[2]),
                   pady=(self.pad[1], self.pad[3]), sticky=tk.NSEW)
        win.grid()
        x, y = tip_pos(self.widget, label)
        self.tw.wm_geometry("+%d+%d" % (x, y))

    def hide(self):
        if self.tw: self.tw.destroy()
        self.tw = None


# ==============================================================================
# CLASS: MainApp
# ==============================================================================
class MainApp(ttk.Frame):
    """
    GUI front-end for acquisition_manager_lib.
    Panels: Acquisition | Extraction | Analysis | Loop control.
    """

    def __init__(self, master):
        self.master = master
        ttk.Frame.__init__(self, self.master)
        self.master.title('Acquisition Manager - XIMEA')
        self.basedir = r"Z:/Loop_Measures/Try1"
        self._define_vars()
        self._set_defaults()
        self._build_gui()

    # --------------------------------------------------------------------------
    # Variables
    # --------------------------------------------------------------------------
    def _define_vars(self):
        """Declare all Tkinter control variables."""
        # Acquisition
        self.serial    = tk.StringVar()
        self.bin_bit   = tk.IntVar()
        self.texp_ms   = tk.DoubleVar()   # exposure time in MILLISECONDS
        self.gain      = tk.DoubleVar()   # camera gain in dB
        self.sequence  = tk.IntVar()
        self.subset    = tk.IntVar()
        self.position  = tk.IntVar()
        self.auto_position = tk.IntVar()  # 1 = detect centroid on frame 0
        self.halfwidth = tk.IntVar()
        self.outdir    = tk.StringVar()
        # Extraction
        self.indir     = tk.StringVar()
        self.suffix    = tk.StringVar()
        self.width     = tk.IntVar()
        self.ave_chunk = tk.IntVar()
        self.dark_sub  = tk.IntVar()
        self.trim_low  = tk.IntVar()
        self.trim_up   = tk.IntVar()
        # Analysis
        self.datacube  = tk.StringVar()
        self.flat      = tk.IntVar()
        self.chunk     = tk.IntVar()
        self.cuts_low  = tk.DoubleVar()
        self.cuts_high = tk.DoubleVar()
        self.poly      = tk.IntVar()
        self.start1    = tk.IntVar()
        self.end1      = tk.IntVar()
        self.start2    = tk.IntVar()
        self.end2      = tk.IntVar()
        self.savextract = tk.IntVar()
        self.saveave   = tk.IntVar()
        self.fourier   = tk.IntVar()
        # Loop control
        self.n_loops   = tk.IntVar()    # number of loop iterations
        self.wait_sec  = tk.DoubleVar() # wait time between iterations (s)
        self.motor_enable = tk.BooleanVar(value=MOTOR_CONFIG_OK)
        # Optional phases
        self.do_extract = tk.BooleanVar(value=True)
        self.do_analyze = tk.BooleanVar(value=True)
        # Camera preview
        self.preview_reopen = tk.BooleanVar(value=True)
        # Stop mechanism state (not tkinter vars)
        self._stop_flag   = threading.Event()
        self._loop_thread = None
        # Camera preview state (not tkinter vars)
        self._camera_view      = None   # CameraView instance while open
        self._preview_was_open = False  # preview state before the current run
        # UI widget references (populated in _build_gui)
        self._btn_go      = None
        self._btn_stop    = None
        self._cb_ana      = None
        self._btn_preview = None
        self._btn_single  = None
        self._btn_extract = None
        self._btn_analyze = None
        self._cb_motor    = None

    def _set_defaults(self):
        """Set default values for all fields (also used by Reset button)."""
        self.serial.set('28720523')
        self.bin_bit.set(16)
        self.texp_ms.set(200.0)          # 200 ms (was 0.2 s)
        self.gain.set(DEFAULT_GAIN_DB)
        self.sequence.set(200)
        self.subset.set(100)
        self.position.set(1750)
        self.auto_position.set(1)
        self.halfwidth.set(250)
        self.outdir.set(r'Z:/Loop_Measures/Try1')

        self.indir.set('')
        self.suffix.set("_datacube.fit")
        self.width.set(50)
        self.ave_chunk.set(0)
        self.dark_sub.set(0)
        self.trim_low.set(10)
        self.trim_up.set(10)

        self.datacube.set('')
        self.flat.set(0)
        self.cuts_low.set(0)
        self.cuts_high.set(0)
        self.poly.set(0)
        self.start1.set(0);  self.end1.set(0)
        self.start2.set(0);  self.end2.set(0)
        self.savextract.set(1)
        self.saveave.set(1)
        self.fourier.set(1)
        self.chunk.set(self.sequence.get() // 2)

        self.n_loops.set(1)
        self.wait_sec.set(5.0)
        # Never turn the motor on if its configuration could not be loaded
        self.motor_enable.set(MOTOR_CONFIG_OK)

        # Reset optional phases
        self.do_extract.set(True)
        self.do_analyze.set(True)
        self.preview_reopen.set(True)
        # Restore analysis checkbox state if widget already exists
        if self._cb_ana is not None:
            self._cb_ana.configure(state='normal')

    # --------------------------------------------------------------------------
    # GUI layout
    # --------------------------------------------------------------------------
    def _build_gui(self):
        s = ttk.Style()
        s.configure('Extract.TButton', background='yellow')
        s.configure('Loop.TButton',    background='cyan')
        s.configure('Stop.TButton',    background='red', foreground='white')

        # ---- Acquisition frame ----
        acq = ttk.LabelFrame(self.master, text='XIMEA acquisition',
                             padding="3 3 12 12")
        acq.grid(row=0, rowspan=4, columnspan=12, sticky=(tk.N, tk.W, tk.E))

        ttk.Label(acq, text="XIMEA Serial number: ").grid(row=1, column=1, sticky=tk.E)
        e = ttk.Entry(acq, textvariable=self.serial, width=12)
        e.grid(row=1, column=3, columnspan=7, sticky=(tk.W, tk.E))
        Tooltip(e, text='Serial number of the XIMEA camera')

        ttk.Label(acq, text="Bit: ").grid(row=1, column=10, sticky=tk.E)
        e = ttk.Entry(acq, textvariable=self.bin_bit, width=3)
        e.grid(row=1, column=11, columnspan=2, sticky=tk.W)
        Tooltip(e, text='Bit depth of the camera output (8 or 16)')

        ttk.Label(acq, text="Exp. time (ms): ").grid(row=2, column=1, columnspan=2, sticky=tk.E)
        e = ttk.Entry(acq, textvariable=self.texp_ms)
        e.grid(row=2, column=3, columnspan=2, sticky=(tk.W, tk.E))
        Tooltip(e, text='Exposure time of a single frame in milliseconds '
                        '(same unit as the camera preview slider)')

        ttk.Label(acq, text="Number of spectra: ").grid(row=2, column=5, columnspan=2, sticky=tk.E)
        e = ttk.Entry(acq, textvariable=self.sequence)
        e.grid(row=2, column=7, columnspan=2, sticky=(tk.W, tk.E))
        Tooltip(e, text='Total number of frames to acquire')

        ttk.Label(acq, text="Subset to save: ").grid(row=2, column=9, columnspan=2, sticky=tk.E)
        e = ttk.Entry(acq, textvariable=self.subset)
        e.grid(row=2, column=11, columnspan=2, sticky=(tk.W, tk.E))
        Tooltip(e, text='Number of raw frames averaged into each FITS file entry')

        ttk.Label(acq, text="Signal position: ").grid(row=3, column=1, columnspan=2, sticky=tk.E)
        e = ttk.Entry(acq, textvariable=self.position)
        e.grid(row=3, column=3, columnspan=2, sticky=(tk.W, tk.E))
        Tooltip(e, text='Fallback column position of the spectrum')

        ttk.Label(acq, text="Half-width window: ").grid(row=3, column=5, columnspan=2, sticky=tk.E)
        e = ttk.Entry(acq, textvariable=self.halfwidth)
        e.grid(row=3, column=7, columnspan=2, sticky=(tk.W, tk.E))
        Tooltip(e, text='Half-width of the image crop around the spectral centroid')

        ttk.Label(acq, text="Output directory: ").grid(row=3, column=9, columnspan=2, sticky=tk.E)
        e = ttk.Entry(acq, textvariable=self.outdir, width=40)
        e.grid(row=3, column=11, columnspan=2, sticky=(tk.W, tk.E))
        Tooltip(e, text='Root output directory')
        ttk.Button(acq, text="Browse",
                   command=self._browse_outdir).grid(row=4, column=11, sticky=(tk.W, tk.E))

        # ---- Row 4: gain and position-detection mode ----
        ttk.Label(acq, text="Gain (dB): ").grid(row=4, column=1, sticky=tk.E)
        e = ttk.Entry(acq, textvariable=self.gain, width=6)
        e.grid(row=4, column=3, sticky=(tk.W, tk.E))
        Tooltip(e, text='Camera gain in dB applied before acquisition')

        b = ttk.Button(acq, text="Reset gain", command=self._reset_gain)
        b.grid(row=4, column=4, columnspan=2, sticky=tk.W)
        Tooltip(b, text=f'Restore the default gain ({DEFAULT_GAIN_DB:g} dB) and, '
                        f'if the preview is open, apply it to the camera')

        ttk.Label(acq, text="Auto position: ").grid(row=4, column=6, columnspan=2, sticky=tk.E)
        cb = ttk.Checkbutton(acq, variable=self.auto_position)
        cb.grid(row=4, column=8, sticky=tk.W)
        Tooltip(cb, text='ON: the spectral column is detected on frame 0 of every '
                         'acquisition and "Signal position" is only a fallback.  '
                         'OFF: the "Signal position" value is used as-is.')

        # Single acquisition (no extraction, no analysis, no motor)
        self._btn_single = ttk.Button(acq, text="GO!", style='Extract.TButton',
                                      command=self._run_single)
        self._btn_single.grid(row=4, column=9, columnspan=2, sticky=(tk.W, tk.E))
        Tooltip(self._btn_single,
                text='Run a single acquisition with the parameters above '
                     '(no extraction, no analysis, no motor move)')

        for child in acq.winfo_children():
            child.grid_configure(padx=5, pady=5)

        # ---- Extraction frame ----
        ext = ttk.LabelFrame(self.master,
                             text='Extract spectra and store them in FITS datacube',
                             padding="3 3 12 12")
        ext.grid(row=4, rowspan=5, columnspan=6, sticky=(tk.N, tk.W, tk.E))

        # Enable/disable extraction toggle (row=1)
        ttk.Label(ext, text="Enable extraction:").grid(row=1, column=1, sticky=tk.E)
        cb = ttk.Checkbutton(ext, variable=self.do_extract,
                             command=self._on_extract_toggle)
        cb.grid(row=1, column=2, sticky=tk.W)
        Tooltip(cb, text='Run spectral extraction in the loop')

        # Existing extraction parameters (row=2+)
        ttk.Label(ext, text="Ext. window: ").grid(row=2, column=1, sticky=tk.E)
        e = ttk.Entry(ext, textvariable=self.width, width=3)
        e.grid(row=2, column=2, sticky=tk.W)
        Tooltip(e, text='Full extraction window width in pixels')

        ttk.Label(ext, text="Average spectra: ").grid(row=2, column=3, sticky=tk.E)
        e = ttk.Entry(ext, textvariable=self.ave_chunk, width=5)
        e.grid(row=2, column=4, sticky=tk.W)
        Tooltip(e, text='Bin N consecutive spectra by averaging (0 = disabled)')

        ttk.Label(ext, text="Dark subtraction:").grid(row=2, column=5, sticky=tk.E)
        e = ttk.Checkbutton(ext, variable=self.dark_sub)
        e.grid(row=2, column=6, sticky=tk.W)
        Tooltip(e, text='Subtract local dark from flanking columns')

        ttk.Label(ext, text="Trim spectra (low): ").grid(row=3, column=1, sticky=tk.E)
        e = ttk.Entry(ext, textvariable=self.trim_low, width=3)
        e.grid(row=3, column=2, sticky=tk.W)
        Tooltip(e, text='Pixels to trim from the bottom of each frame')

        ttk.Label(ext, text="Trim spectra (up): ").grid(row=3, column=3, sticky=tk.E)
        e = ttk.Entry(ext, textvariable=self.trim_up, width=3)
        e.grid(row=3, column=4, sticky=tk.W)
        Tooltip(e, text='Pixels to trim from the top of each frame')

        ttk.Label(ext, text="Output suffix: ").grid(row=4, column=1, columnspan=2, sticky=tk.E)
        e = ttk.Entry(ext, textvariable=self.suffix)
        e.grid(row=4, column=2, columnspan=3, sticky=(tk.W, tk.E))
        Tooltip(e, text='Output filename = input directory name + this suffix')

        # Single extraction on the directory currently held in indir
        self._btn_extract = ttk.Button(ext, text="Extract",
                                       command=self._run_extract)
        self._btn_extract.grid(row=4, column=5, columnspan=2, sticky=(tk.W, tk.E))
        Tooltip(self._btn_extract,
                text='Run extraction on the last acquired directory')

        for child in ext.winfo_children():
            child.grid_configure(padx=5, pady=5)

        # ---- Analysis frame ----
        ana = ttk.LabelFrame(self.master, text='Analyze spectra', padding="3 3 12 12")
        ana.grid(row=4, rowspan=8, column=7, columnspan=6, sticky=(tk.N, tk.W, tk.E))

        # Enable/disable analysis toggle (row=1); ref saved to allow state control
        ttk.Label(ana, text="Enable analysis:").grid(row=1, column=1, sticky=tk.E)
        self._cb_ana = ttk.Checkbutton(ana, variable=self.do_analyze)
        self._cb_ana.grid(row=1, column=2, sticky=tk.W)
        Tooltip(self._cb_ana,
                text='Run spectral analysis (requires extraction enabled)')

        # Existing analysis parameters (row=2+)
        ttk.Label(ana, text="Flat-field: ").grid(row=2, column=1, sticky=tk.E)
        e = ttk.Entry(ana, textvariable=self.flat)
        e.grid(row=2, column=2, sticky=tk.W)
        Tooltip(e, text='Number of initial spectra used as flat-field reference')

        ttk.Label(ana, text="Poly degree: ").grid(row=2, column=3, sticky=tk.E)
        e = ttk.Entry(ana, textvariable=self.poly, width=3)
        e.grid(row=2, column=4, sticky=tk.W)
        Tooltip(e, text='Polynomial degree for continuum normalization (0 = constant)')

        ttk.Label(ana, text="Num. images:").grid(row=3, column=1, sticky=tk.E)
        e = ttk.Entry(ana, textvariable=self.chunk, width=5)
        e.grid(row=3, column=2, sticky=tk.W)
        Tooltip(e, text='Number of spectra for block 2')

        ttk.Label(ana, text="OR start:").grid(row=3, column=3, sticky=tk.E)
        e = ttk.Entry(ana, textvariable=self.start1, width=5)
        e.grid(row=3, column=4, sticky=tk.W)
        Tooltip(e, text='Explicit start index of block 2')

        ttk.Label(ana, text="end:").grid(row=3, column=5, sticky=tk.E)
        e = ttk.Entry(ana, textvariable=self.end1, width=5)
        e.grid(row=3, column=6, sticky=tk.W)
        Tooltip(e, text='Explicit end index of block 2')

        ttk.Label(ana, text="AND start:").grid(row=4, column=3, sticky=tk.E)
        e = ttk.Entry(ana, textvariable=self.start2, width=5)
        e.grid(row=4, column=4, sticky=tk.W)
        Tooltip(e, text='Explicit start index of block 3')

        ttk.Label(ana, text="end:").grid(row=4, column=5, sticky=tk.E)
        e = ttk.Entry(ana, textvariable=self.end2, width=5)
        e.grid(row=4, column=6, sticky=tk.W)
        Tooltip(e, text='Explicit end index of block 3')

        ttk.Label(ana, text="Cuts low: ").grid(row=5, column=1, sticky=tk.E)
        e = ttk.Entry(ana, textvariable=self.cuts_low, width=5)
        e.grid(row=5, column=2, sticky=tk.W)
        Tooltip(e, text='Lower display cut for waterfall plot (0 = auto)')

        ttk.Label(ana, text="Cuts high: ").grid(row=5, column=3, sticky=tk.E)
        e = ttk.Entry(ana, textvariable=self.cuts_high, width=5)
        e.grid(row=5, column=4, sticky=tk.W)
        Tooltip(e, text='Upper display cut for waterfall plot (0 = auto)')

        ttk.Label(ana, text="Save all:").grid(row=6, column=1, sticky=tk.E)
        e = ttk.Checkbutton(ana, variable=self.savextract)
        e.grid(row=6, column=2, sticky=tk.W)
        Tooltip(e, text='Save all corrected spectra as ASCII')

        ttk.Label(ana, text="Save averages:").grid(row=6, column=3, sticky=tk.E)
        e = ttk.Checkbutton(ana, variable=self.saveave)
        e.grid(row=6, column=4, sticky=tk.W)
        Tooltip(e, text='Save the three averaged block spectra as ASCII')

        ttk.Label(ana, text="FFT:").grid(row=6, column=5, sticky=tk.E)
        e = ttk.Checkbutton(ana, variable=self.fourier)
        e.grid(row=6, column=6, sticky=tk.W)
        Tooltip(e, text='Compute and save FFT amplitude spectrum of each block')

        # Single analysis on the datacube currently held in datacube
        self._btn_analyze = ttk.Button(ana, text="Analyze",
                                       command=self._run_analyze)
        self._btn_analyze.grid(row=7, column=1, columnspan=2, sticky=(tk.W, tk.E))
        Tooltip(self._btn_analyze,
                text='Run analysis on the last extracted datacube')

        for child in ana.winfo_children():
            child.grid_configure(padx=5, pady=5)

        # ---- Camera preview frame ----
        # Rows 9-11 of columns 0-5 are free (extraction ends at row 8).
        pv = ttk.LabelFrame(self.master, text='Camera preview',
                            padding="3 3 12 12")
        pv.grid(row=9, rowspan=3, column=0, columnspan=6,
                sticky=(tk.N, tk.W, tk.E))

        self._btn_preview = ttk.Button(pv, text="Open preview",
                                       command=self._toggle_preview)
        self._btn_preview.grid(row=1, column=1, padx=5, sticky=tk.W)
        Tooltip(self._btn_preview,
                text='Live pointing window.  The XIMEA device is exclusive: the '
                     'preview is closed automatically before any acquisition.')

        cb = ttk.Checkbutton(pv, text="Reopen after run",
                             variable=self.preview_reopen)
        cb.grid(row=1, column=2, padx=5, sticky=tk.W)
        Tooltip(cb, text='Reopen the preview when GO! or GO LOOP terminates, '
                         'if it was open before the run started')

        self.preview_status = tk.StringVar(value="Closed")
        ttk.Label(pv, text="State:").grid(row=2, column=1, sticky=tk.E)
        ttk.Label(pv, textvariable=self.preview_status,
                  foreground='dark green',
                  anchor=tk.W).grid(row=2, column=2, columnspan=3,
                                    sticky=(tk.W, tk.E))

        if not CAMERA_VIEW_OK:
            self._btn_preview.configure(state='disabled')
            self.preview_status.set("Unavailable")
            Tooltip(self._btn_preview,
                    text=f'Camera preview unavailable: {CAMERA_VIEW_ERR}')

        for child in pv.winfo_children():
            child.grid_configure(padx=5, pady=5)

        # ---- Loop control frame ----
        lp = ttk.LabelFrame(self.master, text='Loop control', padding="3 3 12 12")
        lp.grid(row=12, columnspan=13, sticky=(tk.N, tk.W, tk.E))

        ttk.Label(lp, text="Number of iterations (N):").grid(row=1, column=1, sticky=tk.E)
        e = ttk.Entry(lp, textvariable=self.n_loops, width=6)
        e.grid(row=1, column=2, sticky=tk.W)
        Tooltip(e, text='Total number of acquire → extract → analyze cycles to run')

        ttk.Label(lp, text="Wait between cycles (s):").grid(row=1, column=3, sticky=tk.E)
        e = ttk.Entry(lp, textvariable=self.wait_sec, width=8)
        e.grid(row=1, column=4, sticky=tk.W)
        Tooltip(e, text='Seconds to wait after exec_motor() before starting the next cycle')

        # Motor switch: when off, no servo enable/disable and no move is issued
        ttk.Label(lp, text="Motor switch:").grid(row=1, column=5, sticky=tk.E)
        self._cb_motor = ttk.Checkbutton(lp, variable=self.motor_enable)
        self._cb_motor.grid(row=1, column=6, sticky=tk.W)
        if MOTOR_CONFIG_OK:
            Tooltip(self._cb_motor,
                    text='ON: the fibre agitation stage is moved between cycles '
                         f'({_MOTOR_CONFIG["host"]}:{_MOTOR_CONFIG["port"]}, '
                         f'axis {_MOTOR_CONFIG["axis"]}).  OFF: no servo command '
                         'and no move is sent; the wait still applies.')
        else:
            self._cb_motor.configure(state='disabled')
            Tooltip(self._cb_motor,
                    text=f'Motor disabled: {MOTOR_CONFIG_MSG}. '
                         f'Check {os.path.basename(MOTOR_CONFIG_PATH)}.')

        # GO LOOP — reference saved to disable/enable during run
        self._btn_go = ttk.Button(lp, text="GO LOOP", style='Loop.TButton',
                                  command=self._run_loop)
        self._btn_go.grid(row=1, column=8, padx=10, sticky=tk.W)

        # STOP button — enabled only while loop is running
        self._btn_stop = ttk.Button(lp, text="STOP", style='Stop.TButton',
                                    command=self._stop_loop, state='disabled')
        self._btn_stop.grid(row=1, column=9, padx=10, sticky=tk.W)

        # Status label shows current loop progress
        self.loop_status = tk.StringVar(value="Idle")
        ttk.Label(lp, text="Progress:").grid(row=2, column=1, sticky=tk.E)
        ttk.Label(lp, textvariable=self.loop_status,
                  foreground='navy', font=('TkDefaultFont', 10, 'bold'),
                  anchor=tk.W).grid(row=2, column=2, columnspan=7, sticky=(tk.W, tk.E))

        for child in lp.winfo_children():
            child.grid_configure(padx=5, pady=5)

        # ---- Bottom frame ----
        bot = ttk.Frame(self.master, padding="3 3 12 12")
        bot.grid(row=13, column=1, columnspan=6, sticky=(tk.N, tk.W, tk.E))
        for col in range(6):
            bot.columnconfigure(col, weight=1)
        ttk.Button(bot, text="Reset all fields",
                   command=self._set_defaults).grid(column=2, row=1, sticky=tk.W)
        ttk.Button(bot, text="Import config",
                   command=self._load_config).grid(column=3, row=1, sticky=tk.W)
        ttk.Button(bot, text="Quit",
                   command=self._quit).grid(column=4, row=1, sticky=tk.W)
        for child in bot.winfo_children():
            child.grid_configure(padx=5, pady=5)

    # --------------------------------------------------------------------------
    # Config dict builders
    # --------------------------------------------------------------------------
    def _acq_cfg(self):
        # NOTE: "texp_ms" is the authoritative value.  "texp" (seconds) is kept
        # so that a version of acquisition_manager_lib not yet aware of milliseconds
        # keeps working with the correct exposure instead of silently falling
        # back to DEFAULT_CFG["texp"].
        texp_ms = self.texp_ms.get()
        return {
            "serial":        self.serial.get(),
            "bin_bit":       self.bin_bit.get(),
            "texp_ms":       texp_ms,
            "texp":          texp_ms / 1000.0,
            "gain":          self.gain.get(),
            "sequence":      self.sequence.get(),
            "subset":        self.subset.get(),
            "position":      self.position.get(),
            "auto_position": self.auto_position.get(),
            "halfwidth":     self.halfwidth.get(),
            "outdir":        self.outdir.get(),
        }

    def _ext_cfg(self):
        return {
            "indir":     self.indir.get(),
            "suffix":    self.suffix.get(),
            "width":     self.width.get(),
            "ave_chunk": self.ave_chunk.get(),
            "dark_sub":  self.dark_sub.get(),
            "trim_low":  self.trim_low.get(),
            "trim_up":   self.trim_up.get(),
        }

    def _ana_cfg(self):
        return {
            "datacube":   self.datacube.get(),
            "flat":       self.flat.get(),
            "poly":       self.poly.get(),
            "chunk":      self.chunk.get(),
            "cuts_low":   self.cuts_low.get(),
            "cuts_high":  self.cuts_high.get(),
            "start1":     self.start1.get(),
            "end1":       self.end1.get(),
            "start2":     self.start2.get(),
            "end2":       self.end2.get(),
            "savextract": self.savextract.get(),
            "saveave":    self.saveave.get(),
            "fourier":    self.fourier.get(),
        }

    def _all_cfg(self):
        """Return a single dict with all configuration parameters."""
        return {
            "acquisition": self._acq_cfg(),
            "extraction":  self._ext_cfg(),
            "analysis":    self._ana_cfg(),
            "loop": {
                "n_loops":     self.n_loops.get(),
                "wait_sec":    self.wait_sec.get(),
                "do_extract":  self.do_extract.get(),
                "do_analyze":  self.do_analyze.get(),
                "motor_enable": self.motor_enable.get(),
            },
            "preview": {
                "reopen_after_run": self.preview_reopen.get(),
            },
        }

    def _save_config(self, outdir: str):
        """Save all GUI parameters as JSON inside outdir."""
        cfg_path = os.path.join(outdir, "acquisition_config.json")
        try:
            with open(cfg_path, "w") as f:
                json.dump(self._all_cfg(), f, indent=4)
        except OSError as exc:
            print(f"[WARNING] Could not save config: {exc}")

    def _load_config(self):
        """Load GUI parameters from a previously saved JSON config file."""
        path = filedialog.askopenfilename(
            title="Import configuration",
            filetypes=[("JSON config", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path) as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[ERROR] Could not load config: {exc}")
            return

        acq = cfg.get("acquisition", {})
        self.serial.set(acq.get("serial",       self.serial.get()))
        self.bin_bit.set(acq.get("bin_bit",     self.bin_bit.get()))
        # Backward compatibility: configs written before the ms switch only
        # carry "texp" in seconds.
        if "texp_ms" in acq:
            self.texp_ms.set(acq["texp_ms"])
        elif "texp" in acq:
            self.texp_ms.set(float(acq["texp"]) * 1000.0)
        self.gain.set(acq.get("gain",           self.gain.get()))
        self.sequence.set(acq.get("sequence",   self.sequence.get()))
        self.subset.set(acq.get("subset",       self.subset.get()))
        self.position.set(acq.get("position",   self.position.get()))
        self.auto_position.set(acq.get("auto_position", self.auto_position.get()))
        self.halfwidth.set(acq.get("halfwidth", self.halfwidth.get()))
        self.outdir.set(acq.get("outdir",       self.outdir.get()))

        ext = cfg.get("extraction", {})
        self.indir.set(ext.get("indir",         self.indir.get()))
        self.suffix.set(ext.get("suffix",       self.suffix.get()))
        self.width.set(ext.get("width",         self.width.get()))
        self.ave_chunk.set(ext.get("ave_chunk", self.ave_chunk.get()))
        self.dark_sub.set(ext.get("dark_sub",   self.dark_sub.get()))
        self.trim_low.set(ext.get("trim_low",   self.trim_low.get()))
        self.trim_up.set(ext.get("trim_up",     self.trim_up.get()))

        ana = cfg.get("analysis", {})
        self.datacube.set(ana.get("datacube",   self.datacube.get()))
        self.flat.set(ana.get("flat",           self.flat.get()))
        self.poly.set(ana.get("poly",           self.poly.get()))
        self.chunk.set(ana.get("chunk",         self.chunk.get()))
        self.cuts_low.set(ana.get("cuts_low",   self.cuts_low.get()))
        self.cuts_high.set(ana.get("cuts_high", self.cuts_high.get()))
        self.start1.set(ana.get("start1",       self.start1.get()))
        self.end1.set(ana.get("end1",           self.end1.get()))
        self.start2.set(ana.get("start2",       self.start2.get()))
        self.end2.set(ana.get("end2",           self.end2.get()))
        self.savextract.set(ana.get("savextract", self.savextract.get()))
        self.saveave.set(ana.get("saveave",     self.saveave.get()))
        self.fourier.set(ana.get("fourier",     self.fourier.get()))

        lp = cfg.get("loop", {})
        self.n_loops.set(lp.get("n_loops",     self.n_loops.get()))
        self.wait_sec.set(lp.get("wait_sec",   self.wait_sec.get()))
        self.do_extract.set(lp.get("do_extract", self.do_extract.get()))
        self.do_analyze.set(lp.get("do_analyze", self.do_analyze.get()))
        self.motor_enable.set(bool(lp.get("motor_enable",
                                          self.motor_enable.get()))
                              and MOTOR_CONFIG_OK)

        pv = cfg.get("preview", {})
        self.preview_reopen.set(pv.get("reopen_after_run",
                                       self.preview_reopen.get()))
        # Restore analysis checkbox state to match loaded value
        self._on_extract_toggle()

    def _ensure_outdir(self) -> str:
        """Create output directory if it does not exist. Returns the path."""
        outdir = self.outdir.get()
        os.makedirs(outdir, exist_ok=True)
        return outdir

    # --------------------------------------------------------------------------
    # Camera preview management
    #
    # The XIMEA device is exclusive: exactly one handle at a time.  The rule is
    # that CameraView owns the device only while its window is open, and no
    # acquisition may start until the preview has been closed and the handle
    # released.  All the methods below run in the GUI thread only.
    # --------------------------------------------------------------------------
    def _preview_is_open(self) -> bool:
        return self._camera_view is not None and self._camera_view.is_open()

    def _toggle_preview(self):
        """Open/close button of the preview panel."""
        if self._preview_is_open():
            self._close_preview()
        else:
            self._open_preview()

    def _open_preview(self):
        """Create the CameraView window and hand it the current parameters."""
        if not CAMERA_VIEW_OK:
            messagebox.showerror("Camera preview",
                                 f"Preview unavailable: {CAMERA_VIEW_ERR}")
            return
        if self._preview_is_open():
            return

        outdir = self._ensure_outdir()
        self._camera_view = cvlib.CameraView(
            self.master,
            serial=self.serial.get(),
            outdir=outdir,
            crop_provider=self._crop_window,
            apply_callback=self._apply_preview_params,
            on_close=self._on_preview_closed,
            initial_exposure_ms=self.texp_ms.get(),
            initial_gain=self.gain.get(),
        )
        if not self._camera_view.open():
            # open() has already reported the reason to the user
            self._camera_view = None
        self._update_preview_state()

    def _close_preview(self):
        """Close the preview window and release the camera handle."""
        if self._camera_view is not None:
            self._camera_view.close()     # triggers _on_preview_closed()
        self._camera_view = None
        self._update_preview_state()

    def _on_preview_closed(self):
        """Callback fired by CameraView once its window has been destroyed."""
        self._camera_view = None
        self._update_preview_state()

    def _update_preview_state(self):
        """Refresh the preview button label and status text."""
        if self._btn_preview is None:
            return
        if self._preview_is_open():
            self._btn_preview.configure(text="Close preview")
            self.preview_status.set("Open - camera busy")
        else:
            self._btn_preview.configure(text="Open preview")
            self.preview_status.set("Closed")

    def _release_camera(self) -> bool:
        """
        Close the preview before an acquisition and report whether it was open.

        Must be called from the GUI thread, before any run_acquisition().
        """
        was_open = self._preview_is_open()
        if was_open:
            self.preview_status.set("Releasing camera...")
            self.master.update_idletasks()
            self._close_preview()
        self._preview_was_open = was_open
        return was_open

    def _maybe_reopen_preview(self):
        """Reopen the preview after a run, if it was open before and enabled."""
        if self._preview_was_open and self.preview_reopen.get():
            self._open_preview()
        self._preview_was_open = False

    def _crop_window(self):
        """
        Provide the acquisition crop window to the preview overlay.

        Called from the GUI thread on every displayed frame, so reading the
        tkinter variables here is safe.  Returns None if a field is empty or
        malformed (the user may be typing into it).
        """
        try:
            return self.position.get(), self.halfwidth.get()
        except (tk.TclError, ValueError):
            return None

    def _apply_preview_params(self, params: dict):
        """
        Receive pointing/camera settings from the preview window.

        Transferring an explicit position only makes sense if frame-0 auto
        detection does not override it, so auto_position is switched off and
        the change is reported in the progress label.
        """
        self.position.set(int(params["position"]))
        self.texp_ms.set(round(float(params["texp_ms"]), 3))
        self.gain.set(round(float(params["gain"]), 2))
        self.auto_position.set(0)
        self.loop_status.set(
            f"From preview: position={params['position']} "
            f"({params['pos_source']}), texp={params['texp_ms']:.1f} ms, "
            f"gain={params['gain']:.1f} dB - auto position disabled")

    def _reset_gain(self):
        """Restore the default gain and push it to the preview if it is open."""
        self.gain.set(DEFAULT_GAIN_DB)
        if self._preview_is_open():
            self._camera_view.set_gain(DEFAULT_GAIN_DB)

    def _quit(self):
        """Release the camera before leaving, otherwise the handle leaks."""
        self._close_preview()
        self.master.quit()

    # --------------------------------------------------------------------------
    # Single-step buttons (GO! / Extract / Analyze)
    #
    # Each phase runs in a background thread so the GUI stays responsive: with
    # sequence=200 at 200 ms a single acquisition already blocks for ~40 s.
    # Only one worker at a time is allowed, loop included, and every tkinter
    # access from a worker goes through master.after().
    # --------------------------------------------------------------------------
    def _worker_busy(self) -> bool:
        """True if a single-step or loop worker is already running."""
        return self._loop_thread is not None and self._loop_thread.is_alive()

    def _set_buttons_running(self, running: bool, lock_preview: bool = True):
        """Enable/disable the run buttons around a worker."""
        state = 'disabled' if running else 'normal'
        for btn in (self._btn_go, self._btn_single,
                    self._btn_extract, self._btn_analyze):
            if btn is not None:
                btn.configure(state=state)
        if lock_preview and self._btn_preview is not None:
            # Never re-enable the preview if its dependencies are missing
            self._btn_preview.configure(
                state=state if CAMERA_VIEW_OK else 'disabled')

    def _run_single(self):
        """GO! button: run a single acquisition in a background thread."""
        if self._worker_busy():
            return
        # The preview owns the camera: release it before opening the device.
        self._release_camera()
        outdir = self._ensure_outdir()
        self._save_config(outdir)
        alib.setup_file_logging(outdir)
        acq_cfg = self._acq_cfg()        # snapshot in the GUI thread
        self._set_buttons_running(True)
        self._loop_thread = threading.Thread(
            target=self._single_worker, args=(acq_cfg,), daemon=True)
        self._loop_thread.start()

    def _single_worker(self, acq_cfg):
        """Background thread: one acquisition, then reopen the preview."""
        try:
            self._set_status("Single acquisition - acquiring")
            out_dir = alib.run_acquisition(acq_cfg)
            if out_dir:
                self.master.after(0, lambda d=out_dir: self.indir.set(d))
                self.master.after(
                    0, lambda: self.chunk.set(self.sequence.get() // 2))
                self._set_status("Single acquisition - done")
            else:
                self._set_status("Single acquisition - FAILED")
        except Exception as exc:
            self._set_status(f"Single acquisition - ERROR: {exc}")
        finally:
            self.master.after(0, lambda: self._set_buttons_running(False))
            self.master.after(0, self._maybe_reopen_preview)

    def _run_extract(self):
        """Extract button: run extraction in a background thread."""
        if self._worker_busy():
            return
        ext_cfg = self._ext_cfg()        # snapshot in the GUI thread
        # Extraction does not touch the camera, so the preview stays available
        self._set_buttons_running(True, lock_preview=False)
        self._loop_thread = threading.Thread(
            target=self._extract_worker, args=(ext_cfg,), daemon=True)
        self._loop_thread.start()

    def _extract_worker(self, ext_cfg):
        """Background thread: one extraction."""
        try:
            self._set_status("Extraction - running")
            datacube_path = alib.run_extraction(ext_cfg)
            if datacube_path:
                self.master.after(
                    0, lambda p=datacube_path: self.datacube.set(p))
                self._set_status("Extraction - done")
            else:
                self._set_status("Extraction - FAILED")
        except Exception as exc:
            self._set_status(f"Extraction - ERROR: {exc}")
        finally:
            self.master.after(
                0, lambda: self._set_buttons_running(False, lock_preview=False))

    def _run_analyze(self):
        """Analyze button: run analysis in a background thread."""
        if self._worker_busy():
            return
        ana_cfg = self._ana_cfg()        # snapshot in the GUI thread
        self._set_buttons_running(True, lock_preview=False)
        self._loop_thread = threading.Thread(
            target=self._analyze_worker, args=(ana_cfg,), daemon=True)
        self._loop_thread.start()

    def _analyze_worker(self, ana_cfg):
        """Background thread: one analysis."""
        try:
            self._set_status("Analysis - running")
            alib.run_analysis(ana_cfg)
            self._set_status("Analysis - done")
        except Exception as exc:
            self._set_status(f"Analysis - ERROR: {exc}")
        finally:
            self.master.after(
                0, lambda: self._set_buttons_running(False, lock_preview=False))

    # --------------------------------------------------------------------------
    # Browse helpers
    # --------------------------------------------------------------------------
    def _browse_outdir(self):
        start = self.outdir.get() if os.path.isdir(self.outdir.get()) else (
            self.basedir if os.path.isdir(self.basedir) else os.getcwd())
        d = filedialog.askdirectory(initialdir=start, title="Select output directory")
        if d:
            self.outdir.set(d)
            # Keep the preview capture directory (<outdir>/live_frames) in sync
            if self._preview_is_open():
                self._camera_view.outdir = d

    # --------------------------------------------------------------------------
    # Optional-phase toggle
    # --------------------------------------------------------------------------
    def _on_extract_toggle(self):
        """Disable analysis checkbox when extraction is turned off."""
        if not self.do_extract.get():
            self.do_analyze.set(False)
            self._cb_ana.configure(state='disabled')
        else:
            self._cb_ana.configure(state='normal')

    # --------------------------------------------------------------------------
    # Stop handling
    # --------------------------------------------------------------------------
    def _stop_loop(self):
        """Signal the running loop to stop after the current phase completes."""
        self._stop_flag.set()
        self._set_status("Stop requested — finishing current phase...")

    def _set_status(self, msg: str):
        """Thread-safe update of the progress label."""
        self.master.after(0, lambda m=msg: self.loop_status.set(m))

    def _cleanup_iteration(self, out_dir: str):
        """
        Delete the timestamped subdirectory of the interrupted iteration.
        This removes all FITS, PNG and TXT files produced in that cycle,
        leaving the root outdir (pipeline.log, ximea_config.json) intact.
        """
        if out_dir and os.path.isdir(out_dir):
            try:
                shutil.rmtree(out_dir)
                print(f"[stop] Deleted incomplete iteration directory: {out_dir}")
            except OSError as exc:
                print(f"[stop] Could not delete {out_dir}: {exc}")

    # --------------------------------------------------------------------------
    # Loop execution (threaded)
    # --------------------------------------------------------------------------
    def _run_loop(self):
        """
        Snapshot GUI state in the main thread, then launch the loop in a
        background thread so the GUI remains responsive (STOP button works
        during acquisition, extraction and analysis).
        """
        if self._worker_busy():
            return

        # The preview owns the camera: release it before the loop starts.
        # This must happen in the GUI thread, before the worker is launched.
        self._release_camera()

        # All tkinter .get() calls must happen in the main thread
        n_loops    = self.n_loops.get()
        wait_sec   = self.wait_sec.get()
        do_extract = self.do_extract.get()
        do_analyze = self.do_analyze.get()
        # A motor.yml that failed to load forces the motor off, whatever the
        # checkbox says (it may have been restored by an imported config)
        use_motor  = bool(self.motor_enable.get()) and MOTOR_CONFIG_OK
        acq_cfg    = self._acq_cfg()
        ext_cfg    = self._ext_cfg()
        ana_cfg    = self._ana_cfg()
        ana_cfg["chunk"] = acq_cfg["sequence"] // 2  # keep chunk in sync with sequence

        outdir = self._ensure_outdir()
        self._save_config(outdir)
        alib.setup_file_logging(outdir)

        self._stop_flag.clear()
        self._set_buttons_running(True)
        self._btn_stop.configure(state='normal')

        self._loop_thread = threading.Thread(
            target=self._loop_worker,
            args=(n_loops, wait_sec, do_extract, do_analyze,
                  acq_cfg, ext_cfg, ana_cfg, use_motor),
            daemon=True,
        )
        self._loop_thread.start()

    def _loop_worker(self, n_loops, wait_sec, do_extract, do_analyze,
                     acq_cfg, ext_cfg, ana_cfg, use_motor=True):
        """
        Background thread: acquire → [extract] → [analyze] → motor → wait.

        Stop behaviour:
          - _stop_flag is checked between every phase.
          - If set, the timestamped subdirectory of the current (incomplete)
            iteration is deleted via _cleanup_iteration(), the motor servo is
            disabled, and the loop exits.
          - Iterations that completed successfully are never touched.

        Motor switch:
          - use_motor False skips the servo enable/disable and every move; the
            wait between cycles still applies, so a run without fibre agitation
            keeps the same timing as one with it.
        """
        if use_motor:
            motor_servo_enable(_MOTOR_CONFIG)
        else:
            print("[motor] Motor switch is OFF: no servo command, no move")
        stopped = False
        # Tracks the timestamped subdir of the iteration in progress;
        # reset to None once the iteration completes cleanly.
        out_dir = None

        try:
            for i in range(n_loops):
                if self._stop_flag.is_set():
                    stopped = True
                    break

                # --- Acquisition ---
                self._set_status(f"Cycle {i+1}/{n_loops} - acquiring")
                out_dir = alib.run_acquisition(acq_cfg)
                # Update indir field in main thread (informational only)
                self.master.after(0, lambda d=out_dir: self.indir.set(d))

                if self._stop_flag.is_set():
                    # Acquisition directory may be partially written: delete it
                    self._cleanup_iteration(out_dir)
                    stopped = True
                    break

                # --- Extraction (optional) ---
                datacube_path = None
                if do_extract:
                    self._set_status(f"Cycle {i+1}/{n_loops} - extracting")
                    # Inject the actual acquisition output dir (avoids relying on
                    # indir tk var, which is updated asynchronously via after())
                    cur_ext = dict(ext_cfg, indir=out_dir)
                    datacube_path = alib.run_extraction(cur_ext)
                    if datacube_path:
                        self.master.after(
                            0, lambda p=datacube_path: self.datacube.set(p))
                    else:
                        self._set_status(
                            f"Cycle {i+1}/{n_loops} - extraction FAILED")

                if self._stop_flag.is_set():
                    self._cleanup_iteration(out_dir)
                    stopped = True
                    break

                # --- Analysis (optional, requires a valid datacube) ---
                if do_analyze and datacube_path:
                    self._set_status(f"Cycle {i+1}/{n_loops} - analysing")
                    cur_ana = dict(ana_cfg, datacube=datacube_path)
                    alib.run_analysis(cur_ana)

                # Iteration completed cleanly: nothing to delete if stopped later
                out_dir = None

                # --- Motor + interruptible wait (skip on last iteration) ---
                if i < n_loops - 1:
                    if self._stop_flag.is_set():
                        stopped = True
                        break
                    if use_motor:
                        self._set_status(
                            f"Cycle {i+1}/{n_loops} - motor + waiting {wait_sec}s")
                        exec_motor({"move": True, "pause": 2.0})
                    else:
                        self._set_status(
                            f"Cycle {i+1}/{n_loops} - waiting {wait_sec}s "
                            "(motor off)")
                    # Sleep in 200 ms steps so stop is detected promptly
                    elapsed = 0.0
                    while elapsed < wait_sec and not self._stop_flag.is_set():
                        time.sleep(0.2)
                        elapsed += 0.2

        finally:
            if use_motor:
                motor_servo_disable(_MOTOR_CONFIG)
            if stopped:
                self._set_status("Stopped by user")
            else:
                self._set_status(
                    f"Done ({n_loops} cycle{'s' if n_loops != 1 else ''})")
            self.master.after(0, lambda: self._set_buttons_running(False))
            self.master.after(0, lambda: self._btn_stop.configure(state='disabled'))
            # Preview handling must run in the GUI thread, never here
            self.master.after(0, self._maybe_reopen_preview)


# ==============================================================================
# ENTRY POINT
# ==============================================================================
if __name__ == '__main__':
    gc.collect()
    root = tk.Tk()
    app = MainApp(root)
    # Closing via the window manager must release the camera as well
    root.protocol("WM_DELETE_WINDOW", app._quit)
    root.mainloop()
