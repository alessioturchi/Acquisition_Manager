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
Camera Puntamento OCT - v3 GUI
Finestra unica: video + pannello slider/controlli
(macOS/Windows - AVFoundation o Ximea)

Uso:
  python camera_puntamento_mac_3.py          # auto-detect
  python camera_puntamento_mac_3.py --webcam # forza webcam
  python camera_puntamento_mac_3.py --ximea  # forza Ximea
"""

import cv2
import numpy as np
from datetime import datetime
import argparse
import json
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox

def _get_base_dir():
    """Directory base per config e file salvati.
    - Script diretto : directory del .py
    - App PyInstaller macOS: directory che contiene il .app
    - App PyInstaller Windows: directory dell'exe
    """
    if getattr(sys, 'frozen', False):
        exe = os.path.abspath(sys.executable)
        if sys.platform == 'darwin':
            # exe = .../CameraPuntamento.app/Contents/MacOS/nome_exe
            # saliamo 4 livelli per arrivare alla dir che contiene il .app
            return os.path.abspath(os.path.join(exe, '../../../..'))
        else:
            return os.path.dirname(exe)
    return os.path.dirname(os.path.abspath(__file__))


try:
    from PIL import Image, ImageTk
except ImportError:
    print("ERRORE: Pillow non installato. Esegui: pip install Pillow")
    sys.exit(1)

try:
    from ximea import xiapi
    XIMEA_AVAILABLE = True
except ImportError:
    XIMEA_AVAILABLE = False

try:
    from astropy.io import fits
    FITS_AVAILABLE = True
except ImportError:
    FITS_AVAILABLE = False

# ============================================================
# PARAMETRI
# ============================================================
XIMEA_EXPOSURE_STEP = 1000
XIMEA_GAIN_STEP = 0.5
OPENCV_EXPOSURE_STEP = 1
OPENCV_GAIN_STEP = 5
DEFAULT_CROSSHAIR_SIZE = 30
DEFAULT_CIRCLE_RADIUS = 100
MOVE_STEP = 5

BG = '#1e1e1e'
BG2 = '#2a2a2a'
FG = '#dddddd'
FG2 = '#888888'
GREEN = '#00cc44'
CYAN = '#00ccff'
ACCENT = '#3a3a3a'


class CameraPuntamentoGUI:
    def __init__(self, root, camera_index=0, config_file='camera_config.json', use_ximea=False):
        self.root = root
        self.root.title("Camera Puntamento OCT  v3")
        self.root.configure(bg=BG)

        self.camera_index = camera_index
        self.config_file = config_file

        # Camera
        self.cap = None
        self.use_ximea = use_ximea and XIMEA_AVAILABLE
        self.ximea_cam = None
        self.ximea_img = None
        self.ximea_exp_min = 110
        self.ximea_exp_max = 10_000_000
        self.ximea_gain_min = -4.0
        self.ximea_gain_max = 38.0
        self.gain_available = False
        self.exposure_available = False

        self.params = {'fps': 30, 'gain': 0.0, 'exposure': 10000.0}
        self.gain_step = OPENCV_GAIN_STEP
        self.exposure_step = OPENCV_EXPOSURE_STEP

        # Overlay state
        self.offset_x = 0
        self.offset_y = 0
        self.crosshair_size = DEFAULT_CROSSHAIR_SIZE
        self.circle_radius = DEFAULT_CIRCLE_RADIUS
        self.centroid_data = None

        # Thread
        self._frame_lock = threading.Lock()
        self._cam_settings_lock = threading.Lock()
        self._pending_cam_settings = {}   # applicate dal camera_loop tra un frame e l'altro
        self.current_raw_frame = None
        self.current_display_frame = None
        self.running = False
        self.paused = False
        self.frame_count = 0

        # Display size (set after camera init)
        self.cam_w = 640
        self.cam_h = 480

        self.load_config()
        self.setup_gui()

        # Init camera
        if not self.init_camera():
            messagebox.showerror("Errore Camera", "Impossibile aprire nessuna camera.")
            self.root.destroy()
            return

        self.configure_sliders_for_camera()

        self.running = True
        self.cam_thread = threading.Thread(target=self.camera_loop, daemon=True)
        self.cam_thread.start()
        self.root.after(33, self.update_display)

    # ------------------------------------------------------------------
    # CONFIG
    # ------------------------------------------------------------------
    def load_config(self):
        if not os.path.exists(self.config_file):
            return
        try:
            with open(self.config_file) as f:
                cfg = json.load(f)
            p = cfg.get('params', {})
            self.params['gain'] = p.get('gain', 0.0)
            self.params['exposure'] = p.get('exposure', 10000.0)
            o = cfg.get('overlay', {})
            self.crosshair_size = o.get('crosshair_size', DEFAULT_CROSSHAIR_SIZE)
            self.circle_radius = o.get('circle_radius', DEFAULT_CIRCLE_RADIUS)
            self.offset_x = o.get('offset_x', 0)
            self.offset_y = o.get('offset_y', 0)
        except Exception as e:
            print(f"Errore caricamento config: {e}")

    def save_config(self):
        try:
            cfg = {
                'camera_index': self.camera_index,
                'params': {'gain': self.params['gain'], 'exposure': self.params['exposure']},
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
            print(f"Config salvata in {self.config_file}")
        except Exception as e:
            print(f"Errore salvataggio config: {e}")

    # ------------------------------------------------------------------
    # GUI
    # ------------------------------------------------------------------
    def setup_gui(self):
        # Main row: canvas | controls
        main = tk.Frame(self.root, bg=BG)
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
        scrollbar = tk.Scrollbar(ctrl_outer, orient=tk.VERTICAL, command=ctrl_canvas.yview)
        ctrl_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        ctrl_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.ctrl = tk.Frame(ctrl_canvas, bg=BG2)
        _ctrl_win = ctrl_canvas.create_window((0, 0), window=self.ctrl, anchor='nw')
        self.ctrl.bind('<Configure>',
                       lambda e: ctrl_canvas.configure(scrollregion=ctrl_canvas.bbox('all')))
        ctrl_canvas.bind('<Configure>',
                         lambda e: ctrl_canvas.itemconfig(_ctrl_win, width=e.width))
        # Mousewheel scroll
        ctrl_canvas.bind_all('<MouseWheel>',
                             lambda e: ctrl_canvas.yview_scroll(-1 * (e.delta // 120), 'units'))

        self._build_controls()

        # --- STATUS BAR ---
        self.status_var = tk.StringVar(value="Inizializzazione...")
        tk.Label(self.root, textvariable=self.status_var, bg='#111111',
                 fg=CYAN, anchor='w', font=('Courier', 10), padx=6
                 ).pack(fill=tk.X, side=tk.BOTTOM)

        # Keyboard bindings
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
            ('i',       lambda e: self._move_offset(0, -MOVE_STEP)),
            ('m',       lambda e: self._move_offset(0, +MOVE_STEP)),
            ('j',       lambda e: self._move_offset(-MOVE_STEP, 0)),
            ('k',       lambda e: self._move_offset(+MOVE_STEP, 0)),
            ('q',       lambda e: self.quit()),
            ('<Escape>', lambda e: self.quit()),
        ]:
            self.root.bind(key, cb)

        self.root.protocol("WM_DELETE_WINDOW", self.quit)

    def _section(self, text):
        tk.Label(self.ctrl, text=text, bg=BG2, fg=FG2,
                 font=('Helvetica', 8, 'bold')).pack(fill=tk.X, padx=8, pady=(10, 0))
        tk.Frame(self.ctrl, bg='#444444', height=1).pack(fill=tk.X, padx=8, pady=(1, 4))

    def _mk_btn(self, text, command, bg_color, pady=3):
        """Pulsante Frame+Label: rispetta fg/bg su macOS (il tema Aqua ignora tk.Button)."""
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

        self.gain_var = tk.DoubleVar(value=self.params['gain'])
        self.gain_slider = self._slider("Gain", self.gain_var, -4, 38, 0.5,
                                        command=self.on_gain_change)

        exp_ms = self.params['exposure'] / 1000.0
        self.exp_var = tk.DoubleVar(value=round(exp_ms, 1))
        self.exp_slider = self._slider("Exposure (ms)", self.exp_var, 0.1, 500, 0.5,
                                       command=self.on_exposure_change)

        # --- OVERLAY ---
        self._section("OVERLAY")

        self.show_crosshair  = tk.BooleanVar(value=True)
        self.show_circle     = tk.BooleanVar(value=True)
        self.show_grid       = tk.BooleanVar(value=False)
        self.show_info       = tk.BooleanVar(value=True)
        self.show_centroid   = tk.BooleanVar(value=False)
        self.show_row_profile = tk.BooleanVar(value=False)
        self.show_col_profile = tk.BooleanVar(value=False)

        checks = [
            ("Reticolo  (x)", self.show_crosshair),
            ("Cerchio   (o)", self.show_circle),
            ("Griglia   (b)", self.show_grid),
            ("Info      (t)", self.show_info),
            ("Centroide (n)", self.show_centroid),
            ("Profilo riga (v)", self.show_row_profile),
            ("Profilo col. (g)", self.show_col_profile),
        ]
        for lbl, var in checks:
            tk.Checkbutton(self.ctrl, text=lbl, variable=var,
                           bg=BG2, fg=FG, selectcolor=ACCENT,
                           activebackground=BG2, activeforeground=FG,
                           font=('Helvetica', 9)).pack(anchor='w', padx=12)

        # --- POSIZIONE ---
        self._section("POSIZIONE RETICOLO")

        self.offset_x_var = tk.IntVar(value=self.offset_x)
        self.offset_x_slider = self._slider("Offset X", self.offset_x_var, -400, 400, 1,
                                             command=lambda v: setattr(self, 'offset_x', int(float(v))))

        self.offset_y_var = tk.IntVar(value=self.offset_y)
        self.offset_y_slider = self._slider("Offset Y", self.offset_y_var, -300, 300, 1,
                                             command=lambda v: setattr(self, 'offset_y', int(float(v))))

        tk.Button(self.ctrl, text="Reset Offset (r)", command=self.reset_offset,
                  bg=ACCENT, fg='white', font=('Helvetica', 9),
                  relief=tk.FLAT, padx=4,
                  highlightbackground=ACCENT).pack(fill=tk.X, padx=10, pady=(2, 4))

        # --- DIMENSIONI ---
        self._section("DIMENSIONI OVERLAY")

        self.crosshair_size_var = tk.IntVar(value=self.crosshair_size)
        self._slider("Dim. reticolo", self.crosshair_size_var, 10, 300, 5,
                     command=lambda v: setattr(self, 'crosshair_size', int(float(v))))

        self.circle_radius_var = tk.IntVar(value=self.circle_radius)
        self._slider("Raggio cerchio", self.circle_radius_var, 20, 500, 10,
                     command=lambda v: setattr(self, 'circle_radius', int(float(v))))

        # --- AZIONI ---
        self._section("AZIONI")

        self.pause_btn = self._mk_btn("⏸  Pausa   (spazio)", self.toggle_pause, '#3a3a5e')
        self._mk_btn("📷  Cattura FITS  (c)", self.capture,     '#1a4a7a')
        self._mk_btn("💾  Salva config  (s)", self.save_config,  '#1a5e30')
        self._mk_btn("✕   Esci         (q)", self.quit,          '#7a1a1a', pady=(3, 12))

    def configure_sliders_for_camera(self):
        """Adatta i range degli slider al tipo di camera effettivamente inizializzato."""
        if self.use_ximea:
            self.cam_label.config(text="Camera: XIMEA")
            self.gain_slider.config(from_=self.ximea_gain_min, to=self.ximea_gain_max,
                                    resolution=0.5)
            self.gain_var.set(round(self.params['gain'], 1))
            # Exposure in ms
            exp_ms = self.params['exposure'] / 1000.0
            self.exp_slider.config(from_=0.1, to=1000, resolution=0.5)
            self.exp_var.set(round(exp_ms, 1))
        else:
            self.cam_label.config(text="Camera: WEBCAM OpenCV")
            self.gain_slider.config(from_=0, to=100, resolution=1)
            self.gain_var.set(max(0, min(100, self.params['gain'])))
            if not self.gain_available:
                self.gain_slider.config(state=tk.DISABLED, label="[non disponibile]")
            self.exp_slider.config(from_=-10, to=0, resolution=1)
            self.exp_var.set(max(-10, min(0, self.params['exposure'])))
            if not self.exposure_available:
                self.exp_slider.config(state=tk.DISABLED, label="[non disponibile]")

        # Range offset basato sulla risoluzione reale della camera
        hx = self.cam_w // 2
        hy = self.cam_h // 2
        self.offset_x_slider.config(from_=-hx, to=hx)
        self.offset_y_slider.config(from_=-hy, to=hy)
        self.offset_x_var.set(max(-hx, min(hx, self.offset_x)))
        self.offset_y_var.set(max(-hy, min(hy, self.offset_y)))

    # ------------------------------------------------------------------
    # CAMERA INIT
    # ------------------------------------------------------------------
    def init_camera(self):
        if self.use_ximea:
            if self._init_ximea():
                return True
            print("Ximea non trovata - fallback a webcam OpenCV...")
            self.use_ximea = False
        return self._init_opencv()

    def _init_ximea(self):
        try:
            self.ximea_cam = xiapi.Camera()
            self.ximea_cam.open_device()
            self.ximea_img = xiapi.Image()
            try:
                self.ximea_cam.set_imgdataformat('XI_MONO16')
            except Exception:
                pass
            self.ximea_exp_min = self.ximea_cam.get_exposure_minimum()
            self.ximea_exp_max = self.ximea_cam.get_exposure_maximum()
            self.ximea_gain_min = self.ximea_cam.get_gain_minimum()
            self.ximea_gain_max = self.ximea_cam.get_gain_maximum()

            exp = max(self.ximea_exp_min, min(self.ximea_exp_max, int(self.params['exposure'])))
            self.ximea_cam.set_exposure(exp)
            gain = max(self.ximea_gain_min, min(self.ximea_gain_max, self.params['gain']))
            self.ximea_cam.set_gain(gain)

            self.cam_w = self.ximea_cam.get_width()
            self.cam_h = self.ximea_cam.get_height()
            self.gain_available = True
            self.exposure_available = True
            self.ximea_cam.start_acquisition()
            print(f"Ximea: {self.ximea_cam.get_device_name().decode()}  {self.cam_w}x{self.cam_h}")
            return True
        except Exception as e:
            print(f"Ximea errore: {e}")
            return False

    def _init_opencv(self):
        backend = cv2.CAP_AVFOUNDATION if sys.platform == 'darwin' else cv2.CAP_DSHOW
        self.cap = cv2.VideoCapture(self.camera_index, backend)
        if not self.cap.isOpened():
            return False
        self.cap.set(cv2.CAP_PROP_FPS, self.params['fps'])
        self.cam_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.cam_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self._check_opencv_controls()
        # Leggi i valori reali dalla camera (possono differire dai default)
        self.params['gain'] = self.cap.get(cv2.CAP_PROP_GAIN)
        self.params['exposure'] = self.cap.get(cv2.CAP_PROP_EXPOSURE)
        print(f"OpenCV webcam: {self.cam_w}x{self.cam_h}  "
              f"gain={'OK' if self.gain_available else 'RO'}  "
              f"exp={'OK' if self.exposure_available else 'RO'}")
        return True

    def _check_opencv_controls(self):
        og = self.cap.get(cv2.CAP_PROP_GAIN)
        self.cap.set(cv2.CAP_PROP_GAIN, og + 1)
        self.gain_available = self.cap.get(cv2.CAP_PROP_GAIN) != og
        self.cap.set(cv2.CAP_PROP_GAIN, og)

        oe = self.cap.get(cv2.CAP_PROP_EXPOSURE)
        self.cap.set(cv2.CAP_PROP_EXPOSURE, oe + 1)
        self.exposure_available = self.cap.get(cv2.CAP_PROP_EXPOSURE) != oe
        self.cap.set(cv2.CAP_PROP_EXPOSURE, oe)

    # ------------------------------------------------------------------
    # CAMERA LOOP (background thread)
    # ------------------------------------------------------------------
    def camera_loop(self):
        while self.running:
            if self.paused:
                time.sleep(0.033)
                continue
            if self.use_ximea:
                try:
                    self.ximea_cam.get_image(self.ximea_img)
                    raw = self.ximea_img.get_image_data_numpy().copy()
                    if raw.dtype == np.uint16:
                        disp8 = (raw / 16).astype(np.uint8)
                    else:
                        disp8 = raw
                    if len(disp8.shape) == 2:
                        frame_bgr = cv2.cvtColor(disp8, cv2.COLOR_GRAY2BGR)
                    else:
                        frame_bgr = disp8
                    with self._frame_lock:
                        self.current_raw_frame = raw.copy()
                        self.current_display_frame = frame_bgr.copy()
                    self.frame_count += 1
                except Exception as e:
                    print(f"ERRORE Ximea: {e}")
                    time.sleep(0.1)
            else:
                # Applica eventuali modifiche gain/exposure richieste dalla GUI
                with self._cam_settings_lock:
                    pending = self._pending_cam_settings.copy()
                    self._pending_cam_settings.clear()
                if 'gain' in pending:
                    self.cap.set(cv2.CAP_PROP_GAIN, pending['gain'])
                    self.params['gain'] = self.cap.get(cv2.CAP_PROP_GAIN)
                if 'exposure' in pending:
                    self.cap.set(cv2.CAP_PROP_EXPOSURE, pending['exposure'])
                    self.params['exposure'] = self.cap.get(cv2.CAP_PROP_EXPOSURE)

                ret, frame = self.cap.read()
                if ret:
                    with self._frame_lock:
                        self.current_raw_frame = frame.copy()
                        self.current_display_frame = frame.copy()
                    self.frame_count += 1
                else:
                    time.sleep(0.033)

    # ------------------------------------------------------------------
    # DISPLAY (main thread)
    # ------------------------------------------------------------------
    def update_display(self):
        if not self.running:
            return
        with self._frame_lock:
            frame = self.current_display_frame.copy() if self.current_display_frame is not None else None
            raw = self.current_raw_frame.copy() if self.current_raw_frame is not None else None

        if frame is not None:
            # Centroide
            if self.show_centroid.get():
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                self.centroid_data = self.compute_centroid(gray)
            else:
                self.centroid_data = None

            # Overlay
            self.draw_overlay(frame, raw)

            # Ridimensiona al canvas
            cw = self.canvas.winfo_width()
            ch = self.canvas.winfo_height()
            if cw > 1 and ch > 1:
                frame = self._fit_frame(frame, cw, ch)
                if self.show_grid.get():
                    self._draw_grid(frame)

            # Converti e mostra
            img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            self.photo_image = ImageTk.PhotoImage(image=img)
            self.canvas.create_image(0, 0, anchor=tk.NW, image=self.photo_image)

            self._update_status()

        self.root.after(33, self.update_display)

    def _fit_frame(self, frame, cw, ch):
        h, w = frame.shape[:2]
        scale = min(cw / w, ch / h)
        nw, nh = int(w * scale), int(h * scale)
        resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
        canvas_img = np.zeros((ch, cw, 3), dtype=np.uint8)
        ox, oy = (cw - nw) // 2, (ch - nh) // 2
        canvas_img[oy:oy+nh, ox:ox+nw] = resized
        self._fit_scale = scale
        self._fit_ox = ox
        self._fit_oy = oy
        return canvas_img

    def _draw_grid(self, frame):
        """Griglia disegnata a risoluzione display per evitare aliasing da resize."""
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
                f"Centroide — Xc={xc:.1f}  Yc={yc:.1f}  "
                f"FWHM_X={fx:.1f}  FWHM_Y={fy:.1f}  px  |  Frame #{self.frame_count}"
            )
        else:
            cam = "Ximea" if self.use_ximea else "OpenCV"
            status = "PAUSA" if self.paused else "live"
            self.status_var.set(
                f"{cam} — {self.cam_w}x{self.cam_h}  [{status}]  |  Frame #{self.frame_count}"
            )

    # --------------------  ----------------------------------------------
    # OVERLAY (stesso algoritmo di v2)
    # ------------------------------------------------------------------
    def draw_overlay(self, frame, raw=None):
        h, w = frame.shape[:2]
        cx = w // 2 + self.offset_x
        cy = h // 2 + self.offset_y

        gray_clean = None
        if self.show_row_profile.get() or self.show_col_profile.get():
            gray_clean = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

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
            cv2.line(frame, (xci-18, yci), (xci+18, yci), (0, 255, 255), 2)
            cv2.line(frame, (xci, yci-18), (xci, yci+18), (0, 255, 255), 2)
            cv2.circle(frame, (xci, yci), 5, (0, 255, 255), 1)
            ax = (max(1, int(fwx/2)), max(1, int(fwy/2)))
            cv2.ellipse(frame, (xci, yci), ax, 0, 0, 360, (0, 200, 255), 1)

        if self.show_info.get():
            # Statistiche dal frame RAW
            if raw is not None:
                rg = raw if len(raw.shape) == 2 else cv2.cvtColor(raw, cv2.COLOR_BGR2GRAY)
                pmin, pmax, pmean = int(rg.min()), int(rg.max()), rg.mean()
            else:
                rg = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                pmin, pmax, pmean = int(rg.min()), int(rg.max()), rg.mean()

            lines = [
                f"Frame: {self.frame_count}" + (" [PAUSA]" if self.paused else ""),
                f"Gain: {self.params['gain']:.1f}" + (" dB" if self.use_ximea else ""),
                f"Exp:  {self.params['exposure']:.0f}" + (" us" if self.use_ximea else ""),
                f"I: {pmin}/{pmean:.0f}/{pmax}",
            ]
            if self.show_centroid.get() and self.centroid_data is not None:
                xc_f, yc_f, fwx, fwy = self.centroid_data
                lines += [f"Xc={xc_f:.1f}  Yc={yc_f:.1f}",
                          f"FWx={fwx:.1f} FWy={fwy:.1f}"]

            iy = 26
            for line in lines:
                col = (0, 255, 255) if line.startswith("Xc") or line.startswith("FW") else (0, 255, 0)
                cv2.putText(frame, line, (8, iy), cv2.FONT_HERSHEY_SIMPLEX, 0.7, col, 2)
                iy += 28

            cv2.putText(frame, f"Centro: ({cx},{cy})",
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

    # ------------------------------------------------------------------
    # CENTROIDE
    # ------------------------------------------------------------------
    def compute_centroid(self, gray):
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

    # ------------------------------------------------------------------
    # SLIDER CALLBACKS
    # ------------------------------------------------------------------
    def on_gain_change(self, val):
        v = float(val)
        if self.use_ximea and self.ximea_cam:
            # Ximea: accesso diretto (thread dedicato all'acquisizione Ximea è bloccante)
            try:
                v = max(self.ximea_gain_min, min(self.ximea_gain_max, v))
                self.ximea_cam.set_gain(v)
                self.params['gain'] = self.ximea_cam.get_gain()
            except Exception:
                pass
        elif self.cap and self.gain_available:
            # OpenCV: usa pending dict per applicare nel camera_loop (thread-safe)
            with self._cam_settings_lock:
                self._pending_cam_settings['gain'] = v

    def on_exposure_change(self, val):
        v = float(val)
        if self.use_ximea and self.ximea_cam:
            us = int(v * 1000)
            us = max(self.ximea_exp_min, min(self.ximea_exp_max, us))
            try:
                self.ximea_cam.set_exposure(us)
                self.params['exposure'] = self.ximea_cam.get_exposure()
            except Exception:
                pass
        elif self.cap and self.exposure_available:
            with self._cam_settings_lock:
                self._pending_cam_settings['exposure'] = v

    # ------------------------------------------------------------------
    # ACTIONS
    # ------------------------------------------------------------------
    def toggle_pause(self):
        self.paused = not self.paused
        self.pause_btn.config(
            text="▶  Riprendi (spazio)" if self.paused else "⏸  Pausa   (spazio)"
        )

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

    def capture(self):
        with self._frame_lock:
            raw = self.current_raw_frame.copy() if self.current_raw_frame is not None else None
        if raw is None:
            return
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        gray = raw if len(raw.shape) == 2 else cv2.cvtColor(raw, cv2.COLOR_BGR2GRAY)
        centroid_raw = self.compute_centroid(gray)

        if FITS_AVAILABLE:
            fname = os.path.join(_get_base_dir(), f"puntamento_{timestamp}.fits")
            h_img, w_img = gray.shape
            hdu = fits.PrimaryHDU(gray)
            hdu.header['GAIN']     = (self.params['gain'],     'Camera gain')
            hdu.header['EXPOSURE'] = (self.params['exposure'], 'Exposure (us for Ximea)')
            hdu.header['CENTERX']  = (w_img//2 + self.offset_x, 'Reticolo center X')
            hdu.header['CENTERY']  = (h_img//2 + self.offset_y, 'Reticolo center Y')
            hdu.header['OFFSET_X'] = (self.offset_x, 'Offset X from center')
            hdu.header['OFFSET_Y'] = (self.offset_y, 'Offset Y from center')
            hdu.header['DATE-OBS'] = (datetime.now().isoformat(), 'Date')
            hdu.header['CAMERA']   = ('Ximea' if self.use_ximea else 'OpenCV', 'Camera type')
            hdu.header['BITDEPTH'] = (12 if gray.dtype == np.uint16 else 8, 'Bit depth')
            hdu.header['DATAMIN']  = (int(gray.min()), 'Min pixel')
            hdu.header['DATAMAX']  = (int(gray.max()), 'Max pixel')
            hdu.header['DATAMEAN'] = (float(gray.mean()), 'Mean pixel')
            if centroid_raw is not None:
                xc, yc, fx, fy = centroid_raw
                hdu.header['CENT_X']  = (round(xc, 3), 'Centroid X (raw px)')
                hdu.header['CENT_Y']  = (round(yc, 3), 'Centroid Y (raw px)')
                hdu.header['FWHM_X']  = (round(fx, 3), 'FWHM X (raw px)')
                hdu.header['FWHM_Y']  = (round(fy, 3), 'FWHM Y (raw px)')
                hdu.header['FWHM_AV'] = (round((fx+fy)/2, 3), 'FWHM mean (raw px)')
            hdu.writeto(fname, overwrite=True)
        else:
            fname = os.path.join(_get_base_dir(), f"puntamento_{timestamp}.png")
            cv2.imwrite(fname, gray)

        print(f"Salvato: {fname}")
        if centroid_raw:
            xc, yc, fx, fy = centroid_raw
            print(f"  Centroide: Xc={xc:.2f}  Yc={yc:.2f}  FWHM_X={fx:.2f}  FWHM_Y={fy:.2f} px")

    def quit(self):
        self.running = False
        time.sleep(0.05)
        if self.use_ximea and self.ximea_cam:
            try:
                self.ximea_cam.stop_acquisition()
                self.ximea_cam.close_device()
            except Exception:
                pass
        elif self.cap:
            self.cap.release()
        self.root.destroy()


# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description='Camera Puntamento OCT v3 - GUI')
    parser.add_argument('--camera', type=int, default=0)
    parser.add_argument('--config', type=str,
                        default=os.path.join(_get_base_dir(), 'camera_config.json'))
    parser.add_argument('--ximea',  action='store_true', help='Forza Ximea')
    parser.add_argument('--webcam', action='store_true', help='Forza webcam')
    args = parser.parse_args()

    if args.webcam:
        use_ximea = False
    else:
        use_ximea = XIMEA_AVAILABLE

    root = tk.Tk()
    root.geometry("1100x680")
    root.minsize(800, 500)

    CameraPuntamentoGUI(root, camera_index=args.camera,
                        config_file=args.config, use_ximea=use_ximea)
    root.mainloop()


if __name__ == '__main__':
    main()
