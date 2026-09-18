# Configuration

Every GUI field maps to one key in `acquisition_config.json`, saved in the
output directory before each run and reloadable with **Import config**.
The same dictionaries can be passed directly to the library functions for
headless use.

---

## Acquisition — `cfg["acquisition"]`

| Field | Key | Unit | Default | Notes |
|---|---|---|---|---|
| XIMEA Serial number | `serial` | — | `28720523` | Used by both the acquisition and the preview, so they always open the same device. |
| Bit | `bin_bit` | bits | `16` | Pixel depth of the averaged output (`uint8` / `uint16`). |
| Exp. time | `texp_ms` | **ms** | `200.0` | Same unit as the preview slider. |
| — | `texp` | s | derived | Written for backward compatibility only; `texp_ms` wins on read. |
| Gain | `gain` | dB | `0.0` | Applied by `run_acquisition()` before grabbing. **Reset gain** restores `0.0` and pushes it to an open preview. |
| Number of spectra | `sequence` | frames | `200` | Truncated down to a multiple of `subset`. |
| Subset to save | `subset` | frames | `100` | Raw frames averaged into each output file. |
| Signal position | `position` | column | `1750` | Spectral column used for cropping. |
| Auto position | `auto_position` | 0/1 | `1` | `1`: the column is detected on frame 0 and `position` is only a fallback. `0`: `position` is used as given. |
| Half-width window | `halfwidth` | px | `250` | Crop is `[position-halfwidth, position+halfwidth]`. |
| Output directory | `outdir` | path | `Z:/Loop_Measures/Try1` | Root of everything the program writes. |

### On `auto_position`

With `auto_position = 1` the crop column is `argmax` of the column-summed
frame 0, unless that lands at or below `halfwidth` (too close to the edge), in
which case `position` is used. This is robust for a bright, isolated spectrum.

With a faint source the argmax can latch onto a hot pixel or a reflection. Set
`auto_position = 0` and give the column explicitly — the **Use for acquisition**
button in the preview does exactly that, and switches the flag off for you.

In both modes `ARGMAX0` is recorded in the FITS header, so the two can always be
compared after the fact.

---

## Extraction — `cfg["extraction"]`

| Field | Key | Unit | Default | Notes |
|---|---|---|---|---|
| Enable extraction | `do_extract` | bool | `true` | Stored under `loop`. Turning it off also disables analysis. |
| — | `indir` | path | filled automatically | Timestamped directory produced by the acquisition. |
| Output suffix | `suffix` | — | `_datacube.fit` | Output name = input directory name + suffix. |
| Ext. window | `width` | px | `50` | Full width of the extraction window. |
| Average spectra | `ave_chunk` | spectra | `0` | Bin N consecutive spectra; `0` disables. |
| Dark subtraction | `dark_sub` | 0/1 | `0` | Subtract a local dark from the flanking columns. |
| Trim spectra (low) | `trim_low` | px | `10` | Rows trimmed from the bottom of each frame. |
| Trim spectra (up) | `trim_up` | px | `10` | Rows trimmed from the top. |

---

## Analysis — `cfg["analysis"]`

| Field | Key | Unit | Default | Notes |
|---|---|---|---|---|
| Enable analysis | `do_analyze` | bool | `true` | Stored under `loop`. Requires extraction. |
| — | `datacube` | path | filled automatically | Datacube produced by the extraction. |
| Flat-field | `flat` | spectra | `0` | Number of initial spectra used as flat-field reference. |
| Poly degree | `poly` | — | `0` | Polynomial degree for continuum normalisation; `0` = constant. |
| Num. images | `chunk` | spectra | `sequence/2` | Size of block 2. Kept in sync with `sequence` automatically. |
| OR start / end | `start1`, `end1` | index | `0` | Explicit bounds for block 2; override `chunk`. |
| AND start / end | `start2`, `end2` | index | `0` | Explicit bounds for block 3. |
| Cuts low / high | `cuts_low`, `cuts_high` | — | `0.0` | Display cuts of the waterfall plot; `0` = auto. |
| Save all | `savextract` | 0/1 | `1` | Write all corrected spectra as ASCII. |
| Save averages | `saveave` | 0/1 | `1` | Write the three averaged blocks as ASCII. |
| FFT | `fourier` | 0/1 | `1` | Compute and save the FFT amplitude spectra. |

---

## Loop — `cfg["loop"]`

| Field | Key | Unit | Default | Notes |
|---|---|---|---|---|
| Number of iterations | `n_loops` | — | `1` | Cycles of acquire → extract → analyse → motor → wait. |
| Wait between cycles | `wait_sec` | s | `5.0` | Slept in 200 ms steps so STOP responds promptly. |
| Enable extraction | `do_extract` | bool | `true` | |
| Enable analysis | `do_analyze` | bool | `true` | |

The motor move and the wait are skipped after the last iteration.

---

## Preview — `cfg["preview"]`

| Field | Key | Default | Notes |
|---|---|---|---|
| Reopen after run | `reopen_after_run` | `true` | Reopen the preview when a run ends, **if it was open before it started**. |

Overlay and pointing state live in a separate file, `camera_config.json`,
written by the preview's **Save config** button:

| Key | Meaning |
|---|---|
| `serial` | Camera the config refers to |
| `params.gain`, `params.exposure` | Gain in dB, exposure in **µs** (native xiAPI unit) |
| `overlay.crosshair`, `.circle`, `.grid`, `.info` | Which overlays are on |
| `overlay.crosshair_size`, `.circle_radius` | Overlay sizes, px |
| `overlay.offset_x`, `.offset_y` | Reticle offset from the frame centre, px. The reticle column is `cam_w/2 + offset_x`. |

---

## Motor

Not exposed in the GUI: edit `_MOTOR_CONFIG` at the top of
`acquisition_manager_gui.py`.

| Key | Default | Meaning |
|---|---|---|
| `host`, `port` | `193.206.154.132`, `2002` | Motor server on the Raspberry Pi |
| `axis` | `"1"` | GCS axis identifier |
| `timeout` | `10.0` s | Socket timeout |
| `home_tol`, `move_tol` | `0.1` | Positioning tolerances |
| `ont_retries`, `ont_delay` | `100`, `0.5` s | On-target polling |
| `move_delay` | `2.5` s | Settle time after a move |

`_MOTOR_MAX_DEV` (default `20.0`) bounds the random walk: every move is chosen
so the accumulated displacement from the starting position stays within
±`_MOTOR_MAX_DEV`. The servo is enabled once at the start of a loop and disabled
in the `finally` block, not per move.

---

## Headless use

```python
import lib.acquisition_manager_lib as alib

acq = {"serial": "28720523", "bin_bit": 16, "texp_ms": 100.0, "gain": 0.0,
       "sequence": 10000, "subset": 100, "position": 1750,
       "auto_position": 1, "halfwidth": 250, "outdir": "/data/spectra"}

alib.setup_file_logging(acq["outdir"])
out_dir  = alib.run_acquisition(acq)
datacube = alib.run_extraction({"indir": out_dir, "suffix": "_datacube.fit",
                                "width": 50, "ave_chunk": 0, "dark_sub": 0,
                                "trim_low": 10, "trim_up": 10})
alib.run_analysis({"datacube": datacube, "flat": 100, "poly": 2, "chunk": 5000,
                   "cuts_low": 0.95, "cuts_high": 1.05,
                   "start1": 0, "end1": 0, "start2": 0, "end2": 0,
                   "savextract": 1, "saveave": 1, "fourier": 1})
```

Any key you omit falls back to `DEFAULT_CFG` in the library. The one exception
is the exposure: if neither `texp_ms` nor `texp` is present the default is used,
but if either is present it wins — the fallback never overrides a value you
actually supplied.
