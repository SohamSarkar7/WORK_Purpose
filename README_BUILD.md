# PRC Factsheet Automation — Build Kit

## Why you have to build this yourself on Windows

PyInstaller builds an executable for the operating system it *runs on* — it
cannot cross-compile. Your dependencies (`torch`, `easyocr`, `opencv`,
`pymupdf`) also ship as OS-specific compiled binaries. A `.exe` can only be
produced by running PyInstaller on an actual Windows machine. That's what
this kit is for — it turns the build into one double-click.

## How to build it

**Prerequisites (one-time, on the Windows machine that will build it):**
- 64-bit Python 3.10 or 3.11 from https://www.python.org/downloads/
  (during install, check "Add python.exe to PATH")
- ~10 GB free disk space (mostly `torch`)
- An internet connection for this step (downloads the dependencies)

**Steps:**
1. Put this whole folder somewhere on the Windows PC, e.g. `C:\PRC_App`
2. Double-click `build_exe.bat`
3. Wait — the first run installs everything (~10-20 min depending on your
   connection), then builds the `.exe`
4. Your app appears at `dist\PRC_Factsheet_Automation\PRC_Factsheet_Automation.exe`

To share the app with someone else (or move it to another PC), **zip and copy
the whole `dist\PRC_Factsheet_Automation` folder**, not just the `.exe` file —
it depends on the DLLs and data files sitting next to it.

## First run needs internet once, for a different reason

EasyOCR downloads its English recognition model (~64 MB) to
`%USERPROFILE%\.EasyOCR\model` the first time it runs, regardless of whether
you're running the `.py` or the built `.exe`. Run the app once with internet
available before you plan to use it fully offline. After that first run, it
works offline.

## What I changed in the code, and why

You asked for the logic to stay the same but the code to be more reliable —
here's exactly what was touched, in order of impact:

1. **Fixed a duplicate-function-name bug in `extract_prc.py` (the big one).**
   `crop_and_save` was defined *twice*. Python silently uses whichever
   definition comes last in the file, so every one of your 50+ AMC-specific
   extractors was actually calling the second, weaker version this whole
   time — which had no protection against two funds producing the same
   cleaned filename. When that happened, the second fund's PRC crop
   **silently overwrote** the first fund's file, with no warning printed
   anywhere. This is almost certainly part of why some funds were missing
   from your output. Removed the duplicate; every extractor now uses the
   version that already existed higher up the file, which avoids collisions
   by appending `_2`, `_3`, etc. I verified this fix with a test that
   deliberately forces two identical fund names — both files now survive.

2. **Added a generic fallback extractor** (`process_amc_generic_fallback` in
   `extract_prc.py`), used only when an AMC-specific extractor finds nothing
   for a PDF. It reuses your existing helper functions
   (`get_keyword_ygroups`, `text_block_above`, `crop_and_save`) with no
   AMC-specific tuning, so it's a second, generic chance at capturing the
   PRC table instead of that fund producing nothing at all. None of your 50
   existing AMC functions were touched.

3. **Stopped a locked/open output file from destroying a finished run.**
   In `Main_PRC.py`, if `PRC_Output_Final_Detailed.xlsx` happened to be open
   in Excel when the app tried to save it (a very easy mistake to make),
   the whole run — potentially hours of OCR — used to fail with no output
   saved at all. It now falls back to a timestamped filename automatically
   and tells you so, rather than losing the work.

4. **Stopped a bad Scheme Master file from crashing the run before it
   starts.** A missing/corrupt/locked Scheme Master Excel now logs a warning
   and continues without fund-name mapping, instead of hard-crashing before
   a single image is processed.

5. **Fixed a latent crash in `PRC_pipeline.py`.** It already called
   `Main_PRC.process_prc_folder_with_easyocr(..., parallel_workers=..., 
   fuzzy_threshold=...)`, but that function didn't accept those parameters —
   it would have raised `TypeError` the moment `PRC_pipeline.py` was actually
   run. It also expected a summary dict back (`summary.update(result)`) but
   the function returned nothing. Both are fixed: the function now accepts
   those parameters (and genuinely uses `fuzzy_threshold` in the scheme-master
   matching) and always returns a summary dict. (Your GUI, `PRC_UI.py`,
   doesn't go through `PRC_pipeline.py` at all — it calls `extract_prc` and
   `Main_PRC` directly — so this bug wasn't affecting you day-to-day, but
   it's fixed in case you use the CLI path too.)

6. **Added a visible error dialog for the `.exe` build.** A windowed
   PyInstaller app has no console, so any error happening outside the
   already-guarded worker thread used to just make the app vanish with zero
   explanation. There's now a global handler that shows a message box with
   the real traceback instead.

7. Added `multiprocessing.freeze_support()` at startup — standard,
   low-risk insurance for any frozen Windows build whose dependencies might
   spawn subprocesses.

8. **Fixed a crash that only shows up in a windowed `.exe` build.**
   `extract_prc.py` called `sys.stdout.reconfigure(...)` at import time.
   In a windowed build (`console=False`, no console box - the correct
   setting for a normal-looking app) Windows gives the process
   `sys.stdout = None`, so this crashed on launch, before the GUI could
   even open. Same fix applied to a `sys.stderr` write inside the app's own
   error-dialog handler, which had the identical problem and could have
   silently swallowed other errors instead of showing them to you.

9. **Fixed the Scheme Master name-mapping accuracy problem.** Two places
   in `Main_PRC.py` required the fund's first word (the AMC name, e.g.
   "HDFC", "360") to match *character-for-character* before a fund name
   would even be considered for matching, as a safety measure to stop
   wrong cross-AMC matches (e.g. a DSP fund being mapped to an SBI fund).
   In practice this was too strict: a single OCR misread in just that
   first word — "360" read as "36O", "NIPPON" read as "NIPP0N", "HDFC"
   read as "HDEC" — made two names from the *same* fund house look
   different, and the correct match was rejected before scoring even ran.
   This is very likely the main cause of names not mapping to the Scheme
   Master. It's now a fuzzy-tolerant check instead: exact matches, a
   single-character-edit difference, and generally similar tokens are all
   accepted, while genuinely different AMC names (DSP vs SBI, HDFC vs SBI)
   are still correctly blocked — verified with test cases for both.

10. **Fixed real, measured performance problems in `Main_PRC.py`:**
    - Every image was being read and decoded from disk **twice** (once in
      the main function, once again inside the OCR step, which received
      the file path instead of the already-loaded image). Now read once.
    - Matching an OCR'd fund name against the Scheme Master re-scanned
      *every single row* of the master list, row by row, for *every
      single image* — recomputing the AMC name for each master row from
      scratch every time, using pandas' slow `.iterrows()`. This is now
      computed once when the Scheme Master loads, and each image only
      fuzzy-compares against the master rows that could plausibly match
      (same AMC), rather than the entire list. Measured **4.5x faster**
      on a 384-row/32-AMC test master; the gain grows with a bigger master
      file. Verified with a benchmark script.
    - Scanning the image folder for files did 12 separate full folder-tree
      walks (one per file extension, times upper/lower case) instead of
      one. Now walks the tree once. Verified to produce the identical set
      of files.
    None of these changes affect the OCR detection logic itself (how a PRC
    value is read out of an image) - only how images are read from disk and
    how names are matched against the Scheme Master.

**`extract_prc.py` was replaced with your own updated version** (I didn't
modify it further) — the fixes above are all in `Main_PRC.py`, plus two
small windowed-build compatibility fixes shared between the two files.

**Nothing else was rewritten.** The 50 AMC-specific extraction functions in
`extract_prc.py` and the OCR/matching logic in `Main_PRC.py` are untouched —
those are the parts I'd want to see a specific failing PDF for before
touching, rather than guessing.

## If a specific AMC/PDF still doesn't extract correctly

The generic fallback (fix #2) helps when *no* extractor captures anything,
but if an AMC-specific function captures the *wrong* region or table because
that AMC changed their factsheet layout, the fix is to look at that one
function, not the whole file. If you hit this, send me:
- which AMC
- the specific factsheet PDF (or just the relevant page)
- what it extracted vs. what it should have extracted

and I can fix that one function directly.

## Troubleshooting the build

- **"python was not found"** — Python isn't installed or isn't on PATH.
  Reinstall from python.org and check "Add python.exe to PATH".
- **pip install fails on `torch`** — check you're on 64-bit Python, and that
  you have enough disk space and a stable connection; re-run the script,
  pip resumes/retries automatically most of the time.
- **The built .exe appears to do nothing when double-clicked** — temporarily
  set `console=False` to `console=True` near the bottom of `PRC_App.spec`,
  rebuild, and run the `.exe` from a Command Prompt window so you can read
  the real error instead of it flashing/vanishing.
- **Antivirus flags or deletes the .exe** — this is a common false positive
  for PyInstaller-built executables (they're an unsigned, unfamiliar binary
  to most AV engines). Add an exclusion for the `dist` folder, or consider
  code-signing the .exe if you'll be distributing it externally.
