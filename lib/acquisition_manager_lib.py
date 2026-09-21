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
acquisition_manager_lib.py
--------------------
Headless library version of the Acquisition Manager GUI.
Exposes three public functions driven by a configuration dictionary:

    run_acquisition(cfg)   – camera acquisition → FITS datacubes
    run_extraction(cfg)    – spectral extraction → FITS datacube
    run_analysis(cfg)      – flat-field, normalisation, FFT

All parameters previously set via the GUI are passed in a single dict `cfg`.
No tkinter dependency; matplotlib is used only for diagnostic plots.

Original code by Monica Rainer (2023).
Modified by Alessio Turchi (2026).
Library refactoring: March 2026 - Alessio Turchi.
"""

import os
import gc
import sys
import json
import unicodedata
import numpy as np
from astropy.io import fits
from datetime import datetime

from ximea import xiapi

# Use non-interactive backend: all figures are saved to disk, never displayed
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ==============================================================================
# DEFAULT CONFIGURATION
# ==============================================================================
DEFAULT_CFG = {
    # ---- Acquisition ----
    "serial":    "28720523",          # camera serial number
    "bin_bit":   16,                  # pixel bit depth (8 or 16)
    "texp_ms":   100.0,               # single exposure time (MILLISECONDS)
    "texp":      0.1,                 # single exposure time (seconds, legacy)
    "gain":      0.0,                 # camera gain (dB)
    "sequence":  10000,               # total frames to acquire
    "subset":    100,                 # frames per FITS output file
    "position":  1750,                # spectral centroid column
    "auto_position": 1,               # 1 = detect the column on frame 0
    "halfwidth": 250,                 # half-width of image crop (pixels)
    "outdir":    r"C:\at\DocOss\lab2023\spectra",  # root output directory

    # ---- Extraction ----
    "indir":     "",                  # directory with raw FITS files
    "suffix":    "_datacube.fit",     # output filename suffix
    "width":     50,                  # extraction window full width (pixels)
    "ave_chunk": 0,                   # temporal binning size (0 = disabled)
    "dark_sub":  0,                   # 1 = subtract local dark
    "trim_low":  10,                  # pixels to trim from spectrum bottom
    "trim_up":   10,                  # pixels to trim from spectrum top

    # ---- Analysis ----
    "datacube":  "",                  # path to extracted FITS datacube
    "flat":      100,                 # spectra used as flat-field reference
    "chunk":     5000,                # spectra for first averaged block
    "cuts_low":  0.0,                 # lower waterfall display cut (0 = auto)
    "cuts_high": 0.0,                 # upper waterfall display cut (0 = auto)
    "poly":      0,                   # polynomial degree for normalisation
    "start1":    0,                   # block 2 start index (0 = after flat)
    "end1":      0,                   # block 2 end index   (0 = use chunk)
    "start2":    0,                   # block 3 start index (0 = after block 2)
    "end2":      0,                   # block 3 end index   (0 = end of cube)
    "savextract": 0,                  # 1 = save all spectra as ASCII
    "saveave":   0,                   # 1 = save averaged blocks as ASCII
    "fourier":   0,                   # 1 = compute and save FFT
}


def _get(cfg, key):
    """Return cfg[key] if present, else DEFAULT_CFG[key]."""
    return cfg.get(key, DEFAULT_CFG[key])


def default_output_dir() -> str:
    """
    Return the current user's Desktop folder, portable across Windows/Linux/macOS.

    - Windows: read the 'Desktop' entry of the per-user Shell Folders registry
      key, so that OneDrive-redirected or localized desktops are honoured.
    - Linux: read XDG_DESKTOP_DIR from user-dirs.dirs (e.g. ~/Scrivania on an
      Italian locale).
    - Fallback: ~/Desktop if it exists, otherwise the home directory.
    """
    home = os.path.expanduser("~")
    desktop = None
    try:
        if sys.platform.startswith("win"):
            import winreg
            key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                desktop = os.path.expandvars(winreg.QueryValueEx(k, "Desktop")[0])
        else:
            cfg_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
            with open(os.path.join(cfg_home, "user-dirs.dirs"), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("XDG_DESKTOP_DIR="):
                        val = line.split("=", 1)[1].strip().strip('"')
                        desktop = val.replace("$HOME", home)
                        break
    except Exception:
        desktop = None      # registry key / user-dirs.dirs missing: use fallback
    if desktop and os.path.isdir(desktop):
        return os.path.normpath(desktop)
    fallback = os.path.join(home, "Desktop")
    return fallback if os.path.isdir(fallback) else home

import logging

log = logging.getLogger(__name__)


# ------------------------------------------------------------------------------
# FITS header extension (metadata appended AFTER the legacy keywords)
# ------------------------------------------------------------------------------
FITS_META_VERSION = 1


def fits_text(text) -> str:
    """FITS headers are ASCII-only: strip accents, flatten newlines to ' | '."""
    text = str(text or "")
    for src, dst in (("\u00b5", "u"), ("\u03bc", "u"), ("\u00b0", "deg"),
                     ("\u00b1", "+/-")):
        text = text.replace(src, dst)   # symbols that NFKD would silently drop
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return " | ".join(lines)


def parse_frd(text):
    """Return the FRD distance as float, or None if empty.  Raises ValueError."""
    text = str(text or "").strip()
    if not text:
        return None
    value = float(text)
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("FRD distance must be finite")   # not writable in FITS
    return value


def fits_meta_cards(meta) -> list:
    """
    Build the extension header cards as a list of (keyword, value, comment).

    Only the entries present in `meta` are written:
      config    : dict -> AMCONFIG, full GUI configuration as JSON (long string,
                  FITS CONTINUE convention); read back with
                  json.loads(header['AMCONFIG'])
      acq_notes : str  -> ACQNOTES
      cam_notes : str  -> CAMNOTES
      frd_dist  : float or None -> FRD_DIST (omitted when None)
    AMCFGVER (metadata version) is always written when meta is non-empty.
    """
    if not meta:
        return []
    cards = [("AMCFGVER", FITS_META_VERSION, "Acquisition Manager metadata version")]
    if "config" in meta:
        cards.append(("AMCONFIG",
                      json.dumps(meta["config"], ensure_ascii=True,
                                 separators=(",", ":")),
                      "GUI configuration (JSON)"))
    if "acq_notes" in meta:
        cards.append(("ACQNOTES", fits_text(meta["acq_notes"]), "Acquisition notes"))
    if "cam_notes" in meta:
        cards.append(("CAMNOTES", fits_text(meta["cam_notes"]), "Camera notes"))
    if meta.get("frd_dist") is not None:
        cards.append(("FRD_DIST", float(meta["frd_dist"]), "FRD-analysis: distance"))
    return cards

def setup_file_logging(outdir: str) -> None:
    """Configure root logger to write INFO+ to outdir/pipeline.log."""
    os.makedirs(outdir, exist_ok=True)
    log_path = os.path.join(outdir, "pipeline.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_path, mode='a', encoding='utf-8'),
            logging.StreamHandler(),
        ]
    )
    log.info(f"Logging to: {log_path}")


# ==============================================================================
# PUBLIC FUNCTIONS
# ==============================================================================

def define_pos(self, spectra):
    """
    Estimate the spectral centroid as the mean of the peak pixel
    in the first and last frame. Not currently called in acquisition().
    """
    pix1 = np.argmax(spectra[0])
    pix2 = np.argmax(spectra[-1])
    return int((pix1 + pix2) / 2)

def run_acquisition(cfg: dict) -> str:
    """
    Camera acquisition loop.

    Acquires `sequence` frames from the XIMEA camera identified by `serial`.
    Every `subset` raw frames are averaged and written to a FITS file inside
    a timestamped subdirectory of `outdir`.

    Exposure is given in milliseconds via `texp_ms`.  Configurations written
    before the switch to milliseconds are still accepted through the legacy
    `texp` key (seconds).

    The spectral column used for cropping depends on `auto_position`:
      1 (default) : detected on frame 0 as argmax of the column-summed frame,
                    with `position` used as a fallback if the detection lands
                    too close to the frame edge;
      0           : `position` is used as given (e.g. set from the live
                    preview window), and the frame-0 detection is only logged.

    Returns
    -------
    path_name : str
        Path to the timestamped output directory (empty string on failure).
    """
    serial    = str(_get(cfg, "serial"))
    bin_bit   = int(_get(cfg, "bin_bit"))
    sequence  = int(_get(cfg, "sequence"))
    subset    = int(_get(cfg, "subset"))
    position  = int(_get(cfg, "position"))
    halfwidth = int(_get(cfg, "halfwidth"))
    outdir    = str(_get(cfg, "outdir"))
    gain      = float(_get(cfg, "gain"))
    auto_position = int(_get(cfg, "auto_position"))

    # Exposure: texp_ms is authoritative, "texp" (seconds) is the legacy key.
    # Resolved explicitly rather than through _get() so that a config carrying
    # only one of the two never silently falls back to the default value.
    if "texp_ms" in cfg:
        texp_ms = float(cfg["texp_ms"])
    elif "texp" in cfg:
        texp_ms = float(cfg["texp"]) * 1000.0
        log.info("[acquisition] Legacy 'texp' key found, converted to "
                 f"{texp_ms:.1f} ms")
    else:
        texp_ms = float(DEFAULT_CFG["texp_ms"])
    texp = texp_ms / 1000.0          # seconds, kept for the FITS headers

    # --- Parameter validation ---
    if subset <= 0 or sequence <= 0:
        log.error(f"[acquisition] Invalid parameters: sequence={sequence}, subset={subset}")
        return ""
    if sequence < subset:
        log.error(f"[acquisition] sequence ({sequence}) < subset ({subset}): no file would be written")
        return ""
    if texp_ms <= 0:
        log.error(f"[acquisition] Invalid exposure time: {texp_ms} ms")
        return ""

    log.info(f"[acquisition] Starting: serial={serial}, texp={texp_ms:.1f} ms, "
             f"gain={gain:.1f} dB, sequence={sequence}, subset={subset}, "
             f"auto_position={auto_position}, outdir={outdir}")

    # BUG FIX B: correct number of frames to acquire (multiple of subset)
    n_subsets    = sequence // subset
    use_sequence = n_subsets * subset
    log.info(f"[acquisition] Adjusted sequence to {use_sequence} frames "
             f"({n_subsets} subsets * {subset} frames/subset)")

    cam = xiapi.Camera()
    path_name = ""

    try:
        # --- Camera setup ---
        try:
            cam.open_device_by_SN(serial)
            log.info(f"[acquisition] Camera opened (SN={serial})")
        except Exception as e:
            log.error(f"[acquisition] Cannot open camera SN={serial}: {e}")
            return ""

        cam.set_imgdataformat('XI_RAW16')

        texp_us = texp_ms * 1000.0
        tout    = max(int(1.5 * texp_ms), 1000)   # ms; at least 1 s
        log.info(f"[acquisition] texp={texp_ms:.1f} ms ({texp_us:.0f} micros), "
                 f"get_image timeout={tout} ms")

        try:
            cam.set_exposure(texp_us)
            actual_texp_us = float(cam.get_exposure())
            if abs(actual_texp_us - texp_us) > 1.0:
                log.warning(f"[acquisition] Camera clamped the exposure to "
                            f"{actual_texp_us / 1000.0:.3f} ms "
                            f"(requested {texp_ms:.3f} ms)")
            texp_ms = actual_texp_us / 1000.0
            texp    = texp_ms / 1000.0
        except Exception as e:
            log.error(f"[acquisition] set_exposure failed: {e}")
            return ""

        # Gain is not fatal: log and carry on with whatever the camera reports,
        # so that the value written in the FITS header is the real one.
        try:
            cam.set_gain(gain)
            actual_gain = float(cam.get_gain())
            if abs(actual_gain - gain) > 0.01:
                log.warning(f"[acquisition] Camera clamped the gain to "
                            f"{actual_gain:.2f} dB (requested {gain:.2f} dB)")
            gain = actual_gain
        except Exception as e:
            log.warning(f"[acquisition] set_gain failed ({e}); "
                        "the camera keeps its current gain")
            try:
                gain = float(cam.get_gain())
            except Exception:
                gain = float('nan')

        # --- Output directory ---
        now       = datetime.now()
        root_name = now.strftime('%Y-%m-%d_%H-%M-%S')
        path_name = os.path.join(outdir, root_name)
        use_dtype = 'uint' + str(bin_bit)
        try:
            os.makedirs(path_name, exist_ok=True)
            log.info(f"[acquisition] Output directory: {path_name}")
        except OSError as e:
            log.error(f"[acquisition] Cannot create output directory {path_name}: {e}")
            return ""

        img = xiapi.Image()
        cam.start_acquisition()
        log.info("[acquisition] Acquisition started")

        # --- Acquisition loop ---
        xmin = xmax = None
        datacube_subset = []
        hea = None
        n_fits_written  = 0
        n_errors        = 0

        for i in range(use_sequence):
            # Get frame
            try:
                cam.get_image(img, timeout=tout)
                data = img.get_image_data_numpy()
            except Exception as e:
                n_errors += 1
                log.warning(f"[acquisition] Frame {i}: get_image error ({n_errors} total): {e}")
                if n_errors > 10:
                    log.error("[acquisition] Too many frame errors, aborting loop")
                    break
                continue

            # Frame 0: detect centroid and define crop window
            if i == 0:
                log.info(f"[acquisition] Frame 0 shape: {data.shape}, dtype: {data.dtype}")
                data_flat = np.sum(data, axis=0)
                new_pos   = int(np.argmax(data_flat))

                if auto_position:
                    log.info(f"[acquisition] Detected centroid at pixel {new_pos} "
                             f"(fallback was {position})")
                    if new_pos > halfwidth:
                        position = new_pos
                    else:
                        log.warning(f"[acquisition] Centroid {new_pos} <= halfwidth {halfwidth}, "
                                    f"using fallback position {position}")
                else:
                    # Manual mode: the configured column wins, but log the
                    # disagreement so a bad pointing is visible in pipeline.log
                    log.info(f"[acquisition] Auto position disabled: using "
                             f"position={position} (frame-0 argmax was {new_pos})")
                    if abs(new_pos - position) > halfwidth:
                        log.warning(f"[acquisition] Frame-0 argmax {new_pos} lies "
                                    f"outside the crop window around {position}: "
                                    "check the pointing")

                xmin = position - halfwidth
                xmax = position + halfwidth
                log.info(f"[acquisition] Crop window: [{xmin}, {xmax}] "
                         f"(width={xmax - xmin} px)")

                # Safety check: crop window must be within frame bounds
                if xmin < 0 or xmax > data.shape[1]:
                    log.error(f"[acquisition] Crop window [{xmin},{xmax}] out of "
                              f"frame width {data.shape[1]}. Aborting.")
                    return path_name

                # Build FITS header once
                hea = fits.Header()
                hea['CROP_MIN'] = (xmin,  'Start of cropped area')
                hea['CROP_MAX'] = (xmax,  'End of cropped area')
                hea['TEXP']     = (texp,  'Exposure time of single frame (s)')
                hea['TEXP_MS']  = (texp_ms, 'Exposure time of single frame (ms)')
                hea['GAIN']     = (gain,  'Camera gain (dB)')
                hea['POSITION'] = (position, 'Spectral column used for cropping')
                hea['POSMODE']  = ('AUTO' if auto_position else 'MANUAL',
                                   'How the spectral column was chosen')
                hea['ARGMAX0']  = (new_pos, 'Frame-0 argmax column')
                hea['SUBSET']   = (subset,'Raw frames averaged per FITS entry')
                hea['TEXP_SUB'] = (texp * subset,
                                   'Effective exposure of each FITS entry (s)')
                # Extension keywords, appended after the legacy ones
                for key, val, com in fits_meta_cards(cfg.get("fits_meta")):
                    hea[key] = (val, com)

            cropped = data[:, xmin:xmax]
            datacube_subset.append(cropped)

            # BUG FIX A+C: write when subset is full, then reset
            if len(datacube_subset) == subset:
                subset_idx = i // subset          # 0-based subset counter
                fitsname   = os.path.join(
                    path_name,
                    f"{root_name}_spec_{str(subset_idx).zfill(len(str(n_subsets)))}.fits")
                try:
                    arr      = np.asarray(datacube_subset, dtype=use_dtype)
                    averaged = np.mean(arr, axis=0)[np.newaxis, ...]  # shape (1,H,W)
                    fits.PrimaryHDU(data=averaged, header=hea).writeto(
                        fitsname, overwrite=True)
                    n_fits_written += 1
                    log.info(f"[acquisition] Written subset {subset_idx+1}/{n_subsets}: "
                             f"{os.path.basename(fitsname)}")
                except Exception as e:
                    log.error(f"[acquisition] Failed to write {fitsname}: {e}")
                datacube_subset = []   # reset for next subset

        # --- Sanity check ---
        if n_fits_written == 0:
            log.error("[acquisition] No FITS files were written! "
                      "Check camera connection, crop window, and output path.")
        else:
            log.info(f"[acquisition] Done. {n_fits_written}/{n_subsets} files written "
                     f"to {path_name}")
        if datacube_subset:
            log.warning(f"[acquisition] {len(datacube_subset)} leftover frames "
                        f"(< 1 subset) were discarded")

    except Exception as e:
        log.exception(f"[acquisition] Unexpected error: {e}")

    finally:
        try:
            cam.stop_acquisition()
            cam.close_device()
            log.info("[acquisition] Camera closed")
        except Exception as e:
            log.warning(f"[acquisition] Error closing camera: {e}")

    return path_name

def run_acquisition_old(cfg: dict) -> str:
    """
    Camera acquisition loop.

    Acquires `sequence` frames from the XIMEA camera identified by `serial`.
    Every `subset` raw frames are averaged and written to a FITS file inside
    a timestamped subdirectory of `outdir`.

    FITS header keywords: CROP_MIN, CROP_MAX, TEXP, SUBSET, TEXP_SUB.

    Parameters
    ----------
    cfg : dict
        Configuration dictionary. Recognised keys (all optional; fall back to
        DEFAULT_CFG): serial, bin_bit, texp, sequence, subset, position,
        halfwidth, outdir.

    Returns
    -------
    path_name : str
        Path to the timestamped output directory containing all FITS files.
    """
    serial    = str(_get(cfg, "serial"))
    bin_bit   = int(_get(cfg, "bin_bit"))
    texp      = float(_get(cfg, "texp"))
    sequence  = int(_get(cfg, "sequence"))
    subset    = int(_get(cfg, "subset"))
    position  = int(_get(cfg, "position"))
    halfwidth = int(_get(cfg, "halfwidth"))
    outdir    = str(_get(cfg, "outdir"))

    cam = xiapi.Camera()
    cam.open_device_by_SN(serial)
    cam.set_imgdataformat('XI_RAW16')

    texp_us = texp * 1_000_000                             # seconds → microseconds
    tout = int(0.001 * texp_us + min(0.5 * 0.001 * texp_us, 1000))     # ms timeout
    tout = max(tout, 1000)

    cam.set_exposure(texp_us)
    img = xiapi.Image()
    cam.start_acquisition()

    # Timestamped output directory
    now = datetime.now()
    root_name = now.strftime('%Y-%m-%d_%H-%M-%S')
    path_name = os.path.join(outdir, root_name)
    use_dtype = 'uint' + str(bin_bit)
    os.makedirs(path_name, exist_ok=True)

    # Align sequence to a subset boundary to avoid incomplete trailing files
    remainder = sequence % subset
    use_sequence = sequence + 1 if remainder == 0 else sequence - remainder + 1

    datacube_subset = []        # accumulates raw frames within each subset
    fitsname = None             # current FITS output filename
    hea = None                  # FITS header (built on first frame)

    for i in range(use_sequence):
        cam.get_image(img, timeout=tout)
        data = img.get_image_data_numpy()

        if i == 0:
            # Auto-detect spectral centroid from the first frame
            data_flat = np.sum(data, axis=0)
            new_pos = np.argmax(data_flat)
            if new_pos > halfwidth:
                position = int(new_pos)
            xmin = position - halfwidth
            xmax = position + halfwidth

        cropped = data[:, xmin:xmax]
        nexp = i % subset # index within current subset

        if nexp == 0:
            if i == 0:
                # First frame of the very first subset: initialize
                datacube_subset = [cropped]
                datacube = np.zeros((1, cropped.shape[0], cropped.shape[1]),
                                    dtype=np.float64)
                hea = fits.Header()
                hea['CROP_MIN'] = (xmin, 'Start of cropped area')
                hea['CROP_MAX'] = (xmax, 'End of cropped area')
                hea['TEXP']     = (texp, 'Exposure time of single frame (s)')
                hea['SUBSET']   = (subset, 'Raw frames averaged per FITS entry')
                hea['TEXP_SUB'] = (texp * subset,
                                   'Effective exposure of each FITS entry (s)')
                fitsname = os.path.join(
                    path_name,
                    f"{root_name}_spec_{str(i).zfill(len(str(use_sequence)))}.fits")
            else:
                # Save the averaged subset, then start a new one
                arr = np.asarray(datacube_subset, dtype=use_dtype)
                datacube[0] = np.mean(arr, axis=0)
                fits.PrimaryHDU(data=datacube, header=hea).writeto(
                    fitsname, overwrite=True)
                fitsname = os.path.join(
                    path_name,
                    f"{root_name}_spec_{str(i).zfill(len(str(use_sequence)))}.fits")
                datacube_subset = [cropped]
        else:
            datacube_subset.append(cropped)

    cam.stop_acquisition()
    cam.close_device()

    log.info("[acquisition] Done. Output directory: {path_name}")
    return path_name


# ------------------------------------------------------------------------------

def run_extraction(cfg: dict) -> str:
    """
    Spectral extraction from raw FITS datacubes.

    For each FITS file in `indir`:
      1. Detect the spectral centroid (peak column of the collapsed profile).
      2. For each frame: optionally trim, sum over the extraction window → 1D
         spectrum; optionally subtract local dark from flanking columns.
    Optionally bin consecutive spectra (ave_chunk > 0).
    Save the resulting (N_spectra × N_pixels) array as a FITS file.

    Parameters
    ----------
    cfg : dict
        Recognised keys: indir, suffix, width, ave_chunk, dark_sub,
        trim_low, trim_up.

    Returns
    -------
    outname : str
        Path to the output FITS datacube, or empty string if nothing was written.
    """
    indir   = str(_get(cfg, "indir"))
    suffix  = str(_get(cfg, "suffix"))
    width   = int(_get(cfg, "width")) // 2   # half-width
    ave     = int(_get(cfg, "ave_chunk"))
    dark    = int(_get(cfg, "dark_sub"))
    trimlow = int(_get(cfg, "trim_low"))
    trimup  = int(_get(cfg, "trim_up"))

    if not os.path.isdir(indir):
        log.warning("[extraction] Directory not found: {indir}")
        return ""

    spectra = sorted(os.listdir(indir))
    if not spectra:
        log.warning("[extraction] No files found.")
        return ""

    outname = os.path.join(indir, os.path.basename(indir) + suffix)
    alls = []
    data_flat = None   # keep last for diagnostic plot

    for spectrum in spectra:
        # Skip output file and non-FITS files
        if spectrum.endswith(suffix) or not spectrum.endswith('.fits'):
            continue

        with fits.open(os.path.join(indir, spectrum), memmap=False) as hdu:
            log.info("  reading {spectrum}")
            cubedata = hdu[0].data   # shape: (N_frames, H, W)

        # Collapse to 1D to find spectral centroid
        if len(cubedata) > 1:
            data_flat = np.sum(cubedata, axis=0)   # sum frames → 2D
        else:
            data_flat = cubedata[0]                # single frame → 2D
        data_flat = np.sum(data_flat, axis=0)      # sum rows → 1D
        pixel = np.argmax(data_flat)

        for frame in cubedata:
            # Optional spatial trimming along dispersion axis
            if trimlow and trimup:
                frame = frame[trimlow:-trimup]
            elif trimlow:
                frame = frame[trimlow:]
            elif trimup:
                frame = frame[:-trimup]

            extracted = np.sum(frame[:, pixel - width:pixel + width + 1], axis=1)

            if dark:
                dark1 = np.sum(frame[:, pixel - 4*width:pixel - 2*width + 1], axis=1)
                dark2 = np.sum(frame[:, pixel + 2*width:pixel + 4*width + 1], axis=1)
                extracted = extracted - (dark1 + dark2) / 2.0

            alls.append(extracted)

    if not alls:
        log.warning("[extraction] No spectra extracted.")
        return ""

    alls = np.asarray(alls)   # shape: (N_total, N_pixels)

    # Diagnostic plot: save centroid/extraction-window figure to the input directory
    if data_flat is not None:
        fig_path = os.path.join(indir, os.path.basename(indir) + "_centroid.png")
        fig, ax = plt.subplots()
        ax.plot(np.arange(len(data_flat)), data_flat)
        ax.axvline(x=pixel, color='r', label='centroid')
        ax.axvline(x=pixel - width, ls='dashed', color='g', label='extraction window')
        ax.axvline(x=pixel + width, ls='dashed', color='g')
        ax.set_xlabel('Pixel (column)')
        ax.set_ylabel('Collapsed signal')
        ax.legend()
        fig.tight_layout()
        fig.savefig(fig_path, dpi=150)
        plt.close(fig)
        log.info(f"[extraction] Centroid plot saved: {fig_path}")

    # Optional temporal binning
    if ave:
        remainder = len(alls) % ave
        if remainder:
            alls = alls[:-remainder]
        c = alls.shape[1]
        alls = alls.T.reshape(-1, ave).mean(axis=1).reshape(c, -1).T

    fits.PrimaryHDU(data=alls).writeto(outname, overwrite=True)
    log.info("[extraction] Saved: {outname}")
    return outname


# ------------------------------------------------------------------------------

def run_analysis(cfg: dict) -> None:
    """
    Spectral analysis pipeline.

    Steps:
      1. Flat-field correction (first `flat` spectra).
      2. Polynomial continuum normalisation (degree `poly`).
      3. Save corrected datacube (_all.fit; optionally _all.txt).
      4. Display 2D waterfall plot.
      5. Compute three time-averaged spectral blocks.
      6. Save averaged blocks (_ave.fit; optionally _ave.txt).
      7. Optional FFT on all three blocks.

    Parameters
    ----------
    cfg : dict
        Recognised keys: datacube, flat, poly, chunk, cuts_low, cuts_high,
        start1, end1, start2, end2, savextract, saveave, fourier.
    """
    datacube_path = str(_get(cfg, "datacube"))
    do_flat   = int(_get(cfg, "flat"))
    deg_norm  = int(_get(cfg, "poly"))
    nspec     = int(_get(cfg, "chunk"))
    cutslow   = float(_get(cfg, "cuts_low"))  or None
    cutshigh  = float(_get(cfg, "cuts_high")) or None
    start1    = int(_get(cfg, "start1"))
    end1      = int(_get(cfg, "end1"))
    start2    = int(_get(cfg, "start2"))
    end2      = int(_get(cfg, "end2"))
    savextract = int(_get(cfg, "savextract"))
    saveave   = int(_get(cfg, "saveave"))
    do_fourier = int(_get(cfg, "fourier"))

    root_name = os.path.splitext(datacube_path)[0]

    with fits.open(datacube_path) as hdu:
        datas = hdu[0].data.copy()   # (N_spectra, N_pixels)

    wave = np.arange(datas.shape[1])

    # Step 1: flat-field correction
    if do_flat:
        max_flat = len(datas) // 3
        if do_flat > max_flat:
            print(f'[analysis] flat {do_flat} > 1/3 of total; reset to {max_flat}')
            do_flat = max_flat
        flatfield = np.nanmean(datas[:do_flat + 1], axis=0)
        datas = datas / flatfield

    # Step 2: polynomial continuum normalisation
    for n, spec in enumerate(datas):
        fit = np.poly1d(np.polyfit(wave, spec, deg_norm))
        datas[n] = spec / fit(wave)

    # Step 3: save corrected datacube
    fits.PrimaryHDU(data=datas).writeto(root_name + '_all.fit', overwrite=True)
    if savextract:
        np.savetxt(root_name + '_all.txt', datas.T)

    # Step 4: waterfall plot — saved to disk alongside the datacube
    waterfall_path = root_name + "_waterfall.png"
    fig, ax = plt.subplots(figsize=(6, 6))
    im = ax.imshow(datas, vmin=cutslow, vmax=cutshigh, aspect='auto', origin='upper')
    ax.set_xlabel('Pixel (wavelength)')
    ax.set_ylabel('Spectrum index (time)')
    fig.colorbar(im, orientation="horizontal", pad=0.2)
    fig.tight_layout()
    fig.savefig(waterfall_path, dpi=150)
    plt.close(fig)
    log.info(f"[analysis] Waterfall plot saved: {waterfall_path}")

    # Step 5: define three time blocks
    if do_flat:
        block1 = np.nanmean(datas[:do_flat], axis=0)
    else:
        do_flat = max(1, len(datas) // 10)
        block1  = np.nanmean(datas[:do_flat], axis=0)

    b2_start = max(start1, do_flat) if start1 else do_flat
    if end1:
        b2_end = end1
    else:
        b2_end = min(nspec, len(datas) // 2)
        if nspec > len(datas) // 2:
            print(f'[analysis] chunk ({nspec}) > half cube; reset end1 to {b2_end}')

    b3_start = max(start2, b2_end) if start2 else b2_end
    b3_end   = end2 if end2 else len(datas)

    block2 = np.nanmean(datas[b2_start:b2_end], axis=0)
    block3 = np.nanmean(datas[b3_start:b3_end], axis=0)

    # Step 6: save averaged blocks
    stacked = np.vstack((block1, block2, block3))
    fits.PrimaryHDU(data=stacked).writeto(root_name + '_ave.fit', overwrite=True)
    if saveave:
        np.savetxt(root_name + '_ave.txt', stacked.T)

    # Step 7: optional FFT — figure saved to disk alongside the datacube
    if do_fourier:
        fft_path = root_name + "_FFT.png"
        fig, ax = plt.subplots()
        for label, block in [('block1', block1), ('block2', block2), ('block3', block3)]:
            freqs, amp = _do_fft(block)
            ax.plot(freqs, amp, label=label)
            np.savetxt(f"{root_name}_FFT_{label}.txt", np.column_stack((freqs, amp)))
        ax.set_xlabel('Frequency (cycles/pixel)')
        ax.set_ylabel('Amplitude')
        ax.legend(loc='best')
        fig.tight_layout()
        fig.savefig(fft_path, dpi=150)
        plt.close(fig)
        log.info(f"[analysis] FFT plot saved: {fft_path}")

    log.info("[analysis] Done. Output root: {root_name}")


# ==============================================================================
# PRIVATE HELPERS
# ==============================================================================

def _do_fft(spec: np.ndarray):
    """
    Compute the one-sided FFT amplitude spectrum of a 1D signal.
    Parameters:
        spec (ndarray): 1D input array (spectrum or time series).

    Returns
    -------
    freqs    : ndarray  positive frequency axis (cycles/pixel)
    yfft_pos : ndarray  FFT amplitude at each positive frequency
    """
    sig_fft = np.fft.fft(spec)
    wave    = np.arange(len(spec))
    yfft    = np.abs(sig_fft)
    xfft    = np.fft.fftfreq(len(wave), d=1.0) # unit pixel spacing
    pos_mask = xfft > 0
    freqs    = xfft[pos_mask]
    yfft_pos = yfft[pos_mask]

    return freqs.real, yfft_pos.real


# ==============================================================================
# USAGE EXAMPLE  (run with:  python acquisition_manager_lib.py)
# ==============================================================================
if __name__ == '__main__':
    gc.collect()

    # ------------------------------------------------------------------
    # Example 1 – full pipeline: acquire → extract → analyse
    # ------------------------------------------------------------------

    acq_cfg = {
        "serial":        "28720523",
        "bin_bit":       16,
        "texp_ms":       100.0,
        "gain":          0.0,
        "sequence":      10000,
        "subset":        100,
        "position":      1750,
        "auto_position": 1,
        "halfwidth":     250,
        "outdir":        r"C:\at\DocOss\lab2023\spectra",
    }

    ext_cfg = {
        # "indir" will be filled automatically from acquisition output
        "suffix":    "_datacube.fit",
        "width":     50,
        "ave_chunk": 0,
        "dark_sub":  1,
        "trim_low":  10,
        "trim_up":   10,
    }

    ana_cfg = {
        # "datacube" will be filled automatically from extraction output
        "flat":       100,
        "poly":       2,
        "chunk":      5000,
        "cuts_low":   0.95,
        "cuts_high":  1.05,
        "start1":     0,
        "end1":       0,
        "start2":     0,
        "end2":       0,
        "savextract": 1,
        "saveave":    1,
        "fourier":    1,
    }

    # Run acquisition
    output_dir = run_acquisition(acq_cfg)

    # Pass output dir to extraction
    ext_cfg["indir"] = output_dir
    datacube_path = run_extraction(ext_cfg)

    # Pass datacube path to analysis
    if datacube_path:
        ana_cfg["datacube"] = datacube_path
        run_analysis(ana_cfg)

    # ------------------------------------------------------------------
    # Example 2 – extraction + analysis only (no camera)
    # ------------------------------------------------------------------

    ext_cfg_only = {
        "indir":     r"C:\at\DocOss\lab2023\spectra\2025-06-06_10-00-00",
        "suffix":    "_datacube.fit",
        "width":     50,
        "ave_chunk": 5,
        "dark_sub":  1,
        "trim_low":  10,
        "trim_up":   10,
    }

    ana_cfg_only = {
        "datacube":   r"C:\at\DocOss\lab2023\spectra\2025-06-06_10-00-00\2025-06-06_10-00-00_datacube.fit",
        "flat":       100,
        "poly":       2,
        "chunk":      5000,
        "cuts_low":   0.0,
        "cuts_high":  0.0,
        "savextract": 0,
        "saveave":    1,
        "fourier":    0,
    }

    datacube_path = run_extraction(ext_cfg_only)
    if datacube_path:
        ana_cfg_only["datacube"] = datacube_path
        run_analysis(ana_cfg_only)
