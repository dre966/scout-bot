"""Scout Bot - Docker-ready version. Automates phone number testing."""

import re
import time
import random
import json
import os
import requests
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
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
# SiteBot
# ---------------------------------------------------------------------------

class SiteBot:
    def __init__(self, driver, bot_id, port, api_base, sim_id):
        self.driver = driver
        self.bot_id = bot_id
        self.port = port
        self.api_base = api_base
        self.sim_id = sim_id
        self.logger = BotLogger(port)
        self.state = "idle"
        self.running = False
        self.current_number = None
        self.current_session_id = None
        self.current_range_id = None
        self.total_calls = 0
        self.total_verifications = 0
        self.disposition_history = []
        self.used_ranges = set()
        self.bearer_token = None
        self.target_tab = None
        self._load_bearer_token()

    # -- bearer token -------------------------------------------------------

    def _load_bearer_token(self):
        token_path = "/app/data/bearer_token.txt"
        try:
            with open(token_path) as f:
                self.bearer_token = f.read().strip()
            log("Loaded bearer token", "ok")
        except FileNotFoundError:
            log(f"Bearer token file not found: {token_path}", "error")
            raise

    # -- disposition history ------------------------------------------------

    def _record_disposition(self, disposition):
        self.disposition_history.append(disposition)
        self.logger.disposition_chosen(disposition)

    def _recent_dispositions(self, n=5):
        return self.disposition_history[-n:]

    def _disposition_is_repeated(self, disposition):
        recent = self._recent_dispositions(5)
        return len(recent) >= 5 and all(d == disposition for d in recent)

    def _pick_disposition(self):
        dispositions = cfg.DISPOSITIONS[:]
        random.shuffle(dispositions)
        for d in dispositions:
            if not self._disposition_is_repeated(d):
                return d
        return dispositions[0]

    # -- page access --------------------------------------------------------

    def _get_page(self, url, tab=None):
        target = tab or self.driver
        try:
            target.get(url)
            time.sleep(fixed_delay())
            return True
        except WebDriverException as e:
            log(f"Failed to navigate: {e}", "error")
            return False

    def _current_url(self, tab=None):
        target = tab or self.driver
        try:
            return target.current_url
        except Exception:
            return ""

    def _page_title(self, tab=None):
        target = tab or self.driver
        try:
            return target.title
        except Exception:
            return ""

    def _page_source_snippet(self, tab=None):
        target = tab or self.driver
        try:
            src = target.page_source or ""
            return src[:3000]
        except Exception:
            return ""

    # -- element finders ----------------------------------------------------

    def _find_element(self, by, value, tab=None):
        target = tab or self.driver
        try:
            return target.find_element(by, value)
        except NoSuchElementException:
            return None

    def _find_elements(self, by, value, tab=None):
        target = tab or self.driver
        try:
            return target.find_elements(by, value)
        except NoSuchElementException:
            return []

    def _find_by_xpath(self, xpath, tab=None):
        return self._find_element(By.XPATH, xpath, tab)

    def _find_by_css(self, css, tab=None):
        return self._find_element(By.CSS_SELECTOR, css, tab)

    def _find_by_id(self, id_val, tab=None):
        return self._find_element(By.ID, id_val, tab)

    def _find_by_text(self, tag, text, tab=None):
        elements = self._find_elements(By.TAG_NAME, tag, tab)
        for el in elements:
            try:
                if _text_matches(el.text, text):
                    return el
            except StaleElementReferenceException:
                continue
        return None

    def _wait_for_element(self, by, value, timeout=10, tab=None):
        deadline = time.time() + timeout
        while time.time() < deadline:
            el = self._find_element(by, value, tab)
            if el is not None:
                return el
            time.sleep(0.5)
        return None

    def _wait_for_xpath(self, xpath, timeout=10, tab=None):
        return self._wait_for_element(By.XPATH, xpath, timeout, tab)

    def _element_visible(self, element):
        try:
            return element.is_displayed()
        except Exception:
            return False

    def _element_text(self, element):
        try:
            return element.text
        except Exception:
            return ""

    def _element_attr(self, element, attr):
        try:
            return element.get_attribute(attr)
        except Exception:
            return None

    # -- click / type -------------------------------------------------------

    def _safe_click(self, element, tab=None):
        target = tab or self.driver
        try:
            human_mouse_move(target, element)
            element.click()
            time.sleep(fixed_delay())
            return True
        except ElementClickInterceptedException:
            try:
                target.execute_script("arguments[0].click();", element)
                time.sleep(fixed_delay())
                return True
            except Exception:
                return False
        except Exception:
            return False

    def _type_into(self, element, text, clear=True, tab=None):
        target = tab or self.driver
        try:
            if clear:
                element.clear()
                time.sleep(0.1)
            for ch in text:
                element.send_keys(ch)
                time.sleep(random.uniform(0.03, 0.09))
            time.sleep(fixed_delay())
            return True
        except Exception:
            return False

    def _press_enter(self, element=None, tab=None):
        target = tab or self.driver
        try:
            from selenium.webdriver.common.keys import Keys
            el = element or target.switch_to.active_element
            el.send_keys(Keys.RETURN)
            time.sleep(fixed_delay())
            return True
        except Exception:
            return False

    # -- containers ---------------------------------------------------------

    def _find_container(self, classes, tab=None):
        xpath = build_container_xpath(classes)
        return self._find_by_xpath(xpath, tab)

    def _find_all_containers(self, classes, tab=None):
        xpath = build_container_xpath(classes)
        return self._find_elements(By.XPATH, xpath, tab)

    # -- state machine steps ------------------------------------------------

    def step_click_test_number(self):
        log("Looking for test-number button...")
        btn = self._find_by_css(cfg.TEST_NUMBER_CSS)
        if btn and self._element_visible(btn):
            self._safe_click(btn)
            self.state = "confirm_session_and_start"
            return True
        btn = self._find_by_xpath(cfg.TEST_NUMBER_XPATH)
        if btn and self._element_visible(btn):
            self._safe_click(btn)
            self.state = "confirm_session_and_start"
            return True
        log("Test-number button not found", "warn")
        return False

    def step_confirm_session_and_start(self):
        log("Confirming session start...")
        time.sleep(1)
        start_btn = self._find_by_css(cfg.START_SESSION_CSS)
        if start_btn and self._element_visible(start_btn):
            self._safe_click(start_btn)
            self.state = "enter_balance_and_continue"
            return True
        start_btn = self._find_by_xpath(cfg.START_SESSION_XPATH)
        if start_btn and self._element_visible(start_btn):
            self._safe_click(start_btn)
            self.state = "enter_balance_and_continue"
            return True
        if _text_matches(self._page_source_snippet(), "already in session"):
            log("Already in session, continuing", "warn")
            self.state = "enter_balance_and_continue"
            return True
        log("Start-session button not found", "warn")
        return False

    def step_enter_balance_and_continue(self):
        log("Looking for balance input...")
        bal_input = self._find_by_css(cfg.BALANCE_INPUT_CSS)
        if bal_input and self._element_visible(bal_input):
            bal_input.clear()
            self._type_into(bal_input, cfg.BALANCE_VALUE, clear=False)
            time.sleep(0.3)
            cont_btn = self._find_by_css(cfg.CONTINUE_CSS)
            if cont_btn and self._element_visible(cont_btn):
                self._safe_click(cont_btn)
            else:
                self._press_enter(bal_input)
            self.state = "select_package_option"
            return True
        bal_input = self._find_by_xpath(cfg.BALANCE_INPUT_XPATH)
        if bal_input and self._element_visible(bal_input):
            bal_input.clear()
            self._type_into(bal_input, cfg.BALANCE_VALUE, clear=False)
            time.sleep(0.3)
            cont_btn = self._find_by_xpath(cfg.CONTINUE_XPATH)
            if cont_btn and self._element_visible(cont_btn):
                self._safe_click(cont_btn)
            else:
                self._press_enter(bal_input)
            self.state = "select_package_option"
            return True
        log("Balance input not found, maybe not needed", "warn")
        self.state = "select_package_option"
        return True

    def step_select_package_option(self):
        log("Selecting package option...")
        time.sleep(1)
        options = self._find_elements(By.CSS_SELECTOR, cfg.PACKAGE_OPTION_CSS)
        if not options:
            options = self._find_elements(By.XPATH, cfg.PACKAGE_OPTION_XPATH)
        if options:
            chosen = random.choice(options)
            self._safe_click(chosen)
            self.state = "select_one_option"
            return True
        log("No package options found", "warn")
        self.state = "select_one_option"
        return True

    def step_select_one_option(self):
        log("Selecting one option...")
        time.sleep(1)
        options = self._find_elements(By.CSS_SELECTOR, cfg.ONE_OPTION_CSS)
        if not options:
            options = self._find_elements(By.XPATH, cfg.ONE_OPTION_XPATH)
        if options:
            chosen = random.choice(options)
            self._safe_click(chosen)
            self.state = "call_this_number"
            return True
        log("No single options found", "warn")
        self.state = "call_this_number"
        return True

    def step_call_this_number(self):
        log("Looking for call button...")
        time.sleep(1)
        call_btn = self._find_by_css(cfg.CALL_BUTTON_CSS)
        if call_btn and self._element_visible(call_btn):
            self._safe_click(call_btn)
            self.total_calls += 1
            self.state = "continue_verification"
            return True
        call_btn = self._find_by_xpath(cfg.CALL_BUTTON_XPATH)
        if call_btn and self._element_visible(call_btn):
            self._safe_click(call_btn)
            self.total_calls += 1
            self.state = "continue_verification"
            return True
        log("Call button not found", "warn")
        return False

    def step_continue_verification(self):
        log("Waiting for call page...")
        time.sleep(cfg.CALL_WAIT_SECONDS)
        ver_btn = self._find_by_css(cfg.VERIFY_CSS)
        if ver_btn and self._element_visible(ver_btn):
            self._safe_click(ver_btn)
            self.total_verifications += 1
            self.state = "submit_call_result"
            return True
        ver_btn = self._find_by_xpath(cfg.VERIFY_XPATH)
        if ver_btn and self._element_visible(ver_btn):
            self._safe_click(ver_btn)
            self.total_verifications += 1
            self.state = "submit_call_result"
            return True
        log("Verify button not found", "warn")
        self.state = "submit_call_result"
        return True

    def step_submit_call_result(self):
        log("Submitting call result (ban evasion: cancel attempt)...")
        time.sleep(1)
        cancel_btn = self._find_by_css(cfg.CANCEL_CSS)
        if cancel_btn and self._element_visible(cancel_btn):
            self._safe_click(cancel_btn)
            time.sleep(0.5)
        else:
            cancel_btn = self._find_by_xpath(cfg.CANCEL_XPATH)
            if cancel_btn and self._element_visible(cancel_btn):
                self._safe_click(cancel_btn)
                time.sleep(0.5)

        disposition = self._pick_disposition()
        self._record_disposition(disposition)
        log(f"Disposition: {disposition}", "ok")

        submit_btn = self._find_by_css(cfg.SUBMIT_CSS)
        if submit_btn and self._element_visible(submit_btn):
            self._safe_click(submit_btn)
        else:
            submit_btn = self._find_by_xpath(cfg.SUBMIT_XPATH)
            if submit_btn and self._element_visible(submit_btn):
                self._safe_click(submit_btn)

        self.state = "start_next_call"
        return True

    def step_start_next_call(self):
        log("Preparing next call...")
        time.sleep(fixed_delay())
        self.state = "call_this_number"
        return True

    def step_verification_complete(self):
        log("Verification complete", "ok")
        self.state = "start_next_call"
        return True

    def step_verification_ended(self):
        log("Verification ended by remote", "warn")
        self.state = "start_next_call"
        return True

    def step_suspended(self):
        log("Suspended! Waiting 60s before continuing...", "warn")
        self.logger.session_terminal("suspended")
        time.sleep(60)
        self.state = "start_next_call"
        return True

    def step_already_tested_back_and_retry(self):
        log("Number already tested, going back...", "warn")
        back_btn = self._find_by_css(cfg.BACK_CSS)
        if back_btn and self._element_visible(back_btn):
            self._safe_click(back_btn)
        else:
            back_btn = self._find_by_xpath(cfg.BACK_XPATH)
            if back_btn and self._element_visible(back_btn):
                self._safe_click(back_btn)
            else:
                self.driver.back()
        time.sleep(fixed_delay())
        self.state = "call_this_number"
        return True

    def step_dismiss_max_sessions(self):
        log("Max sessions reached, dismissing...", "warn")
        ok_btn = self._find_by_css(cfg.OK_CSS)
        if ok_btn and self._element_visible(ok_btn):
            self._safe_click(ok_btn)
        else:
            ok_btn = self._find_by_xpath(cfg.OK_XPATH)
            if ok_btn and self._element_visible(ok_btn):
                self._safe_click(ok_btn)
        self.state = "start_next_call"
        return True

    def recovery_find_available_sim(self):
        log("Recovery: looking for available SIM...", "warn")
        time.sleep(5)
        self.state = "call_this_number"
        return True

    def _detect_page_state(self):
        src = self._page_source_snippet()
        url = self._current_url()
        if _text_matches(src, "already tested") or _text_matches(src, "already been tested"):
            return "already_tested"
        if _text_matches(src, "max session") or _text_matches(src, "maximum sessions"):
            return "max_sessions"
        if _text_matches(src, "suspended") or _text_matches(src, "suspended."):
            return "suspended"
        if _text_matches(src, "verification complete") or _text_matches(src, "successfully verified"):
            return "verification_complete"
        if _text_matches(src, "verification ended") or _text_matches(src, "call ended"):
            return "verification_ended"
        if "landing" in url.lower() or _text_matches(src, "landing page"):
            return "landing_page"
        if _text_matches(src, "test number") or _text_matches(src, "start session"):
            return "test_number"
        if _text_matches(src, "enter balance") or _text_matches(src, "balance"):
            return "balance"
        if _text_matches(src, "package") or _text_matches(src, "select option"):
            return "package"
        if _text_matches(src, "call now") or _text_matches(src, "calling"):
            return "calling"
        if _text_matches(src, "verify") or _text_matches(src, "verification"):
            return "verification"
        return "unknown"

    # -- tick dispatcher ----------------------------------------------------

    def tick(self):
        dispatch = {
            "idle": lambda: self._goto_first_page(),
            "confirm_session_and_start": self.step_confirm_session_and_start,
            "enter_balance_and_continue": self.step_enter_balance_and_continue,
            "select_package_option": self.step_select_package_option,
            "select_one_option": self.step_select_one_option,
            "call_this_number": self.step_call_this_number,
            "continue_verification": self.step_continue_verification,
            "submit_call_result": self.step_submit_call_result,
            "start_next_call": self.step_start_next_call,
            "verification_complete": self.step_verification_complete,
            "verification_ended": self.step_verification_ended,
            "suspended": self.step_suspended,
            "already_tested_back_and_retry": self.step_already_tested_back_and_retry,
            "max_sessions": self.step_dismiss_max_sessions,
            "recovery": self.recovery_find_available_sim,
        }
        page = self._detect_page_state()
        if page == "suspended":
            self.state = "suspended"
        elif page == "max_sessions":
            self.state = "max_sessions"
        elif page == "already_tested":
            self.state = "already_tested_back_and_retry"
        elif page == "verification_complete":
            self.state = "verification_complete"
        elif page == "verification_ended":
            self.state = "verification_ended"
        elif page == "landing_page":
            log("Landing page detected - cannot proceed via ADB in Docker", "warn")
            return False

        handler = dispatch.get(self.state)
        if handler:
            return handler()
        log(f"Unknown state: {self.state}", "error")
        return False

    def _goto_first_page(self):
        if not self._get_page(cfg.TARGET_URL):
            return False
        page = self._detect_page_state()
        if page == "test_number":
            self.state = "click_test_number"
            return self.step_click_test_number()
        elif page == "landing_page":
            log("Landing page - cannot use ADB in Docker", "warn")
            return False
        else:
            self.state = "call_this_number"
            return True


# ---------------------------------------------------------------------------
# Chrome / tab helpers
# ---------------------------------------------------------------------------

def connect_to_chrome(port):
    opts = Options()
    opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    try:
        driver = webdriver.Chrome(options=opts)
        log(f"Connected to Chrome on port {port}", "ok")
        return driver
    except WebDriverException as e:
        log(f"Cannot connect to Chrome on port {port}: {e}", "error")
        return None


def switch_to_target_tab(driver, url_substring):
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
    port = int(os.environ.get("CHROME_PORT", cfg.CHROME_DEBUG_PORT))
    api_base = os.environ.get("API_BASE", cfg.API_BASE_URL)
    sim_id = os.environ.get("SIM_ID", cfg.DEFAULT_SIM_ID)
    bot_id = os.environ.get("BOT_ID", f"bot_{port}")

    driver = connect_to_chrome(port)
    if driver is None:
        return

    switch_to_target_tab(driver, cfg.TARGET_URL_SUBSTRING)

    bot = SiteBot(driver, bot_id, port, api_base, sim_id)
    bot.running = True
    log(f"Bot {bot_id} started on port {port}", "ok")

    while bot.running:
        try:
            bot.tick()
            time.sleep(random_delay())
        except KeyboardInterrupt:
            log("Interrupted by user", "warn")
            bot.running = False
        except Exception as e:
            log(f"Tick error: {e}", "error")
            bot.logger.error(str(e))
            time.sleep(5)

    log("Bot stopped", "ok")
    bot.logger.session_complete(bot.total_calls, bot.total_verifications)


def main():
    log("Scout Bot starting...", "ok")
    run()


if __name__ == "__main__":
    main()
