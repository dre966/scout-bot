"""Gmail OTP helper - IMAP App Password primary, OAuth fallback.

Primary: IMAP via imaplib.IMAP4_SSL("imap.gmail.com") using App Password.
  - Login with poll_inbox email + app password from env GMAIL_FAXCHECK2_APP_PASSWORD
    (or GMAIL_APP_PASSWORD, or per-inbox GMAIL_<LOCAL>_APP_PASSWORD, or
    data/gmail_app_password.txt file).
  - For poll_inbox == faxcheck2@gmail.com, single password suffices for all
    proxy emails because Cloudflare Email Routing forwards to that inbox.
    To header is preserved, so search HEADER To "proxy_email".

Fallback: Gmail API OAuth via googleapiclient if IMAP not configured (kept for compat).

Provides:
  - extract_otp(text) -> str | None
  - fetch_otp(proxy_email, poll_inbox, timeout=60, poll_interval=3) -> str | None
  - get_gmail_service(poll_inbox_email=None)  (OAuth fallback)
  - list_recent_messages(poll_inbox, proxy_email=None, max_results=5)
"""

import base64
import email
import email.policy
import imaplib
import json
import os
import re
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Regex
# ---------------------------------------------------------------------------
OTP_RE = re.compile(r"\b\d{6}\b")

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

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

# App password file candidates (in order of preference)
_CANDIDATE_APP_PW_PATHS = [
    Path("/app/data/gmail_app_password.txt"),
    Path("data/gmail_app_password.txt"),
    Path(__file__).resolve().parent.parent / "data" / "gmail_app_password.txt",
]

# ---------------------------------------------------------------------------
# Helpers - App Password resolution
# ---------------------------------------------------------------------------

def _read_password_file(path: Path):
    """Read password from file, stripping whitespace and removing internal spaces."""
    try:
        if path and path.exists():
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
            # remove all whitespace (App Password displayed as "qece wuff aqbc bdjf")
            cleaned = re.sub(r"\s+", "", text)
            if cleaned:
                return cleaned
    except Exception:
        pass
    return None


def _get_app_password(poll_inbox_email=None):
    """Resolve App Password for poll_inbox.

    Priority:
      1. Per-inbox env: GMAIL_<LOCAL>_APP_PASSWORD  (e.g., GMAIL_FAXCHECK2_APP_PASSWORD)
      2. Generic envs: GMAIL_FAXCHECK2_APP_PASSWORD, GMAIL_APP_PASSWORD, GMAIL_PASSWORD
      3. Per-inbox file: data/gmail_app_password_<safe>.txt
      4. Generic file: data/gmail_app_password.txt (and /app/data/...)

    For direct proxy==poll_inbox cases, also tries faxcheck2 password as fallback
    so single inbox migration still works.
    Returns cleaned password string (no spaces) or None.
    """
    poll_inbox = (poll_inbox_email or "").strip()
    poll_lower = poll_inbox.lower()
    local = poll_lower.split("@")[0] if "@" in poll_lower else poll_lower
    safe_local = re.sub(r"[^a-z0-9]", "_", local).upper() if local else ""
    safe_full = poll_lower.replace("@", "_at_").replace(".", "_") if poll_lower else ""

    # 1. Per-inbox env vars
    env_candidates = []
    if safe_local:
        env_candidates.append(f"GMAIL_{safe_local}_APP_PASSWORD")
    # Also try full email variant just in case
    # e.g., GMAIL_FAXCHECK2_AT_GMAIL_COM_APP_PASSWORD (rare, but cheap to check)
    # Skip to avoid noise - keep simple

    # 2. Generic env vars (order matters)
    env_candidates.extend([
        "GMAIL_FAXCHECK2_APP_PASSWORD",
        "GMAIL_APP_PASSWORD",
        "GMAIL_PASSWORD",
    ])

    # Deduplicate preserving order
    seen = set()
    uniq_envs = []
    for e in env_candidates:
        if e not in seen:
            seen.add(e)
            uniq_envs.append(e)

    for env_name in uniq_envs:
        val = os.getenv(env_name)
        if val and val.strip():
            cleaned = re.sub(r"\s+", "", val.strip())
            if cleaned:
                # For per-inbox env, only return if it matches poll_inbox OR generic
                # If poll_inbox is faxcheck2, GMAIL_FAXCHECK2_APP_PASSWORD is expected
                # If poll_inbox is direct but faxcheck2 env is set, we allow fallback later,
                # but here we return whichever is found first. To prioritize per-inbox,
                # check if this env was per-inbox specific.
                if env_name == f"GMAIL_{safe_local}_APP_PASSWORD":
                    print(f"[gmail] Using App Password from env {env_name} for {poll_inbox}")
                    return cleaned
                # For generic faxcheck2 env, we still allow it as fallback for any poll_inbox
                # But if poll_inbox is not faxcheck2 and we have no per-inbox, this generic is ok.
                # To avoid returning faxcheck2 password for wrong inbox prematurely when per-inbox not yet checked?
                # Since per-inbox was first, it's fine.
                print(f"[gmail] Using App Password from env {env_name} for {poll_inbox}")
                return cleaned

    # 3. File candidates - per-inbox specific file first
    if safe_full:
        per_inbox_files = [
            Path(f"/app/data/gmail_app_password_{safe_full}.txt"),
            Path(f"data/gmail_app_password_{safe_full}.txt"),
            Path(__file__).resolve().parent.parent / "data" / f"gmail_app_password_{safe_full}.txt",
        ]
        for p in per_inbox_files:
            pw = _read_password_file(p)
            if pw:
                print(f"[gmail] Using App Password from file {p} for {poll_inbox}")
                return pw

    # 4. Generic file
    for p in _CANDIDATE_APP_PW_PATHS:
        pw = _read_password_file(p)
        if pw:
            print(f"[gmail] Using App Password from file {p} for {poll_inbox}")
            return pw

    # 5. Also check env GMAIL_FAXCHECK2_APP_PASSWORD explicitly as final fallback
    # (already checked above, but keep for clarity if duplicate logic missed)
    return None


# ---------------------------------------------------------------------------
# OAuth fallback helpers (kept for compatibility)
# ---------------------------------------------------------------------------

def _find_existing_path(candidates):
    for p in candidates:
        if p.exists():
            return p
    return None


def _get_credentials_path():
    p = _find_existing_path(_CANDIDATE_CREDENTIALS_PATHS)
    if p is not None:
        return p
    if Path("/app").exists():
        return Path("/app/data/gmail_credentials.json")
    return Path(__file__).resolve().parent.parent / "data" / "gmail_credentials.json"


def _get_token_path(poll_inbox_email=None):
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
    env_path = os.getenv("GMAIL_TOKEN_PATH")
    if env_path:
        return Path(env_path)
    if Path("/app").exists():
        return Path("/app/data/gmail_token.json")
    return Path(__file__).resolve().parent.parent / "data" / "gmail_token.json"


def get_gmail_service(poll_inbox_email=None):
    """Return authenticated Gmail API service (OAuth fallback).

    Raises FileNotFoundError / RuntimeError if not configured.
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

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.parent.mkdir(parents=True, exist_ok=True)
            with open(token_path, "w") as f:
                f.write(creds.to_json())
        except Exception as e:
            raise RuntimeError(f"Failed to refresh Gmail token: {e}. Re-run tools/gmail_auth.py") from e

    if not creds or not creds.valid:
        raise RuntimeError(
            f"Gmail token not found or invalid at {token_path}. "
            f"Run: python tools/gmail_auth.py --poll-inbox {poll_inbox_email or 'faxcheck2@gmail.com'} to authorize."
        )

    service = build("gmail", "v1", credentials=creds)
    return service


# ---------------------------------------------------------------------------
# OTP extraction & IMAP body decoding
# ---------------------------------------------------------------------------

def extract_otp(text):
    """Extract first 6-digit OTP code from text."""
    if not text:
        return None
    m = OTP_RE.search(text)
    return m.group(0) if m else None


def _extract_text_from_email(msg):
    """Extract decoded text (plain + html + subject) from email.message.Message.

    Handles multipart, base64, quoted-printable, charset variations.
    """
    text_parts = []

    # Walk through parts
    if msg.is_multipart():
        for part in msg.walk():
            # Skip containers
            if part.is_multipart():
                continue
            ctype = part.get_content_type() or ""
            disp = (part.get_content_disposition() or "")
            if disp == "attachment":
                continue
            if ctype.startswith("text/"):
                payload = part.get_payload(decode=True)
                if payload is None:
                    # fallback to string payload
                    try:
                        payload_str = part.get_payload()
                        if isinstance(payload_str, str) and payload_str:
                            text_parts.append(payload_str)
                        continue
                    except Exception:
                        continue
                charset = part.get_content_charset() or "utf-8"
                try:
                    text = payload.decode(charset, errors="ignore")
                except Exception:
                    try:
                        text = payload.decode("utf-8", errors="ignore")
                    except Exception:
                        text = payload.decode("latin-1", errors="ignore")
                if text:
                    text_parts.append(text)
    else:
        payload = msg.get_payload(decode=True)
        if payload is not None:
            charset = msg.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="ignore")
            except Exception:
                text = payload.decode("utf-8", errors="ignore")
            if text:
                text_parts.append(text)
        else:
            try:
                payload_str = msg.get_payload()
                if isinstance(payload_str, str) and payload_str:
                    text_parts.append(payload_str)
            except Exception:
                pass

    # Add subject & snippet-like headers for robustness
    for hdr in ("Subject",):
        val = msg.get(hdr)
        if val:
            text_parts.append(str(val))

    return "\n".join(text_parts)


def _decode_gmail_api_body(msg):
    """Extract decoded text body from Gmail API message payload (compat)."""
    body_text = ""
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
        for sub in part.get("parts", []) or []:
            collect_parts(sub)

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
        body_text = snippet

    if snippet and snippet not in body_text:
        body_text += "\n" + snippet

    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", []) if "name" in h and "value" in h}
    subject = headers.get("subject", "")
    if subject:
        body_text += "\n" + subject

    return body_text


# ---------------------------------------------------------------------------
# IMAP core
# ---------------------------------------------------------------------------

def _imap_fetch_once(proxy_email, poll_inbox, app_password, max_fetch=5):
    """Single IMAP poll attempt.

    Returns (code_or_None, query_used).
    """
    if not app_password:
        return None, None

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
    except Exception as e:
        print(f"[gmail] IMAP connection failed: {e}")
        return None, None

    try:
        mail.login(poll_inbox, app_password)
    except imaplib.IMAP4.error as e:
        print(f"[gmail] IMAP login failed for {poll_inbox}: {e} (check App Password & IMAP enabled)")
        try:
            mail.logout()
        except Exception:
            pass
        return None, None
    except Exception as e:
        print(f"[gmail] IMAP login error for {poll_inbox}: {e}")
        try:
            mail.logout()
        except Exception:
            pass
        return None, None

    try:
        typ, _ = mail.select("INBOX")
        if typ != "OK":
            print(f"[gmail] IMAP SELECT INBOX failed: {typ}")
            try:
                mail.logout()
            except Exception:
                pass
            return None, None

        # Search strategies in order:
        # 1. HEADER To "<proxy_email>"  (preserved by Cloudflare forwarding)
        # 2. TO "<proxy_email>"
        # 3. HEADER Delivered-To "<proxy_email>"
        # 4. ALL -> manual header filter (fallback)
        search_queries = [
            f'HEADER To "{proxy_email}"',
            f'TO "{proxy_email}"',
            f'HEADER Delivered-To "{proxy_email}"',
        ]

        msg_ids = []
        query_used = None

        for q in search_queries:
            try:
                # IMAP search expects separate args; we pass charset None and query string
                typ, data = mail.search(None, q)
                if typ == "OK" and data and data[0]:
                    ids = data[0].split()
                    if ids:
                        msg_ids = ids
                        query_used = q
                        break
                # empty result -> try next query
            except Exception as e:
                print(f"[gmail] IMAP search failed for query '{q}': {e}")
                continue

        # Fallback: search ALL and manually filter by headers (handles forwarding quirks)
        if not msg_ids:
            try:
                typ, data = mail.search(None, "ALL")
                if typ == "OK" and data and data[0]:
                    all_ids = data[0].split()
                    if all_ids:
                        # Take last 20 most recent for manual filtering
                        msg_ids = all_ids[-20:] if len(all_ids) > 20 else all_ids
                        query_used = "ALL (fallback manual filter)"
            except Exception as e:
                print(f"[gmail] IMAP search ALL failed: {e}")

        if not msg_ids:
            try:
                mail.logout()
            except Exception:
                pass
            return None, query_used

        # Take latest max_fetch, newest first
        latest_ids = msg_ids[-max_fetch:] if len(msg_ids) > max_fetch else msg_ids
        latest_ids = list(reversed(latest_ids))

        for mid in latest_ids:
            try:
                typ, data = mail.fetch(mid, "(RFC822)")
                if typ != "OK" or not data or not data[0]:
                    continue
                # data is like [(b'1 (RFC822 {size}', b'raw_email_bytes'), b')']
                raw = None
                for part in data:
                    if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
                        raw = part[1]
                        break
                if not raw:
                    continue
                msg = email.message_from_bytes(raw, policy=email.policy.default)
                # If we used ALL fallback, ensure header matches before OTP extraction
                if query_used and query_used.startswith("ALL"):
                    to_hdr = str(msg.get("To", "") or "")
                    delivered = str(msg.get("Delivered-To", "") or "")
                    env_to = str(msg.get("X-Envelope-To", "") or "")
                    x_gm = str(msg.get("X-Gm-Original-To", "") or "")
                    combined = f"{to_hdr} {delivered} {env_to} {x_gm}".lower()
                    if proxy_email.lower() not in combined:
                        # Still check body for proxy_email? but skip if not recipient
                        # For debugging, we could log
                        continue

                body_text = _extract_text_from_email(msg)
                # Also include raw To headers in text for OTP? not needed
                code = extract_otp(body_text)
                if code:
                    print(f"[gmail] OTP found for {proxy_email} (query={query_used} id={mid.decode() if isinstance(mid, bytes) else mid}): {code}")
                    try:
                        mail.logout()
                    except Exception:
                        pass
                    return code, query_used
            except Exception as e:
                print(f"[gmail] IMAP fetch/decode error for id {mid}: {e}")
                continue

        try:
            mail.logout()
        except Exception:
            pass
        return None, query_used

    except Exception as e:
        print(f"[gmail] IMAP error during fetch: {e}")
        try:
            mail.logout()
        except Exception:
            pass
        return None, None


# ---------------------------------------------------------------------------
# Public: fetch_otp
# ---------------------------------------------------------------------------

def fetch_otp(proxy_email, poll_inbox, timeout=60, poll_interval=3):
    """Poll Gmail for OTP sent to proxy_email.

    Primary: IMAP App Password.
    Fallback: Gmail API OAuth if IMAP password not configured or IMAP repeatedly fails.

    Args:
        proxy_email: recipient address OTP was sent to (e.g., junchun@cultinet.site)
        poll_inbox: inbox to poll (e.g., faxcheck2@gmail.com)
        timeout: max seconds to poll
        poll_interval: seconds between polls

    Returns:
        str 6-digit code or None if not found within timeout.
    """
    proxy_email = (proxy_email or "").strip()
    poll_inbox = (poll_inbox or "").strip()
    if not proxy_email or not poll_inbox:
        print(f"[gmail] fetch_otp missing proxy_email or poll_inbox: proxy={proxy_email} poll={poll_inbox}")
        return None

    # Try IMAP first if password available
    app_password = _get_app_password(poll_inbox)

    # If no password found for this poll_inbox, try faxcheck2 fallback (single inbox migration)
    if not app_password and poll_inbox.lower() != "faxcheck2@gmail.com":
        fallback = _get_app_password("faxcheck2@gmail.com")
        if fallback:
            print(f"[gmail] No App Password for {poll_inbox}, falling back to faxcheck2@gmail.com password")
            app_password = fallback
            # For IMAP login we still need to login as poll_inbox; if that fails because
            # poll_inbox is direct but we used faxcheck2 password, login will fail and we fallback to API.
            # Instead, switch poll_inbox to faxcheck2 for search if proxy is forward type (all now forward).
            # Detect: if direct type but password missing, try faxcheck2 inbox directly.
            # The utils/otp routing says all forwards poll_inbox=faxcheck2, so this case rare.

    if app_password:
        print(f"[gmail] IMAP polling for {proxy_email} via {poll_inbox} (timeout={timeout}s)")
        end_time = time.time() + timeout
        attempt = 0
        last_query = None
        imap_login_failed = False

        while time.time() < end_time:
            attempt += 1
            code, q = _imap_fetch_once(proxy_email, poll_inbox, app_password, max_fetch=5)
            if q:
                last_query = q
            if code:
                return code
            # Detect login failure to avoid pointless polling: if _imap_fetch_once returns None,None due to login fail,
            # we should break to fallback. We can't distinguish empty inbox vs login fail without more signals.
            # Simple heuristic: if app_password set but _imap_fetch_once repeatedly returns None,None, treat as login fail after 1 attempt.
            # Our _imap_fetch_once returns (None, None) only on login/connection failure. Empty inbox returns (None, query).
            if code is None and q is None:
                imap_login_failed = True
                print("[gmail] IMAP login failed, will try Gmail API fallback if available")
                break
            remaining = end_time - time.time()
            if remaining <= 0:
                break
            # Log periodic
            if attempt == 1 or attempt % 5 == 0:
                print(f"[gmail] No OTP yet for {proxy_email} (attempt {attempt}, last_query={last_query}), waiting {poll_interval}s ...")
            time.sleep(min(poll_interval, remaining))

        if imap_login_failed:
            pass  # fall through to Gmail API fallback
        else:
            print(f"[gmail] IMAP fetch_otp timed out for {proxy_email} after {timeout}s (last_query={last_query}) - no OTP found")
            # Don't fallback to Gmail API if IMAP succeeded but just no mail? Could fallback but IMAP was correct path.
            # Still try fallback if OAuth is configured, as secondary chance.
            # But to avoid duplicate timeout, only fallback if we want exhaustive search.
            # We'll attempt fallback if credentials exist.

    else:
        print(f"[gmail] No App Password found for {poll_inbox} (checked env GMAIL_FAXCHECK2_APP_PASSWORD / GMAIL_APP_PASSWORD / file data/gmail_app_password.txt)")
        print("[gmail] Trying Gmail API OAuth fallback if configured...")

    # -------------------------------------------------------------------
    # Fallback: Gmail API OAuth (if IMAP not configured or failed)
    # -------------------------------------------------------------------
    try:
        # Only attempt if credentials file exists to avoid noisy error
        creds_path = _get_credentials_path()
        if not creds_path.exists():
            if not app_password:
                print(f"[gmail] Gmail API fallback skipped: credentials not found at {creds_path}")
            return None
        # Attempt Gmail API fetch
        from googleapiclient.errors import HttpError
    except ImportError:
        HttpError = Exception

    try:
        service = get_gmail_service(poll_inbox)
    except Exception as e:
        print(f"[gmail] Gmail API fallback get_gmail_service failed: {e}")
        return None

    print(f"[gmail] Gmail API fallback polling for {proxy_email} via {poll_inbox} (timeout={timeout}s)")
    end_time = time.time() + timeout
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
                for msg_meta in messages:
                    msg_id = msg_meta.get("id")
                    if not msg_id:
                        continue
                    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
                    body = _decode_gmail_api_body(msg)
                    code = extract_otp(body)
                    if code:
                        try:
                            service.users().messages().modify(
                                userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}
                            ).execute()
                        except Exception:
                            pass
                        print(f"[gmail] OTP found via Gmail API for {proxy_email} (query={q}): {code}")
                        return code
                    snippet = msg.get("snippet", "") or ""
                    code_snip = extract_otp(snippet)
                    if code_snip:
                        try:
                            service.users().messages().modify(
                                userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}
                            ).execute()
                        except Exception:
                            pass
                        print(f"[gmail] OTP found in snippet via Gmail API for {proxy_email}: {code_snip}")
                        return code_snip
            except HttpError as e:
                last_error = e
                print(f"[gmail] Gmail API error for query '{q}': {e}")
                continue
            except Exception as e:
                last_error = e
                print(f"[gmail] Gmail API error for query '{q}': {e}")
                continue
        remaining = end_time - time.time()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))

    if last_error:
        print(f"[gmail] Gmail API fallback timed out for {proxy_email} after {timeout}s (last error: {last_error})")
    else:
        print(f"[gmail] Gmail API fallback timed out for {proxy_email} after {timeout}s - no OTP found")
    return None


def list_recent_messages(poll_inbox, proxy_email=None, max_results=5):
    """List recent messages via IMAP (primary) or Gmail API fallback.

    Returns list of dicts with keys id, subject, snippet-like preview.
    For IMAP mode, id is IMAP UID bytes decoded.
    """
    app_password = _get_app_password(poll_inbox)
    if app_password:
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(poll_inbox, app_password)
            mail.select("INBOX")
            # Search
            if proxy_email:
                typ, data = mail.search(None, f'HEADER To "{proxy_email}"')
                if typ != "OK" or not data or not data[0]:
                    typ, data = mail.search(None, "ALL")
            else:
                typ, data = mail.search(None, "ALL")
            if typ == "OK" and data and data[0]:
                ids = data[0].split()
                ids = list(reversed(ids[-max_results:]))
                result = []
                for mid in ids:
                    try:
                        typ2, d2 = mail.fetch(mid, "(RFC822)")
                        if typ2 != "OK":
                            continue
                        raw = None
                        for part in d2:
                            if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
                                raw = part[1]
                                break
                        if not raw:
                            continue
                        msg = email.message_from_bytes(raw, policy=email.policy.default)
                        subject = str(msg.get("Subject", "") or "")
                        body = _extract_text_from_email(msg)[:200]
                        result.append({"id": mid.decode() if isinstance(mid, bytes) else str(mid), "subject": subject, "snippet": body[:120]})
                    except Exception:
                        continue
                try:
                    mail.logout()
                except Exception:
                    pass
                return result
            try:
                mail.logout()
            except Exception:
                pass
        except Exception as e:
            print(f"[gmail] list_recent_messages IMAP failed: {e}")

    # Fallback to Gmail API
    try:
        service = get_gmail_service(poll_inbox)
        q = f"to:{proxy_email}" if proxy_email else ""
        results = service.users().messages().list(userId="me", q=q, maxResults=max_results).execute()
        return results.get("messages", []) or []
    except Exception as e:
        print(f"[gmail] list_recent_messages fallback failed: {e}")
        return []

