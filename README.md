# Acquisition Manager

Spectral acquisition suite for a XIMEA camera, with an integrated live preview
and pointing window, optional motorised fibre agitation, and an
acquisition → extraction → analysis loop.

Developed at INAF – Osservatorio Astrofisico di Arcetri in the context of
modal-noise characterisation of the multimode fibres feeding the ANDES
high-resolution spectrograph for the ELT.

---

## What it does

The program drives a full measurement cycle:

1. **Acquisition** — grabs `sequence` frames from the XIMEA camera, crops a
   column window around the spectrum, averages every `subset` frames and
   writes one FITS file per averaged block.
2. **Extraction** *(optional)* — collapses the cropped frames into 1-D spectra
   and stacks them into a FITS datacube.
3. **Analysis** *(optional)* — flat-field correction, continuum normalisation,
   waterfall plot, block averages and FFT amplitude spectra.
4. **Motor move + wait** — between loop iterations, a PI C-863 controller moves
   a rotary stage following a bounded random walk, to decorrelate the modal
   pattern in the fibre.

A **live preview window** (`lib/camera_view_lib.py`) can be opened whenever no
acquisition is running. It shows the camera at ~30 fps with a reticle, a
centroid measurement with FWHM, row/column intensity profiles, and an overlay of
the crop window that the acquisition will actually use.

### Camera ownership

The XIMEA device is an **exclusive resource**: only one handle may be open at a
time. The program enforces a single rule:

> The preview owns the camera only while its window is open, and no acquisition
> may start until the preview has been closed and the handle released.

In practice, pressing **GO!** or **GO LOOP** closes the preview, joins its
grabber thread, calls `stop_acquisition()` / `close_device()`, waits a short
settle time, and only then opens the device for acquisition. If
*Reopen after run* is ticked, the preview comes back when the run ends.

### Motor

The fibre agitation stage is optional and controlled by the **Motor switch** in
the *Loop control* panel. With the switch off, no servo command and no move are
issued; the wait between cycles still applies, so a run without agitation keeps
the same timing as one with it — useful when measuring the static modal pattern.

All motor parameters live in `motor.yml` next to the entry point: controller
address and port, GCS axis, timeouts, tolerances and the bound of the random
walk. Edit it and restart the GUI. If the file is missing or unreadable the
Motor switch is forced off and disabled, with the reason in its tooltip: the
program never falls back to a guessed address.

> `motor.yml` contains the address of an instrument on your network. If the
> repository is public, uncomment `motor.yml` in `.gitignore`, run
> `git rm --cached motor.yml`, and let `motor.example.yml` be the tracked copy.

---

## Repository layout

```
Acquisition_Manager/
├── acquisition_manager_gui.py    entry point (Tkinter GUI)
├── lib/
│   ├── acquisition_manager_lib.py  headless acquisition / extraction / analysis
│   ├── camera_view_lib.py          live preview and pointing window
│   └── motor_client_lib.py         TCP client for the PI C-863 motor server
├── legacy/                        superseded code, kept for reference only
│   ├── camera_puntamento_mac_3.py  standalone preview (replaced by camera_view_lib)
│   └── acquisition_manager_lib_plot.py  alternative plotting library, not imported
├── tests/
│   └── test_acquisition_synthetic.py  runs without camera or astropy
├── docs/
│   ├── CONFIGURATION.md           every parameter, unit and default
│   └── OUTPUTS.md                 every file the program writes
├── motor.yml                      motor controller parameters (address, limits)
├── motor.example.yml              tracked template of the above
├── camera_config.json             preview overlay / pointing state
├── requirements.txt
└── LICENSE                        GPL-3.0
```

Nothing in `legacy/` is imported by the running code.

---

## Requirements

Python 3.8 or later.

| Package | Used by | Required? |
|---|---|---|
| `numpy` | everything | yes |
| `astropy` | FITS I/O | yes |
| `matplotlib` | diagnostic plots (`Agg` backend) | yes |
| `PyYAML` | reads `motor.yml` | motor only |
| `ximea` (xiAPI Python bindings) | camera access | yes, for real acquisition |
| `opencv-python` | preview overlay rendering | preview only |
| `Pillow` | preview frame display in Tkinter | preview only |
| `tkinter` | GUI | yes (ships with CPython on Windows/macOS; `apt install python3-tk` on Debian/Ubuntu) |

```bash
pip install -r requirements.txt
```

The `ximea` package is **not** on PyPI: it is installed by the XIMEA xiAPI
software package from the vendor. Install the xiAPI first, then make sure its
Python bindings are on `PYTHONPATH`.

If `opencv-python`, `Pillow` or `ximea` are missing, the GUI still starts: the
preview button is disabled and its tooltip reports the missing module.

---

## Running

```bash
cd Acquisition_Manager
python acquisition_manager_gui.py
```

The preview can also be used on its own, without the acquisition GUI:

```bash
python -m lib.camera_view_lib --serial 28720523 --outdir /path/to/output
```

### Typical session

1. Set the output directory and the camera serial number.
2. Press **Open preview**, adjust exposure and gain on the sliders until the
   spectrum is bright but not saturated (watch the `I: min/mean/max` readout).
3. Enable **Centroid** and press **Use for acquisition**: exposure, gain and the
   measured spectral column are copied into the acquisition panel, and
   *Auto position* is switched off so the manual column is actually used.
4. Check the magenta crop-window lines in the preview: that is exactly what will
   be written to the FITS files.
5. Set the number of iterations and the wait time, decide whether the fibre
   agitation motor should run (**Motor switch**), then press **GO LOOP**.
   The preview closes automatically.

---

## Tests

The synthetic test bench stubs the camera, `astropy` and `matplotlib`, so it
runs anywhere with only `numpy` installed:

```bash
python tests/test_acquisition_synthetic.py
```

It checks exposure resolution (`texp_ms`, legacy `texp`, missing key), gain
clamping, both branches of `auto_position`, and the FITS header content.

---

## Documentation

- [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) — every parameter, its unit,
  its default and the corresponding JSON key.
- [`docs/OUTPUTS.md`](docs/OUTPUTS.md) — every file written, where, and what it
  contains, including all FITS header keywords.

---

## Known limitations

- The exposure/gain transfer from the preview is one-way at the moment: changing
  the acquisition fields does not update an already open preview.
- `run_acquisition_old()` in `lib/acquisition_manager_lib.py` is a superseded
  implementation kept for reference; it is never called and does not support
  `texp_ms`, `gain` or `auto_position`.
- The FWHM reported by the preview is biased slightly low (about −0.4 % on a
  synthetic Gaussian) because the centroid algorithm subtracts the 10th
  percentile as background, which clips the wings. It is adequate for pointing,
  not for photometric work.
- `motor.yml` is read once at start-up: editing it requires restarting the GUI.
- `home_tol` in `motor.yml` is declared but not read by any code path; it is
  kept for a future homing routine.

---

## Credits

Originally written by Monica Rainer (2023).
Extended and restructured by Alessio Turchi (2026): loop control, threading,
motor integration, live preview merge.

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE).
