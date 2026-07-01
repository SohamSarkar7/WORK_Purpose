#!/usr/bin/env python3
"""
CAMS NAV & IDCW Scraper - Async Playwright, Multi-Tab Concurrent Version
--------------------------------------------------------------------------

Rewrite of the Selenium ThreadPoolExecutor scraper using async Playwright.
Concurrency model:
    - One Chromium *process* is launched.
    - Each mutual fund gets its own isolated BrowserContext + Page
      ("tab"). Contexts give cookie/storage isolation similar to the
      old "--incognito" per-driver approach, but far cheaper than a
      whole new OS process per fund.
    - An asyncio.Semaphore(MAX_CONCURRENT_TABS) bounds how many tabs
      run at once, so N funds are scraped in parallel without opening
      an unbounded number of tabs against CAMS' site at once.
    - Every await point in Playwright is already non-blocking I/O, so
      asyncio.gather-style concurrency gives real parallelism here
      (unlike raw Selenium, which needed threads to work around a
      single-command-at-a-time WebDriver session).

FAST-FAIL IDCW DETECTION (requirement 6/7)
    Playwright's `page.wait_for_function()` polls a JS predicate and
    raises TimeoutError the moment its timeout elapses without the
    predicate ever returning truthy. That is used directly (no manual
    polling loops, no long fixed sleeps) to enforce a strict 1-2s
    ceiling on "does the IDCW tab exist for this scheme". If it times
    out, the scheme is logged and skipped immediately - no retries on
    that specific failure mode, exactly mirroring the original
    "SKIPPED_NO_IDCW_TAB / do not retry" rule.

WHAT WAS DELIBERATELY KEPT FROM THE ORIGINAL SELENIUM SCRIPT
    The DOM-querying JavaScript itself (tab-text matching, table
    detection/extraction heuristics, ng-select virtual-scroll option
    collection) is carried over almost verbatim - only the driver call
    changed from `driver.execute_script(...)` to
    `await page.evaluate(...)`. That JS was already validated against
    the live CAMS Angular/Material app, so there's no reason to
    reinvent it.

NOTE ON TESTING
    This sandbox's network egress allowlist does not include
    camsonline.com, so this script could not be executed against the
    live site from here. It has been syntax-checked and each Playwright
    call has been reviewed against the documented API. Run it in an
    environment with normal internet access and:
        pip install playwright pandas openpyxl
        playwright install chromium
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

try:
    from playwright.async_api import (
        async_playwright,
        Browser,
        Page,
        TimeoutError as PWTimeoutError,
    )
except ImportError:
    print(
        "Playwright is not installed. Run:\n"
        "    pip install playwright pandas openpyxl\n"
        "    playwright install chromium",
        file=sys.stderr,
    )
    raise


# ============================================================
# CONFIG
# ============================================================

URL = "https://www.camsonline.com/Investors/Transactions/Other-services/NAV&IDCW"

# If True, scrape every fund found on the landing page. If False,
# scrape only the funds listed in FUND_NAMES.
SCRAPE_ALL_FUNDS = False
FUND_NAMES: List[str] = ["360 ONE Mutual Fund"]

# How many funds get their own tab (context+page) running at once.
# Keep this modest - many concurrent tabs against one site is exactly
# the kind of traffic that trips rate limiting/anti-bot defenses.
MAX_CONCURRENT_TABS = 4

HEADLESS = True

# None = collect every IDCW scheme for a fund. An int caps how many.
MAX_SCHEMES: Optional[int] = None

# Extra attempts (beyond the first) for a single scheme before its
# status is accepted as final. Schemes with a missing IDCW tab are
# never retried (see scrape_scheme_with_retry).
MAX_SCHEME_RETRIES = 1

# Extra attempts (beyond the first, fresh context+page each time) for
# an entire fund before it is accepted as final.
MAX_FUND_RETRIES = 1

RETRY_BACKOFF_SECONDS = 0.3

BASE_DIR = os.getcwd()
OUTPUT_EXCEL_FILE = os.path.join(BASE_DIR, "cams_idcw_output.xlsx")
OUTPUT_SCHEME_JSON_FILE = os.path.join(BASE_DIR, "cams_scheme_names.json")
LOG_FILE = os.path.join(BASE_DIR, "cams_scraper.log")
CHECKPOINT_DIR = os.path.join(BASE_DIR, "cams_checkpoints")

# Skip a fund entirely if it already has a real (non-empty) checkpoint
# from a previous run, so a crash mid-run doesn't force a full re-scrape.
RESUME_FROM_CHECKPOINTS = False

WRITE_INDIVIDUAL_SCHEME_SHEETS = False


# ============================================================
# TIMEOUTS (milliseconds - Playwright's native unit)
# ============================================================

DEFAULT_TIMEOUT_MS = 10_000
SHORT_TIMEOUT_MS = 3_000
POPUP_TIMEOUT_MS = 900          # per-candidate popup-dismiss attempt
POST_SUBMIT_TIMEOUT_MS = 2_000

# Strict fast-fail ceiling: requirement 6. If the IDCW tab hasn't
# appeared within this window, stop waiting - do not use a standard
# long timeout.
IDCW_TAB_TIMEOUT_MS = 1_500
IDCW_TABLE_TIMEOUT_MS = 2_000

FAST_RETURN_TIMEOUT_MS = 900


# ============================================================
# SELECTORS
# ============================================================

SCHEME_DROPDOWN_SELECTOR = (
    "ng-select[formcontrolname='schemename'], ng-select[placeholder='Scheme Name']"
)
BACK_BUTTON_SELECTOR = "button.check-now-btn.csp-class-2"
SUBMIT_SELECTORS = ["input[type='submit']", "button:has-text('Submit')"]

POPUP_DISMISS_SELECTORS = [
    "input[type='checkbox']",
    "button:has-text('I Agree')",
    "button:has-text('I agree')",
    "button:has-text('Agree')",
    "button:has-text('Accept')",
    "button:has-text('Proceed')",
    "button:has-text('Continue')",
    "button:has-text('OK')",
]


# ============================================================
# LOGGING
# ============================================================

logger = logging.getLogger("cams_scraper")
logger.setLevel(logging.INFO)
logger.propagate = False

if not logger.handlers:
    _formatter = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")

    _file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    _file_handler.setFormatter(_formatter)
    logger.addHandler(_file_handler)

    _console_handler = logging.StreamHandler(sys.stdout)
    _console_handler.setFormatter(_formatter)
    logger.addHandler(_console_handler)


def log_info(fund_name: str, message: str) -> None:
    logger.info(f"[{fund_name}] {message}")


def log_warn(fund_name: str, message: str) -> None:
    logger.warning(f"[{fund_name}] {message}")


def log_error(fund_name: str, message: str) -> None:
    logger.error(f"[{fund_name}] {message}")


# ============================================================
# DATA MODEL
# ============================================================

@dataclass
class ScrapeResult:
    fund_name: str
    scheme_name: str
    dataframe: pd.DataFrame
    status: str
    message: str


# ============================================================
# BASIC HELPERS
# ============================================================

def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def safe_sheet_name(name: str, existing_names: set) -> str:
    sheet_name = re.sub(r"[\\/*?:\[\]]", "_", name).strip()
    sheet_name = sheet_name or "Sheet"
    sheet_name = sheet_name[:31]

    base_name = sheet_name
    counter = 1
    while sheet_name in existing_names:
        suffix = f"_{counter}"
        sheet_name = base_name[: 31 - len(suffix)] + suffix
        counter += 1

    existing_names.add(sheet_name)
    return sheet_name


def safe_file_token(name: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_\-]+", "_", name).strip("_")
    return (token or "fund")[:80]


# ============================================================
# JS SNIPPETS
# (kept as close as possible to the validated Selenium version -
#  only the calling convention changed, per module docstring.)
# ============================================================

FUND_ROWS_JS = r"""
() => {
    function cleanText(v){ return (v||'').replace(/\s+/g,' ').trim(); }
    const rowSets = [
        Array.from(document.querySelectorAll('table tbody tr')).filter(r => r.querySelectorAll('td').length > 0),
        Array.from(document.querySelectorAll('tr.mat-row')).filter(r => r.querySelectorAll('td').length > 0),
    ];
    let rows = [];
    for (const set of rowSets) { if (set.length) { rows = set; break; } }
    const results = [];
    rows.forEach((row, idx) => {
        const cells = Array.from(row.querySelectorAll('td'));
        if (cells.length < 2) return;
        const name = cleanText(cells[1].innerText || cells[1].textContent);
        if (name) results.push({ index: idx, name: name });
    });
    return results;
}
"""

FUND_ROWS_READY_JS = r"""
() => {
    const a = document.querySelectorAll('table tbody tr').length;
    const b = document.querySelectorAll('tr.mat-row').length;
    return (a + b) > 0;
}
"""

CLICK_FUND_ROW_JS = r"""
(idx) => {
    const rowSets = [
        Array.from(document.querySelectorAll('table tbody tr')).filter(r => r.querySelectorAll('td').length > 0),
        Array.from(document.querySelectorAll('tr.mat-row')).filter(r => r.querySelectorAll('td').length > 0),
    ];
    let rows = [];
    for (const set of rowSets) { if (set.length) { rows = set; break; } }
    const row = rows[idx];
    if (!row) return false;
    const cells = Array.from(row.querySelectorAll('td'));
    if (cells.length < 2) return false;
    const fundCell = cells[1];
    let clickable = fundCell;
    const child = fundCell.querySelector('a, button, u');
    if (child) clickable = child;
    clickable.scrollIntoView({ block: 'center', inline: 'nearest' });
    clickable.click();
    return true;
}
"""

GET_OPTIONS_JS = r"""
() => {
    function cleanText(v){ return (v||'').replace(/\s+/g,' ').trim(); }
    return Array.from(document.querySelectorAll('ng-dropdown-panel .ng-option'))
        .map((o, i) => ({ index: i, text: cleanText(o.innerText || o.textContent) }))
        .filter(o => o.text);
}
"""

OPTIONS_PRESENT_JS = "() => document.querySelectorAll('ng-dropdown-panel .ng-option').length > 0"

CLICK_OPTION_JS = r"""
(idx) => {
    const options = Array.from(document.querySelectorAll('ng-dropdown-panel .ng-option'));
    const option = options[idx];
    if (!option) return false;
    option.scrollIntoView({ block: 'center', inline: 'nearest' });
    option.click();
    return true;
}
"""

TAB_EXISTS_JS = r"""
() => {
    const tabs = Array.from(document.querySelectorAll(
        "div.mat-tab-label-container div[role='tab'], div.mat-tab-list div[role='tab']"
    ));
    return tabs.some(tab => {
        const text = (tab.innerText || tab.textContent || '').replace(/\s+/g,' ').trim().toLowerCase();
        return text.includes('idcw');
    });
}
"""

CLICK_IDCW_TAB_JS = r"""
() => {
    const tabs = Array.from(document.querySelectorAll(
        "div.mat-tab-label-container div[role='tab'], div.mat-tab-list div[role='tab']"
    ));
    for (const tab of tabs) {
        const text = (tab.innerText || tab.textContent || '').replace(/\s+/g,' ').trim().toLowerCase();
        if (text.includes('idcw')) {
            tab.scrollIntoView({ block: 'center', inline: 'center' });
            tab.click();
            return true;
        }
    }
    return false;
}
"""

IS_TABLE_PRESENT_JS = r"""
() => {
    function isVisible(el) {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const s = window.getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
    }
    function cleanText(v){ return (v||'').replace(/\s+/g,' ').trim().toLowerCase(); }

    const candidates = [];
    const activeBody = document.querySelector('mat-tab-body.mat-tab-body-active');
    if (activeBody) {
        candidates.push(...activeBody.querySelectorAll('table.navdi'));
        candidates.push(...activeBody.querySelectorAll('table'));
    }
    candidates.push(...document.querySelectorAll('table.navdi'));
    candidates.push(...document.querySelectorAll('table'));

    const uniqueTables = Array.from(new Set(candidates));

    for (const table of uniqueTables) {
        if (!isVisible(table)) continue;
        const rows = Array.from(table.querySelectorAll('tbody tr'));
        const hasRows = rows.some(row => row.querySelectorAll('td').length > 0);
        if (!hasRows) continue;

        const tableText = cleanText(table.innerText || table.textContent);
        if (
            table.classList.contains('navdi') ||
            tableText.includes('idcw date') ||
            tableText.includes('idcw per unit') ||
            tableText.includes('corporate') ||
            tableText.includes('retail')
        ) {
            return true;
        }
    }
    return false;
}
"""

EXTRACT_TABLE_JS = r"""
() => {
    function cleanText(v){ return (v||'').replace(/\s+/g,' ').trim(); }
    function isVisible(el) {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const s = window.getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
    }
    function extractTable(table) {
        const headers = Array.from(table.querySelectorAll('thead th'))
            .map(th => cleanText(th.innerText || th.textContent))
            .filter(Boolean);
        const rows = Array.from(table.querySelectorAll('tbody tr'))
            .map(tr => Array.from(tr.querySelectorAll('td')).map(td => cleanText(td.innerText || td.textContent)))
            .filter(row => row.length && row.some(Boolean));
        if (!rows.length) return null;
        return { headers: headers, rows: rows };
    }

    const candidates = [];
    const activeBody = document.querySelector('mat-tab-body.mat-tab-body-active');
    if (activeBody) {
        candidates.push(...activeBody.querySelectorAll('table.navdi'));
        candidates.push(...activeBody.querySelectorAll('table'));
    }
    candidates.push(...document.querySelectorAll('table.navdi'));
    candidates.push(...document.querySelectorAll('table'));

    const uniqueTables = Array.from(new Set(candidates));

    for (const table of uniqueTables) {
        if (!isVisible(table)) continue;
        const tableText = cleanText(table.innerText || table.textContent).toLowerCase();
        const looksLikeIDCW =
            table.classList.contains('navdi') ||
            tableText.includes('idcw date') ||
            tableText.includes('idcw per unit') ||
            tableText.includes('retail') ||
            tableText.includes('corporate');
        if (!looksLikeIDCW) continue;

        const extracted = extractTable(table);
        if (extracted && extracted.rows && extracted.rows.length) return extracted;
    }
    return null;
}
"""

POST_SUBMIT_READY_JS = r"""
() => {
    const bodyText = (document.body.innerText || '').toLowerCase();
    if (bodyText.includes('idcw')) return true;
    if (bodyText.includes('nav') && bodyText.includes('scheme')) return true;
    return document.querySelectorAll('button.check-now-btn.csp-class-2').length > 0;
}
"""


# ============================================================
# POPUP HANDLING
# ============================================================

async def handle_declaration_popup(page: Page) -> None:
    """
    Best-effort, fast dismissal of a declaration/disclaimer overlay.

    Each candidate gets a small bounded wait (POPUP_TIMEOUT_MS). If a
    checkbox appears, it's ticked; then any agree/accept/proceed style
    button is clicked if present. If nothing matches at all, this
    returns almost immediately - no long fixed timeout is ever spent
    on a popup that isn't there.
    """
    try:
        checkbox = page.locator("input[type='checkbox']").first
        await checkbox.wait_for(state="visible", timeout=POPUP_TIMEOUT_MS)
        await checkbox.check(timeout=POPUP_TIMEOUT_MS)
        logger.info("Declaration checkbox ticked.")
    except PWTimeoutError:
        pass
    except Exception:
        pass

    for selector in POPUP_DISMISS_SELECTORS[1:]:
        try:
            button = page.locator(selector).first
            await button.wait_for(state="visible", timeout=POPUP_TIMEOUT_MS)
            await button.click(timeout=POPUP_TIMEOUT_MS)
            logger.info(f"Declaration popup dismissed via: {selector}")
            await asyncio.sleep(0.15)
            return
        except PWTimeoutError:
            continue
        except Exception:
            continue


async def wait_page_ready(page: Page) -> None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
    except PWTimeoutError:
        pass
    await asyncio.sleep(0.2)


# ============================================================
# FUND SELECTION
# ============================================================

async def get_fund_rows(page: Page) -> List[Dict]:
    try:
        await page.wait_for_function(FUND_ROWS_READY_JS, timeout=DEFAULT_TIMEOUT_MS)
    except PWTimeoutError:
        raise RuntimeError("Mutual fund table rows not found.")

    rows = await page.evaluate(FUND_ROWS_JS)
    return rows or []


async def get_all_fund_names(page: Page) -> List[str]:
    rows = await get_fund_rows(page)
    return [row["name"] for row in rows]


async def select_fund(page: Page, target_fund_name: str) -> str:
    rows = await get_fund_rows(page)
    target = target_fund_name.lower().strip()

    exact_matches = [r for r in rows if r["name"].lower() == target]
    contains_matches = [
        r for r in rows if target in r["name"].lower() or r["name"].lower() in target
    ]
    matches = exact_matches or contains_matches

    if not matches:
        available = "\n".join(f"- {r['name']}" for r in rows[:80])
        raise ValueError(f"Fund not found: {target_fund_name}\n\nAvailable funds:\n{available}")

    selected = matches[0]
    log_info(target_fund_name, f"Selected fund: {selected['name']}")

    clicked = await page.evaluate(CLICK_FUND_ROW_JS, selected["index"])
    if not clicked:
        raise RuntimeError(f"Could not click fund row for: {selected['name']}")

    return selected["name"]


# ============================================================
# SCHEME DROPDOWN
# ============================================================

async def clear_scheme_dropdown(page: Page) -> None:
    try:
        clear_btn = page.locator("ng-select .ng-clear-wrapper").first
        if await clear_btn.count() > 0:
            await clear_btn.click(timeout=500)
            await asyncio.sleep(0.08)
    except Exception:
        pass


async def open_scheme_dropdown(page: Page):
    dropdown = page.locator(SCHEME_DROPDOWN_SELECTOR).first
    await dropdown.wait_for(state="visible", timeout=SHORT_TIMEOUT_MS)
    await dropdown.click(timeout=SHORT_TIMEOUT_MS)
    return dropdown


async def get_scheme_input(page: Page):
    input_box = page.locator("ng-dropdown-panel input, input[type='text']").first
    await input_box.wait_for(state="visible", timeout=SHORT_TIMEOUT_MS)
    return input_box


async def close_dropdown(page: Page) -> None:
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    await asyncio.sleep(0.08)


async def type_into_scheme_search(page: Page, search_text: str) -> None:
    await clear_scheme_dropdown(page)
    await open_scheme_dropdown(page)
    input_box = await get_scheme_input(page)
    await input_box.press("Control+a")
    await input_box.press("Backspace")
    await input_box.type(search_text)


async def collect_idcw_scheme_names(page: Page) -> List[str]:
    """
    Opens the scheme dropdown, searches "IDCW", then scrolls the
    virtual ng-dropdown-panel collecting every option whose text
    contains "idcw" (requirement 4: filter + index scheme names).
    """
    try:
        await type_into_scheme_search(page, "IDCW")
        await page.wait_for_function(OPTIONS_PRESENT_JS, timeout=SHORT_TIMEOUT_MS)
    except PWTimeoutError:
        await close_dropdown(page)
        return []

    collected: List[str] = []

    async def add_current_options() -> None:
        options = await page.evaluate(GET_OPTIONS_JS)
        for opt in options or []:
            text = clean_text(opt.get("text", ""))
            if not text:
                continue
            lower = text.lower()
            if "no scheme" in lower:
                continue
            if "idcw" in lower and text not in collected:
                collected.append(text)

    await add_current_options()

    no_new_count = 0
    for _ in range(80):
        try:
            before = len(collected)
            await page.evaluate(
                "() => { const p = document.querySelector('ng-dropdown-panel'); "
                "if (p) p.scrollTop = p.scrollTop + 450; }"
            )
            await asyncio.sleep(0.08)
            await add_current_options()
            after = len(collected)
            no_new_count = no_new_count + 1 if after == before else 0
            if no_new_count >= 5:
                break
        except Exception:
            break

    await close_dropdown(page)

    if MAX_SCHEMES:
        collected = collected[:MAX_SCHEMES]

    return collected


async def select_first_dropdown_option_after_search(page: Page, search_text: str) -> None:
    await type_into_scheme_search(page, search_text)

    try:
        await page.wait_for_function(OPTIONS_PRESENT_JS, timeout=SHORT_TIMEOUT_MS)
    except PWTimeoutError:
        await close_dropdown(page)
        raise TimeoutError(f"No dropdown option found after searching: {search_text}")

    options = await page.evaluate(GET_OPTIONS_JS)
    valid_options = [o for o in (options or []) if o.get("text") and "no scheme" not in o["text"].lower()]

    if not valid_options:
        await close_dropdown(page)
        raise TimeoutError(f"No dropdown option found after searching: {search_text}")

    first = valid_options[0]
    clicked = await page.evaluate(CLICK_OPTION_JS, first["index"])

    if not clicked:
        await close_dropdown(page)
        raise TimeoutError(f"Could not click first dropdown option for: {search_text}")

    await asyncio.sleep(0.12)


async def select_scheme_by_name(page: Page, scheme_name: str, fund_name: str) -> None:
    """
    1. Search the full scheme name, click the first visible result.
    2. If that fails, search "IDCW" and match by exact/contains text,
       falling back to the first IDCW option available.
    """
    try:
        await select_first_dropdown_option_after_search(page, scheme_name)
        return
    except Exception as first_error:
        log_warn(
            fund_name,
            f"Full-name selection failed for '{scheme_name}'. Trying IDCW fallback. "
            f"Reason: {first_error!r}",
        )

    await type_into_scheme_search(page, "IDCW")
    await asyncio.sleep(0.35)

    options = await page.evaluate(GET_OPTIONS_JS)
    target = clean_text(scheme_name).lower()

    for opt in options or []:
        option_clean = clean_text(opt.get("text", "")).lower()
        if not option_clean or "no scheme" in option_clean:
            continue
        if option_clean == target or target in option_clean or option_clean in target:
            clicked = await page.evaluate(CLICK_OPTION_JS, opt["index"])
            if clicked:
                return

    idcw_options = [
        o for o in (options or [])
        if "idcw" in o.get("text", "").lower() and "no scheme" not in o.get("text", "").lower()
    ]
    if idcw_options:
        clicked = await page.evaluate(CLICK_OPTION_JS, idcw_options[0]["index"])
        if clicked:
            return

    await close_dropdown(page)
    raise TimeoutError(f"Could not select scheme: {scheme_name}")


# ============================================================
# SUBMIT / RESULT PAGE
# ============================================================

async def click_submit(page: Page) -> None:
    for selector in SUBMIT_SELECTORS:
        try:
            button = page.locator(selector).first
            await button.wait_for(state="visible", timeout=SHORT_TIMEOUT_MS)
            await button.click(timeout=SHORT_TIMEOUT_MS)
            return
        except PWTimeoutError:
            continue
        except Exception:
            continue
    raise TimeoutError("Submit button not found.")


async def scheme_dropdown_present(page: Page) -> bool:
    try:
        return await page.locator(SCHEME_DROPDOWN_SELECTOR).count() > 0
    except Exception:
        return False


async def return_to_scheme_page_fast_after_no_idcw(page: Page, fund_name: str) -> None:
    """Fast return path when the IDCW tab was missing - avoid a full reload."""
    if await scheme_dropdown_present(page):
        return

    try:
        back_button = page.locator(BACK_BUTTON_SELECTOR).first
        if await back_button.count() > 0 and await back_button.is_visible():
            await back_button.click(timeout=FAST_RETURN_TIMEOUT_MS)
            await page.wait_for_function(
                f"() => document.querySelectorAll(\"{SCHEME_DROPDOWN_SELECTOR}\").length > 0",
                timeout=FAST_RETURN_TIMEOUT_MS,
            )
            return
    except Exception:
        pass

    try:
        await page.go_back(timeout=FAST_RETURN_TIMEOUT_MS)
        await page.wait_for_function(
            f"() => document.querySelectorAll(\"{SCHEME_DROPDOWN_SELECTOR}\").length > 0",
            timeout=FAST_RETURN_TIMEOUT_MS,
        )
        return
    except Exception:
        pass

    log_warn(fund_name, "Fast no-IDCW return failed. Using normal return flow.")
    await return_to_scheme_page(page, fund_name)


async def return_to_scheme_page(page: Page, fund_name: str) -> None:
    """
    1. Already on scheme page? Done.
    2. Try the CAMS back/change button.
    3. Try browser back.
    4. Final fallback: reload the URL and reselect the fund.
    """
    if await scheme_dropdown_present(page):
        return

    try:
        back_button = page.locator(BACK_BUTTON_SELECTOR).first
        if await back_button.count() > 0 and await back_button.is_visible():
            await back_button.click(timeout=SHORT_TIMEOUT_MS)
            await page.wait_for_selector(SCHEME_DROPDOWN_SELECTOR, timeout=SHORT_TIMEOUT_MS)
            await asyncio.sleep(0.12)
            return
    except Exception:
        pass

    try:
        await page.go_back(timeout=SHORT_TIMEOUT_MS)
        await page.wait_for_selector(SCHEME_DROPDOWN_SELECTOR, timeout=SHORT_TIMEOUT_MS)
        await asyncio.sleep(0.12)
        return
    except Exception:
        pass

    log_warn(fund_name, "Fast return failed. Reloading fund page.")
    await page.goto(URL, wait_until="domcontentloaded", timeout=35_000)
    await wait_page_ready(page)
    await select_fund(page, fund_name)
    await page.wait_for_selector(SCHEME_DROPDOWN_SELECTOR, timeout=DEFAULT_TIMEOUT_MS)
    await asyncio.sleep(0.2)


# ============================================================
# TABLE EXTRACTION
# ============================================================

def build_dataframe(table_data: Optional[dict]) -> pd.DataFrame:
    if not table_data or not table_data.get("rows"):
        return pd.DataFrame()

    rows = table_data["rows"]
    headers = table_data.get("headers") or []
    max_columns = max(len(row) for row in rows)

    if not headers or len(headers) != max_columns:
        headers = ["IDCW DATE", "IDCW PER UNIT(RETAIL)", "IDCW PER UNIT(CORPORATE)"][:max_columns]
        headers += [f"Column_{i}" for i in range(len(headers) + 1, max_columns + 1)]

    normalized_rows = [row + [""] * (max_columns - len(row)) for row in rows]
    return pd.DataFrame(normalized_rows, columns=headers)


# ============================================================
# SCHEME-LEVEL SCRAPE (with strict fast-fail IDCW detection)
# ============================================================

async def scrape_scheme(page: Page, fund_name: str, scheme_name: str) -> ScrapeResult:
    try:
        await select_scheme_by_name(page, scheme_name, fund_name)
        await click_submit(page)

        try:
            await page.wait_for_function(POST_SUBMIT_READY_JS, timeout=POST_SUBMIT_TIMEOUT_MS)
        except PWTimeoutError:
            pass  # not fatal - the IDCW-tab wait below is the real gate

        # --- Requirement 6: strict 1-2s fast-fail ceiling ---
        try:
            await page.wait_for_function(TAB_EXISTS_JS, timeout=IDCW_TAB_TIMEOUT_MS)
        except PWTimeoutError:
            log_warn(fund_name, f"'{scheme_name}': IDCW tab not found")
            return ScrapeResult(fund_name, scheme_name, pd.DataFrame(), "SKIPPED_NO_IDCW_TAB", "IDCW tab not found")

        clicked = await page.evaluate(CLICK_IDCW_TAB_JS)
        if not clicked:
            log_warn(fund_name, f"'{scheme_name}': IDCW tab not found")
            return ScrapeResult(fund_name, scheme_name, pd.DataFrame(), "SKIPPED_NO_IDCW_TAB", "IDCW tab not found")

        try:
            await page.wait_for_function(IS_TABLE_PRESENT_JS, timeout=IDCW_TABLE_TIMEOUT_MS)
        except PWTimeoutError:
            return ScrapeResult(fund_name, scheme_name, pd.DataFrame(), "SKIPPED_NO_IDCW_DATA", "IDCW table not loaded")

        table_data = await page.evaluate(EXTRACT_TABLE_JS)
        dataframe = build_dataframe(table_data)

        if dataframe.empty:
            return ScrapeResult(fund_name, scheme_name, pd.DataFrame(), "SKIPPED_NO_IDCW_DATA", "IDCW table empty")

        dataframe.insert(0, "Scheme Name", scheme_name)
        dataframe.insert(0, "Mutual Fund", fund_name)

        log_info(fund_name, f"Extracted {len(dataframe)} rows for: {scheme_name}")
        return ScrapeResult(fund_name, scheme_name, dataframe, "OK", "")

    except Exception as exc:
        log_warn(fund_name, f"Scheme error on '{scheme_name}'. Reason: {exc!r}")
        return ScrapeResult(fund_name, scheme_name, pd.DataFrame(), "ERROR_SKIPPED", repr(exc))


async def scrape_scheme_with_retry(page: Page, fund_name: str, scheme_name: str) -> ScrapeResult:
    """
    Retries a scheme up to MAX_SCHEME_RETRIES extra times - EXCEPT when
    the failure was "IDCW tab not found", which is never retried
    (fast-fail rule, requirement 6/7: log and move on immediately).
    """
    total_attempts = MAX_SCHEME_RETRIES + 1
    result: Optional[ScrapeResult] = None

    for attempt in range(1, total_attempts + 1):
        result = await scrape_scheme(page, fund_name, scheme_name)

        if result.status == "OK":
            if attempt > 1:
                log_info(fund_name, f"Recovered '{scheme_name}' on attempt {attempt}.")
            return result

        if result.status == "SKIPPED_NO_IDCW_TAB":
            log_info(fund_name, f"Skipping retry for '{scheme_name}': IDCW tab was not found.")
            return result

        if attempt >= total_attempts:
            log_warn(fund_name, f"Giving up on '{scheme_name}' after {attempt} attempt(s) -> {result.status}")
            return result

        log_warn(fund_name, f"'{scheme_name}' attempt {attempt}/{total_attempts} -> {result.status}. Retrying.")

        try:
            await return_to_scheme_page(page, fund_name)
        except Exception as exc:
            log_warn(fund_name, f"return_to_scheme_page failed before retry: {exc!r}")

        await asyncio.sleep(RETRY_BACKOFF_SECONDS)

    return result  # type: ignore[return-value]


# ============================================================
# FUND-LEVEL SCRAPE
# ============================================================

async def scrape_fund_once(page: Page, fund_name: str) -> List[ScrapeResult]:
    selected_fund = await select_fund(page, fund_name)
    await page.wait_for_selector(SCHEME_DROPDOWN_SELECTOR, timeout=DEFAULT_TIMEOUT_MS)

    scheme_names = await collect_idcw_scheme_names(page)

    if not scheme_names:
        return [
            ScrapeResult(selected_fund, "", pd.DataFrame(), "NO_SCHEMES", "No IDCW schemes found")
        ]

    results: List[ScrapeResult] = []

    for index, scheme_name in enumerate(scheme_names, start=1):
        log_info(selected_fund, f"Scheme {index}/{len(scheme_names)}: {scheme_name}")

        result = await scrape_scheme_with_retry(page, selected_fund, scheme_name)
        results.append(result)
        log_info(selected_fund, f"Status: {result.status}")

        if result.status == "SKIPPED_NO_IDCW_TAB":
            await return_to_scheme_page_fast_after_no_idcw(page, selected_fund)
        else:
            await return_to_scheme_page(page, selected_fund)

    return results


async def process_fund_with_retry(browser: Browser, semaphore: asyncio.Semaphore, fund_name: str) -> List[ScrapeResult]:
    """
    Runs in its own bounded "tab" (context + page). Retries the whole
    fund with a fresh context if the flow throws, or if zero IDCW
    schemes were found at all on a non-final attempt.
    """
    async with semaphore:
        total_attempts = MAX_FUND_RETRIES + 1
        last_results: Optional[List[ScrapeResult]] = None
        last_exception: Optional[Exception] = None

        for attempt in range(1, total_attempts + 1):
            context = None
            try:
                log_info(fund_name, f"Attempt {attempt}/{total_attempts}: opening tab.")

                context = await browser.new_context()
                page = await context.new_page()

                await page.goto(URL, wait_until="domcontentloaded", timeout=35_000)
                await handle_declaration_popup(page)
                await wait_page_ready(page)

                results = await scrape_fund_once(page, fund_name)
                found_any_scheme = any(r.status != "NO_SCHEMES" for r in results)

                if found_any_scheme or attempt == total_attempts:
                    return results

                log_warn(fund_name, f"Zero IDCW schemes found on attempt {attempt}. Retrying.")
                last_results = results

            except Exception as exc:
                last_exception = exc
                log_warn(fund_name, f"Attempt {attempt} raised: {exc!r}")

            finally:
                if context is not None:
                    try:
                        await context.close()
                    except Exception:
                        pass

            if attempt < total_attempts:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)

        if last_results is not None:
            return last_results

        log_error(fund_name, f"Fund permanently failed after {total_attempts} attempt(s).")
        return [
            ScrapeResult(fund_name, "", pd.DataFrame(), "FUND_ERROR_SKIPPED", repr(last_exception))
        ]


async def fetch_fund_name_list(browser: Browser) -> List[str]:
    context = await browser.new_context()
    page = await context.new_page()
    try:
        await page.goto(URL, wait_until="domcontentloaded", timeout=35_000)
        await handle_declaration_popup(page)
        await wait_page_ready(page)
        names = await get_all_fund_names(page)
        logger.info(f"Discovered {len(names)} funds on the landing page.")
        return names
    finally:
        await context.close()


# ============================================================
# CHECKPOINTING
# ============================================================

def checkpoint_path(fund_name: str) -> str:
    return os.path.join(CHECKPOINT_DIR, f"{safe_file_token(fund_name)}.csv")


def save_checkpoint(fund_name: str, results: List[ScrapeResult]) -> None:
    os.makedirs(CHECKPOINT_DIR, exist_ok=True)
    frames = [r.dataframe for r in results if not r.dataframe.empty]

    if frames:
        pd.concat(frames, ignore_index=True).to_csv(checkpoint_path(fund_name), index=False)
    else:
        pd.DataFrame([{"Mutual Fund": fund_name, "Note": "No IDCW rows captured this run."}]).to_csv(
            checkpoint_path(fund_name), index=False
        )

    log_info(fund_name, "Checkpoint saved.")


def load_checkpoint(fund_name: str) -> Optional[pd.DataFrame]:
    path = checkpoint_path(fund_name)
    if not os.path.exists(path):
        return None
    try:
        return pd.read_csv(path)
    except Exception as exc:
        log_warn(fund_name, f"Could not read checkpoint: {exc!r}")
        return None


def results_from_checkpoint(fund_name: str, cached: pd.DataFrame) -> List[ScrapeResult]:
    if "Scheme Name" not in cached.columns:
        return [ScrapeResult(fund_name, "", cached, "RESUMED_FROM_CHECKPOINT", "")]

    results: List[ScrapeResult] = []
    for scheme_name, group in cached.groupby("Scheme Name", sort=False):
        results.append(
            ScrapeResult(fund_name, str(scheme_name), group.reset_index(drop=True), "RESUMED_FROM_CHECKPOINT", "")
        )
    return results


# ============================================================
# OUTPUT
# ============================================================

def write_excel(results: List[ScrapeResult]) -> None:
    valid_dataframes = [r.dataframe for r in results if not r.dataframe.empty]

    summary_rows = [
        {
            "Mutual Fund": r.fund_name,
            "Scheme Name": r.scheme_name,
            "Status": r.status,
            "Rows": len(r.dataframe),
            "Message": r.message,
        }
        for r in results
    ]
    summary_dataframe = pd.DataFrame(summary_rows)
    combined_dataframe = pd.concat(valid_dataframes, ignore_index=True) if valid_dataframes else pd.DataFrame()

    with pd.ExcelWriter(OUTPUT_EXCEL_FILE, engine="openpyxl") as writer:
        summary_dataframe.to_excel(writer, index=False, sheet_name="Summary")

        if not combined_dataframe.empty:
            combined_dataframe.to_excel(writer, index=False, sheet_name="All_IDCW_Data")

        if WRITE_INDIVIDUAL_SCHEME_SHEETS:
            existing_sheet_names: set = set()
            for r in results:
                if r.dataframe.empty:
                    continue
                sheet_name = safe_sheet_name(r.scheme_name, existing_sheet_names)
                r.dataframe.to_excel(writer, index=False, sheet_name=sheet_name)

    logger.info(f"Excel saved: {OUTPUT_EXCEL_FILE}")


def save_scheme_json(results: List[ScrapeResult]) -> None:
    fund_to_schemes: Dict[str, List[str]] = {}

    for r in results:
        if not r.scheme_name:
            continue
        fund_to_schemes.setdefault(r.fund_name, [])
        if r.scheme_name not in fund_to_schemes[r.fund_name]:
            fund_to_schemes[r.fund_name].append(r.scheme_name)

    payload = {
        "fund_count": len(fund_to_schemes),
        "funds": [
            {"fund_name": fund_name, "scheme_count": len(schemes), "schemes": schemes}
            for fund_name, schemes in fund_to_schemes.items()
        ],
    }

    with open(OUTPUT_SCHEME_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=4, ensure_ascii=False)

    logger.info(f"Scheme JSON saved: {OUTPUT_SCHEME_JSON_FILE}")


# ============================================================
# MAIN
# ============================================================

async def main() -> int:
    logger.info("CAMS NAV & IDCW async Playwright scraper starting.")
    logger.info(f"MAX_CONCURRENT_TABS={MAX_CONCURRENT_TABS}  HEADLESS={HEADLESS}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS)

        try:
            if SCRAPE_ALL_FUNDS:
                fund_names = await fetch_fund_name_list(browser)
            else:
                fund_names = list(FUND_NAMES)

            if not fund_names:
                logger.error("No funds discovered/configured. Exiting.")
                return 1

            os.makedirs(CHECKPOINT_DIR, exist_ok=True)

            all_results: List[ScrapeResult] = []
            funds_to_process: List[str] = []

            for fund_name in fund_names:
                if RESUME_FROM_CHECKPOINTS:
                    cached = load_checkpoint(fund_name)
                    if cached is not None and not cached.empty and "Note" not in cached.columns:
                        log_info(fund_name, "Resuming from checkpoint, skipping re-scrape.")
                        all_results.extend(results_from_checkpoint(fund_name, cached))
                        continue
                funds_to_process.append(fund_name)

            logger.info(
                f"Funds to scrape this run: {len(funds_to_process)} "
                f"(resumed from checkpoint: {len(fund_names) - len(funds_to_process)})"
            )

            if funds_to_process:
                semaphore = asyncio.Semaphore(MAX_CONCURRENT_TABS)
                tasks = {
                    asyncio.create_task(process_fund_with_retry(browser, semaphore, fund_name)): fund_name
                    for fund_name in funds_to_process
                }

                completed = 0
                for task in asyncio.as_completed(tasks):
                    fund_results = await task
                    completed += 1

                    fund_name = fund_results[0].fund_name if fund_results else "unknown"
                    all_results.extend(fund_results)
                    save_checkpoint(fund_name, fund_results)

                    logger.info(f"Progress: {completed}/{len(funds_to_process)} funds complete.")

            write_excel(all_results)
            save_scheme_json(all_results)

            not_ok = [r for r in all_results if r.status not in ("OK", "RESUMED_FROM_CHECKPOINT")]
            if not_ok:
                logger.warning(
                    f"{len(not_ok)} row(s) are not 'OK' after all retries - "
                    f"check the Summary sheet in {OUTPUT_EXCEL_FILE} and {LOG_FILE}."
                )

            logger.info("Done.")
            return 0

        finally:
            await browser.close()


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
