"""Gmail API helper for OTP fetching.

Handles OAuth flow, token storage (/app/data/gmail_token.json), refresh,
and Gmail API calls to fetch 6-digit OTP codes.
"""

import base64
import json
import os
import re
import time
from pathlib import Path

# Scopes required for reading Gmail
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Token / credentials paths - support both Docker (/app/data) and local (data/)
_CANDIDATE_TOKEN_PATHS = [
    Path("/app/data/gmail_token.json"),
    Path("data/gmail_token.json"),
    Path(__file__).resolve().parent.parent / "data" / "gmail_token.json",
]

_CANDIDATE_CREDENTIALS_PATHS = [
    Path("/app/data/gmail_credentials.json"),
    Path("data/gmail_credentials.json"),
    Path(__file__).resolve().parent.parent / "data" / "gmail_credentials.json",
]

# Regex for 6-digit OTP
OTP_RE = re.compile(r"\b\d{6}\b")


def _find_existing_path(candidates):
    for p in candidates:
        if p.exists():
            return p
    return None


def _get_credentials_path():
    p = _find_existing_path(_CANDIDATE_CREDENTIALS_PATHS)
    if p is not None:
        return p
    # default to Docker path or local fallback
    # Prefer /app/data if /app exists (Docker), else data/
    if Path("/app").exists():
        return Path("/app/data/gmail_credentials.json")
    return Path(__file__).resolve().parent.parent / "data" / "gmail_credentials.json"


def _get_token_path(poll_inbox_email=None):
    """Return token path. Supports per-inbox token if needed, but defaults to single file."""
    # If poll_inbox_email provided, check for per-inbox token first
    if poll_inbox_email:
        safe = poll_inbox_email.replace("@", "_at_").replace(".", "_")
        per_inbox_candidates = [
            Path(f"/app/data/gmail_token_{safe}.json"),
            Path(f"data/gmail_token_{safe}.json"),
            Path(__file__).resolve().parent.parent / "data" / f"gmail_token_{safe}.json",
        ]
        per = _find_existing_path(per_inbox_candidates)
        if per is not None:
            return per
    p = _find_existing_path(_CANDIDATE_TOKEN_PATHS)
    if p is not None:
        return p
    if Path("/app").exists():
        return Path("/app/data/gmail_token.json")
    return Path(__file__).resolve().parent.parent / "data" / "gmail_token.json"


def _get_token_path_for_save(poll_inbox_email=None):
    """Path to save token to (always main token path unless per-inbox env requested)."""
    # Use env GMAIL_TOKEN_PATH override if set
    env_path = os.getenv("GMAIL_TOKEN_PATH")
    if env_path:
        return Path(env_path)
    # Prefer /app/data if /app exists
    if Path("/app").exists():
        return Path("/app/data/gmail_token.json")
    return Path(__file__).resolve().parent.parent / "data" / "gmail_token.json"


def get_gmail_service(poll_inbox_email=None):
    """Return authenticated Gmail API service.

    Args:
        poll_inbox_email: email address of inbox to poll (e.g., faxcheck2@gmail.com).
                          Used to locate per-inbox token if applicable.

    Returns:
        googleapiclient.discovery.Resource for Gmail API.

    Raises:
        FileNotFoundError: if credentials file not found
        RuntimeError: if token not found or invalid and needs auth
    """
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError as e:
        raise ImportError(
            "Missing google API dependencies. Install with: pip install google-api-python-client google-auth google-auth-oauthlib"
        ) from e

    creds_path = _get_credentials_path()
    if not creds_path.exists():
        raise FileNotFoundError(
            f"Gmail credentials not found at {creds_path}. "
            f"Create data/gmail_credentials.json from OAuth client JSON."
        )

    token_path = _get_token_path(poll_inbox_email)

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    # Refresh if expired
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            # save refreshed token
            token_path.parent.mkdir(parents=True, exist_ok=True)
            with open(token_path, "w") as f:
                f.write(creds.to_json())
        except Exception as e:
            # refresh failed, will need re-auth
            raise RuntimeError(f"Failed to refresh Gmail token: {e}. Re-run tools/gmail_auth.py") from e

    if not creds or not creds.valid:
        raise RuntimeError(
            f"Gmail token not found or invalid at {token_path}. "
            f"Run: python tools/gmail_auth.py --poll-inbox {poll_inbox_email or 'faxcheck2@gmail.com'} to authorize."
        )

    service = build("gmail", "v1", credentials=creds)
    return service


def _decode_message_body(msg):
    """Extract decoded text body from Gmail message payload."""
    body_text = ""

    # Prefer snippet as fallback
    snippet = msg.get("snippet", "") or ""

    payload = msg.get("payload", {})
    parts = []

    def collect_parts(part):
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if data and mime.startswith("text/"):
            try:
                decoded = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
                parts.append(decoded)
            except Exception:
                pass
        # html part may contain OTP too, include but stripped
        for sub in part.get("parts", []) or []:
            collect_parts(sub)

    # Start with payload itself
    body_data = payload.get("body", {}).get("data")
    if body_data:
        try:
            decoded = base64.urlsafe_b64decode(body_data + "==").decode("utf-8", errors="ignore")
            parts.append(decoded)
        except Exception:
            pass
    for p in payload.get("parts", []) or []:
        collect_parts(p)

    if parts:
        body_text = "\n".join(parts)
    else:
        # Fallback to snippet if no parts decoded
        body_text = snippet

    # Also include snippet and headers text for OTP search
    if snippet and snippet not in body_text:
        body_text += "\n" + snippet

    # Add subject header if present
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", []) if "name" in h and "value" in h}
    subject = headers.get("subject", "")
    if subject:
        body_text += "\n" + subject

    return body_text


def extract_otp(text):
    """Extract first 6-digit OTP code from text."""
    if not text:
        return None
    m = OTP_RE.search(text)
    return m.group(0) if m else None


def fetch_otp(proxy_email, poll_inbox, timeout=60, poll_interval=3):
    """Poll Gmail API for OTP sent to proxy_email.

    Args:
        proxy_email: The recipient address OTP was sent to (e.g., junchun@cultinet.site)
        poll_inbox: Inbox to poll (e.g., faxcheck2@gmail.com) - Gmail account to query
        timeout: Max seconds to poll
        poll_interval: Seconds between polls

    Returns:
        str 6-digit code or None if not found within timeout.

    Search strategy:
        q="to:proxy_email newer_than:5m" to limit to recent messages.
        Also checks is:unread fallback.
        Decodes body and extracts \b\d{6}\b.
        Marks message as read (removes UNREAD label) if OTP found.
    """
    try:
        from googleapiclient.errors import HttpError
    except ImportError:
        HttpError = Exception

    # Lazy import helper to avoid circular
    try:
        service = get_gmail_service(poll_inbox)
    except Exception as e:
        print(f"[gmail] get_gmail_service failed: {e}")
        return None

    end_time = time.time() + timeout
    # Build queries to try
    # Primary: to:proxy_email newer_than:5m (most precise)
    # Fallback: to:proxy_email is:unread
    queries = [
        f"to:{proxy_email} newer_than:5m",
        f"to:{proxy_email} is:unread",
        f"to:{proxy_email}",
    ]

    last_error = None
    while time.time() < end_time:
        for q in queries:
            try:
                results = service.users().messages().list(userId="me", q=q, maxResults=5).execute()
                messages = results.get("messages", []) or []
                if not messages:
                    continue
                # Fetch each message detail, newest first (list is already sorted newest first)
                for msg_meta in messages:
                    msg_id = msg_meta.get("id")
                    if not msg_id:
                        continue
                    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
                    body = _decode_message_body(msg)
                    code = extract_otp(body)
                    if code:
                        # Optionally mark as read to avoid re-processing
                        try:
                            service.users().messages().modify(
                                userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}
                            ).execute()
                        except Exception:
                            pass
                        print(f"[gmail] OTP found for {proxy_email} (query={q}): {code}")
                        return code
                    # Also try snippet directly
                    snippet = msg.get("snippet", "") or ""
                    code_snip = extract_otp(snippet)
                    if code_snip:
                        try:
                            service.users().messages().modify(
                                userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}
                            ).execute()
                        except Exception:
                            pass
                        print(f"[gmail] OTP found in snippet for {proxy_email}: {code_snip}")
                        return code_snip
            except HttpError as e:
                last_error = e
                print(f"[gmail] API error for query '{q}': {e}")
                continue
            except Exception as e:
                last_error = e
                print(f"[gmail] error for query '{q}': {e}")
                continue
        # No OTP found this round, wait
        remaining = end_time - time.time()
        if remaining <= 0:
            break
        sleep_for = min(poll_interval, remaining)
        time.sleep(sleep_for)

    if last_error:
        print(f"[gmail] fetch_otp timed out for {proxy_email} after {timeout}s (last error: {last_error})")
    else:
        print(f"[gmail] fetch_otp timed out for {proxy_email} after {timeout}s - no OTP found")
    return None


def list_recent_messages(poll_inbox, proxy_email=None, max_results=5):
    """Utility to list recent messages for debugging."""
    service = get_gmail_service(poll_inbox)
    q = f"to:{proxy_email}" if proxy_email else ""
    results = service.users().messages().list(userId="me", q=q, maxResults=max_results).execute()
    return results.get("messages", []) or []
