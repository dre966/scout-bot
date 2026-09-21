"""OTP wrapper - BOT_ID aware routing to Gmail fetch.

Given BOT_ID, loads routing.json, determines proxy_email and poll_inbox,
then calls fetch_otp from utils.gmail.
"""

import json
import os
from pathlib import Path

_ROUTING_PATHS = [
    Path("/app/data/routing.json"),
    Path("data/routing.json"),
    Path(__file__).resolve().parent.parent / "data" / "routing.json",
]


def _find_routing_path():
    for p in _ROUTING_PATHS:
        if p.exists():
            return p
    # default
    if Path("/app").exists():
        return Path("/app/data/routing.json")
    return Path(__file__).resolve().parent.parent / "data" / "routing.json"


def load_routing():
    """Load routing.json and return list of entries."""
    path = _find_routing_path()
    if not path.exists():
        raise FileNotFoundError(f"routing.json not found at {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data


def get_proxy_for_bot(bot_id=None, routing=None):
    """Determine proxy email for given BOT_ID.

    If BOT_EMAIL env var is set, it overrides BOT_ID selection.
    Otherwise proxy_email = emails[BOT_ID % len(emails)] where emails is list of proxy addresses from routing.json.

    Returns:
        tuple (proxy_email, poll_inbox, entry)
    """
    # BOT_EMAIL override
    bot_email_override = os.getenv("BOT_EMAIL")
    if bot_email_override:
        bot_email_override = bot_email_override.strip()
        # Try to find matching entry
        if routing is None:
            routing = load_routing()
        for entry in routing:
            if entry.get("proxy", "").lower() == bot_email_override.lower():
                return entry["proxy"], entry["poll_inbox"], entry
        # Not found in routing, assume direct type but poll_inbox is itself if gmail, else faxcheck2
        # For unknown proxy, default poll_inbox to faxcheck2@gmail.com
        return bot_email_override, "faxcheck2@gmail.com", {"proxy": bot_email_override, "poll_inbox": "faxcheck2@gmail.com", "type": "override"}

    if bot_id is None:
        try:
            bot_id = int(os.getenv("BOT_ID", "0"))
        except ValueError:
            bot_id = 0

    if routing is None:
        routing = load_routing()

    if not routing:
        raise ValueError("routing.json is empty")

    idx = bot_id % len(routing)
    entry = routing[idx]
    return entry["proxy"], entry["poll_inbox"], entry


def get_routing_entry(proxy_email):
    """Find routing entry by proxy email."""
    routing = load_routing()
    for e in routing:
        if e.get("proxy", "").lower() == proxy_email.lower():
            return e
    return None


def fetch_otp_for_bot(bot_id=None, timeout=60, poll_interval=3):
    """Fetch OTP for bot's proxy email via Gmail IMAP (App Password) with OAuth fallback.

    Args:
        bot_id: int BOT_ID, if None reads from env BOT_ID
        timeout: seconds to poll Gmail
        poll_interval: seconds between polls

    Returns:
        str 6-digit OTP or None
    """
    # Import here to avoid circular at import time and allow missing deps gracefully
    try:
        from utils.gmail import fetch_otp
    except ImportError:
        from .gmail import fetch_otp

    proxy_email, poll_inbox, entry = get_proxy_for_bot(bot_id)
    print(f"[otp] BOT_ID={bot_id if bot_id is not None else os.getenv('BOT_ID', '0')} -> proxy={proxy_email} poll_inbox={poll_inbox} type={entry.get('type')}")
    code = fetch_otp(proxy_email, poll_inbox, timeout=timeout, poll_interval=poll_interval)
    return code


# Backwards compat alias
fetch_otp_for_current_bot = fetch_otp_for_bot
