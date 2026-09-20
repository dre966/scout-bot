"""
Configuration for Scout Bot - Docker-ready version.
All settings can be overridden via environment variables.
"""

import os

# Core Settings
SITE_DOMAIN = os.getenv("SITE_DOMAIN", "scoutandrunner.com")
DEBUGGER_PORT = int(os.getenv("DEBUGGER_PORT", "9222"))

# Mode: 1 = semi-manual, 2 = auto
MODE = int(os.getenv("BOT_MODE", "2"))
SEMI_MANUAL_MODE = (MODE == 1)

# URLs
SIMS_PAGE_URL = f"https://{SITE_DOMAIN}/scout/sims"
TEST_NUMBERS_PAGE_URL = f"https://{SITE_DOMAIN}/scout/test-numbers"

# Chrome Settings
CHROME_PATH = os.getenv("CHROME_PATH", "/usr/bin/google-chrome")
CHROMEDRIVER_PATH = os.getenv("CHROMEDRIVER_PATH", "/usr/bin/chromedriver")

# Container Detection
CONTAINER_CLASSES = ["flex-1", "flex", "flex-col"]
TEXT_MATCH_CASE_SENSITIVE = False

# UI Triggers
TRIGGERS = {
    "test_numbers_label": "Test Numbers",
    "test_number_button": "Test Number",
    "session_label": "Session",
    "confirm_session_label": "Confirm verification session",
    "start_button": "Start",
    "max_sessions_label": "You've reached the maximum",
    "cancel_button": "Cancel",
    "balance_entry_label": "remaining minutes or balance amount",
    "continue_button": "Continue",
    "package_label": "Test this number using your approved Package",
    "select_one_label": "What did you hear",
    "call_result_label": "Submit remaining balance",
    "submit_button": "Submit",
    "continue_verification_label": "Continue Verification Session",
    "call_this_number_label": "Call this number using your approved",
    "heard_ivr_button": "I heard audio",
    "call_completed_label": "Completed",
    "start_next_call_button": "Start call",
    "verification_complete_label": "Verification Complete",
    "back_to_available_button": "Back to Available Numbers",
    "already_tested_label": "already tested this number",
    "resume_test_label": "Finish your current test first",
    "resume_test_button": "Resume test",
    "keep_testing_button": "Keep Testing",
    "verification_ended_label": "Verification Ended",
    "nothing_to_scout_label": "Nothing to scout yet",
    "landing_page_label": "Earn real rewards",
    "suspended_label": "suspended",
}

# Balance Input
BALANCE_INPUT_VALUE = os.getenv("BALANCE_INPUT_VALUE", "60")

# Package Option
PACKAGE_OPTION_BUTTON = "I heard audio"

# Dispositions - NEVER include "correct IVR" (honeypot trap)
SELECT_ONE_OPTIONS = [
    "I heard an IVR, but it was incorrect",
    "I heard an IVR, but it was incorrect",
    "I heard an IVR, but it was incorrect",
    "I heard unusual or unexpected audio",
    "I heard unusual or unexpected audio",
    "I heard a busy tone",
    "I heard a busy tone",
]

# Negative options - lock to category for session consistency
NEGATIVE_OPTIONS = [
    "I heard an IVR, but it was incorrect",
    "I heard unusual or unexpected audio",
    "I heard a busy tone",
]

# Banned dispositions (causes Verification Ended)
BANNED_DISPOSITIONS = ["A human answered", "I heard voice mail"]

# Call behavior
NO_CONNECT_CHANCE = 0.15
NOTES_CHANCE = 0.8

# Notes pool
NOTES_POOL = [
    "audio was clear",
    "heard standard IVR menu",
    "brief hold music then IVR",
    "number rang several times",
    "heard automated greeting",
    "standard business IVR",
    "menu options were clear",
    "audio quality was good",
    "heard dial tone then IVR",
    "typical phone system response",
]

NOTES_BY_DISPOSITION = {
    "I heard a busy tone": [
        "heard continuous busy tone",
        "line was busy",
        "engaged tone throughout",
        "number was busy",
        "heard busy signal",
    ],
    "I heard an IVR, but it was incorrect": [
        "this number does not exist",
        "there is no record of this number",
        "number not recognized in system",
        "automated message saying number invalid",
        "heard recording that number does not exist",
    ],
    "A human answered": [
        "live person answered the call",
        "someone picked up and spoke",
        "person answered the phone",
        "real person on the line",
        "human voice answered",
    ],
    "I heard voice mail": [
        "reached voicemail box",
        "voicemail greeting played",
        "automated voicemail message",
        "call went to voicemail",
        "heard voicemail prompt",
    ],
    "I heard unusual or unexpected audio": [
        "heard strange audio",
        "unusual sound on the line",
        "unexpected noise or static",
        "weird audio played",
        "non-standard audio detected",
    ],
    "The call did not connect": [
        "call did not go through",
        "number was unreachable",
        "call failed to connect",
        "no answer or connection",
        "line was dead",
    ],
}

# Country filtering
ALLOWED_COUNTRIES = os.getenv("ALLOWED_COUNTRIES", "IT,FR,NL,ES,BE,SI,GB,SN,BI,JO,CD,SL,BO,HR,AT,MX,BR,DK").split(",")

# Number validation (all disabled for flexibility)
SKIP_PREFIX_FILTER = True
SKIP_LENGTH_FILTER = True
SKIP_COUNTRY_FILTER = True

MAX_NUMBER_LENGTH = {
    "NL": 9, "IT": 10, "FR": 9, "ES": 9, "BE": 9, "SI": 8,
    "GB": 10, "SN": 9, "BI": 8, "JO": 9, "CD": 9, "SL": 8,
    "BO": 8, "HR": 9, "AT": 10, "MX": 10, "BR": 11, "DK": 8,
}

MOBILE_PREFIXES = {
    "NL": ["6"], "IT": ["3"], "FR": ["6", "7"], "ES": ["6", "7"],
    "BE": ["4"], "SI": ["3", "4", "5", "6", "7"], "GB": ["7"],
    "SN": ["7"], "BI": ["690"], "JO": ["7"], "CD": ["8"],
    "SL": ["7"], "BO": ["7"], "HR": ["9"], "AT": ["6", "7"],
    "MX": ["1"], "BR": ["219"], "DK": ["2", "3", "4", "5", "6", "7", "8", "9"],
}

# CSS Selectors
CHEVRON_SVG_SELECTOR = "svg.lucide-chevron-down"
DISPOSITION_CONTAINER_SELECTOR = "div.mt-auto.flex.flex-col.gap-2"

# Number Selection
FILTER_SELECTION_STRATEGY = "random"
PHONE_NUMBER_PATTERN = r"[\d][\d\-\.\s\(\)]{6,}\d"
MIN_NUMBER_BUTTON_DIGITS = 7
NUMBER_SELECTION_STRATEGY = os.getenv("NUMBER_SELECTION_STRATEGY", "first")
AVOID_REPEAT_NUMBERS = True
ENABLE_PHONE_SELECTION = False

# Reuse dispositions for repeat numbers
REUSE_NUMBERS = True

# Timing (in seconds)
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "1.0"))
FIXED_DELAY_MIN = 0
FIXED_DELAY_MAX = 1
STEP_DELAY_MIN = 0.5
STEP_DELAY_MAX = 1.0
LONG_PAUSE_CHANCE = 0.1
LONG_PAUSE_MIN = 0.5
LONG_PAUSE_MAX = 1
NEXT_DELAY_MIN = 0.5
NEXT_DELAY_MAX = 1
NO_SIM_TIMEOUT = 5

# Human-like behavior
HUMAN_LIKE_MODE = os.getenv("HUMAN_LIKE_MODE", "true").lower() == "true"
RANDOM_SCROLL_CHANCE = 0.3
RANDOM_PAUSE_CHANCE = 0.2
RANDOM_PAUSE_MIN = 0.5
RANDOM_PAUSE_MAX = 2.0
MOUSE_WOBBLE_CHANCE = 0.25
DOUBLE_CLICK_CHANCE = 0.05

# API Pairing
API_PAIRING = os.getenv("API_PAIRING", "true").lower() == "true"

# API Candidates - can be loaded from environment or file
API_CANDIDATES = [
    {"id": "57ca0fd4-0f64-440e-b483-af4e6fb057eb", "phone": "+59170702213", "country": "BO"},
    {"id": "57f4364c-eeac-4e2c-b53d-496eb0b2ffab", "phone": "+59170702225", "country": "BO"},
    {"id": "45d6a83b-5419-457c-afa6-0d59ba4ef298", "phone": "+556238420016", "country": "BR"},
    {"id": "2a385acd-0ff6-425e-a2b0-97447d8ecbca", "phone": "+972555072455", "country": "IL"},
    {"id": "6d7c54f7-41b9-4588-adc1-6c87dab33eb6", "phone": "+576015800761", "country": "CO"},
    {"id": "c5fe4e7a-a4f0-4e92-811b-69cde757bb47", "phone": "+51920960315", "country": "PE"},
    {"id": "af4581b7-15e5-473d-b3b0-d50910bef180", "phone": "+3197010278461", "country": "NL"},
    {"id": "d15626e9-a085-40d3-bfcf-b2b471b2f065", "phone": "+447520685978", "country": "GB"},
    {"id": "7ef51023-d3e4-4e9d-8174-e09d5bb49b5b", "phone": "+5531910142717", "country": "BR"},
    {"id": "3f706d3b-8ee8-4213-a300-1edfb453d753", "phone": "+529986090147", "country": "MX"},
    {"id": "e174241e-f3de-45a4-9410-a1d7f3880fb7", "phone": "+59170702216", "country": "BO"},
    {"id": "40df488c-7309-4ed8-9926-c4be803a9d76", "phone": "+441224015590", "country": "GB"},
    {"id": "cf355799-8441-441d-83c4-2a8fc2fd0105", "phone": "+573009158931", "country": "CO"},
    {"id": "5bf0c604-a41a-4a95-8963-60ed9ac42940", "phone": "+4592452876", "country": "DK"},
    {"id": "ceb8a7e9-4a6e-478a-8c46-fe310788b3d3", "phone": "+4592452878", "country": "DK"},
    {"id": "5b52125a-ad56-4f24-aac2-9b8718dccdba", "phone": "+5511920839551", "country": "BR"},
    {"id": "df083f36-ce60-4a2b-a071-0462a57955d6", "phone": "+527446020345", "country": "MX"},
    {"id": "fc674b52-6700-4e86-b354-6354b34fa11f", "phone": "+5521910055680", "country": "BR"},
]

# Disposition map for user input
DISPOSITION_MAP = {
    "busy": "I heard a busy tone",
    "tone": "I heard a busy tone",
    "ivr": "I heard an IVR, but it was incorrect",
    "incorrect": "I heard an IVR, but it was incorrect",
    "wrong": "I heard an IVR, but it was incorrect",
    "unusual": "I heard unusual or unexpected audio",
    "weird": "I heard unusual or unexpected audio",
    "strange": "I heard unusual or unexpected audio",
    "human": "A human answered",
    "person": "A human answered",
    "live": "A human answered",
    "voicemail": "I heard voice mail",
    "vm": "I heard voice mail",
    "icemail": "I heard voice mail",
    "nothing": "The call did not connect",
    "no connect": "The call did not connect",
    "fail": "The call did not connect",
    "dead": "The call did not connect",
}

# Manual hunt mode
MANUAL_HUNT = False
MANUAL_HUNT_REFRESH_INTERVAL = 30

# Colors (disable in Docker for cleaner logs)
ENABLE_COLORS = os.getenv("ENABLE_COLORS", "false").lower() == "true"

# Max stale retries before recovery
MAX_STALE_RETRIES = 2

# API endpoints
BASE_URL = f"https://{SITE_DOMAIN}"
SCOUT_API = "api/scout"
RUNNER_API = "api/runner"

# Auth API
AUTH_API_URL = "https://api.unityedge.io/auth/v1"
AUTH_API_KEY = os.getenv("AUTH_API_KEY", "sb_publishable_yKqi0fu5vV6G4ryUIMJuzw_NCoFEl1c")
