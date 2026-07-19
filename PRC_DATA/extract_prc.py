import pymupdf as fitz
import os
import sys
import re
import argparse
from PIL import Image
import html

# ---------------------------------------------------------------------------
# PATH CONFIG (EDIT HERE ONLY)
# ---------------------------------------------------------------------------

INPUT_DIR = r"../Riskometer & PRC Automation/factsheet April 2026"
OUTPUT_DIR = r"Automated_image_data"
ZOOM = 5.0


if sys.stdout is not None:
    # A windowed (console=False) PyInstaller build has no attached
    # console, so sys.stdout is None there. Calling .reconfigure() on
    # None used to crash the app immediately on import, before the GUI
    # window could even open.
    sys.stdout.reconfigure(encoding='utf-8')

# ---------------------------------------------------------------------------
# Shared utility constants and helpers
# ---------------------------------------------------------------------------

CELL_KEYWORDS = [
    "relatively low (class", "moderate (class", "relatively high (class",
    "(class a)", "(class b)", "(class c)",
    "(class i)", "(class ii)", "(class iii)",
    "relatively low (cl i)", "relatively low (cl a)", "moderate (cl b)",
    "moderate (cl ii)", "relatively high (cl c)", "relatively high (cl iii)",
    "relatively low (class i)",
    "relatively low: class a", "moderate: class b", "relatively high: class c",
    "relatively low: class i", "moderate: class ii", "relatively high: class iii",
    "relatively low - class a", "moderate - class b", "relatively high - class c",
    "relatively low - class i", "moderate - class ii", "relatively high - class iii",
    "relatively low class i", "moderate class ii", "relatively high class iii",
    "class a:", "class b:", "class c:",
    "class i:", "class ii:", "class iii:"
]


def clean_filename(name):
    """Cleans a string into a safe filename (max 150 chars)."""

    if not name:
        return ""

    # ✅ Fix &amp; globally
    name = html.unescape(name)   # &amp; → &
    name = name.replace("&", "and")
    
    # ✅ Convert Unicode en-dashes and em-dashes to standard hyphens
    name = name.replace("–", "-").replace("—", "-")

    # ✅ Existing cleaning logic
    cleaned = "".join([c if c.isalnum() or c in " _-" else "_" for c in name])

    # ✅ Normalize whitespace
    cleaned = " ".join(cleaned.split())

    return cleaned.rstrip("_- ").strip()[:150].strip()


def is_snapshot_page(page):
    """Returns True if the page is a table-of-contents or snapshot/index page."""
    text = page.get_text("text").lower()
    if "snapshot" in text[:400]:
        return True
    lines = [re.sub(r'^[\d\.\s]+', '', l).strip().lower() for l in text.split('\n') if l.strip()]
    if any(x in lines[:5] for x in ["index", "contents", "table of contents", "scheme index"]):
        return True
    if "contents" in text[:300] or "table of contents" in text[:400] or "index" in text[:400]:
        if "....." in text or "....  " in text or "page number" in text or "page no" in text:
            return True
    return False


def strip_fund_name(text):
    """
    Cleans a raw fund name candidate: strips category descriptions, disclaimers,
    scheme codes, performance data, and normalises whitespace.
    Returns a clean fund name string.
    """
    if not text:
        return ""
    raw_lines = text.split('\n')
    cleaned_lines = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        line_lower = line.lower()
        if any(x in line_lower for x in [
            "funds managed by", "fund manager", "investment objective", 
            "suitable for", "benchmark", "nav as on", "mr.", "mr ", 
            "ms.", "ms ", "minimum investment", "aum as on", "•", "option"
        ]):
            break
        cleaned_lines.append(line)
        
    if not cleaned_lines:
        return ""
        
    name = " ".join(cleaned_lines).strip()
    name = " ".join(name.split())

    # ✅ Strip any "(formerly known as ...)" or "(Erstwhile ...)" parenthetical clauses
    name = re.sub(r'\s*\((?:formerly known as|formerly|erstwhile).*?\)', '', name, flags=re.IGNORECASE)

    # ✅ Case-insensitive open-ended and product label delimiter stripping
    name_lower = name.lower()
    for delim in ["an open", "open ended", "open-ended", "open "]:
        idx = name_lower.find(delim)
        if idx > 0:
            name = name[:idx].strip()
            name_lower = name.lower()
            
    for delim in ["this product", "*investors"]:
        idx = name_lower.find(delim)
        if idx > 0:
            name = name[:idx].strip()
            name_lower = name.lower()
            
    name = name.rstrip("-–—_ ").strip()
    name = name[:180].strip()
    return name


CURRENT_PDF_MAPPINGS = {}

def find_fund_rect_on_page(page, fund_name):
    """
    Finds the best fitz.Rect bounding box for a fund name on the page.
    Groups close blocks if the fund name is multi-line.
    """
    if not fund_name:
        return None

    # Try searching for exact text first
    rects = page.search_for(fund_name)
    if rects:
        return rects[0]

    # Normalize fund_name into lower-case words
    ignore_words = {'and', 'the', 'of', 'for', 'in', 'a', 'to', '&', '-', '–', '/'}
    target_words = [w.lower().strip(".,:;()-") for w in fund_name.split()]
    target_words = [w for w in target_words if w and w not in ignore_words]
    
    if not target_words:
        return None

    blocks = page.get_text("blocks")
    matched_blocks = []
    
    for b in blocks:
        x0, y0, x1, y1, text = b[0], b[1], b[2], b[3], b[4].strip()
        block_text = text.lower()
        
        # Count how many target words are in this block
        matches = sum(1 for w in target_words if w in block_text)
        if matches > 0:
            score = matches / len(target_words)
            matched_blocks.append((score, fitz.Rect(x0, y0, x1, y1), text))
            
    matched_blocks.sort(key=lambda x: x[0], reverse=True)
    
    if matched_blocks:
        best_score, best_rect, best_text = matched_blocks[0]
        if best_score >= 0.4:
            union_rect = fitz.Rect(best_rect)
            for score, rect, text in matched_blocks[1:]:
                if score >= 0.2:
                    dist_y = min(abs(rect.y0 - union_rect.y1), abs(rect.y1 - union_rect.y0))
                    dist_x = min(abs(rect.x0 - union_rect.x1), abs(rect.x1 - union_rect.x0))
                    if dist_y < 30 and dist_x < 150:
                        union_rect.include_rect(rect)
            return union_rect

    # Final fallbacks
    if len(fund_name) > 15:
        rects = page.search_for(fund_name[:15])
        if rects:
            return rects[0]
    if len(fund_name) > 10:
        rects = page.search_for(fund_name[:10])
        if rects:
            return rects[0]
            
    return None

def save_page_debug_image(page, amc_dir, pdf_name, page_num, crops, zoom=2.0):
    """
    Saves a page-level debug image showing all cropped tables, matched fund names,
    and lines connecting them.
    crops is a list of tuples: (table_rect, fund_name, fund_rect)
    """
    try:
        import cv2
        import numpy as np
        
        # Render the entire page as a pixmap with zoom
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        img_data = np.frombuffer(pix.tobytes("png"), dtype=np.uint8)
        img = cv2.imdecode(img_data, cv2.IMREAD_COLOR)
        
        if img is None:
            return
            
        for table_rect, fund_name, fund_rect in crops:
            # Scale coordinates by zoom
            tx0 = int(table_rect.x0 * zoom)
            ty0 = int(table_rect.y0 * zoom)
            tx1 = int(table_rect.x1 * zoom)
            ty1 = int(table_rect.y1 * zoom)
            
            if fund_rect is None:
                fund_rect = find_fund_rect_on_page(page, fund_name)
                
            if fund_rect is not None:
                fx0 = int(fund_rect.x0 * zoom)
                fy0 = int(fund_rect.y0 * zoom)
                fx1 = int(fund_rect.x1 * zoom)
                fy1 = int(fund_rect.y1 * zoom)
                
                # Draw Blue box around fund name text
                cv2.rectangle(img, (fx0, fy0), (fx1, fy1), (255, 0, 0), 2)
                # Draw red connecting line from center of fund name to top-left of table
                cx = (fx0 + fx1) // 2
                cy = (fy0 + fy1) // 2
                cv2.line(img, (cx, cy), (tx0, ty0), (0, 0, 255), 2)
            else:
                # No text bbox found on page, draw connecting line from top-left (10, 10)
                cv2.line(img, (10, 10), (tx0, ty0), (0, 0, 255), 1)
                
            # Draw Green box around table
            cv2.rectangle(img, (tx0, ty0), (tx1, ty1), (0, 255, 0), 2)
            
            # Write mapped label above table
            label = f"Mapped: {fund_name}"
            cv2.putText(img, label, (tx0, max(15, ty0 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
            
        debug_dir = os.path.join(amc_dir, "debug_pages")
        os.makedirs(debug_dir, exist_ok=True)
        clean_pdf_name = os.path.splitext(pdf_name)[0]
        debug_file = os.path.join(debug_dir, f"{clean_pdf_name}_page_{page_num + 1}_debug.png")
        cv2.imwrite(debug_file, img)
        print(f"    [DEBUG PAGE SAVED] {debug_file}")
    except Exception as dbg_err:
        print(f"    [DEBUG PAGE ERROR] Failed to save debug page: {dbg_err}")


def crop_and_save(page, rect, zoom, amc_dir, fund_name, suffix="", fund_rect=None):
    """Crops rect from page at given zoom and saves as PNG named after fund_name."""
    clean_name = clean_filename(fund_name)
    if not clean_name:
        clean_name = f"Fund_Page_{page.number + 1}"

    # Record crop for debug page generation
    p_num = page.number
    if p_num not in CURRENT_PDF_MAPPINGS:
        CURRENT_PDF_MAPPINGS[p_num] = []
    CURRENT_PDF_MAPPINGS[p_num].append((rect, fund_name, fund_rect))

    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, clip=rect)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

    base_name = clean_name
    if suffix:
        base_name = f"{base_name}_{suffix}"

    out_path = os.path.join(amc_dir, f"{base_name}.png")
    counter = 1
    while os.path.exists(out_path):
        counter += 1
        out_path = os.path.join(amc_dir, f"{base_name}_{counter}.png")

    img.save(out_path)
    print(f"    [SAVED] '{clean_name}' -> '{os.path.basename(out_path)}'")
    return out_path


def page_has_cell_keywords(page):
    """Returns True if the page contains PRC cell keywords."""
    txt = page.get_text("text").lower()
    return any(kw in txt for kw in CELL_KEYWORDS)


def get_cell_keyword_bbox(page):
    """
    Returns a fitz.Rect bounding box of all cell-keyword locations on the page,
    or None if none found.
    """
    rects = []
    for kw in CELL_KEYWORDS:
        rects.extend(page.search_for(kw))
    if not rects:
        return None
    xs = [r.x0 for r in rects] + [r.x1 for r in rects]
    ys = [r.y0 for r in rects] + [r.y1 for r in rects]
    return fitz.Rect(min(xs), min(ys), max(xs), max(ys))


def text_block_above(page, y_top, x0=None, x1=None, min_y=0, max_chars=200):
    """
    Returns the text of the block closest above y_top (optionally within x0..x1).
    """
    blocks = page.get_text("blocks")
    best = None
    best_y = -9999
    for b in blocks:
        bx0, by0, bx1, by1, text = b[0], b[1], b[2], b[3], b[4].strip()
        if by1 > y_top:
            continue
        if by1 < min_y:
            continue
        if x0 is not None and bx1 < x0 - 10:
            continue
        if x1 is not None and bx0 > x1 + 10:
            continue
        if len(text) > max_chars:
            continue
        if by1 > best_y:
            best_y = by1
            best = text
    return best

# ---------------------------------------------------------------------------
# Advanced utility: split keyword locations into vertical groups (tables)
# ---------------------------------------------------------------------------

def get_keyword_ygroups(page, y_gap=30):
    """
    Finds all cell-keyword locations on a page and groups them by vertical
    proximity. Each group represents a separate PRC table.
    Returns a list of fitz.Rect bounding boxes, one per group, sorted top-to-bottom.
    """
    kw_locs = []
    for kw in CELL_KEYWORDS:
        for r in page.search_for(kw):
            kw_locs.append(r)
    if not kw_locs:
        return []

    # Sort by y0
    kw_locs.sort(key=lambda r: r.y0)

    # Group by vertical proximity
    groups = []
    current = [kw_locs[0]]
    for r in kw_locs[1:]:
        if r.y0 - current[-1].y0 > y_gap:
            groups.append(current)
            current = [r]
        else:
            current.append(r)
    if current:
        groups.append(current)

    # Compute bounding box for each group
    bboxes = []
    for grp in groups:
        xs = [r.x0 for r in grp] + [r.x1 for r in grp]
        ys = [r.y0 for r in grp] + [r.y1 for r in grp]
        bboxes.append(fitz.Rect(min(xs), min(ys), max(xs), max(ys)))

    return bboxes


# ---------------------------------------------------------------------------
# AMC Processor Functions — One per AMC, fully self-contained
# Each function signature: (doc, output_dir, zoom, amc_dir, seen_names)
# seen_names is a shared set passed in from the caller to deduplicate across PDFs.
# ---------------------------------------------------------------------------

def process_amc_360(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    360 ONE MF — Isolated PRC extractor.
    Consolidated table on Page 20 shared by multiple funds.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        if is_snapshot_page(page):
            continue
        if not page_has_cell_keywords(page):
            continue

        bbox = get_cell_keyword_bbox(page)
        if bbox is None:
            continue

        table_rect = fitz.Rect(
            max(0, bbox.x0 - 15), max(0, bbox.y0 - 25),
            min(page.rect.width, bbox.x1 + 15), min(page.rect.height, bbox.y1 + 15)
        )

        blocks = page.get_text("blocks")
        fund_names = []
        for b in blocks:
            text = b[4].strip()
            tl = text.lower()
            lines = [line.strip() for line in text.split('\n') if line.strip()]
            for line in lines:
                l_lower = line.lower()
                if "360" in l_lower and "fund" in l_lower:
                    if not any(x in l_lower for x in ["asset", "suitable", "benchmark", "position"]):
                        fund_names.append(line)

        if not fund_names:
            fund_names = [f"360 ONE Fund Page {page_num + 1}"]

        for raw_name in fund_names:
            fund_name = strip_fund_name(raw_name)
            if not fund_name:
                continue
            if fund_name in seen_names:
                continue
            seen_names.add(fund_name)

            crop_and_save(page, table_rect, zoom, amc_dir, fund_name)
            success = True

    return success


def process_amc_angel_one(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    Angel One MF — Isolated PRC extractor.
    Drawing: w>250, 80<h<130, x0<80. Fund name: block with "angel" above drawing.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        if not page_has_cell_keywords(page):
            continue

        drawings = page.get_drawings()
        table_rect = None
        for d in drawings:
            r = fitz.Rect(d['rect'])
            if 250 < r.width < 350 and 80 < r.height < 130 and r.x0 < 80:
                table_rect = fitz.Rect(r.x0 - 2, r.y0 - 2, r.x1 + 2, r.y1 + 2)
                break

        if table_rect is None:
            bbox = get_cell_keyword_bbox(page)
            if bbox is None:
                continue
            table_rect = fitz.Rect(max(0, bbox.x0 - 5), max(0, bbox.y0 - 15),
                                   min(page.rect.width, bbox.x1 + 5),
                                   min(page.rect.height, bbox.y1 + 15))

        blocks = page.get_text("blocks")
        fund_name = None
        for b in sorted(blocks, key=lambda x: -x[3]):
            by1, text = b[3], b[4].strip()
            tl = text.lower()
            if by1 > table_rect.y0 + 5:
                continue
            if "angel" in tl and ("fund" in tl or "scheme" in tl or "etf" in tl):
                fund_name = strip_fund_name(text)
                break

        if not fund_name:
            fund_name = "Angel One Fund"

        if fund_name in seen_names:
            continue
        seen_names.add(fund_name)

        crop_and_save(page, table_rect, zoom, amc_dir, fund_name)
        success = True

    return success


def process_amc_boi(doc, output_dir, zoom, amc_dir, seen_names=None):


    END_KEYWORDS = ["fund", "etf", "plan", "fof", "days"]

    if seen_names is None:
        seen_names = set()

    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]

        if not page_has_cell_keywords(page):
            continue

        bbox = get_cell_keyword_bbox(page)
        if bbox is None:
            continue
        if bbox.x0 < 200:
            continue

        table_rect = fitz.Rect(
            max(0, bbox.x0 - 5), max(0, bbox.y0 - 20),
            min(page.rect.width, bbox.x1 + 5), min(page.rect.height, bbox.y1 + 20)
        )

        blocks = page.get_text("blocks")
        candidates = []

        for i, b in enumerate(blocks):
            bx0, by0, bx1, by1, text = b[0], b[1], b[2], b[3], b[4].strip()
            tl = text.lower()

            # ✅ Only top-left region
            if bx0 > 350 or by0 > 200:
                continue

            # ✅ Start detection
            if "bank of india" in tl:

                words_collected = []
                found_end = False

                # ✅ Go through current + next few blocks
                for j in range(i, min(i + 5, len(blocks))):
                    t = blocks[j][4].strip()
                    words = t.split()

                    for word in words:
                        lw = word.lower().strip(".,:-")

                        words_collected.append(word)

                        # ✅ STOP EXACTLY HERE
                        if any(lw == k for k in END_KEYWORDS):
                            found_end = True
                            break

                    if found_end:
                        break

                if found_end:
                    fund_name = " ".join(words_collected)

                    # ✅ clean extra spaces/symbols
                    fund_name = strip_fund_name(fund_name)

                    candidates.append((by0, fund_name))

        if candidates:
            candidates.sort(key=lambda x: x[0])
            fund_name = candidates[0][1]
        else:
            fund_name = f"Bank of India Fund Page {page_num + 1}"

        if fund_name in seen_names:
            continue
        seen_names.add(fund_name)

        crop_and_save(page, table_rect, zoom, amc_dir, fund_name)
        success = True

    return success

def process_amc_choice(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    Choice MF — No PRC pages detected. Broad search fallback with warning.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        txt = page.get_text("text").lower()
        if "potential risk class" not in txt and not page_has_cell_keywords(page):
            continue

        bbox = get_cell_keyword_bbox(page)
        if bbox is None:
            matches = page.search_for("potential risk class")
            if not matches:
                continue
            m = matches[0]
            bbox = fitz.Rect(m.x0 - 5, m.y0 - 5, m.x1 + 100, m.y1 + 100)

        table_rect = fitz.Rect(max(0, bbox.x0 - 5), max(0, bbox.y0 - 5),
                               min(page.rect.width, bbox.x1 + 5),
                               min(page.rect.height, bbox.y1 + 5))

        fund_name = strip_fund_name(
            text_block_above(page, bbox.y0, max_chars=150) or f"Choice Fund Page {page_num + 1}"
        )

        if fund_name in seen_names:
            continue
        seen_names.add(fund_name)

        crop_and_save(page, table_rect, zoom, amc_dir, fund_name)
        success = True

    return success


def process_amc_dsp(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    DSP MF — Isolated PRC extractor.
    Page 158-162+ are consolidated table pages. Uses ygroup splitting.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        if page_num < 50:
            continue
        if not page_has_cell_keywords(page):
            continue

        ygroups = get_keyword_ygroups(page, y_gap=30)
        if not ygroups:
            continue

        blocks = page.get_text("blocks")
        fund_candidates = []
        for b in blocks:
            bx0, by0, bx1, by1, text = b[0], b[1], b[2], b[3], b[4].strip()
            tl = text.lower()
            if "dsp" in tl and len(text) < 300:
                if any(x in tl for x in ["suitable", "objective", "parameters", "benchmark", "disclaimer", "performance", "potential risk class matrix"]):
                    continue
                if re.match(r'^\d+\.\s+', text) or any(x in tl for x in ["fund", "liquidity", "etf", "savings"]):
                    cleaned = re.sub(r'^\d+\.\s*', '', text).strip()
                    if cleaned.endswith(':'):
                        cleaned = cleaned[:-1].strip()
                    fund_candidates.append((by1, cleaned, fitz.Rect(bx0, by0, bx1, by1)))

        for yg in ygroups:
            table_rect = fitz.Rect(50, yg.y0 - 20, min(page.rect.width, 530), yg.y1 + 20)

            fund_name = None
            fund_rect = None
            best_dist = 99999
            for by1, name, frect in fund_candidates:
                if by1 <= yg.y0:
                    dist = yg.y0 - by1
                    if dist < best_dist:
                        best_dist = dist
                        fund_name = strip_fund_name(name)
                        fund_rect = frect

            if not fund_name:
                fund_name = f"DSP Fund Page {page_num + 1}"

            if fund_name in seen_names:
                continue
            seen_names.add(fund_name)

            crop_and_save(page, table_rect, zoom, amc_dir, fund_name, fund_rect=fund_rect)
            success = True

    return success


def process_amc_groww(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    Groww MF — Isolated PRC extractor.
    Pages 127-128 have multiple tables. Split by y-groups.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        if not page_has_cell_keywords(page):
            continue

        # Use y_gap=50 to group row and column headers into single table boxes
        ygroups = get_keyword_ygroups(page, y_gap=50)
        if not ygroups:
            continue

        blocks = page.get_text("blocks")
        headings = []
        for b in blocks:
            bx0, by0, bx1, by1, text = b[0], b[1], b[2], b[3], b[4].strip()
            tl = text.lower()
            if "prc for" in tl or "potential risk class" in tl:
                headings.append((by1, text))

        for yg in ygroups:
            table_rect = fitz.Rect(95, max(0, yg.y0 - 15), 570, min(page.rect.height, yg.y1 + 15))

            # Match to nearest heading above the table
            fund_name = None
            best_dist = 99999
            for by1, text in headings:
                if by1 <= yg.y0:
                    dist = yg.y0 - by1
                    if dist < best_dist:
                        best_dist = dist
                        m = re.search(r'prc\s+for\s+(groww\s+.+)', text, re.IGNORECASE)
                        if m:
                            fund_name = strip_fund_name(m.group(1))
                        else:
                            cleaned = re.sub(r'#|prc for|potential risk class.*?of', '', text, flags=re.IGNORECASE).strip()
                            fund_name = strip_fund_name(cleaned)

            if not fund_name:
                fund_name = f"Groww Fund Page {page_num + 1}"

            if fund_name in seen_names:
                continue
            seen_names.add(fund_name)

            crop_and_save(page, table_rect, zoom, amc_dir, fund_name)
            success = True

    return success


def process_amc_hdfc(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    HDFC MF — Isolated PRC extractor.
    Drawing: 140<w<200, 50<h<90, x0>350. Fund name: block in left column vertically aligned with the table.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        txt = page.get_text("text").lower()
        if "potential risk class" not in txt and not page_has_cell_keywords(page):
            continue

        drawings = page.get_drawings()
        found_rects = []
        for d in drawings:
            r = fitz.Rect(d['rect'])
            if 140 < r.width < 200 and 50 < r.height < 90 and r.x0 > 350:
                found_rects.append(r)

        if not found_rects:
            bbox = get_cell_keyword_bbox(page)
            if bbox is None:
                continue
            found_rects = [bbox]

        # Merge duplicate/overlapping drawing rectangles
        merged = []
        for r in found_rects:
            added = False
            for idx, m in enumerate(merged):
                if abs(r.x0 - m.x0) < 5 and abs(r.y0 - m.y0) < 5:
                    merged[idx] = r | m
                    added = True
                    break
            if not added:
                merged.append(r)

        for r in merged:
            table_rect = fitz.Rect(
                max(0.0, r.x0 - 2.0),
                max(0.0, r.y0 - 2.0),
                min(page.rect.width, r.x1 + 2.0),
                min(page.rect.height, r.y1 + 2.0)
            )

            blocks = page.get_text("blocks")
            fund_name = None
            candidates = []
            for b in blocks:
                bx0, by0, bx1, by1 = b[0], b[1], b[2], b[3]
                text = b[4].strip()
                tl = text.lower()
                if bx0 > 80 or len(text) > 150:
                    continue
                if any(x in tl for x in ["suitable", "product label", "benchmark", "risk-o-meter"]):
                    continue
                if not ("hdfc" in tl or "fund" in tl or "scheme" in tl or "fmp" in tl or "charity" in tl):
                    continue

                if by0 <= r.y0 <= by1:
                    dist = 0.0
                else:
                    dist = min(abs(r.y0 - by0), abs(r.y0 - by1))

                if dist < 40:
                    candidates.append((dist, text))

            if candidates:
                candidates.sort(key=lambda x: x[0])
                raw_text = candidates[0][1]
                joined_text = " ".join([line.strip() for line in raw_text.split("\n") if line.strip()])
                fund_name = strip_fund_name(joined_text)

            if not fund_name:
                fund_name = f"HDFC Fund Page {page_num + 1}"

            if fund_name in seen_names:
                continue
            seen_names.add(fund_name)

            crop_and_save(page, table_rect, zoom, amc_dir, fund_name)
            success = True

    return success


def process_amc_hsbc(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    HSBC MF — Isolated PRC extractor.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        if not page_has_cell_keywords(page):
            continue

        rects = []
        for kw in CELL_KEYWORDS:
            for r in page.search_for(kw):
                if r.x0 > 300:
                    rects.append(r)

        if not rects:
            continue

        xs = [r.x0 for r in rects] + [r.x1 for r in rects]
        ys = [r.y0 for r in rects] + [r.y1 for r in rects]
        bbox = fitz.Rect(min(xs), min(ys), max(xs), max(ys))

        drawings = page.get_drawings()
        table_rect = None
        for d in drawings:
            r = fitz.Rect(d['rect'])
            if 175 < r.width < 210 and 80 < r.height < 180 and r.x0 > 350:
                if r.y1 > bbox.y0 - 20 and r.y0 < bbox.y1 + 20:
                    table_rect = fitz.Rect(r.x0 - 2, r.y0 - 2, r.x1 + 2, r.y1 + 2)
                    break

        if table_rect is None:
            table_rect = fitz.Rect(max(0, bbox.x0 - 5), max(0, bbox.y0 - 20),
                                   min(page.rect.width, bbox.x1 + 5),
                                   min(page.rect.height, bbox.y1 + 20))

        blocks = page.get_text("blocks")
        fund_name = None
        for b in sorted(blocks, key=lambda x: x[1]):
            bx0, by0, bx1, by1, text = b[0], b[1], b[2], b[3], b[4].strip()
            tl = text.lower()
            if by0 > 120 or bx0 > 300:
                continue
            if ("hsbc" in tl or "fund" in tl or "scheme" in tl) and len(text) < 150:
                if any(x in tl for x in ["suitable", "benchmark", "product label"]):
                    continue
                fund_name = strip_fund_name(text)
                break

        if not fund_name:
            fund_name = f"HSBC Fund Page {page_num + 1}"

        if fund_name in seen_names:
            continue
        seen_names.add(fund_name)

        crop_and_save(page, table_rect, zoom, amc_dir, fund_name)
        success = True

    return success


def process_amc_iti(doc, output_dir, zoom, amc_dir, seen_names=None):

    if seen_names is None:
        seen_names = set()

    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text("text").lower()

        # ✅ Detect PRC page
        if "risk class" not in text:
            continue

        drawings = page.get_drawings()
        rects = []

        # ✅ STEP 1: detect PRC rectangles
        for d in drawings:
            r = fitz.Rect(d["rect"])

            if (
                100 < r.width < 220 and
                40 < r.height < 130 and
                r.x0 > page.rect.width * 0.4
            ):
                rects.append(r)

        if not rects:
            continue

        # ✅ STEP 2: group by Y (like other AMCs)
        rects.sort(key=lambda r: r.y0)

        y_groups = []
        for r in rects:
            placed = False

            for g in y_groups:
                # ✅ FIXED: use dict access
                if abs(r.y0 - g["y0"]) < 25:
                    g["rects"].append(r)
                    g["box"] = g["box"] | r
                    placed = True
                    break

            if not placed:
                y_groups.append({
                    "y0": r.y0,
                    "rects": [r],
                    "box": r
                })

        # ✅ sort groups top → bottom
        y_groups.sort(key=lambda g: g["y0"])

        blocks = page.get_text("blocks")

        # ✅ STEP 3: map each group → fund name (like Invesco)
        for g in y_groups:

            r = g["box"]

            candidates = []

            for b in blocks:
                bx0, by0, bx1, by1, btext = b[:5]
                text = btext.strip()
                tl = text.lower()

                # ✅ Left-side only (scheme column)
                if bx0 > page.rect.width * 0.6:
                    continue

                if not tl.startswith("iti"):
                    continue

                # ✅ OVERLAP matching (critical fix)
                if by0 <= r.y1 and by1 >= r.y0:
                    candidates.append((abs(by0 - r.y0), text))

            if not candidates:
                continue

            candidates.sort(key=lambda x: x[0])
            fund_name = strip_fund_name(candidates[0][1])

            if fund_name in seen_names:
                continue

            seen_names.add(fund_name)

            # ✅ KEEP ORIGINAL CROPPING
            rect = fitz.Rect(
                max(0, r.x0 - 5),
                max(0, r.y0 - 5),
                min(page.rect.width, r.x1 + 5),
                min(page.rect.height, r.y1 + 5)
            )

            crop_and_save(page, rect, zoom, amc_dir, fund_name)
            success = True

    return success


def process_amc_jm(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    JM Financial MF — Isolated PRC extractor.
    Consolidated table on Page 50 shared by multiple funds.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        if not page_has_cell_keywords(page):
            continue

        bbox = get_cell_keyword_bbox(page)
        if bbox is None:
            continue

        table_rect = fitz.Rect(
            max(0, bbox.x0 - 15), max(0, bbox.y0 - 20),
            min(page.rect.width, bbox.x1 + 15), min(page.rect.height, bbox.y1 + 20)
        )

        blocks = page.get_text("blocks")
        fund_candidates = []
        for b in blocks:
            bx0, by0, bx1, by1, text = b[0], b[1], b[2], b[3], b[4].strip()
            # Ensure the block is inside the PRC table vertical bounds
            if by0 < bbox.y0 - 10 or by1 > bbox.y1 + 10:
                continue
            tl = text.lower()
            if "jm" in tl and "fund" in tl:
                lines = [line.strip() for line in text.split('\n') if line.strip()]
                for line in lines:
                    l_lower = line.lower()
                    if "jm" in l_lower and "fund" in l_lower:
                        if not any(x in l_lower for x in ["suitable", "benchmark", "annexure", "nature"]):
                            cleaned = re.sub(r'\s*\([A-Z]+-[I|V|X|L|C\d]+\)', '', line, flags=re.IGNORECASE).strip()
                            fund_candidates.append((cleaned, line))

        if not fund_candidates:
            fund_candidates = [((f"JM Fund Page {page_num + 1}"), None)]

        for raw_name, raw_line in fund_candidates:
            fund_name = strip_fund_name(raw_name)
            if not fund_name:
                continue
            if fund_name in seen_names:
                continue
            seen_names.add(fund_name)

            # Find bounding box for raw_line
            fund_rect = None
            if raw_line:
                rects = page.search_for(raw_line)
                if rects:
                    fund_rect = rects[0]

            crop_and_save(page, table_rect, zoom, amc_dir, fund_name, fund_rect=fund_rect)
            success = True

    return success


def process_amc_jio(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    JioBlackRock MF — Isolated PRC extractor.
    Page 40-42 are consolidated table pages. Uses ygroup splitting.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        if not page_has_cell_keywords(page):
            continue

        ygroups = get_keyword_ygroups(page, y_gap=30)
        if not ygroups:
            continue

        blocks = page.get_text("blocks")
        title_candidates = []
        for b in blocks:
            bx0, by0, bx1, by1, text = b[0], b[1], b[2], b[3], b[4].strip()
            tl = text.lower()
            if "potential risk class" in tl and "jioblackrock" in tl:
                m = re.search(r'JioBlackRock\s+(.+?)(?:\)|\n|$)', text, re.IGNORECASE)
                if m:
                    fund_name = strip_fund_name("JioBlackRock " + m.group(1).strip())
                    title_candidates.append((by1, fund_name))

        for yg in ygroups:
            table_rect = fitz.Rect(max(0, yg.x0 - 5), max(0, yg.y0 - 25), min(page.rect.width, yg.x1 + 5), min(page.rect.height, yg.y1 + 25))

            fund_name = None
            best_dist = 99999
            for by1, name in title_candidates:
                if by1 <= yg.y0:
                    dist = yg.y0 - by1
                    if dist < best_dist:
                        best_dist = dist
                        fund_name = name

            if not fund_name:
                fund_name = f"JioBlackRock Fund Page {page_num + 1}"

            if fund_name in seen_names:
                continue
            seen_names.add(fund_name)

            crop_and_save(page, table_rect, zoom, amc_dir, fund_name)
            success = True

    return success


def process_amc_lic(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    LIC MF — Isolated PRC extractor.
    Consolidated debt matrix page. Crops 10 rows independently.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        txt = page.get_text("text").lower()
        if "potential risk class" not in txt and not page_has_cell_keywords(page):
            continue

        blocks = page.get_text("blocks")
        fund_blocks = []
        for b in blocks:
            bx0, by0, bx1, by1, text = b[0], b[1], b[2], b[3], b[4].strip()
            tl = text.lower()
            if bx0 < 200 and len(text) < 150:
                if "lic" in tl and not any(x in tl for x in ["suitable", "benchmark", "disclaimer", "matrix"]):
                    fund_blocks.append((by0, by1, strip_fund_name(text)))

        # Consolidated page has at least 5 fund names listed on the left
        if len(fund_blocks) >= 5:
            fund_blocks.sort(key=lambda x: x[0])
            y0_start = 121.47
            row_height = 69.25
            for idx, (f_y0, f_y1, fund_name) in enumerate(fund_blocks):
                # Ensure we only process up to the 10 known rows on page 82
                if idx >= 10:
                    break
                y0 = y0_start + idx * row_height
                y1 = y0_start + (idx + 1) * row_height
                rect = fitz.Rect(25.0, y0 - 1.0, 591.5, y1 + 1.0)
                if fund_name in seen_names:
                    continue
                seen_names.add(fund_name)
                crop_and_save(page, rect, zoom, amc_dir, fund_name)
                success = True
        else:
            # Skip single pages like page 9, since all debt funds are covered on consolidated page 82
            continue

    return success


def process_amc_nj(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    NJ AMC — Isolated PRC extractor. Single page. Cell keyword bbox.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        if not page_has_cell_keywords(page):
            continue

        bbox = get_cell_keyword_bbox(page)
        if bbox is None:
            continue

        table_rect = fitz.Rect(
            max(0, bbox.x0 - 5), max(0, bbox.y0 - 20),
            min(page.rect.width, bbox.x1 + 5), min(page.rect.height, bbox.y1 + 20)
        )

        blocks = page.get_text("blocks")
        fund_name = None
        for b in blocks:
            text = b[4].strip()
            tl = text.lower()
            if "nj" in tl and ("fund" in tl or "scheme" in tl) and len(text) < 150:
                fund_name = strip_fund_name(text)
                break

        if not fund_name:
            fund_name = f"NJ Fund Page {page_num + 1}"

        if fund_name in seen_names:
            continue
        seen_names.add(fund_name)

        crop_and_save(page, table_rect, zoom, amc_dir, fund_name)
        success = True

    return success


def process_amc_pgim(doc, output_dir, zoom, amc_dir, seen_names=None):


    if seen_names is None:
        seen_names = set()

    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]

        # ✅ detection unchanged
        if not page_has_cell_keywords(page):
            continue

        bbox = get_cell_keyword_bbox(page)
        if bbox is None or bbox.x1 > 220:
            continue

        # ✅ cropping unchanged
        table_rect = fitz.Rect(
            max(0, bbox.x0 - 5), max(0, bbox.y0 - 20),
            min(page.rect.width, bbox.x1 + 5), min(page.rect.height, bbox.y1 + 20)
        )

        blocks = page.get_text("blocks")
        fund_rect = None

        # ==================================================
        # ✅ FINAL NAME EXTRACTION (ROBUST)
        # ==================================================
        for b in sorted(blocks, key=lambda x: x[1]):
            bx0, by0, bx1, by1, text = b[:5]

            # normalize text
            line = " ".join(text.split())
            tl = line.lower()

            # ✅ only scan top region
            if by0 > 150:
                break

            # ✅ only left side (where name exists)
            if bx0 > page.rect.width * 0.6:
                continue

            # ✅ actual fund name line always has "fund"
            if "fund" in tl:

                # remove bracket noise
                clean = re.sub(r"\(.*?\)", "", line)

                # ✅ REMOVE trailing 'PGIM INDIA'
                clean = re.sub(r"\bPGIM\s+INDIA\b$", "", clean, flags=re.I)

                clean = " ".join(clean.split())

                # ✅ ensure prefix
                if not clean.lower().startswith("pgim india"):
                    clean = "PGIM India " + clean

                fund_name = strip_fund_name(clean)
                fund_rect = fitz.Rect(bx0, by0, bx1, by1)
                break

        # ✅ fallback (rare edge)
        if not fund_name:
            fund_name = f"PGIM India Fund Page {page_num + 1}"

        if fund_name in seen_names:
            continue

        seen_names.add(fund_name)

        crop_and_save(page, table_rect, zoom, amc_dir, fund_name, fund_rect=fund_rect)
        success = True

    return success


def process_amc_ppfas(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    PPFAS / Parag Parikh MF — Isolated PRC extractor.
    Cell keywords in RIGHT column (x0>300). Fund name from "Potential Risk Class (PRC) of Parag Parikh ..." block.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        if not page_has_cell_keywords(page):
            continue

        bbox = get_cell_keyword_bbox(page)
        if bbox is None or bbox.x0 < 300:
            continue

        table_rect = fitz.Rect(
            max(0, bbox.x0 - 5), max(0, bbox.y0 - 20),
            min(page.rect.width, bbox.x1 + 5), min(page.rect.height, bbox.y1 + 20)
        )

        blocks = page.get_text("blocks")
        fund_name = None
        for b in blocks:
            text = b[4].strip()
            tl = text.lower()
            if "potential risk class" in tl and "parag parikh" in tl:
                m = re.search(
                    r'(?:prc\)|class\s*\(prc\))\s+of\s+(parag parikh\s+\w[\w\s]*?)(?:\n|$)',
                    text, re.IGNORECASE)
                if m:
                    fund_name = strip_fund_name(m.group(1))
                    break
                after = re.sub(r'.*?Potential Risk Class.*?of\s+', '', text,
                               flags=re.IGNORECASE).strip()
                fund_name = strip_fund_name(after)
                break

        if not fund_name:
            fund_name = f"Parag Parikh Fund Page {page_num + 1}"

        if fund_name in seen_names:
            continue
        seen_names.add(fund_name)

        crop_and_save(page, table_rect, zoom, amc_dir, fund_name)
        success = True

    return success


def process_amc_sbi(doc, output_dir, zoom, amc_dir, seen_names=None):

    if seen_names is None:
        seen_names = set()

    # ==================================================
    # ✅ CLEANER
    # ==================================================
    def clean_sbi_name(name):
        name = " ".join(name.split())

        # remove bracket text
        if "(" in name:
            name = name.split("(")[0].strip()

        # remove duplicate "Fund Fund"
        name = re.sub(r'\b(Fund)\s+\1\b', r'\1', name)

        return name.strip()

    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]

        if is_snapshot_page(page):
            continue

        if not page_has_cell_keywords(page):
            continue

        y_groups = get_keyword_ygroups(page, y_gap=40)
        if not y_groups:
            continue

        blocks = page.get_text("blocks")

        # ==================================================
        # ✅ FUND DETECTION (UNCHANGED)
        # ==================================================
        fund_candidates = []

        for b in blocks:
            bx0, by0, bx1, by1, val = b[0], b[1], b[2], b[3], b[4].strip()
            tl = val.lower()

            # ✅ Only left side (fund column) and correct y threshold
            if bx0 < 250 and (by0 < 120 or (page_num > 40 and by0 < 560)):

                if any(x in tl for x in ["sbi", "fund", "etf", "index", "g-sec", "liquid"]):

                    if any(x in tl for x in [
                        "suitable", "objective", "parameters",
                        "benchmark index", "benchmark risk",
                        "scheme benchmark", "disclaimer", "performance"
                    ]):
                        continue

                    fund_candidates.append((by0, by1, val, fitz.Rect(bx0, by0, bx1, by1)))

        fund_candidates.sort(key=lambda x: x[0])

        # ==================================================
        # ✅ PROCESS PRC TABLES
        # ==================================================
        for yg in y_groups:

            if len(y_groups) == 1 and fund_candidates:
                matched_name = fund_candidates[0][2]
                matched_rect = fund_candidates[0][3]
            else:
                matched_name = None
                matched_rect = None
                closest_diff = float('inf')

                for fby0, fby1, ftext, frect in fund_candidates:
                    if fby1 < yg.y0:
                        diff = yg.y0 - fby1
                        if diff < closest_diff:
                            closest_diff = diff
                            matched_name = ftext
                            matched_rect = frect

            raw_name = matched_name if matched_name else f"SBI Fund Page {page_num + 1}"

            raw_name = clean_sbi_name(raw_name)

            # ==================================================
            # ✅ STRUCTURAL SPLIT (UNCHANGED)
            # ==================================================
            STOP_WORDS = ["fund", "etf"]

            parts = []

            if "|" in raw_name:
                parts = raw_name.split("|")
            else:
                words = raw_name.split()
                current = []

                for i, w in enumerate(words):
                    current.append(w)
                    w_lower = w.lower()

                    if w_lower in STOP_WORDS:

                        if i + 1 < len(words):
                            next_word = words[i + 1]

                            if next_word.strip().startswith("SBI"):
                                parts.append(" ".join(current))
                                current = []

                if current:
                    parts.append(" ".join(current))

            # ==================================================
            # ✅ FINAL CLEAN
            # ==================================================
            fund_list = [
                clean_sbi_name(strip_fund_name(p.strip()))
                for p in parts if p.strip()
            ]

            # ==================================================
            # ✅ FIND PRC TABLE
            # ==================================================
            drawings = page.get_drawings()
            prc_draw = None

            for d in drawings:
                r = fitz.Rect(d['rect'])

                if 420 < r.width < 480 and 180 < r.height < 220 and 40 < r.x0 < 70:
                    if abs(r.y0 - yg.y0 + 39) < 15:
                        prc_draw = r
                        break

            if prc_draw:
                table_rect = fitz.Rect(
                    prc_draw.x0 - 2,
                    prc_draw.y0 - 2,
                    prc_draw.x1 + 2,
                    prc_draw.y1 + 2
                )
            else:
                table_rect = fitz.Rect(
                    max(0, yg.x0 - 10),
                    yg.y0 - 30,
                    min(page.rect.width, yg.x1 + 10),
                    yg.y1 + 10
                )

            # ==================================================
            # ✅ SAVE (FINAL FIX — SPLIT MULTIPLE FUNDS)
            # ==================================================
            for fn in fund_list:

                if not fn:
                    continue

                split_funds = [fn]

                # ✅ FIX: if merged funds exist → split instead of cutting
                if fn.count("Fund") > 1:
                    temp = fn.split("Fund")
                    split_funds = []

                    for t in temp:
                        t = t.strip()
                        if not t:
                            continue
                        split_funds.append(t + " Fund")

                # ✅ process each fund separately
                for f in split_funds:

                    f = f.strip()

                    if not f:
                        continue

                    if not f.lower().startswith("sbi"):
                        f = "SBI " + f

                    if f in seen_names:
                        continue

                    seen_names.add(f)

                    crop_and_save(page, table_rect, zoom, amc_dir, f, fund_rect=matched_rect)

                    success = True

    return success


def extract_full_absl_fund_name(page):
    blocks = page.get_text("blocks")

    header_blocks = []

    for b in blocks:
        x0, y0, x1, y1, text = b[0], b[1], b[2], b[3], b[4].strip()

        if not text:
            continue

        # ✅ STRICTLY top-left header region
        if y0 > 150 or x0 > page.rect.width * 0.6:
            continue

        header_blocks.append((y0, x0, text, fitz.Rect(x0, y0, x1, y1)))

    if not header_blocks:
        return None, None

    # ✅ sort correctly
    header_blocks = sorted(header_blocks, key=lambda x: (x[0], x[1]))

    # ✅ merge everything in header
    merged_text = " ".join([b[2] for b in header_blocks])

    # ✅ clean junk spacing
    merged_text = re.sub(r"\s+", " ", merged_text)

    match = re.search(
        r"(Aditya Birla Sun Life.*?(Fund|ETF|Plan|FOF))",
        merged_text,
        re.IGNORECASE
    )

    if match:
        matched_name = match.group(1).strip()
        matched_words = [w.lower().strip(".,:;()-") for w in matched_name.split()]
        matched_words = [w for w in matched_words if w and w not in {'and', 'the', 'of', 'for', 'in', 'a', 'to', '&', '-'}]
        
        union_rect = fitz.Rect()
        for b in header_blocks:
            bx0, by0, bx1, by1 = b[3].x0, b[3].y0, b[3].x1, b[3].y1
            block_text_lower = b[2].lower()
            
            # Core title block
            if "aditya birla" in block_text_lower or "sun life" in block_text_lower:
                union_rect.include_rect(b[3])
            # Additional adjacent title blocks, excluding body description text below y0=90
            elif by0 < 90 and (bx1 - bx0 > 20) and any(w in block_text_lower for w in matched_words):
                union_rect.include_rect(b[3])
                
        if not union_rect.is_empty:
            return matched_name, union_rect
        return matched_name, None

    return None, None

def process_amc_absl(doc, output_dir, zoom, amc_dir, seen_names=None):

    if seen_names is None:
        seen_names = set()

    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]

        # ✅ detect PRC pages
        if not page_has_cell_keywords(page):
            continue

        bbox = get_cell_keyword_bbox(page)
        if bbox is None or bbox.x0 > 30:
            continue

        # ✅ crop PRC table
        table_rect = fitz.Rect(
            max(0, bbox.x0 - 5),
            max(0, bbox.y0 - 20),
            min(page.rect.width, bbox.x1 + 5),
            min(page.rect.height, bbox.y1 + 20),
        )

        # ✅ ONLY SOURCE OF TRUTH → page header
        fund_name, fund_rect = extract_full_absl_fund_name(page)

        # ✅ fix &amp;
        if fund_name:
            fund_name = html.unescape(fund_name)
            fund_name = fund_name.replace("&", "and")

        # ✅ fallback
        if not fund_name:
            fund_name = f"ABSL Fund Page {page_num + 1}"

        # ✅ dedupe
        if fund_name in seen_names:
            continue
        seen_names.add(fund_name)

        crop_and_save(page, table_rect, zoom, amc_dir, fund_name, fund_rect=fund_rect)

        success = True

    return success


def process_amc_axis(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    Axis MF — Isolated PRC extractor.
    Splits multiple stacked tables on pages, supporting both Active and Passive factsheet layouts.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        
        # Check text for general keywords or potential risk class context
        text = page.get_text("text").lower()
        if not (("potential risk" in text and "matrix" in text) or "potential risk class" in text or "prc matrix" in text or page_has_cell_keywords(page)):
            continue

        drawings = page.get_drawings()
        headers = []
        for d in drawings:
            r = fitz.Rect(d['rect'])
            # Type A (Active stacked table headers): width ~123, height ~13, on the right
            if 120 < r.width < 125 and 10 < r.height < 15 and r.x0 > 400:
                headers.append((r, "active"))
            # Type B (Passive row PRC outlines): width ~59, height ~6, on the far right
            elif 50 < r.width < 85 and 4 < r.height < 8 and r.x0 > 480:
                headers.append((r, "passive"))

        if not headers:
            # Fallback to single table behavior if no header rects/outlines are found
            # Restrict keyword search to right side of page (x0 > 300) to avoid left-column text pollution
            rects = []
            for kw in CELL_KEYWORDS:
                for r in page.search_for(kw):
                    if r.x0 > 300:
                        rects.append(r)
            if not rects:
                continue
            xs = [r.x0 for r in rects] + [r.x1 for r in rects]
            ys = [r.y0 for r in rects] + [r.y1 for r in rects]
            bbox = fitz.Rect(min(xs), min(ys), max(xs), max(ys))
            
            table_rect = fitz.Rect(
                max(0, bbox.x0 - 5), max(0, bbox.y0 - 20),
                min(page.rect.width, bbox.x1 + 5), min(page.rect.height, bbox.y1 + 20)
            )
            blocks = page.get_text("blocks")
            fund_name = None
            fund_rect = None
            for b in blocks:
                bx0, by0, bx1, by1, text_block = b[0], b[1], b[2], b[3], b[4].strip()
                tl = text_block.lower()
                if by1 > 110 or bx0 < 350:
                    continue
                if "axis" in tl and ("fund" in tl or "scheme" in tl or "etf" in tl or "plan" in tl):
                    for delim in ["This product", "this product"]:
                        idx = text_block.find(delim)
                        if idx > 5:
                            text_block = text_block[:idx].strip()
                    fund_name = strip_fund_name(text_block)
                    fund_rect = fitz.Rect(bx0, by0, bx1, by1)
                    break
            if not fund_name:
                fund_name = f"Axis Fund Page {page_num + 1}"
            if fund_name in seen_names:
                success = True
                continue
            seen_names.add(fund_name)
            crop_and_save(page, table_rect, zoom, amc_dir, fund_name, fund_rect=fund_rect)
            success = True
            continue

        # Sort headers from top to bottom
        headers.sort(key=lambda x: x[0].y0)
        blocks = page.get_text("blocks")
        
        # Find candidate title blocks in the left column
        candidates = []
        for b in blocks:
            bx0, by0, bx1, by1, text_block = b[0], b[1], b[2], b[3], b[4].strip()
            tl = text_block.lower()
            if bx0 > 300:
                continue
            if "axis" in tl:
                candidates.append((bx0, by0, bx1, by1, text_block))

        for h_idx, (h, h_type) in enumerate(headers):
            best_c = None
            best_dist = 99999
            for c in candidates:
                bx0, by0, bx1, by1, text_block = c
                # Filter out candidates starting below the header
                if by0 > h.y0 + 120:
                    continue
                # Minimize vertical distance between middle of text block and middle of table
                dist = abs((by0 + by1)/2 - (h.y0 + h.y1)/2)
                if dist < best_dist:
                    best_dist = dist
                    best_c = c

            fund_name = None
            fund_rect = None
            if best_c:
                raw_text = best_c[4]
                fund_rect = fitz.Rect(best_c[0], best_c[1], best_c[2], best_c[3])
                lines = [line.strip() for line in raw_text.split('\n') if line.strip()]
                valid_lines = []
                disclaimer_kws = ["suitable for", "this product", "investors", "seeking", "benchmark", "risk-o-meter", "suitable", "consult"]
                for line in lines:
                    line_lower = line.lower()
                    if any(kw in line_lower for kw in disclaimer_kws):
                        break
                    valid_lines.append(line)
                
                if valid_lines:
                    name_str = " ".join(valid_lines)
                    name_str = " ".join(name_str.split())
                    for delim in ["(An open", "(an open", " An open", " an open"]:
                        idx = name_str.find(delim)
                        if idx > 0:
                            name_str = name_str[:idx].strip()
                    fund_name = name_str.strip("(-, ")

            if not fund_name:
                fund_name = f"Axis Fund Page {page_num + 1} Table {h_idx + 1}"

            if fund_name in seen_names:
                success = True
                continue
            seen_names.add(fund_name)

            if h_type == "active":
                table_rect = fitz.Rect(h.x0 - 5, h.y0 - 5, h.x0 + 127.5, h.y0 + 115)
            else: # passive
                table_rect = fitz.Rect(h.x0 - 5, h.y0 - 20, h.x1 + 15, h.y0 + 90)

            crop_and_save(page, table_rect, zoom, amc_dir, fund_name, fund_rect=fund_rect)
            success = True

    return success


def process_amc_bajaj(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    Bajaj Finserv MF — Isolated PRC extractor.
    Page 56 (index 55) has consolidated PRC tables for all 7 debt schemes.
    We crop them individually to avoid combining them or cropping riskometers from other pages.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    if len(doc) > 55:
        page = doc[55]
        # Coordinates of the 7 sections on page 56
        sections = [
            (fitz.Rect(30, 98, 530, 185), "Bajaj Finserv Liquid Fund"),
            (fitz.Rect(30, 200, 530, 287), "Bajaj Finserv Money Market Fund"),
            (fitz.Rect(30, 301, 530, 386), "Bajaj Finserv Overnight Fund"),
            (fitz.Rect(30, 403, 530, 488), "Bajaj Finserv Banking and PSU Debt Fund"),
            (fitz.Rect(30, 505, 530, 590), "Bajaj Finserv Nifty 1D Rate Liquid ETF - Growth"),
            (fitz.Rect(30, 606, 530, 692), "Bajaj Finserv Gilt Fund"),
            (fitz.Rect(30, 708, 530, 793), "Bajaj Finserv Low Duration Fund")
        ]

        for rect, fund_name in sections:
            if fund_name in seen_names:
                continue
            seen_names.add(fund_name)
            crop_and_save(page, rect, zoom, amc_dir, fund_name)
            success = True

    return success


def process_amc_bandhan(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    Bandhan MF — Isolated PRC extractor.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        if not page_has_cell_keywords(page):
            continue

        bbox = get_cell_keyword_bbox(page)
        if bbox is None:
            continue

        table_rect = fitz.Rect(max(0, bbox.x0 - 5), bbox.y0 - 20, min(page.rect.width, bbox.x1 + 5), bbox.y1 + 15)

        blocks = page.get_text("blocks")
        fund_name = None
        fund_rect = None
        for b in blocks:
            bx0, by0, bx1, by1, text = b[0], b[1], b[2], b[3], b[4].strip()
            tl = text.lower()
            if by0 < 100 and bx0 < page.rect.width * 0.5:
                if "bandhan" in tl:
                    cleaned_text = re.sub(r'[^\x00-\x7F]+', '', text).strip()
                    fund_name = strip_fund_name(cleaned_text)
                    fund_rect = fitz.Rect(bx0, by0, bx1, by1)
                    break

        if not fund_name:
            fund_name = f"Bandhan Fund Page {page_num + 1}"

        if fund_name in seen_names:
            continue
        seen_names.add(fund_name)

        crop_and_save(page, table_rect, zoom, amc_dir, fund_name, fund_rect=fund_rect)
        success = True

    return success


def process_amc_baroda(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    Baroda BNP Paribas MF — Isolated PRC extractor.
    Header: "SCHEME WISE POTENTIAL RISK CLASS (PRC) MATRIX". Fund name above header.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        txt = page.get_text("text").lower()
        if "scheme wise potential risk class" not in txt and not page_has_cell_keywords(page):
            continue
        if not page_has_cell_keywords(page):
            continue

        bbox = get_cell_keyword_bbox(page)
        if bbox is None:
            continue

        table_rect = fitz.Rect(
            max(0, bbox.x0 - 5), max(0, bbox.y0 - 30),
            min(page.rect.width, bbox.x1 + 5), min(page.rect.height, bbox.y1 + 30)
        )

        blocks = page.get_text("blocks")
        fund_name = None
        for b in blocks:
            by0, text = b[1], b[4].strip()
            tl = text.lower()
            if by0 < 100 and any(k in tl for k in ['baroda', 'bnp', 'paribas']):
                fund_name = strip_fund_name(text)
                break

        if not fund_name:
            fund_name = f"Baroda BNP Fund Page {page_num + 1}"

        if fund_name in seen_names:
            continue
        seen_names.add(fund_name)

        crop_and_save(page, table_rect, zoom, amc_dir, fund_name)
        success = True

    return success


def process_amc_canara(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    Canara Robeco MF — Isolated PRC extractor.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        if not page_has_cell_keywords(page):
            continue

        bbox = get_cell_keyword_bbox(page)
        if bbox is None:
            continue

        table_rect = fitz.Rect(415, bbox.y0 - 25, min(page.rect.width, 580), bbox.y1 + 10)

        blocks = page.get_text("blocks")
        fund_name = None
        for b in blocks:
            bx0, by0, bx1, by1, text = b[0], b[1], b[2], b[3], b[4].strip()
            tl = text.lower()
            if by0 < 60:
                if "canara" in tl or "robeco" in tl:
                    fund_name = strip_fund_name(text)
                    break

        if not fund_name:
            fund_name = f"Canara Robeco Fund Page {page_num + 1}"

        if fund_name in seen_names:
            continue
        seen_names.add(fund_name)

        crop_and_save(page, table_rect, zoom, amc_dir, fund_name)
        success = True

    return success


def process_amc_capital(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    Capitalmind MF — No PRC pages detected. Broad search fallback with warning.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        txt = page.get_text("text").lower()
        if "potential risk class" not in txt and not page_has_cell_keywords(page):
            continue

        bbox = get_cell_keyword_bbox(page)
        if bbox is None:
            matches = page.search_for("potential risk class")
            if not matches:
                continue
            m = matches[0]
            bbox = fitz.Rect(m.x0 - 5, m.y0 - 5, m.x1 + 200, m.y1 + 100)

        table_rect = fitz.Rect(max(0, bbox.x0 - 5), max(0, bbox.y0 - 10),
                               min(page.rect.width, bbox.x1 + 5),
                               min(page.rect.height, bbox.y1 + 10))

        fund_name = strip_fund_name(
            text_block_above(page, bbox.y0, max_chars=150) or f"Capitalmind Fund Page {page_num + 1}"
        )

        if fund_name in seen_names:
            continue
        seen_names.add(fund_name)

        crop_and_save(page, table_rect, zoom, amc_dir, fund_name)
        success = True

    return success


def process_amc_edelweiss(doc, output_dir, zoom, amc_dir, seen_names=None):


    if seen_names is None:
        seen_names = set()

    success = False

    # ✅ robust PRC detection (fixes missed Bharat pages)
    def has_prc(page):
        text = page.get_text("text").lower()
        return (
            "potential risk class matrix" in text
            or "risk class matrix" in text
        )

    for page_num in range(len(doc)):
        page = doc[page_num]

        # ✅ only process PRC pages (improved)
        if not has_prc(page):
            continue

        blocks = page.get_text("blocks")

        # ==========================================
        # ✅ STEP 1: FIND PRC HEADER → bbox
        # ==========================================
        bbox = None

        for b in blocks:
            if "potential risk class matrix" in b[4].lower():
                bbox = fitz.Rect(b[0], b[1], b[2], b[3])
                break

        if bbox is None:
            continue

        # ==========================================
        # ✅ STEP 2: ORIGINAL CROPPING (UPDATED ✅)
        # ==========================================
        table_rect = fitz.Rect(
            410, bbox.y0 - 15,
            min(page.rect.width, 568), bbox.y0 + 115
        )

        # ==========================================
        # ✅ STEP 3: FUND NAME DETECTION (FIXED)
        # ==========================================
        candidates = []

        for i, b in enumerate(blocks):
            bx0, by0, bx1, by1, text = b[:5]
            text = text.strip()
            tl = text.lower()

            if (
                "edelweiss" in tl
                or "bharat bond" in tl
            ):
                if "bharat bond" in tl:
                    # BHARAT Bond ETFs contain maturity year after 'ETF' in the same block.
                    # We take the full block text to prevent cutting off the year.
                    fund_name = text.replace("\n", " ")
                    fund_name = strip_fund_name(fund_name)
                    candidates.append((by0, fund_name))
                else:
                    words_collected = []
                    found_end = False

                    for j in range(i, min(i + 6, len(blocks))):
                        t = blocks[j][4].strip()
                        words = t.split()

                        for word in words:
                            lw = word.lower().strip(".,:-")

                            words_collected.append(word)

                            if lw in ["fund", "etf", "fof"]:
                                found_end = True
                                break

                            if lw in ["index", "offshore"]:
                                continue

                        if found_end:
                            break

                    if found_end:
                        fund_name = " ".join(words_collected)
                        fund_name = strip_fund_name(fund_name)

                        candidates.append((by0, fund_name))

        # ✅ pick top-most
        if candidates:
            candidates.sort(key=lambda x: x[0])
            fund_name = candidates[0][1]
        else:
            continue   # ✅ no fake names

        if fund_name in seen_names:
            continue

        seen_names.add(fund_name)

        crop_and_save(page, table_rect, zoom, amc_dir, fund_name)

        success = True

    return success


def process_amc_franklin(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    Franklin Templeton MF — Isolated PRC extractor.
    Page 85 has 4 separate PRC tables stacked vertically. Split by y-groups.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        if page_num < 50:
            continue
        if not page_has_cell_keywords(page):
            continue

        ygroups = get_keyword_ygroups(page, y_gap=25)
        if len(ygroups) < 4:
            continue

        # Correct mapping of the 4 tables on page 85 corresponding to the 11 schemes
        mapping = {
            0: ["Franklin India Overnight Fund"],
            1: ["Franklin India Liquid Fund",
                "Franklin India Money Market Fund",
                "Franklin India Ultra Short Duration Fund"],
            2: ["Franklin India Low Duration Fund",
                "Franklin India Medium To Long Duration Fund",
                "Franklin India Floating Rate Fund",
                "Franklin India Banking & PSU Debt Fund",
                "Franklin India Corporate Debt Fund"],
            3: ["Franklin India Government Securities Fund",
                "Franklin India Long Duration Fund"]
        }

        for idx, yg in enumerate(ygroups):
            table_rect = fitz.Rect(
                max(0, yg.x0 - 10), max(0, yg.y0 - 15),
                min(page.rect.width, yg.x1 + 10), min(page.rect.height, yg.y1 + 15)
            )

            funds = mapping.get(idx, [])
            for fund_name in funds:
                if fund_name in seen_names:
                    continue
                seen_names.add(fund_name)

                # Dynamically find the bounding box for the specific scheme text on the page
                fund_rect = find_fund_rect_on_page(page, fund_name)

                crop_and_save(page, table_rect, zoom, amc_dir, fund_name, fund_rect=fund_rect)
                success = True

    return success


def process_amc_helios(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    Helios MF — Isolated PRC extractor (fixed version)
    Extracts fund name strictly from:
    'Potential Risk Class (PRC) of Helios <Fund Name>'
    """

    if seen_names is None:
        seen_names = set()

    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]

        # ✅ STRICT PAGE FILTER (prevents footer pages like your screenshot)
        page_text = page.get_text()
        tl_text = page_text.lower()

        if "potential risk class" not in tl_text or "(prc)" not in tl_text:
            continue

        # ✅ Find PRC table bbox
        if not page_has_cell_keywords(page):
            continue

        bbox = get_cell_keyword_bbox(page)
        if bbox is None:
            continue

        table_rect = fitz.Rect(
            max(0, bbox.x0 - 5), max(0, bbox.y0 - 20),
            min(page.rect.width, bbox.x1 + 5), min(page.rect.height, bbox.y1 + 20)
        )

        # ✅ Extract fund name ONLY from PRC line (full page text)
        fund_name = None

        m = re.search(
            r'Potential\s+Risk\s+Class\s*\(PRC\)\s+of\s+(Helios\s+[A-Za-z&\-\s]+)',
            page_text,
            re.IGNORECASE
        )

        if m:
            fund_name = strip_fund_name(m.group(1))

        # ✅ NO loose fallback here (this caused "Small Cap Fund" bug)
        if not fund_name:
            fund_name = f"Helios Fund Page {page_num + 1}"

        # ✅ Skip duplicates
        if fund_name in seen_names:
            continue
        seen_names.add(fund_name)

        # ✅ Crop and save PRC table
        crop_and_save(page, table_rect, zoom, amc_dir, fund_name)

        success = True

    return success

def process_amc_icici(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    ICICI Prudential MF — Isolated PRC extractor.
    Cell keywords in LEFT small rect (x1<180). Fund name: "Returns of ICICI Prudential FundName".
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        if not page_has_cell_keywords(page):
            continue

        bbox = get_cell_keyword_bbox(page)
        if bbox is None or bbox.x1 > 180:
            continue

        table_rect = fitz.Rect(
            max(0, bbox.x0 - 5), max(0, bbox.y0 - 15),
            min(page.rect.width, bbox.x1 + 5), min(page.rect.height, bbox.y1 + 15)
        )

        blocks = page.get_text("blocks")
        fund_name = None
        for b in sorted(blocks, key=lambda x: x[1]):
            by1, text = b[3], b[4].strip()
            tl = text.lower()
            if by1 > 100:
                break
            if "returns of icici prudential" in tl:
                after = re.sub(r'^Returns of\s+', '', text, flags=re.IGNORECASE)
                # Split at any dash followed by Growth Option, Growth, or IDCW
                after = re.split(r'\s*[-\u2013\u2014]\s*(?:Growth Option|Growth|IDCW)', after, flags=re.IGNORECASE)[0].strip()
                # Clean up any trailing 'as on' text
                after = re.split(r'\s+as\s+on', after, flags=re.IGNORECASE)[0].strip()
                fund_name = strip_fund_name(after)
                break
            if "icici prudential" in tl and ("fund" in tl or "scheme" in tl or "etf" in tl):
                fund_name = strip_fund_name(text)
                break

        if not fund_name:
            fund_name = f"ICICI Prudential Fund Page {page_num + 1}"

        if fund_name in seen_names:
            continue
        seen_names.add(fund_name)

        crop_and_save(page, table_rect, zoom, amc_dir, fund_name)
        success = True

    return success



def process_amc_invesco(doc, output_dir, zoom, amc_dir, seen_names=None):

    
    if seen_names is None:
        seen_names = set()

    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]

        # ✅ original working condition (DO NOT TOUCH)
        if not page_has_cell_keywords(page):
            continue

        blocks = page.get_text("blocks")

        # ✅ get table regions (original logic)
        y_groups = get_keyword_ygroups(page, y_gap=40)
        if not y_groups:
            continue

        # ---------------------------------------------
        # ✅ STEP 1: detect fund headers (original style)
        # ---------------------------------------------
        fund_starts = []

        for i, b in enumerate(blocks):
            bx0, by0, bx1, by1, text = b[:5]
            text = text.strip()
            tl = text.lower()

            if by0 > 500:
                continue

            if tl.startswith("invesco india"):
                fund_starts.append((i, by0))

        # ---------------------------------------------
        # ✅ STEP 2: build fund names (original logic)
        # ---------------------------------------------
        fund_names = []

        for idx, by0 in fund_starts:
            words_collected = []
            found_end = False

            for j in range(idx, min(idx + 6, len(blocks))):
                t = blocks[j][4].strip()
                words = t.split()

                for w in words:
                    lw = w.lower().strip(".,:-")
                    words_collected.append(w)

                    if lw in ["fund", "etf"]:
                        if len(words_collected) >= 2:
                            found_end = True
                            break

                if found_end:
                    break

            if found_end:
                fund_name = strip_fund_name(" ".join(words_collected))
                fund_names.append((by0, fund_name))

        # ---------------------------------------------
        # ✅ STEP 3: FIXED mapping (ONLY CHANGE ✅)
        # ---------------------------------------------
        for i, (by0, fname) in enumerate(fund_names):

            if i < len(fund_names) - 1:
                next_by0 = fund_names[i + 1][0]
            else:
                next_by0 = float('inf')

            for yg in y_groups:
                if by0 <= yg.y0 < next_by0:

                    if fname in seen_names:
                        continue

                    seen_names.add(fname)

                    rect = fitz.Rect(
                        max(0, yg.x0 - 5),
                        max(0, yg.y0 - 20),
                        min(page.rect.width, yg.x1 + 5),
                        min(page.rect.height, yg.y1 + 20)
                    )

                    crop_and_save(page, rect, zoom, amc_dir, fname)
                    success = True

    return success


def process_amc_kotak(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    Kotak MF — Isolated PRC extractor.
    PRC drawing: w~124, h~76, x0<30 in far-left column.
    Cell keywords: tiny, x1<160. Fund name: first block at y<55, x<60 "KOTAK FUND NAME".
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        if not page_has_cell_keywords(page):
            continue

        bbox = get_cell_keyword_bbox(page)
        if bbox is None or bbox.x1 > 180:
            continue

        drawings = page.get_drawings()
        prc_draw = None
        for d in drawings:
            r = fitz.Rect(d['rect'])
            if 115 < r.width < 135 and 68 < r.height < 85 and r.x0 < 30:
                if abs(r.y0 - bbox.y0 + 20) < 50:
                    prc_draw = r
                    break

        if prc_draw is None:
            prc_draw = fitz.Rect(14, bbox.y0 - 25, 148, bbox.y1 + 25)

        table_rect = fitz.Rect(
            max(0, prc_draw.x0 - 2), max(0, prc_draw.y0 - 2),
            min(page.rect.width, prc_draw.x1 + 2), min(page.rect.height, prc_draw.y1 + 2)
        )

        blocks = page.get_text("blocks")
        fund_name = None
        for b in blocks:
            bx0, by0, bx1, by1, text = b[0], b[1], b[2], b[3], b[4].strip()
            if by0 < 80 and bx0 < 100:
                first_line = text.split('\n')[0].strip()
                if 'kotak' in first_line.lower():
                    fund_name = strip_fund_name(first_line).title()
                    break

        if not fund_name:
            fund_name = f"Kotak Fund Page {page_num + 1}"

        if fund_name in seen_names:
            continue
        seen_names.add(fund_name)

        crop_and_save(page, table_rect, zoom, amc_dir, fund_name)
        success = True

    return success


def process_amc_mahindra(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    Mahindra Manulife MF — Isolated PRC extractor.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        if not page_has_cell_keywords(page):
            continue

        txt = page.get_text("text").lower()
        if "potential risk class matrix for debt" not in txt and "potential risk class matrix for" not in txt:
            continue

        ygroups = get_keyword_ygroups(page, y_gap=30)
        if not ygroups:
            continue

        blocks = page.get_text("blocks")
        fund_candidates = []
        for b in blocks:
            bx0, by0, bx1, by1, text = b[0], b[1], b[2], b[3], b[4].strip()
            first_line = text.split('\n')[0].strip()
            fl_lower = first_line.lower()
            if 'mahindra' in fl_lower and any(x in fl_lower for x in ['fund', 'scheme', 'etf']):
                fund_candidates.append((by1, first_line))

        for yg in ygroups:
            table_rect = fitz.Rect(max(0, yg.x0 - 5), max(0, yg.y0 - 20), min(page.rect.width, yg.x1 + 5), min(page.rect.height, yg.y1 + 5))

            fund_name = None
            best_dist = 99999
            for by1, text in fund_candidates:
                if by1 <= yg.y0:
                    dist = yg.y0 - by1
                    if dist < best_dist:
                        best_dist = dist
                        fund_name = strip_fund_name(text)

            if not fund_name:
                fund_name = f"Mahindra Manulife Fund Page {page_num + 1}"

            if fund_name in seen_names:
                continue
            seen_names.add(fund_name)

            crop_and_save(page, table_rect, zoom, amc_dir, fund_name)
            success = True

    return success


def process_amc_motilal(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    Motilal Oswal MF — Isolated PRC extractor.
    Each fund has own "Potential Risk Class Matrix" block.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        
        # Check text for general keywords or potential risk class context
        text = page.get_text("text").lower()
        if not ("potential risk class" in text or "prc matrix" in text or page_has_cell_keywords(page)):
            continue
        if is_snapshot_page(page):
            continue

        blocks = page.get_text("blocks")
        prc_blocks = [(b[1], b[3], b[4].strip()) for b in blocks
                      if "potential risk class matrix" in b[4].strip().lower()]

        fund_blocks = []
        for b in blocks:
            bx0, by0, bx1, by1, text_block = b[0], b[1], b[2], b[3], b[4].strip()
            tl = text_block.lower()
            if ("motilal oswal" in tl or "most" in tl) and ("fund" in tl or "etf" in tl):
                if not any(x in tl for x in ["suitable", "benchmark", "performance"]):
                    clean_text = None
                    lines = [l.strip() for l in text_block.split("\n") if l.strip()]
                    for line in lines:
                        l_lower = line.lower()
                        if "motilal" in l_lower or "most" in l_lower or "fund" in l_lower or "etf" in l_lower or "scheme" in l_lower:
                            clean_text = line
                            break
                    if not clean_text and lines:
                        clean_text = lines[0]
                    if clean_text:
                        fund_blocks.append((by0, by1, bx0, bx1, clean_text))

        prc_blocks.sort(key=lambda x: x[0])
        fund_blocks.sort(key=lambda x: x[0])

        for py0, py1, prc_text in prc_blocks:
            if py0 < 50:
                continue
            fund_name = None
            fund_rect = None
            for fy0, fy1, fx0, fx1, fund_text in reversed(fund_blocks):
                if fy1 <= py0 + 5:
                    fund_name = strip_fund_name(fund_text)
                    fund_rect = fitz.Rect(fx0, fy0, fx1, fy1)
                    break

            if not fund_name:
                fund_name = f"Motilal Oswal Fund Page {page_num + 1}"

            if fund_name in seen_names:
                success = True
                continue
            seen_names.add(fund_name)

            drawings = page.get_drawings()
            table_rect = None
            for d in drawings:
                r = fitz.Rect(d['rect'])
                if 450 < r.width < 510 and 60 < r.height < 100 and abs(r.y0 - py1) < 30:
                    table_rect = fitz.Rect(r.x0 - 2, r.y0 - 2, r.x1 + 2, r.y1 + 2)
                    break

            if table_rect is None:
                # Fallback to standard crop relative to heading
                table_rect = fitz.Rect(40, py0 - 5, 545, py0 + 105)

            crop_and_save(page, table_rect, zoom, amc_dir, fund_name, fund_rect=fund_rect)
            success = True

    return success


def process_amc_navi(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    Navi MF — Isolated PRC extractor. Cell keyword bbox. Fund name: "Navi FundName" block.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        if not page_has_cell_keywords(page):
            continue

        bbox = get_cell_keyword_bbox(page)
        if bbox is None:
            continue

        table_rect = fitz.Rect(
            max(0, bbox.x0 - 5), max(0, bbox.y0 - 20),
            min(page.rect.width, bbox.x1 + 5), min(page.rect.height, bbox.y1 + 20)
        )

        blocks = page.get_text("blocks")
        fund_name = None
        for b in sorted(blocks, key=lambda x: x[1]):
            text = b[4].strip()
            tl = text.lower()
            if "navi" in tl and ("fund" in tl or "scheme" in tl) and len(text) < 150:
                if any(x in tl for x in ["suitable", "benchmark", "portfolio", "holdings"]):
                    continue
                fund_name = strip_fund_name(text)
                break

        if not fund_name:
            fund_name = f"Navi Fund Page {page_num + 1}"

        if fund_name in seen_names:
            continue
        seen_names.add(fund_name)

        crop_and_save(page, table_rect, zoom, amc_dir, fund_name)
        success = True

    return success

def process_amc_nippon(doc, output_dir, zoom, amc_dir, seen_names=None):

    if seen_names is None:
        seen_names = set()

    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]

        has_keywords = page_has_cell_keywords(page)
        bbox = get_cell_keyword_bbox(page)

        # ✅ ORIGINAL fallback (unchanged)
        if bbox is None:
            if not has_keywords:
                continue

            bbox = fitz.Rect(
                page.rect.width * 0.70,
                page.rect.height * 0.20,
                page.rect.width,
                page.rect.height * 0.90
            )

        # ✅ ORIGINAL crop
        table_rect = fitz.Rect(
            max(0, bbox.x0 - 5),
            max(0, bbox.y0 - 10),
            min(page.rect.width, bbox.x1 + 5),
            min(page.rect.height, bbox.y1 + 10)
        )

        blocks = page.get_text("blocks")

        fund_name = None

        # ==================================================
        # ✅ SIMPLE + ROBUST NAME EXTRACTION
        # ==================================================
        for b in sorted(blocks, key=lambda x: x[1]):
            bx0, by0, bx1, by1, text = b[:5]
            line = " ".join(text.split())
            tl = line.lower()

            # ✅ look only in top-left region
            if by0 > 100:
                break

            if bx0 > page.rect.width * 0.5:
                continue

            if tl.startswith("nippon india"):
                fund_name = strip_fund_name(line)
                break

        # ✅ fallback (rare)
        if not fund_name:
            fund_name = f"Nippon India Fund Page {page_num + 1}"

        if fund_name in seen_names:
            continue

        seen_names.add(fund_name)

        crop_and_save(page, table_rect, zoom, amc_dir, fund_name)
        success = True

    return success

def process_amc_old(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    Old Bridge MF — No PRC pages detected. Broad search fallback with warning.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        txt = page.get_text("text").lower()
        if "potential risk class" not in txt and not page_has_cell_keywords(page):
            continue

        bbox = get_cell_keyword_bbox(page)
        if bbox is None:
            matches = page.search_for("potential risk class")
            if not matches:
                continue
            m = matches[0]
            bbox = fitz.Rect(m.x0 - 5, m.y0 - 5, m.x1 + 200, m.y1 + 120)

        table_rect = fitz.Rect(max(0, bbox.x0 - 5), max(0, bbox.y0 - 10),
                               min(page.rect.width, bbox.x1 + 5),
                               min(page.rect.height, bbox.y1 + 10))

        fund_name = strip_fund_name(
            text_block_above(page, bbox.y0, max_chars=150) or f"Old Bridge Fund Page {page_num + 1}"
        )

        if fund_name in seen_names:
            continue
        seen_names.add(fund_name)

        crop_and_save(page, table_rect, zoom, amc_dir, fund_name)
        success = True

    return success


def process_amc_quant(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    Quant MF — Isolated PRC extractor. Cell keyword bbox + enclosing drawing.
    Fund name: block above table with "quant" and "fund".
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        if not page_has_cell_keywords(page):
            continue

        bbox = get_cell_keyword_bbox(page)
        if bbox is None:
            continue

        drawings = page.get_drawings()
        table_rect = None
        for d in drawings:
            r = fitz.Rect(d['rect'])
            if r.contains(fitz.Rect(bbox.x0 - 5, bbox.y0 - 5, bbox.x1 + 5, bbox.y1 + 5)):
                table_rect = fitz.Rect(r.x0 - 2, r.y0 - 2, r.x1 + 2, r.y1 + 2)
                break

        if table_rect is None:
            table_rect = fitz.Rect(max(0, bbox.x0 - 5), max(0, bbox.y0 - 20),
                                   min(page.rect.width, bbox.x1 + 5),
                                   min(page.rect.height, bbox.y1 + 20))

        blocks = page.get_text("blocks")
        fund_name = None
        candidates = []
        for b in blocks:
            by1, text = b[3], b[4].strip()
            tl = text.lower()
            if by1 > bbox.y0 + 5 or by1 < bbox.y0 - 300:
                continue
            if "quant" in tl and ("fund" in tl or "scheme" in tl) and len(text) < 150:
                candidates.append((bbox.y0 - by1, text))

        if candidates:
            candidates.sort(key=lambda x: x[0])
            fund_name = strip_fund_name(candidates[0][1])

        if not fund_name:
            fund_name = f"Quant Fund Page {page_num + 1}"

        if fund_name in seen_names:
            continue
        seen_names.add(fund_name)

        crop_and_save(page, table_rect, zoom, amc_dir, fund_name)
        success = True

    return success


def process_amc_quantum(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    Quantum MF — Isolated PRC extractor. Single page. Cell keyword bbox.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        if not page_has_cell_keywords(page):
            continue

        bbox = get_cell_keyword_bbox(page)
        if bbox is None:
            continue

        table_rect = fitz.Rect(
            max(0, bbox.x0 - 5), max(0, bbox.y0 - 20),
            min(page.rect.width, bbox.x1 + 5), min(page.rect.height, bbox.y1 + 20)
        )

        blocks = page.get_text("blocks")
        fund_name = None
        fund_rect = None
        for b in sorted(blocks, key=lambda x: x[1]):
            bx0, by0, bx1, by1, text = b[0], b[1], b[2], b[3], b[4].strip()
            tl = text.lower()
            if by0 > 100:
                break
            if bx0 < 200 and "quantum" in tl and ("fund" in tl or "scheme" in tl) and len(text) < 300:
                fund_name = strip_fund_name(text)
                fund_rect = fitz.Rect(bx0, by0, bx1, by1)
                break

        if not fund_name:
            fund_name = f"Quantum Fund Page {page_num + 1}"

        if fund_name in seen_names:
            continue
        seen_names.add(fund_name)

        crop_and_save(page, table_rect, zoom, amc_dir, fund_name, fund_rect=fund_rect)
        success = True

    return success


def process_amc_samco(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    Samco MF — Isolated PRC extractor. Fund name: "samco" + "fund/scheme/etf" block
    at top of page — NOT the risk description text.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        if not page_has_cell_keywords(page):
            continue

        bbox = get_cell_keyword_bbox(page)
        if bbox is None:
            continue

        table_rect = fitz.Rect(
            max(0, bbox.x0 - 180),   # ✅ more left
            max(0, bbox.y0 - 20),   # ✅ moderate upward (NOT huge)
            min(page.rect.width, bbox.x1 + 45),  # ✅ more right
            min(page.rect.height, bbox.y1 + 70)  # ✅ more bottom
        )

        blocks = page.get_text("blocks")
        fund_name = None
        candidates = []
        for b in sorted(blocks, key=lambda x: x[1]):
            by1, text = b[3], b[4].strip()
            tl = text.lower()
            if by1 > 150:
                continue
            if "samco" in tl and ("fund" in tl or "scheme" in tl or "etf" in tl) and len(text) < 150:
                if any(x in tl for x in ["suitable", "benchmark", "risk of the scheme is"]):
                    continue
                candidates.append((abs(bbox.y0 - by1), text))

        if candidates:
            candidates.sort(key=lambda x: x[0])
            fund_name = strip_fund_name(candidates[0][1])

        # Check backward page if not found
        if not fund_name and page_num > 0:
            prev_page = doc[page_num - 1]
            prev_blocks = prev_page.get_text("blocks")
            for b in sorted(prev_blocks, key=lambda x: x[1]):
                bx0, by0, bx1, by1, text = b[0], b[1], b[2], b[3], b[4].strip()
                tl = text.lower()
                if by0 < 150 and bx0 < 150:
                    first_line = text.split('\n')[0].strip()
                    fl_lower = first_line.lower()
                    if 'samco' in fl_lower and any(x in fl_lower for x in ['fund', 'scheme', 'etf']):
                        fund_name = strip_fund_name(first_line)
                        break

        if not fund_name:
            fund_name = f"Samco Fund Page {page_num + 1}"

        if fund_name in seen_names:
            continue
        seen_names.add(fund_name)

        crop_and_save(page, table_rect, zoom, amc_dir, fund_name)
        success = True

    return success


def process_amc_shriram(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    Shriram MF — Isolated PRC extractor.
    Fund name: topmost block with "SHRIRAM" and "FUND/ETF". For ETF pages,
    use first line of the block (not the objective paragraph).
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        if not page_has_cell_keywords(page):
            continue

        bbox = get_cell_keyword_bbox(page)
        if bbox is None:
            continue

        table_rect = fitz.Rect(
            max(0, bbox.x0 - 5), max(0, bbox.y0 - 20),
            min(page.rect.width, bbox.x1 + 5), min(page.rect.height, bbox.y1 + 20)
        )

        blocks = page.get_text("blocks")
        fund_name = None
        for b in sorted(blocks, key=lambda x: x[1]):
            text = b[4].strip()
            tl = text.lower()
            if b[3] > 100:
                break
            if "shriram" in tl and ("fund" in tl or "etf" in tl or "scheme" in tl):
                if any(x in tl for x in ["suitable", "benchmark", "aims to", "objective"]):
                    first_line = text.split('\n')[0].strip()
                    if "shriram" in first_line.lower():
                        fund_name = strip_fund_name(first_line)
                        break
                    continue
                joined_text = " ".join([line.strip() for line in text.split("\n") if line.strip()])
                fund_name = strip_fund_name(joined_text)
                break

        if not fund_name:
            fund_name = f"Shriram Fund Page {page_num + 1}"

        if fund_name in seen_names:
            continue
        seen_names.add(fund_name)

        crop_and_save(page, table_rect, zoom, amc_dir, fund_name)
        success = True

    return success


def process_amc_taurus(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    Taurus MF — No PRC pages detected in inspection. Broad search fallback with warning.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        txt = page.get_text("text").lower()
        if "potential risk class" not in txt and not page_has_cell_keywords(page):
            continue

        bbox = get_cell_keyword_bbox(page)
        if bbox is None:
            matches = page.search_for("potential risk class")
            if not matches:
                continue
            m = matches[0]
            bbox = fitz.Rect(m.x0 - 5, m.y0 - 5, m.x1 + 200, m.y1 + 120)

        table_rect = fitz.Rect(max(0, bbox.x0 - 5), max(0, bbox.y0 - 10),
                               min(page.rect.width, bbox.x1 + 5),
                               min(page.rect.height, bbox.y1 + 10))

        fund_name = strip_fund_name(
            text_block_above(page, bbox.y0, max_chars=150) or f"Taurus Fund Page {page_num + 1}"
        )

        if fund_name in seen_names:
            continue
        seen_names.add(fund_name)

        crop_and_save(page, table_rect, zoom, amc_dir, fund_name)
        success = True

    return success


def process_amc_tata(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    Tata MF — Isolated PRC extractor.
    Page 122 has 5 PRC tables vertically stacked. Split by y-groups.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        if not page_has_cell_keywords(page):
            continue

        drawings = page.get_drawings()
        prc_rects = sorted(
            [fitz.Rect(d['rect']) for d in drawings
             if 160 < fitz.Rect(d['rect']).width < 180
             and 85 < fitz.Rect(d['rect']).height < 100
             and fitz.Rect(d['rect']).x0 > 330],
            key=lambda r: r.y0
        )

        if not prc_rects:
            # Fallback if no drawings detected
            bbox = get_cell_keyword_bbox(page)
            if bbox is None:
                continue
            table_rect = fitz.Rect(max(0, bbox.x0 - 5), max(0, bbox.y0 - 20),
                                   min(page.rect.width, bbox.x1 + 5),
                                   min(page.rect.height, bbox.y1 + 20))
            fn = f"Tata Fund Page {page_num + 1}"
            if fn not in seen_names:
                seen_names.add(fn)
                crop_and_save(page, table_rect, zoom, amc_dir, fn)
                success = True
            continue

        # We have 5 tables. The sections are vertically separated:
        # Table 1: y < 222
        # Table 2: 222 <= y < 345
        # Table 3: 345 <= y < 472
        # Table 4: 472 <= y < 600
        # Table 5: y >= 600
        boundaries = [0.0, 222.0, 345.0, 472.0, 600.0, 1000.0]
        blocks = page.get_text("blocks")

        for idx, r in enumerate(prc_rects):
            y_start = boundaries[idx]
            y_end = boundaries[idx+1]

            section_funds = []
            for b in blocks:
                bx0, by0, bx1, by1, text = b[0], b[1], b[2], b[3], b[4].strip()
                tl = text.lower()
                if bx0 > 300:
                    continue
                yc = (by0 + by1) / 2.0
                if y_start <= yc < y_end:
                    if "tata" in tl and len(text) < 150:
                        if not any(x in tl for x in ["sr no", "scheme name", "potential risk", "suitable", "annexure", "market risks", "tatamutualfund"]):
                            text_clean = " ".join(text.split())
                            # Remove leading numbers and dots/spaces
                            text_clean = re.sub(r'^\d+[\.\s\n]*', '', text_clean).strip()
                            section_funds.append(text_clean)

            table_rect = fitz.Rect(r.x0 - 2, r.y0 - 2, r.x1 + 2, r.y1 + 2)
            if not section_funds:
                fallback_name = f"Tata Fund {idx + 1} Page {page_num + 1}"
                section_funds = [fallback_name]

            for raw_name in section_funds:
                fund_name = strip_fund_name(raw_name)
                if not fund_name:
                    continue
                if fund_name in seen_names:
                    continue
                seen_names.add(fund_name)
                crop_and_save(page, table_rect, zoom, amc_dir, fund_name)
                success = True

    return success


def process_amc_trust(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    Trust MF — Isolated PRC extractor.
    Pages 21-22 have multiple PRC tables. Split by y-groups.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        if not page_has_cell_keywords(page):
            continue

        ygroups = get_keyword_ygroups(page, y_gap=30)
        if not ygroups:
            continue

        blocks = page.get_text("blocks")
        fund_names = []
        for b in blocks:
            bx0, by0, bx1, by1, text = b[0], b[1], b[2], b[3], b[4].strip()
            tl = text.lower()
            if bx0 > 300:
                continue
            first_line = text.split('\n')[0].strip()
            fl_lower = first_line.lower()
            if "trust" in fl_lower and ("fund" in fl_lower or "scheme" in fl_lower or "etf" in fl_lower) and len(first_line) < 150:
                if not any(x in tl for x in ["potential risk", "suitable", "benchmark"]):
                    fund_names.append((by1, first_line))

        for yg in ygroups:
            # Table is on the right side: x from 445 to 575
            table_rect = fitz.Rect(445, max(0, yg.y0 - 30), 575, min(page.rect.height, yg.y1 + 10))

            # Match to nearest fund name block above the table
            fund_name = None
            best_dist = 99999
            for by1, text in fund_names:
                if by1 <= yg.y0:
                    dist = yg.y0 - by1
                    if dist < best_dist:
                        best_dist = dist
                        fund_name = strip_fund_name(text)

            if not fund_name:
                fund_name = f"TrustMF Fund Page {page_num + 1}"

            if fund_name in seen_names:
                continue
            seen_names.add(fund_name)

            crop_and_save(page, table_rect, zoom, amc_dir, fund_name)
            success = True

    return success


def process_amc_unifi(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    Unifi MF — Isolated PRC extractor.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        txt = page.get_text("text").lower()
        if "potential risk class" not in txt and not page_has_cell_keywords(page):
            continue

        bbox = get_cell_keyword_bbox(page)
        if bbox is None:
            continue

        table_rect = fitz.Rect(60, bbox.y0 - 55, min(page.rect.width, 495), bbox.y0 + 75)

        blocks = page.get_text("blocks")
        fund_name = None
        for b in blocks:
            bx0, by0, bx1, by1, text = b[0], b[1], b[2], b[3], b[4].strip()
            tl = text.lower()
            if by0 < 300:
                if "unifi" in tl and "fund" in tl:
                    fund_name = strip_fund_name(text)
                    break

        if not fund_name:
            fund_name = f"Unifi Fund Page {page_num + 1}"

        if fund_name in seen_names:
            continue
        seen_names.add(fund_name)

        crop_and_save(page, table_rect, zoom, amc_dir, fund_name)
        success = True

    return success


def process_amc_union(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    Union MF — Isolated PRC extractor.
    Fund name: "Union ... Fund" block above table — NOT footnotes or risk description.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        if not page_has_cell_keywords(page):
            continue

        bbox = get_cell_keyword_bbox(page)
        if bbox is None:
            continue

        table_rect = fitz.Rect(
            max(0, bbox.x0 - 5), max(0, bbox.y0 - 20),
            min(page.rect.width, bbox.x1 + 5), min(page.rect.height, bbox.y1 + 20)
        )

        blocks = page.get_text("blocks")
        fund_name = None
        fund_rect = None

        # Strict top-left fund name search for Union
        union_block = None
        scheme_block = None
        for b in blocks:
            bx0, by0, bx1, by1, text = b[0], b[1], b[2], b[3], b[4].strip()
            tl = text.lower()
            if bx0 < 150 and by0 < 60:
                if tl == 'union':
                    union_block = b
            elif bx0 < 200 and 60 <= by0 <= 90:
                if 'fund' in tl or 'gilt' in tl:
                    scheme_block = b
                    
        if union_block and scheme_block:
            raw_name = "Union " + scheme_block[4].strip()
            raw_name = " ".join(raw_name.split()).title()
            fund_rect = fitz.Rect(union_block[0], union_block[1], union_block[2], union_block[3])
            fund_rect.include_rect(fitz.Rect(scheme_block[0], scheme_block[1], scheme_block[2], scheme_block[3]))
            fund_name = strip_fund_name(raw_name)
        else:
            benchmark_map = {
                "corporate debt": "Union Corporate Debt Fund",
                "dynamic bond": "Union Dynamic Bond Fund",
                "dynamic gilt": "Union Gilt Fund",
                "short duration": "Union Short Duration Fund",
                "money market": "Union Money Market Fund",
                "low duration": "Union Low Duration Fund",
                "liquid debt": "Union Liquid Fund",
                "liquid overnight": "Union Overnight Fund"
            }

            # Try benchmark mapping first (titles are vectors)
            for b in blocks:
                bx0, by0, bx1, by1, text = b[0], b[1], b[2], b[3], b[4].strip()
                tl = text.replace('\n', ' ').lower()
                for key, val in benchmark_map.items():
                    if key in tl:
                        if bx0 < 250 and by0 < 100 and len(text) < 100:
                            fund_name = val
                            fund_rect = fitz.Rect(bx0, by0, bx1, by1)
                            break
                if fund_name:
                    break

            if not fund_name:
                candidates = []
                for b in blocks:
                    bx0, by0, bx1, by1, text = b[0], b[1], b[2], b[3], b[4].strip()
                    tl = text.lower()
                    if by1 > bbox.y0 + 5:
                        continue
                    if bx0 < 250 and "union" in tl and ("fund" in tl or "scheme" in tl) and len(text) < 150:
                        if any(x in tl for x in ["aum and aaum", "interest rate risk", "aaum is inclusive",
                                                  "benchmark", "suitable", "note:", "the aaum"]):
                            continue
                        candidates.append((bbox.y0 - by1, text, fitz.Rect(bx0, by0, bx1, by1)))

                if candidates:
                    candidates.sort(key=lambda x: x[0])
                    fund_name = strip_fund_name(candidates[0][1])
                    fund_rect = candidates[0][2]

            if not fund_name:
                for b in sorted(blocks, key=lambda x: x[1]):
                    bx0, by0, bx1, by1, text = b[0], b[1], b[2], b[3], b[4].strip()
                    tl = text.lower()
                    if by1 > 100:
                        break
                    if bx0 < 250 and "union" in tl and ("fund" in tl or "scheme" in tl) and len(text) < 150:
                        if any(x in tl for x in ["aum", "aaum", "interest rate", "benchmark", "suitable"]):
                            continue
                        fund_name = strip_fund_name(text)
                        fund_rect = fitz.Rect(bx0, by0, bx1, by1)
                        break

            if not fund_name:
                fund_name = f"Union Fund Page {page_num + 1}"

        if fund_name in seen_names:
            continue
        seen_names.add(fund_name)

        crop_and_save(page, table_rect, zoom, amc_dir, fund_name, fund_rect=fund_rect)
        success = True

    return success


def process_amc_uti(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    UTI MF — Isolated PRC extractor.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        txt = page.get_text("text").lower()
        if "potential risk class" not in txt:
            continue

        bbox = get_cell_keyword_bbox(page)
        if bbox is None:
            continue

        matches = page.search_for("POTENTIAL RISK CLASS")
        if matches:
            m = matches[0]
            y0 = m.y0 - 5
            y1 = max(m.y0 + 110, bbox.y1 + 10)
        else:
            y0 = bbox.y0 - 25
            y1 = bbox.y1 + 10

        x0 = max(0, bbox.x0 - 15)
        x1 = min(page.rect.width, bbox.x1 + 15)
        table_rect = fitz.Rect(x0, y0, x1, y1)

        blocks = page.get_text("blocks")
        fund_name = None
        fund_rect = None
        for b in blocks:
            bx0, by0, bx1, by1, val = b[0], b[1], b[2], b[3], b[4].strip()
            if by0 < 60 and bx0 < 200:
                if "uti" in val.lower():
                    first_line = val.split("\n")[0].strip()
                    first_line = re.sub(r'\s*\(Erstwhile.*?\)', '', first_line, flags=re.IGNORECASE)
                    first_line = re.sub(r'\s*\(An open ended.*?\)', '', first_line, flags=re.IGNORECASE)
                    first_line = first_line.replace('@', '').strip()
                    fund_name = strip_fund_name(first_line)
                    fund_rect = fitz.Rect(bx0, by0, bx1, by1)
                    break

        if not fund_name:
            fund_name = f"UTI Fund Page {page_num + 1}"

        if fund_name in seen_names:
            continue
        seen_names.add(fund_name)

        crop_and_save(page, table_rect, zoom, amc_dir, fund_name, fund_rect=fund_rect)
        success = True

    return success


def process_amc_wealth(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    The Wealth Company MF — Isolated PRC extractor.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        if not page_has_cell_keywords(page):
            continue

        ygroups = get_keyword_ygroups(page, y_gap=30)
        if not ygroups:
            continue

        blocks = page.get_text("blocks")
        fund_candidates = []
        for b in blocks:
            bx0, by0, bx1, by1, text = b[0], b[1], b[2], b[3], b[4].strip()
            tl = text.lower()
            if "wealth" in tl and "fund" in tl:
                if not any(x in tl for x in ["suitable", "benchmark", "note:", "managing", "period"]):
                    fund_candidates.append((by0, by1, text))

        for yg in ygroups:
            table_rect = fitz.Rect(25, yg.y0 - 25, min(page.rect.width, 540), yg.y1 + 10)

            fund_name = None
            best_dist = 99999
            for fby0, fby1, text in fund_candidates:
                if fby1 <= yg.y0:
                    dist = yg.y0 - fby1
                    if dist < best_dist:
                        best_dist = dist
                        fund_name = strip_fund_name(text)

            if not fund_name:
                fund_name = f"The Wealth Company Fund Page {page_num + 1}"

            if fund_name in seen_names:
                continue
            seen_names.add(fund_name)

            crop_and_save(page, table_rect, zoom, amc_dir, fund_name)
            success = True

    return success

def process_amc_whiteoak(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    WhiteOak PRC table detector (STRICT)

    - Detects ONLY PRC tables for WhiteOak
    - Uses heading → next heading segmentation
    - Uses page geometry for accurate width
    - No generic logic, no fallback heuristics
    """

    if seen_names is None:
        seen_names = set()

    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text("text").lower()

        # ✅ strict filter → only PRC section
        if "potential risk class" not in text:
            continue

        blocks = page.get_text("blocks")

        # ✅ Step 1: detect PRC headings
        headings = []
        for b in blocks:
            rect = fitz.Rect(b[:4])
            txt = b[4].strip()

            if re.search(r'prc\s+for\s+whiteoak\s+capital', txt, re.IGNORECASE):
                headings.append((rect, txt))

        if len(headings) < 2:
            continue  # must have multiple PRC tables

        # ✅ sort vertically
        headings = sorted(headings, key=lambda x: x[0].y0)

        # ✅ Step 2: process each PRC table
        for i, (rect, txt) in enumerate(headings):

            # ✅ vertical bounds → heading to next heading
            if i < len(headings) - 1:
                next_rect = headings[i + 1][0]
                y0 = rect.y0
                y1 = next_rect.y0
            else:
                # last table → extend until table ends
                y0 = rect.y0
                y1 = rect.y0 + 260  # safe height for one PRC table

            # ✅ horizontal bounds → FULL PAGE WIDTH
            margin = 20
            x0 = page.rect.x0 + margin
            x1 = page.rect.x1 - margin

            crop_rect = fitz.Rect(x0, y0 - 5, x1, y1 - 10)

            # ✅ extract fund name strictly
            m = re.search(
                r'PRC\s+for\s+(WhiteOak\s+Capital\s+[A-Za-z&\-\s]+)',
                txt,
                re.IGNORECASE
            )

            if not m:
                continue

            fund_name = strip_fund_name(m.group(1))

            if fund_name in seen_names:
                continue

            seen_names.add(fund_name)

            crop_and_save(page, crop_rect, zoom, amc_dir, fund_name)
            success = True

    return success

def process_amc_abbakkus(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    Abakkus MF — Isolated PRC extractor. Fund name: "abakkus" + "fund/scheme" block above table.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        if not page_has_cell_keywords(page):
            continue

        bbox = get_cell_keyword_bbox(page)
        if bbox is None:
            continue

        table_rect = fitz.Rect(
            max(0, bbox.x0 - 5), max(0, bbox.y0 - 20),
            min(page.rect.width, bbox.x1 + 5), min(page.rect.height, bbox.y1 + 20)
        )

        blocks = page.get_text("blocks")
        fund_name = None
        fund_rect = None
        # Sort blocks by by1 descending to find the one closest above the PRC table first
        for b in sorted(blocks, key=lambda x: x[3], reverse=True):
            bx0, by0, bx1, by1, text = b[0], b[1], b[2], b[3], b[4].strip()
            tl = text.lower()
            if by1 > bbox.y0 + 5:
                continue
            if "abakkus" in tl and ("fund" in tl or "scheme" in tl) and len(text) < 150:
                if any(x in tl for x in ["suitable", "benchmark", "product label"]):
                    continue
                fund_name = strip_fund_name(text)
                fund_rect = fitz.Rect(bx0, by0, bx1, by1)
                break

        if not fund_name:
            fund_name = f"Abakkus Fund Page {page_num + 1}"

        if fund_name in seen_names:
            continue
        seen_names.add(fund_name)

        crop_and_save(page, table_rect, zoom, amc_dir, fund_name, fund_rect=fund_rect)
        success = True

    return success


def process_amc_sundaram(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    Sundaram PRC extractor — STRICT TABLE LOGIC

    ✅ Detects 'PRC Matrix'
    ✅ Identifies table region below it
    ✅ Extracts fund names ONLY inside table
    ✅ Crops ONLY the table
    ✅ No footer, no noise
    """

    if seen_names is None:
        seen_names = set()

    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        blocks = page.get_text("blocks")

        # ==================================================
        # ✅ STEP 1: FIND PRC MATRIX
        # ==================================================
        prc_y = None

        for b in blocks:
            if "prc matrix" in b[4].lower():
                prc_y = b[1]
                break

        if prc_y is None:
            continue

        # ==================================================
        # ✅ STEP 2: FIND FUND ROWS INSIDE TABLE
        # ==================================================
        fund_blocks = []

        for b in blocks:
            by0, by1, text = b[1], b[3], b[4].strip()
            tl = text.lower()

            # Only consider content BELOW PRC
            if by0 <= prc_y:
                continue

            # ✅ STRICT FUND FILTER (table rows only)
            if (
                tl.startswith("sundaram") and   # must start correctly
                "fund" in tl and
                "www" not in tl
            ):
                cleaned = re.sub(r'\(.*?\)', '', text)
                cleaned = " ".join(cleaned.split())

                # Cut exact fund name
                if "Fund" in cleaned:
                    idx = cleaned.find("Fund") + len("Fund")
                    cleaned = cleaned[:idx].strip()

                fund_blocks.append((by0, by1, cleaned))

        if not fund_blocks:
            continue

        # ==================================================
        # ✅ STEP 3: SORT + UNIQUE
        # ==================================================
        fund_blocks.sort(key=lambda x: x[0])

        fund_list = []
        for by0, by1, name in fund_blocks:
            if not any(x[2] == name for x in fund_list):
                fund_list.append((by0, by1, name))

        # ==================================================
        # ✅ STEP 4: DEFINE TABLE REGION (TIGHT)
        # ==================================================
        top_y = prc_y       
        bottom_y = max(b[1] for b in fund_blocks)

        table_rect = fitz.Rect(
            10,
            top_y - 5,                  # small margin for header row
            page.rect.width - 10,
            bottom_y + 5
        )

        # ==================================================
        # ✅ STEP 5: SAVE
        # ==================================================
        for by0, by1, name in fund_list:

            fn = strip_fund_name(name)

            if not fn:
                continue

            if fn in seen_names:
                continue

            seen_names.add(fn)

            # Construct fund_rect based on the block vertical bounds
            # The column is on the left, so let's set x0=10 and x1=250
            fund_rect = fitz.Rect(10, by0, 250, by1)

            crop_and_save(page, table_rect, zoom, amc_dir, fn, fund_rect=fund_rect)

            success = True

    return success


def process_amc_mirae(doc, output_dir, zoom, amc_dir, seen_names=None):
    """
    Mirae Asset MF — Isolated PRC extractor.
    Mirae has a consolidated PRC Matrix table page(s) (Page 79 & 80 in active factsheet, 
    and Page 124 & 125 in passive factsheet).
    There are two columns of PRC matrix images per fund in the active factsheet,
    but only one column in the passive factsheet.
    We group images by horizontal position and pick the left-most column.
    """
    if seen_names is None:
        seen_names = set()
    success = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        
        # Check if page is a PRC matrix page
        text = page.get_text("text").lower()
        if not (("potential risk" in text and "matrix" in text) or "potential risk class" in text or "prc matrix" in text):
            continue
        if is_snapshot_page(page):
            continue

        # Get all text blocks on the page to identify fund names and their y-coordinates
        blocks = page.get_text("blocks")
        left_blocks = []
        for b in blocks:
            bx0, by0, bx1, by1, btext = b[0], b[1], b[2], b[3], b[4].strip()
            # Only keep blocks starting in the fund name column (x0 < 100)
            if bx0 < 100 and len(btext) > 0:
                left_blocks.append((bx0, by0, bx1, by1, btext))

        # Sort by y0
        left_blocks.sort(key=lambda x: x[1])

        # Merge vertically adjacent blocks (within 15 points)
        merged_blocks = []
        for b in left_blocks:
            if not merged_blocks:
                merged_blocks.append(b)
                continue
            prev_x0, prev_y0, prev_x1, prev_y1, prev_text = merged_blocks[-1]
            bx0, by0, bx1, by1, btext = b
            if by0 - prev_y1 < 15:
                new_text = prev_text + "\n" + btext
                new_x0 = min(prev_x0, bx0)
                new_y0 = min(prev_y0, by0)
                new_x1 = max(prev_x1, bx1)
                new_y1 = max(prev_y1, by1)
                merged_blocks[-1] = (new_x0, new_y0, new_x1, new_y1, new_text)
            else:
                merged_blocks.append(b)

        fund_candidates = []
        for bx0, by0, bx1, by1, btext in merged_blocks:
            if "mirae" in btext.lower():
                fund_name = strip_fund_name(btext)
                if fund_name:
                    fund_candidates.append((by0, by1, bx0, bx1, fund_name))

        # Get all images on this page
        image_info = page.get_image_info(xrefs=True)
        x_groups = {}
        for img in image_info:
            bbox = img['bbox']
            x0 = bbox[0]
            # Ignore headers, logos, or tiny graphics (e.g. y0 < 100 or x0 < 100 or width < 50)
            if bbox[1] < 100 or x0 < 100 or (bbox[2] - bbox[0]) < 50:
                continue
            
            # Group by x0
            found = False
            for gk in x_groups:
                if abs(gk - x0) < 15:
                    x_groups[gk].append(bbox)
                    found = True
                    break
            if not found:
                x_groups[x0] = [bbox]

        if not x_groups:
            continue

        # Sort groups by x-coordinate and pick the left-most one (Current PRC)
        sorted_groups = sorted(x_groups.items(), key=lambda x: x[0])
        left_images = sorted_groups[0][1]

        # Sort lists by y-coordinate
        fund_candidates.sort(key=lambda x: x[0])
        left_images.sort(key=lambda x: x[1])

        # Match each left-hand image with the fund candidate closest vertically
        for img_bbox in left_images:
            ix0, iy0, ix1, iy1 = img_bbox
            best_fund_info = None
            min_dist = 99999
            for fy0, fy1, fx0, fx1, fname in fund_candidates:
                dist = abs((fy0 + fy1)/2 - (iy0 + iy1)/2)
                if dist < min_dist:
                    min_dist = dist
                    best_fund_info = (fname, fx0, fy0, fx1, fy1)

            if best_fund_info:
                fund_name, fx0, fy0, fx1, fy1 = best_fund_info
                fund_rect = fitz.Rect(fx0, fy0, fx1, fy1)
            else:
                fund_name = f"Mirae Asset Fund Page {page_num + 1} Y_{iy0:.1f}"
                fund_rect = None

            if fund_name in seen_names:
                success = True
                continue
            seen_names.add(fund_name)

            # Crop the page using the image's bbox (with a small padding of 2 points)
            crop_rect = fitz.Rect(ix0 - 2, iy0 - 2, ix1 + 2, iy1 + 2)
            crop_and_save(page, crop_rect, zoom, amc_dir, fund_name, fund_rect=fund_rect)
            success = True

    return success


# ---------------------------------------------------------------------------
# AMC Dispatch Table
# ---------------------------------------------------------------------------

AMC_DISPATCH = {
    "360": process_amc_360,
    "angel_one": process_amc_angel_one,
    "boi": process_amc_boi,
    "choice": process_amc_choice,
    "dsp": process_amc_dsp,
    "groww": process_amc_groww,
    "hdfc": process_amc_hdfc,
    "hsbc": process_amc_hsbc,
    "iti": process_amc_iti,
    "jm": process_amc_jm,
    "jio": process_amc_jio,
    "lic": process_amc_lic,
    "nj": process_amc_nj,
    "pgim": process_amc_pgim,
    "ppfas": process_amc_ppfas,
    "sbi": process_amc_sbi,
    "absl": process_amc_absl,
    "axis": process_amc_axis,
    "bajaj": process_amc_bajaj,
    "bandhan": process_amc_bandhan,
    "baroda": process_amc_baroda,
    "canara": process_amc_canara,
    "capital": process_amc_capital,
    "edelweiss": process_amc_edelweiss,
    "franklin": process_amc_franklin,
    "helios": process_amc_helios,
    "icici": process_amc_icici,
    "invesco": process_amc_invesco,
    "kotak": process_amc_kotak,
    "mahindra": process_amc_mahindra,
    "mirae": process_amc_mirae,
    "motilal": process_amc_motilal,
    "navi": process_amc_navi,
    "nippon": process_amc_nippon,
    "old": process_amc_old,
    "quant": process_amc_quant,
    "quantum": process_amc_quantum,
    "samco": process_amc_samco,
    "shriram": process_amc_shriram,
    "taurus": process_amc_taurus,
    "tata": process_amc_tata,
    "trust": process_amc_trust,
    "unifi": process_amc_unifi,
    "union": process_amc_union,
    "uti": process_amc_uti,
    "wealth": process_amc_wealth,
    "whiteoak": process_amc_whiteoak,
    "abbakkus": process_amc_abbakkus,
    "sundaram": process_amc_sundaram,
}


# ---------------------------------------------------------------------------
# AMC Detection
# ---------------------------------------------------------------------------

def detect_amc_from_pdf(pdf_path, doc):
    """Detects which AMC a PDF belongs to. Returns an AMC key string or None."""
    filename = os.path.basename(pdf_path).lower()
    first_page_text = doc[0].get_text("text").lower() if len(doc) > 0 else ""
    sample = first_page_text[:2000]

    fname_map = [
        ("360", "360"), ("angel", "angel_one"), ("boi", "boi"),
        ("choice_mutual", "choice"), ("dsp", "dsp"), ("groww", "groww"),
        ("hdfc", "hdfc"), ("hsbc", "hsbc"), ("iti", "iti"),
        ("jm", "jm"), ("jio", "jio"), ("lic", "lic"), ("nj", "nj"),
        ("pgim", "pgim"), ("ppfas", "ppfas"), ("parag", "ppfas"),
        ("sbimf", "sbi"), ("sbi", "sbi"), ("absl", "absl"),
        ("birla", "absl"), ("axis", "axis"), ("bajaj", "bajaj"),
        ("bandhan", "bandhan"), ("bbnpp", "baroda"), ("baroda", "baroda"),
        ("capitalmind", "capital"), ("edelweiss", "edelweiss"),
        ("franklin", "franklin"), ("helios", "helios"), ("icici", "icici"),
        ("invesco", "invesco"), ("kotak", "kotak"), ("mahindra", "mahindra"),
        ("manulife", "mahindra"), ("mirae", "mirae"), ("motilal", "motilal"),
        ("most_", "motilal"), ("navi", "navi"), ("nippon", "nippon"),
        ("old_bridge", "old"), ("samc-", "shriram"), ("shriram", "shriram"),
        ("taurus", "taurus"), ("tata", "tata"), ("trustmf", "trust"),
        ("trust", "trust"), ("unifi", "unifi"), ("union", "union"),
        ("uti_", "uti"), ("uti-", "uti"), ("wealth", "wealth"),
        ("whiteoak", "whiteoak"), ("white_oak", "whiteoak"),
        ("abakkus", "abbakkus"), ("sundaram", "sundaram"),
    ]

    # Special: "quant" but not "quantum"
    if "quantum" in filename:
        return "quantum"
    if "quant" in filename:
        return "quant"

    for key, amc in fname_map:
        if key in filename:
            return amc

    text_map = [
        ("360 one", "360"), ("angel one", "angel_one"), ("bank of india", "boi"),
        ("dsp mutual", "dsp"), ("groww mutual", "groww"), ("hdfc mutual", "hdfc"),
        ("hsbc mutual", "hsbc"), ("iti mutual", "iti"), ("jm financial", "jm"),
        ("jioblackrock", "jio"), ("lic mutual", "lic"), ("nj amc", "nj"),
        ("pgim india", "pgim"), ("parag parikh", "ppfas"), ("sbi mutual", "sbi"),
        ("aditya birla", "absl"), ("axis mutual", "axis"), ("bajaj finserv", "bajaj"),
        ("bandhan mutual", "bandhan"), ("baroda bnp", "baroda"),
        ("canara robeco", "canara"), ("capitalmind", "capital"),
        ("edelweiss mutual", "edelweiss"), ("franklin india", "franklin"),
        ("helios mutual", "helios"), ("icici prudential", "icici"),
        ("invesco india", "invesco"), ("kotak mahindra", "kotak"),
        ("mahindra manulife", "mahindra"), ("mirae asset", "mirae"),
        ("motilal oswal", "motilal"), ("navi mutual", "navi"),
        ("nippon india", "nippon"), ("old bridge", "old"),
        ("quantum asset", "quantum"), ("quant mutual", "quant"),
        ("samco asset", "samco"), ("shriram asset", "shriram"),
        ("taurus asset", "taurus"), ("tata asset", "tata"),
        ("tata mutual", "tata"), ("trust mutual", "trust"),
        ("unifi mutual", "unifi"), ("union asset", "union"),
        ("union mutual", "union"), ("uti asset", "uti"),
        ("uti mutual", "uti"), ("the wealth company", "wealth"),
        ("whiteoak capital", "whiteoak"), ("abakkus asset", "abbakkus"),
        ("sundaram asset", "sundaram"), ("sundaram mutual", "sundaram"),
    ]

    for key, amc in text_map:
        if key in sample:
            return amc

    return None


# ---------------------------------------------------------------------------
# Core Processing — PDF and Folder Level
# ---------------------------------------------------------------------------

def process_pdf(pdf_path, output_dir, zoom, amc_key, amc_dir, seen_names):
    """
    Processes a single PDF: opens it, runs the AMC-specific processor,
    and reports per-PDF warnings.

    Returns True if at least one PRC table was found and saved.
    """
    filename = os.path.basename(pdf_path)
    print(f"\n  Processing: {filename}")

    # Clear global mappings for the new PDF
    CURRENT_PDF_MAPPINGS.clear()

    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        print(f"    [ERROR] Could not open PDF: {e}")
        return False

    processor = AMC_DISPATCH.get(amc_key)
    if processor is None:
        print(f"    [WARNING] No processor registered for AMC key: {amc_key}")
        doc.close()
        return False

    success = False
    try:
        success = processor(doc, output_dir, zoom, amc_dir, seen_names)
        
        # After processor finishes, draw and save page-level debug images
        if CURRENT_PDF_MAPPINGS:
            for page_num, crops in CURRENT_PDF_MAPPINGS.items():
                page = doc[page_num]
                save_page_debug_image(page, amc_dir, filename, page_num, crops, zoom=2.0)
    except Exception as e:
        print(f"    [ERROR] Exception while processing {filename}: {e}")
        import traceback
        traceback.print_exc()

    doc.close()

    if not success:
        print(f"    [WARNING] No PRC table found in '{filename}'. "
              f"Requires manual check.")

    return success


def process_folder(input_dir, output_dir, zoom=3.0):
    """
    Main batch processor.

    Scans each immediate subdirectory of input_dir as one AMC.
    All PDFs in a given AMC subdirectory share a single seen_names set so
    each fund produces exactly one crop regardless of how many PDFs it appears in.

    Prints a warning if an entire AMC directory yields no PRC tables.
    """
    os.makedirs(output_dir, exist_ok=True)

    # Collect AMC subdirectories
    try:
        amc_subdirs = sorted([
            d for d in os.listdir(input_dir)
            if os.path.isdir(os.path.join(input_dir, d))
        ])
    except Exception as e:
        print(f"[ERROR] Cannot list input directory: {e}")
        return

    if not amc_subdirs:
        # Fallback: treat root directory as single AMC folder
        amc_subdirs = ["."]

    total_amcs = 0
    success_amcs = 0

    for amc_subdir in amc_subdirs:
        amc_path = os.path.join(input_dir, amc_subdir)
        pdfs = sorted([
            f for f in os.listdir(amc_path)
            if f.lower().endswith('.pdf')
        ])

        if not pdfs:
            continue

        total_amcs += 1
        print(f"\n{'=' * 50}")
        print(f"AMC Folder: {amc_subdir}  ({len(pdfs)} PDF{'s' if len(pdfs) > 1 else ''})")
        print(f"{'=' * 50}")

        # Detect AMC from first PDF
        first_pdf_path = os.path.join(amc_path, pdfs[0])
        try:
            probe_doc = fitz.open(first_pdf_path)
            amc_key = detect_amc_from_pdf(first_pdf_path, probe_doc)
            probe_doc.close()
        except Exception as e:
            print(f"  [ERROR] Cannot probe first PDF for AMC detection: {e}")
            continue

        if amc_key is None:
            folder_map = {
                "360": "360", "angel one": "angel_one", "boi": "boi", "choice": "choice",
                "dsp": "dsp", "groww": "groww", "hdfc": "hdfc", "hsbc": "hsbc", "iti": "iti",
                "jm": "jm", "jio": "jio", "lic": "lic", "nj": "nj", "pgim": "pgim",
                "ppfas": "ppfas", "sbi": "sbi", "aditya": "absl", "axis": "axis",
                "bajaj": "bajaj", "bandhan": "bandhan", "baroda": "baroda",
                "canara": "canara", "capital": "capital", "edelweiss": "edelweiss",
                "franklin": "franklin", "helios": "helios", "icici": "icici",
                "invesco": "invesco", "kotak": "kotak", "mahindra": "mahindra",
                "mirae": "mirae", "motilal": "motilal", "navi": "navi",
                "nippon": "nippon", "old": "old", "quant": "quant", "quantum": "quantum",
                "samco": "samco", "shriram": "shriram", "taurus": "taurus", "tata": "tata",
                "trust": "trust", "unifi": "unifi", "union": "union", "uti": "uti",
                "wealth": "wealth", "whiteoak": "whiteoak", "abbakkus": "abbakkus",
                "sundaram": "sundaram"
            }
            amc_key = folder_map.get(amc_subdir.lower().strip())

        if amc_key is None:
            print(f"  [WARNING] Cannot identify AMC for folder '{amc_subdir}'. Skipping.")
            continue

        print(f"  [DETECTED AMC] -> {amc_key.upper()}")

        amc_dir = os.path.join(output_dir, amc_subdir.upper())
        os.makedirs(amc_dir, exist_ok=True)

        amc_success = False

        for pdf_file in pdfs:
            # Bounded deduplication set — unique per PDF to ensure all files crop fully
            seen_names = set()
            pdf_path = os.path.join(amc_path, pdf_file)
            pdf_success = process_pdf(pdf_path, output_dir, zoom,
                                      amc_key, amc_dir, seen_names)
            if pdf_success:
                amc_success = True

        if not amc_success:
            print(f"\n  [WARNING] No PRC tables extracted for AMC '{amc_subdir}'. "
                  f"Manual inspection required.")
        else:
            success_amcs += 1

    print(f"\n{'=' * 50}")
    print(f"Batch Complete: {success_amcs}/{total_amcs} AMC folders had PRC tables extracted.")
    print(f"Output saved in: {os.path.abspath(output_dir)}")



def main():
    if not os.path.isdir(INPUT_DIR):
        print(f"Error: Input directory does not exist: {INPUT_DIR}")
        sys.exit(1)

    process_folder(INPUT_DIR, OUTPUT_DIR, ZOOM)

if __name__ == "__main__":
    main()
