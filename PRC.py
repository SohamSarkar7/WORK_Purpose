import cv2
import numpy as np
import pandas as pd
from pathlib import Path
import os
import re
import traceback
from difflib import SequenceMatcher

# ============================================================
# CONFIG
# ============================================================

SUPPORTED_EXTENSIONS = [".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"]
DEBUG_FOLDER = "PRC_Debug_Output"

VALID_PRC_VALUES = {
    "A-I", "A-II", "A-III",
    "B-I", "B-II", "B-III",
    "C-I", "C-II", "C-III"
}

CLASS_COL_MAP = {
    "A": "Relatively Low / Class A",
    "B": "Moderate / Class B",
    "C": "Relatively High / Class C"
}

CLASS_ROW_MAP = {
    "I": "Relatively Low / Class I",
    "II": "Moderate / Class II",
    "III": "Relatively High / Class III"
}

FUZZY_PRC_PATTERN = r"\b([ABC])\s*[-–—]?\s*(III|II|I|[I1L|]{1,3}|1|2|3)\b"
PARTIAL_PRC_PATTERN = r"\b([ABC])\s*[-–—]\s*$|\b([ABC])\s*[-–—]\s+"

# ============================================================
# EasyOCR Loader
# ============================================================

def load_easyocr():
    try:
        import easyocr
        # Set gpu=True if you have CUDA installed for a massive speedup
        reader = easyocr.Reader(["en"], gpu=False)
        return reader
    except Exception as e:
        print("EasyOCR could not be loaded. Install using: pip install easyocr")
        print("Error:", e)
        return None

def read_image_cv(image_path):
    try:
        data = np.fromfile(str(image_path), dtype=np.uint8)
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except Exception:
        return cv2.imread(str(image_path))

# ============================================================
# Text Normalization
# ============================================================

def normalize_text(text):
    if not text:
        return ""
    text = str(text).upper()
    replacements = {
        "—": "-", "–": "-", "−": "-", "_": "-", "|": "I", "!": "I",
        "Ⅰ": "I", "Ⅱ": "II", "Ⅲ": "III", "‘": "'", "’": "'", "“": '"', "”": '"',
        "（": "(", "）": ")", "Ｃ": "C", "Ｂ": "B", "Ａ": "A",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return re.sub(r"\s+", " ", text).strip()

def normalize_name_for_match(name):
    if not name: return ""
    text = str(name).upper().replace("&amp;", " AND ").replace("&", " AND ")
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    remove_words = {"PRC", "RISK", "RISKOMETER", "SCREENSHOT", "IMAGE", "PAGE", "COPY", "FINAL", "TABLE", "DEBUG", "OUTPUT", "MATRIX", "POTENTIAL", "CLASS"}
    return " ".join([w for w in text.split() if w not in remove_words]).strip()

def compact_name_for_match(name):
    return re.sub(r"[^A-Z0-9]", "", normalize_name_for_match(name))

def normalize_roman_ocr(raw_row):
    if not raw_row: return None
    row = str(raw_row).upper().strip()
    for old, new in {"|": "I", "!": "I", "1": "I", "L": "I", "l": "I", "Ⅲ": "III", "Ⅱ": "II", "Ⅰ": "I"}.items():
        row = row.replace(old, new)
    row = re.sub(r"[^I123]", "", row)
    
    if row in ["1", "I"]: return "I"
    if row in ["2", "II"]: return "II"
    if row in ["3", "III"] or row.count("I") >= 3: return "III"
    return None

def extract_all_prc_from_text(text):
    text = normalize_text(text)
    prc_values = []
    for m in re.finditer(FUZZY_PRC_PATTERN, text):
        row = normalize_roman_ocr(m.group(2))
        if row:
            prc = f"{m.group(1)}-{row}"
            if prc in VALID_PRC_VALUES:
                prc_values.append(prc)
    return list(dict.fromkeys(prc_values))

def extract_partial_prc_column(text):
    m = re.search(PARTIAL_PRC_PATTERN, normalize_text(text))
    if m and (m.group(1) or m.group(2)) in {"A", "B", "C"}:
        return m.group(1) or m.group(2)
    return None

# ============================================================
# Scheme Mapping
# ============================================================

def clean_scheme_name(name):
    name = Path(str(name)).stem
    name = re.sub(r"[_\-]+", " ", name)
    remove_words = {"prc", "risk", "riskometer", "page", "screenshot", "image", "final", "copy", "table", "potential", "class", "output", "debug", "automated", "matrix"}
    words = [w for w in name.split() if w.lower() not in remove_words]
    cleaned = " ".join(words).strip()
    return cleaned if cleaned else name

def get_first_meaningful_token(name):
    tokens = normalize_name_for_match(name).split()
    return tokens[0] if tokens else ""

def load_scheme_master(scheme_master_path, fund_col="Fund Name"):
    if not scheme_master_path or not Path(scheme_master_path).exists():
        return pd.DataFrame(columns=[fund_col])
    df_master = pd.read_excel(scheme_master_path, engine="openpyxl")
    df_master[fund_col] = df_master[fund_col].astype(str).str.strip()
    df_master = df_master[df_master[fund_col].str.len() > 0].drop_duplicates(subset=[fund_col]).reset_index(drop=True)
    df_master["_norm"] = df_master[fund_col].apply(normalize_name_for_match)
    df_master["_compact"] = df_master[fund_col].apply(compact_name_for_match)
    return df_master

def map_to_scheme_master(extracted_name, df_master, fund_col="Fund Name", fuzzy_threshold=98):
    if df_master.empty:
        return {"mapped_fund_name": extracted_name, "mapping_status": "Master not found", "mapping_score": 0}

    ex_norm, ex_comp, ex_token = normalize_name_for_match(extracted_name), compact_name_for_match(extracted_name), get_first_meaningful_token(extracted_name)
    
    exact = df_master[df_master["_norm"] == ex_norm]
    if not exact.empty: return {"mapped_fund_name": exact.iloc[0][fund_col], "mapping_status": "Exact", "mapping_score": 100}
    
    exact_comp = df_master[df_master["_compact"] == ex_comp]
    if not exact_comp.empty: return {"mapped_fund_name": exact_comp.iloc[0][fund_col], "mapping_status": "Exact Compact", "mapping_score": 100}

    best_name, best_score = extracted_name, 0
    for _, row in df_master.iterrows():
        if ex_token and get_first_meaningful_token(row[fund_col]) != ex_token:
            continue
        score = max(SequenceMatcher(None, ex_norm, row["_norm"]).ratio(), SequenceMatcher(None, ex_comp, row["_compact"]).ratio()) * 100
        if score > best_score: best_score, best_name = score, row[fund_col]

    if best_score >= fuzzy_threshold:
        return {"mapped_fund_name": best_name, "mapping_status": "Fuzzy Match", "mapping_score": round(best_score, 2)}
    
    return {"mapped_fund_name": extracted_name, "mapping_status": "No Match", "mapping_score": round(best_score, 2)}

# ============================================================
# Core OCR & Region Functions
# ============================================================

def run_easyocr(reader, img):
    if reader is None or img is None: return []
    results = reader.readtext(img, detail=1, paragraph=False)
    items = []
    for bbox, text, conf in results:
        xs, ys = [float(p[0]) for p in bbox], [float(p[1]) for p in bbox]
        items.append({
            "text": normalize_text(text), "raw_text": str(text), "confidence": float(conf),
            "bbox": bbox, "x1": min(xs), "y1": min(ys), "x2": max(xs), "y2": max(ys),
            "cx": float(np.mean(xs)), "cy": float(np.mean(ys)), "w": max(xs) - min(xs), "h": max(ys) - min(ys)
        })
    return items

def targeted_crop_ocr_retry(reader, img, table_rect):
    """Blazing fast fallback: OCR only the cropped table area with enhancement."""
    if reader is None or img is None or not table_rect: return []
    
    H, W = img.shape[:2]
    tx1, ty1, tx2, ty2 = table_rect
    # Pad crop slightly
    x1, y1 = max(0, tx1 - 20), max(0, ty1 - 20)
    x2, y2 = min(W, tx2 + 20), min(H, ty2 + 20)
    
    crop = img[y1:y2, x1:x2]
    if crop.size == 0: return []

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    
    # Upscale crop for small text readability
    scale = 3.0
    enlarged = cv2.resize(enhanced, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    
    results = reader.readtext(enlarged, detail=1, paragraph=False, allowlist="ABCIL123-|", text_threshold=0.3)
    
    items = []
    for bbox, text, conf in results:
        orig_bbox = [[(float(p[0])/scale) + x1, (float(p[1])/scale) + y1] for p in bbox]
        xs, ys = [p[0] for p in orig_bbox], [p[1] for p in orig_bbox]
        items.append({
            "text": normalize_text(text), "raw_text": str(text), "confidence": float(conf),
            "bbox": orig_bbox, "x1": min(xs), "y1": min(ys), "x2": max(xs), "y2": max(ys),
            "cx": float(np.mean(xs)), "cy": float(np.mean(ys)), "w": max(xs) - min(xs), "h": max(ys) - min(ys)
        })
    return items

def find_prc_table_area(img, ocr_items):
    H, W = img.shape[:2]
    keywords = ["POTENTIAL RISK", "MATRIX", "CREDIT RISK", "INTEREST RATE", "CLASS A", "CLASS B", "CLASS C", "RELATIVELY LOW", "RELATIVELY HIGH", "MODERATE"]
    k_items = [i for i in ocr_items if any(k in i["text"] for k in keywords)]
    
    if k_items:
        return (
            max(0, int(min(i["x1"] for i in k_items) - 40)),
            max(0, int(min(i["y1"] for i in k_items) - 50)),
            min(W, int(max(i["x2"] for i in k_items) + 100)),
            min(H, int(max(i["y2"] for i in k_items) + 220))
        )
    return (0, 0, W, int(H * 0.95))

def infer_prc_from_position(img, table_rect, cx, cy, known_col=None):
    tx1, ty1, tx2, ty2 = table_rect
    if not (tx1 <= cx <= tx2 and ty1 <= cy <= ty2): return None

    H, W = img.shape[:2]
    # Estimate grid excluding headers (left 25% usually row labels, top 30% col labels)
    matrix_x1 = max(tx1, int(tx1 + (tx2-tx1)*0.25))
    matrix_y1 = max(ty1, int(ty1 + (ty2-ty1)*0.30))
    
    if not (matrix_x1 <= cx <= tx2 and matrix_y1 <= cy <= ty2): return None

    col_idx = max(0, min(2, int((cx - matrix_x1) / ((tx2 - matrix_x1) / 3.0))))
    row_idx = max(0, min(2, int((cy - matrix_y1) / ((ty2 - matrix_y1) / 3.0))))

    col = known_col if known_col else ["A", "B", "C"][col_idx]
    row = ["I", "II", "III"][row_idx]
    
    return f"{col}-{row}" if f"{col}-{row}" in VALID_PRC_VALUES else None

# ============================================================
# Logic Flow
# ============================================================

def scan_items_for_prc(items, img, table_rect, method_prefix):
    candidates = []
    for item in items:
        prcs = extract_all_prc_from_text(item["text"])
        if prcs:
            candidates.append({"prc": prcs[0], "item": item, "method": f"{method_prefix} - Direct Match", "score": item["confidence"] * 100})
            continue
        
        # Check partials (e.g. "B-") and infer row from Y coordinate
        partial = extract_partial_prc_column(item["text"])
        if partial:
            inferred = infer_prc_from_position(img, table_rect, item["cx"], item["cy"], partial)
            if inferred:
                candidates.append({"prc": inferred, "item": item, "method": f"{method_prefix} - Partial Inference", "score": (item["confidence"] * 100) - 10})

    if candidates:
        return sorted(candidates, key=lambda x: x["score"], reverse=True)[0]
    return None

def extract_prc_from_image(image_path, reader=None, debug=True, debug_folder=DEBUG_FOLDER):
    img = read_image_cv(image_path)
    file_fund_name = clean_scheme_name(Path(image_path).stem)
    
    if img is None:
        return {"file_fund_name": file_fund_name, "status": "Image read failed"}

    # 1. Primary Full-Page OCR
    ocr_items = run_easyocr(reader, img)
    table_rect = find_prc_table_area(img, ocr_items)
    extracted_text = " | ".join([x["text"] for x in ocr_items])

    # 2. Check full page OCR results inside the table bounding box
    table_items = [i for i in ocr_items if table_rect[0] <= i["cx"] <= table_rect[2] and table_rect[1] <= i["cy"] <= table_rect[3]]
    best_match = scan_items_for_prc(table_items, img, table_rect, "Pass 1")

    # 3. Targeted Crop Retry (Extremely fast, highly accurate for dense/small text)
    if not best_match:
        crop_items = targeted_crop_ocr_retry(reader, img, table_rect)
        if crop_items:
            extracted_text += " | " + " | ".join([x["text"] for x in crop_items])
            best_match = scan_items_for_prc(crop_items, img, table_rect, "Crop Retry")

    # Finalize Output
    if best_match:
        col, row = best_match["prc"].split("-")
        
        if debug:
            os.makedirs(debug_folder, exist_ok=True)
            dbg = img.copy()
            x1, y1, x2, y2 = table_rect
            cv2.rectangle(dbg, (x1, y1), (x2, y2), (255, 0, 0), 2)
            ix1, iy1, ix2, iy2 = map(int, [best_match["item"]["x1"], best_match["item"]["y1"], best_match["item"]["x2"], best_match["item"]["y2"]])
            cv2.rectangle(dbg, (ix1, iy1), (ix2, iy2), (0, 255, 0), 3)
            cv2.putText(dbg, f"PRC: {best_match['prc']}", (20, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
            cv2.imwrite(str(Path(debug_folder) / f"{Path(image_path).stem}_debug.jpg"), dbg)

        return {
            "file_fund_name": file_fund_name,
            "prc_class": best_match["prc"],
            "credit_risk_class": CLASS_COL_MAP.get(col, ""),
            "interest_rate_risk_class": CLASS_ROW_MAP.get(row, ""),
            "status": "Detected",
            "method": best_match["method"],
            "confidence": best_match["score"],
            "source_text": best_match["item"]["text"],
            "extracted_text": extracted_text
        }

    return {
        "file_fund_name": file_fund_name,
        "prc_class": "", "credit_risk_class": "", "interest_rate_risk_class": "",
        "status": "PRC not detected", "method": "Failed", "confidence": 0,
        "source_text": "", "extracted_text": extracted_text
    }

# ============================================================
# Batch Processor
# ============================================================

def process_prc_folder_with_easyocr(root_folder, scheme_master_path=None, output_excel="PRC_Output.xlsx"):
    image_files = [p for ext in SUPPORTED_EXTENSIONS for p in Path(root_folder).rglob(f"*{ext}") if DEBUG_FOLDER.lower() not in str(p).lower()]
    
    if not image_files:
        print("No image files found.")
        return

    df_master = load_scheme_master(scheme_master_path)
    reader = load_easyocr()
    if reader is None: return

    results = []
    for idx, image_path in enumerate(image_files, start=1):
        print(f"[{idx}/{len(image_files)}] Processing: {image_path}")
        try:
            det = extract_prc_from_image(image_path, reader)
            master_map = map_to_scheme_master(det.get("file_fund_name", ""), df_master)
            
            final_status = "Matched" if det.get("prc_class") and master_map["mapping_status"] != "No Match" else "Not matched"

            results.append({
                "Fund Name": master_map["mapped_fund_name"],
                "PRC Score": det.get("prc_class", ""),
                "Final Status": final_status,
                "Master Mapping Status": master_map["mapping_status"],
                "Image File Name": Path(image_path).name,
                "Detection Status": det.get("status", ""),
                "Method": det.get("method", ""),
                "Confidence": det.get("confidence", 0)
            })
        except Exception as e:
            print(f"Error on {image_path}: {e}")

    df = pd.DataFrame(results).sort_values(by=["Final Status", "Fund Name"])
    df.to_excel(output_excel, index=False)
    print(f"\nCompleted. Saved to: {output_excel}")

if __name__ == "__main__":
    process_prc_folder_with_easyocr(
        root_folder=r"../Riskometer & PRC Automation/Automated_image_data",
        scheme_master_path=r"../Riskometer & PRC Automation/Scheme_master_for_Fund.xlsx",
        output_excel="PRC_testing.xlsx"
    )
