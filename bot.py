"""Scout Bot - Docker-ready version. Automates phone number testing.
Refactored: STATE IDENTIFIER + DO_ACTION per state.
"""

import re
import time
import random
import json
import os
import requests
from pathlib import Path
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


class NoSimsRegistered(Exception):
    """Raised when scout dashboard has 0 SIMs registered - server should notify."""
    pass


# OTP / Gmail helpers - lazy import to keep bot runnable without google deps installed
try:
    from utils.otp import get_proxy_for_bot as _get_proxy_for_bot
    from utils.gmail import fetch_otp as _fetch_otp
except ImportError:
    try:
        from utils.otp import get_proxy_for_bot as _get_proxy_for_bot
    except ImportError:
        _get_proxy_for_bot = None
    try:
        from utils.gmail import fetch_otp as _fetch_otp
    except ImportError:
        _fetch_otp = None

PHONE_NUMBER_RE = re.compile(cfg.PHONE_NUMBER_PATTERN)

# Bot identity from env - BOT_ID selects proxy_email via routing.json, BOT_EMAIL overrides
BOT_ID = int(os.getenv("BOT_ID", "0") or "0")
BOT_EMAIL = os.getenv("BOT_EMAIL", "").strip() or None

# XAMPP comms server wiring - Docker uses host.docker.internal to reach XAMPP on host
SERVER_URL = os.getenv("SERVER_URL", os.getenv("XAMPP_SERVER_URL", "http://host.docker.internal/scout-server/api"))
BOT_TOKEN = os.getenv("BOT_TOKEN", "scout-secret")


def _post_to_server(endpoint, payload):
    """POST payload to XAMPP comms server. Silent fail - log warn but don't crash if unreachable."""
    try:
        url = f"{SERVER_URL.rstrip('/')}/{endpoint.lstrip('/')}"
        requests.post(url, json=payload, headers={"X-Bot-Token": BOT_TOKEN}, timeout=2)
    except Exception as e:
        log(f"server post {endpoint} failed: {e}", "warn")


def _get_from_server(endpoint, params=None):
    """GET from comms server with auth. Returns parsed JSON or None on fail."""
    try:
        url = f"{SERVER_URL.rstrip('/')}/{endpoint.lstrip('/')}"
        r = requests.get(url, params=params or {}, headers={"X-Bot-Token": BOT_TOKEN}, timeout=3)
        if r.ok:
            return r.json()
        log(f"server get {endpoint} http={r.status_code}", "warn")
    except Exception as e:
        log(f"server get {endpoint} failed: {e}", "warn")
    return None


def _extract_auth_token(driver):
    """Robustly extract bearer token from browser storage.
    Checks cv-auth-storage (capture_token.py key: state.token), Supabase keys, etc.
    Returns raw token string or None.
    """
    js = r"""
    try {
        const keys = [
            'cv-auth-storage',
            'auth',
            'sb-yKqi0fu5vV6G4ryUIMJuzw-auth-token',
            'sb-auth-token',
            'token',
            'access_token'
        ];
        function tryParse(v){
            if (!v) return null;
            v = v.trim();
            if (!v) return null;
            // try JSON
            try {
                const o = JSON.parse(v);
                if (typeof o === 'string' && o.length > 20) return o;
                if (o.state && o.state.token) return o.state.token;
                if (o.token) return o.token;
                if (o.access_token) return o.access_token;
                if (o.accessToken) return o.accessToken;
                if (o.currentSession && o.currentSession.access_token) return o.currentSession.access_token;
                if (o.session && o.session.access_token) return o.session.access_token;
                // supabase format: {access_token: ..., ...}
                if (o.access_token) return o.access_token;
            } catch(e) {
                // plain string token (maybe JWT)
                if (v.length > 20 && v.split('.').length === 3) return v;
                if (v.length > 20 && v.startsWith('eyJ')) return v;
            }
            return null;
        }
        // explicit keys first
        for (const k of keys) {
            try {
                let v = localStorage.getItem(k);
                let t = tryParse(v);
                if (t) return t;
            } catch(e) {}
            try {
                let v = sessionStorage.getItem(k);
                let t = tryParse(v);
                if (t) return t;
            } catch(e) {}
        }
        // scan all localStorage keys for anything containing auth/token
        try {
            for (let i = 0; i < localStorage.length; i++) {
                const k = localStorage.key(i);
                if (!k) continue;
                const kl = k.toLowerCase();
                if (kl.includes('auth') || kl.includes('token') || kl.includes('sb-')) {
                    let v = localStorage.getItem(k);
                    let t = tryParse(v);
                    if (t) return t;
                }
            }
        } catch(e) {}
        try {
            for (let i = 0; i < sessionStorage.length; i++) {
                const k = sessionStorage.key(i);
                if (!k) continue;
                const kl = k.toLowerCase();
                if (kl.includes('auth') || kl.includes('token') || kl.includes('sb-')) {
                    let v = sessionStorage.getItem(k);
                    let t = tryParse(v);
                    if (t) return t;
                }
            }
        } catch(e) {}
        // cookie fallback - look for jwt pattern
        try {
            const c = document.cookie || '';
            // try to extract bearer-like tokens from cookies values
            const parts = c.split(';');
            for (const p of parts) {
                const v = p.split('=')[1] || '';
                const t = tryParse(v.trim());
                if (t) return t;
                const vt = v.trim();
                if (vt.length > 30 && vt.split('.').length === 3) return vt;
            }
        } catch(e) {}
        return null;
    } catch(e) { return null; }
    """
    try:
        token = driver.execute_script(f"return (function(){{{js}}})()")
        if token and isinstance(token, str):
            token = token.strip()
            # strip Bearer prefix if present
            if token.lower().startswith("bearer "):
                token = token[7:].strip()
            # strip surrounding quotes
            token = token.strip('"').strip("'")
            if len(token) > 20:
                return token
    except Exception as e:
        log(f"extract token js failed: {e}", "warn")
    return None


STATES = [
    "max_sessions",
    "keep_testing_dialog",
    "already_tested",
    "resume_test",
    "verification_ended",
    "suspended",
    "landing_page",
    "no_active_license",
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
    # --- extended states (auth / onboarding) ---
    "sign_in_options",
    "email_access",
    "otp_verification",
    "license_select",
    "country_select",
    "role_select",
    "country_role_select",
    "verify_identity",
    "identity_verified",
    "terms_service",
    "terms_scout_addendum",
    "terms_runner_addendum",
    "terms_privacy",
    # --- extended states (scout app pages) ---
    "scout_dashboard",
    "test_numbers_available",
    "test_numbers_my_verified",
    "test_numbers_my_promoted",
    "test_numbers_sim_dropdown",
    "sims_onboarding",
    "sims_page",
    "add_sim_select_plan",
    "scoutquest_my_submissions",
    "scoutquest_results",
    "messages_inbox",
    "messages_resolved",
    # --- extended states (runner app pages) ---
    "runner_dashboard",
    "runner_register_sim",
    "runner_available_numbers",
    "runner_call_history",
    "runner_top_up",
    "runner_sims_page",
    "runner_add_sim_packages",
    "runner_register_sim_form",
    # --- extended states (settings / modals) ---
    "settings_page",
    "delete_account_modal",
    "switch_role_modal",
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
        self.bot_id = BOT_ID
        # proxy identity - resolve once via routing.json; refreshed on heartbeat if needed
        try:
            _pe, _pi, _entry = self.get_proxy_email_and_inbox()
        except Exception:
            _pe = os.getenv("BOT_EMAIL", "").strip() or BOT_EMAIL or "unknown"
            _pi = "unknown"
            _entry = {}
        self.proxy_email = _pe
        self.poll_inbox = _pi
        self.proxy_entry = _entry
        self.noted_proxy = None
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
        self._last_command_poll = 0

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
            # extended
            "sign_in_options": self.do_sign_in_options,
            "email_access": self.do_email_access,
            "otp_verification": self.do_otp_verification,
            "license_select": self.do_license_select,
            "country_select": self.do_country_select,
            "role_select": self.do_role_select,
            "country_role_select": self.do_country_role_select,
            "verify_identity": self.do_verify_identity,
            "identity_verified": self.do_identity_verified,
            "terms_service": self.do_terms_service,
            "terms_scout_addendum": self.do_terms_scout_addendum,
            "terms_runner_addendum": self.do_terms_runner_addendum,
            "terms_privacy": self.do_terms_privacy,
            "sims_onboarding": self.do_sims_onboarding,
            "scout_dashboard": self.do_scout_dashboard,
            "test_numbers_available": self.do_test_numbers_available,
            "test_numbers_my_verified": self.do_test_numbers_my_verified,
            "test_numbers_my_promoted": self.do_test_numbers_my_promoted,
            "test_numbers_sim_dropdown": self.do_test_numbers_sim_dropdown,
            "sims_page": self.do_sims_page,
            "add_sim_select_plan": self.do_add_sim_select_plan,
            "scoutquest_my_submissions": self.do_scoutquest_my_submissions,
            "scoutquest_results": self.do_scoutquest_results,
            "messages_inbox": self.do_messages_inbox,
            "messages_resolved": self.do_messages_resolved,
            "runner_dashboard": self.do_runner_dashboard,
            "runner_register_sim": self.do_runner_register_sim,
            "runner_available_numbers": self.do_runner_available_numbers,
            "runner_call_history": self.do_runner_call_history,
            "runner_top_up": self.do_runner_top_up,
            "runner_sims_page": self.do_runner_sims_page,
            "runner_add_sim_packages": self.do_runner_add_sim_packages,
            "runner_register_sim_form": self.do_runner_register_sim_form,
            "no_active_license": self.do_no_active_license,
            "settings_page": self.do_settings_page,
            "delete_account_modal": self.do_delete_account_modal,
            "switch_role_modal": self.do_switch_role_modal,
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

    def _is_tab_active(self, tab_text):
        """Return True if a tab button with tab_text has active class (border-white)."""
        try:
            for btn in self.driver.find_elements(By.TAG_NAME, "button"):
                try:
                    if _text_matches(btn.text, tab_text):
                        cls = btn.get_attribute("class") or ""
                        if "border-white" in cls or "text-white" in cls and "border-b-2" in cls:
                            # need border-white specifically for active tab
                            if "border-white" in cls:
                                return True
                except StaleElementReferenceException:
                    continue
        except Exception:
            pass
        return False

    def get_filter_dropdown_options(self, toggle_button):
        """Returns the option <button> elements in the dropdown panel."""
        try:
            panel = toggle_button.find_element(By.XPATH, "following-sibling::div[1]")
        except (NoSuchElementException, StaleElementReferenceException):
            return []
        return panel.find_elements(By.TAG_NAME, "button")

    def _switch_to_scout(self):
        # Runner -> Scout requires role switch, not just navigation (runner can't access /scout/*)
        # Try UI role switch: open profile menu -> click Switch to Scout
        try:
            # 1. Try to open the user menu (bottom left card with email + Scout/Runner label)
            profile_btn = None
            for btn in self.driver.find_elements(By.TAG_NAME, "button"):
                try:
                    txt = (btn.text or "") + (btn.get_attribute("innerText") or "")
                    # profile button contains email or has chevrons-up-down
                    if "chevrons-up-down" in (btn.get_attribute("innerHTML") or ""):
                        profile_btn = btn
                        break
                    if "@" in txt and ("Scout" in txt or "Runner" in txt):
                        profile_btn = btn
                        break
                except StaleElementReferenceException:
                    continue
            # fallback: find the gradient card button with email
            if profile_btn is None:
                profile_btn = self.driver.find_elements(By.CSS_SELECTOR, "button.w-full.flex.items-center.gap-2")
                profile_btn = profile_btn[0] if profile_btn else None

            if profile_btn is not None:
                try:
                    self.click(profile_btn, label="profile menu")
                    time.sleep(1)
                except Exception:
                    try:
                        self.driver.execute_script("arguments[0].click();", profile_btn)
                        time.sleep(1)
                    except Exception:
                        pass
                # Now look for Switch to Scout button in the modal
                switch_btn = self.find_button_with_text("Switch to Scout")
                if switch_btn is not None:
                    log("_switch_to_scout: clicking Switch to Scout", "info")
                    self.click(switch_btn, label="Switch to Scout")
                    time.sleep(3)
                    # After switch, navigate to scout
                    self.driver.get(cfg.TEST_NUMBERS_PAGE_URL)
                    time.sleep(2)
                    return
                # If already Scout, no switch button, just navigate
                if switch_btn is None:
                    log("_switch_to_scout: no Switch to Scout button, already Scout or modal not open", "info")
            # Fallback: direct navigation (for accounts already have scout role)
            self.driver.get(cfg.TEST_NUMBERS_PAGE_URL)
            time.sleep(2)
        except Exception as e:
            log(f"_switch_to_scout failed: {e}", "warn")
            try:
                fallback = getattr(cfg, "SCOUT_DASHBOARD_URL", None) or (cfg.BASE_URL + "/scout")
                self.driver.get(fallback)
                time.sleep(2)
            except Exception as e2:
                log(f"_switch_to_scout fallback failed: {e2}", "warn")

    # -- disposition history helpers (restored from smart_call_yours.py) ----

    def _history_path(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "tested_numbers.json")

    def load_disposition_history(self):
        path = self._history_path()
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def save_disposition_history(self, history):
        try:
            with open(self._history_path(), "w") as f:
                json.dump(history, f, indent=2)
        except Exception as e:
            log(f"save_disposition_history failed: {e}", "warn")

    def get_saved_disposition(self, phone, call_num):
        if not getattr(cfg, "REUSE_NUMBERS", False) or not phone:
            return None
        history = self.load_disposition_history()
        disps = history.get(phone)
        if disps and call_num <= len(disps):
            return disps[call_num - 1]
        return None

    def record_disposition(self, phone, call_num, disposition):
        if not getattr(cfg, "REUSE_NUMBERS", False):
            return
        if not phone:
            phone = self.noted_phone_number
        if not phone or not disposition:
            return
        history = self.load_disposition_history()
        existing = history.get(phone, [])
        while len(existing) < call_num:
            existing.append(None)
        existing[call_num - 1] = disposition
        history[phone] = existing
        self.save_disposition_history(history)

    def _get_bearer_token(self):
        if getattr(self, "api_token", None):
            return self.api_token
        token = _extract_auth_token(self.driver)
        if token:
            self.api_token = token
            return token
        return None

    def _get_current_range_id(self):
        js = """
        const entries = performance.getEntriesByType('resource');
        let sessionId = null;
        for (let i = entries.length - 1; i >= 0; i--) {
            const m = entries[i].name.match(/\/scout\/sessions\/([a-f0-9-]+)/);
            if (m) { sessionId = m[1]; break; }
        }
        if (!sessionId) return null;
        const xhr = new XMLHttpRequest();
        xhr.open('GET', '/api/scout/sessions/' + sessionId, false);
        xhr.send();
        if (xhr.status === 200) {
            const d = JSON.parse(xhr.responseText);
            return d.rangeId || (d.session && d.session.rangeId) || null;
        }
        return null;
        """
        try:
            return self.driver.execute_script(js)
        except Exception:
            return None

    def api_pair_session(self):
        log("API PAIRING", "info")
        token = self._get_bearer_token()
        if not token:
            log("No bearer token found in localStorage", "warn")
            return False
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Origin": "https://scoutandrunner.com",
            "Referer": "https://scoutandrunner.com/scout/test-numbers",
        }
        try:
            resp = requests.get("https://scoutandrunner.com/api/scout/sims", headers=headers, timeout=15)
            if resp.status_code != 200:
                log(f"Failed to fetch SIMs: {resp.status_code}", "warn")
                return False
            data = resp.json()
            sims_raw = data if isinstance(data, list) else data.get("sims", data.get("data", []))
        except Exception as e:
            log(f"Error fetching SIMs: {e}", "warn")
            return False
        available_sims = []
        for s in sims_raw:
            if isinstance(s, dict) and "sim" in s:
                sim = dict(s["sim"])
                if "packages" in s:
                    sim["packages"] = s["packages"]
                if "package" in s:
                    sim["package"] = s["package"]
            else:
                sim = s
            phone = sim.get("phoneNumber", "?")
            cycle = sim.get("testsInCycle", 0)
            cooldown = sim.get("cooldownEndsAt", "")
            status = sim.get("status", "?")
            on_cooldown = False
            if cooldown:
                try:
                    from datetime import timezone
                    cd_end = datetime.fromisoformat(cooldown.replace("Z", "+00:00"))
                    if cd_end < datetime.now(timezone.utc):
                        on_cooldown = True
                except Exception:
                    pass
            log(f"[sim] {phone} cycle={cycle}/8 status={status} cooldown={'YES' if on_cooldown else 'no'}", "info")
            if not on_cooldown and cycle < 8:
                available_sims.append(sim)
        if not available_sims:
            log("No SIMs with available slots", "warn")
            return False
        if getattr(self, "current_sim_id", None):
            sim = None
            for s in available_sims:
                if s.get("id") == self.current_sim_id:
                    sim = s
                    break
            if sim:
                log(f"[sim] Reusing {sim.get('phoneNumber', '?')}", "info")
            else:
                sim = available_sims[0]
                self.current_sim_id = sim.get("id")
                log(f"[sim] Switching to {sim.get('phoneNumber', '?')} (old one finished)", "info")
        else:
            sim = available_sims[0]
            self.current_sim_id = sim.get("id")
            log(f"[sim] Selected {sim.get('phoneNumber', '?')} ({sim.get('testsInCycle', 0)}/8)", "info")
        sim_id = sim.get("id")
        packages = sim.get("packages", [])
        pkg = packages[0] if packages else sim.get("package", {})
        package_id = pkg.get("id") if isinstance(pkg, dict) else None
        if not sim_id or not package_id:
            log(f"Missing simId or packageId (sim={sim_id}, pkg={package_id})", "warn")
            return False
        candidates = getattr(cfg, "API_CANDIDATES", [])
        if not candidates:
            log("No API_CANDIDATES configured", "warn")
            return False
        allowed = [c.upper() for c in getattr(cfg, "ALLOWED_COUNTRIES", [])]
        pool = [c for c in candidates if not allowed or c["country"].upper() in allowed]
        if not pool:
            pool = candidates
        pool = [c for c in pool if c["phone"] not in self.tested_numbers]
        if not pool:
            self.tested_numbers.clear()
            pool = candidates
        cand = random.choice(pool)
        payload = {"simId": sim_id, "numberId": cand["id"], "packageId": package_id}
        try:
            resp = requests.post("https://scoutandrunner.com/api/scout/sessions", json=payload, headers=headers, timeout=10)
            if resp.status_code in (200, 201):
                log(f"Session created! SIM: {sim.get('phoneNumber', sim_id[:8])} | Number: {cand['phone']} [{cand['country']}]", "ok")
                self.tested_numbers.add(cand["phone"])
                self.noted_phone_number = cand["phone"]
                self.driver.refresh()
                time.sleep(2)
                return True
            elif resp.status_code == 409:
                log("Session already exists for this pair — trying another", "warn")
                return False
            else:
                log(f"POST failed: {resp.status_code} {resp.text[:200]}", "warn")
                return False
        except Exception as e:
            log(f"Error creating session: {e}", "warn")
            return False

    def step_click_test_number(self, rows=None, retry_count=0, max_retries=10):
        if retry_count == 0:
            log("SELECTING TEST NUMBER", "info")
            self.test_start_time = time.time()
        if retry_count >= max_retries:
            log(f"Failed after {max_retries} attempts", "warn")
            return
        if rows is None:
            rows = self.find_test_number_rows()
        if not rows:
            log("no 'Test Number' rows found", "warn")
            return
        skip_country = getattr(cfg, "SKIP_COUNTRY_FILTER", False)
        if cfg.ALLOWED_COUNTRIES and not skip_country:
            allowed_lower = [c.upper() for c in cfg.ALLOWED_COUNTRIES]
            filtered = [(b, n, cc) for b, n, cc in rows if cc.upper() in allowed_lower]
            if not filtered:
                log(f"no rows from allowed countries: {cfg.ALLOWED_COUNTRIES}", "warn")
                return
            rows = filtered
        if cfg.MAX_NUMBER_LENGTH:
            before = len(rows)
            filtered = []
            for b, n, cc in rows:
                digits = re.sub(r'\D', '', n)
                cc_digits = {"NL": "31", "IT": "39", "FR": "33", "ES": "34", "BE": "32", "SI": "386", "GB": "44", "SN": "221", "BI": "257", "JO": "962", "CD": "243", "SL": "232", "BO": "591", "HR": "385", "AT": "43", "MX": "52", "BR": "55", "DK": "45"}
                cc_prefix = cc_digits.get(cc.upper(), "")
                if digits.startswith(cc_prefix):
                    local = digits[len(cc_prefix):]
                else:
                    local = digits
                num_len = len(local)
                max_len = cfg.MAX_NUMBER_LENGTH.get(cc.upper(), 9)
                skip_len = getattr(cfg, "SKIP_LENGTH_FILTER", False)
                if skip_len:
                    passed_len = True
                else:
                    passed_len = num_len <= max_len
                skip_prefix = getattr(cfg, "SKIP_PREFIX_FILTER", False)
                if skip_prefix:
                    has_valid_prefix = True
                else:
                    valid_prefixes = cfg.MOBILE_PREFIXES.get(cc.upper(), [])
                    has_valid_prefix = any(local.startswith(p) for p in valid_prefixes) if valid_prefixes else True
                passed = passed_len and has_valid_prefix
                if passed:
                    filtered.append((b, n, cc))
            if not filtered:
                log(f"ALL {before} numbers excluded - no valid numbers available", "warn")
                return
            rows = filtered
        if not rows:
            log("no valid numbers available", "warn")
            return
        if cfg.AVOID_REPEAT_NUMBERS:
            available = [(b, n, cc) for b, n, cc in rows if n not in self.tested_numbers]
            if not available:
                self.tested_numbers.clear()
                available = rows
        else:
            available = rows
        if not available:
            log("no valid numbers available after filtering", "warn")
            return
        if cfg.NUMBER_SELECTION_STRATEGY == "random":
            btn, number, country = random.choice(available)
        elif cfg.NUMBER_SELECTION_STRATEGY == "last":
            btn, number, country = available[-1]
        else:
            btn, number, country = available[0]
        if number:
            self.tested_numbers.add(number)
        try:
            time.sleep(fixed_delay())
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            btn.click()
            self.stale_error_count = 0
            self.saved_dispositions = None
            if number:
                self.noted_phone_number = number
            if getattr(cfg, "REUSE_NUMBERS", False) and number:
                history = self.load_disposition_history()
                if number in history:
                    self.saved_dispositions = history[number]
                    log(f"[reuse] Loaded {len(self.saved_dispositions)} saved dispositions for {number}", "info")
                else:
                    log(f"[new] No saved dispositions for {number}", "info")
            log(f"Clicked Test Number: {number or 'unknown'} [{country}]", "ok")
        except StaleElementReferenceException:
            log(f"Stale element ({retry_count + 1}/{max_retries}) - retrying", "warn")
            self.tested_numbers.discard(number)
            time.sleep(0.5)
            self.driver.refresh()
            time.sleep(fixed_delay() * 2)
            self.step_click_test_number(None, retry_count + 1, max_retries)
        except Exception as e:
            log(f"Click failed: {e}", "warn")

    # -- auth helpers ------------------------------------------------------

    def _find_element(self, by, value, timeout=3):
        """Find single element with short wait, return None if not found."""
        try:
            return self.driver.find_element(by, value)
        except NoSuchElementException:
            return None

    def _find_elements(self, by, value):
        try:
            return self.driver.find_elements(by, value)
        except Exception:
            return []

    def _wait_for_element(self, by, value, timeout=5):
        """Poll for element to appear."""
        end = time.time() + timeout
        while time.time() < end:
            el = self._find_element(by, value)
            if el is not None:
                try:
                    # ensure attached
                    _ = el.is_displayed()
                    return el
                except StaleElementReferenceException:
                    pass
            time.sleep(0.3)
        return None

    def get_proxy_email_and_inbox(self):
        """Resolve proxy_email and poll_inbox via BOT_ID / BOT_EMAIL and routing.json.

        Priority:
          1. BOT_EMAIL env override
          2. BOT_ID % len(routing) via utils.otp.get_proxy_for_bot
          3. Fallback to env BOT_ID local logic if utils not available
        """
        # Use utils.otp helper if available
        if _get_proxy_for_bot is not None:
            try:
                # Re-read BOT_ID from env at call time to allow dynamic changes
                try:
                    bid = int(os.getenv("BOT_ID", str(BOT_ID)) or "0")
                except ValueError:
                    bid = BOT_ID
                # If BOT_EMAIL override set, get_proxy_for_bot will handle it
                proxy, inbox, entry = _get_proxy_for_bot(bot_id=bid)
                return proxy, inbox, entry
            except Exception as e:
                log(f"get_proxy_for_bot failed: {e}, falling back to manual", "warn")

        # Manual fallback: load routing.json directly
        try:
            # Check override first
            override = os.getenv("BOT_EMAIL", "").strip() or BOT_EMAIL
            if override:
                # try to find inbox for override: assume faxcheck2 if not gmail direct
                if override.lower().endswith("@gmail.com"):
                    return override, override, {"proxy": override, "poll_inbox": override, "type": "direct"}
                return override, "faxcheck2@gmail.com", {"proxy": override, "poll_inbox": "faxcheck2@gmail.com", "type": "forward"}

            # Load routing.json manually
            routing_paths = [
                Path("/app/data/routing.json"),
                Path("data/routing.json"),
                Path(__file__).parent / "data" / "routing.json",
            ]
            routing = None
            for p in routing_paths:
                try:
                    if p.exists():
                        with open(p, "r", encoding="utf-8") as f:
                            routing = json.load(f)
                        break
                except Exception:
                    continue
            if routing:
                try:
                    bid = int(os.getenv("BOT_ID", str(BOT_ID)) or "0")
                except ValueError:
                    bid = 0
                idx = bid % len(routing)
                entry = routing[idx]
                return entry["proxy"], entry["poll_inbox"], entry
        except Exception as e:
            log(f"manual routing fallback failed: {e}", "warn")

        # Ultimate fallback
        fallback_proxy = os.getenv("BOT_EMAIL", "junchun@cultinet.site")
        fallback_inbox = "faxcheck2@gmail.com"
        # If proxy is gmail direct, inbox is itself
        if fallback_proxy.lower().endswith("@gmail.com") and fallback_proxy.lower() in [
            "vettychecky@gmail.com", "nettychecky@gmail.com", "hond2367@gmail.com", "jimmykcricket1010@gmail.com"
        ]:
            fallback_inbox = fallback_proxy
        return fallback_proxy, fallback_inbox, {"proxy": fallback_proxy, "poll_inbox": fallback_inbox, "type": "fallback"}

    # -- state identifier --------------------------------------------------

    def identify_state(self) -> str:
        """Pure detection: returns a state string based SOLELY on page text /
        DOM detection (no side effects).

        Priority order (documented):
        1. Core test-flow critical states (max_sessions -> suspended) - must win over everything.
        2. Landing + auth/onboarding (landing_page, sign_in_options, email_access,
           otp_verification, license_select, country_select, role_select/country_role_select,
           verify_identity, terms_*) - checked early so login flow never hits unknown.
        3. Modal / overlay states (delete_account_modal, switch_role_modal) - checked early
           because they overlay other pages; text appears on top of body.
        4. Core test-flow main loop (test_numbers variants, nothing_to_scout,
           confirm_session -> call_result) - original 19 STATES priority preserved.
        5. Extended app pages (scout_dashboard, sims_page, scoutquest, messages,
           runner pages, settings) - AFTER test-flow so active testing is prioritized.
        6. unknown fallback.
        """
        body_text = self.get_body_text()

        # ---- 1. Core critical (page-wide first, every tick) ----
        if _text_matches(body_text, cfg.TRIGGERS["max_sessions_label"]):
            return "max_sessions"
        if self.find_button_with_text(cfg.TRIGGERS["keep_testing_button"]) is not None:
            return "keep_testing_dialog"
        if _text_matches(body_text, cfg.TRIGGERS["already_tested_label"]):
            return "already_tested"
        if _text_matches(body_text, cfg.TRIGGERS["resume_test_label"]):
            return "resume_test"
        if _text_matches(body_text, cfg.TRIGGERS["verification_ended_label"]):
            return "verification_ended"
        # suspended must be specific - "suspended" alone matches "Country temporarily suspended" in dropdown
        # check terms before generic suspended, and require account/suspension context
        # will be checked after terms/pages below; keep placeholder here but defer generic suspended

        # ---- 2. Landing / license ----
        if _text_matches(body_text, "No active license found") and _text_matches(body_text, "active Unetwork license bound"):
            return "no_active_license"
        if _text_matches(body_text, cfg.TRIGGERS["landing_page_label"]):
            return "landing_page"

        # ---- 3. Modal / overlay (must beat page content) ----
        if _text_matches(body_text, "Delete Account?") and _text_matches(body_text, "Type DELETE to confirm"):
            return "delete_account_modal"
        # switch_role_modal: appears when clicking user name box
        if _text_matches(body_text, "Switch to Runner") or _text_matches(body_text, "Switch to Scout"):
            return "switch_role_modal"
        if _text_matches(body_text, "Sign Out") and _text_matches(body_text, "Switch to"):
            return "switch_role_modal"

        # ---- 4. Auth / onboarding (before generic test_numbers) ----
        if _text_matches(body_text, "Sign In Options") and _text_matches(body_text, "Select how you want to access"):
            return "sign_in_options"
        if _text_matches(body_text, "Access Scout & Runner") and _text_matches(body_text, "Unetwork account email"):
            return "email_access"
        if _text_matches(body_text, "Enter your Unetwork account email"):
            return "email_access"
        if _text_matches(body_text, "Verification Code") and _text_matches(body_text, "6-digit code"):
            return "otp_verification"
        if _text_matches(body_text, "We\'ve sent a 6-digit code"):
            return "otp_verification"
        if _text_matches(body_text, "Select Your License") or _text_matches(body_text, "Select a License"):
            return "license_select"
        if _text_matches(body_text, "Select your country of operation"):
            return "country_select"
        if _text_matches(body_text, "Choose a country..."):
            return "country_select"
        if _text_matches(body_text, "Choose a country") and _text_matches(body_text, "Select country"):
            return "country_select"
        # role_select vs country_role_select: both have "How would you like to participate?"
        if _text_matches(body_text, "How would you like to participate?"):
            if _text_matches(body_text, "Choose a country") or _text_matches(body_text, "Select your country"):
                return "country_role_select"
            return "role_select"
        if _text_matches(body_text, "Your Scout seat is reserved"):
            return "verify_identity"
        if _text_matches(body_text, "Verify my identity"):
            return "verify_identity"
        if _text_matches(body_text, "Identity verified") and _text_matches(body_text, "has been verified in accordance"):
            return "identity_verified"
        if _text_matches(body_text, "Identity verified") and _text_matches(body_text, "AML"):
            return "identity_verified"
        # Terms pages - most specific first
        if _text_matches(body_text, "Scout Terms \u2014 Addendum A"):
            return "terms_scout_addendum"
        if _text_matches(body_text, "Runner Terms \u2014 Addendum B"):
            return "terms_runner_addendum"
        if _text_matches(body_text, "Scout & Runner Privacy Notice"):
            return "terms_privacy"
        if _text_matches(body_text, "Scout & Runner Terms of Service"):
            return "terms_service"
        if _text_matches(body_text, "I have read and accept") and _text_matches(body_text, "Terms"):
            if _text_matches(body_text, "Terms of Service"):
                return "terms_service"

        # ---- suspended (after terms, so "Country temporarily suspended" in terms dropdown doesn't misfire) ----
        if _text_matches(body_text, cfg.TRIGGERS["suspended_label"]):
            # ignore if this is just the country dropdown "Country temporarily suspended" text on terms/country pages
            is_country_dropdown = _text_matches(body_text, "Country temporarily suspended")
            is_terms_page = _text_matches(body_text, "Terms of Service") or _text_matches(body_text, "Privacy Notice") or _text_matches(body_text, "Addendum")
            is_country_page = _text_matches(body_text, "Select your country of operation")
            if not (is_country_dropdown and (is_terms_page or is_country_page)):
                return "suspended"

        # ---- 5. Core test-flow: test_numbers variants (before generic) ----
        # My Promoted Numbers tab: unique empty-state text "None of your tested numbers are promoted yet."
        # Do NOT trigger on just the tab label since all Test Numbers pages contain all three tab labels.
        if _text_matches(body_text, "None of your tested numbers are promoted yet"):
            # if dropdown expanded, it also contains this empty text plus duplicate SIM entries -> prioritize dropdown
            # dropdown check is below, so we let dropdown win when toggle expanded
            # For now return promoted; dropdown will be checked before available but after this, so need to check dropdown first
            # To allow dropdown to win, we defer promoted when dropdown would match; we check dropdown before final return
            # Check dropdown early: if toggle expanded, treat as dropdown
            try:
                _toggle = self.find_filter_toggle_button()
                if _toggle is not None:
                    try:
                        _chev = _toggle.find_element(By.CSS_SELECTOR, cfg.CHEVRON_SVG_SELECTOR)
                        _is_exp = "rotate-180" in (_chev.get_attribute("class") or "")
                    except Exception:
                        _is_exp = False
                    if _is_exp and len(self.get_filter_dropdown_options(_toggle)) > 1:
                        return "test_numbers_sim_dropdown"
            except Exception:
                pass
            return "test_numbers_my_promoted"
        # Tabs: check active tab via DOM class border-white (most reliable). All Test Numbers pages contain all 3 tab labels, so text match alone misclassifies.
        if self._is_tab_active("My Promoted Numbers"):
            return "test_numbers_my_promoted"
        if self._is_tab_active("My Verified Numbers"):
            return "test_numbers_my_verified"
        if self._is_tab_active("Available Numbers"):
            # if dropdown open, prioritize dropdown (already handled above via rotate-180), but re-check here in case promoted check didn't fire
            # dropdown already returns before this, so safe to return available
            return "test_numbers_available"
        # SIM dropdown expanded: only when chevron is rotated (dropdown open) - check here as fallback if promoted check didn't fire
        if _text_matches(body_text, "Test Numbers"):
            try:
                toggle = self.find_filter_toggle_button()
                if toggle is not None:
                    # check if dropdown is actually expanded: chevron has rotate-180
                    try:
                        chevron = toggle.find_element(By.CSS_SELECTOR, cfg.CHEVRON_SVG_SELECTOR)
                        chevron_classes = chevron.get_attribute("class") or ""
                        is_expanded = "rotate-180" in chevron_classes
                    except Exception:
                        is_expanded = False
                    if is_expanded:
                        opts = self.get_filter_dropdown_options(toggle)
                        if len(opts) > 1:
                            return "test_numbers_sim_dropdown"
            except Exception:
                pass
        # generic test_numbers_list (via find_test_number_rows) - fallback for Available Numbers when tab active check missed
        rows = self.find_test_number_rows()
        if rows:
            return "test_numbers_list"

        if _text_matches(body_text, cfg.TRIGGERS["nothing_to_scout_label"]):
            return "nothing_to_scout"
        if _text_matches(body_text, cfg.TRIGGERS["confirm_session_label"]):
            return "confirm_session"
        if _text_matches(body_text, cfg.TRIGGERS["balance_entry_label"]):
            return "balance_entry"
        if _text_matches(body_text, cfg.TRIGGERS["package_label"]):
            return "package_select"
        if _text_matches(body_text, cfg.TRIGGERS["select_one_label"]):
            return "select_one"
        # ---- 6. Extended app pages (scout) - MUST be before call_completed
        # call_completed label "Completed" is too generic (matches "Completed test" on dashboard and "Call Completed" toggle on settings)
        if _text_matches(body_text, "Scout Dashboard") and _text_matches(body_text, "Start Validating Numbers"):
            return "scout_dashboard"
        if _text_matches(body_text, "Scout Dashboard"):
            return "scout_dashboard"
        # fallback: dashboard sidebar visible but title not in preview (e.g. after identity verified)
        if _text_matches(body_text, "Dashboard") and _text_matches(body_text, "Total UPs gained") and _text_matches(body_text, "Test Numbers"):
            return "scout_dashboard"
        if _text_matches(body_text, "Settings") and _text_matches(body_text, "Delete Account"):
            return "settings_page"
        if _text_matches(body_text, "Runner Settings") or _text_matches(body_text, "Manage your runner profile"):
            return "settings_page"
        if _text_matches(body_text, "Scout Settings") or _text_matches(body_text, "Manage your scout profile"):
            return "settings_page"

        if _text_matches(body_text, cfg.TRIGGERS["call_completed_label"]):
            # tighten: real call_completed page has "Call X of 5 Completed", not just "Completed"
            if re.search(r"Call\s*\d+\s*of\s*5", body_text, re.IGNORECASE):
                return "call_completed"
            # fallback: require "Call" nearby "Completed"
            if _text_matches(body_text, "Call") and _text_matches(body_text, "Completed"):
                # but exclude settings notification toggle and dashboard recent activity
                if "Scout Dashboard" not in body_text and "Settings" not in body_text:
                    return "call_completed"
        if _text_matches(body_text, cfg.TRIGGERS["verification_complete_label"]):
            return "verification_complete"
        if _text_matches(body_text, cfg.TRIGGERS["call_this_number_label"]):
            return "call_this_number"
        if _text_matches(body_text, cfg.TRIGGERS["continue_verification_label"]):
            return "continue_verification"
        if _text_matches(body_text, cfg.TRIGGERS["call_result_label"]):
            return "call_result"
        if _text_matches(body_text, "Welcome to the Scout Role"):
            return "sims_onboarding"
        if _text_matches(body_text, "SIM Management") and _text_matches(body_text, "Total SIMs"):
            return "sims_page"
        if _text_matches(body_text, "Select an approved call plan"):
            return "add_sim_select_plan"
        if _text_matches(body_text, "ScoutQuest Results"):
            return "scoutquest_results"
        if _text_matches(body_text, "ScoutQuest") and _text_matches(body_text, "My Submissions"):
            return "scoutquest_my_submissions"
        # messages - use unique empty-state strings since both tabs labels appear on both pages
        # Inbox tab: "No messages yet" ; Resolved tab: "No resolved tickets"
        if _text_matches(body_text, "No resolved tickets"):
            return "messages_resolved"
        if _text_matches(body_text, "No messages yet"):
            return "messages_inbox"
        # fallback generic tab check (when messages list not empty)
        if _text_matches(body_text, "Messages") and _text_matches(body_text, "Resolved"):
            return "messages_resolved"
        if _text_matches(body_text, "Messages") and _text_matches(body_text, "Inbox"):
            return "messages_inbox"

        # ---- 7. Extended app pages (runner) ----
        if _text_matches(body_text, "Runner Dashboard"):
            return "runner_dashboard"
        # Runner Available Numbers page has "Reward Rate" + "Register SIM to Test" buttons -> must be checked BEFORE generic Register SIM
        if _text_matches(body_text, "Available Numbers") and _text_matches(body_text, "Reward Rate"):
            return "runner_available_numbers"
        if _text_matches(body_text, "Available Numbers") and _text_matches(body_text, "Get package"):
            return "runner_available_numbers"
        # Runner register form has input fields with labels Country/Phone Number/Carrier - not just the card button text "Register SIM to Test"
        if _text_matches(body_text, "Register SIM") and _text_matches(body_text, "Phone Number") and _text_matches(body_text, "Country"):
            if _text_matches(body_text, "Available Packages"):
                return "runner_add_sim_packages"
            # require Carrier field to distinguish form from card button
            if _text_matches(body_text, "Carrier"):
                return "runner_register_sim_form"
            return "runner_register_sim_form"
        if _text_matches(body_text, "Register SIM") and _text_matches(body_text, "Available Packages"):
            return "runner_add_sim_packages"
        # generic Register SIM page (scout Add SIM / runner Add SIM flow) - only if it has Step indicator or Browse/SIM/Verify/Package stepper
        if _text_matches(body_text, "Register SIM") and (_text_matches(body_text, "Browse") or _text_matches(body_text, "Step 2 of 4")):
            return "runner_register_sim"
        if _text_matches(body_text, "Call History") and _text_matches(body_text, "Total Rewards"):
            return "runner_call_history"
        if _text_matches(body_text, "Top Up with UP") or _text_matches(body_text, "Instant credit delivery"):
            return "runner_top_up"
        if _text_matches(body_text, "My SIMs") and _text_matches(body_text, "Use your UP balance to add credit"):
            return "runner_sims_page"
        if _text_matches(body_text, "Available Packages"):
            return "runner_add_sim_packages"
        if _text_matches(body_text, "My SIMs"):
            return "runner_sims_page"

        # ---- 8. Settings (already handled above before call_completed, kept here as fallback) ----

        # ---- 9. unknown (fallback) ----
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
        log("STATE: resume_test", "info")
        btn = self.find_button_with_text(cfg.TRIGGERS["resume_test_button"])
        if btn is not None:
            self.click(btn, label=cfg.TRIGGERS["resume_test_button"])
            time.sleep(1)
        else:
            log("resume_test: Resume test button not found", "warn")
            # fallback: try generic text "Resume"
            btn2 = self.find_button_with_text("Resume")
            if btn2 is not None:
                self.click(btn2, label="Resume")
        return True

    def do_verification_ended(self):
        log("STATE: verification_ended", "warn")
        return True

    def do_suspended(self):
        log("STATE: suspended", "error")
        return True

    def do_landing_page(self):
        log("STATE: landing_page - clicking Login", "info")
        # Try multiple selectors for Login button/link
        # Primary: a[href="/auth/login"] (Next.js Link)
        el = None
        for selector in ['a[href="/auth/login"]', 'a[href*="/auth/login"]']:
            el = self._find_element(By.CSS_SELECTOR, selector)
            if el is not None:
                break
        if el is None:
            # Fallback: button with text Log In / Login / Sign In
            for txt in ["Log In", "Login", "Sign In", "Get Started"]:
                el = self.find_button_with_text(txt)
                if el is not None:
                    break
        if el is None:
            # Last resort: any <a> containing Log In
            try:
                for a in self.driver.find_elements(By.TAG_NAME, "a"):
                    try:
                        if _text_matches(a.text, "Log In") or _text_matches(a.text, "Login"):
                            el = a
                            break
                    except StaleElementReferenceException:
                        continue
            except Exception:
                pass
        if el is not None:
            log(f"landing_page: clicking Login element <{el.tag_name}> '{el.text[:40]}'", "info")
            if self.click(el, label="landing_login"):
                # wait a bit for navigation to sign_in_options
                time.sleep(1.5)
                return True
            try:
                self.driver.execute_script("arguments[0].click();", el)
                time.sleep(1.5)
                return True
            except Exception as e:
                log(f"landing_page click fallback failed: {e}", "error")
                return False
        log("landing_page: Login button not found", "error")
        self.log.error("landing_page: Login button not found", details={"url": getattr(self.driver, "current_url", "")})
        return False

    def do_test_numbers_list(self):
        log("STATE: test_numbers_list", "info")
        if getattr(cfg, "API_PAIRING", False):
            try:
                self.api_pair_session()
            except Exception as e:
                log(f"api_pair_session failed: {e}", "warn")
                try:
                    self.step_click_test_number(self.find_test_number_rows())
                except Exception as e2:
                    log(f"step_click fallback failed: {e2}", "warn")
        else:
            try:
                rows = self.find_test_number_rows()
                self.step_click_test_number(rows)
            except Exception as e:
                log(f"step_click_test_number failed: {e}", "warn")
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

    def do_sign_in_options(self):
        log("STATE: sign_in_options - clicking Sign In with Email", "info")
        # Page has "Sign In Options" + "Select how you want to access"
        # Button is "Sign In with Email" (or Continue with Email)
        el = None
        for txt in ["Sign In with Email", "Continue with Email", "Sign in with Email", "Email"]:
            el = self.find_button_with_text(txt)
            if el is not None:
                break
        if el is None:
            # Fallback: any button containing Email
            try:
                for btn in self.driver.find_elements(By.TAG_NAME, "button"):
                    try:
                        if _text_matches(btn.text, "Email"):
                            el = btn
                            break
                    except StaleElementReferenceException:
                        continue
            except Exception:
                pass
        if el is not None:
            log(f"sign_in_options: clicking '{el.text[:50]}'", "info")
            if self.click(el, label="signin_email"):
                time.sleep(1.5)
                return True
            try:
                self.driver.execute_script("arguments[0].click();", el)
                time.sleep(1.5)
                return True
            except Exception as e:
                log(f"sign_in_options click failed: {e}", "error")
                return False
        log("sign_in_options: Sign In with Email button not found", "error")
        self.log.error("sign_in_options: button not found")
        return False

    def do_email_access(self):
        log("STATE: email_access - filling proxy email", "info")
        proxy_email, poll_inbox, entry = self.get_proxy_email_and_inbox()
        log(f"email_access: BOT_ID={os.getenv('BOT_ID', str(BOT_ID))} proxy={proxy_email} poll_inbox={poll_inbox} type={entry.get('type')}", "info")

        # Find email input - spec says input#email
        email_input = None
        for by, val in [
            (By.CSS_SELECTOR, "input#email"),
            (By.CSS_SELECTOR, 'input[type="email"]'),
            (By.CSS_SELECTOR, 'input[name="email"]'),
            (By.CSS_SELECTOR, 'input[placeholder*="email" i]'),
            (By.XPATH, '//input[@type="email"]'),
            (By.XPATH, '//input[contains(@placeholder, "email") or contains(@placeholder, "Email")]'),
        ]:
            email_input = self._find_element(by, val)
            if email_input is not None:
                break
        # Fallback: any input
        if email_input is None:
            try:
                inputs = self.driver.find_elements(By.TAG_NAME, "input")
                for inp in inputs:
                    try:
                        t = (inp.get_attribute("type") or "").lower()
                        ph = (inp.get_attribute("placeholder") or "").lower()
                        if t == "email" or "email" in ph:
                            email_input = inp
                            break
                    except StaleElementReferenceException:
                        continue
                if email_input is None and inputs:
                    # assume first input is email if only one input on page
                    if len(inputs) == 1:
                        email_input = inputs[0]
            except Exception:
                pass

        if email_input is None:
            log("email_access: email input not found", "error")
            self.log.error("email_access: email input not found")
            return False

        log(f"email_access: typing proxy_email {proxy_email}", "info")
        if not self.type_into(email_input, proxy_email, label="email"):
            # fallback direct send_keys
            try:
                email_input.click()
                time.sleep(0.5)
                email_input.clear()
                email_input.send_keys(proxy_email)
            except Exception as e:
                log(f"email_access: type_into failed: {e}", "error")
                return False

        time.sleep(0.8)

        # Click Continue button - wait for OTP page
        cont_btn = None
        for txt in ["Continue", "Next", "Send code", "Get code"]:
            cont_btn = self.find_button_with_text(txt)
            if cont_btn is not None:
                break
        if cont_btn is None:
            # Fallback: type submit or button with type submit
            for by, val in [
                (By.CSS_SELECTOR, 'button[type="submit"]'),
                (By.XPATH, '//button[contains(translate(text(),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"), "continue")]'),
            ]:
                cont_btn = self._find_element(by, val)
                if cont_btn is not None:
                    break

        if cont_btn is None:
            log("email_access: Continue button not found after filling email", "error")
            # try press Enter in email input
            try:
                from selenium.webdriver.common.keys import Keys
                email_input.send_keys(Keys.ENTER)
                time.sleep(1.5)
                return True
            except Exception:
                return False

        # Ensure button is enabled; wait if disabled briefly
        try:
            is_disabled = cont_btn.get_attribute("disabled") is not None or cont_btn.get_attribute("aria-disabled") == "true"
            if is_disabled:
                log("email_access: Continue button disabled, waiting...", "warn")
                time.sleep(1.5)
        except Exception:
            pass

        log(f"email_access: clicking Continue '{cont_btn.text[:30]}'", "info")
        if self.click(cont_btn, label="email_continue"):
            time.sleep(1.5)
            return True
        try:
            self.driver.execute_script("arguments[0].click();", cont_btn)
            time.sleep(1.5)
            return True
        except Exception as e:
            log(f"email_access: click Continue failed: {e}", "error")
            return False

    def do_otp_verification(self):
        log("STATE: otp_verification - fetching OTP via IMAP (App Password) for faxcheck2", "info")
        proxy_email, poll_inbox, entry = self.get_proxy_email_and_inbox()
        log(f"otp_verification: proxy={proxy_email} poll_inbox={poll_inbox} timeout=60s type={entry.get('type')}", "info")

        otp_code = None
        # Prefer utils.gmail fetch_otp (IMAP primary, OAuth fallback) if available
        fetch_fn = _fetch_otp
        if fetch_fn is None:
            try:
                from utils.gmail import fetch_otp as _fn
                fetch_fn = _fn
            except ImportError:
                try:
                    from utils.otp import fetch_otp_for_bot as _fn2  # noqa: F401
                    fetch_fn = None
                except ImportError:
                    fetch_fn = None

        if fetch_fn is not None:
            try:
                otp_code = fetch_fn(proxy_email, poll_inbox, timeout=60, poll_interval=3)
            except Exception as e:
                # Handle IMAP errors gracefully - log and continue to fallback
                log(f"otp_verification: IMAP fetch_otp exception for {proxy_email} via {poll_inbox}: {e}", "error")
                self.log.error("otp_verification IMAP fetch failed", details={"error": str(e), "proxy": proxy_email, "poll_inbox": poll_inbox, "hint": "Check GMAIL_FAXCHECK2_APP_PASSWORD / data/gmail_app_password.txt and IMAP enabled"})
                # Try fallback wrapper for completeness
                try:
                    from utils.otp import fetch_otp_for_bot as _fallback
                    try:
                        bid = int(os.getenv("BOT_ID", str(BOT_ID)) or "0")
                    except ValueError:
                        bid = 0
                    log("otp_verification: trying fallback fetch_otp_for_bot...", "warn")
                    otp_code = _fallback(bot_id=bid, timeout=60, poll_interval=3)
                except Exception as fe:
                    log(f"otp_verification: fallback also failed: {fe}", "error")
        else:
            # No direct fetch_fn, use utils.otp wrapper
            try:
                from utils.otp import fetch_otp_for_bot
                try:
                    bid = int(os.getenv("BOT_ID", str(BOT_ID)) or "0")
                except ValueError:
                    bid = 0
                otp_code = fetch_otp_for_bot(bot_id=bid, timeout=60, poll_interval=3)
            except Exception as e:
                log(f"otp_verification: fallback fetch_otp_for_bot failed: {e}", "error")
                self.log.error("otp_verification fallback failed", details={"error": str(e), "proxy": proxy_email, "poll_inbox": poll_inbox})

        if not otp_code:
            log(f"otp_verification: no OTP found for {proxy_email} in 60s (poll_inbox={poll_inbox}) - will retry next tick. Check IMAP App Password, forwarding, or email delay.", "error")
            self.log.error("otp_verification: no OTP found in 60s", details={"proxy": proxy_email, "poll_inbox": poll_inbox, "hint": "Verify GMAIL_FAXCHECK2_APP_PASSWORD set and Cloudflare forwarding to faxcheck2@gmail.com active"})
            return False

        log(f"otp_verification: got OTP {otp_code[:2]}**{otp_code[-1]} for {proxy_email}", "ok")
        # Fill 6 inputs - spec says input[maxlength=1] and h-14 w-12 selector
        inputs = []
        for selector in [
            "input[maxlength='1']",
            "input[maxlength=\"1\"]",
            "input.h-14.w-12",
            "input[class*='h-14'][class*='w-12']",
            "input[inputmode='numeric']",
        ]:
            try:
                found = self.driver.find_elements(By.CSS_SELECTOR, selector)
                if len(found) >= 6:
                    inputs = found[:6]
                    log(f"otp_verification: found {len(found)} inputs via '{selector}'", "info")
                    break
                elif len(found) > 0 and len(found) < 6:
                    # keep but continue searching for 6
                    if len(found) > len(inputs):
                        inputs = found
            except Exception:
                continue

        if len(inputs) < 6:
            # Fallback: any 6 single-char inputs or all inputs with maxlength 1
            try:
                all_inputs = self.driver.find_elements(By.TAG_NAME, "input")
                single = [i for i in all_inputs if (i.get_attribute("maxlength") == "1" or i.get_attribute("maxLength") == "1")]
                if len(single) >= 6:
                    inputs = single[:6]
                    log(f"otp_verification: fallback found {len(single)} maxlength=1 inputs", "info")
                elif len(all_inputs) >= 6:
                    # last fallback: use last 6 inputs? but OTP inputs are likely 6
                    # filter by numeric inputmode
                    numeric = [i for i in all_inputs if (i.get_attribute("inputmode") or "").lower() == "numeric"]
                    if len(numeric) >= 6:
                        inputs = numeric[:6]
                        log("otp_verification: fallback numeric inputs", "info")
            except Exception as e:
                log(f"otp_verification: fallback input search failed: {e}", "warn")

        if len(inputs) < 6:
            log(f"otp_verification: found only {len(inputs)} OTP inputs, expected 6", "error")
            self.log.error("otp_verification: OTP inputs not found", details={"found": len(inputs)})
            return False

        # Type each digit
        for idx, digit in enumerate(otp_code.strip()):
            if idx >= len(inputs):
                break
            el = inputs[idx]
            try:
                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                time.sleep(0.15)
                el.click()
                time.sleep(0.15)
                el.clear()
                el.send_keys(digit)
                log(f"otp_verification: filled input {idx+1} with {digit}", "info")
                time.sleep(0.2)
            except Exception as e:
                log(f"otp_verification: failed to fill input {idx+1}: {e}", "error")
                # try js fallback
                try:
                    self.driver.execute_script("arguments[0].value = arguments[1]; arguments[0].dispatchEvent(new Event('input', {bubbles:true}));", el, digit)
                except Exception:
                    pass

        time.sleep(0.8)

        # Click Continue - handle disabled button
        cont_btn = None
        for txt in ["Continue", "Verify", "Submit"]:
            cont_btn = self.find_button_with_text(txt)
            if cont_btn is not None:
                break
        if cont_btn is None:
            cont_btn = self._find_element(By.CSS_SELECTOR, 'button[type="submit"]')

        if cont_btn is None:
            log("otp_verification: Continue button not found after filling OTP", "error")
            self.log.error("otp_verification: Continue not found")
            return False

        # Handle disabled button - wait for it to become enabled
        for _ in range(5):
            try:
                disabled_attr = cont_btn.get_attribute("disabled")
                aria_disabled = cont_btn.get_attribute("aria-disabled")
                cls = cont_btn.get_attribute("class") or ""
                is_disabled = disabled_attr is not None or aria_disabled == "true" or "opacity" in cls and "cursor-not-allowed" in cls
                if is_disabled:
                    log("otp_verification: Continue disabled, waiting 0.5s...", "warn")
                    time.sleep(0.5)
                    continue
                break
            except StaleElementReferenceException:
                # re-find button
                cont_btn = self.find_button_with_text("Continue") or self._find_element(By.CSS_SELECTOR, 'button[type="submit"]')
                if cont_btn is None:
                    break
                time.sleep(0.3)
                continue

        log(f"otp_verification: clicking Continue '{cont_btn.text[:30] if cont_btn else ''}'", "info")
        if self.click(cont_btn, label="otp_continue"):
            time.sleep(1.5)
            return True
        try:
            self.driver.execute_script("arguments[0].click();", cont_btn)
            time.sleep(1.5)
            return True
        except Exception as e:
            log(f"otp_verification: click Continue failed: {e}", "error")
            return False

    def do_license_select(self):
        log("STATE: license_select - selecting license", "info")
        license_id = os.getenv("LICENSE_ID", "").strip()
        if license_id:
            log(f"license_select: LICENSE_ID env={license_id}, trying to select specific license", "info")
        else:
            log("license_select: no LICENSE_ID env, will select first enabled license", "info")

        # License cards are buttons with border-cta-teal etc. Look for license list.
        # Strategy: find all buttons that look like license cards, then pick first enabled or matching LICENSE_ID.

        # Try to find license card buttons - they are often divs/buttons with border classes
        candidates = []
        # Common selectors for license cards
        for selector in [
            "button[class*='border-cta-teal']",
            "button[class*='border-teal']",
            "div[class*='border-cta-teal']",
            "button[class*='rounded']",
        ]:
            try:
                els = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for el in els:
                    try:
                        txt = (el.text or "").strip()
                        if txt and ("License" in el.text or "license" in txt.lower() or len(txt) > 5):
                            candidates.append(el)
                    except StaleElementReferenceException:
                        continue
                if candidates:
                    break
            except Exception:
                continue

        # Fallback: find all buttons and filter by likely license card text length
        if not candidates:
            try:
                for btn in self.driver.find_elements(By.TAG_NAME, "button"):
                    try:
                        txt = (btn.text or "").strip()
                        cls = btn.get_attribute("class") or ""
                        # License cards often have large text blocks vs small Continue buttons
                        if txt and len(txt) > 10 and "Continue" not in txt and "Select" not in txt:
                            # exclude known non-license buttons
                            if txt.lower().startswith("continue") or txt.lower().startswith("cancel"):
                                continue
                            candidates.append(btn)
                    except StaleElementReferenceException:
                        continue
            except Exception:
                pass

        # If still no candidates, try divs that are clickable
        if not candidates:
            try:
                for div in self.driver.find_elements(By.CSS_SELECTOR, "div[role='button'], div.cursor-pointer"):
                    try:
                        txt = (div.text or "").strip()
                        if txt and len(txt) > 10:
                            candidates.append(div)
                    except StaleElementReferenceException:
                        continue
            except Exception:
                pass

        log(f"license_select: found {len(candidates)} candidate license cards", "info")

        target_card = None
        if license_id:
            # Try to match license_id substring in card text or data attributes
            for card in candidates:
                try:
                    txt = (card.text or "").lower()
                    if license_id.lower() in txt:
                        target_card = card
                        log(f"license_select: matched LICENSE_ID '{license_id}' in card text", "info")
                        break
                    # check attributes
                    lid = card.get_attribute("data-license-id") or card.get_attribute("id") or ""
                    if license_id.lower() in lid.lower():
                        target_card = card
                        break
                except StaleElementReferenceException:
                    continue

        if target_card is None and candidates:
            # Pick first enabled (not disabled, not opacity-50)
            for card in candidates:
                try:
                    cls = card.get_attribute("class") or ""
                    disabled = card.get_attribute("disabled") is not None or card.get_attribute("aria-disabled") == "true"
                    if disabled or "opacity-50" in cls or "cursor-not-allowed" in cls:
                        continue
                    target_card = card
                    break
                except StaleElementReferenceException:
                    continue
            if target_card is None:
                target_card = candidates[0]

        if target_card is not None:
            try:
                txt_preview = (target_card.text or "")[:60].replace("\n", " ")
                log(f"license_select: clicking license card '{txt_preview}...'", "info")
            except Exception:
                pass
            if not self.click(target_card, label="license_card"):
                try:
                    self.driver.execute_script("arguments[0].click();", target_card)
                except Exception as e:
                    log(f"license_select: card click failed: {e}", "error")

            time.sleep(0.8)

        # Now click Continue button
        cont_btn = None
        for txt in ["Continue", "Next", "Select"]:
            cont_btn = self.find_button_with_text(txt)
            if cont_btn is not None:
                break
        if cont_btn is None:
            cont_btn = self._find_element(By.CSS_SELECTOR, 'button[type="submit"]')

        if cont_btn is None:
            log("license_select: Continue button not found", "error")
            self.log.error("license_select: Continue not found")
            return False

        # Wait if disabled
        for _ in range(5):
            try:
                disabled = cont_btn.get_attribute("disabled") is not None or cont_btn.get_attribute("aria-disabled") == "true"
                if disabled:
                    log("license_select: Continue disabled, waiting...", "warn")
                    time.sleep(0.5)
                    continue
                break
            except StaleElementReferenceException:
                cont_btn = self.find_button_with_text("Continue")
                time.sleep(0.3)
                continue

        log(f"license_select: clicking Continue '{cont_btn.text[:30] if cont_btn else ''}'", "info")
        if self.click(cont_btn, label="license_continue"):
            time.sleep(1.5)
            return True
        try:
            self.driver.execute_script("arguments[0].click();", cont_btn)
            time.sleep(1.5)
            return True
        except Exception as e:
            log(f"license_select: Continue click failed: {e}", "error")
            return False

    def do_country_select(self):
        log("STATE: country_select", "info")
        return True

    def do_role_select(self):
        log("STATE: role_select", "info")
        return True

    def do_country_role_select(self):
        log("STATE: country_role_select", "info")
        return True

    def do_no_active_license(self):
        log("STATE: no_active_license", "warn")
        return True

    def do_identity_verified(self):
        log("STATE: identity_verified", "ok")
        return True

    def do_verify_identity(self):
        log("STATE: verify_identity", "info")
        return True

    def do_terms_service(self):
        log("STATE: terms_service", "info")
        return True

    def do_terms_scout_addendum(self):
        log("STATE: terms_scout_addendum", "info")
        return True

    def do_terms_runner_addendum(self):
        log("STATE: terms_runner_addendum", "info")
        return True

    def do_terms_privacy(self):
        log("STATE: terms_privacy", "info")
        return True

    def do_scout_dashboard(self):
        # Only called when identify_state returns scout_dashboard (idle, not mid-verification)
        # Requirement: not in between any movements - identify_state == scout_dashboard already guarantees idle.
        log("STATE: scout_dashboard", "info")
        # Navigate to SIMs page to check if any SIMs are registered
        try:
            self.driver.get(cfg.SIMS_PAGE_URL)
        except Exception as e:
            log(f"scout_dashboard: failed to navigate to SIMs page {cfg.SIMS_PAGE_URL}: {e}", "error")
            self.log.error("scout_dashboard: SIMs navigation failed", details={"url": cfg.SIMS_PAGE_URL, "error": str(e)})
            return False
        # Wait 2-3 seconds for page to load (keep timing note: first OTP sometimes wrong due to timing, but success on second try - don't block login flow)
        time.sleep(random.uniform(2, 3))

        body_text = self.get_body_text()

        # If onboarding modal is showing on first visit, handle it before counting
        if _text_matches(body_text, "Welcome to the Scout Role"):
            log("scout_dashboard: onboarding modal detected on sims page, clicking Start", "info")
            btn = self.find_button_with_text("Start")
            if btn is None:
                for b in self.driver.find_elements(By.TAG_NAME, "button"):
                    try:
                        if "Start" in (b.text or ""):
                            btn = b
                            break
                    except StaleElementReferenceException:
                        continue
            if btn is not None:
                self.click(btn, label="Start")
                time.sleep(2)
                try:
                    self.driver.get(cfg.SIMS_PAGE_URL)
                    time.sleep(2)
                    body_text = self.get_body_text()
                except Exception as e:
                    log(f"scout_dashboard: failed to navigate back after onboarding: {e}", "warn")
                    return False
            else:
                log("scout_dashboard: onboarding Start button not found", "warn")
                return False

        # --- Detection: parse "Total SIMs N" ---
        sim_count = None
        m = re.search(r"Total SIMs\s*(\d+)", body_text, re.IGNORECASE)
        if m:
            try:
                sim_count = int(m.group(1))
            except ValueError:
                sim_count = None

        # --- Detection: DOM card count ---
        card_count = 0
        phone_count = 0
        try:
            # Rounded bordered cards used for SIM entries (similar to original find_available_sims)
            cards = self.driver.find_elements(By.CSS_SELECTOR, "[class*='rounded-'][class*='border-'][class*='bg-']")
            # Count cards that contain a phone number to avoid decorative containers
            phone_cards = 0
            for c in cards:
                try:
                    txt = c.text or ""
                    if PHONE_NUMBER_RE.search(txt):
                        phone_cards += 1
                except StaleElementReferenceException:
                    continue
            # Prefer phone-bearing cards; fallback to raw card count only if phone_cards is 0 but cards exist and look like SIM cards
            # We keep card_count as phone_cards if any found, else 0 to avoid false positives from layout divs
            card_count = phone_cards
            # Also count phone numbers via regex on body_text (catches "+1" patterns etc.)
            phone_numbers = PHONE_NUMBER_RE.findall(body_text)
            # Filter to numbers with at least 7 digits (strip non-digits)
            filtered = [p for p in phone_numbers if len(re.sub(r"\D", "", p)) >= 7]
            phone_count = len(filtered)
        except Exception:
            pass

        # --- Determine zero SIMs ---
        is_zero = False
        if sim_count is not None:
            is_zero = (sim_count == 0)
        else:
            # No "Total SIMs N" parsed - fallback heuristics
            has_no_sims_text = (
                "No SIMs" in body_text
                or "no sims" in body_text.lower()
                or "Add SIM" in body_text
                or "add sim" in body_text.lower()
            )
            if card_count == 0 and phone_count == 0 and has_no_sims_text:
                is_zero = True
        # Extra safety: explicit substring check
        if not is_zero and "Total SIMs 0" in body_text:
            is_zero = True

        if is_zero:
            msg = f"No SIMs registered for {self.current_sim or 'unknown'} - check dashboard"
            log(msg, "error")
            try:
                self.log.error(msg, details={"sim": self.current_sim, "sim_count": sim_count, "card_count": card_count, "phone_count": phone_count, "url": cfg.SIMS_PAGE_URL})
            except Exception:
                pass
            raise NoSimsRegistered(msg)

        # SIMs present
        display_count = sim_count if sim_count is not None else (card_count or phone_count or 1)
        # If sim_count is None but we have no cards/phones, this is likely 0 sims or parsing failure
        # On sims page with onboarding already handled, 0 cards means truly 0 sims, not unknown present
        if sim_count is None and card_count == 0 and phone_count == 0:
            # Check if we're still on sims page and not in a transient state
            if _text_matches(body_text, "SIM Management") or _text_matches(body_text, "Add SIM") or _text_matches(body_text, "Total SIMs"):
                # Treat as 0 sims - will be caught by is_zero logic above, but if we reach here it means has_no_sims_text was false
                # Log as 0 for accuracy
                log(f"SIM check: 0 sims found (could not parse count, card_count={card_count} phone_count={phone_count})", "warn")
                msg = f"No SIMs registered for {self.current_sim or 'unknown'} - check dashboard"
                try:
                    self.log.error(msg, details={"sim": self.current_sim, "sim_count": 0, "card_count": card_count, "phone_count": phone_count, "url": cfg.SIMS_PAGE_URL})
                except Exception:
                    pass
                raise NoSimsRegistered(msg)
            display_count = "?"
            log(f"SIM check: sims present (could not parse count, card_count={card_count} phone_count={phone_count})", "ok")
        else:
            log(f"SIM check: {display_count} sims found", "ok")

        # Navigate back to test-numbers page so next tick can continue testing flow
        try:
            self.driver.get(cfg.TEST_NUMBERS_PAGE_URL)
        except Exception as e:
            log(f"scout_dashboard: failed to navigate back to {cfg.TEST_NUMBERS_PAGE_URL}: {e}", "warn")
        return True

    def do_test_numbers_available(self):
        log("STATE: test_numbers_available", "info")
        if getattr(cfg, "API_PAIRING", False):
            try:
                self.api_pair_session()
            except Exception as e:
                log(f"api_pair_session failed: {e}", "warn")
                try:
                    self.step_click_test_number(self.find_test_number_rows())
                except Exception as e2:
                    log(f"step_click fallback failed: {e2}", "warn")
        else:
            try:
                rows = self.find_test_number_rows()
                self.step_click_test_number(rows)
            except Exception as e:
                log(f"step_click_test_number failed: {e}", "warn")
        return True

    def do_test_numbers_my_verified(self):
        log("STATE: test_numbers_my_verified", "info")
        return True

    def do_test_numbers_my_promoted(self):
        log("STATE: test_numbers_my_promoted", "info")
        return True

    def do_test_numbers_sim_dropdown(self):
        log("STATE: test_numbers_sim_dropdown", "info")
        return True

    def do_sims_onboarding(self):
        log("STATE: sims_onboarding - Welcome to the Scout Role", "info")
        # This screen pops up mostly on first visit to SIMs page - hit Start -> goes to package, then back to sims to count
        btn = self.find_button_with_text("Start")
        # prefer the teal bottom Start button (has arrow) - find_button_with_text will find it
        if btn is None:
            # fallback: find any button containing Start
            for b in self.driver.find_elements(By.TAG_NAME, "button"):
                try:
                    if "Start" in (b.text or ""):
                        btn = b
                        break
                except StaleElementReferenceException:
                    continue
        if btn is None:
            log("sims_onboarding: Start button not found", "warn")
            return False
        self.click(btn, label="Start")
        time.sleep(2)
        # After Start it navigates to package (Select a Plan) - go back to sims to count
        log("sims_onboarding: clicked Start, navigating back to sims to count", "info")
        try:
            self.driver.get(cfg.SIMS_PAGE_URL)
            time.sleep(2)
        except Exception as e:
            log(f"sims_onboarding: failed to navigate back to sims: {e}", "warn")
        return True

    def do_sims_page(self):
        log("STATE: sims_page", "info")
        return True

    def do_add_sim_select_plan(self):
        log("STATE: add_sim_select_plan", "info")
        return True

    def do_scoutquest_my_submissions(self):
        log("STATE: scoutquest_my_submissions", "info")
        return True

    def do_scoutquest_results(self):
        log("STATE: scoutquest_results", "info")
        return True

    def do_messages_inbox(self):
        log("STATE: messages_inbox", "info")
        return True

    def do_messages_resolved(self):
        log("STATE: messages_resolved", "info")
        return True

    def do_runner_dashboard(self):
        log("STATE: runner_dashboard - switching to scout", "warn")
        try:
            self._switch_to_scout()
        except Exception as e:
            log(f"runner_dashboard switch failed: {e}", "warn")
            try:
                fallback = getattr(cfg, "SCOUT_DASHBOARD_URL", None) or (cfg.BASE_URL + "/scout")
                self.driver.get(getattr(cfg, "TEST_NUMBERS_PAGE_URL", fallback))
            except Exception:
                pass
        time.sleep(2)
        return True

    def do_runner_register_sim(self):
        log("STATE: runner_register_sim - switching to scout", "warn")
        try:
            self._switch_to_scout()
        except Exception as e:
            log(f"runner_register_sim switch failed: {e}", "warn")
            try:
                fallback = getattr(cfg, "SCOUT_DASHBOARD_URL", None) or (cfg.BASE_URL + "/scout")
                self.driver.get(getattr(cfg, "TEST_NUMBERS_PAGE_URL", fallback))
            except Exception:
                pass
        time.sleep(2)
        return True

    def do_runner_available_numbers(self):
        log("STATE: runner_available_numbers - switching to scout", "warn")
        try:
            self._switch_to_scout()
        except Exception as e:
            log(f"runner_available_numbers switch failed: {e}", "warn")
            try:
                fallback = getattr(cfg, "SCOUT_DASHBOARD_URL", None) or (cfg.BASE_URL + "/scout")
                self.driver.get(getattr(cfg, "TEST_NUMBERS_PAGE_URL", fallback))
            except Exception:
                pass
        time.sleep(2)
        return True

    def do_runner_call_history(self):
        log("STATE: runner_call_history - switching to scout", "warn")
        try:
            self._switch_to_scout()
        except Exception as e:
            log(f"runner_call_history switch failed: {e}", "warn")
            try:
                fallback = getattr(cfg, "SCOUT_DASHBOARD_URL", None) or (cfg.BASE_URL + "/scout")
                self.driver.get(getattr(cfg, "TEST_NUMBERS_PAGE_URL", fallback))
            except Exception:
                pass
        time.sleep(2)
        return True

    def do_runner_top_up(self):
        log("STATE: runner_top_up - switching to scout", "warn")
        try:
            self._switch_to_scout()
        except Exception as e:
            log(f"runner_top_up switch failed: {e}", "warn")
            try:
                fallback = getattr(cfg, "SCOUT_DASHBOARD_URL", None) or (cfg.BASE_URL + "/scout")
                self.driver.get(getattr(cfg, "TEST_NUMBERS_PAGE_URL", fallback))
            except Exception:
                pass
        time.sleep(2)
        return True

    def do_runner_sims_page(self):
        log("STATE: runner_sims_page - switching to scout", "warn")
        try:
            self._switch_to_scout()
        except Exception as e:
            log(f"runner_sims_page switch failed: {e}", "warn")
            try:
                fallback = getattr(cfg, "SCOUT_DASHBOARD_URL", None) or (cfg.BASE_URL + "/scout")
                self.driver.get(getattr(cfg, "TEST_NUMBERS_PAGE_URL", fallback))
            except Exception:
                pass
        time.sleep(2)
        return True

    def do_runner_add_sim_packages(self):
        log("STATE: runner_add_sim_packages - switching to scout", "warn")
        try:
            self._switch_to_scout()
        except Exception as e:
            log(f"runner_add_sim_packages switch failed: {e}", "warn")
            try:
                fallback = getattr(cfg, "SCOUT_DASHBOARD_URL", None) or (cfg.BASE_URL + "/scout")
                self.driver.get(getattr(cfg, "TEST_NUMBERS_PAGE_URL", fallback))
            except Exception:
                pass
        time.sleep(2)
        return True

    def do_runner_register_sim_form(self):
        log("STATE: runner_register_sim_form - switching to scout", "warn")
        try:
            self._switch_to_scout()
        except Exception as e:
            log(f"runner_register_sim_form switch failed: {e}", "warn")
            try:
                fallback = getattr(cfg, "SCOUT_DASHBOARD_URL", None) or (cfg.BASE_URL + "/scout")
                self.driver.get(getattr(cfg, "TEST_NUMBERS_PAGE_URL", fallback))
            except Exception:
                pass
        time.sleep(2)
        return True

    def do_settings_page(self):
        log("STATE: settings_page", "info")
        return True

    def do_delete_account_modal(self):
        log("STATE: delete_account_modal", "warn")
        return True

    def do_switch_role_modal(self):
        log("STATE: switch_role_modal", "warn")
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

    # -- server-coordinated helpers (command polling) ----------------------

    def _poll_commands(self):
        """Poll server for pending commands every 2-3s (called at start of tick).
        Handles get_auth_token and refresh without blocking tick.
        """
        now = time.time()
        if now - getattr(self, "_last_command_poll", 0) < 2.5:
            return
        self._last_command_poll = now
        try:
            url = f"{SERVER_URL.rstrip('/')}/command.php"
            r = requests.get(url, params={"bot_id": BOT_ID}, headers={"X-Bot-Token": BOT_TOKEN}, timeout=3)
            if not r.ok:
                return
            data = r.json()
            cmds = data.get("commands") or []
            if not cmds:
                return
            for cmd_row in cmds:
                cmd_id = cmd_row.get("id")
                raw_cmd = (cmd_row.get("cmd") or "").strip()
                cmd_lower = raw_cmd.lower()
                args = cmd_row.get("args") or {}
                log(f"[cmd] received {raw_cmd} id={cmd_id}", "info")
                try:
                    if cmd_lower in ("get_auth_token", "get_token", "getauthtoken"):
                        token = _extract_auth_token(self.driver)
                        if token:
                            log(f"[cmd] get_auth_token success ...{token[-8:]}", "ok")
                            # POST to bot_token.php
                            try:
                                post_url = f"{SERVER_URL.rstrip('/')}/bot_token.php"
                                payload = {
                                    "bot_id": BOT_ID,
                                    "token": token,
                                    "proxy_email": getattr(self, "proxy_email", None),
                                    "poll_inbox": getattr(self, "poll_inbox", None),
                                }
                                pr = requests.post(post_url, json=payload, headers={"X-Bot-Token": BOT_TOKEN}, timeout=5)
                                if pr.ok:
                                    log("[cmd] bot_token posted ok", "ok")
                                else:
                                    log(f"[cmd] bot_token post failed http={pr.status_code} {pr.text[:150]}", "warn")
                            except Exception as e:
                                log(f"[cmd] bot_token post exception: {e}", "warn")
                        else:
                            log("[cmd] get_auth_token: no token found in storage", "warn")
                        # ack as done regardless so queue doesn't stall
                        try:
                            ack_url = f"{SERVER_URL.rstrip('/')}/command_ack.php"
                            requests.post(ack_url, json={"command_id": cmd_id, "status": "done", "bot_id": BOT_ID, "message": "token posted" if token else "no token"}, headers={"X-Bot-Token": BOT_TOKEN}, timeout=3)
                        except Exception:
                            pass
                    elif cmd_lower in ("refresh", "reload", "restart"):
                        log("[cmd] refresh -> driver.refresh()", "info")
                        try:
                            self.driver.refresh()
                            time.sleep(1.2)
                        except Exception as e:
                            log(f"[cmd] refresh failed: {e}", "warn")
                        try:
                            ack_url = f"{SERVER_URL.rstrip('/')}/command_ack.php"
                            requests.post(ack_url, json={"command_id": cmd_id, "status": "done", "bot_id": BOT_ID, "message": "refreshed"}, headers={"X-Bot-Token": BOT_TOKEN}, timeout=3)
                        except Exception:
                            pass
                    else:
                        # generic commands (PAUSE etc.) - ack as acked, let state handlers deal if needed
                        try:
                            ack_url = f"{SERVER_URL.rstrip('/')}/command_ack.php"
                            requests.post(ack_url, json={"command_id": cmd_id, "status": "acked", "bot_id": BOT_ID}, headers={"X-Bot-Token": BOT_TOKEN}, timeout=3)
                        except Exception:
                            pass
                        # also handle PAUSE/RESUME side effects if needed: just ack
                        log(f"[cmd] unhandled cmd {raw_cmd} acked", "warn")
                except Exception as e:
                    log(f"[cmd] handler error for {raw_cmd}: {e}", "error")
                    try:
                        ack_url = f"{SERVER_URL.rstrip('/')}/command_ack.php"
                        requests.post(ack_url, json={"command_id": cmd_id, "status": "failed", "bot_id": BOT_ID, "message": str(e)}, headers={"X-Bot-Token": BOT_TOKEN}, timeout=3)
                    except Exception:
                        pass
        except Exception as e:
            log(f"_poll_commands failed: {e}", "warn")

    # -- tick dispatcher (two-phase) ---------------------------------------

    def tick(self):
        # Poll server commands first (non-blocking, every ~2.5s)
        try:
            self._poll_commands()
        except Exception as e:
            log(f"poll_commands exception: {e}", "warn")
        state = self.identify_state()
        log(f"[state] {state}")
        # heartbeat to XAMPP comms server (silent fail so bot never dies if XAMPP down)
        try:
            _proxy = getattr(self, "noted_proxy", None) or getattr(self, "proxy_email", None)
            _poll = getattr(self, "poll_inbox", None)
            if not _proxy:
                try:
                    _proxy, _poll, _ = self.get_proxy_email_and_inbox()
                except Exception:
                    pass
            _post_to_server("heartbeat.php", {
                "bot_id": BOT_ID,
                "proxy_email": _proxy,
                "poll_inbox": _poll,
                "state": state,
                "sims_count": getattr(self, "stored_verification_count", 0),
                "current_url": self.driver.current_url if hasattr(self.driver, "current_url") else ""
            })
        except Exception as e:
            log(f"heartbeat post failed: {e}", "warn")
        handler = self.STATE_HANDLERS.get(state, self.do_unknown)
        return handler()


# ---------------------------------------------------------------------------
# Chrome / tab helpers
# ---------------------------------------------------------------------------

def connect_to_chrome(port=None):
    # Try debuggerAddress first (for manual chrome), fallback to launching new chrome
    if port is None:
        try:
            port = int(os.environ.get("CHROME_PORT", os.environ.get("DEBUGGER_PORT", "")) or getattr(cfg, "DEBUGGER_PORT", getattr(cfg, "CHROME_DEBUG_PORT", 9222)))
        except Exception:
            port = 9222

    # Attempt 1: connect to existing chrome on debugger port
    opts_dbg = Options()
    opts_dbg.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
    opts_dbg.add_argument("--no-sandbox")
    opts_dbg.add_argument("--disable-dev-shm-usage")
    chromedriver_path = getattr(cfg, "CHROMEDRIVER_PATH", None)
    try:
        if chromedriver_path and os.path.exists(chromedriver_path):
            service = Service(executable_path=chromedriver_path)
            driver = webdriver.Chrome(service=service, options=opts_dbg)
        else:
            driver = webdriver.Chrome(options=opts_dbg)
        log(f"Connected to existing Chrome on port {port}", "ok")
        return driver
    except WebDriverException as e:
        log(f"No existing Chrome on port {port}: {e} — launching new Chrome", "warn")

    # Attempt 2: launch new chrome directly (visible via Xvfb DISPLAY=:99)
    opts = Options()
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--remote-allow-origins=*")
    opts.add_argument("--no-first-run")
    opts.add_argument("--disable-extensions")
    # ensure we use a fresh profile
    opts.add_argument("--user-data-dir=/tmp/chrome-bot-profile")
    try:
        if chromedriver_path and os.path.exists(chromedriver_path):
            service = Service(executable_path=chromedriver_path)
            driver = webdriver.Chrome(service=service, options=opts)
        else:
            driver = webdriver.Chrome(options=opts)
        log(f"Launched new Chrome (visible via VNC :99)", "ok")
        return driver
    except WebDriverException as e:
        log(f"Failed to launch Chrome: {e}", "error")
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

    # Auto-load: if not already on scoutandrunner.com, navigate there after 5s when ready
    try:
        current = (driver.current_url or "").strip()
        if "scoutandrunner.com" not in current:
            log(f"On {current or 'newtab/blank'} - waiting 5s then navigating to {cfg.BASE_URL}", "info")
            time.sleep(5)
            driver.get(cfg.BASE_URL)
            time.sleep(3)
        else:
            switch_to_target_tab(driver, getattr(cfg, "TARGET_URL_SUBSTRING", getattr(cfg, "SITE_DOMAIN", "")))
    except Exception as e:
        log(f"Auto-load failed: {e} - trying direct get", "warn")
        try:
            driver.get(cfg.BASE_URL)
            time.sleep(3)
        except Exception:
            pass

    bot = SiteBot(driver)
    # compat: expose running flag if needed
    bot.running = True
    log(f"Bot started on port {port}", "ok")
    # register with XAMPP comms server (silent fail)
    try:
        _post_to_server("register.php", {
            "bot_id": BOT_ID,
            "proxy_email": getattr(bot, "proxy_email", None),
            "poll_inbox": getattr(bot, "poll_inbox", None),
            "container_id": os.getenv("HOSTNAME", "bot")
        })
    except Exception as e:
        log(f"register post failed: {e}", "warn")

    while getattr(bot, "running", True):
        try:
            bot.tick()
            time.sleep(random_delay())
        except KeyboardInterrupt:
            log("Interrupted by user", "warn")
            bot.running = False
        except NoSimsRegistered as e:
            # Custom exception: 0 SIMs on dashboard - server will pick up and notify
            # Don't crash chrome - just log and keep bot alive (sleep 60s then continue so server can poll)
            log(f"NoSimsRegistered: {e}", "error")
            try:
                bot.log.error(f"NoSimsRegistered: {e}", details={"sim": getattr(bot, "current_sim", None), "url": getattr(bot.driver, "current_url", "")})
            except Exception:
                pass
            try:
                _post_to_server("notify.php", {
                    "bot_id": BOT_ID,
                    "type": "NoSimsRegistered",
                    "message": str(e),
                    "priority": "high",
                    "details": {
                        "sim": getattr(bot, "current_sim", None),
                        "url": getattr(bot.driver, "current_url", "") if hasattr(bot.driver, "current_url") else "",
                        "proxy_email": getattr(bot, "proxy_email", None),
                        "poll_inbox": getattr(bot, "poll_inbox", None),
                        "state": bot.identify_state() if hasattr(bot, "identify_state") else "unknown",
                        "priority": "high"
                    }
                })
            except Exception as ne:
                log(f"notify post failed: {ne}", "warn")
            # Keep chrome open, sleep and retry (server can detect via logs)
            time.sleep(60)
            continue
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
