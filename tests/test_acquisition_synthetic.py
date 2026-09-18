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
Synthetic validation of run_acquisition() without camera or astropy.

Stubs xiapi, astropy.io.fits and matplotlib, then exercises:
  - exposure resolution (texp_ms, legacy texp, missing key)
  - gain application and clamping
  - auto_position ON/OFF branch
  - FITS header content and number of files written
"""
import os
import sys
import shutil
import tempfile
import types

import numpy as np

# ----------------------------------------------------------------- stubs -----
calls = {"exposure_us": None, "gain": None, "format": None, "opened": None}


class FakeImage:
    def __init__(self):
        self.data = None

    def get_image_data_numpy(self):
        return self.data


class FakeCamera:
    """Minimal xiapi.Camera replacement driven by a module-level frame."""
    GAIN_MAX = 24.0

    def open_device_by_SN(self, sn):
        calls["opened"] = sn

    def set_imgdataformat(self, fmt):
        calls["format"] = fmt

    def set_exposure(self, us):
        calls["exposure_us"] = us

    def get_exposure(self):
        return calls["exposure_us"]

    def set_gain(self, g):
        calls["gain"] = min(g, self.GAIN_MAX)   # emulate camera clamping

    def get_gain(self):
        return calls["gain"]

    def start_acquisition(self):
        pass

    def stop_acquisition(self):
        pass

    def close_device(self):
        pass

    def get_image(self, img, timeout=None):
        img.data = FRAME


xiapi_mod = types.ModuleType("xiapi")
xiapi_mod.Camera = FakeCamera
xiapi_mod.Image = FakeImage
ximea_pkg = types.ModuleType("ximea")
ximea_pkg.xiapi = xiapi_mod
sys.modules["ximea"] = ximea_pkg
sys.modules["ximea.xiapi"] = xiapi_mod


class FakeHeader(dict):
    def __setitem__(self, k, v):
        dict.__setitem__(self, k, v[0] if isinstance(v, tuple) else v)


written = []


class FakePrimaryHDU:
    def __init__(self, data=None, header=None):
        self.data = data
        self.header = header if header is not None else FakeHeader()

    def writeto(self, path, overwrite=False):
        written.append((path, None if self.data is None else self.data.shape))
        open(path, "wb").close()


fits_mod = types.ModuleType("fits")
fits_mod.Header = FakeHeader
fits_mod.PrimaryHDU = FakePrimaryHDU
fits_mod.open = lambda *a, **k: None
astropy_io = types.ModuleType("astropy.io")
astropy_io.fits = fits_mod
astropy_pkg = types.ModuleType("astropy")
astropy_pkg.io = astropy_io
sys.modules["astropy"] = astropy_pkg
sys.modules["astropy.io"] = astropy_io
sys.modules["astropy.io.fits"] = fits_mod

mpl = types.ModuleType("matplotlib")
mpl.use = lambda *a, **k: None
pyplot = types.ModuleType("matplotlib.pyplot")
mpl.pyplot = pyplot
sys.modules["matplotlib"] = mpl
sys.modules["matplotlib.pyplot"] = pyplot

# Repository root on sys.path, so that "lib" is importable from tests/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lib.acquisition_manager_lib as alib   # noqa: E402

# A frame whose brightest column sits at 1200
FRAME = np.zeros((64, 2048), dtype=np.uint16)
FRAME[:, 1200] = 4000
FRAME[:, 300] = 500

BASE = {
    "serial": "TEST123", "bin_bit": 16, "sequence": 6, "subset": 3,
    "position": 1750, "halfwidth": 250,
}


def run(tag, extra, expect):
    """Run one acquisition and compare the checked quantities with expect."""
    global written
    written = []
    for k in calls:
        calls[k] = None
    tmp = tempfile.mkdtemp()
    cfg = dict(BASE, outdir=tmp, **extra)
    out = alib.run_acquisition(cfg)
    hdr = FakePrimaryHDU().header
    # Re-read the header actually used: it is the one attached to the writes,
    # so rebuild it from the last HDU created inside the library.
    got = {
        "exposure_us": calls["exposure_us"],
        "gain": calls["gain"],
        "n_files": len(written),
        "format": calls["format"],
    }
    ok = all(abs(got[k] - v) < 1e-6 if isinstance(v, float) else got[k] == v
             for k, v in expect.items())
    print(f"[{'PASS' if ok else 'FAIL'}] {tag}")
    for k, v in expect.items():
        flag = "  " if (got[k] == v or (isinstance(v, float) and abs(got[k] - v) < 1e-6)) else ">>"
        print(f"   {flag} {k}: got {got[k]!r}, expected {v!r}")
    shutil.rmtree(tmp, ignore_errors=True)
    return ok


# Capture the header built inside the library by wrapping PrimaryHDU
_headers = []
_orig_hdu = fits_mod.PrimaryHDU


class SpyHDU(_orig_hdu):
    def __init__(self, data=None, header=None):
        super().__init__(data=data, header=header)
        if header is not None:
            _headers.append(dict(header))


fits_mod.PrimaryHDU = SpyHDU
alib.fits.PrimaryHDU = SpyHDU

results = []
print("=== exposure resolution ===")
results.append(run("texp_ms authoritative (250 ms)",
                   {"texp_ms": 250.0, "gain": 3.0},
                   {"exposure_us": 250000.0, "gain": 3.0, "n_files": 2,
                    "format": "XI_RAW16"}))
results.append(run("legacy texp only (0.3 s -> 300 ms)",
                   {"texp": 0.3},
                   {"exposure_us": 300000.0, "n_files": 2}))
results.append(run("no exposure key -> DEFAULT_CFG texp_ms (100 ms)",
                   {},
                   {"exposure_us": 100000.0}))
print("=== gain ===")
results.append(run("gain clamped by the camera (40 -> 24 dB)",
                   {"texp_ms": 10.0, "gain": 40.0},
                   {"gain": 24.0}))

print("=== auto_position ===")
_headers.clear()
run("auto_position=1", {"texp_ms": 10.0, "auto_position": 1}, {"n_files": 2})
h_auto = _headers[0]
_headers.clear()
run("auto_position=0", {"texp_ms": 10.0, "auto_position": 0}, {"n_files": 2})
h_man = _headers[0]

checks = [
    ("AUTO: POSITION follows frame-0 argmax", h_auto["POSITION"], 1200),
    ("AUTO: POSMODE",                          h_auto["POSMODE"], "AUTO"),
    ("AUTO: CROP_MIN",                         h_auto["CROP_MIN"], 950),
    ("MANUAL: POSITION is the configured one", h_man["POSITION"], 1750),
    ("MANUAL: POSMODE",                        h_man["POSMODE"], "MANUAL"),
    ("MANUAL: ARGMAX0 still recorded",         h_man["ARGMAX0"], 1200),
    ("MANUAL: CROP_MIN",                       h_man["CROP_MIN"], 1500),
    ("TEXP_MS in header",                      h_man["TEXP_MS"], 10.0),
    ("TEXP (s) in header",                     h_man["TEXP"], 0.01),
    ("GAIN in header",                         h_man["GAIN"], 0.0),
]
print("=== FITS header ===")
for tag, got, exp in checks:
    ok = got == exp
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {tag}: got {got!r}, expected {exp!r}")

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
