# legacy/

Superseded code, kept for reference. **Nothing here is imported by the running
program.**

| File | Status |
|---|---|
| `camera_puntamento_mac_3.py` | Original standalone preview program (v3), with its own `tk.Tk` root and an OpenCV webcam fallback. Replaced by `lib/camera_view_lib.py`, which runs in a `Toplevel`, opens the camera by serial number, uses `XI_RAW16` and joins its grabber thread before releasing the handle. |
| `acquisition_manager_lib_plot.py` | Alternative plotting variant of the acquisition library. It was never imported by the GUI; kept in case some of its plotting code is still wanted. |

A third piece of superseded code lives inside the active library:
`run_acquisition_old()` in `lib/acquisition_manager_lib.py`. It is never called
and does not support `texp_ms`, `gain` or `auto_position`.
