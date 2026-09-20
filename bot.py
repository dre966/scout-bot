"""Scout Bot - Docker-ready version. Automates phone number testing.
Refactored: STATE IDENTIFIER + DO_ACTION per state.
"""

import re
import time
import random
import json
import os
import requests
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import (
    WebDriverException,
    NoSuchElementException,
    StaleElementReferenceException,
    ElementClickInterceptedException,
)
import config as cfg

PHONE_NUMBER_RE = re.compile(cfg.PHONE_NUMBER_PATTERN)

STATES = [
    "max_sessions",
    "keep_testing_dialog",
    "already_tested",
    "resume_test",
    "verification_ended",
    "suspended",
    "landing_page",
    "test_numbers_list",
    "nothing_to_scout",
    "confirm_session",
    "balance_entry",
    "package_select",
    "select_one",
    "call_completed",
    "verification_complete",
    "call_this_number",
    "continue_verification",
    "call_result",
    "unknown",
]


class BotLogger:
    def __init__(self, port):
        os.makedirs("logs", exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = f"logs/bot_{port}_{ts}.jsonl"
        self.session_id = None
        self.range_id = None
        self.session_start = None
        self._log("bot_start", {"port": port})

    def _log(self, event, data=None):
        entry = {"ts": datetime.now().isoformat(), "event": event}
        if data:
            entry["data"] = data
        with open(self.path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def start_session(self, session_id, range_id, sim_id, number):
        self.session_id = session_id
        self.range_id = range_id
        self.session_start = time.time()
        self._log("session_start", {"session_id": session_id, "range_id": range_id, "sim_id": sim_id, "number": number})

    def range_check(self, range_id, reused):
        self._log("range_check", {"range_id": range_id, "reused": reused})

    def disposition_chosen(self, disposition):
        self._log("disposition", {"session_id": self.session_id, "disposition": disposition})

    def session_complete(self, total_calls, verifications):
        elapsed = time.time() - self.session_start if self.session_start else 0
        self._log("session_complete", {"session_id": self.session_id, "range_id": self.range_id, "total_calls": total_calls, "verifications": verifications, "elapsed_seconds": round(elapsed, 1)})

    def session_terminal(self, reason):
        elapsed = time.time() - self.session_start if self.session_start else 0
        self._log("session_terminal", {"session_id": self.session_id, "range_id": self.range_id, "reason": reason, "elapsed_seconds": round(elapsed, 1)})

    def error(self, message, details=None):
        self._log("error", {"message": message, "details": details})


def log(msg, level="info"):
    ts = datetime.now().strftime("%H:%M:%S")
    prefix = {"info": "[i]", "ok": "[ok]", "warn": "[!]", "error": "[x]"}.get(level, "[?]")
    print(f"  {ts} {prefix} {msg}")


def human_scroll(driver):
    if not cfg.HUMAN_LIKE_MODE or random.random() > cfg.RANDOM_SCROLL_CHANCE:
        return
    try:
        driver.execute_script(f"window.scrollBy(0, {random.randint(-100, 150)});")
    except Exception:
        pass


def human_pause():
    if not cfg.HUMAN_LIKE_MODE or random.random() > cfg.RANDOM_PAUSE_CHANCE:
        return
    time.sleep(random.uniform(cfg.RANDOM_PAUSE_MIN, cfg.RANDOM_PAUSE_MAX))


def human_mouse_move(driver, element):
    if not cfg.HUMAN_LIKE_MODE or random.random() > cfg.MOUSE_WOBBLE_CHANCE:
        return
    try:
        ActionChains(driver).move_to_element_with_offset(element, random.randint(-5, 5), random.randint(-3, 3)).perform()
    except Exception:
        pass


def random_delay():
    if random.random() < cfg.LONG_PAUSE_CHANCE:
        delay = random.uniform(cfg.LONG_PAUSE_MIN, cfg.LONG_PAUSE_MAX)
    else:
        delay = random.uniform(cfg.STEP_DELAY_MIN, cfg.STEP_DELAY_MAX)
    time.sleep(delay)
    return delay


def fixed_delay():
    return random.uniform(cfg.FIXED_DELAY_MIN, cfg.FIXED_DELAY_MAX)


def _text_matches(haystack, needle):
    if haystack is None or needle is None:
        return False
    if cfg.TEXT_MATCH_CASE_SENSITIVE:
        return needle in haystack
    return needle.lower() in haystack.lower()


def build_container_xpath(classes):
    conditions = " and ".join(f"contains(concat(' ', normalize-space(@class), ' '), ' {c} ')" for c in classes)
    return f"//*[{conditions}]"


# ---------------------------------------------------------------------------
# SiteBot - STATE IDENTIFIER + DO_ACTION
# ---------------------------------------------------------------------------

class SiteBot:
    def __init__(self, driver):
        self.driver = driver
        self.container_xpath = build_container_xpath(cfg.CONTAINER_CLASSES)
        self.noted_phone_number = None
        self.tested_numbers = set()
        self.test_start_time = None
        self.stale_error_count = 0
        self.current_sim = None
        self.stored_verification_count = 0
        self.call_count = 0
        self.used_ranges = set()
        # BotLogger port: use DEBUGGER_PORT or fallback
        _port = getattr(cfg, "DEBUGGER_PORT", getattr(cfg, "CHROME_DEBUG_PORT", 9222))
        self.log = BotLogger(_port)
        self.logger = self.log  # compat alias
        self.session_disposition = None
        self.session_disposition_category = None
        self.empty_state_switch_count = 0
        self.saved_dispositions = None
        self.api_token = None
        self.current_sim_id = None

        # Dispatch dictionary: state -> handler
        self.STATE_HANDLERS = {
            "max_sessions": self.do_max_sessions,
            "keep_testing_dialog": self.do_keep_testing_dialog,
            "already_tested": self.do_already_tested,
            "resume_test": self.do_resume_test,
            "verification_ended": self.do_verification_ended,
            "suspended": self.do_suspended,
            "landing_page": self.do_landing_page,
            "test_numbers_list": self.do_test_numbers_list,
            "nothing_to_scout": self.do_nothing_to_scout,
            "confirm_session": self.do_confirm_session,
            "balance_entry": self.do_balance_entry,
            "package_select": self.do_package_select,
            "select_one": self.do_select_one,
            "call_completed": self.do_call_completed,
            "verification_complete": self.do_verification_complete,
            "call_this_number": self.do_call_this_number,
            "continue_verification": self.do_continue_verification,
            "call_result": self.do_call_result,
            "unknown": self.do_unknown,
        }

    # -- generic helpers (kept) -------------------------------------------

    def get_body_text(self):
        """Full visible page text -- used for things that may render outside
        the main container, like toasts or portal-based error messages."""
        try:
            return self.driver.execute_script("return document.body.innerText;") or ""
        except WebDriverException:
            return ""

    def get_container(self):
        elements = self.driver.find_elements(By.XPATH, self.container_xpath)
        return elements[0] if elements else None

    def get_container_text(self):
        container = self.get_container()
        if container is None:
            return ""
        try:
            return container.text
        except StaleElementReferenceException:
            return ""

    def find_button_with_text(self, text, within=None):
        """Returns the first <button> whose visible text contains `text`,
        searched page-wide by default, or scoped to `within` if given."""
        scope = within if within is not None else self.driver
        for btn in scope.find_elements(By.TAG_NAME, "button"):
            try:
                if _text_matches(btn.text, text):
                    return btn
            except StaleElementReferenceException:
                continue
        return None

    def click(self, element, label="", retries=2, testing_mode=False):
        if element is None:
            return False
        for attempt in range(retries + 1):
            try:
                if testing_mode:
                    delay = random_delay()
                else:
                    delay = fixed_delay()
                time.sleep(delay)
                human_pause()
                human_scroll(self.driver)
                human_mouse_move(self.driver, element)
                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
                if cfg.HUMAN_LIKE_MODE and random.random() < cfg.DOUBLE_CLICK_CHANCE:
                    actions = ActionChains(self.driver)
                    actions.double_click(element).perform()
                else:
                    element.click()
                self.stale_error_count = 0
                return True
            except StaleElementReferenceException:
                if attempt < retries:
                    time.sleep(0.3)
                    continue
                self.stale_error_count += 1
                return False
            except ElementClickInterceptedException:
                if attempt < retries:
                    time.sleep(0.8)
                    try:
                        self.driver.execute_script("arguments[0].click();", element)
                        self.stale_error_count = 0
                        return True
                    except Exception:
                        continue
                return False
            except WebDriverException:
                return False
        return False

    def type_into(self, element, text, label=""):
        """Clicks an input to focus it, clears it, and types `text`."""
        if element is None:
            return False
        try:
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
            human_mouse_move(self.driver, element)
            element.click()
            time.sleep(fixed_delay())
            element.clear()
            element.send_keys(text)
            return True
        except (WebDriverException, StaleElementReferenceException):
            return False

    def parse_call_count(self):
        """Parse 'Call X of 5' from page text to get actual call number."""
        body_text = self.get_body_text()
        match = re.search(r"Call\s+(\d+)\s+of\s+5", body_text)
        if match:
            return int(match.group(1))
        return None

    def parse_verification_count(self):
        """Parse 'Tests This Cycle' X/8 from page text."""
        body_text = self.get_body_text()
        match = re.search(r"Tests This Cycle.*?(\d+)/8", body_text, re.DOTALL)
        if match:
            return int(match.group(1))
        return None

    def _extract_phone_number(self, scope):
        """Finds the phone number within `scope` (an element)."""
        candidates = []
        for span in scope.find_elements(By.TAG_NAME, "span"):
            try:
                span_text = span.text or ""
            except StaleElementReferenceException:
                continue
            match = PHONE_NUMBER_RE.search(span_text)
            if match:
                candidates.append((span, match.group(0)))
        if not candidates:
            return ""
        for span, number in candidates:
            css_class = span.get_attribute("class") or ""
            if "truncate" in css_class:
                return number
        candidates.sort(key=lambda sn: len(re.sub(r"\D", "", sn[1])), reverse=True)
        return candidates[0][1]

    def _extract_country_code(self, scope):
        """Extract the 2-letter country code from a test number row."""
        try:
            spans = scope.find_elements(By.TAG_NAME, "span")
            for span in spans:
                try:
                    span_text = (span.text or "").strip()
                except StaleElementReferenceException:
                    continue
                match = re.match(r'^([A-Z]{2})\b', span_text)
                if match:
                    return match.group(1)
        except (NoSuchElementException, StaleElementReferenceException):
            pass
        return ""

    def find_test_number_rows(self):
        """Finds every 'Test Number' button on the page, paired with the
        phone number from its row."""
        rows = []
        buttons = self.driver.find_elements(
            By.XPATH,
            f".//button[contains(normalize-space(.), \"{cfg.TRIGGERS['test_number_button']}\")]"
        )
        for btn in buttons:
            number_text = ""
            country_code = ""
            try:
                row = btn.find_element(By.XPATH, "..")
                number_text = self._extract_phone_number(row)
                country_code = self._extract_country_code(row)
            except (NoSuchElementException, StaleElementReferenceException):
                pass
            rows.append((btn, number_text, country_code))
        return rows

    def find_filter_toggle_button(self):
        """Returns the number-filter dropdown's toggle <button>."""
        svgs = self.driver.find_elements(By.CSS_SELECTOR, cfg.CHEVRON_SVG_SELECTOR)
        for svg in svgs:
            try:
                return svg.find_element(By.XPATH, "./ancestor::button[1]")
            except NoSuchElementException:
                continue
        return None

    def get_filter_dropdown_options(self, toggle_button):
        """Returns the option <button> elements in the dropdown panel."""
        try:
            panel = toggle_button.find_element(By.XPATH, "following-sibling::div[1]")
        except (NoSuchElementException, StaleElementReferenceException):
            return []
        return panel.find_elements(By.TAG_NAME, "button")

    # -- state identifier --------------------------------------------------

    def identify_state(self) -> str:
        """Pure detection: returns a state string based SOLELY on page text /
        DOM detection (no side effects). Respects priority order from
        smart_call_yours.py tick()."""
        body_text = self.get_body_text()

        # 1. max_sessions (page-wide first, every tick)
        if _text_matches(body_text, cfg.TRIGGERS["max_sessions_label"]):
            return "max_sessions"

        # 2. keep_testing_dialog (check button exists, regardless of call_count)
        if self.find_button_with_text(cfg.TRIGGERS["keep_testing_button"]) is not None:
            return "keep_testing_dialog"

        # 3. already_tested
        if _text_matches(body_text, cfg.TRIGGERS["already_tested_label"]):
            return "already_tested"

        # 4. resume_test
        if _text_matches(body_text, cfg.TRIGGERS["resume_test_label"]):
            return "resume_test"

        # 5. verification_ended
        if _text_matches(body_text, cfg.TRIGGERS["verification_ended_label"]):
            return "verification_ended"

        # 6. suspended
        if _text_matches(body_text, cfg.TRIGGERS["suspended_label"]):
            return "suspended"

        # 7. landing_page
        if _text_matches(body_text, cfg.TRIGGERS["landing_page_label"]):
            return "landing_page"

        # 8. test_numbers_list (via find_test_number_rows)
        rows = self.find_test_number_rows()
        if rows:
            return "test_numbers_list"

        # 9. nothing_to_scout
        if _text_matches(body_text, cfg.TRIGGERS["nothing_to_scout_label"]):
            return "nothing_to_scout"

        # 10. confirm_session / balance_entry / package_select / select_one / call_completed / verification_complete / call_this_number / continue_verification / call_result
        if _text_matches(body_text, cfg.TRIGGERS["confirm_session_label"]):
            return "confirm_session"
        if _text_matches(body_text, cfg.TRIGGERS["balance_entry_label"]):
            return "balance_entry"
        if _text_matches(body_text, cfg.TRIGGERS["package_label"]):
            return "package_select"
        if _text_matches(body_text, cfg.TRIGGERS["select_one_label"]):
            return "select_one"
        if _text_matches(body_text, cfg.TRIGGERS["call_completed_label"]):
            return "call_completed"
        if _text_matches(body_text, cfg.TRIGGERS["verification_complete_label"]):
            return "verification_complete"
        if _text_matches(body_text, cfg.TRIGGERS["call_this_number_label"]):
            return "call_this_number"
        if _text_matches(body_text, cfg.TRIGGERS["continue_verification_label"]):
            return "continue_verification"
        if _text_matches(body_text, cfg.TRIGGERS["call_result_label"]):
            return "call_result"

        # 11. unknown (fallback) - debug preview
        preview = body_text[:120].replace("\n", " ") if body_text else ""
        log(f"unknown state - page preview: '{preview}...'", "warn")
        return "unknown"

    # -- do_* handler stubs (template) ------------------------------------

    def do_max_sessions(self):
        log("STATE: max_sessions", "warn")
        return True

    def do_keep_testing_dialog(self):
        log("STATE: keep_testing_dialog", "warn")
        return True

    def do_already_tested(self):
        log("STATE: already_tested", "warn")
        return True

    def do_resume_test(self):
        log("STATE: resume_test")
        return True

    def do_verification_ended(self):
        log("STATE: verification_ended", "warn")
        return True

    def do_suspended(self):
        log("STATE: suspended", "error")
        return True

    def do_landing_page(self):
        log("STATE: landing_page", "warn")
        return True

    def do_test_numbers_list(self):
        log("STATE: test_numbers_list", "ok")
        return True

    def do_nothing_to_scout(self):
        log("STATE: nothing_to_scout", "warn")
        return True

    def do_confirm_session(self):
        log("STATE: confirm_session")
        return True

    def do_balance_entry(self):
        log("STATE: balance_entry")
        return True

    def do_package_select(self):
        log("STATE: package_select")
        return True

    def do_select_one(self):
        log("STATE: select_one")
        return True

    def do_call_completed(self):
        log("STATE: call_completed", "ok")
        return True

    def do_verification_complete(self):
        log("STATE: verification_complete", "ok")
        return True

    def do_call_this_number(self):
        log("STATE: call_this_number")
        return True

    def do_continue_verification(self):
        log("STATE: continue_verification")
        return True

    def do_call_result(self):
        log("STATE: call_result")
        return True

    def do_unknown(self):
        log("STATE: unknown - no handler", "error")
        try:
            body = self.get_body_text()
            preview = body[:200].replace("\n", " ") if body else ""
            log(f"unknown page preview: '{preview}...'", "error")
        except Exception:
            pass
        return False

    # -- tick dispatcher (two-phase) ---------------------------------------

    def tick(self):
        state = self.identify_state()
        log(f"[state] {state}")
        handler = self.STATE_HANDLERS.get(state, self.do_unknown)
        return handler()


# ---------------------------------------------------------------------------
# Chrome / tab helpers
# ---------------------------------------------------------------------------

def connect_to_chrome(port=None):
    # Resolve port: explicit arg > env > config
    if port is None:
        try:
            port = int(os.environ.get("CHROME_PORT", os.environ.get("DEBUGGER_PORT", "")) or getattr(cfg, "DEBUGGER_PORT", getattr(cfg, "CHROME_DEBUG_PORT", 9222)))
        except Exception:
            port = 9222
    opts = Options()
    opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    # Also try Service if CHROMEDRIVER_PATH is set (compat with legacy)
    chromedriver_path = getattr(cfg, "CHROMEDRIVER_PATH", None)
    try:
        if chromedriver_path and os.path.exists(chromedriver_path):
            service = Service(executable_path=chromedriver_path)
            driver = webdriver.Chrome(service=service, options=opts)
        else:
            driver = webdriver.Chrome(options=opts)
        log(f"Connected to Chrome on port {port}", "ok")
        return driver
    except WebDriverException as e:
        log(f"Cannot connect to Chrome on port {port}: {e}", "error")
        return None


def switch_to_target_tab(driver, url_substring=None):
    if url_substring is None:
        url_substring = getattr(cfg, "TARGET_URL_SUBSTRING", getattr(cfg, "SITE_DOMAIN", "scout"))
    handles = driver.window_handles
    for h in handles:
        driver.switch_to.window(h)
        try:
            if url_substring in driver.current_url:
                log(f"Switched to tab: {driver.current_url}", "ok")
                return True
        except Exception:
            continue
    log(f"Tab matching '{url_substring}' not found", "warn")
    return False


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def run():
    # Support both DEBUGGER_PORT and CHROME_DEBUG_PORT naming
    default_port = getattr(cfg, "DEBUGGER_PORT", getattr(cfg, "CHROME_DEBUG_PORT", 9222))
    port = int(os.environ.get("CHROME_PORT", os.environ.get("DEBUGGER_PORT", default_port)))
    driver = connect_to_chrome(port)
    if driver is None:
        return

    # Switch to target tab (best-effort)
    try:
        switch_to_target_tab(driver, getattr(cfg, "TARGET_URL_SUBSTRING", getattr(cfg, "SITE_DOMAIN", "")))
    except Exception:
        pass

    bot = SiteBot(driver)
    # compat: expose running flag if needed
    bot.running = True
    log(f"Bot started on port {port}", "ok")

    while getattr(bot, "running", True):
        try:
            bot.tick()
            time.sleep(random_delay())
        except KeyboardInterrupt:
            log("Interrupted by user", "warn")
            bot.running = False
        except SystemExit as e:
            log(f"Exit: {e}", "warn")
            break
        except Exception as e:
            log(f"Tick error: {e}", "error")
            try:
                bot.log.error(str(e))
            except Exception:
                pass
            time.sleep(5)

    log("Bot stopped", "ok")
    try:
        bot.log.session_complete(getattr(bot, "call_count", 0), getattr(bot, "stored_verification_count", 0))
    except Exception:
        pass


def main():
    log("Scout Bot starting...", "ok")
    run()


if __name__ == "__main__":
    main()
