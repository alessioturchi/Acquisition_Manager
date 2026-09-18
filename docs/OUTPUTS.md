# Output files

All paths are relative to the **output directory** set in the acquisition panel
(`outdir`). Every acquisition cycle creates its own timestamped subdirectory
named `YYYY-MM-DD_HH-MM-SS`; `root` below is that same timestamp string.

---

## 1. Session level — written once, in `outdir/`

| File | Written by | Content |
|---|---|---|
| `pipeline.log` | `setup_file_logging()` | Full INFO-level log of the session, appended across runs. The place to check exposure clamping, gain clamping, crop window, pointing warnings and frame errors. |
| `acquisition_config.json` | GUI, before each run | Snapshot of every GUI parameter. Reloadable with **Import config**. |
| `live_frames/` | preview **Capture FITS** | One `live_<YYYYMMDD_HHMMSS>.fits` per capture. Not part of the pipeline; useful for documenting a pointing configuration. |

> `acquisition_config.json` was called `ximea_config.json` before September 2026.
> Old files still import correctly: `texp` in seconds is converted to `texp_ms`.

---

## 2. Acquisition — `run_acquisition()`

Creates `outdir/root/` and writes, for each averaged block:

```
outdir/root/root_spec_NNN.fits
```

`NNN` is a zero-padded 0-based block index. The number of files is
`sequence // subset`; leftover frames that do not fill a block are discarded
(with a warning in the log). Each file holds one image of shape
`(1, height, 2*halfwidth)`: the mean of `subset` cropped raw frames.

### FITS header

| Keyword | Type | Meaning |
|---|---|---|
| `CROP_MIN` | int | First column of the crop window (`position - halfwidth`) |
| `CROP_MAX` | int | Last column of the crop window (`position + halfwidth`) |
| `POSITION` | int | Spectral column actually used |
| `POSMODE` | str | `AUTO` (column detected on frame 0) or `MANUAL` (`position` used as given) |
| `ARGMAX0` | int | Column of the frame-0 argmax, recorded in both modes |
| `TEXP` | float | Single-frame exposure, seconds |
| `TEXP_MS` | float | Single-frame exposure, milliseconds |
| `GAIN` | float | Camera gain in dB, **as read back from the camera** after setting it |
| `SUBSET` | int | Raw frames averaged per file |
| `TEXP_SUB` | float | Effective exposure of each file, `TEXP * SUBSET`, seconds |

`TEXP`, `TEXP_MS` and `GAIN` report the values the camera confirmed, not the
requested ones. When they differ, `pipeline.log` carries a WARNING naming both.

Comparing `POSITION` with `ARGMAX0` tells you whether the pointing was good:
in `MANUAL` mode a gap larger than `halfwidth` also raises a log warning.

---

## 3. Extraction — `run_extraction()`

Writes into the same timestamped directory:

| File | Content |
|---|---|
| `root_datacube.fit` | Extracted spectra, shape `(N_spectra, N_pixels)`. The suffix is configurable (`suffix`, default `_datacube.fit`). |
| `root_centroid.png` | Diagnostic plot: collapsed signal versus column, with the detected centroid (red) and the extraction window (green dashed). |

If temporal binning is enabled (`ave_chunk > 0`), `N_spectra` is reduced
accordingly.

---

## 4. Analysis — `run_analysis()`

Writes alongside the datacube, where `base` is the datacube path without its
extension:

| File | Condition | Content |
|---|---|---|
| `base_all.fit` | always | Corrected spectra: flat-fielded and continuum-normalised |
| `base_all.txt` | `savextract = 1` | Same data as ASCII, transposed (one column per spectrum) |
| `base_waterfall.png` | always | 2-D waterfall plot of the corrected spectra |
| `base_ave.fit` | always | The three averaged blocks stacked together |
| `base_ave.txt` | `saveave = 1` | Same data as ASCII, transposed |
| `base_FFT.png` | `fourier = 1` | FFT amplitude spectra of the three blocks, overlaid |
| `base_FFT_block1.txt` | `fourier = 1` | Two columns: frequency (cycles/pixel), amplitude |
| `base_FFT_block2.txt` | `fourier = 1` | idem |
| `base_FFT_block3.txt` | `fourier = 1` | idem |

The three blocks are: the flat-field region, the region defined by `chunk` or by
`start1`/`end1`, and the region defined by `start2`/`end2`.

---

## 5. What STOP deletes

Pressing **STOP** during a loop interrupts it after the current phase and
deletes the timestamped directory of the **incomplete** iteration only, with
everything in it. Iterations that completed cleanly are never touched, and
`pipeline.log` and `acquisition_config.json` in the root `outdir` are preserved.
