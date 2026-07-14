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

# Handles:
# A-I, A I, A-L, A-IL, A-ILI, B-ILI, C-ILI, C III, C 3, C - III
FUZZY_PRC_PATTERN = r"\b([ABC])\s*[-–—]?\s*(III|II|I|[I1L|]{1,3}|1|2|3)\b"

# Handles partial OCR:
# B-
# B -
# C-
# A -
PARTIAL_PRC_PATTERN = r"\b([ABC])\s*[-–—]\s*$|\b([ABC])\s*[-–—]\s+"


# ============================================================
# EasyOCR Loader
# ============================================================

def load_easyocr():
    try:
        import easyocr
        reader = easyocr.Reader(["en"], gpu=False)
        return reader
    except Exception as e:
        print("EasyOCR could not be loaded.")
        print("Install using: pip install easyocr")
        print("Error:", e)
        return None


# ============================================================
# Safe Image Read
# ============================================================

def read_image_cv(image_path):
    try:
        image_path = str(image_path)
        data = np.fromfile(image_path, dtype=np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_COLOR)
        return img
    except Exception:
        return cv2.imread(str(image_path))


# ============================================================
# Text Normalization
# ============================================================

def normalize_text(text):
    if text is None:
        return ""

    text = str(text).upper()

    replacements = {
        "—": "-",
        "–": "-",
        "−": "-",
        "_": "-",
        "|": "I",
        "!": "I",
        "Ⅰ": "I",
        "Ⅱ": "II",
        "Ⅲ": "III",
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "（": "(",
        "）": ")",
        "Ｃ": "C",
        "Ｂ": "B",
        "Ａ": "A",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_name_for_match(name):
    """
    Strong normalization for scheme/fund matching.
    Keeps numbers and alphabets only.
    """
    if name is None:
        return ""

    text = str(name).upper()
    text = text.replace("&amp;", " AND ")
    text = text.replace("&", " AND ")
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    remove_words = {
        "PRC", "RISK", "RISKOMETER", "SCREENSHOT", "IMAGE",
        "PAGE", "COPY", "FINAL", "TABLE", "DEBUG", "OUTPUT",
        "MATRIX", "POTENTIAL", "CLASS"
    }

    words = [w for w in text.split() if w not in remove_words]
    return " ".join(words).strip()


def compact_name_for_match(name):
    return re.sub(r"[^A-Z0-9]", "", normalize_name_for_match(name))


def normalize_roman_ocr(raw_row):
    """
    Converts OCR-confused Roman numerals into I / II / III.

    Examples:
    L     -> I
    IL    -> II
    LI    -> II
    LL    -> II
    ILI   -> III
    B-ILI -> B-III
    """

    if raw_row is None:
        return None

    row = str(raw_row).upper().strip()

    replacements = {
        "|": "I",
        "!": "I",
        "1": "I",
        "L": "I",
        "l": "I",
        "Ⅲ": "III",
        "Ⅱ": "II",
        "Ⅰ": "I"
    }

    for old, new in replacements.items():
        row = row.replace(old, new)

    row = re.sub(r"[^I123]", "", row)

    if row == "1":
        return "I"

    if row == "2":
        return "II"

    if row == "3":
        return "III"

    i_count = row.count("I")

    if i_count == 1:
        return "I"

    if i_count == 2:
        return "II"

    if i_count >= 3:
        return "III"

    return None


def normalize_prc(text):
    text = normalize_text(text)
    m = re.search(FUZZY_PRC_PATTERN, text)

    if not m:
        return None

    col = m.group(1)
    raw_row = m.group(2)

    row = normalize_roman_ocr(raw_row)

    if not row:
        return None

    prc = f"{col}-{row}"

    if prc in VALID_PRC_VALUES:
        return prc

    return None

def deduplicate_prc_retry_items(items, center_tolerance=25):
    """
    Removes duplicate PRC detections produced by different
    preprocessing variants.
    """

    if not items:
        return []

    # Keep higher-confidence detections first.
    items = sorted(
        items,
        key=lambda item: item.get("confidence", 0),
        reverse=True
    )

    unique_items = []

    for item in items:
        item_prcs = extract_all_prc_from_text(item["text"])
        item_partial = extract_partial_prc_column(item["text"])

        item_key = (
            item_prcs[0]
            if item_prcs
            else f"{item_partial}-PARTIAL"
        )

        duplicate_found = False

        for existing in unique_items:
            existing_prcs = extract_all_prc_from_text(
                existing["text"]
            )
            existing_partial = extract_partial_prc_column(
                existing["text"]
            )

            existing_key = (
                existing_prcs[0]
                if existing_prcs
                else f"{existing_partial}-PARTIAL"
            )

            same_text = item_key == existing_key

            close_position = (
                abs(item["cx"] - existing["cx"]) <= center_tolerance
                and
                abs(item["cy"] - existing["cy"]) <= center_tolerance
            )

            if same_text and close_position:
                duplicate_found = True
                break

        if not duplicate_found:
            unique_items.append(item)

    return unique_items

def detect_prc_enhanced_retry(reader, img, original_ocr_items):
    """
    Last OCR fallback for small or low-contrast PRC text.

    Existing OCR results and retry OCR results are combined,
    then passed through the existing PRC detection functions.
    """

    retry_items = run_easyocr_prc_retry(reader, img)

    if not retry_items:
        return None, [], original_ocr_items

    combined_items = list(original_ocr_items) + retry_items

    # First reuse the existing general detector.
    result, candidates = detect_prc_dynamic_general(
        img,
        combined_items
    )

    if result:
        result["method"] = (
            "Enhanced OCR retry - "
            + result.get("method", "")
        )
        return result, candidates, combined_items

    # Then reuse the existing ultimate fallback.
    result, candidates = detect_prc_ultimate_anywhere(
        img,
        combined_items
    )

    if result:
        result["method"] = (
            "Enhanced OCR retry - "
            + result.get("method", "")
        )
        return result, candidates, combined_items

    return None, [], combined_items


def extract_all_prc_from_text(text):
    """
    Extracts all complete PRC values from OCR text.
    Handles fuzzy values like B-ILI -> B-III.
    """

    text = normalize_text(text)

    matches = re.finditer(FUZZY_PRC_PATTERN, text)
    prc_values = []

    for m in matches:
        col = m.group(1)
        raw_row = m.group(2)

        row = normalize_roman_ocr(raw_row)

        if not row:
            continue

        prc = f"{col}-{row}"

        if prc in VALID_PRC_VALUES:
            prc_values.append(prc)

    return list(dict.fromkeys(prc_values))


def extract_partial_prc_column(text):
    """
    Captures partial PRC OCR values:
    B-
    B -
    C-
    A -

    Returns only column A/B/C.
    """

    text = normalize_text(text)

    m = re.search(PARTIAL_PRC_PATTERN, text)

    if not m:
        return None

    col = m.group(1) or m.group(2)

    if col in {"A", "B", "C"}:
        return col

    return None


# ============================================================
# File Name / Fund Name Helpers
# ============================================================

def clean_scheme_name(name):
    name = Path(str(name)).stem
    name = re.sub(r"[_\-]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()

    remove_words = {
        "prc", "risk", "riskometer", "page", "screenshot",
        "image", "final", "copy", "table", "potential",
        "class", "output", "debug", "automated", "matrix"
    }

    words = name.split()
    words = [w for w in words if w.lower() not in remove_words]

    cleaned = " ".join(words).strip()
    return cleaned if cleaned else name


def get_file_fund_name(image_path):
    """
    Main fund name logic:
    JM Liquid Fund.png -> JM Liquid Fund
    360 ONE Dynamic Bond Fund.png -> 360 ONE Dynamic Bond Fund
    """
    return clean_scheme_name(Path(image_path).stem)


def get_scheme_name_from_path(image_path):
    image_path = Path(image_path)

    file_name_clean = clean_scheme_name(image_path.stem)
    folder_name_clean = clean_scheme_name(image_path.parent.name)

    if len(file_name_clean) >= 5:
        return file_name_clean

    return folder_name_clean


# ============================================================
# Scheme Master Mapping
# ============================================================

def load_scheme_master(scheme_master_path, fund_col="Fund Name"):
    """
    Loads Scheme master Excel and returns cleaned master database.
    """

    if scheme_master_path is None or str(scheme_master_path).strip() == "":
        return pd.DataFrame(columns=[fund_col])

    scheme_master_path = Path(scheme_master_path)

    if not scheme_master_path.exists():
        print(f"Scheme master not found: {scheme_master_path}")
        return pd.DataFrame(columns=[fund_col])

    df_master = pd.read_excel(scheme_master_path, engine="openpyxl")

    if fund_col not in df_master.columns:
        raise ValueError(
            f"Column '{fund_col}' not found in Scheme master. "
            f"Available columns: {list(df_master.columns)}"
        )

    df_master[fund_col] = df_master[fund_col].astype(str).str.strip()
    df_master = df_master[df_master[fund_col].str.len() > 0].copy()

    df_master = df_master.drop_duplicates(subset=[fund_col]).reset_index(drop=True)

    df_master["_norm"] = df_master[fund_col].apply(normalize_name_for_match)
    df_master["_compact"] = df_master[fund_col].apply(compact_name_for_match)

    return df_master


def get_first_meaningful_token(name):
    """
    Gets first token / AMC name from fund name.

    Example:
    DSP CRISIL-IBX Financial Services 3-6 Months Debt Index Fund -> DSP
    HDFC CRISIL-IBX Financial Services 9-12 Months Debt Index Fund -> HDFC
    SBI CRISIL IBX Financial Services 3 6 Months Debt Index Fund -> SBI
    360 ONE Dynamic Bond Fund -> 360
    """

    norm = normalize_name_for_match(name)

    if not norm:
        return ""

    tokens = norm.split()

    if not tokens:
        return ""

    return tokens[0]

def map_to_scheme_master(extracted_name, df_master, fund_col="Fund Name", fuzzy_threshold=98):
    """
    Strict fund name mapping.

    Logic:
    1. Exact normalized match
    2. Exact compact match
    3. Fuzzy match only if score >= 98
    4. Fuzzy match allowed only when first AMC/token is same
    5. If no match, keep original file name

    This prevents wrong mapping like:
    DSP ... -> SBI ...
    HDFC ... -> SBI ...
    """

    if df_master is None or df_master.empty:
        return {
            "mapped_fund_name": extracted_name,
            "mapping_status": "Scheme master not used / not found",
            "mapping_score": 0
        }

    extracted_norm = normalize_name_for_match(extracted_name)
    extracted_compact = compact_name_for_match(extracted_name)
    extracted_first_token = get_first_meaningful_token(extracted_name)

    if not extracted_norm:
        return {
            "mapped_fund_name": extracted_name,
            "mapping_status": "Blank extracted name",
            "mapping_score": 0
        }

    # ============================================================
    # 1. Exact normalized match
    # ============================================================

    exact = df_master[df_master["_norm"] == extracted_norm]

    if not exact.empty:
        return {
            "mapped_fund_name": exact.iloc[0][fund_col],
            "mapping_status": "Exact master match",
            "mapping_score": 100
        }

    # ============================================================
    # 2. Exact compact match
    # ============================================================

    exact_compact = df_master[df_master["_compact"] == extracted_compact]

    if not exact_compact.empty:
        return {
            "mapped_fund_name": exact_compact.iloc[0][fund_col],
            "mapping_status": "Exact compact master match",
            "mapping_score": 100
        }

    # ============================================================
    # 3. Strict fuzzy match
    # ============================================================

    best_name = extracted_name
    best_score = 0

    for _, row in df_master.iterrows():
        master_name = row[fund_col]
        master_norm = row["_norm"]
        master_compact = row["_compact"]
        master_first_token = get_first_meaningful_token(master_name)

        # Very important safety check:
        # Do not allow different AMC/fund house mapping.
        # DSP should not map to SBI.
        # HDFC should not map to SBI.
        if extracted_first_token and master_first_token:
            if extracted_first_token != master_first_token:
                continue

        score1 = SequenceMatcher(None, extracted_norm, master_norm).ratio() * 100
        score2 = SequenceMatcher(None, extracted_compact, master_compact).ratio() * 100

        score = max(score1, score2)

        if score > best_score:
            best_score = score
            best_name = master_name

    # Only accept fuzzy if score is >= 98
    if best_score >= fuzzy_threshold:
        return {
            "mapped_fund_name": best_name,
            "mapping_status": f"Strict fuzzy master match >= {fuzzy_threshold}%",
            "mapping_score": round(best_score, 2)
        }

    # ============================================================
    # 4. No reliable match
    # Keep original file name
    # ============================================================

    return {
        "mapped_fund_name": extracted_name,
        "mapping_status": f"No reliable master match below {fuzzy_threshold}%",
        "mapping_score": round(best_score, 2)
    }
# ============================================================
# OCR
# ============================================================

def run_easyocr_prc_retry(reader, img):
    """
    Additional OCR pass for small PRC text such as:
    A-I, B-I, C-I, A-II, B-III, etc.

    This does not replace the existing OCR logic.
    It should be called only when existing detection fails.
    """

    if reader is None or img is None:
        return []

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # Improve local contrast, particularly inside grey highlighted cells.
    clahe = cv2.createCLAHE(
        clipLimit=2.5,
        tileGridSize=(8, 8)
    )
    enhanced = clahe.apply(gray)

    image_variants = []

    for scale in (3.0, 4.0):
        enlarged = cv2.resize(
            enhanced,
            None,
            fx=scale,
            fy=scale,
            interpolation=cv2.INTER_CUBIC
        )

        # Variant 1: enlarged grayscale
        image_variants.append((enlarged, scale, "CLAHE"))

        # Variant 2: Otsu threshold
        _, otsu = cv2.threshold(
            enlarged,
            0,
            255,
            cv2.THRESH_BINARY + cv2.THRESH_OTSU
        )
        image_variants.append((otsu, scale, "OTSU"))

        # Variant 3: adaptive threshold
        adaptive = cv2.adaptiveThreshold(
            enlarged,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            31,
            9
        )
        image_variants.append((adaptive, scale, "ADAPTIVE"))

    retry_items = []

    for processed_img, scale, variant_name in image_variants:
        try:
            results = reader.readtext(
                processed_img,
                detail=1,
                paragraph=False,

                # Helps EasyOCR detect small text.
                text_threshold=0.35,
                low_text=0.20,
                link_threshold=0.20,
                mag_ratio=2.0,

                # Characters expected in PRC values.
                allowlist="ABCIL123-|"
            )

        except Exception:
            continue

        for bbox, text, conf in results:
            try:
                # Convert enlarged-image coordinates back to
                # original-image coordinates.
                original_bbox = [
                    [
                        float(point[0]) / scale,
                        float(point[1]) / scale
                    ]
                    for point in bbox
                ]

                xs = [point[0] for point in original_bbox]
                ys = [point[1] for point in original_bbox]

                normalized_text = normalize_text(text)

                # Keep only OCR boxes that contain a complete
                # or partial PRC possibility.
                complete_prcs = extract_all_prc_from_text(normalized_text)
                partial_col = extract_partial_prc_column(normalized_text)

                if not complete_prcs and not partial_col:
                    continue

                x1 = min(xs)
                y1 = min(ys)
                x2 = max(xs)
                y2 = max(ys)

                retry_items.append({
                    "text": normalized_text,
                    "raw_text": str(text),
                    "confidence": float(conf),
                    "bbox": original_bbox,
                    "x1": x1,
                    "y1": y1,
                    "x2": x2,
                    "y2": y2,
                    "cx": float(np.mean(xs)),
                    "cy": float(np.mean(ys)),
                    "w": x2 - x1,
                    "h": y2 - y1,
                    "ocr_variant": variant_name,
                    "ocr_scale": scale
                })

            except Exception:
                continue

    return deduplicate_prc_retry_items(retry_items)

def run_easyocr(reader, image_path):
    if reader is None:
        return []

    img = read_image_cv(image_path)

    if img is None:
        return []

    try:
        results = reader.readtext(img, detail=1, paragraph=False)
    except Exception:
        return []

    items = []

    for bbox, text, conf in results:
        try:
            xs = [float(p[0]) for p in bbox]
            ys = [float(p[1]) for p in bbox]

            x1 = min(xs)
            y1 = min(ys)
            x2 = max(xs)
            y2 = max(ys)

            item = {
                "text": normalize_text(text),
                "raw_text": str(text),
                "confidence": float(conf),
                "bbox": bbox,
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
                "cx": float(np.mean(xs)),
                "cy": float(np.mean(ys)),
                "w": x2 - x1,
                "h": y2 - y1
            }

            items.append(item)

        except Exception:
            continue

    return items


# ============================================================
# OCR Line Grouping
# ============================================================

def group_ocr_items_into_lines(ocr_items, y_tolerance=18):
    """
    Groups OCR boxes into visual text lines.
    Helps when EasyOCR splits fund name into multiple boxes.
    """

    if not ocr_items:
        return []

    sorted_items = sorted(ocr_items, key=lambda x: (x["cy"], x["cx"]))
    lines = []

    for item in sorted_items:
        placed = False

        for line in lines:
            if abs(item["cy"] - line["cy"]) <= y_tolerance:
                line["items"].append(item)

                xs1 = [i["x1"] for i in line["items"]]
                ys1 = [i["y1"] for i in line["items"]]
                xs2 = [i["x2"] for i in line["items"]]
                ys2 = [i["y2"] for i in line["items"]]

                line["x1"] = min(xs1)
                line["y1"] = min(ys1)
                line["x2"] = max(xs2)
                line["y2"] = max(ys2)
                line["cx"] = (line["x1"] + line["x2"]) / 2
                line["cy"] = np.mean([i["cy"] for i in line["items"]])
                placed = True
                break

        if not placed:
            lines.append({
                "items": [item],
                "x1": item["x1"],
                "y1": item["y1"],
                "x2": item["x2"],
                "y2": item["y2"],
                "cx": item["cx"],
                "cy": item["cy"]
            })

    final_lines = []

    for line in lines:
        line_items = sorted(line["items"], key=lambda x: x["cx"])
        text = " ".join([i["text"] for i in line_items])
        raw_text = " ".join([i["raw_text"] for i in line_items])
        conf = np.mean([i["confidence"] for i in line_items]) if line_items else 0

        line["text"] = normalize_text(text)
        line["raw_text"] = raw_text
        line["confidence"] = float(conf)
        line["w"] = line["x2"] - line["x1"]
        line["h"] = line["y2"] - line["y1"]

        final_lines.append(line)

    return sorted(final_lines, key=lambda x: (x["cy"], x["cx"]))


def find_target_fund_line(target_fund_name, ocr_items):
    """
    Finds OCR line that best matches file name fund.
    """

    lines = group_ocr_items_into_lines(ocr_items)

    target_norm = normalize_name_for_match(target_fund_name)
    target_compact = compact_name_for_match(target_fund_name)

    best_line = None
    best_score = 0

    for line in lines:
        line_text = line["text"]
        line_norm = normalize_name_for_match(line_text)
        line_compact = compact_name_for_match(line_text)

        if not line_norm:
            continue

        score1 = SequenceMatcher(None, target_norm, line_norm).ratio() * 100
        score2 = SequenceMatcher(None, target_compact, line_compact).ratio() * 100

        if target_compact and line_compact:
            if target_compact in line_compact or line_compact in target_compact:
                score2 = max(score2, 95)

        score = max(score1, score2)

        if score > best_score:
            best_score = score
            best_line = line

    if best_line is not None and best_score >= 70:
        target_first_token = get_first_meaningful_token(
            target_fund_name
        )

        line_first_token = get_first_meaningful_token(
            best_line["text"]
        )

        # Prevent:
        # UTI MONEY MARKET FUND -> CLASS I
        # TATA OVERNIGHT FUND   -> RELATIVELY LOW
        #
        # Allow the match only if the first meaningful token agrees.
        if (
            target_first_token
            and line_first_token
            and target_first_token != line_first_token
        ):
            return None

        best_line["fund_match_score"] = round(best_score, 2)
        return best_line

    return None


# ============================================================
# Table Area Detection
# ============================================================

def find_prc_table_area(img, ocr_items):
    H, W = img.shape[:2]

    keywords = [
        "POTENTIAL RISK",
        "POTENTIAL RISK CLASS",
        "POTENTIAL RISK CLASS MATRIX",
        "PRC",
        "MATRIX",
        "CREDIT RISK",
        "INTEREST RATE",
        "CLASS A",
        "CLASS B",
        "CLASS C",
        "CLASS I",
        "CLASS II",
        "CLASS III",
        "RELATIVELY LOW",
        "RELATIVELY HIGH",
        "MODERATE"
    ]

    keyword_items = []

    for item in ocr_items:
        text = item["text"]

        if any(k in text for k in keywords):
            keyword_items.append(item)

    if keyword_items:
        x1 = max(0, int(min(i["x1"] for i in keyword_items) - 40))
        y1 = max(0, int(min(i["y1"] for i in keyword_items) - 50))
        x2 = min(W, int(max(i["x2"] for i in keyword_items) + 100))
        y2 = min(H, int(max(i["y2"] for i in keyword_items) + 220))

        x1 = max(0, min(x1, int(W * 0.01)))
        x2 = W
        y2 = min(H, max(y2, int(H * 0.90)))

        return (x1, y1, x2, y2)

    return (0, 0, W, int(H * 0.95))


def inside_rect(cx, cy, rect):
    x1, y1, x2, y2 = rect
    return x1 <= cx <= x2 and y1 <= cy <= y2


# ============================================================
# Filled / Highlight Mask
# ============================================================

def get_colored_or_filled_mask(img):
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]

    color_mask = ((sat > 35) & (val > 50)).astype(np.uint8) * 255
    grey_mask = ((gray > 80) & (gray < 245)).astype(np.uint8) * 255

    combined = cv2.bitwise_or(color_mask, grey_mask)

    combined = cv2.morphologyEx(
        combined,
        cv2.MORPH_CLOSE,
        np.ones((7, 7), np.uint8),
        iterations=1
    )

    return combined


def fill_score_around_text(mask, item, pad=12):
    H, W = mask.shape[:2]

    x1 = max(0, int(item["x1"]) - pad)
    y1 = max(0, int(item["y1"]) - pad)
    x2 = min(W, int(item["x2"]) + pad)
    y2 = min(H, int(item["y2"]) + pad)

    crop = mask[y1:y2, x1:x2]

    if crop.size == 0:
        return 0.0

    return float(np.mean(crop > 0))


# ============================================================
# PRC Position Inference
# ============================================================

def infer_prc_from_position(img, ocr_items, cx, cy, known_col=None):
    """
    Infers PRC from matrix cell position.

    Useful when OCR captures only:
    B-
    C-
    A-
    """

    if img is None:
        return None

    H, W = img.shape[:2]
    table_rect = find_prc_table_area(img, ocr_items)

    tx1, ty1, tx2, ty2 = table_rect

    if not inside_rect(cx, cy, table_rect):
        return None

    # Left part is row-label area.
    # Remaining part is A/B/C columns.
    matrix_x1 = max(tx1, int(W * 0.24))
    matrix_x2 = tx2

    # Header/title is top area.
    # Body has I/II/III rows.
    matrix_y1 = max(ty1, int(H * 0.32))
    matrix_y2 = min(ty2, int(H * 0.95))

    if not (matrix_x1 <= cx <= matrix_x2 and matrix_y1 <= cy <= matrix_y2):
        return None

    col_width = (matrix_x2 - matrix_x1) / 3.0
    row_height = (matrix_y2 - matrix_y1) / 3.0

    col_idx = int((cx - matrix_x1) / col_width)
    row_idx = int((cy - matrix_y1) / row_height)

    col_idx = max(0, min(2, col_idx))
    row_idx = max(0, min(2, row_idx))

    col_map = ["A", "B", "C"]
    row_map = ["I", "II", "III"]

    col = known_col if known_col in {"A", "B", "C"} else col_map[col_idx]
    row = row_map[row_idx]

    prc = f"{col}-{row}"

    if prc in VALID_PRC_VALUES:
        return prc

    return None


def estimate_prc_from_matrix_position(fund_line, img, ocr_items):
    """
    If fund name is found but PRC text is unreadable,
    estimate PRC from the matrix cell where fund line is placed.
    """

    if fund_line is None or img is None:
        return None

    return infer_prc_from_position(
        img=img,
        ocr_items=ocr_items,
        cx=fund_line["cx"],
        cy=fund_line["cy"],
        known_col=None
    )


# ============================================================
# PRC From Target Fund Location
# ============================================================

def find_prc_near_target_fund(target_fund_name, img, ocr_items):
    """
    Main detection:
    1. Find fund name from file name inside image OCR.
    2. Extract PRC from same line or nearby text.
    3. If not found, estimate PRC using matrix position.
    """

    fund_line = find_target_fund_line(target_fund_name, ocr_items)

    if fund_line is None:
        return None, None, "Target fund name not found in OCR", 0

    same_line_prcs = extract_all_prc_from_text(fund_line["text"])

    if same_line_prcs:
        return (
            same_line_prcs[0],
            fund_line,
            "Target fund line OCR - PRC in same line",
            fund_line.get("fund_match_score", 0)
        )

    for item in fund_line.get("items", []):
        item_prcs = extract_all_prc_from_text(item["text"])

        if item_prcs:
            return (
                item_prcs[0],
                fund_line,
                "Target fund OCR item - PRC in same line",
                fund_line.get("fund_match_score", 0)
            )

    candidates = []

    fx1, fy1, fx2, fy2 = fund_line["x1"], fund_line["y1"], fund_line["x2"], fund_line["y2"]
    fcx, fcy = fund_line["cx"], fund_line["cy"]

    H, W = img.shape[:2]

    search_x_pad = max(180, W * 0.12)
    search_y_above = max(120, H * 0.18)
    search_y_below = max(60, H * 0.08)

    for item in ocr_items:
        text = item["text"]
        prcs = extract_all_prc_from_text(text)
        partial_col = extract_partial_prc_column(text)

        if not prcs and not partial_col:
            continue

        cx = item["cx"]
        cy = item["cy"]

        horizontally_related = (
            abs(cx - fcx) <= search_x_pad or
            (item["x1"] <= fx2 and item["x2"] >= fx1)
        )

        vertically_related = (
            fcy - search_y_above <= cy <= fcy + search_y_below
        )

        if horizontally_related and vertically_related:
            if prcs:
                prc = prcs[0]
            else:
                prc = infer_prc_from_position(
                    img=img,
                    ocr_items=ocr_items,
                    cx=cx,
                    cy=cy,
                    known_col=partial_col
                )

            if prc:
                distance = abs(cx - fcx) + abs(cy - fcy)
                confidence = item.get("confidence", 0) * 100
                score = confidence - distance * 0.15

                candidates.append({
                    "prc": prc,
                    "item": item,
                    "score": score,
                    "distance": distance
                })

    if candidates:
        candidates = sorted(candidates, key=lambda x: x["score"], reverse=True)
        best = candidates[0]

        return (
            best["prc"],
            fund_line,
            "PRC near target fund name in same matrix cell",
            fund_line.get("fund_match_score", 0)
        )

    estimated_prc = estimate_prc_from_matrix_position(fund_line, img, ocr_items)

    if estimated_prc:
        return (
            estimated_prc,
            fund_line,
            "Estimated from target fund matrix cell position",
            fund_line.get("fund_match_score", 0)
        )

    return None, fund_line, "Target fund found but PRC not detected", fund_line.get("fund_match_score", 0)


# ============================================================
# General PRC Detection Fallback
# ============================================================

def estimate_cell_position_score(item, img_shape):
    H, W = img_shape[:2]

    score = 0

    if item["cy"] < H * 0.08:
        score -= 30

    if item["cy"] > H * 0.94:
        score -= 45

    if item["cx"] > W * 0.20:
        score += 25

    if item["cy"] > H * 0.16:
        score += 20

    if item["w"] < 8 or item["h"] < 8:
        score -= 20

    return score


def score_prc_candidate_fuzzy(item, img, fill_mask, table_rect, forced_prc=None):
    prc = forced_prc or normalize_prc(item["text"])

    if not prc:
        return None

    if not inside_rect(item["cx"], item["cy"], table_rect):
        return None

    fill_score = fill_score_around_text(fill_mask, item, pad=12)
    position_score = estimate_cell_position_score(item, img.shape)

    text = normalize_text(item["text"])
    compact = re.sub(r"[^A-Z0-9]", "", text)

    short_prc_bonus = 0
    long_line_penalty = 0
    fuzzy_bonus = 0
    header_penalty = 0

    fuzzy_variants = [
        "A-L", "A-IL", "A-ILI",
        "B-L", "B-IL", "B-ILI",
        "C-L", "C-IL", "C-ILI"
    ]

    if any(v in text for v in fuzzy_variants):
        fuzzy_bonus += 35

    clean_compacts = {
        "AI", "A1", "AL",
        "AII", "AIL", "ALI", "ALL",
        "AIII", "AILI", "AIIL", "ALII", "ALLL",

        "BI", "B1", "BL",
        "BII", "BIL", "BLI", "BLL",
        "BIII", "BILI", "BIIL", "BLII", "BLLL",

        "CI", "C1", "CL",
        "CII", "CIL", "CLI", "CLL",
        "CIII", "CILI", "CIIL", "CLII", "CLLL"
    }

    if compact in clean_compacts:
        short_prc_bonus += 45
    elif len(text) <= 20:
        short_prc_bonus += 25
    else:
        long_line_penalty -= 12

    header_words = [
        "CLASS A", "CLASS B", "CLASS C",
        "CLASS I", "CLASS II", "CLASS III",
        "CREDIT RISK", "INTEREST RATE",
        "POTENTIAL RISK"
    ]

    if any(h in text for h in header_words) and len(text) > 30:
        header_penalty -= 12

    score = (
        item["confidence"] * 100
        + fill_score * 25
        + position_score
        + short_prc_bonus
        + fuzzy_bonus
        + long_line_penalty
        + header_penalty
    )

    return {
        "prc_class": prc,
        "confidence": round(min(99, max(60, score)), 2),
        "method": "General Dynamic Fuzzy OCR PRC Scan",
        "source_text": item["text"],
        "raw_text": item.get("raw_text", item["text"]),
        "bbox": (
            int(item["x1"]),
            int(item["y1"]),
            int(item["x2"]),
            int(item["y2"])
        ),
        "score": score,
        "fill_score": fill_score,
        "table_rect": table_rect,
        "ocr_item": item
    }


def detect_prc_dynamic_general(img, ocr_items):
    """
    General fallback.
    Used when target-fund-based detection fails.
    """

    if img is None or not ocr_items:
        return None, []

    table_rect = find_prc_table_area(img, ocr_items)
    fill_mask = get_colored_or_filled_mask(img)

    candidates = []

    for item in ocr_items:
        prc_values = extract_all_prc_from_text(item["text"])

        if not prc_values:
            continue

        for prc in prc_values:
            candidate = score_prc_candidate_fuzzy(
                item=item,
                img=img,
                fill_mask=fill_mask,
                table_rect=table_rect,
                forced_prc=prc
            )

            if candidate:
                candidates.append(candidate)

    if not candidates:
        return None, []

    candidates = sorted(candidates, key=lambda x: x["score"], reverse=True)
    best = candidates[0]

    result = {
        "prc_class": best["prc_class"],
        "confidence": best["confidence"],
        "method": best["method"],
        "source_text": best["source_text"],
        "raw_text": best["raw_text"],
        "bbox": best["bbox"],
        "table_rect": best["table_rect"],
        "fill_score": best["fill_score"],
        "ocr_item": best["ocr_item"]
    }

    return result, candidates


# ============================================================
# Ultimate Fallback For Difficult OCR
# ============================================================

def detect_prc_ultimate_anywhere(img, ocr_items):
    """
    Final fallback for difficult images.

    Handles:
    1. Full PRC anywhere in table:
       C-III, B-III, A-I

    2. Partial PRC anywhere in table:
       B-, C-, A-

    3. Long OCR lines:
       POTENTIAL RISK CLASS ... C-III

    4. B- captured with IDCW below:
       Completes B-III using matrix position.
    """

    if img is None or not ocr_items:
        return None, []

    table_rect = find_prc_table_area(img, ocr_items)

    full_candidates = []

    # ------------------------------------------------------------
    # Full PRC anywhere in matrix
    # ------------------------------------------------------------
    for item in ocr_items:
        if not inside_rect(item["cx"], item["cy"], table_rect):
            continue

        prcs = extract_all_prc_from_text(item["text"])

        if not prcs:
            continue

        for prc in prcs:
            score = item.get("confidence", 0) * 100

            compact = re.sub(r"[^A-Z0-9]", "", item["text"])

            if compact in {
                "AI", "AII", "AIII",
                "BI", "BII", "BIII",
                "CI", "CII", "CIII"
            }:
                score += 40

            header_words = [
                "CLASS A",
                "CLASS B",
                "CLASS C",
                "CREDIT RISK",
                "INTEREST RATE",
                "POTENTIAL RISK"
            ]

            if any(h in item["text"] for h in header_words) and len(item["text"]) > 35:
                score -= 20

            position_prc = infer_prc_from_position(
                img=img,
                ocr_items=ocr_items,
                cx=item["cx"],
                cy=item["cy"],
                known_col=None
            )

            if position_prc == prc:
                score += 20

            full_candidates.append({
                "prc_class": prc,
                "confidence": round(max(70, min(99, score)), 2),
                "method": "Ultimate fallback - full PRC found anywhere in matrix",
                "source_text": item["text"],
                "raw_text": item.get("raw_text", item["text"]),
                "bbox": (
                    int(item["x1"]),
                    int(item["y1"]),
                    int(item["x2"]),
                    int(item["y2"])
                ),
                "score": score,
                "table_rect": table_rect,
                "ocr_item": item
            })

    if full_candidates:
        full_candidates = sorted(full_candidates, key=lambda x: x["score"], reverse=True)
        best = full_candidates[0]
        return best, full_candidates

    # ------------------------------------------------------------
    # Partial PRC anywhere in matrix
    # Example: B- -> use position to infer B-III
    # ------------------------------------------------------------
    partial_candidates = []

    for item in ocr_items:
        if not inside_rect(item["cx"], item["cy"], table_rect):
            continue

        partial_col = extract_partial_prc_column(item["text"])

        if not partial_col:
            continue

        inferred_prc = infer_prc_from_position(
            img=img,
            ocr_items=ocr_items,
            cx=item["cx"],
            cy=item["cy"],
            known_col=partial_col
        )

        if not inferred_prc:
            continue

        score = item.get("confidence", 0) * 100 + 20

        partial_candidates.append({
            "prc_class": inferred_prc,
            "confidence": round(max(68, min(95, score)), 2),
            "method": "Ultimate fallback - partial PRC completed using matrix position",
            "source_text": item["text"],
            "raw_text": item.get("raw_text", item["text"]),
            "bbox": (
                int(item["x1"]),
                int(item["y1"]),
                int(item["x2"]),
                int(item["y2"])
            ),
            "score": score,
            "table_rect": table_rect,
            "ocr_item": item
        })

    if partial_candidates:
        partial_candidates = sorted(partial_candidates, key=lambda x: x["score"], reverse=True)
        best = partial_candidates[0]
        return best, partial_candidates

    return None, []


# ============================================================
# Debug Output
# ============================================================

def save_debug_output(
    img,
    image_path,
    debug_folder,
    result=None,
    candidates=None,
    fund_name="",
    target_fund_line=None
):
    os.makedirs(debug_folder, exist_ok=True)

    dbg = img.copy()

    if result and result.get("table_rect"):
        x1, y1, x2, y2 = result["table_rect"]
        cv2.rectangle(dbg, (x1, y1), (x2, y2), (255, 180, 0), 2)

    if candidates:
        for c in candidates:
            if "bbox" not in c:
                continue

            x1, y1, x2, y2 = c["bbox"]

            cv2.rectangle(dbg, (x1, y1), (x2, y2), (0, 200, 255), 2)
            cv2.putText(
                dbg,
                c.get("prc_class", ""),
                (x1, max(15, y1 - 5)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (0, 200, 255),
                2
            )

    if target_fund_line:
        x1 = int(target_fund_line["x1"])
        y1 = int(target_fund_line["y1"])
        x2 = int(target_fund_line["x2"])
        y2 = int(target_fund_line["y2"])

        cv2.rectangle(dbg, (x1, y1), (x2, y2), (255, 0, 255), 3)
        cv2.putText(
            dbg,
            "TARGET FUND",
            (x1, max(15, y1 - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (255, 0, 255),
            2
        )

    if result and result.get("bbox"):
        x1, y1, x2, y2 = result["bbox"]
        cv2.rectangle(dbg, (x1, y1), (x2, y2), (0, 255, 0), 4)

    label = "PRC: NOT_DETECTED"

    if result:
        label = f"PRC: {result['prc_class']} | {result['method']}"

    cv2.putText(
        dbg,
        label[:120],
        (20, 35),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (0, 0, 255),
        2
    )

    if fund_name:
        cv2.putText(
            dbg,
            f"Fund: {fund_name[:80]}",
            (20, 70),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 0, 255),
            2
        )

    image_name = Path(image_path).stem

    cv2.imwrite(
        str(Path(debug_folder) / f"{image_name}_debug.jpg"),
        dbg
    )


# ============================================================
# Single Image Extractor
# ============================================================

def extract_prc_from_image(image_path, reader=None, debug=True, debug_folder=DEBUG_FOLDER):
    img = read_image_cv(image_path)
    file_fund_name = get_file_fund_name(image_path)

    if img is None:
        return {
            "file_fund_name": file_fund_name,
            "ocr_matched_fund_name": "",
            "fund_match_score": 0,
            "prc_class": "",
            "credit_risk_class": "",
            "interest_rate_risk_class": "",
            "status": "Image read failed",
            "method": "",
            "confidence": 0,
            "source_text": "",
            "extracted_text": ""
        }

    ocr_items = run_easyocr(reader, image_path)
    extracted_text = " | ".join([x["text"] for x in ocr_items])

    # ------------------------------------------------------------
    # Main logic: file name as target fund
    # ------------------------------------------------------------
    target_prc, target_fund_line, target_method, fund_match_score = find_prc_near_target_fund(
        target_fund_name=file_fund_name,
        img=img,
        ocr_items=ocr_items
    )

    if target_prc:
        col, row = target_prc.split("-")

        result_for_debug = {
            "prc_class": target_prc,
            "method": target_method,
            "bbox": (
                int(target_fund_line["x1"]),
                int(target_fund_line["y1"]),
                int(target_fund_line["x2"]),
                int(target_fund_line["y2"])
            ),
            "table_rect": find_prc_table_area(img, ocr_items),
            "ocr_item": target_fund_line
        }

        if debug:
            save_debug_output(
                img=img,
                image_path=image_path,
                debug_folder=debug_folder,
                result=result_for_debug,
                candidates=[],
                fund_name=file_fund_name,
                target_fund_line=target_fund_line
            )

        return {
            "file_fund_name": file_fund_name,
            "ocr_matched_fund_name": target_fund_line.get("text", ""),
            "fund_match_score": fund_match_score,
            "prc_class": target_prc,
            "credit_risk_class": CLASS_COL_MAP.get(col, ""),
            "interest_rate_risk_class": CLASS_ROW_MAP.get(row, ""),
            "status": "Detected",
            "method": target_method,
            "confidence": max(75, fund_match_score),
            "source_text": target_fund_line.get("text", ""),
            "extracted_text": extracted_text
        }

    # ------------------------------------------------------------
    # Fallback 1: General dynamic PRC scan
    # ------------------------------------------------------------
    result, candidates = detect_prc_dynamic_general(img, ocr_items)

    # ------------------------------------------------------------
    # Fallback 2: Ultimate difficult OCR fallback
    # Handles C-III captured alone and B- partial capture
    # ------------------------------------------------------------
    if result is None:
        result, candidates = detect_prc_ultimate_anywhere(img, ocr_items)
    # ------------------------------------------------------------
    # Fallback 3: Enhanced OCR retry for small PRC text
    # ------------------------------------------------------------
    if result is None:
        retry_result, retry_candidates, retry_ocr_items = (
            detect_prc_enhanced_retry(
                reader=reader,
                img=img,
                original_ocr_items=ocr_items
            )
        )

        if retry_result is not None:
            result = retry_result
            candidates = retry_candidates
            ocr_items = retry_ocr_items

            # Update extracted OCR text to include retry results.
            extracted_text = " | ".join(
                [item["text"] for item in ocr_items]
            )


    if result:
        prc = result["prc_class"]
        col, row = prc.split("-")

        if debug:
            save_debug_output(
                img=img,
                image_path=image_path,
                debug_folder=debug_folder,
                result=result,
                candidates=candidates,
                fund_name=file_fund_name,
                target_fund_line=target_fund_line
            )

        return {
            "file_fund_name": file_fund_name,
            "ocr_matched_fund_name": target_fund_line.get("text", "") if target_fund_line else "",
            "fund_match_score": fund_match_score,
            "prc_class": prc,
            "credit_risk_class": CLASS_COL_MAP.get(col, ""),
            "interest_rate_risk_class": CLASS_ROW_MAP.get(row, ""),
            "status": "Detected - fallback PRC",
            "method": result["method"],
            "confidence": result["confidence"],
            "source_text": result.get("source_text", ""),
            "extracted_text": extracted_text
        }

    if debug:
        save_debug_output(
            img=img,
            image_path=image_path,
            debug_folder=debug_folder,
            result=None,
            candidates=candidates if "candidates" in locals() else [],
            fund_name=file_fund_name,
            target_fund_line=target_fund_line
        )

    return {
        "file_fund_name": file_fund_name,
        "ocr_matched_fund_name": target_fund_line.get("text", "") if target_fund_line else "",
        "fund_match_score": fund_match_score,
        "prc_class": "",
        "credit_risk_class": "",
        "interest_rate_risk_class": "",
        "status": "PRC not detected",
        "method": target_method,
        "confidence": 0,
        "source_text": "",
        "extracted_text": extracted_text
    }


# ============================================================
# Collect Images
# ============================================================

def collect_images(root_folder):
    root_folder = Path(root_folder)

    image_files = []

    for ext in SUPPORTED_EXTENSIONS:
        image_files.extend(root_folder.rglob(f"*{ext}"))
        image_files.extend(root_folder.rglob(f"*{ext.upper()}"))

    image_files = [
        p for p in image_files
        if DEBUG_FOLDER.lower() not in str(p).lower()
        and not p.stem.lower().endswith("_debug")
    ]

    return sorted(list(set(image_files)))


# ============================================================
# Batch Processor - Final + Detailed Sheets
# ============================================================

def process_prc_folder_with_easyocr(
    root_folder,
    scheme_master_path=None,
    scheme_master_fund_col="Fund Name",
    output_excel="PRC_Output_Final_Detailed.xlsx",
    debug=True
):
    root_folder = Path(root_folder)

    image_files = collect_images(root_folder)

    if not image_files:
        print("No image files found.")
        return

    print(f"Total images found: {len(image_files)}")
    print("Loading Scheme master...")

    df_master = load_scheme_master(
        scheme_master_path=scheme_master_path,
        fund_col=scheme_master_fund_col
    )

    print("Loading EasyOCR...")
    reader = load_easyocr()

    if reader is None:
        print("EasyOCR unavailable. Stopping.")
        return

    results = []

    for idx, image_path in enumerate(image_files, start=1):
        print(f"[{idx}/{len(image_files)}] Processing: {image_path}")

        try:
            detection = extract_prc_from_image(
                image_path=image_path,
                reader=reader,
                debug=debug,
                debug_folder=DEBUG_FOLDER
            )

            file_fund_name = detection["file_fund_name"]

            master_map = map_to_scheme_master(
                extracted_name=file_fund_name,
                df_master=df_master,
                fund_col=scheme_master_fund_col
            )

            mapped_fund_name = master_map["mapped_fund_name"]
            prc_class = detection["prc_class"]

            mapping_status = master_map["mapping_status"]

            is_master_matched = (
                mapping_status == "Exact master match"
                or mapping_status == "Exact compact master match"
                or mapping_status.startswith("Strict fuzzy master match")
            )
            if str(prc_class).strip() and is_master_matched:
                final_status = "Matched"
            else:
                final_status = "Not matched"

            results.append({
                "Fund Name": mapped_fund_name,
                "PRC Score": prc_class,
                "Final Status": final_status,

                "Scheme / Fund Name": mapped_fund_name,
                "File Fund Name": file_fund_name,
                "OCR Matched Fund Text": detection["ocr_matched_fund_name"],
                "Fund OCR Match Score": detection["fund_match_score"],

                "Master Mapping Status": mapping_status,
                "Master Mapping Score": master_map["mapping_score"],

                "Image File Name": Path(image_path).name,
                "Image Path": str(image_path),

                "PRC Class": prc_class,
                "Credit Risk Class": detection["credit_risk_class"],
                "Interest Rate Risk Class": detection["interest_rate_risk_class"],

                "Detection Status": detection["status"],
                "Detection Method": detection["method"],
                "Confidence Score": detection["confidence"],

                "Source Text": detection["source_text"],
                "Extracted OCR Text": detection["extracted_text"]
            })

        except Exception as e:
            file_fund_name = get_file_fund_name(image_path)

            results.append({
                "Fund Name": file_fund_name,
                "PRC Score": "",
                "Final Status": "Not matched",

                "Scheme / Fund Name": file_fund_name,
                "File Fund Name": file_fund_name,
                "OCR Matched Fund Text": "",
                "Fund OCR Match Score": 0,

                "Master Mapping Status": "Error",
                "Master Mapping Score": 0,

                "Image File Name": Path(image_path).name,
                "Image Path": str(image_path),

                "PRC Class": "",
                "Credit Risk Class": "",
                "Interest Rate Risk Class": "",

                "Detection Status": "Error",
                "Detection Method": "",
                "Confidence Score": 0,

                "Source Text": "",
                "Extracted OCR Text": str(e)
            })

            print("Error processing:", image_path)
            print(traceback.format_exc())

    df_detailed = pd.DataFrame(results)

    df_final = df_detailed[[
        "Fund Name",
        "PRC Score",
        "Final Status"
    ]].copy()

    df_final = df_final.sort_values(
        by=["Final Status", "Fund Name"],
        ascending=[True, True]
    ).reset_index(drop=True)

    df_detailed = df_detailed.sort_values(
        by=["Final Status", "Fund Name", "Image File Name"],
        ascending=[True, True, True]
    ).reset_index(drop=True)

    with pd.ExcelWriter(output_excel, engine="openpyxl") as writer:
        df_final.to_excel(writer, index=False, sheet_name="Final")
        df_detailed.to_excel(writer, index=False, sheet_name="Detailed")

        for sheet_name in ["Final", "Detailed"]:
            ws = writer.sheets[sheet_name]

            for column_cells in ws.columns:
                max_length = 0
                column_letter = column_cells[0].column_letter

                for cell in column_cells:
                    if cell.value is not None:
                        max_length = max(max_length, len(str(cell.value)))

                ws.column_dimensions[column_letter].width = min(max_length + 3, 90)

            ws.freeze_panes = "A2"

    print("\nProcessing completed.")
    print(f"Excel saved as: {output_excel}")

    if debug:
        print(f"Debug files saved in: {DEBUG_FOLDER}")


# ============================================================
# Optional Local Test
# ============================================================

def test_prc_normalization():
    test_texts = [
        "A-L",
        "A-IL",
        "A-ILI",
        "B-L",
        "B-IL",
        "B-ILI",
        "C-L",
        "C-IL",
        "C-ILI",
        "B I",
        "C III",
        "A 1",
        "B 2",
        "C 3",
        "JM Liquid Fund (B-I)",
        "360 ONE Dynamic Bond Fund C - III",
        "CREDIT RISK | RELATIVELY | LOW | MODERATE | B-ILI | HIGH",
        "CREDIT RISK | RELATIVELY | LOW | MODERATE | A-L | HIGH",
        "POTENTIAL RISK CLASS | CREDIT RISK | C-III",
        "POTENTIAL RISK CLASS MATRIX | CREDIT RISK - | B- | IDCW",
    ]

    for t in test_texts:
        print(
            t,
            "=> Full:",
            extract_all_prc_from_text(t),
            "| Partial:",
            extract_partial_prc_column(t)
        )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    ROOT_FOLDER = r"../Riskometer & PRC Automation/Automated_image_data"

    SCHEME_MASTER_PATH = r"../Riskometer & PRC Automation/Scheme_master_for_Fund.xlsx"

    # Uncomment this only for checking PRC normalization.
    # test_prc_normalization()

    process_prc_folder_with_easyocr(
        root_folder=ROOT_FOLDER,
        scheme_master_path=SCHEME_MASTER_PATH,
        scheme_master_fund_col="Fund Name",
        output_excel="PRC_testing.xlsx",
        debug=True
    )
