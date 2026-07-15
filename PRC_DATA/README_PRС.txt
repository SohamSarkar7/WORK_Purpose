PRC AUTOMATION - RUN INSTRUCTIONS

Keep these files in the same folder:
- PRC_UI.py
- PRC_pipeline.py
- extract_prc.py
- Main_PRC.py

Run:
    python PRC_UI.py

Recommended settings:
- OCR processes: 2
- Match threshold: 88
- Debug images: Off for faster processing

Required packages:
    pip install pymupdf pillow opencv-python numpy pandas openpyxl easyocr

Pipeline:
1. Select the root factsheet folder.
2. Select/create the PRC image output folder.
3. Select Scheme Master.xlsx and keep Fund column as "Fund Name" unless your column differs.
4. Select the final .xlsx save location.
5. Click Run complete pipeline.

The UI uses a separate backend process, so OCR/PDF work does not run inside Tkinter's event thread.
The success popup appears only after a backend RESULT message and verification that output exists.
