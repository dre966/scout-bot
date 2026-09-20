#!/usr/bin/env python3
"""One-time Gmail OAuth authorization.

Usage:
    python tools/gmail_auth.py --poll-inbox faxcheck2@gmail.com
    python tools/gmail_auth.py --poll-inbox faxcheck2@gmail.com --credentials data/gmail_credentials.json

Opens browser flow (or prints auth URL) to get refresh token and saves to data/gmail_token.json
Supports both Docker (/app/data) and local (data/) paths.

If running headless (no browser), it prints the auth URL and prompts for code.
"""

import argparse
import json
import os
import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Resolve paths
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CREDENTIALS = ROOT / "data" / "gmail_credentials.json"
DEFAULT_TOKEN_DOCKER = Path("/app/data/gmail_token.json")
DEFAULT_TOKEN_LOCAL = ROOT / "data" / "gmail_token.json"


def get_credentials_path(arg_path=None):
    if arg_path:
        return Path(arg_path)
    # Check docker path first if /app exists
    if Path("/app").exists() and (Path("/app/data/gmail_credentials.json").exists()):
        return Path("/app/data/gmail_credentials.json")
    if DEFAULT_CREDENTIALS.exists():
        return DEFAULT_CREDENTIALS
    # fallback to docker default if exists else local
    if Path("/app").exists():
        return Path("/app/data/gmail_credentials.json")
    return DEFAULT_CREDENTIALS


def get_token_path(poll_inbox=None, arg_path=None):
    if arg_path:
        return Path(arg_path)
    # Per-inbox token support? Keep main token as spec, but also check env
    env_path = os.getenv("GMAIL_TOKEN_PATH")
    if env_path:
        return Path(env_path)
    if Path("/app").exists():
        # if token already exists in /app/data use it
        if DEFAULT_TOKEN_DOCKER.exists():
            return DEFAULT_TOKEN_DOCKER
        # also check per-inbox variant
        if poll_inbox:
            safe = poll_inbox.replace("@", "_at_").replace(".", "_")
            per = Path(f"/app/data/gmail_token_{safe}.json")
            # still default to main
            return DEFAULT_TOKEN_DOCKER
        return DEFAULT_TOKEN_DOCKER
    return DEFAULT_TOKEN_LOCAL


def main():
    parser = argparse.ArgumentParser(description="Authorize Gmail API for OTP fetching")
    parser.add_argument("--poll-inbox", default="faxcheck2@gmail.com", help="Gmail address to authorize (e.g., faxcheck2@gmail.com)")
    parser.add_argument("--credentials", default=None, help="Path to gmail_credentials.json")
    parser.add_argument("--token", default=None, help="Path to save gmail_token.json")
    parser.add_argument("--no-browser", action="store_true", help="Do not open browser, print URL instead")
    parser.add_argument("--port", type=int, default=0, help="Port for local server (0=auto)")

    args = parser.parse_args()

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
    except ImportError:
        print("Missing google auth dependencies. Install with:")
        print("  pip install google-api-python-client google-auth google-auth-oauthlib")
        sys.exit(1)

    creds_path = get_credentials_path(args.credentials)
    token_path = get_token_path(args.poll_inbox, args.token)

    if not creds_path.exists():
        print(f"[error] Credentials file not found at {creds_path}")
        print("Create it from the provided web client JSON. Example:")
        print(f"  cp data/gmail_credentials.json {creds_path}")
        sys.exit(1)

    print(f"[info] Using credentials: {creds_path}")
    print(f"[info] Token will be saved to: {token_path}")
    print(f"[info] Poll inbox: {args.poll_inbox}")
    print(f"[info] Scope: {SCOPES}")

    # Check if token already exists and is valid
    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            if creds and creds.valid:
                print(f"[ok] Existing token is still valid for {token_path}. No re-auth needed.")
                # Verify it can refresh
                return 0
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    print("[ok] Token refreshed successfully.")
                    token_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(token_path, "w") as f:
                        f.write(creds.to_json())
                    return 0
                except Exception as e:
                    print(f"[warn] Refresh failed: {e}, continuing to full auth flow...")
                    creds = None
        except Exception as e:
            print(f"[warn] Could not load existing token: {e}")

    # Need to run OAuth flow
    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)

    # Use run_local_server if browser available, else console
    if args.no_browser:
        # Use console flow: print URL, ask for code
        flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
        auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
        print("\n" + "="*70)
        print("Open this URL in your browser and authorize:")
        print(auth_url)
        print("="*70 + "\n")
        code = input("Enter the authorization code: ").strip()
        flow.fetch_token(code=code)
        creds = flow.credentials
    else:
        try:
            # Try local server flow (opens browser)
            creds = flow.run_local_server(port=args.port, prompt="consent", access_type="offline")
        except Exception as e:
            print(f"[warn] Local server flow failed ({e}), falling back to console flow...")
            flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
            auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
            print("\n" + "="*70)
            print("Open this URL in your browser and authorize:")
            print(auth_url)
            print("="*70 + "\n")
            code = input("Enter the authorization code: ").strip()
            flow.fetch_token(code=code)
            creds = flow.credentials

    # Save token
    token_path.parent.mkdir(parents=True, exist_ok=True)
    with open(token_path, "w") as f:
        f.write(creds.to_json())

    print(f"[ok] Token saved to {token_path}")
    print(f"[ok] Authorized for poll_inbox={args.poll_inbox}")

    # Fix permissions for Docker volume
    try:
        os.chmod(token_path, 0o600)
    except Exception:
        pass

    # Also save to alternate location for convenience (both /app/data and local)
    alternate = DEFAULT_TOKEN_LOCAL if token_path == DEFAULT_TOKEN_DOCKER else DEFAULT_TOKEN_DOCKER
    try:
        if not alternate.exists() and token_path.exists():
            alternate.parent.mkdir(parents=True, exist_ok=True)
            with open(token_path, "r") as src, open(alternate, "w") as dst:
                dst.write(src.read())
            print(f"[info] Also copied token to {alternate}")
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
