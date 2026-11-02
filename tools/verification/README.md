# Verification scripts

Headless (Qt offscreen) scripts used to reproduce the defects and measure the fixes in the
audit document. Run each from the application folder, e.g.

    cd BreedingCharts_v1_24_perf_patched
    QT_QPA_PLATFORM=offscreen python tools/verification/verify_f1.py

They redirect the data directory to a temp folder (`database._data_dir`), stub `save_settings`,
and never touch your real data. Timings are machine dependent; compare ratios, not absolutes.

| Script | Covers |
|---|---|
| verify_v1 | year labels (F6), get_generation (F8), notes cache reset (F9), grid renders (F1), port hit-test (F20) |
| verify_v2 | canvas frame times at 3 zoom levels, scroll step, cached vs uncached pixel equality (F17-F19) |
| verify_v3 | failed background save restores photos and retry persists them (F2) |
| verify_e1 | atomic save keeps old file on error, search cache (F3/F25) |
| verify_e2 | Ctrl+A respects filter, resize skip, theme change keeps thumbnails (F5/F37/F38) |
| verify_e4 | Ctrl+A / deselect timing on 1,500 cards (F65) |
| verify_c2 | load_tree / auto_layout / auto_organize timing and edge-geometry equality (F55/F56) |
| verify_c4 | incremental add_node edges (F57) |
| verify_d3 | save-time metadata cache (F61) and zoom cache suspend/resume (F62) |
| verify_f1 | open does not write (F7), settings coalescing (F29), stale decode token (F15), revision skip (F26), sheet parents (F10), remove_many (F27), fit_view (F45) |
| verify_g1 | scaled JPEG thumbnail decode and photo format choice (F22/F51) |
| verify_perf2 | `.plf.meta` sidecar/stale fallback (F30), incremental connect/disconnect/remove edges vs full rebuild (F42) |
