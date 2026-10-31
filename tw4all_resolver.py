"""
tw4all_resolver.py - ToonWorld4All Redirect Link Resolver & Local Sync Bridge

Takes any archive.toonworld4all.me/redirect/<id> link and resolves it
to the real file host (https://new4.gdflix.io/file/..., Filepress, MEGA)
using the 24h verification cookie.

Includes a background HTTP bridge server (port 4545) so the Tampermonkey script
running in your browser can automatically sync the 24h cookie to AutoPoster
without you ever having to copy-paste hex tokens manually!
"""

import sys
import os
import re
import json
import logging
import threading
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("tw4all_resolver")

# Settings file reference
SETTINGS_FILE = Path(__file__).resolve().parent / "settings.json"

DEFAULT_COOKIE = ""

# In-memory cookie cache
_current_cookie: str = ""
_bridge_server_started = False


def extract_cookie_from_verify_url(verify_url: str) -> str:
    """Fetch a /verify/<token> URL with no-redirect and extract user=<hex> from Set-Cookie."""
    verify_url = verify_url.strip()
    if not verify_url or "/verify/" not in verify_url:
        return ""
    try:
        opener = urllib.request.build_opener(_NoRedirectHandler)
        req = urllib.request.Request(
            verify_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        sc = ""
        try:
            resp = opener.open(req, timeout=12)
            sc = resp.headers.get("Set-Cookie", "")
        except urllib.error.HTTPError as he:
            sc = he.headers.get("Set-Cookie", "")
        m = re.search(r'user=([0-9a-fA-F]+)', sc)
        if m:
            cookie_val = m.group(1)
            logger.info("Extracted cookie from verify URL (%d chars)", len(cookie_val))
            return cookie_val
    except Exception as ex:
        logger.warning("Failed to extract cookie from verify url: %s", ex)
    return ""


def load_saved_cookie() -> str:
    """Load tw4all_cookie from settings.json or in-memory cache, converting verify URL if needed."""
    global _current_cookie
    if _current_cookie and not _current_cookie.startswith("http"):
        return _current_cookie
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                cookie = data.get("tw4all_cookie", "").strip()
                if cookie:
                    if "/verify/" in cookie or cookie.startswith("http"):
                        extracted = extract_cookie_from_verify_url(cookie)
                        if extracted:
                            save_synced_cookie(extracted)
                            return extracted
                    _current_cookie = cookie
                    return cookie
        except Exception:
            pass
    return DEFAULT_COOKIE


def save_synced_cookie(cookie: str):
    """Save synced cookie to memory and settings.json (auto-extracting if a verify URL is passed)."""
    global _current_cookie
    cookie = cookie.strip()
    if not cookie:
        return
    if "/verify/" in cookie or cookie.startswith("http"):
        extracted = extract_cookie_from_verify_url(cookie)
        if extracted:
            cookie = extracted
    _current_cookie = cookie
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["tw4all_cookie"] = cookie
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            logger.info("Successfully updated tw4all_cookie in settings.json (%d chars)", len(cookie))
        except Exception as e:
            logger.error("Failed to save synced cookie to settings.json: %s", e)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def resolve_redirect_link(
    redirect_url: str,
    cookie_value: Optional[str] = None
) -> Tuple[Optional[str], str]:
    """
    Resolves an archive.toonworld4all.me/redirect/<id> link into its final
    direct download link (e.g. https://new4.gdflix.io/file/..., Filepress, MEGA).

    Follows the real authenticated 302 redirect chain using the 24h verification cookie.
    """
    redirect_url = redirect_url.strip()
    if not redirect_url or "/redirect/" not in redirect_url:
        return None, "Invalid URL (must contain /redirect/)"

    cookie = cookie_value.strip() if cookie_value else load_saved_cookie()
    if cookie and ("/verify/" in cookie or cookie.startswith("http")):
        extracted = extract_cookie_from_verify_url(cookie)
        if extracted:
            cookie = extracted
            save_synced_cookie(extracted)

    if not cookie:
        return None, (
            "No ToonWorld4All cookie found! "
            "Please solve the shortener once in your browser. "
            "The Tampermonkey script will auto-sync it to AutoPoster."
        )

    cookie_header = f"user={cookie}"
    opener = urllib.request.build_opener(_NoRedirectHandler)

    req1 = urllib.request.Request(
        redirect_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Cookie": cookie_header,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://toonworld4all.me/",
        }
    )

    try:
        resp1 = opener.open(req1, timeout=15)
        loc1 = resp1.headers.get("Location")
    except urllib.error.HTTPError as e:
        loc1 = e.headers.get("Location")
    except Exception as ex:
        return None, f"Network error on step 1: {ex}"

    if not loc1:
        return None, (
            "No redirect returned. Your 24-hour verification cookie may have expired. "
            "Please solve the shortener again in your browser."
        )

    # If location is intermediate /verify/<token> endpoint:
    if "/verify/" in loc1:
        if not loc1.startswith("http"):
            loc1 = "https://archive.toonworld4all.me" + loc1

        req2 = urllib.request.Request(
            loc1,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Cookie": cookie_header,
            }
        )
        try:
            resp2 = opener.open(req2, timeout=15)
            final_loc = resp2.headers.get("Location")
        except urllib.error.HTTPError as e2:
            final_loc = e2.headers.get("Location")
        except Exception as ex2:
            return None, f"Network error on step 2: {ex2}"

        if final_loc:
            return final_loc, "OK"
        return None, "Could not determine final target from /verify/ endpoint"

    return loc1, "OK"


# ──────────────────────────────────────────────────────────────────────────────
# Local Sync Bridge HTTP Server (port 4545)
# ──────────────────────────────────────────────────────────────────────────────

class _BridgeHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress noisy standard HTTP access logs
        pass

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/api/status"):
            cookie = load_saved_cookie()
            data = {
                "status": "ok",
                "has_cookie": bool(cookie),
                "cookie_len": len(cookie),
            }
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self._send_cors_headers()
            self.end_headers()

    def do_POST(self):
        if self.path.startswith("/api/set_tw_cookie"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw_body = self.rfile.read(length).decode("utf-8")
                payload = json.loads(raw_body)
                cookie_val = payload.get("cookie", "").strip()
                verify_url = payload.get("verify_url", "").strip()

                if verify_url and "/verify/" in verify_url:
                    logger.info("Received verify_url: %s. Fetching session cookie...", verify_url[:70])
                    try:
                        opener = urllib.request.build_opener(_NoRedirectHandler)
                        v_req = urllib.request.Request(
                            verify_url,
                            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                        )
                        v_sc = ""
                        try:
                            v_resp = opener.open(v_req, timeout=12)
                            v_sc = v_resp.headers.get("Set-Cookie", "")
                        except urllib.error.HTTPError as he:
                            v_sc = he.headers.get("Set-Cookie", "")
                        
                        import re
                        m = re.search(r'user=([0-9a-fA-F]+)', v_sc)
                        if m:
                            cookie_val = m.group(1)
                            logger.info("Successfully extracted 24h user cookie from verify_url!")
                    except Exception as ve:
                        logger.warning("Failed to extract cookie from verify_url: %s", ve)

                if cookie_val:
                    save_synced_cookie(cookie_val)
                    resp = {"status": "ok", "message": f"Cookie synced ({len(cookie_val)} chars)"}
                    status_code = 200
                else:
                    resp = {"status": "error", "message": "Empty cookie or could not extract from verify_url"}
                    status_code = 400
            except Exception as ex:
                resp = {"status": "error", "message": str(ex)}
                status_code = 500

            body = json.dumps(resp).encode("utf-8")
            self.send_response(status_code)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self._send_cors_headers()
            self.end_headers()


def start_cookie_bridge_server(port: int = 4545) -> bool:
    """Start local background HTTP bridge server on 127.0.0.1:port."""
    global _bridge_server_started
    if _bridge_server_started:
        return True

    def _serve():
        try:
            server = HTTPServer(("127.0.0.1", port), _BridgeHandler)
            logger.info("ToonWorld cookie bridge listening on http://127.0.0.1:%d", port)
            server.serve_forever()
        except OSError as e:
            logger.warning("Cookie bridge port %d in use or unavailable: %s", port, e)
        except Exception as e:
            logger.error("Cookie bridge server error: %s", e)

    t = threading.Thread(target=_serve, daemon=True, name="TW4AllBridge")
    t.start()
    _bridge_server_started = True
    return True


if __name__ == "__main__":
    start_cookie_bridge_server(4545)
    print("=" * 65)
    print(" ToonWorld4All Redirect Link Resolver & Bridge")
    print("=" * 65)
    cookie = load_saved_cookie()
    if cookie:
        print(f"[*] Active cookie loaded from settings ({len(cookie)} chars)")
    else:
        print("[!] No cookie in settings.json. Waiting for Tampermonkey auto-sync on http://127.0.0.1:4545 ...")

    if len(sys.argv) > 1:
        res, msg = resolve_redirect_link(sys.argv[1])
        if res:
            print(f"[OK] Direct Link: {res}")
        else:
            print(f"[ERROR] {msg}")
    else:
        print("\nEnter /redirect/... URLs to resolve (Ctrl+C to quit):\n")
        try:
            while True:
                line = input("URL > ").strip()
                if not line:
                    continue
                res, msg = resolve_redirect_link(line)
                if res:
                    print(f" -> {res}\n")
                else:
                    print(f" [X] {msg}\n")
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
