#!/usr/bin/env python3
"""
CAMS NAV & IDCW Scraper - Parallel & Reliability-Hardened Version
-------------------------------------------------------------------

Built directly on top of the working "Fast Optimized Version". Every
selector, wait condition, and JS table-extraction routine from that
version is kept exactly as-is, since it was already validated against
the live site. Only the orchestration layer around it changed.

WHY "SEPARATE BROWSERS", NOT "SEPARATE TABS"
   Selenium's WebDriver protocol is a single command-at-a-time channel
   per session: every click/read goes through that one session
   serially, even if the session has several tabs open. You cannot get
   two actions running at once inside ONE driver, no matter how many
   tabs it has. The only way to get real concurrency is several
   independent driver sessions running at the same time. So instead of
   "one driver, many tabs", this version runs "many drivers, each its
   own OS-level Chrome process" - one per fund - managed by a bounded
   ThreadPoolExecutor. Threads (not multiprocessing) are the right
   tool because every Selenium call blocks waiting on the browser
   (I/O), and a thread releases the GIL while it's blocked.

WHY FUNDS/SCHEMES WERE GETTING SKIPPED
   The original timeouts (2s for the IDCW tab, 3s for the IDCW table)
   were tuned purely for speed. A real but slightly slow render - or
   any resource contention from running several browsers at once -
   reads as "tab/table not found" even though the data is really
   there, and the original code treated that as final. This version:
     - retries a single scheme up to MAX_SCHEME_RETRIES extra times,
       returning to a clean scheme-selection page between attempts
     - retries a whole fund (fresh browser) up to MAX_FUND_RETRIES
       extra times if fund-selection throws, or if zero IDCW schemes
       were found at all on a non-final attempt
     - loosens the tightest timeouts slightly (still fast, just not
       hair-trigger)
     - writes a checkpoint file the moment each fund finishes, so a
       crash mid-run doesn't lose already-scraped funds, and a re-run
       skips funds that already have one
     - logs every attempt, with the fund name on every line even
       though several funds run at once, to both console and a log
       file, so anything still skipped after all retries is easy to
       find

BEING A GOOD CITIZEN ON CAMS' SITE
   Many browsers hitting the same site at once is exactly the kind of
   traffic that can trip rate limiting/anti-bot defenses - which would
   make missing data *worse*, not better. MAX_WORKERS defaults to a
   conservative number for that reason. Raise it only as far as the
   site keeps responding cleanly, and check CAMS' terms of use before
   pushing concurrency or frequency higher.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from pathlib import Path
import pandas as pd
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
import queue
import shutil
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


# ============================================================
# INPUT VARIABLES
# ============================================================

SCRAPE_ALL_FUNDS = False

FUND_NAME = "360 ONE Mutual Fund"

# Use "ALL" or "SPECIFIC"
SCHEME_SELECTION_MODE = "ALL"

SPECIFIC_SCHEME_NAME = "Aditya Birla Sun Life ELSS Tax Saver Fund- (ELSS U/S 80C of IT ACT) - IDCW-Regular Plan"

BASE_DIR = os.getcwd()

OUTPUT_EXCEL_FILE = os.path.join(BASE_DIR, "cams_idcw_output.xlsx")
OUTPUT_SCHEME_JSON_FILE = os.path.join(BASE_DIR, "cams_scheme_names.json")
LOG_FILE = os.path.join(BASE_DIR, "cams_scraper.log")

# CHANGED from the original default (False). Several visible Chrome
# windows fighting for screen/CPU is slower AND more error-prone than
# the same windows running headless. Set False only to visually watch
# a single debugging run.
HEADLESS = False

# None means all schemes.
# Example: 2 means only first 2 schemes.
MAX_SCHEMES: Optional[int] = None

# Recommended False for fast output.
# True will create one sheet per scheme, but Excel writing becomes slower.
WRITE_INDIVIDUAL_SCHEME_SHEETS = False


# ----- Parallel run tuning -----

# How many funds get their own browser running at the same time. Each
# Chrome process is genuinely heavy (CPU + a few hundred MB of RAM
# each), and too much concurrency against one site invites rate
# limits/CAPTCHAs - exactly what causes *more* missed data, not less.
# Start at 4, watch cams_scraper.log for a clean run, then raise it.
MAX_WORKERS = 1

# Extra attempts (beyond the first) for a single scheme before its
# status is accepted as final.
MAX_SCHEME_RETRIES = 1

# Extra attempts (beyond the first, each with a brand-new browser) for
# an entire fund before it is accepted as final.
MAX_FUND_RETRIES = 1

RETRY_BACKOFF_SECONDS = 0.2

# Random delay before each worker launches its browser, so MAX_WORKERS
# browsers don't all spawn in the exact same instant.
STAGGER_SECONDS = 0.1

CHECKPOINT_DIR = "cams_checkpoints"

# If True, a fund whose checkpoint already holds real data from a
# previous run is loaded from disk instead of being scraped again -
# makes it safe to just re-run the whole script after a crash.
RESUME_FROM_CHECKPOINTS = False


# ============================================================
# WEBSITE CONSTANTS  (unchanged - these are the validated selectors)
# ============================================================

URL = "https://www.camsonline.com/Investors/Transactions/Other-services/NAV&IDCW"


IDCW_TAB_XPATH = "//div[contains(@class,'mat-tab-label-container')]//div[@role='tab'][4]"

# Strict IDCW table: only class navdi inside active visible tab body
IDCW_TABLE_XPATH = (
    "//mat-tab-body[contains(@class,'mat-tab-body-active')]"
    "//table[contains(concat(' ', normalize-space(@class), ' '), ' navdi ')]"
)


BACK_BUTTON_XPATH = (
    '//button[@class="check-now-btn csp-class-2"]'
)

TARGET_DIR = r"C:\S2"
# ============================================================
# TIMEOUTS
# ============================================================

DEFAULT_TIMEOUT = 10
SHORT_TIMEOUT = 3          # was 2 - small bump, see module docstring
MICRO_TIMEOUT = 0.8
FAST_SLEEP = 0.08

IDCW_TAB_WAIT_SECONDS = 1      # was hard-coded 2 inside wait_for_idcw_tab
IDCW_TABLE_WAIT_SECONDS = 1     # was hard-coded 3 inside scrape_scheme
POST_SUBMIT_WAIT_SECONDS = 1    # was SHORT_TIMEOUT(=2) inside wait_after_submit_fast


# ============================================================
# LOGGING
# ============================================================
# One logger shared by every thread. Every line carries the thread
# name (set to whichever fund that thread is currently working on) so
# you can tell several funds' interleaved output apart.

logger = logging.getLogger("cams_scraper")
logger.setLevel(logging.INFO)
logger.propagate = False

if not logger.handlers:
    _formatter = logging.Formatter(
        "%(asctime)s | %(threadName)-28s | %(levelname)-7s | %(message)s"
    )

    _file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    _file_handler.setFormatter(_formatter)
    logger.addHandler(_file_handler)

    _console_handler = logging.StreamHandler(sys.stdout)
    _console_handler.setFormatter(_formatter)
    logger.addHandler(_console_handler)


# ============================================================
# TKINTER LOG QUEUE HANDLER
# ============================================================

ui_log_queue: "queue.Queue[str]" = queue.Queue()


class TkQueueLogHandler(logging.Handler):
    """
    Sends logger messages safely from scraper thread to Tkinter UI thread.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            ui_log_queue.put(message)
        except Exception:
            pass


_ui_queue_handler = TkQueueLogHandler()
_ui_queue_handler.setFormatter(
    logging.Formatter("%(asctime)s | %(threadName)-28s | %(levelname)-7s | %(message)s")
)
logger.addHandler(_ui_queue_handler)
def make_thread_label(fund_name: str) -> str:
    label = re.sub(r"\s+", " ", fund_name).strip()
    return label[:26]


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


def safe_sheet_name(name: str, existing_names: set[str]) -> str:
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
# DRIVER SETUP  (unchanged - identical to the original)
# ============================================================

def create_driver() -> webdriver.Chrome:
    # 1) Ensure the folder exists
    Path(TARGET_DIR).mkdir(parents=True, exist_ok=True)
 
    # 2) Tell Selenium Manager to cache drivers here
    os.environ["SE_CACHE_PATH"] = TARGET_DIR
    # Optional: debug logs to confirm path used
    # os.environ["SE_LOG_LEVEL"] = "DEBUG"
 
    # 3) Chrome options: Incognito + some quiet flags
    options = Options()
    
    if HEADLESS:
        options.add_argument("--headless=new")

    options.add_argument("--incognito")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
 
    # 4) Launch Chrome
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(35)
    driver.implicitly_wait(0)
    
 
    # 5) Clear cookies & cache, and keep network cache disabled for this session
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.clearBrowserCookies", {})
        driver.execute_cdp_cmd("Network.clearBrowserCache", {})
        driver.execute_cdp_cmd("Network.setCacheDisabled", {"cacheDisabled": True})
    except Exception:
        # Fallback (older Chrome/Selenium): at least remove cookies
        try:
            driver.delete_all_cookies()
        except Exception:
            pass
 
    return driver

def wait_page_ready(driver: WebDriver) -> None:
    try:
        WebDriverWait(driver, DEFAULT_TIMEOUT, poll_frequency=0.15).until(
            lambda browser: browser.execute_script(
                "return document.readyState === 'complete' || "
                "document.readyState === 'interactive'"
            )
        )
    except TimeoutException:
        pass

    time.sleep(0.25)


def scroll_to_element(driver: WebDriver, element: WebElement) -> None:
    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center', inline:'nearest'});",
        element,
    )


def click_element(driver: WebDriver, element: WebElement) -> None:
    try:
        scroll_to_element(driver, element)
        driver.execute_script("arguments[0].click();", element)
    except Exception:
        try:
            element.click()
        except Exception:
            ActionChains(driver).move_to_element(element).click().perform()

    time.sleep(0.05)


# ============================================================
# FUND PAGE FUNCTIONS  (unchanged logic; timeout now a named constant)
# ============================================================

def get_idcw_tab_fast(driver: WebDriver) -> Optional[WebElement]:
    """
    Fast IDCW tab detection.

    If tab labels are already rendered and IDCW is not present,
    return None immediately instead of waiting full timeout.
    """
    try:
        tab = driver.execute_script(
            """
            const tabs = Array.from(
                document.querySelectorAll(
                    "div.mat-tab-label-container div[role='tab']"
                )
            );

            if (!tabs.length) {
                return null;
            }

            for (const tab of tabs) {
                const text = (tab.innerText || tab.textContent || '')
                    .replace(/\\s+/g, ' ')
                    .trim()
                    .toLowerCase();

                if (text.includes('idcw')) {
                    return tab;
                }
            }

            // Tabs are rendered but IDCW is not there.
            // Example: Details, Returns, Loads /Fees
            return null;
            """
        )

        if tab:
            return tab

        # If tab container exists but IDCW tab is absent, do not wait.
        tab_count = driver.execute_script(
            """
            return document.querySelectorAll(
                "div.mat-tab-label-container div[role='tab']"
            ).length;
            """
        )

        if tab_count and int(tab_count) > 0:
            return None

    except Exception:
        pass

    return None

def get_idcw_tab_fast(driver: WebDriver):
    """
    Finds IDCW tab by visible text.

    If tab list exists and IDCW is absent, returns None immediately.
    """
    try:
        return driver.execute_script(
            """
            const tabs = Array.from(
                document.querySelectorAll(
                    "div.mat-tab-label-container div[role='tab'], div.mat-tab-list div[role='tab']"
                )
            );

            if (!tabs.length) {
                return null;
            }

            for (const tab of tabs) {
                const text = (tab.innerText || tab.textContent || '')
                    .replace(/\\s+/g, ' ')
                    .trim()
                    .toLowerCase();

                if (text.includes('idcw')) {
                    return tab;
                }
            }

            return null;
            """
        )
    except Exception:
        return None


def wait_for_idcw_tab(driver):
    """
    Fast IDCW tab detection.

    If tablist is already present but IDCW tab is missing,
    do not wait unnecessarily.
    """
    tab = get_idcw_tab_fast(driver)

    if tab:
        return tab

    try:
        tab_count = driver.execute_script(
            """
            return document.querySelectorAll(
                "div.mat-tab-label-container div[role='tab'], div.mat-tab-list div[role='tab']"
            ).length;
            """
        )

        if tab_count and int(tab_count) > 0:
            return None

    except Exception:
        pass

    try:
        return WebDriverWait(
            driver,
            0.8,
            poll_frequency=0.1,
        ).until(
            lambda browser: get_idcw_tab_fast(browser)
        )
    except TimeoutException:
        return None


def click_idcw_tab(driver: WebDriver) -> bool:
    """
    Click IDCW tab by text.

    Handles cases where IDCW is not always tab index 4.
    """
    try:
        clicked = driver.execute_script(
            """
            const tabs = Array.from(
                document.querySelectorAll(
                    "div.mat-tab-label-container div[role='tab'], div.mat-tab-list div[role='tab']"
                )
            );

            for (const tab of tabs) {
                const text = (tab.innerText || tab.textContent || '')
                    .replace(/\\s+/g, ' ')
                    .trim()
                    .toLowerCase();

                if (text.includes('idcw')) {
                    tab.scrollIntoView({
                        block: 'center',
                        inline: 'center'
                    });

                    tab.click();
                    return true;
                }
            }

            return false;
            """
        )

        if clicked:
            WebDriverWait(driver, SHORT_TIMEOUT).until(
                lambda d: is_idcw_table_present(d)
            )
            return True

    except Exception:
        pass

    return False

def get_fund_table_rows(driver: WebDriver) -> List[WebElement]:
    possible_xpaths = [
        "//table//tbody/tr[td]",
        "//tr[contains(@class,'mat-row') and td]",
        "//td[contains(@class,'cdk-column-name') or contains(@class,'mat-column-name')]/ancestor::tr",
    ]

    for xpath in possible_xpaths:
        try:
            rows = WebDriverWait(driver, DEFAULT_TIMEOUT, poll_frequency=0.15).until(
                EC.presence_of_all_elements_located((By.XPATH, xpath))
            )

            valid_rows = [row for row in rows if clean_text(row.text)]

            if valid_rows:
                return valid_rows

        except TimeoutException:
            continue

    raise TimeoutException("Mutual Fund table rows not found.")


def get_fund_rows(driver: WebDriver) -> List[Tuple[str, WebElement]]:
    rows = get_fund_table_rows(driver)
    fund_rows: List[Tuple[str, WebElement]] = []

    for row in rows:
        try:
            cells = row.find_elements(By.XPATH, "./td")

            if len(cells) < 2:
                continue

            fund_cell = cells[1]
            fund_name = clean_text(fund_cell.text)

            if not fund_name:
                continue

            clickable = fund_cell

            child_clickables = fund_cell.find_elements(By.XPATH, ".//a|.//button|.//u")
            if child_clickables:
                clickable = child_clickables[0]

            fund_rows.append((fund_name, clickable))

        except StaleElementReferenceException:
            continue

    return fund_rows


def get_all_fund_names(driver: WebDriver) -> List[str]:
    return [fund_name for fund_name, _ in get_fund_rows(driver)]


def select_fund(driver: WebDriver, target_fund_name: str) -> str:
    fund_rows = get_fund_rows(driver)

    target = target_fund_name.lower().strip()

    exact_matches = [
        (name, element)
        for name, element in fund_rows
        if name.lower() == target
    ]

    contains_matches = [
        (name, element)
        for name, element in fund_rows
        if target in name.lower() or name.lower() in target
    ]

    matches = exact_matches or contains_matches

    if not matches:
        available_funds = "\n".join(f"- {name}" for name, _ in fund_rows[:80])
        raise ValueError(
            f"Fund not found: {target_fund_name}\n\nAvailable funds:\n{available_funds}"
        )

    selected_fund_name, clickable = matches[0]

    logger.info(f"Selected fund: {selected_fund_name}")

    click_element(driver, clickable)

    return selected_fund_name


# ============================================================
# SCHEME DROPDOWN FUNCTIONS  (unchanged)
# ============================================================

def find_scheme_dropdown(driver: WebDriver) -> WebElement:
    selectors = [
        (By.CSS_SELECTOR, "ng-select[formcontrolname='schemename']"),
        (By.CSS_SELECTOR, "ng-select[placeholder='Scheme Name']"),
        (By.XPATH, "//ng-select[contains(., 'Scheme Name') or @bindlabel='SCHEME_NAME']"),
    ]

    last_exception: Optional[Exception] = None

    for by, selector in selectors:
        try:
            return WebDriverWait(driver, SHORT_TIMEOUT, poll_frequency=0.12).until(
                EC.element_to_be_clickable((by, selector))
            )
        except Exception as exception:
            last_exception = exception
            continue

    raise TimeoutException(f"Scheme dropdown not found. Last error: {repr(last_exception)}")


def clear_scheme_dropdown(driver: WebDriver) -> None:
    try:
        clear_buttons = driver.find_elements(By.CSS_SELECTOR, "ng-select .ng-clear-wrapper")

        for button in clear_buttons:
            if button.is_displayed():
                driver.execute_script("arguments[0].click();", button)
                time.sleep(0.08)
                return

    except Exception:
        pass


def open_scheme_dropdown(driver: WebDriver) -> WebElement:
    dropdown = find_scheme_dropdown(driver)
    click_element(driver, dropdown)
    return dropdown


def get_scheme_input(driver: WebDriver, dropdown: WebElement) -> WebElement:
    inputs = dropdown.find_elements(By.CSS_SELECTOR, "input")

    if not inputs:
        inputs = driver.find_elements(
            By.CSS_SELECTOR,
            "ng-dropdown-panel input, input[type='text']",
        )

    if not inputs:
        raise TimeoutException("Scheme search input not found.")

    return inputs[0]


def close_dropdown(driver: WebDriver) -> None:
    try:
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
    except Exception:
        pass

    time.sleep(0.08)


def get_visible_dropdown_option_texts(driver: WebDriver) -> List[str]:
    option_texts = driver.execute_script(
        """
        function cleanText(value) {
            return (value || '').replace(/\\s+/g, ' ').trim();
        }

        const options = Array.from(
            document.querySelectorAll('ng-dropdown-panel .ng-option')
        );

        return options
            .map(option => cleanText(option.innerText || option.textContent))
            .filter(Boolean);
        """
    )

    unique_options: List[str] = []

    for option in option_texts or []:
        option = clean_text(option)
        if option and option not in unique_options:
            unique_options.append(option)

    return unique_options


def collect_idcw_scheme_names(driver: WebDriver) -> List[str]:
    """
    Opens dropdown, searches IDCW, and collects all visible/scrollable IDCW schemes.

    Collection is still name-based for reporting,
    but later selection is index-based after searching each scheme.
    """
    clear_scheme_dropdown(driver)

    dropdown = open_scheme_dropdown(driver)
    input_box = get_scheme_input(driver, dropdown)

    input_box.send_keys(Keys.CONTROL, "a")
    input_box.send_keys(Keys.BACKSPACE)
    input_box.send_keys("IDCW")

    WebDriverWait(driver, SHORT_TIMEOUT).until(
    lambda d: len(get_dropdown_option_texts_with_index(d)) > 0
        )

    collected: List[str] = []

    def add_current_options() -> None:
        options = get_dropdown_option_texts_with_index(driver)

        for _, option_text in options:
            option_clean = clean_text(option_text)

            if not option_clean:
                continue

            lower = option_clean.lower()

            if "no scheme" in lower:
                continue

            if "idcw" in lower and option_clean not in collected:
                collected.append(option_clean)

    add_current_options()

    no_new_count = 0

    for _ in range(80):
        try:
            panel = driver.find_element(By.CSS_SELECTOR, "ng-dropdown-panel")

            before_count = len(collected)

            driver.execute_script(
                """
                arguments[0].scrollTop = arguments[0].scrollTop + 450;
                """,
                panel,
            )

            time.sleep(0.08)

            add_current_options()

            after_count = len(collected)

            if after_count == before_count:
                no_new_count += 1
            else:
                no_new_count = 0

            if no_new_count >= 5:
                break

        except Exception:
            break

    close_dropdown(driver)

    if MAX_SCHEMES:
        collected = collected[:MAX_SCHEMES]

    logger.info(f"Collected IDCW schemes: {len(collected)}")

    for index, scheme in enumerate(collected, start=1):
        logger.info(f"Collected scheme index {index}: {scheme}")

    return collected

def get_target_scheme_names(driver: WebDriver, fund_name: str) -> List[str]:
    mode = SCHEME_SELECTION_MODE.strip().upper()

    if mode == "ALL":
        return collect_idcw_scheme_names(driver)

    if mode == "SPECIFIC":
        scheme = SPECIFIC_SCHEME_NAME.strip()

        if not scheme:
            raise ValueError("SPECIFIC_SCHEME_NAME is blank.")

        return [scheme]

    raise ValueError("SCHEME_SELECTION_MODE must be either 'ALL' or 'SPECIFIC'.")

def get_dropdown_options(driver: WebDriver) -> List[WebElement]:
    """
    Returns currently rendered dropdown options.
    Works with ng-select virtual dropdown.
    """
    return driver.find_elements(
        By.CSS_SELECTOR,
        "ng-dropdown-panel .ng-option"
    )


def get_dropdown_option_texts_with_index(driver: WebDriver) -> List[Tuple[int, str]]:
    """
    Returns visible dropdown options as index + cleaned text.
    """
    option_texts = driver.execute_script(
        """
        function cleanText(value) {
            return (value || '').replace(/\\s+/g, ' ').trim();
        }

        const options = Array.from(
            document.querySelectorAll('ng-dropdown-panel .ng-option')
        );

        return options.map((option, index) => {
            return {
                index: index,
                text: cleanText(option.innerText || option.textContent)
            };
        }).filter(item => item.text);
        """
    )

    results: List[Tuple[int, str]] = []

    for item in option_texts or []:
        text = clean_text(item.get("text", ""))
        if text:
            results.append((int(item.get("index", 0)), text))

    return results


def click_dropdown_option_by_visible_index(
    driver: WebDriver,
    visible_index: int,
) -> bool:
    """
    Click dropdown option by currently visible index.
    This avoids fragile text-click issues.
    """
    try:
        clicked = driver.execute_script(
            """
            const index = arguments[0];

            const options = Array.from(
                document.querySelectorAll('ng-dropdown-panel .ng-option')
            );

            const option = options[index];

            if (!option) {
                return false;
            }

            option.scrollIntoView({
                block: 'center',
                inline: 'nearest'
            });

            option.click();
            return true;
            """,
            visible_index,
        )

        if clicked:
            time.sleep(0.12)
            return True

    except Exception:
        pass

    return False


def select_first_dropdown_option_after_search(
    driver: WebDriver,
    search_text: str,
) -> None:
    """
    Search text in ng-select and click first visible option by index.

    This is faster and safer than trying to click exact text WebElement.
    """
    clear_scheme_dropdown(driver)

    dropdown = open_scheme_dropdown(driver)
    input_box = get_scheme_input(driver, dropdown)

    input_box.send_keys(Keys.CONTROL, "a")
    input_box.send_keys(Keys.BACKSPACE)
    input_box.send_keys(search_text)

    WebDriverWait(driver, SHORT_TIMEOUT).until(
    lambda d: len(d.find_elements(By.CSS_SELECTOR, "ng-dropdown-panel .ng-option")) > 0
    )

    options = get_dropdown_option_texts_with_index(driver)

    valid_options = [
        (index, text)
        for index, text in options
        if text and "no scheme" not in text.lower()
    ]

    if not valid_options:
        close_dropdown(driver)
        raise TimeoutException(f"No dropdown option found after searching: {search_text}")

    # Click first search result by visible index.
    first_index, first_text = valid_options[0]

    logger.info(f"Index-select dropdown option [{first_index}]: {first_text}")

    if click_dropdown_option_by_visible_index(driver, first_index):
        return

    close_dropdown(driver)
    raise TimeoutException(f"Could not click first dropdown option for: {search_text}")


def select_scheme_by_name(driver: WebDriver, scheme_name: str) -> None:
    """
    Select scheme using index-based dropdown click.

    Flow:
    1. Search full scheme name.
    2. Click first visible result by index.
    3. If full-name search fails, search IDCW and try matching index/text fallback.
    """
    try:
        select_first_dropdown_option_after_search(driver, scheme_name)
        return

    except Exception as first_error:
        logger.warning(
            f"Full-name index selection failed for '{scheme_name}'. "
            f"Trying IDCW fallback. Reason: {first_error!r}"
        )

    clear_scheme_dropdown(driver)

    dropdown = open_scheme_dropdown(driver)
    input_box = get_scheme_input(driver, dropdown)

    input_box.send_keys(Keys.CONTROL, "a")
    input_box.send_keys(Keys.BACKSPACE)
    input_box.send_keys("IDCW")

    time.sleep(0.35)

    options = get_dropdown_option_texts_with_index(driver)

    target = clean_text(scheme_name).lower()

    # First try exact/contains matching, but click by index.
    for visible_index, option_text in options:
        option_clean = clean_text(option_text).lower()

        if not option_clean:
            continue

        if "no scheme" in option_clean:
            continue

        if option_clean == target or target in option_clean or option_clean in target:
            logger.info(
                f"IDCW fallback index-select option [{visible_index}]: {option_text}"
            )

            if click_dropdown_option_by_visible_index(driver, visible_index):
                return

    # Final fallback: choose first IDCW option if available.
    idcw_options = [
        (visible_index, option_text)
        for visible_index, option_text in options
        if "idcw" in option_text.lower()
        and "no scheme" not in option_text.lower()
    ]

    if idcw_options:
        visible_index, option_text = idcw_options[0]

        logger.warning(
            f"No exact match. Selecting first IDCW option by index "
            f"[{visible_index}]: {option_text}"
        )

        if click_dropdown_option_by_visible_index(driver, visible_index):
            return

    close_dropdown(driver)

    raise TimeoutException(f"Could not select scheme by index: {scheme_name}")

# ============================================================
# RESULT PAGE FUNCTIONS  (unchanged except named timeout constants)
# ============================================================

def click_submit(driver: WebDriver) -> None:
    selectors = [
        (By.CSS_SELECTOR, "input[type='submit']"),
        (By.XPATH, "//input[@type='submit']"),
        (
            By.XPATH,
            "//button[normalize-space()='Submit' or contains(normalize-space(.),'Submit')]",
        ),
    ]

    for by, selector in selectors:
        try:
            button = WebDriverWait(driver, SHORT_TIMEOUT, poll_frequency=0.12).until(
                EC.element_to_be_clickable((by, selector))
            )

            click_element(driver, button)
            return

        except TimeoutException:
            continue

    raise TimeoutException("Submit button not found.")


def wait_after_submit_fast(driver: WebDriver) -> None:
    """
    Fast post-submit wait.
    Stops as soon as result related content appears.
    Avoids fixed long sleep.
    """
    end_time = time.time() + POST_SUBMIT_WAIT_SECONDS

    while time.time() < end_time:
        try:
            body_text = driver.execute_script(
                "return (document.body.innerText || '').toLowerCase();"
            )

            if "idcw" in body_text:
                return

            if "nav" in body_text and "scheme" in body_text:
                return

            back_buttons = driver.find_elements(By.XPATH, BACK_BUTTON_XPATH)
            if back_buttons:
                return

        except Exception:
            pass

        time.sleep(0.08)


def is_idcw_table_present(driver: WebDriver) -> bool:
    """
    Robust IDCW table detection.

    Checks:
    1. Active mat-tab-body table.navdi
    2. Any visible table.navdi
    3. Any visible table whose headers contain IDCW
    """
    try:
        return bool(driver.execute_script(
            """
            function isVisible(element) {
                if (!element) {
                    return false;
                }

                const rect = element.getBoundingClientRect();
                const style = window.getComputedStyle(element);

                return (
                    rect.width > 0 &&
                    rect.height > 0 &&
                    style.display !== 'none' &&
                    style.visibility !== 'hidden' &&
                    style.opacity !== '0'
                );
            }

            function cleanText(value) {
                return (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            }

            const candidates = [];

            const activeBody = document.querySelector(
                'mat-tab-body.mat-tab-body-active'
            );

            if (activeBody) {
                candidates.push(...activeBody.querySelectorAll('table.navdi'));
                candidates.push(...activeBody.querySelectorAll('table'));
            }

            candidates.push(...document.querySelectorAll('table.navdi'));
            candidates.push(...document.querySelectorAll('table'));

            const uniqueTables = Array.from(new Set(candidates));

            for (const table of uniqueTables) {
                if (!isVisible(table)) {
                    continue;
                }

                const rows = Array.from(table.querySelectorAll('tbody tr'));
                const hasRows = rows.some(row => row.querySelectorAll('td').length > 0);

                if (!hasRows) {
                    continue;
                }

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
            """
        ))
    except Exception:
        return False

def extract_idcw_table(driver: WebDriver) -> pd.DataFrame:
    """
    Robust IDCW table extraction.

    Tries:
    1. Active tab table.navdi
    2. Any visible table.navdi
    3. Any visible table with IDCW headers/text
    """
    table_data = driver.execute_script(
        """
        function cleanText(value) {
            return (value || '').replace(/\\s+/g, ' ').trim();
        }

        function isVisible(element) {
            if (!element) {
                return false;
            }

            const rect = element.getBoundingClientRect();
            const style = window.getComputedStyle(element);

            return (
                rect.width > 0 &&
                rect.height > 0 &&
                style.display !== 'none' &&
                style.visibility !== 'hidden' &&
                style.opacity !== '0'
            );
        }

        function extractTable(table) {
            const headers = Array.from(table.querySelectorAll('thead th'))
                .map(th => cleanText(th.innerText || th.textContent))
                .filter(Boolean);

            const rows = Array.from(table.querySelectorAll('tbody tr'))
                .map(tr => Array.from(tr.querySelectorAll('td'))
                    .map(td => cleanText(td.innerText || td.textContent))
                )
                .filter(row => row.length && row.some(Boolean));

            if (!rows.length) {
                return null;
            }

            return {
                headers: headers,
                rows: rows
            };
        }

        const candidates = [];

        const activeBody = document.querySelector(
            'mat-tab-body.mat-tab-body-active'
        );

        if (activeBody) {
            candidates.push(...activeBody.querySelectorAll('table.navdi'));
            candidates.push(...activeBody.querySelectorAll('table'));
        }

        candidates.push(...document.querySelectorAll('table.navdi'));
        candidates.push(...document.querySelectorAll('table'));

        const uniqueTables = Array.from(new Set(candidates));

        for (const table of uniqueTables) {
            if (!isVisible(table)) {
                continue;
            }

            const tableText = cleanText(table.innerText || table.textContent).toLowerCase();

            const looksLikeIDCW =
                table.classList.contains('navdi') ||
                tableText.includes('idcw date') ||
                tableText.includes('idcw per unit') ||
                tableText.includes('retail') ||
                tableText.includes('corporate');

            if (!looksLikeIDCW) {
                continue;
            }

            const extracted = extractTable(table);

            if (extracted && extracted.rows && extracted.rows.length) {
                return extracted;
            }
        }

        return null;
        """
    )

    if not table_data or not table_data.get("rows"):
        return pd.DataFrame()

    rows = table_data["rows"]
    headers = table_data.get("headers") or []

    max_columns = max(len(row) for row in rows)

    if not headers or len(headers) != max_columns:
        headers = [
            "IDCW DATE",
            "IDCW PER UNIT(RETAIL)",
            "IDCW PER UNIT(CORPORATE)",
        ][:max_columns]

        headers += [
            f"Column_{index}"
            for index in range(len(headers) + 1, max_columns + 1)
        ]

    normalized_rows = [
        row + [""] * (max_columns - len(row))
        for row in rows
    ]

    return pd.DataFrame(normalized_rows, columns=headers)

def return_to_scheme_page_fast_after_no_idcw(
    driver: WebDriver,
    fund_name: str,
) -> None:
    """
    Faster return when IDCW tab is absent.

    Avoids the heavier return_to_scheme_page() path as much as possible.
    """
    # Already back on scheme page
    try:
        find_scheme_dropdown(driver)
        return
    except Exception:
        pass

    # Try CAMS back/change button quickly
    try:
        back_buttons = driver.find_elements(By.XPATH, BACK_BUTTON_XPATH)

        for button in back_buttons:
            try:
                if button.is_displayed():
                    driver.execute_script("arguments[0].click();", button)

                    WebDriverWait(driver, 1, poll_frequency=0.1).until(
                        lambda browser: find_scheme_dropdown(browser)
                    )
                    return
            except Exception:
                continue
    except Exception:
        pass

    # Browser back quick fallback
    try:
        driver.back()

        WebDriverWait(driver, 1, poll_frequency=0.1).until(
            lambda browser: find_scheme_dropdown(browser)
        )
        return

    except Exception:
        pass

    # Final fallback only if quick return failed
    logger.warning("Fast no-IDCW return failed. Using normal return flow.")
    return_to_scheme_page(driver, fund_name)


def return_to_scheme_page(driver: WebDriver, fund_name: str) -> None:
    """
    Fast return logic:
    1. If already on scheme dropdown page, do nothing.
    2. Try CAMS back/change button.
    3. Try browser back.
    4. Reload fund page only as final fallback.
    """

    # Already on scheme page.
    try:
        find_scheme_dropdown(driver)
        return
    except Exception:
        pass

    # CAMS back button.
    try:
        back_buttons = driver.find_elements(By.XPATH, BACK_BUTTON_XPATH)

        for button in back_buttons:
            try:
                if button.is_displayed():
                    click_element(driver, button)

                    WebDriverWait(driver, SHORT_TIMEOUT, poll_frequency=0.12).until(
                        lambda browser: find_scheme_dropdown(browser)
                    )

                    time.sleep(0.12)
                    return
            except Exception:
                continue

    except Exception:
        pass

    # Browser back fallback.
    try:
        driver.back()

        WebDriverWait(driver, SHORT_TIMEOUT, poll_frequency=0.12).until(
            lambda browser: find_scheme_dropdown(browser)
        )

        time.sleep(0.12)
        return

    except Exception:
        pass

    # Final fallback: reload fund page.
    logger.warning("Fast return failed. Reloading fund page.")

    driver.get(URL)
    wait_page_ready(driver)

    select_fund(driver, fund_name)

    WebDriverWait(driver, DEFAULT_TIMEOUT, poll_frequency=0.15).until(
        lambda browser: find_scheme_dropdown(browser)
    )

    time.sleep(0.2)


# ============================================================
# SCRAPING FUNCTIONS
# ============================================================

def scrape_scheme(
    driver: WebDriver,
    fund_name: str,
    scheme_name: str,
) -> ScrapeResult:
    try:
        logger.info(f"Selecting scheme: {scheme_name}")

        # --------------------------------------------------
        # STEP 1: Select scheme and submit
        # --------------------------------------------------
        select_scheme_by_name(driver, scheme_name)

        click_submit(driver)

        WebDriverWait(driver, POST_SUBMIT_WAIT_SECONDS).until(
            lambda d: (
                is_idcw_table_present(d)
                or wait_for_idcw_tab(d) is not None
            )
        )


        # --------------------------------------------------
        # STEP 2: Wait for IDCW tab
        # --------------------------------------------------
        tab = wait_for_idcw_tab(driver)

        if not tab:
            return ScrapeResult(
                fund_name=fund_name,
                scheme_name=scheme_name,
                dataframe=pd.DataFrame(),
                status="SKIPPED_NO_IDCW_TAB",
                message="IDCW tab not found",
            )

        # --------------------------------------------------
        # STEP 3: Click IDCW tab
        # --------------------------------------------------
        if not click_idcw_tab(driver):
            return ScrapeResult(
                fund_name=fund_name,
                scheme_name=scheme_name,
                dataframe=pd.DataFrame(),
                status="SKIPPED_NO_IDCW_TAB",
                message="Could not click IDCW tab",
            )

        # --------------------------------------------------
        # STEP 4: Wait for IDCW table only
        # --------------------------------------------------
        try:
            WebDriverWait(
                driver,
                IDCW_TABLE_WAIT_SECONDS,
                poll_frequency=0.1,
            ).until(
                lambda browser: is_idcw_table_present(browser)
            )

        except TimeoutException:
            return ScrapeResult(
                fund_name=fund_name,
                scheme_name=scheme_name,
                dataframe=pd.DataFrame(),
                status="SKIPPED_NO_IDCW_DATA",
                message="IDCW table not loaded",
            )

        # --------------------------------------------------
        # STEP 5: Extract IDCW table
        # --------------------------------------------------
        dataframe = extract_idcw_table(driver)

        if dataframe.empty:
            return ScrapeResult(
                fund_name=fund_name,
                scheme_name=scheme_name,
                dataframe=pd.DataFrame(),
                status="SKIPPED_NO_IDCW_DATA",
                message="IDCW table empty",
            )

        # --------------------------------------------------
        # STEP 6: Add metadata columns
        # --------------------------------------------------
        dataframe.insert(0, "Scheme Name", scheme_name)
        dataframe.insert(0, "Mutual Fund", fund_name)

        logger.info(f"Extracted {len(dataframe)} rows for: {scheme_name}")

        return ScrapeResult(
            fund_name=fund_name,
            scheme_name=scheme_name,
            dataframe=dataframe,
            status="OK",
            message="",
        )

    except Exception as exception:
        logger.warning(f"Scheme error on '{scheme_name}'. Reason: {repr(exception)}")

        return ScrapeResult(
            fund_name=fund_name,
            scheme_name=scheme_name,
            dataframe=pd.DataFrame(),
            status="ERROR_SKIPPED",
            message=repr(exception),
        )

def scrape_scheme_with_retry(
    driver: WebDriver,
    fund_name: str,
    scheme_name: str,
) -> ScrapeResult:
    """
    Wraps scrape_scheme() with retries.

    Fast rule:
    If IDCW tab is not found, do NOT retry.
    This avoids wasting time on schemes/pages where IDCW tab is unavailable.
    """
    total_attempts = MAX_SCHEME_RETRIES + 1
    result: Optional[ScrapeResult] = None

    for attempt in range(1, total_attempts + 1):
        result = scrape_scheme(driver, fund_name, scheme_name)

        if result.status == "OK":
            if attempt > 1:
                logger.info(f"Recovered '{scheme_name}' on attempt {attempt}.")
            return result

        # FAST EXIT: Do not retry when IDCW tab is missing
        if result.status == "SKIPPED_NO_IDCW_TAB":
            logger.info(
                f"Skipping retry for '{scheme_name}' because IDCW tab was not found."
            )
            return result

        if attempt >= total_attempts:
            logger.warning(
                f"Giving up on '{scheme_name}' after {attempt} attempt(s) -> {result.status}"
            )
            return result

        logger.warning(
            f"'{scheme_name}' attempt {attempt}/{total_attempts} -> {result.status}. Retrying."
        )

        try:
            return_to_scheme_page(driver, fund_name)
        except Exception as exc:
            logger.warning(f"return_to_scheme_page failed before retry: {exc!r}")

        time.sleep(RETRY_BACKOFF_SECONDS)

    return result  # type: ignore[return-value]



def scrape_fund(driver: WebDriver, fund_name: str) -> List[ScrapeResult]:
    selected_fund = select_fund(driver, fund_name)

    WebDriverWait(driver, DEFAULT_TIMEOUT, poll_frequency=0.15).until(
        lambda browser: find_scheme_dropdown(browser)
    )

    scheme_names = get_target_scheme_names(driver, selected_fund)

    if not scheme_names:
        return [
            ScrapeResult(
                fund_name=selected_fund,
                scheme_name="",
                dataframe=pd.DataFrame(),
                status="NO_SCHEMES",
                message="No IDCW schemes found",
            )
        ]

    results: List[ScrapeResult] = []

    for index, scheme_name in enumerate(scheme_names, start=1):
        logger.info(f"[{selected_fund}] Scheme {index}/{len(scheme_names)}: {scheme_name}")

        result = scrape_scheme_with_retry(
            driver=driver,
            fund_name=selected_fund,
            scheme_name=scheme_name,
        )

        results.append(result)

        logger.info(f"Status: {result.status}")

        # Fast return if IDCW tab is missing.
        # Do not waste time in the full return flow.
        if result.status == "SKIPPED_NO_IDCW_TAB":
            return_to_scheme_page_fast_after_no_idcw(driver, selected_fund)
        else:
            return_to_scheme_page(driver, selected_fund)

    return results


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
        pd.DataFrame(
            [{"Mutual Fund": fund_name, "Note": "No IDCW rows captured this run."}]
        ).to_csv(checkpoint_path(fund_name), index=False)

    logger.info(f"Checkpoint saved for '{fund_name}'.")


def load_checkpoint(fund_name: str) -> Optional[pd.DataFrame]:
    path = checkpoint_path(fund_name)

    if not os.path.exists(path):
        return None

    try:
        return pd.read_csv(path)
    except Exception as exc:
        logger.warning(f"Could not read checkpoint for '{fund_name}': {exc!r}")
        return None


def results_from_checkpoint(fund_name: str, cached: pd.DataFrame) -> List[ScrapeResult]:
    """
    Rebuilds proper per-scheme ScrapeResult rows from a checkpoint CSV
    so resumed funds feed into the Excel/JSON output identically to
    freshly-scraped ones (instead of one fake placeholder row, which
    would otherwise leak a bogus scheme name into the scheme JSON).
    """
    if "Scheme Name" not in cached.columns:
        return [
            ScrapeResult(
                fund_name=fund_name,
                scheme_name="",
                dataframe=cached,
                status="RESUMED_FROM_CHECKPOINT",
                message="",
            )
        ]

    results: List[ScrapeResult] = []

    for scheme_name, group in cached.groupby("Scheme Name", sort=False):
        results.append(
            ScrapeResult(
                fund_name=fund_name,
                scheme_name=str(scheme_name),
                dataframe=group.reset_index(drop=True),
                status="RESUMED_FROM_CHECKPOINT",
                message="",
            )
        )

    return results


# ============================================================
# PARALLEL FUND WORKER
# ============================================================

def fetch_fund_name_list() -> List[str]:
    driver = create_driver()

    try:
        driver.get(URL)
        wait_page_ready(driver)
        names = get_all_fund_names(driver)
        logger.info(f"Discovered {len(names)} funds on the landing page.")
        return names
    finally:
        driver.quit()


def process_single_fund(fund_name: str) -> List[ScrapeResult]:
    """
    Runs in its own thread with its own browser. Retries the entire
    fund (fresh browser each time) if the flow throws, or if zero IDCW
    schemes were found at all on a non-final attempt.
    """
    threading.current_thread().name = make_thread_label(fund_name)

    total_attempts = MAX_FUND_RETRIES + 1
    last_results: Optional[List[ScrapeResult]] = None
    last_exception: Optional[Exception] = None

    for attempt in range(1, total_attempts + 1):
        driver: Optional[WebDriver] = None

        try:
            time.sleep(random.uniform(0, STAGGER_SECONDS))

            logger.info(f"Attempt {attempt}/{total_attempts}: launching browser.")

            driver = create_driver()
            driver.get(URL)
            wait_page_ready(driver)

            results = scrape_fund(driver, fund_name)

            found_any_scheme = any(r.status != "NO_SCHEMES" for r in results)

            if found_any_scheme or attempt == total_attempts:
                return results

            logger.warning(
                f"Zero IDCW schemes found on attempt {attempt}. "
                "Retrying once in case it was a timing issue."
            )
            last_results = results

        except Exception as exc:
            last_exception = exc
            logger.warning(f"Attempt {attempt} raised: {exc!r}")

        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass

        if attempt < total_attempts:
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)

    if last_results is not None:
        return last_results

    logger.error(f"Fund permanently failed after {total_attempts} attempt(s).")

    return [
        ScrapeResult(
            fund_name=fund_name,
            scheme_name="",
            dataframe=pd.DataFrame(),
            status="FUND_ERROR_SKIPPED",
            message=repr(last_exception),
        )
    ]


# ============================================================
# EXCEL & JSON OUTPUT
# ============================================================

def write_excel(results: List[ScrapeResult]) -> None:
    valid_dataframes = [
        result.dataframe
        for result in results
        if not result.dataframe.empty
    ]

    summary_rows = [
        {
            "Mutual Fund": result.fund_name,
            "Scheme Name": result.scheme_name,
            "Status": result.status,
            "Rows": len(result.dataframe),
            "Message": result.message,
        }
        for result in results
    ]

    summary_dataframe = pd.DataFrame(summary_rows)

    combined_dataframe = (
        pd.concat(valid_dataframes, ignore_index=True)
        if valid_dataframes
        else pd.DataFrame()
    )

    with pd.ExcelWriter(OUTPUT_EXCEL_FILE, engine="openpyxl") as writer:
        summary_dataframe.to_excel(
            writer,
            index=False,
            sheet_name="Summary",
        )

        if not combined_dataframe.empty:
            combined_dataframe.to_excel(
                writer,
                index=False,
                sheet_name="All_IDCW_Data",
            )

        if WRITE_INDIVIDUAL_SCHEME_SHEETS:
            existing_sheet_names: set[str] = set()

            for result in results:
                if result.dataframe.empty:
                    continue

                sheet_name = safe_sheet_name(
                    result.scheme_name,
                    existing_sheet_names,
                )

                result.dataframe.to_excel(
                    writer,
                    index=False,
                    sheet_name=sheet_name,
                )

    logger.info(f"Excel saved: {OUTPUT_EXCEL_FILE}")


def save_scheme_json(results: List[ScrapeResult]) -> None:
    """
    Consolidated across every fund processed this run - fixes a gap in
    the original script, where in ALL-funds mode this file only ever
    held the *last* fund's schemes because it was overwritten on every
    loop iteration.
    """
    fund_to_schemes: Dict[str, List[str]] = {}

    for result in results:
        if not result.scheme_name:
            continue
        fund_to_schemes.setdefault(result.fund_name, [])
        if result.scheme_name not in fund_to_schemes[result.fund_name]:
            fund_to_schemes[result.fund_name].append(result.scheme_name)

    payload = {
        "fund_count": len(fund_to_schemes),
        "funds": [
            {
                "fund_name": fund_name,
                "scheme_count": len(schemes),
                "schemes": schemes,
            }
            for fund_name, schemes in fund_to_schemes.items()
        ],
    }

    with open(OUTPUT_SCHEME_JSON_FILE, "w", encoding="utf-8") as file:
        json.dump(payload, file, indent=4, ensure_ascii=False)

    logger.info(f"Scheme JSON saved: {OUTPUT_SCHEME_JSON_FILE}")


# ============================================================
# MAIN
# ============================================================

def main() -> int:
    logger.info("CAMS NAV & IDCW parallel scraper starting.")
    logger.info(f"MAX_WORKERS={MAX_WORKERS}  HEADLESS={HEADLESS}")

    if SCRAPE_ALL_FUNDS:
        fund_names = fetch_fund_name_list()
    else:
        fund_names = [FUND_NAME]

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
                logger.info(f"Resuming from checkpoint, skipping re-scrape: {fund_name}")
                all_results.extend(results_from_checkpoint(fund_name, cached))
                continue

        funds_to_process.append(fund_name)

    logger.info(
        f"Funds to scrape this run: {len(funds_to_process)} "
        f"(resumed from checkpoint: {len(fund_names) - len(funds_to_process)})"
    )

    if funds_to_process:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="Fund") as executor:
            future_to_fund = {
                executor.submit(process_single_fund, fund_name): fund_name
                for fund_name in funds_to_process
            }

            completed = 0

            for future in as_completed(future_to_fund):
                fund_name = future_to_fund[future]
                completed += 1

                try:
                    fund_results = future.result()
                except Exception as exc:
                    logger.error(f"Unhandled worker crash for '{fund_name}': {exc!r}")
                    fund_results = [
                        ScrapeResult(
                            fund_name=fund_name,
                            scheme_name="",
                            dataframe=pd.DataFrame(),
                            status="FUND_ERROR_SKIPPED",
                            message=repr(exc),
                        )
                    ]

                all_results.extend(fund_results)
                save_checkpoint(fund_name, fund_results)

                logger.info(
                    f"Progress: {completed}/{len(funds_to_process)} funds complete "
                    f"(just finished: {fund_name})"
                )

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

# ============================================================
# TKINTER UI APPLICATION
# ============================================================

class CAMSScraperApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("CAMS NAV & IDCW Scraper")
        self.root.geometry("980x720")
        self.root.minsize(900, 620)

        self.scraper_thread: Optional[threading.Thread] = None
        self.is_running = False
        self.last_output_file = ""

        self.scrape_all_var = tk.BooleanVar(value=SCRAPE_ALL_FUNDS)
        self.headless_var = tk.BooleanVar(value=HEADLESS)
        self.resume_var = tk.BooleanVar(value=RESUME_FROM_CHECKPOINTS)
        self.individual_sheets_var = tk.BooleanVar(value=WRITE_INDIVIDUAL_SCHEME_SHEETS)

        self.fund_name_var = tk.StringVar(value=FUND_NAME)
        self.scheme_mode_var = tk.StringVar(value=SCHEME_SELECTION_MODE)
        self.specific_scheme_var = tk.StringVar(value=SPECIFIC_SCHEME_NAME)

        self.output_file_var = tk.StringVar(value=OUTPUT_EXCEL_FILE)
        self.scheme_json_var = tk.StringVar(value=OUTPUT_SCHEME_JSON_FILE)

        self.max_workers_var = tk.StringVar(value=str(MAX_WORKERS))
        self.max_schemes_var = tk.StringVar(value="" if MAX_SCHEMES is None else str(MAX_SCHEMES))
        self.scheme_retries_var = tk.StringVar(value=str(MAX_SCHEME_RETRIES))
        self.fund_retries_var = tk.StringVar(value=str(MAX_FUND_RETRIES))

        self._build_ui()
        self._poll_log_queue()

    # --------------------------------------------------------
    # UI BUILD
    # --------------------------------------------------------

    def _build_ui(self) -> None:
        main_frame = ttk.Frame(self.root, padding=14)
        main_frame.pack(fill="both", expand=True)

        title_label = ttk.Label(
            main_frame,
            text="CAMS NAV & IDCW Scraper",
            font=("Segoe UI", 18, "bold"),
        )
        title_label.pack(anchor="w")

        subtitle_label = ttk.Label(
            main_frame,
            text="Dynamic UI wrapper for your existing Selenium scraper",
            font=("Segoe UI", 10),
        )
        subtitle_label.pack(anchor="w", pady=(0, 12))

        notebook = ttk.Notebook(main_frame)
        notebook.pack(fill="both", expand=True)

        self.settings_tab = ttk.Frame(notebook, padding=12)
        self.logs_tab = ttk.Frame(notebook, padding=12)

        notebook.add(self.settings_tab, text="Settings")
        notebook.add(self.logs_tab, text="Live Logs")

        self._build_settings_tab()
        self._build_logs_tab()
        self._build_footer(main_frame)

    def _build_settings_tab(self) -> None:
        container = ttk.Frame(self.settings_tab)
        container.pack(fill="both", expand=True)

        # ---------------- Fund Settings ----------------
        fund_box = ttk.LabelFrame(container, text="Fund Selection", padding=12)
        fund_box.pack(fill="x", pady=(0, 10))

        ttk.Checkbutton(
            fund_box,
            text="Scrape all mutual funds",
            variable=self.scrape_all_var,
            command=self._toggle_fund_input,
        ).grid(row=0, column=0, sticky="w", columnspan=3, pady=(0, 8))

        ttk.Label(fund_box, text="Fund Name:").grid(row=1, column=0, sticky="w", padx=(0, 8))
        self.fund_entry = ttk.Entry(fund_box, textvariable=self.fund_name_var, width=70)
        self.fund_entry.grid(row=1, column=1, sticky="ew")

        fund_box.columnconfigure(1, weight=1)

        # ---------------- Scheme Settings ----------------
        scheme_box = ttk.LabelFrame(container, text="Scheme Selection", padding=12)
        scheme_box.pack(fill="x", pady=(0, 10))

        ttk.Label(scheme_box, text="Scheme Mode:").grid(row=0, column=0, sticky="w", padx=(0, 8))

        mode_combo = ttk.Combobox(
            scheme_box,
            textvariable=self.scheme_mode_var,
            values=["ALL", "SPECIFIC"],
            state="readonly",
            width=18,
        )
        mode_combo.grid(row=0, column=1, sticky="w")
        mode_combo.bind("<<ComboboxSelected>>", lambda event: self._toggle_scheme_input())

        ttk.Label(scheme_box, text="Specific Scheme:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        self.scheme_entry = ttk.Entry(scheme_box, textvariable=self.specific_scheme_var, width=90)
        self.scheme_entry.grid(row=1, column=1, sticky="ew", pady=(8, 0))

        ttk.Label(scheme_box, text="Max Schemes blank = all:").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        ttk.Entry(scheme_box, textvariable=self.max_schemes_var, width=20).grid(row=2, column=1, sticky="w", pady=(8, 0))

        scheme_box.columnconfigure(1, weight=1)

        # ---------------- Output Settings ----------------
        output_box = ttk.LabelFrame(container, text="Output Files", padding=12)
        output_box.pack(fill="x", pady=(0, 10))

        ttk.Label(output_box, text="Excel Output:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(output_box, textvariable=self.output_file_var, width=80).grid(row=0, column=1, sticky="ew")
        ttk.Button(output_box, text="Browse", command=self._browse_excel_output).grid(row=0, column=2, padx=(8, 0))

        ttk.Label(output_box, text="Scheme JSON:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        ttk.Entry(output_box, textvariable=self.scheme_json_var, width=80).grid(row=1, column=1, sticky="ew", pady=(8, 0))
        ttk.Button(output_box, text="Browse", command=self._browse_json_output).grid(row=1, column=2, padx=(8, 0), pady=(8, 0))

        output_box.columnconfigure(1, weight=1)

        # ---------------- Runtime Settings ----------------
        runtime_box = ttk.LabelFrame(container, text="Runtime Settings", padding=12)
        runtime_box.pack(fill="x", pady=(0, 10))

        ttk.Checkbutton(runtime_box, text="Headless Chrome", variable=self.headless_var).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(runtime_box, text="Resume from checkpoints", variable=self.resume_var).grid(row=0, column=1, sticky="w")
        ttk.Checkbutton(runtime_box, text="Write individual scheme sheets", variable=self.individual_sheets_var).grid(row=0, column=2, sticky="w")

        ttk.Label(runtime_box, text="Max Workers:").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(runtime_box, textvariable=self.max_workers_var, width=12).grid(row=1, column=0, sticky="e", pady=(10, 0))

        ttk.Label(runtime_box, text="Scheme Retries:").grid(row=1, column=1, sticky="w", pady=(10, 0))
        ttk.Entry(runtime_box, textvariable=self.scheme_retries_var, width=12).grid(row=1, column=1, sticky="e", pady=(10, 0))

        ttk.Label(runtime_box, text="Fund Retries:").grid(row=1, column=2, sticky="w", pady=(10, 0))
        ttk.Entry(runtime_box, textvariable=self.fund_retries_var, width=12).grid(row=1, column=2, sticky="e", pady=(10, 0))

        runtime_box.columnconfigure(0, weight=1)
        runtime_box.columnconfigure(1, weight=1)
        runtime_box.columnconfigure(2, weight=1)

        self._toggle_fund_input()
        self._toggle_scheme_input()

    def _build_logs_tab(self) -> None:
        self.log_text = tk.Text(
            self.logs_tab,
            wrap="word",
            height=25,
            font=("Consolas", 9),
            bg="#111827",
            fg="#E5E7EB",
            insertbackground="#E5E7EB",
        )
        self.log_text.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(self.logs_tab, command=self.log_text.yview)
        scrollbar.pack(side="right", fill="y")

        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _build_footer(self, parent: ttk.Frame) -> None:
        footer = ttk.Frame(parent)
        footer.pack(fill="x", pady=(12, 0))

        self.status_var = tk.StringVar(value="Ready")
        self.status_label = ttk.Label(footer, textvariable=self.status_var)
        self.status_label.pack(side="left")

        self.progress = ttk.Progressbar(footer, mode="indeterminate", length=220)
        self.progress.pack(side="left", padx=16)

        self.start_button = ttk.Button(
            footer,
            text="Start Scraping",
            command=self.start_scraping,
        )
        self.start_button.pack(side="right")

        self.save_as_button = ttk.Button(
            footer,
            text="Save As...",
            command=self.save_as_output,
            state="disabled",
        )
        self.save_as_button.pack(side="right", padx=(0, 8))

    # --------------------------------------------------------
    # UI EVENTS
    # --------------------------------------------------------

    def _toggle_fund_input(self) -> None:
        if self.scrape_all_var.get():
            self.fund_entry.configure(state="disabled")
        else:
            self.fund_entry.configure(state="normal")

    def _toggle_scheme_input(self) -> None:
        if self.scheme_mode_var.get().strip().upper() == "SPECIFIC":
            self.scheme_entry.configure(state="normal")
        else:
            self.scheme_entry.configure(state="disabled")

    def _browse_excel_output(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save Excel Output",
            defaultextension=".xlsx",
            filetypes=[("Excel Files", "*.xlsx")],
            initialfile="cams_idcw_output.xlsx",
        )
        if path:
            self.output_file_var.set(path)

    def _browse_json_output(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save Scheme JSON",
            defaultextension=".json",
            filetypes=[("JSON Files", "*.json")],
            initialfile="cams_scheme_names.json",
        )
        if path:
            self.scheme_json_var.set(path)

    # --------------------------------------------------------
    # SCRAPER CONTROL
    # --------------------------------------------------------

    def start_scraping(self) -> None:
        if self.is_running:
            messagebox.showinfo("Scraper Running", "Scraping is already running.")
            return

        if not self._apply_ui_settings():
            return

        self.is_running = True
        self.last_output_file = OUTPUT_EXCEL_FILE

        self.start_button.configure(state="disabled")
        self.save_as_button.configure(state="disabled")
        self.progress.start(10)
        self.status_var.set("Scraping running...")

        self._append_log("UI | Starting scraper in background thread...")

        self.scraper_thread = threading.Thread(
            target=self._run_scraper_worker,
            name="TkScraperWorker",
            daemon=True,
        )
        self.scraper_thread.start()

    def _apply_ui_settings(self) -> bool:
        global SCRAPE_ALL_FUNDS
        global FUND_NAME
        global SCHEME_SELECTION_MODE
        global SPECIFIC_SCHEME_NAME
        global OUTPUT_EXCEL_FILE
        global OUTPUT_SCHEME_JSON_FILE
        global HEADLESS
        global MAX_SCHEMES
        global MAX_WORKERS
        global MAX_SCHEME_RETRIES
        global MAX_FUND_RETRIES
        global RESUME_FROM_CHECKPOINTS
        global WRITE_INDIVIDUAL_SCHEME_SHEETS

        try:
            SCRAPE_ALL_FUNDS = bool(self.scrape_all_var.get())
            FUND_NAME = self.fund_name_var.get().strip()
            SCHEME_SELECTION_MODE = self.scheme_mode_var.get().strip().upper()
            SPECIFIC_SCHEME_NAME = self.specific_scheme_var.get().strip()

            OUTPUT_EXCEL_FILE = self.output_file_var.get().strip()
            OUTPUT_SCHEME_JSON_FILE = self.scheme_json_var.get().strip()

            HEADLESS = bool(self.headless_var.get())
            RESUME_FROM_CHECKPOINTS = bool(self.resume_var.get())
            WRITE_INDIVIDUAL_SCHEME_SHEETS = bool(self.individual_sheets_var.get())

            MAX_WORKERS = int(self.max_workers_var.get().strip())
            MAX_SCHEME_RETRIES = int(self.scheme_retries_var.get().strip())
            MAX_FUND_RETRIES = int(self.fund_retries_var.get().strip())

            max_schemes_text = self.max_schemes_var.get().strip()
            MAX_SCHEMES = int(max_schemes_text) if max_schemes_text else None

            if not SCRAPE_ALL_FUNDS and not FUND_NAME:
                messagebox.showerror("Validation Error", "Please enter Fund Name or select Scrape All Funds.")
                return False

            if SCHEME_SELECTION_MODE not in ("ALL", "SPECIFIC"):
                messagebox.showerror("Validation Error", "Scheme mode must be ALL or SPECIFIC.")
                return False

            if SCHEME_SELECTION_MODE == "SPECIFIC" and not SPECIFIC_SCHEME_NAME:
                messagebox.showerror("Validation Error", "Please enter Specific Scheme Name.")
                return False

            if not OUTPUT_EXCEL_FILE.lower().endswith(".xlsx"):
                messagebox.showerror("Validation Error", "Excel output file must end with .xlsx")
                return False

            if not OUTPUT_SCHEME_JSON_FILE.lower().endswith(".json"):
                messagebox.showerror("Validation Error", "Scheme JSON file must end with .json")
                return False

            output_dir = os.path.dirname(os.path.abspath(OUTPUT_EXCEL_FILE))
            json_dir = os.path.dirname(os.path.abspath(OUTPUT_SCHEME_JSON_FILE))

            os.makedirs(output_dir, exist_ok=True)
            os.makedirs(json_dir, exist_ok=True)

            return True

        except ValueError:
            messagebox.showerror(
                "Validation Error",
                "Max Workers, Max Schemes, Scheme Retries, and Fund Retries must be numeric.",
            )
            return False
        except Exception as exc:
            messagebox.showerror("Validation Error", repr(exc))
            return False

    def _run_scraper_worker(self) -> None:
        try:
            exit_code = main()

            if exit_code == 0:
                ui_log_queue.put("__SCRAPER_SUCCESS__")
            else:
                ui_log_queue.put("__SCRAPER_FAILED__")

        except Exception as exc:
            logger.exception(f"UI worker crashed: {exc!r}")
            ui_log_queue.put("__SCRAPER_FAILED__")

    # --------------------------------------------------------
    # LOG POLLING
    # --------------------------------------------------------

    def _poll_log_queue(self) -> None:
        try:
            while True:
                message = ui_log_queue.get_nowait()

                if message == "__SCRAPER_SUCCESS__":
                    self._on_scraper_finished(success=True)
                elif message == "__SCRAPER_FAILED__":
                    self._on_scraper_finished(success=False)
                else:
                    self._append_log(message)

        except queue.Empty:
            pass

        self.root.after(150, self._poll_log_queue)

    def _append_log(self, message: str) -> None:
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")

    def _on_scraper_finished(self, success: bool) -> None:
        self.is_running = False
        self.progress.stop()
        self.start_button.configure(state="normal")

        if success:
            self.status_var.set("Completed successfully")
            self.save_as_button.configure(state="normal")

            try:
                self.root.bell()
            except Exception:
                pass

            messagebox.showinfo(
                "Download Successful",
                f"Scraping completed successfully.\n\nExcel saved at:\n{OUTPUT_EXCEL_FILE}",
            )

        else:
            self.status_var.set("Failed")
            messagebox.showerror(
                "Scraper Failed",
                f"Scraper failed. Please check logs:\n{LOG_FILE}",
            )

    # --------------------------------------------------------
    # SAVE AS AFTER SCRAPING
    # --------------------------------------------------------

    def save_as_output(self) -> None:
        if not OUTPUT_EXCEL_FILE or not os.path.exists(OUTPUT_EXCEL_FILE):
            messagebox.showwarning("File Not Found", "No completed Excel output file found.")
            return

        target_path = filedialog.asksaveasfilename(
            title="Save Excel File As",
            defaultextension=".xlsx",
            filetypes=[("Excel Files", "*.xlsx")],
            initialfile=os.path.basename(OUTPUT_EXCEL_FILE),
        )

        if not target_path:
            return

        try:
            shutil.copy2(OUTPUT_EXCEL_FILE, target_path)
            messagebox.showinfo(
                "Saved Successfully",
                f"File copied successfully to:\n{target_path}",
            )
        except Exception as exc:
            messagebox.showerror("Save Failed", repr(exc))


def launch_ui() -> None:
    root = tk.Tk()

    try:
        style = ttk.Style(root)
        style.theme_use("clam")
    except Exception:
        pass

    app = CAMSScraperApp(root)
    root.mainloop()


if __name__ == "__main__":
    launch_ui()
