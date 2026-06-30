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

import pandas as pd
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver import ChromeOptions
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


# ============================================================
# INPUT VARIABLES
# ============================================================

SCRAPE_ALL_FUNDS = False

FUND_NAME = "Helios Mutual Fund"

# Use "ALL" or "SPECIFIC"
SCHEME_SELECTION_MODE = "ALL"

SPECIFIC_SCHEME_NAME = "Aditya Birla Sun Life ELSS Tax Saver Fund- (ELSS U/S 80C of IT ACT) - IDCW-Regular Plan"

OUTPUT_EXCEL_FILE = "cams_idcw_output_test_aditya.xlsx"

OUTPUT_SCHEME_JSON_FILE = "cams_scheme_names.json"

LOG_FILE = "cams_scraper.log"

# CHANGED from the original default (False). Several visible Chrome
# windows fighting for screen/CPU is slower AND more error-prone than
# the same windows running headless. Set False only to visually watch
# a single debugging run.
HEADLESS = True

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
MAX_WORKERS = 4

# Extra attempts (beyond the first) for a single scheme before its
# status is accepted as final.
MAX_SCHEME_RETRIES = 2

# Extra attempts (beyond the first, each with a brand-new browser) for
# an entire fund before it is accepted as final.
MAX_FUND_RETRIES = 2

RETRY_BACKOFF_SECONDS = 1.2

# Random delay before each worker launches its browser, so MAX_WORKERS
# browsers don't all spawn in the exact same instant.
STAGGER_SECONDS = 1.5

CHECKPOINT_DIR = "cams_checkpoints"

# If True, a fund whose checkpoint already holds real data from a
# previous run is loaded from disk instead of being scraped again -
# makes it safe to just re-run the whole script after a crash.
RESUME_FROM_CHECKPOINTS = True


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


# ============================================================
# TIMEOUTS
# ============================================================

DEFAULT_TIMEOUT = 10
SHORT_TIMEOUT = 3          # was 2 - small bump, see module docstring
MICRO_TIMEOUT = 0.8
FAST_SLEEP = 0.08

IDCW_TAB_WAIT_SECONDS = 4       # was hard-coded 2 inside wait_for_idcw_tab
IDCW_TABLE_WAIT_SECONDS = 5     # was hard-coded 3 inside scrape_scheme
POST_SUBMIT_WAIT_SECONDS = 3    # was SHORT_TIMEOUT(=2) inside wait_after_submit_fast


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

def create_driver() -> WebDriver:
    options = ChromeOptions()

    if HEADLESS:
        options.add_argument("--headless=new")

    # Faster than waiting for every image/script to finish.
    options.page_load_strategy = "eager"

    options.add_argument("--window-size=1600,1000")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--log-level=3")

    prefs = {
        "profile.managed_default_content_settings.images": 2,
        "profile.default_content_setting_values.notifications": 2,
        "profile.managed_default_content_settings.fonts": 2,
    }

    options.add_experimental_option("prefs", prefs)
    options.add_experimental_option(
        "excludeSwitches",
        ["enable-automation", "enable-logging"],
    )
    options.add_experimental_option("useAutomationExtension", False)

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(35)

    # Important: keep implicit wait zero because explicit waits are used.
    driver.implicitly_wait(0)

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

def wait_for_idcw_tab(driver):
    try:
        return WebDriverWait(
            driver,
            IDCW_TAB_WAIT_SECONDS,
            poll_frequency=0.1
        ).until(
            EC.element_to_be_clickable(
                (By.XPATH, IDCW_TAB_XPATH)
            )
        )
    except TimeoutException:
        return None


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
    clear_scheme_dropdown(driver)

    dropdown = open_scheme_dropdown(driver)
    input_box = get_scheme_input(driver, dropdown)

    input_box.send_keys(Keys.CONTROL, "a")
    input_box.send_keys(Keys.BACKSPACE)
    input_box.send_keys("IDCW")

    time.sleep(0.35)

    collected: List[str] = []

    def add_current_options() -> None:
        visible_options = get_visible_dropdown_option_texts(driver)

        for option in visible_options:
            option_clean = clean_text(option)

            if not option_clean:
                continue

            if "no scheme" in option_clean.lower():
                continue

            if "idcw" in option_clean.lower() and option_clean not in collected:
                collected.append(option_clean)

    add_current_options()

    no_new_count = 0

    for _ in range(45):
        try:
            panel = driver.find_element(By.CSS_SELECTOR, "ng-dropdown-panel")
            before_count = len(collected)

            driver.execute_script(
                "arguments[0].scrollTop = arguments[0].scrollTop + 650;",
                panel,
            )

            time.sleep(0.08)

            add_current_options()

            after_count = len(collected)

            if after_count == before_count:
                no_new_count += 1
            else:
                no_new_count = 0

            if no_new_count >= 3:
                break

        except Exception:
            break

    close_dropdown(driver)

    if MAX_SCHEMES:
        collected = collected[:MAX_SCHEMES]

    logger.info(f"Collected IDCW schemes: {len(collected)}")

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


def find_dropdown_option_by_text(driver: WebDriver, scheme_name: str) -> Optional[WebElement]:
    option_elements = driver.find_elements(
        By.XPATH,
        "//ng-dropdown-panel//div[contains(@class,'ng-option')]",
    )

    target = clean_text(scheme_name).lower()

    # Exact match first
    for
