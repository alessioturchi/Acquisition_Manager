#!/usr/bin/env python3
# Acquisition Manager - spectral acquisition suite for XIMEA cameras
# Copyright (C) 2026  Alessio Turchi
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
camera_view_lib.py
------------------
Live preview / pointing window for the XIMEA camera, refactored from
`camera_puntamento_mac_3.py` (v3) to be embeddable in acquisition_manager_gui.

Main differences with respect to the standalone v3 program:
  - The GUI lives in a `tk.Toplevel` owned by a parent application, not in
    its own `tk.Tk` root.  Closing the preview never closes the host app.
  - Explicit lifecycle: open() / close() / is_open().  close() joins the
    grabber thread *before* releasing the camera handle, so the device is
    guaranteed to be free when the acquisition pipeline reopens it.
  - XIMEA only (the OpenCV webcam fallback has been removed) and the camera
    is opened by serial number, not by index.
  - Image format is XI_RAW16, identical to run_acquisition(), so what is
    displayed is what will be acquired.
  - Camera settings (gain / exposure) are applied by the grabber thread via
    a pending-settings dict, never from the GUI thread while a frame is
    being fetched.
  - Optional overlay of the acquisition crop window [pos-hw, pos+hw].
  - Frame captures are written into <outdir>/live_frames/.

Original program written by Alessio Turchi.
Refactored for integration: September 2026.
"""

import json
import os
import sys
import threading
import time
from datetime import datetime

import tkinter as tk
from tkinter import messagebox

import numpy as np

# --- Optional / heavy dependencies, imported defensively so that the host
# --- application can still start (with the preview disabled) if they are
# --- missing on the acquisition PC.
try:
    import cv2
    CV2_AVAILABLE = True
except ImportError as _exc:
    cv2 = None
    CV2_AVAILABLE = False
    _CV2_ERROR = _exc

try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except ImportError as _exc:
    PIL_AVAILABLE = False
    _PIL_ERROR = _exc

try:
    from ximea import xiapi
    XIMEA_AVAILABLE = True
except ImportError as _exc:
    XIMEA_AVAILABLE = False
    _XIMEA_ERROR = _exc

try:
    from astropy.io import fits
    FITS_AVAILABLE = True
except ImportError:
    FITS_AVAILABLE = False


def preview_available() -> tuple:
    """
    Report whether the preview can run in this installation.

    Returns
    -------
    (ok, reason) : (bool, str)
        reason is an empty string when ok is True.
    """
    missing = []
    if not CV2_AVAILABLE:
        missing.append("opencv-python")
    if not PIL_AVAILABLE:
        missing.append("Pillow")
    if not XIMEA_AVAILABLE:
        missing.append("ximea (xiAPI Python)")
    if missing:
        return False, "Missing module(s): " + ", ".join(missing)
    return True, ""


def _get_base_dir() -> str:
    """
    Base directory for the preview config file.
      - plain script          : directory of this .py
      - PyInstaller (macOS)   : directory containing the .app bundle
      - PyInstaller (Windows) : directory of the .exe
    """
    if getattr(sys, 'frozen', False):
        exe = os.path.abspath(sys.executable)
        if sys.platform == 'darwin':
            return os.path.abspath(os.path.join(exe, '../../../..'))
        return os.path.dirname(exe)
    return os.path.dirname(os.path.abspath(__file__))


# ==============================================================================
# PARAMETERS
# ==============================================================================
DEFAULT_CROSSHAIR_SIZE = 30
DEFAULT_CIRCLE_RADIUS = 100
MOVE_STEP = 5

# Default gain (dB) restored by the "Reset gain" control of the host GUI.
DEFAULT_GAIN_DB = 0.0

# Minimum get_image() timeout, milliseconds.  The actual timeout is
# max(MIN_GRAB_TIMEOUT_MS, 2 * exposure) so the grabber thread is always
# guaranteed to unblock and close() can join it.
MIN_GRAB_TIMEOUT_MS = 1000

# Settle time after close_device() before the handle is reused (seconds).
CAMERA_RELEASE_SETTLE_S = 0.3

BG = '#1e1e1e'
BG2 = '#2a2a2a'
FG = '#dddddd'
FG2 = '#888888'
GREEN = '#00cc44'
CYAN = '#00ccff'
ACCENT = '#3a3a3a'
MAGENTA_BGR = (255, 0, 255)


# ==============================================================================
# CLASS: CameraView
# ==============================================================================
class CameraView:
    """
    Live XIMEA preview in a Toplevel window.

    The instance owns the camera handle only between open() and close().
    The host application must call close() before starting any acquisition.

    Parameters
    ----------
    master : tk.Misc
        Parent widget (typically the host application root).
    serial : str
        XIMEA serial number.  Empty string opens the first device found.
    config_file : str
        Path of the JSON file holding overlay/pointing state.
    outdir : str
        Root output directory; captures go to <outdir>/live_frames/.
    crop_provider : callable or None
        Called from the GUI thread on every displayed frame; must return
        (position, halfwidth) in raw pixel columns, or None.  Used to draw
        the acquisition crop window on the preview.
    apply_callback : callable or None
        Called with the dict returned by get_params() when the user presses
        "Use for acquisition".
    on_close : callable or None
        Called (from the GUI thread) after the window has been destroyed.
    initial_exposure_ms : float or None
        Exposure to apply at open(), overriding the stored config.
    initial_gain : float or None
        Gain to apply at open(), overriding the stored config.
    """

    def __init__(self, master, serial="", config_file=None, outdir=".",
                 crop_provider=None, apply_callback=None, on_close=None,
                 initial_exposure_ms=None, initial_gain=None):
        self.master = master
        self.serial = str(serial or "")
        self.config_file = config_file or os.path.join(_get_base_dir(),
                                                       'camera_config.json')
        self.outdir = outdir
        self.crop_provider = crop_provider
        self.apply_callback = apply_callback
        self.on_close = on_close

        self.win = None                 # Toplevel, created in open()

        # Camera
        self.ximea_cam = None
        self.ximea_img = None
        self.ximea_exp_min = 110
        self.ximea_exp_max = 10_000_000
        self.ximea_gain_min = -4.0
        self.ximea_gain_max = 38.0

        # exposure is stored in microseconds (native XIMEA unit)
        self.params = {'gain': DEFAULT_GAIN_DB, 'exposure': 10000.0}

        # Overlay state
        self.offset_x = 0
        self.offset_y = 0
        self.crosshair_size = DEFAULT_CROSSHAIR_SIZE
        self.circle_radius = DEFAULT_CIRCLE_RADIUS
        self.centroid_data = None

        # Threading
        self._frame_lock = threading.Lock()
        self._cam_settings_lock = threading.Lock()
        self._pending_cam_settings = {}   # applied by camera_loop between frames
        self.current_raw_frame = None
        self.current_display_frame = None
        self.running = False
        self.paused = False
        self.frame_count = 0
        self.cam_thread = None

        # Display geometry (set after camera init)
        self.cam_w = 640
        self.cam_h = 480
        self._fit_scale = 1.0
        self._fit_ox = 0
        self._fit_oy = 0

        self.load_config()
        if initial_exposure_ms is not None and initial_exposure_ms > 0:
            self.params['exposure'] = float(initial_exposure_ms) * 1000.0
        if initial_gain is not None:
            self.params['gain'] = float(initial_gain)

    # --------------------------------------------------------------------------
    # LIFECYCLE
    # --------------------------------------------------------------------------
    def is_open(self) -> bool:
        """True while the Toplevel exists and the grabber thread is running."""
        return self.win is not None and self.running

    def open(self) -> bool:
        """
        Create the window, open the camera and start the grabber thread.

        Returns False (and leaves nothing behind) if the preview cannot run.
        """
        if self.is_open():
            self.win.lift()
            return True

        ok, reason = preview_available()
        if not ok:
            messagebox.showerror("Camera preview", reason, parent=self.master)
            return False

        self.win = tk.Toplevel(self.master)
        self.win.title("Camera preview / pointing")
        self.win.configure(bg=BG)
        self.win.geometry("1100x680")
        self.win.minsize(800, 500)

        self.setup_gui()

        if not self._init_ximea():
            messagebox.showerror(
                "Camera preview",
                f"Cannot open XIMEA camera (SN='{self.serial or 'first'}').\n"
                "Check that no other program is holding the device.",
                parent=self.master)
            self._destroy_window()
            return False

        self.configure_sliders_for_camera()

        self.running = True
        self.cam_thread = threading.Thread(target=self.camera_loop, daemon=True)
        self.cam_thread.start()
        self.win.after(33, self.update_display)
        return True

    def close(self):
        """
        Stop the grabber, release the camera handle and destroy the window.

        The grabber thread is joined *before* the device is closed: closing a
        handle while get_image() is in flight can leave the device busy and
        make the next open_device_by_SN() fail.
        """
        if self.win is None:
            return

        was_running = self.running
        self.running = False

        if self.cam_thread is not None and was_running:
            # Worst case the thread is inside get_image(): wait for its timeout.
            grab_timeout_s = self._grab_timeout_ms() / 1000.0
            self.cam_thread.join(timeout=grab_timeout_s + 3.0)
            if self.cam_thread.is_alive():
                print("[CameraView] WARNING: grabber thread did not terminate; "
                      "closing the device anyway")
        self.cam_thread = None

        if self.ximea_cam is not None:
            try:
                self.ximea_cam.stop_acquisition()
            except Exception as exc:
                print(f"[CameraView] stop_acquisition failed: {exc}")
            try:
                self.ximea_cam.close_device()
            except Exception as exc:
                print(f"[CameraView] close_device failed: {exc}")
            self.ximea_cam = None
            self.ximea_img = None
            # Give the driver a moment to release the device before reuse.
            time.sleep(CAMERA_RELEASE_SETTLE_S)

        self._destroy_window()

        if self.on_close is not None:
            try:
                self.on_close()
            except Exception as exc:
                print(f"[CameraView] on_close callback failed: {exc}")

    def _destroy_window(self):
        """Tear down the Toplevel and drop all widget references."""
        if self.win is not None:
            try:
                self.win.destroy()
            except tk.TclError:
                pass
        self.win = None
        self.running = False
        self.current_raw_frame = None
        self.current_display_frame = None

    # --------------------------------------------------------------------------
    # CONFIG
    # --------------------------------------------------------------------------
    def load_config(self):
        """Load overlay/pointing state from the JSON config, if present."""
        if not os.path.exists(self.config_file):
            return
        try:
            with open(self.config_file) as f:
                cfg = json.load(f)
            p = cfg.get('params', {})
            self.params['gain'] = p.get('gain', DEFAULT_GAIN_DB)
            self.params['exposure'] = p.get('exposure', 10000.0)
            o = cfg.get('overlay', {})
            self.crosshair_size = o.get('crosshair_size', DEFAULT_CROSSHAIR_SIZE)
            self.circle_radius = o.get('circle_radius', DEFAULT_CIRCLE_RADIUS)
            self.offset_x = o.get('offset_x', 0)
            self.offset_y = o.get('offset_y', 0)
        except Exception as e:
            print(f"[CameraView] Config load error: {e}")

    def save_config(self):
        """Persist camera settings and overlay state to the JSON config."""
        try:
            cfg = {
                'serial': self.serial,
                'params': {'gain': self.params['gain'],
                           'exposure': self.params['exposure']},
                'overlay': {
                    'crosshair': self.show_crosshair.get(),
                    'grid': self.show_grid.get(),
                    'circle': self.show_circle.get(),
                    'info': self.show_info.get(),
                    'crosshair_size': self.crosshair_size,
                    'circle_radius': self.circle_radius,
                    'offset_x': self.offset_x,
                    'offset_y': self.offset_y,
                }
            }
            with open(self.config_file, 'w') as f:
                json.dump(cfg, f, indent=4)
            print(f"[CameraView] Config saved to {self.config_file}")
        except Exception as e:
            print(f"[CameraView] Config save error: {e}")

    # --------------------------------------------------------------------------
    # GUI
    # --------------------------------------------------------------------------
    def setup_gui(self):
        # Main row: canvas | controls
        main = tk.Frame(self.win, bg=BG)
        main.pack(fill=tk.BOTH, expand=True)

        # --- VIDEO CANVAS ---
        self.canvas = tk.Canvas(main, bg='black', cursor='crosshair',
                                highlightthickness=0)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.photo_image = None

        # --- CONTROL PANEL ---
        ctrl_outer = tk.Frame(main, bg=BG2, width=265)
        ctrl_outer.pack(side=tk.RIGHT, fill=tk.Y)
        ctrl_outer.pack_propagate(False)

        # Scrollable inner panel
        ctrl_canvas = tk.Canvas(ctrl_outer, bg=BG2, highlightthickness=0, width=248)
        scrollbar = tk.Scrollbar(ctrl_outer, orient=tk.VERTICAL,
                                 command=ctrl_canvas.yview)
        ctrl_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        ctrl_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.ctrl = tk.Frame(ctrl_canvas, bg=BG2)
        _ctrl_win = ctrl_canvas.create_window((0, 0), window=self.ctrl, anchor='nw')
        self.ctrl.bind('<Configure>',
                       lambda e: ctrl_canvas.configure(
                           scrollregion=ctrl_canvas.bbox('all')))
        ctrl_canvas.bind('<Configure>',
                         lambda e: ctrl_canvas.itemconfig(_ctrl_win, width=e.width))

        # Mousewheel: bound on this Toplevel, never with bind_all.  A bind_all
        # would hijack the wheel in the host application and keep firing (with
        # a TclError) after this window has been destroyed.  Events from child
        # widgets propagate up the bindtag chain, so one binding is enough;
        # it is gated on the pointer being over the control panel.
        def _wheel(event, step=None):
            if event.x_root < ctrl_outer.winfo_rootx():
                return
            if step is None:
                step = -1 * (event.delta // 120)
            ctrl_canvas.yview_scroll(step, 'units')

        self.win.bind('<MouseWheel>', _wheel)                       # Windows / macOS
        self.win.bind('<Button-4>', lambda e: _wheel(e, step=-1))   # X11
        self.win.bind('<Button-5>', lambda e: _wheel(e, step=+1))   # X11

        self._build_controls()

        # --- STATUS BAR ---
        self.status_var = tk.StringVar(value="Initialising...")
        tk.Label(self.win, textvariable=self.status_var, bg='#111111',
                 fg=CYAN, anchor='w', font=('Courier', 10), padx=6
                 ).pack(fill=tk.X, side=tk.BOTTOM)

        # Keyboard bindings are scoped to this Toplevel, not to the whole app.
        for key, cb in [
            ('<space>', lambda e: self.toggle_pause()),
            ('c',       lambda e: self.capture()),
            ('s',       lambda e: self.save_config()),
            ('r',       lambda e: self.reset_offset()),
            ('n',       lambda e: self.show_centroid.set(not self.show_centroid.get())),
            ('t',       lambda e: self.show_info.set(not self.show_info.get())),
            ('x',       lambda e: self.show_crosshair.set(not self.show_crosshair.get())),
            ('o',       lambda e: self.show_circle.set(not self.show_circle.get())),
            ('b',       lambda e: self.show_grid.set(not self.show_grid.get())),
            ('v',       lambda e: self.show_row_profile.set(not self.show_row_profile.get())),
            ('g',       lambda e: self.show_col_profile.set(not self.show_col_profile.get())),
            ('w',       lambda e: self.show_crop.set(not self.show_crop.get())),
            ('i',       lambda e: self._move_offset(0, -MOVE_STEP)),
            ('m',       lambda e: self._move_offset(0, +MOVE_STEP)),
            ('j',       lambda e: self._move_offset(-MOVE_STEP, 0)),
            ('k',       lambda e: self._move_offset(+MOVE_STEP, 0)),
            ('q',       lambda e: self.close()),
            ('<Escape>', lambda e: self.close()),
        ]:
            self.win.bind(key, cb)

        self.win.protocol("WM_DELETE_WINDOW", self.close)

    def _section(self, text):
        tk.Label(self.ctrl, text=text, bg=BG2, fg=FG2,
                 font=('Helvetica', 8, 'bold')).pack(fill=tk.X, padx=8, pady=(10, 0))
        tk.Frame(self.ctrl, bg='#444444', height=1).pack(fill=tk.X, padx=8, pady=(1, 4))

    def _mk_btn(self, text, command, bg_color, pady=3):
        """Frame+Label button: honours fg/bg on macOS (Aqua ignores tk.Button)."""
        f = tk.Frame(self.ctrl, bg=bg_color, cursor='hand2')
        f.pack(fill=tk.X, padx=10, pady=pady)
        lbl = tk.Label(f, text=text, bg=bg_color, fg='white',
                       font=('Helvetica', 10), padx=4, pady=5, cursor='hand2')
        lbl.pack(fill=tk.X)
        for w in (f, lbl):
            w.bind('<Button-1>', lambda _e, cmd=command: cmd())
        return lbl

    def _slider(self, label, var, from_, to_, res, command=None):
        tk.Label(self.ctrl, text=label, bg=BG2, fg=FG,
                 font=('Helvetica', 9)).pack(anchor='w', padx=10)
        row = tk.Frame(self.ctrl, bg=BG2)
        row.pack(fill=tk.X, padx=8, pady=(0, 4))
        s = tk.Scale(row, variable=var, orient=tk.HORIZONTAL,
                     from_=from_, to=to_, resolution=res,
                     bg=BG2, fg=FG, troughcolor='#555555',
                     activebackground=CYAN, highlightthickness=0,
                     showvalue=False,
                     command=command if command else lambda v: None)
        s.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(row, textvariable=var, bg=BG2, fg=GREEN,
                 font=('Courier', 9), width=7).pack(side=tk.RIGHT)
        return s

    def _build_controls(self):
        # Camera info
        self.cam_label = tk.Label(self.ctrl, text="Camera: -",
                                  bg=BG2, fg=FG, font=('Courier', 10, 'bold'))
        self.cam_label.pack(fill=tk.X, padx=8, pady=(10, 4))

        # --- CAMERA ---
        self._section("CAMERA")

        self.gain_var = tk.DoubleVar(value=round(self.params['gain'], 1))
        self.gain_slider = self._slider("Gain (dB)", self.gain_var, -4, 38, 0.5,
                                        command=self.on_gain_change)

        exp_ms = self.params['exposure'] / 1000.0
        self.exp_var = tk.DoubleVar(value=round(exp_ms, 1))
        self.exp_slider = self._slider("Exposure (ms)", self.exp_var, 0.1, 1000, 0.5,
                                       command=self.on_exposure_change)

        # --- OVERLAY ---
        self._section("OVERLAY")

        self.show_crosshair   = tk.BooleanVar(value=True)
        self.show_circle      = tk.BooleanVar(value=True)
        self.show_grid        = tk.BooleanVar(value=False)
        self.show_info        = tk.BooleanVar(value=True)
        self.show_centroid    = tk.BooleanVar(value=False)
        self.show_row_profile = tk.BooleanVar(value=False)
        self.show_col_profile = tk.BooleanVar(value=False)
        self.show_crop        = tk.BooleanVar(value=True)

        checks = [
            ("Crosshair    (x)", self.show_crosshair),
            ("Circle       (o)", self.show_circle),
            ("Grid         (b)", self.show_grid),
            ("Info         (t)", self.show_info),
            ("Centroid     (n)", self.show_centroid),
            ("Row profile  (v)", self.show_row_profile),
            ("Col profile  (g)", self.show_col_profile),
            ("Crop window  (w)", self.show_crop),
        ]
        for lbl, var in checks:
            tk.Checkbutton(self.ctrl, text=lbl, variable=var,
                           bg=BG2, fg=FG, selectcolor=ACCENT,
                           activebackground=BG2, activeforeground=FG,
                           font=('Helvetica', 9)).pack(anchor='w', padx=12)

        # --- RETICLE POSITION ---
        self._section("RETICLE POSITION")

        self.offset_x_var = tk.IntVar(value=self.offset_x)
        self.offset_x_slider = self._slider(
            "Offset X", self.offset_x_var, -400, 400, 1,
            command=lambda v: setattr(self, 'offset_x', int(float(v))))

        self.offset_y_var = tk.IntVar(value=self.offset_y)
        self.offset_y_slider = self._slider(
            "Offset Y", self.offset_y_var, -300, 300, 1,
            command=lambda v: setattr(self, 'offset_y', int(float(v))))

        tk.Button(self.ctrl, text="Reset offset (r)", command=self.reset_offset,
                  bg=ACCENT, fg='white', font=('Helvetica', 9),
                  relief=tk.FLAT, padx=4,
                  highlightbackground=ACCENT).pack(fill=tk.X, padx=10, pady=(2, 4))

        # --- OVERLAY SIZES ---
        self._section("OVERLAY SIZES")

        self.crosshair_size_var = tk.IntVar(value=self.crosshair_size)
        self._slider("Crosshair size", self.crosshair_size_var, 10, 300, 5,
                     command=lambda v: setattr(self, 'crosshair_size', int(float(v))))

        self.circle_radius_var = tk.IntVar(value=self.circle_radius)
        self._slider("Circle radius", self.circle_radius_var, 20, 500, 10,
                     command=lambda v: setattr(self, 'circle_radius', int(float(v))))

        # --- ACTIONS ---
        self._section("ACTIONS")

        self.pause_btn = self._mk_btn("||  Pause     (space)", self.toggle_pause,
                                      '#3a3a5e')
        self._mk_btn("->  Use for acquisition", self.apply_to_acquisition, '#6a4a00')
        self._mk_btn("[]  Capture FITS  (c)", self.capture,    '#1a4a7a')
        self._mk_btn("==  Save config   (s)", self.save_config, '#1a5e30')
        self._mk_btn("X   Close         (q)", self.close,       '#7a1a1a', pady=(3, 12))

    def configure_sliders_for_camera(self):
        """Adapt slider ranges to the camera actually opened."""
        self.cam_label.config(text=f"XIMEA {self.cam_w}x{self.cam_h}")
        self.gain_slider.config(from_=self.ximea_gain_min, to=self.ximea_gain_max,
                                resolution=0.5)
        self.gain_var.set(round(self.params['gain'], 1))

        exp_ms_min = max(0.1, self.ximea_exp_min / 1000.0)
        exp_ms_max = min(2000.0, self.ximea_exp_max / 1000.0)
        self.exp_slider.config(from_=round(exp_ms_min, 1), to=round(exp_ms_max, 1),
                               resolution=0.5)
        self.exp_var.set(round(self.params['exposure'] / 1000.0, 1))

        # Offset ranges follow the real sensor size
        hx = self.cam_w // 2
        hy = self.cam_h // 2
        self.offset_x_slider.config(from_=-hx, to=hx)
        self.offset_y_slider.config(from_=-hy, to=hy)
        self.offset_x_var.set(max(-hx, min(hx, self.offset_x)))
        self.offset_y_var.set(max(-hy, min(hy, self.offset_y)))
        self.offset_x = self.offset_x_var.get()
        self.offset_y = self.offset_y_var.get()

    # --------------------------------------------------------------------------
    # CAMERA INIT
    # --------------------------------------------------------------------------
    def _init_ximea(self) -> bool:
        """Open the XIMEA device by serial number and start acquisition."""
        try:
            self.ximea_cam = xiapi.Camera()
            if self.serial:
                self.ximea_cam.open_device_by_SN(self.serial)
            else:
                self.ximea_cam.open_device()
            self.ximea_img = xiapi.Image()

            # Same format as run_acquisition(): what you see is what you acquire.
            try:
                self.ximea_cam.set_imgdataformat('XI_RAW16')
            except Exception as exc:
                print(f"[CameraView] set_imgdataformat failed: {exc}")

            self.ximea_exp_min = self.ximea_cam.get_exposure_minimum()
            self.ximea_exp_max = self.ximea_cam.get_exposure_maximum()
            self.ximea_gain_min = self.ximea_cam.get_gain_minimum()
            self.ximea_gain_max = self.ximea_cam.get_gain_maximum()

            exp = max(self.ximea_exp_min,
                      min(self.ximea_exp_max, int(self.params['exposure'])))
            self.ximea_cam.set_exposure(exp)
            self.params['exposure'] = self.ximea_cam.get_exposure()

            gain = max(self.ximea_gain_min,
                       min(self.ximea_gain_max, float(self.params['gain'])))
            self.ximea_cam.set_gain(gain)
            self.params['gain'] = self.ximea_cam.get_gain()

            self.cam_w = self.ximea_cam.get_width()
            self.cam_h = self.ximea_cam.get_height()
            self.ximea_cam.start_acquisition()
            print(f"[CameraView] XIMEA opened: {self.cam_w}x{self.cam_h}, "
                  f"exp={self.params['exposure']:.0f} us, "
                  f"gain={self.params['gain']:.1f} dB")
            return True
        except Exception as e:
            print(f"[CameraView] XIMEA open error: {e}")
            try:
                if self.ximea_cam is not None:
                    self.ximea_cam.close_device()
            except Exception:
                pass
            self.ximea_cam = None
            self.ximea_img = None
            return False

    def _grab_timeout_ms(self) -> int:
        """get_image() timeout: twice the exposure, at least MIN_GRAB_TIMEOUT_MS."""
        return max(MIN_GRAB_TIMEOUT_MS, int(2 * self.params['exposure'] / 1000.0))

    # --------------------------------------------------------------------------
    # CAMERA LOOP (background thread)
    # --------------------------------------------------------------------------
    def camera_loop(self):
        """
        Grabber thread.  All writes to the camera happen here, between frames,
        so no xiAPI call is ever issued while get_image() is in flight.
        """
        n_errors = 0
        while self.running:
            # Apply pending gain/exposure changes requested by the GUI thread
            with self._cam_settings_lock:
                pending = self._pending_cam_settings.copy()
                self._pending_cam_settings.clear()
            if 'gain' in pending:
                try:
                    v = max(self.ximea_gain_min,
                            min(self.ximea_gain_max, float(pending['gain'])))
                    self.ximea_cam.set_gain(v)
                    self.params['gain'] = self.ximea_cam.get_gain()
                except Exception as exc:
                    print(f"[CameraView] set_gain failed: {exc}")
            if 'exposure' in pending:
                try:
                    us = int(max(self.ximea_exp_min,
                                 min(self.ximea_exp_max, float(pending['exposure']))))
                    self.ximea_cam.set_exposure(us)
                    self.params['exposure'] = self.ximea_cam.get_exposure()
                except Exception as exc:
                    print(f"[CameraView] set_exposure failed: {exc}")

            if self.paused:
                time.sleep(0.033)
                continue

            try:
                self.ximea_cam.get_image(self.ximea_img,
                                         timeout=self._grab_timeout_ms())
                raw = self.ximea_img.get_image_data_numpy().copy()
                if raw.dtype == np.uint16:
                    # 12-bit data right-aligned in a 16-bit container.
                    # Clip instead of plain division: values above 4095 would
                    # silently wrap around when cast to uint8.
                    disp8 = np.clip(raw >> 4, 0, 255).astype(np.uint8)
                else:
                    disp8 = raw
                if disp8.ndim == 2:
                    frame_bgr = cv2.cvtColor(disp8, cv2.COLOR_GRAY2BGR)
                else:
                    frame_bgr = disp8
                with self._frame_lock:
                    self.current_raw_frame = raw
                    self.current_display_frame = frame_bgr
                self.frame_count += 1
                n_errors = 0
            except Exception as e:
                n_errors += 1
                if n_errors <= 5 or n_errors % 50 == 0:
                    print(f"[CameraView] grab error ({n_errors}): {e}")
                time.sleep(0.1)

    # --------------------------------------------------------------------------
    # DISPLAY (GUI thread)
    # --------------------------------------------------------------------------
    def update_display(self):
        if not self.running or self.win is None:
            return
        with self._frame_lock:
            frame = (self.current_display_frame.copy()
                     if self.current_display_frame is not None else None)
            raw = (self.current_raw_frame.copy()
                   if self.current_raw_frame is not None else None)

        if frame is not None:
            # Centroid is computed on the raw data when available, so that it
            # is not biased by the 12->8 bit display rescaling.
            if self.show_centroid.get():
                if raw is not None and raw.ndim == 2:
                    gray = raw
                else:
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                self.centroid_data = self.compute_centroid(gray)
            else:
                self.centroid_data = None

            self.draw_overlay(frame, raw)

            cw = self.canvas.winfo_width()
            ch = self.canvas.winfo_height()
            if cw > 1 and ch > 1:
                frame = self._fit_frame(frame, cw, ch)
                if self.show_grid.get():
                    self._draw_grid(frame)

            img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            self.photo_image = ImageTk.PhotoImage(image=img)
            self.canvas.create_image(0, 0, anchor=tk.NW, image=self.photo_image)

            self._update_status()

        if self.running and self.win is not None:
            self.win.after(33, self.update_display)

    def _fit_frame(self, frame, cw, ch):
        h, w = frame.shape[:2]
        scale = min(cw / w, ch / h)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
        canvas_img = np.zeros((ch, cw, 3), dtype=np.uint8)
        ox, oy = (cw - nw) // 2, (ch - nh) // 2
        canvas_img[oy:oy + nh, ox:ox + nw] = resized
        self._fit_scale = scale
        self._fit_ox = ox
        self._fit_oy = oy
        return canvas_img

    def _draw_grid(self, frame):
        """Grid drawn at display resolution to avoid aliasing from the resize."""
        dh, dw = frame.shape[:2]
        scale = self._fit_scale
        ox = self._fit_ox
        oy = self._fit_oy
        cx_d = ox + int((self.cam_w // 2 + self.offset_x) * scale)
        cy_d = oy + int((self.cam_h // 2 + self.offset_y) * scale)
        for x in range(0, dw, 50):
            cv2.line(frame, (x, 0), (x, dh), (80, 80, 80), 1)
        for y in range(0, dh, 50):
            cv2.line(frame, (0, y), (dw, y), (80, 80, 80), 1)
        if 0 <= cx_d < dw:
            cv2.line(frame, (cx_d, 0), (cx_d, dh), (0, 200, 0), 1)
        if 0 <= cy_d < dh:
            cv2.line(frame, (0, cy_d), (dw, cy_d), (0, 200, 0), 1)

    def _update_status(self):
        if self.centroid_data is not None:
            xc, yc, fx, fy = self.centroid_data
            self.status_var.set(
                f"Centroid - Xc={xc:.1f}  Yc={yc:.1f}  "
                f"FWHM_X={fx:.1f}  FWHM_Y={fy:.1f} px  |  Frame #{self.frame_count}"
            )
        else:
            status = "PAUSED" if self.paused else "live"
            self.status_var.set(
                f"XIMEA - {self.cam_w}x{self.cam_h}  [{status}]  "
                f"|  Frame #{self.frame_count}"
            )

    # --------------------------------------------------------------------------
    # OVERLAY
    # --------------------------------------------------------------------------
    def draw_overlay(self, frame, raw=None):
        h, w = frame.shape[:2]
        cx = w // 2 + self.offset_x
        cy = h // 2 + self.offset_y

        gray_clean = None
        if self.show_row_profile.get() or self.show_col_profile.get():
            gray_clean = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # Acquisition crop window [position-halfwidth, position+halfwidth]
        crop = self._get_crop_window()
        if self.show_crop.get() and crop is not None:
            xmin, xmax = crop
            for xv, tag in ((xmin, "crop min"), (xmax, "crop max")):
                if 0 <= xv < w:
                    cv2.line(frame, (xv, 0), (xv, h), MAGENTA_BGR, 2)
                    cv2.putText(frame, tag, (max(2, xv - 40), h - 34),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, MAGENTA_BGR, 1)

        if self.show_crosshair.get():
            sz = self.crosshair_size
            cv2.line(frame, (cx - sz, cy), (cx + sz, cy), (0, 255, 0), 2)
            cv2.line(frame, (cx, cy - sz), (cx, cy + sz), (0, 255, 0), 2)
            cv2.circle(frame, (cx, cy), 3, (0, 255, 0), -1)

        if self.show_circle.get():
            r = self.circle_radius
            cv2.circle(frame, (cx, cy), r, (0, 255, 0), 2)
            cv2.circle(frame, (cx, cy), r // 2, (0, 255, 0), 1)

        if self.show_centroid.get() and self.centroid_data is not None:
            xc_f, yc_f, fwx, fwy = self.centroid_data
            xci, yci = int(round(xc_f)), int(round(yc_f))
            cv2.line(frame, (xci - 18, yci), (xci + 18, yci), (0, 255, 255), 2)
            cv2.line(frame, (xci, yci - 18), (xci, yci + 18), (0, 255, 255), 2)
            cv2.circle(frame, (xci, yci), 5, (0, 255, 255), 1)
            ax = (max(1, int(fwx / 2)), max(1, int(fwy / 2)))
            cv2.ellipse(frame, (xci, yci), ax, 0, 0, 360, (0, 200, 255), 1)

        if self.show_info.get():
            # Statistics come from the RAW frame, not from the 8-bit display copy
            if raw is not None:
                rg = raw if raw.ndim == 2 else cv2.cvtColor(raw, cv2.COLOR_BGR2GRAY)
            else:
                rg = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            pmin, pmax, pmean = int(rg.min()), int(rg.max()), rg.mean()

            lines = [
                f"Frame: {self.frame_count}" + (" [PAUSED]" if self.paused else ""),
                f"Gain: {self.params['gain']:.1f} dB",
                f"Exp:  {self.params['exposure'] / 1000.0:.1f} ms",
                f"I: {pmin}/{pmean:.0f}/{pmax}",
            ]
            if self.show_centroid.get() and self.centroid_data is not None:
                xc_f, yc_f, fwx, fwy = self.centroid_data
                lines += [f"Xc={xc_f:.1f}  Yc={yc_f:.1f}",
                          f"FWx={fwx:.1f} FWy={fwy:.1f}"]

            iy = 26
            for line in lines:
                col = (0, 255, 255) if line.startswith(("Xc", "FW")) else (0, 255, 0)
                cv2.putText(frame, line, (8, iy), cv2.FONT_HERSHEY_SIMPLEX,
                            0.7, col, 2)
                iy += 28

            cv2.putText(frame, f"Reticle: ({cx},{cy})",
                        (8, h - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 0), 2)

        if self.show_row_profile.get() and gray_clean is not None and 0 <= cy < h:
            row = gray_clean[cy, :].astype(float)
            ph = h // 4
            rm = row.max()
            rn = (row / rm * ph).astype(int) if rm > 0 else np.zeros(w, int)
            pts = np.column_stack((np.arange(w), cy - rn)).astype(np.int32)
            cv2.polylines(frame, [pts], False, (0, 0, 255), 1, cv2.LINE_AA)

        if self.show_col_profile.get() and gray_clean is not None and 0 <= cx < w:
            col = gray_clean[:, cx].astype(float)
            pw = w // 4
            cm = col.max()
            cn = (col / cm * pw).astype(int) if cm > 0 else np.zeros(h, int)
            pts = np.column_stack((cx + cn, np.arange(h))).astype(np.int32)
            cv2.polylines(frame, [pts], False, (0, 0, 255), 1, cv2.LINE_AA)

    def _get_crop_window(self):
        """
        Query the host application for the acquisition crop window.

        Called from the GUI thread only, so the provider may safely read
        tkinter variables.  Returns (xmin, xmax) or None.
        """
        if self.crop_provider is None:
            return None
        try:
            res = self.crop_provider()
        except Exception:
            return None
        if not res:
            return None
        position, halfwidth = res
        if halfwidth <= 0:
            return None
        return int(position) - int(halfwidth), int(position) + int(halfwidth)

    # --------------------------------------------------------------------------
    # CENTROID
    # --------------------------------------------------------------------------
    def compute_centroid(self, gray):
        """Intensity-weighted centroid and FWHM after 10th-percentile background."""
        bg = float(np.percentile(gray, 10))
        img = gray.astype(np.float64) - bg
        np.clip(img, 0, None, out=img)
        total = img.sum()
        if total < 1.0:
            return None
        h, w = img.shape
        xx = np.arange(w, dtype=np.float64)
        yy = np.arange(h, dtype=np.float64)
        px = img.sum(axis=0)
        py = img.sum(axis=1)
        xc = (px * xx).sum() / total
        yc = (py * yy).sum() / total
        sx = np.sqrt(((px * (xx - xc) ** 2).sum()) / total)
        sy = np.sqrt(((py * (yy - yc) ** 2).sum()) / total)
        return xc, yc, 2.3548 * sx, 2.3548 * sy

    # --------------------------------------------------------------------------
    # SLIDER CALLBACKS
    # --------------------------------------------------------------------------
    def on_gain_change(self, val):
        """Queue a gain change; applied by camera_loop between two frames."""
        with self._cam_settings_lock:
            self._pending_cam_settings['gain'] = float(val)

    def on_exposure_change(self, val):
        """Queue an exposure change (slider is in ms, xiAPI wants us)."""
        with self._cam_settings_lock:
            self._pending_cam_settings['exposure'] = float(val) * 1000.0

    def set_gain(self, gain_db):
        """Programmatic gain change (used by the host 'Reset gain' button)."""
        self.gain_var.set(round(float(gain_db), 1))
        self.on_gain_change(gain_db)

    def set_exposure_ms(self, exp_ms):
        """Programmatic exposure change, milliseconds."""
        self.exp_var.set(round(float(exp_ms), 1))
        self.on_exposure_change(exp_ms)

    # --------------------------------------------------------------------------
    # ACTIONS
    # --------------------------------------------------------------------------
    def toggle_pause(self):
        self.paused = not self.paused
        self.pause_btn.config(
            text=">   Resume    (space)" if self.paused else "||  Pause     (space)")

    def reset_offset(self):
        self.offset_x = 0
        self.offset_y = 0
        self.offset_x_var.set(0)
        self.offset_y_var.set(0)

    def _move_offset(self, dx, dy):
        self.offset_x += dx
        self.offset_y += dy
        self.offset_x_var.set(self.offset_x)
        self.offset_y_var.set(self.offset_y)

    def get_params(self) -> dict:
        """
        Current pointing/camera state, for transfer to the acquisition GUI.

        'position' is the suggested spectral column: the measured centroid X
        when the centroid overlay is active and valid, otherwise the reticle X.
        """
        reticle_x = self.cam_w // 2 + self.offset_x
        if self.show_centroid.get() and self.centroid_data is not None:
            position = int(round(self.centroid_data[0]))
            source = "centroid"
        else:
            position = int(reticle_x)
            source = "reticle"
        return {
            "position":    position,
            "pos_source":  source,
            "reticle_x":   int(reticle_x),
            "reticle_y":   int(self.cam_h // 2 + self.offset_y),
            "gain":        float(self.params['gain']),
            "texp_ms":     float(self.params['exposure']) / 1000.0,
            "centroid":    self.centroid_data,
            "cam_w":       int(self.cam_w),
            "cam_h":       int(self.cam_h),
        }

    def apply_to_acquisition(self):
        """Push the current pointing/camera state to the host application."""
        if self.apply_callback is None:
            print("[CameraView] No apply callback registered")
            return
        p = self.get_params()
        try:
            self.apply_callback(p)
            self.status_var.set(
                f"Sent to acquisition: position={p['position']} ({p['pos_source']}), "
                f"texp={p['texp_ms']:.1f} ms, gain={p['gain']:.1f} dB")
        except Exception as exc:
            print(f"[CameraView] apply callback failed: {exc}")

    def capture(self):
        """Save the current raw frame as FITS into <outdir>/live_frames/."""
        with self._frame_lock:
            raw = (self.current_raw_frame.copy()
                   if self.current_raw_frame is not None else None)
        if raw is None:
            print("[CameraView] No frame to capture")
            return

        save_dir = os.path.join(self.outdir, "live_frames")
        try:
            os.makedirs(save_dir, exist_ok=True)
        except OSError as exc:
            print(f"[CameraView] Cannot create {save_dir}: {exc}")
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        gray = raw if raw.ndim == 2 else cv2.cvtColor(raw, cv2.COLOR_BGR2GRAY)
        centroid_raw = self.compute_centroid(gray)

        if FITS_AVAILABLE:
            fname = os.path.join(save_dir, f"live_{timestamp}.fits")
            h_img, w_img = gray.shape
            hdu = fits.PrimaryHDU(gray)
            hdu.header['GAIN']     = (self.params['gain'], 'Camera gain (dB)')
            hdu.header['EXPOSURE'] = (self.params['exposure'], 'Exposure (us)')
            hdu.header['TEXP_MS']  = (self.params['exposure'] / 1000.0,
                                      'Exposure (ms)')
            hdu.header['CENTERX']  = (w_img // 2 + self.offset_x, 'Reticle center X')
            hdu.header['CENTERY']  = (h_img // 2 + self.offset_y, 'Reticle center Y')
            hdu.header['OFFSET_X'] = (self.offset_x, 'Offset X from center')
            hdu.header['OFFSET_Y'] = (self.offset_y, 'Offset Y from center')
            hdu.header['DATE-OBS'] = (datetime.now().isoformat(), 'Date')
            hdu.header['CAMERA']   = ('XIMEA', 'Camera type')
            hdu.header['CAMSN']    = (self.serial, 'Camera serial number')
            hdu.header['DATAMIN']  = (int(gray.min()), 'Min pixel')
            hdu.header['DATAMAX']  = (int(gray.max()), 'Max pixel')
            hdu.header['DATAMEAN'] = (float(gray.mean()), 'Mean pixel')
            if centroid_raw is not None:
                xc, yc, fx, fy = centroid_raw
                hdu.header['CENT_X']  = (round(xc, 3), 'Centroid X (raw px)')
                hdu.header['CENT_Y']  = (round(yc, 3), 'Centroid Y (raw px)')
                hdu.header['FWHM_X']  = (round(fx, 3), 'FWHM X (raw px)')
                hdu.header['FWHM_Y']  = (round(fy, 3), 'FWHM Y (raw px)')
                hdu.header['FWHM_AV'] = (round((fx + fy) / 2, 3),
                                         'FWHM mean (raw px)')
            hdu.writeto(fname, overwrite=True)
        else:
            fname = os.path.join(save_dir, f"live_{timestamp}.png")
            cv2.imwrite(fname, gray)

        print(f"[CameraView] Saved: {fname}")
        self.status_var.set(f"Saved {os.path.basename(fname)}")
        if centroid_raw:
            xc, yc, fx, fy = centroid_raw
            print(f"  Centroid: Xc={xc:.2f}  Yc={yc:.2f}  "
                  f"FWHM_X={fx:.2f}  FWHM_Y={fy:.2f} px")


# ==============================================================================
# STANDALONE ENTRY POINT (lab convenience: preview without the acquisition GUI)
# ==============================================================================
def main():
    import argparse
    parser = argparse.ArgumentParser(description='XIMEA preview / pointing window')
    parser.add_argument('--serial', type=str, default='',
                        help='XIMEA serial number (default: first device)')
    parser.add_argument('--outdir', type=str, default='.',
                        help='Root output directory for live_frames/')
    parser.add_argument('--config', type=str,
                        default=os.path.join(_get_base_dir(), 'camera_config.json'))
    args = parser.parse_args()

    root = tk.Tk()
    root.withdraw()
    view = CameraView(root, serial=args.serial, config_file=args.config,
                      outdir=args.outdir, on_close=root.quit)
    if not view.open():
        root.destroy()
        sys.exit(1)
    root.mainloop()
    root.destroy()


if __name__ == '__main__':
    main()
