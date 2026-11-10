"""
stream_extractor.py — Multi-source Stream Extractor & Encrypter
Supports:
- Ruby Server (Toonstream: toonstream.dad / toonstream.vip / toonstream.site)
- Server 2 (Multimovies via WP AJAX → iqsmartgames myseriesapi + embedhelper2.php → smoothpre/hanerix)
"""

import re
import base64
import json
import logging
import threading
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, quote as _url_quote
from typing import Optional, List, Dict, Tuple


def _encode_url_path(url: str) -> str:
    """
    Percent-encode special characters (apostrophes, spaces, curly quotes, etc.)
    in the PATH portion of a URL, while leaving the scheme, host, and query intact.
    This fixes episode URLs like .../jojo's-bizarre-adventure-4x25/ where the
    apostrophe is literal and some servers reject un-encoded requests.
    """
    try:
        parsed = urlparse(url)
        safe_chars = "/-._~!$&()*+,;=:@%"  # chars legal in a URL path, don't re-encode %
        encoded_path = _url_quote(parsed.path, safe=safe_chars)
        return parsed._replace(path=encoded_path).geturl()
    except Exception:
        return url

logger = logging.getLogger(__name__)

WORKER_BASE = "https://new-frosty.toonsraja-df3.workers.dev/"
DORABASH_WORKER_URL = "https://new-frosty.nsr-developer78.workers.dev/"
USE_WATCHANIMEWORLD = False
SERVER_UG_SOURCE = "toonstream"
ANIMEDEKHO_URL = "https://animedekho.tv"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}
IQ_HEADERS = {
    **HEADERS,
    "Referer": "https://streams.iqsmartgames.com/",
    "Content-Type": "application/x-www-form-urlencoded",
}
HELPER_HEADERS = {
    **HEADERS,
    "Referer": "https://pro.iqsmartgames.com/",
    "Content-Type": "application/x-www-form-urlencoded",
}

# Load domains dynamically from settings.json if present
from pathlib import Path
_settings_file = Path(__file__).parent / "settings.json"
_toon_list = ["toonstream.vip", "toon-stream.site"]
_multi_list = ["m1.multimovies.courses", "multimovies.garden", "multimovies.casa", "multimovies.beer"]

if _settings_file.exists():
    try:
        with open(_settings_file, encoding="utf-8") as f:
            _s = json.load(f)

        def _clean_domain(d: str) -> str:
            d = d.strip().rstrip("/")
            if d.startswith("https://"):
                d = d[8:]
            elif d.startswith("http://"):
                d = d[7:]
            return d

        if "toonstream_domains" in _s and _s["toonstream_domains"].strip():
            _toon_list = [_clean_domain(d) for d in _s["toonstream_domains"].split(",") if d.strip()]
        if "multimovies_domains" in _s and _s["multimovies_domains"].strip():
            _multi_list = [_clean_domain(d) for d in _s["multimovies_domains"].split(",") if d.strip()]
        if "cf_clearance" in _s and _s["cf_clearance"].strip():
            HEADERS["Cookie"] = f"cf_clearance={_s['cf_clearance'].strip()}"
        if "cf_user_agent" in _s and _s["cf_user_agent"].strip():
            HEADERS["User-Agent"] = _s["cf_user_agent"].strip()
        if "worker_base" in _s and _s["worker_base"].strip():
            WORKER_BASE = _s["worker_base"].strip().rstrip("/") + "/"
        if "dorabash_worker_url" in _s and _s["dorabash_worker_url"].strip():
            DORABASH_WORKER_URL = _s["dorabash_worker_url"].strip().rstrip("/") + "/"
        USE_WATCHANIMEWORLD = bool(_s.get("use_watchanimeworld", False))
        if "server_ug_source" in _s and _s["server_ug_source"].strip():
            SERVER_UG_SOURCE = str(_s["server_ug_source"]).strip().lower()
        if "animedekho_url" in _s and _s["animedekho_url"].strip():
            ANIMEDEKHO_URL = str(_s["animedekho_url"]).strip().rstrip("/")
    except Exception:
        pass

TOONSTREAM_DOMAINS = _toon_list
MULTIMOVIES_DOMAINS = _multi_list

# ── CloudStream domain auto-sync ──────────────────────────────────────────────
# The phisher98 CloudStream extensions (Toonstream.cs3, MultiMoviesProvider.cs3)
# both fetch their live domains at runtime from this JSON file.
# We use the exact same source so our domain list always matches theirs.
_CLOUDSTREAM_DOMAINS_URL = (
    "https://raw.githubusercontent.com/phisher98/TVVVV/refs/heads/main/domains.json"
)


def sync_domains_from_cloudstream(save_to_settings: bool = True, timeout: int = 10) -> dict:
    """
    Fetch the live domain list from phisher98's TVVVV/domains.json — the same
    source used by the Toonstream and MultiMoviesProvider CloudStream extensions.

    Returns a dict with keys 'toonstream' and 'multimovies' containing the
    new primary domain strings (e.g. 'toonstream.us'), or empty strings on failure.

    If save_to_settings=True (default), also persists the updated domains into
    settings.json and reloads TOONSTREAM_DOMAINS / MULTIMOVIES_DOMAINS in-process.
    """
    global TOONSTREAM_DOMAINS, MULTIMOVIES_DOMAINS

    result = {"toonstream": "", "multimovies": ""}
    try:
        resp = requests.get(_CLOUDSTREAM_DOMAINS_URL, timeout=timeout)
        resp.raise_for_status()
        data: dict = resp.json()
    except Exception as e:
        logger.warning("sync_domains_from_cloudstream: failed to fetch domains.json: %s", e)
        return result

    def _strip(url: str) -> str:
        """Remove scheme so it matches the domain-list format used internally."""
        url = url.strip().rstrip("/")
        for prefix in ("https://", "http://"):
            if url.startswith(prefix):
                return url[len(prefix):]
        return url

    toon_url   = data.get("toonstream", "")
    multi_url  = data.get("MultiMovies", "")

    if not toon_url or not multi_url:
        logger.warning("sync_domains_from_cloudstream: missing keys in domains.json: %s", list(data.keys()))
        return result

    toon_domain  = _strip(toon_url)
    multi_domain = _strip(multi_url)

    result["toonstream"]  = toon_domain
    result["multimovies"] = multi_domain

    # Update in-process domain lists so current run immediately uses new domains
    if toon_domain and toon_domain not in TOONSTREAM_DOMAINS:
        TOONSTREAM_DOMAINS = [toon_domain] + [d for d in TOONSTREAM_DOMAINS if d != toon_domain]
        logger.info("sync_domains_from_cloudstream: Toonstream primary → %s", toon_domain)
    if multi_domain and multi_domain not in MULTIMOVIES_DOMAINS:
        MULTIMOVIES_DOMAINS = [multi_domain] + [d for d in MULTIMOVIES_DOMAINS if d != multi_domain]
        logger.info("sync_domains_from_cloudstream: MultiMovies primary → %s", multi_domain)

    # Persist to settings.json so next startup picks up the new domains
    if save_to_settings and _settings_file.exists():
        try:
            with open(_settings_file, encoding="utf-8") as f:
                _cfg = json.load(f)
            _cfg["toonstream_domains"]  = ", ".join(TOONSTREAM_DOMAINS)
            _cfg["multimovies_domains"] = ", ".join(MULTIMOVIES_DOMAINS)
            with open(_settings_file, "w", encoding="utf-8") as f:
                json.dump(_cfg, f, indent=2)
            logger.info(
                "sync_domains_from_cloudstream: settings.json updated — "
                "toonstream=%s  multimovies=%s", toon_domain, multi_domain
            )
        except Exception as e:
            logger.warning("sync_domains_from_cloudstream: could not save settings.json: %s", e)

    return result

# ── FlareSolverr integration ──────────────────────────────────────────────
# FlareSolverr is a local proxy that uses a real browser to bypass Cloudflare.
# Download from: https://github.com/FlareSolverr/FlareSolverr/releases
# Set flaresolverr_url in settings to e.g. "http://localhost:8191"
FLARESOLVERR_URL: str = ""
if _settings_file.exists():
    try:
        with open(_settings_file, encoding="utf-8") as _fsf:
            _fss = json.load(_fsf)
        _fsu = _fss.get("flaresolverr_url", "").strip().rstrip("/")
        if _fsu:
            FLARESOLVERR_URL = _fsu
    except Exception:
        pass


_flaresolverr_lock = threading.Lock()

def _flaresolverr_get(url: str, timeout: int = 40) -> Optional[str]:
    """
    Fetch a Cloudflare-protected URL via a locally-running FlareSolverr instance.
    Uses a Lock to ensure only 1 request reaches FlareSolverr at a time (prevents queue buildup).
    Also updates HEADERS with fresh cf_clearance cookie and User-Agent when solved!
    """
    if not FLARESOLVERR_URL:
        return None
    with _flaresolverr_lock:
        try:
            payload = {
                "cmd": "request.get",
                "url": url,
                "maxTimeout": timeout * 1000,
            }
            resp = requests.post(
                f"{FLARESOLVERR_URL}/v1",
                json=payload,
                timeout=timeout + 10,
            )
            if resp.status_code == 200:
                data = resp.json()
                status = data.get("status", "")
                solution = data.get("solution", {})
                html = solution.get("response", "")
                if status == "ok" and html:
                    logger.info("FlareSolverr: successfully fetched %s", url)
                    ua = solution.get("userAgent", "")
                    cookies = solution.get("cookies", [])
                    for c in cookies:
                        if c.get("name") == "cf_clearance":
                            HEADERS["Cookie"] = f"cf_clearance={c.get('value')}"
                            if ua:
                                HEADERS["User-Agent"] = ua
                            break
                    return html
                else:
                    logger.warning("FlareSolverr returned status=%s for %s", status, url)
            else:
                logger.warning("FlareSolverr HTTP %s for %s", resp.status_code, url)
        except Exception as exc:
            logger.warning("FlareSolverr error for %s: %s", url, exc)
    return None


# ── Quality / Edition / Language tag stripper regex ──────────────────────
# Strip tags that appear in scraped post titles but never in Toonstream or Multimovies URLs
_STRIP_TAGS = re.compile(
    r'\b(bl?u[\-\s]?ray|bdrip|brrip|webrip|web[\-\s]?dl|hdrip|dvdrip|dvd'  # video sources (BuRay, BluRay)
    r'|4k|uhd|fhd|hd'                                                        # quality tags
    r'|hindi|english|japanese|tamil|telugu|korean|chinese|french|spanish'   # languages
    r'|multi[\s\-]?audio|dual[\s\-]?audio|dubbed|subbed|sub|dub'            # audio tags
    r'|uncensored|censored|remastered|extended|uncut'                        # edition tags
    r'|episodes?|all[\s\-]?episodes|batch'                                   # episode tags
    r')\b',
    re.IGNORECASE,
)

# ── Domain health cache (session-level) ──────────────────────────────────
# Maps domain -> False once it has been confirmed unreachable in this session.
_domain_dead: dict[str, bool] = {}

def _domain_ok(domain: str) -> bool:
    """Return True if domain has not been blacklisted this session."""
    return not _domain_dead.get(domain, False)

def _mark_dead(domain: str):
    """Mark a domain as unreachable so it is skipped for the rest of the session."""
    if not _domain_dead.get(domain):
        logger.warning("Toonstream domain '%s' marked unreachable for this session.", domain)
        _domain_dead[domain] = True


# ─────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────

def abbreviate_title(series_name: str) -> str:
    """Convert series name to abbreviation e.g. 'Tamon's B-Side' -> 'TBS'."""
    stop = {'of', 'the', 'a', 'an', 'and', 'in', 'on', 'to', 'for', 'at', 'by', 'with'}
    words = re.sub(r'[^a-zA-Z0-9\s]', '', series_name).split()
    abbr = ''.join(w[0].upper() for w in words if w.lower() not in stop and w)
    return abbr if abbr else series_name[:4].upper()


def encrypt_iframe_url(title: str, iframe_url: str, delay: str = "30") -> str:
    """Encrypt iframe URL into base64 payload for Cloudflare worker."""
    if not iframe_url:
        return ""
    if "?data=" in iframe_url or "blogspot." in iframe_url.lower():
        return iframe_url
    title_underscored = re.sub(r'\s+', '_', title.strip())
    payload = {"t": title_underscored, "u": iframe_url, "d": str(delay)}
    encoded = base64.b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode()
    wb = WORKER_BASE.rstrip('/') + '/'
    return f"{wb}?data={encoded}"


def convert_ruby_url(url: str) -> Optional[str]:
    """
    Convert / validate stream iframe URL (supporting rubyvidhub.com, rubystm.com, and as-cdn servers).
    Extracts slug from /e/{slug}, /d/{slug}, /embed-{slug}, or /{slug} and converts to:
    https://rubyvidhub.com/embed-{slug}.html?streamrurucy=true
    """
    if not url:
        return None
    url = url.strip()
    if "as-cdn" in url or "vexal.top" in url or "ravok.buzz" in url:
        return url
    if "rubyvidhub.com" in url and "streamrurucy=true" in url:
        return url
    if "rubystm.com" not in url and "rubyvidhub.com" not in url and "ruby" not in url.lower():
        return None

    # Extract slug from /e/{slug}, /d/{slug}, /embed-{slug}[.html], or /{slug}
    slug = None
    m = re.search(r'/(?:e|d|embed|v)/([a-zA-Z0-9]+)', url)
    if m:
        slug = m.group(1).split('.')[0]
    else:
        m = re.search(r'/embed-([a-zA-Z0-9]+)', url)
        if m:
            slug = m.group(1).split('.')[0]
        else:
            # bare path: strip hostname and grab first path segment
            path = urlparse(url).path.strip("/")
            parts = [p.split(".")[0] for p in path.split("/") if p]
            for part in parts:
                if re.fullmatch(r'[a-zA-Z0-9]{4,}', part) and part.lower() not in ("embed", "video", "watch"):
                    slug = part
                    break
    if slug:
        return f"https://rubyvidhub.com/embed-{slug}.html?streamrurucy=true"
    return url


def extract_toonstream_rubystm_for_episode(ep_url: str, session: Optional[requests.Session] = None) -> Optional[str]:
    """
    Fetch a Toonstream episode page and extract the rubystm/rubyvidhub stream URL,
    converting it into https://rubyvidhub.com/embed-{slug}.html?streamrurucy=true format.
    Validates candidates using is_ruby_alive() so expired or deleted links are ignored
    when multiple options exist on the episode page.
    """
    s = session or requests
    try:
        r = s.get(ep_url, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            return None

        candidates: list[str] = []

        # 1. Option divs and iframes
        soup = BeautifulSoup(r.text, 'html.parser')
        domain = urlparse(ep_url).netloc or (TOONSTREAM_DOMAINS[0] if TOONSTREAM_DOMAINS else 'toon-stream.site')
        for opt in soup.find_all("div", id=re.compile(r"option|player", re.I)):
            for ifr in opt.find_all('iframe'):
                src = ifr.get('src') or ifr.get('data-src') or ''
                if not src:
                    continue
                full_src = src if src.startswith('http') else f"https://{domain}{src}"
                conv = convert_ruby_url(full_src)
                if conv and "rubyvidhub.com" in conv and conv not in candidates:
                    candidates.append(conv)
                if '/embed/' in full_src:
                    try:
                        r_emb = s.get(full_src, headers=HEADERS, timeout=8)
                        for m in re.finditer(r'https?://[^\s"\'<>]*(?:rubystm\.com|rubyvidhub\.com)[^\s"\'<>]*', r_emb.text):
                            conv2 = convert_ruby_url(m.group(0))
                            if conv2 and "rubyvidhub.com" in conv2 and conv2 not in candidates:
                                candidates.append(conv2)
                    except Exception:
                        pass

        # 2. Page HTML regex
        for m in re.finditer(r'https?://[^\s"\'<>]*(?:rubystm\.com|rubyvidhub\.com)[^\s"\'<>]*', r.text):
            conv = convert_ruby_url(m.group(0))
            if conv and "rubyvidhub.com" in conv and conv not in candidates:
                candidates.append(conv)

        if not candidates:
            return None

        # Return the first candidate that is ALIVE
        for cand in candidates:
            if is_ruby_alive(cand):
                return cand

    except Exception as exc:
        logger.debug("extract_toonstream_rubystm_for_episode failed on %s: %s", ep_url, exc)
    return None


def is_stream_alive(url: str) -> bool:
    """Check if a smoothpre/hanerix stream URL is still active."""
    if not url:
        return False
    try:
        r = requests.get(url, headers=HEADERS, timeout=5, stream=True)
        if r.status_code == 200:
            sample = r.content[:3000].decode("utf-8", errors="ignore").lower()
            if any(k in sample for k in ["deleted", "not found", "file was removed", "video not available"]):
                return False
            return True
    except Exception:
        pass
    return False


def is_as_cdn_alive(url: str) -> bool:
    """Check if an as-cdn / vexal / ravok stream URL (e.g. ravok.buzz/video/...) is active."""
    if not url:
        return False
    # If the URL is still pointing to the old as-cdn or vexal domain, normalize to ravok.buzz for check
    check_url = re.sub(r'https?://(?:as-cdn\d*\.top|vexal\.top)', 'https://ravok.buzz', url, flags=re.IGNORECASE)
    check_url = re.sub(r'(?:as-cdn\d*\.top|vexal\.top)', 'ravok.buzz', check_url, flags=re.IGNORECASE)
    try:
        r = requests.get(check_url, headers=HEADERS, timeout=5, stream=True)
        if r.status_code != 200:
            return False
        content_sample = r.content[:4000]
        if len(content_sample) < 100:  # blank page returned for deleted/non-existent video IDs
            return False
        sample = content_sample.decode("utf-8", errors="ignore").lower()
        dead_markers = [
            "video not found", "file not found", "file was deleted",
            "has been deleted", "removed", "invalid embed", "error 404", "not found",
            "player domain is does not match", "domain does not match",
            "something went wrong on the video process", "something went wrong",
            "<title>warning</title>"
        ]
        if any(k in sample for k in dead_markers):
            return False
        return True
    except Exception:
        # On network timeout or connection reset, do not discard working stream
        return True


def is_ruby_alive(url: str) -> bool:
    """Check if a rubyvidhub stream URL is still active (not deleted/removed/expired)."""
    if not url:
        return False
    try:
        r = requests.get(url, headers=HEADERS, timeout=6, stream=True)
        if r.status_code != 200:
            return False
        content_sample = r.content[:4000]
        if len(content_sample) < 100:
            return False
        sample = content_sample.decode("utf-8", errors="ignore").lower()
        dead_markers = [
            "video not found", "file not found", "file was deleted",
            "has been deleted", "removed", "invalid embed", "error 404",
            "video unavailable", "this video is private", "not available",
            "no longer available", "expired or has been deleted", "expired",
        ]
        if any(k in sample for k in dead_markers):
            return False
        return True
    except Exception:
        return False
    except Exception:
        pass
    return False


# ─────────────────────────────────────────────────────────────────
#  IQSmartGames full chain resolver
# ─────────────────────────────────────────────────────────────────

def resolve_iqsmartgames_embed(embed_url: str) -> Optional[str]:
    """
    Fully resolve an iqsmartgames embed URL to its smoothpre/hanerix stream link.

    For TV episodes:  embed → myseriesapi (fileslug) → embedhelper2.php → smoothpre/hanerix
    For movies:       embed → mymovieapi  (fileslug) → embedhelper2.php → smoothpre/hanerix

    NOTE: mymovieapi requires cookies set by visiting the embed page first.
    A requests.Session() is used to preserve those cookies.
    """
    try:
        # Use a Session so cookies from the embed page carry over to the API call
        sess = requests.Session()
        sess.headers.update(IQ_HEADERS)

        # Step 1: fetch embed page to get JS vars AND set session cookies
        r = sess.get(embed_url, timeout=10)
        if r.status_code != 200:
            return None

        text = r.text
        # FinalID can be numeric (TMDB) or "tt…" (IMDB)
        final_id_m = re.search(r'let FinalID\s*=\s*["\']([^"\']+)["\']', text)
        key_m      = re.search(r'let myKey\s*=\s*["\']([a-zA-Z0-9]+)["\']', text)
        season_m   = re.search(r'let season\s*=\s*["\'](\d+)["\']', text)
        ep_m       = re.search(r'let epname\s*=\s*["\'](\d+)["\']', text)

        if not (final_id_m and key_m):
            return None

        final_id = final_id_m.group(1)
        mykey    = key_m.group(1)
        is_movie = not (season_m and ep_m)   # movies don't have season/epname vars

        sess.headers.update({"Referer": embed_url, "Accept": "application/json"})

        # Step 2: call the right API to get file slugs
        if is_movie:
            id_type = "imdbid" if final_id.startswith("tt") else "tmdbid"
            api_url = (
                f"https://streams.iqsmartgames.com/mymovieapi"
                f"?{id_type}={final_id}&key={mykey}"
            )
        else:
            api_url = (
                f"https://streams.iqsmartgames.com/myseriesapi"
                f"?tmdbid={final_id}&season={season_m.group(1)}&epname={ep_m.group(1)}&key={mykey}"
            )

        r_api = sess.get(api_url, timeout=10)
        if r_api.status_code != 200:
            return None

        file_items = r_api.json().get("data", [])
        if not file_items:
            return None

        # Step 3: For each fileslug, call embedhelper2.php → decode mresult
        for item in file_items:
            slug = item.get("fileslug")
            if not slug:
                continue

            r_helper = requests.post(
                "https://pro.iqsmartgames.com/embedhelper2.php",
                headers=HELPER_HEADERS,
                data={"sid": slug, "UserFavSite": "", "currentDomain": "[]"},
                timeout=10,
            )
            if r_helper.status_code != 200:
                continue

            helper_data = r_helper.json()
            mresult_b64 = helper_data.get("mresult", "")
            if not mresult_b64:
                continue

            # Decode base64 mresult → {source_key: file_slug}
            try:
                padded = mresult_b64 + "=" * (4 - len(mresult_b64) % 4)
                mresult = json.loads(base64.b64decode(padded).decode("utf-8", errors="ignore"))
            except Exception:
                continue

            logger.debug("mresult for slug %s: %s", slug, mresult)

            # Use siteUrl from response_sources when available (handles smoothpre→morencius rename)
            response_sources = r_api.json().get("sources", {}) if hasattr(r_api, 'json') else {}
            # Only allow morencius/smoothpre (flls) and hanerix (smwh)
            priority_keys = ["flls", "smwh"]
            fallback_urls = {
                "flls":    "https://morencius.com/v/",
                "smwh":    "https://hanerix.com/e/",
            }

            for key in priority_keys:
                if key not in mresult or not mresult[key]:
                    continue
                site_url = (
                    response_sources.get(key, {}).get("siteUrl")
                    or fallback_urls.get(key)
                )
                if not site_url:
                    continue
                final_url = site_url + mresult[key]
                if any(d in site_url for d in ("smoothpre.com", "morencius.com")):
                    if not is_stream_alive(final_url):
                        continue
                if any(d in final_url for d in ("bysetayico.com", "bysevepoin.com", "byse.sx")):
                    byse_worker_url = resolve_byse_stream_url(final_url)
                    if byse_worker_url:
                        final_url = byse_worker_url
                logger.info("Resolved %s → %s", slug, final_url)
                return final_url


    except Exception as exc:
        logger.warning("iqsmartgames resolve error: %s", exc)

    return None


_GDMIRROR_EMBED_HOSTS = re.compile(
    r'(?:gdmirrorbot\.nl|filesforever\.link|freeembed\.cc|gdmirrorbot\.[a-z]{2,})/embed/([A-Za-z0-9_-]{4,})',
    re.IGNORECASE,
)

def resolve_gdmirrorbot_embed(url: str) -> Optional[str]:
    """
    Resolve a gdmirrorbot / filesforever.link / similar embed URL to a
    smoothpre (morencius) / hanerix stream link.

    Supports any host that calls /embedhelper2.php with {sid: slug}.
    Uses siteUrl from the API response when available (avoids hardcoding
    domains that may change, e.g. smoothpre.com → morencius.com).
    """
    try:
        m = _GDMIRROR_EMBED_HOSTS.search(url)
        if not m:
            return None
        slug = m.group(1)

        helper_data = None
        for endpoint in [
            "https://pro.iqsmartgames.com/embedhelper2.php",
            "https://ddn.iqsmartgames.com/embedhelper2.php",
        ]:
            try:
                r_helper = requests.post(
                    endpoint,
                    headers=HELPER_HEADERS,
                    data={"sid": slug, "UserFavSite": "", "currentDomain": "[]"},
                    timeout=15,
                )
                if r_helper.status_code == 200:
                    helper_data = r_helper.json()
                    break
            except Exception:
                pass

        if not helper_data:
            return None

        mresult_b64 = helper_data.get("mresult", "")
        if not mresult_b64:
            return None

        padded = mresult_b64 + "=" * (4 - len(mresult_b64) % 4)
        mresult = json.loads(base64.b64decode(padded).decode("utf-8", errors="ignore"))
        logger.debug("gdmirrorbot mresult for %s: %s", slug, mresult)

        # Use siteUrl from sources response when available (dynamic — handles smoothpre→morencius)
        response_sources = helper_data.get("sources", {})

        # Only allow smoothpre/morencius (flls) and hanerix (smwh)
        priority_keys = ["flls", "smwh"]
        fallback_urls = {
            "flls":    "https://morencius.com/v/",
            "smwh":    "https://hanerix.com/e/",
        }

        for key in priority_keys:
            if key not in mresult or not mresult[key]:
                continue
            # Prefer siteUrl from API response; fall back to hardcoded table
            site_url = (
                response_sources.get(key, {}).get("siteUrl")
                or fallback_urls.get(key)
            )
            if not site_url:
                continue
            final_url = site_url + mresult[key]
            # For smoothpre/morencius, verify it's alive
            if any(d in site_url for d in ("smoothpre.com", "morencius.com")):
                if not is_stream_alive(final_url):
                    continue
            if any(d in final_url for d in ("bysetayico.com", "bysevepoin.com", "byse.sx")):
                byse_worker_url = resolve_byse_stream_url(final_url)
                if byse_worker_url:
                    final_url = byse_worker_url
            logger.info("Resolved gdmirrorbot/filesforever %s → %s", slug, final_url)
            return final_url

    except Exception as exc:
        logger.warning("gdmirrorbot resolve error for %s: %s", url, exc)

    return None


def _is_title_match(target_title: str, candidate_title: str, min_score: int = 65) -> bool:
    """
    Strictly verify if candidate_title matches target_title to prevent attaching streams
    from completely different shows (e.g. Naruto or Game of Thrones).
    """
    if not target_title or not candidate_title:
        return False
    from rapidfuzz import fuzz

    def _clean_str(s: str) -> str:
        s_c = re.sub(r'(?i)\b(watch|online|free|hd|full|series|movie|movies|multimovies|toonstream|animesalt|episode|episodes|\d+x\d+|\(\d{4}\))\b', '', s)
        s_c = re.sub(r'[:–—\-\|\!\?\.\'\"]', ' ', s_c)
        return re.sub(r'\s+', ' ', s_c).strip().lower()

    clean_tgt = _clean_str(target_title)
    clean_cand = _clean_str(candidate_title)

    if not clean_tgt or not clean_cand:
        return False

    ratio = fuzz.token_sort_ratio(clean_tgt, clean_cand)
    if ratio >= min_score:
        return True

    # Subtitle prefix check (e.g. 'Show Name, Subtitle...' or 'Show Name: Subtitle')
    for delim in (':', ' - ', ' – ', ' — ', ','):
        if delim in target_title:
            prefix = target_title.split(delim)[0].strip()
            clean_pref = _clean_str(prefix)
            if clean_pref and len(clean_pref.split()) >= 2:
                if fuzz.token_sort_ratio(clean_pref, clean_cand) >= min_score:
                    return True

    tgt_words = [w for w in clean_tgt.split() if len(w) > 3 and w not in ("season", "episode", "part")]
    if tgt_words:
        matched = [w for w in tgt_words if w in clean_cand]
        if len(matched) == len(tgt_words) and fuzz.partial_ratio(clean_tgt, clean_cand) >= 60:
            return True

    return False


# ─────────────────────────────────────────────────────────────────
#  Toonstream Ruby extractor
# ─────────────────────────────────────────────────────────────────

_toonstream_session: Optional[requests.Session] = None

def _get_toonstream_session() -> requests.Session:
    global _toonstream_session
    if _toonstream_session is None:
        _toonstream_session = requests.Session()
        _toonstream_session.headers.update(HEADERS)
    return _toonstream_session

def _toonstream_get(url: str, **kwargs) -> requests.Response:
    """
    Execute a GET request against Toonstream, automatically handling the
    '24-Hour Access Redirect System' (/access?returnTo=...) gate.
    If redirected to /access, automatically unlocks 24-hour access via
    /api/access/ads/verify and re-requests the target page with active cookies.
    """
    s = _get_toonstream_session()
    if "timeout" not in kwargs:
        kwargs["timeout"] = 10
    if "headers" not in kwargs:
        kwargs["headers"] = HEADERS

    safe_url = _encode_url_path(url)
    resp = s.get(safe_url, **kwargs)

    # Check if redirected to the 24-Hour access gate
    if "/access" in resp.url or "24-Hour Access Redirect" in resp.text:
        domain = urlparse(resp.url).netloc or "toonstream.us"
        parsed = urlparse(resp.url)
        from urllib.parse import parse_qs
        qs = parse_qs(parsed.query)
        return_to = qs.get("returnTo", ["/"])[0]

        logger.info("[Toonstream Access Gate] Unlocking 24-hr access for domain %s (returnTo: %s)...", domain, return_to)
        verify_url = f"https://{domain}/api/access/ads/verify"
        try:
            s.post(verify_url, json={"returnTo": return_to}, timeout=10)
        except Exception as v_err:
            logger.warning("[Toonstream Access Gate] Verify error: %s", v_err)

        resp = s.get(safe_url, **kwargs)

    return resp

_toonstream_search_cache: dict[str, List[str]] = {}

def _toonstream_search_series_urls(show_name: str) -> List[str]:
    """
    Query Toonstream's AJAX search API (/search/all?q=) to discover the correct
    series page URL(s) for a show.
    """
    from urllib.parse import quote as _url_quote

    # Normalise: strip season tags, quality suffixes, parenthetical tags
    _sq = re.sub(r"\(Season\s*\d+\)", "", show_name, flags=re.IGNORECASE).strip()
    _sq = re.sub(r"Season\s*\d+", "", _sq, flags=re.IGNORECASE).strip()
    _sq = _STRIP_TAGS.sub("", _sq).strip()
    _sq = re.sub(r'\s*\([^)]*\)', '', _sq).strip()
    _sq = re.sub(r'\s{2,}', ' ', _sq).strip()
    _sq = _sq.replace('’', "'").replace('‘', "'")

    words = _sq.split()
    queries: List[str] = [_sq]

    # Split on punctuation delimiters (comma, colon, dash) to catch main title part
    for p in re.split(r'[-–—:,]', _sq):
        p_clean = p.strip()
        if p_clean and p_clean not in queries:
            queries.append(p_clean)

    # Short prefix queries (Toonstream AJAX search often returns 0 results for queries with >3 words or complex symbols)
    if len(words) >= 4:
        _w3 = ' '.join(words[:3]).strip()
        if _w3 and _w3 not in queries:
            queries.append(_w3)
    if len(words) >= 2:
        _w2 = ' '.join(words[:2]).strip()
        if _w2 and _w2 not in queries:
            queries.append(_w2)

    # Also include versions without apostrophes
    for q_cand in list(queries):
        if "'" in q_cand:
            q_no_apos = q_cand.replace("'", "")
            if q_no_apos not in queries:
                queries.append(q_no_apos)

    if len(words) > 1 and (len(words[-1]) <= 3 or words[-1].isupper()):
        _shorter = ' '.join(words[:-1]).strip()
        if _shorter and _shorter not in queries:
            queries.append(_shorter)
    if len(words) > 2:
        _drop1 = ' '.join(words[:-1]).strip()
        if _drop1 and _drop1 not in queries:
            queries.append(_drop1)

    cache_key = show_name.lower().strip()
    if cache_key in _toonstream_search_cache:
        return _toonstream_search_cache[cache_key]

    for domain in TOONSTREAM_DOMAINS:
        if not _domain_ok(domain):
            continue
        for q in queries:
            try:
                _api = f"https://{domain}/search/all?q={_url_quote(q)}"
                _r = _toonstream_get(_api, timeout=8)
                if _r.status_code != 200:
                    continue
                _j = _r.json()
                _found: List[str] = []
                for _item in _j.get("data", []):
                    if _item.get("type") in ("series", "anime"):
                        item_title = _item.get("title", "") or _item.get("name", "")
                        if item_title and not _is_title_match(show_name, item_title, min_score=65):
                            logger.debug("Toonstream AJAX title mismatch: '%s' vs '%s'", show_name, item_title)
                            continue
                        _path = _item.get("url", "")
                        _full = _path if _path.startswith("http") else f"https://{domain}{_path}"
                        if _full not in _found:
                            _found.append(_full)
                if _found:
                    logger.debug("Toonstream AJAX '%s' → %s", q, _found)
                    _toonstream_search_cache[cache_key] = _found
                    return _found
            except Exception:
                pass

    _toonstream_search_cache[cache_key] = []
    return []


def _get_toonstream_slug_candidates(show_name: str) -> List[str]:
    """
    Build candidate toonstream URL slugs from a show name.
    Handles apostrophes, smart quotes, em-dashes, colons, parenthesized tags, and word compounds.
    Also strips quality/edition/language suffixes that appear in scraped titles but never in URLs.
    e.g. "Captain Tsubasa (2018)" -> ["captain-tsubasa", "captain-tsubasa-2018"]
    e.g. "(18+)Yowayowa Sensei" -> ["yowayowa-sensei", "18-yowayowa-sensei"]
    e.g. "Slime BluRay" -> ["slime", "slime-bluray"]
    """
    # 1. Normalize smart quotes, em-dashes, and season tags
    t = show_name
    t = re.sub(r'[‘’`]', "'", t)
    t = re.sub(r'[–—]', '-', t)
    t = re.sub(r'\s*\(season\s*\d+\)', '', t, flags=re.IGNORECASE).strip()
    t = re.sub(r'\s*season\s*\d+', '', t, flags=re.IGNORECASE).strip()

    # Strip quality/language tags (keep variant for fallback too)
    t_clean = _STRIP_TAGS.sub('', t).strip()
    t_clean = re.sub(r'\s{2,}', ' ', t_clean).strip()

    # Strip parenthesized tags like (2018), (18+), (uncensored), (dub), etc.
    t_noparen = re.sub(r'\s*\([^)]*\)', '', t).strip()
    if not t_noparen:
        t_noparen = t
    t_clean_noparen = re.sub(r'\s*\([^)]*\)', '', t_clean).strip()
    if not t_clean_noparen:
        t_clean_noparen = t_clean

    # Collect all base variants: clean (no quality tags) first, then originals as fallback
    base_inputs = []
    for inp in [t_clean_noparen, t_clean, t_noparen, t]:
        if inp and inp not in base_inputs:
            base_inputs.append(inp)
            if ":" in inp:
                b_col = inp.split(":")[0].strip()
                if b_col and b_col not in base_inputs:
                    base_inputs.append(b_col)
            if " - " in inp:
                b_dash = inp.split(" - ")[0].strip()
                if b_dash and b_dash not in base_inputs:
                    base_inputs.append(b_dash)

    base_texts = []
    for inp in base_inputs:
        base_texts.append(inp)
        # Split CamelCase (e.g. MarriageToxin -> Marriage Toxin)
        t_camel = re.sub(r'([a-z])([A-Z])', r'\1 \2', inp)
        base_texts.append(t_camel)

        # Remove colons/punctuation (e.g. Re:Zero -> ReZero)
        t_nocolon = re.sub(r'[:;,!?()]+', '', inp)
        t_nocolon_camel = re.sub(r'([a-z])([A-Z])', r'\1 \2', t_nocolon)
        base_texts.extend([t_nocolon, t_nocolon_camel])

    candidates = []
    for text in base_texts:
        # c1: keep apostrophe
        c1 = re.sub(r"[^\w']+", "-", text.lower()).strip("-")
        c1 = re.sub(r"-+", "-", c1)

        # c2: strip apostrophe completely (tamon's -> tamons)
        c2 = text.lower().replace("'", "")
        c2 = re.sub(r"[^a-z0-9]+", "-", c2).strip("-")
        c2 = re.sub(r"-+", "-", c2)

        # c3: replace apostrophe with hyphen (tamon's -> tamon-s)
        c3 = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
        c3 = re.sub(r"-+", "-", c3)

        for c in [c1, c2, c3]:
            if c and c not in candidates:
                candidates.append(c)

    # Add progressive word-truncated variants for long titles (>4 words)
    long_candidates = list(candidates)
    for c in long_candidates:
        parts = c.split("-")
        if len(parts) > 4:
            for num_w in [4, 5, 6]:
                if num_w < len(parts):
                    short_c = "-".join(parts[:num_w])
                    if short_c and short_c not in candidates:
                        candidates.append(short_c)

    # Special case mappings & audio/version slug suffix variants for Toonstream
    extra_variants = []
    suffix_additions = ["-multi-audio", "-dual-audio", "-hindi", "-english", "-subbed", "-dubbed"]
    for c in list(candidates):
        if "re-zero" in c:
            extra_variants.append(c.replace("re-zero", "rezero"))
        if "marriagetoxin" in c:
            extra_variants.append(c.replace("marriagetoxin", "marriage-toxin"))
        for sfx in suffix_additions:
            if not c.endswith(sfx):
                extra_variants.append(f"{c}{sfx}")

    for ex in extra_variants:
        if ex not in candidates:
            candidates.append(ex)

    return candidates

_last_flaresolverr_sync: float = 0.0

def _flaresolverr_sync_session(target_url: str = "https://toon-stream.site/series/captain-tsubasa-2/") -> bool:
    """
    Call FlareSolverr ONCE to solve Cloudflare challenge and extract fresh cf_clearance cookie + matching User-Agent.
    Updates HEADERS and settings.json so all subsequent direct python requests run at ultra-fast speed (<0.5s)!
    """
    global _last_flaresolverr_sync
    import time
    if time.time() - _last_flaresolverr_sync < 60:
        return False
    if not FLARESOLVERR_URL:
        return False

    try:
        logger.info("FlareSolverr: Fetching fresh session tokens for %s...", target_url)
        payload = {"cmd": "request.get", "url": target_url, "maxTimeout": 45000}
        resp = requests.post(f"{FLARESOLVERR_URL}/v1", json=payload, timeout=55)
        if resp.status_code == 200:
            data = resp.json()
            sol = data.get("solution", {})
            ua = sol.get("userAgent", "")
            cookies = sol.get("cookies", [])
            cf_val = None
            for c in cookies:
                if c.get("name") == "cf_clearance":
                    cf_val = c.get("value")
                    break

            if cf_val and ua:
                HEADERS["Cookie"] = f"cf_clearance={cf_val}"
                HEADERS["User-Agent"] = ua
                _last_flaresolverr_sync = time.time()

                try:
                    if _settings_file.exists():
                        with open(_settings_file, "r", encoding="utf-8") as f:
                            _s = json.load(f)
                        _s["cf_clearance"] = cf_val
                        _s["cf_user_agent"] = ua
                        with open(_settings_file, "w", encoding="utf-8") as f:
                            json.dump(_s, f, indent=2)
                except Exception:
                    pass
                logger.info("FlareSolverr: Successfully updated HEADERS and settings.json with fresh cf_clearance!")
                return True
    except Exception as exc:
        logger.warning("FlareSolverr session sync error: %s", exc)
    return False


# ── AnimeDekho.app (Server UG alternative) ───────────────────────────────────

_animedekho_session = requests.Session()
_animedekho_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})
_last_animedekho_verify_time = 0.0


def _clean_verify_url(raw_url: str) -> str:
    """Clean & decode HTML entities in AnimeDekho dgt_verify URLs."""
    if not raw_url:
        return ""
    import html as _html
    u = raw_url.replace("&#038;", "&").replace("&#x26;", "&").replace("&amp;", "&")
    u = _html.unescape(u)
    u = u.replace("&#038;", "&").replace("&#x26;", "&").replace("&amp;", "&")
    u = re.sub(r'#(?=e=|i=|t=|next=)', '&', u)
    return u.strip()


def _animedekho_bypass_gate(gate_url: str) -> bool:
    """
    Automatically bypass AnimeDekho's dgt_gate shortener.
    Fetches the gate page, extracts the shortlink verify URL, waits the required 11s cooldown,
    and visits the verify URL to unlock video players for 24 hours.
    """
    global _last_animedekho_verify_time
    import time
    if not gate_url:
        return False
    try:
        r_gate = _animedekho_session.get(gate_url, timeout=15)
        if r_gate.status_code != 200:
            return False
        sl_m = re.search(r'name=["\']shortlink["\']\s+value=["\']([^"\']+)["\']', r_gate.text)
        if not sl_m:
            sl_m = re.search(r'value=["\']([^"\']*dgt_verify=1[^"\']*)["\']', r_gate.text)
        if not sl_m:
            return False
        verify_url = _clean_verify_url(sl_m.group(1))
        if not verify_url or "dgt_verify=1" not in verify_url:
            return False

        # Server enforces a minimum 10-second wait after token creation (issuedAt parameter 'i')
        i_m = re.search(r'[?&]i=(\d+)', verify_url)
        issued_at = int(i_m.group(1)) if i_m else 0
        now_sec = int(time.time())
        elapsed = now_sec - issued_at if issued_at > 0 else 11
        wait_sec = max(0, 11 - elapsed)
        if wait_sec > 0:
            logger.info("AnimeDekho: waiting %ds cooldown for dgt_gate verification token...", wait_sec)
            time.sleep(wait_sec)

        r_ver = _animedekho_session.get(verify_url, headers={"Referer": gate_url}, timeout=20)
        _last_animedekho_verify_time = time.time()
        logger.info("AnimeDekho: dgt_gate bypass complete (status=%d).", r_ver.status_code)
        return True
    except Exception as exc:
        logger.warning("AnimeDekho gate bypass error: %s", exc)
        return False


def _animedekho_verify_bypass(html_content: str = "", gate_url: str = ""):
    """
    AnimeDekho shows a link-shortener verification page on episode links that lasts 24h.
    - If gate_url or html_content contains a 'dgt_gate=1' link, bypass it via _animedekho_bypass_gate.
    - If html_content has a dynamic 'shortlink' input pointing to /24hr/verify.php, visit it directly.
    - Otherwise, visit /skip/verify.php which sets the required 'toronites_server=vidstream' cookie.
    """
    global _last_animedekho_verify_time
    import time

    base = ANIMEDEKHO_URL.rstrip("/")

    if gate_url:
        _animedekho_bypass_gate(gate_url)
        return

    # Check for dgt_gate in html_content
    if html_content and "dgt_gate=1" in html_content:
        m_gate = re.search(r'href=["\']([^"\']*dgt_gate=1[^"\']*)["\']', html_content)
        if m_gate:
            g_url = m_gate.group(1)
            full_gate = g_url if g_url.startswith("http") else f"{base}{g_url}"
            _animedekho_bypass_gate(full_gate)
            return

    # If an HTML page is provided that contains a dynamic 24hr shortlink, resolve it directly
    if html_content and "24hr/verify.php" in html_content:
        m = re.search(r'https?://[^\s"\'<>]+/24hr/verify\.php\?[^\s"\'<>]+', html_content)
        if m:
            try:
                r = _animedekho_session.get(m.group(0), timeout=15)
                _last_animedekho_verify_time = time.time()
                logger.info("AnimeDekho: 24hr dynamic verification bypass done (status=%d, cookie=%s).",
                            r.status_code, bool("toronites_server" in _animedekho_session.cookies))
                return
            except Exception as e:
                logger.debug("AnimeDekho dynamic verify error: %s", e)

    if time.time() - _last_animedekho_verify_time < 43200 and "toronites_server" in _animedekho_session.cookies:
        return

    try:
        r = _animedekho_session.get(f"{base}/skip/verify.php", timeout=15)
        _last_animedekho_verify_time = time.time()
        logger.info("AnimeDekho: verification bypass done (status=%d, cookie=%s).",
                    r.status_code, bool("toronites_server" in _animedekho_session.cookies))
    except Exception as e:
        logger.debug("AnimeDekho verify error (non-fatal): %s", e)


_animedekho_show_cache: dict[str, Optional[str]] = {}


def search_animedekho_series(show_name: str) -> Optional[str]:
    """Search AnimeDekho for series URL (e.g. https://animedekho.app/series-hindi/{slug}/)."""
    show_key = show_name.lower().strip()
    if show_key in _animedekho_show_cache:
        return _animedekho_show_cache[show_key]

    _animedekho_verify_bypass()

    clean_name = re.sub(r'[\s\ufffd\u2013\u2014\-–—]+(Episode\s*\d+\s*Added!|All\s*Episodes\s*Added!).*$', '', show_name, flags=re.IGNORECASE)
    clean_name = re.sub(r'\s*(season\s*\d+|s\d+)\s*$', '', clean_name, flags=re.IGNORECASE).strip()
    clean_name = clean_name.replace('\ufffdm', "'m").replace('\ufffds', "'s").replace('Fianc\ufffd', 'Fiance')
    clean_name = re.sub(r'[:–—\-\|\!\?\.\'\"\ufffd]', ' ', clean_name)
    clean_name = re.sub(r'\s+', ' ', clean_name).strip()

    from urllib.parse import quote
    from rapidfuzz import fuzz

    base = ANIMEDEKHO_URL.rstrip("/")
    search_url = f"{base}/?s={quote(clean_name)}"
    try:
        r = _animedekho_session.get(search_url, timeout=20)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            candidates = []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                # Accept both /series-hindi/ and /series/ paths
                if ("/series-hindi/" in href or "/series/" in href) and href not in (f"{base}/series-hindi/", f"{base}/series/"):
                    t = a.get_text(strip=True)
                    if not t:
                        img = a.find("img", alt=True)
                        t = img["alt"] if img else ""
                    if not t:
                        m_slug = re.search(r'/series(?:-hindi)?/([^/]+)/', href)
                        t = m_slug.group(1).replace('-', ' ') if m_slug else ""
                    if href not in [c[1] for c in candidates]:
                        candidates.append((t, href))

            if candidates:
                best_url = None
                best_score = 0
                for title, href in candidates:
                    score = fuzz.token_sort_ratio(clean_name.lower(), title.lower())
                    if score > best_score:
                        best_score = score
                        best_url = href
                if best_score >= 55 and best_url:
                    _animedekho_show_cache[show_key] = best_url
                    return best_url

            # Direct slug fallback: /series-hindi/{slug}/ and /series/{slug}/
            slug = re.sub(r'[^a-z0-9]+', '-', clean_name.lower()).strip('-')
            for try_slug in [slug, f"{slug}-hindi"]:
                for path in [f"/series-hindi/{try_slug}/", f"/series/{try_slug}/"]:
                    test_url = f"{base}{path}"
                    try:
                        r_try = _animedekho_session.get(test_url, timeout=20)
                        if r_try.status_code == 200 and len(r_try.text) > 1000 and "404" not in r_try.text:
                            _animedekho_show_cache[show_key] = test_url
                            return test_url
                    except Exception:
                        pass
    except Exception as e:
        logger.debug("search_animedekho_series error for %s: %s", show_name, e)

    _animedekho_show_cache[show_key] = None
    return None



_animedekho_ep_maps: dict[tuple[str, int], dict[int, str]] = {}


def get_animedekho_episode_map(show_name: str, season: int = 1, series_url: str = "") -> dict[int, str]:
    """Map episode numbers to direct episode URLs for a given season on AnimeDekho.
    Handles paginated episode lists by following page=2, page=3, etc."""
    cache_key = (show_name.lower().strip(), season)
    if not series_url and cache_key in _animedekho_ep_maps:
        return _animedekho_ep_maps[cache_key]

    if not series_url:
        series_url = search_animedekho_series(show_name)
    if not series_url:
        _animedekho_ep_maps[cache_key] = {}
        return {}

    base = ANIMEDEKHO_URL.rstrip("/")
    ep_map = {}
    season_pattern = re.compile(rf"-{season}x(\d+)/?", re.IGNORECASE)

    def _scrape_page(page_url: str) -> bool:
        """Scrape one page of episode links. Returns True if any found."""
        try:
            r = _animedekho_session.get(page_url, timeout=20)
            if r.status_code != 200:
                return False
            soup = BeautifulSoup(r.text, "html.parser")
            found_any = False
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/epi/" in href:
                    m = season_pattern.search(href)
                    if m:
                        ep_num = int(m.group(1))
                        full_ep = href if href.startswith("http") else f"{base}{href}"
                        if ep_num not in ep_map:
                            ep_map[ep_num] = full_ep
                            found_any = True
            return found_any
        except Exception as e:
            logger.debug("get_animedekho_episode_map page error for %s: %s", page_url, e)
            return False

    try:
        # Fetch first page
        _scrape_page(series_url)
        # If episodes found, check for paginated pages (page=2, page=3...)
        if ep_map:
            for page_num in range(2, 30):
                sep = "&" if "?" in series_url else "?"
                page_url = f"{series_url.rstrip('/')}{sep}page={page_num}"
                found = _scrape_page(page_url)
                if not found:
                    break  # No more pages
    except Exception as e:
        logger.debug("get_animedekho_episode_map error for %s: %s", show_name, e)

    _animedekho_ep_maps[cache_key] = ep_map
    return ep_map



def count_animedekho_episodes(show_name: str, season: int = 1) -> int:
    """Return highest episode number found on AnimeDekho for a season."""
    ep_map = get_animedekho_episode_map(show_name, season)
    if ep_map:
        return max(ep_map.keys())
    return 0


def _resolve_animedekho_embed(embed_url: str, ref_url: str = "") -> Optional[str]:
    """Fetch an AnimeDekho player.php or /embed/ URL and extract the actual iframe stream link."""
    if not embed_url:
        return None
    if any(k in embed_url for k in ("ravok.buzz", "vidara.to", "vidaara.", "vidmoly.")):
        return embed_url
    try:
        headers = {"Referer": ref_url} if ref_url else {}
        r = _animedekho_session.get(embed_url, headers=headers, timeout=12)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            for ifr in soup.find_all("iframe"):
                src = ifr.get("src") or ifr.get("data-src") or ""
                if src:
                    return src
            m = re.search(r'https?://[^\s"\'<>]+(?:vidara\.[a-z0-9]+|vidaara\.[a-z0-9]+|ravok\.buzz|vidmoly\.[a-z0-9]+|as-cdn\d*\.top)[^\s"\'<>]*', r.text)
            if m:
                return m.group(0)
    except Exception as exc:
        logger.debug("Error resolving AnimeDekho embed %s: %s", embed_url, exc)
    return None


def fetch_animedekho_sources(ep_url: str) -> list[dict]:
    """
    Fetch all server sources for an AnimeDekho episode page using the DGT REST API.
    Automatically detects if the page is gated (?dgt_gate=1), executes the bypass,
    and retrieves all unlocked sources (Vidstream/ravok.buzz, Vidara/Vidaara, Vidmoly, etc.).
    """
    if not ep_url:
        return []
    base = ANIMEDEKHO_URL.rstrip("/")
    try:
        r_ep = _animedekho_session.get(ep_url, timeout=15)
        if r_ep.status_code != 200:
            return []
        soup = BeautifulSoup(r_ep.text, "html.parser")

        # 1. Check if the page itself has a gate link or gatebox
        gate_m = re.search(r'href=["\']([^"\']*dgt_gate=1[^"\']*)["\']', r_ep.text)
        if gate_m:
            gate_u = gate_m.group(1)
            full_gate = gate_u if gate_u.startswith("http") else f"{base}{gate_u}"
            _animedekho_bypass_gate(full_gate)
            r_ep = _animedekho_session.get(ep_url, timeout=15)
            soup = BeautifulSoup(r_ep.text, "html.parser")

        # 2. Extract DGT REST base and post_id / episode_id
        m_dgt = re.search(r'var\s+DGT\s*=\s*(\{.+?\})(?:;|\n)', r_ep.text)
        dgt_cfg = json.loads(m_dgt.group(1)) if m_dgt else {}
        nonce = dgt_cfg.get("nonce", "")
        rest_base = dgt_cfg.get("rest", f"{base}/wp-json/dgt/v1/").rstrip("/")

        root = soup.find(id="dgt-watch")
        post_id = int(root.get("data-title", 0)) if root else 0
        episode_id = int(root.get("data-term", 0)) if root else 0
        if not post_id or not episode_id:
            m_post = re.search(r'data-title=["\'](\d+)["\']', r_ep.text)
            m_ep = re.search(r'data-term=["\'](\d+)["\']', r_ep.text)
            post_id = int(m_post.group(1)) if m_post else 0
            episode_id = int(m_ep.group(1)) if m_ep else 0
        if not post_id or not episode_id:
            return []

        watch_url = f"{rest_base}/watch"
        headers = {"Content-Type": "application/json", "Referer": ep_url}
        if nonce:
            headers["X-WP-Nonce"] = nonce
        payload = {"post_id": post_id, "episode_id": episode_id}

        r_watch = _animedekho_session.post(watch_url, headers=headers, json=payload, timeout=15)
        try:
            watch_data = r_watch.json()
        except Exception:
            watch_data = {}
        if watch_data.get("code") == "rest_cookie_invalid_nonce":
            headers.pop("X-WP-Nonce", None)
            r_watch = _animedekho_session.post(watch_url, headers=headers, json=payload, timeout=15)
            try:
                watch_data = r_watch.json()
            except Exception:
                watch_data = {}

        if watch_data.get("gated"):
            gate_url = watch_data.get("gate", "")
            if gate_url:
                full_gate = gate_url if gate_url.startswith("http") else f"{base}{gate_url}"
                if _animedekho_bypass_gate(full_gate):
                    r_watch2 = _animedekho_session.post(watch_url, headers=headers, json=payload, timeout=15)
                    try:
                        watch_data = r_watch2.json()
                    except Exception:
                        pass
        return watch_data.get("sources", [])
    except Exception as exc:
        logger.warning("fetch_animedekho_sources error for %s: %s", ep_url, exc)
        return []


def extract_animedekho_all_streams(
    show_name: str,
    season: int = 1,
    episode: int = 1,
    direct_url: str = "",
) -> dict[str, Optional[str]]:
    """
    Extract all available streams (ravok.buzz, vidaara, vidmoly) from AnimeDekho.
    Returns dict: {"ravok_url": str|None, "vidaara_url": str|None, "vidmoly_url": str|None}
    """
    base = ANIMEDEKHO_URL.rstrip("/")
    ep_url = direct_url.strip() if direct_url else ""
    if not ep_url or "/epi/" not in ep_url:
        ep_map = get_animedekho_episode_map(show_name, season)
        ep_url = ep_map.get(episode, "")

    if not ep_url:
        c_name = re.sub(r'[\s\ufffd\u2013\u2014\-–—]+(Episode\s*\d+\s*Added!|All\s*Episodes\s*Added!).*$', '', show_name, flags=re.IGNORECASE)
        c_name = re.sub(r'\s*(season\s*\d+|s\d+)\s*$', '', c_name, flags=re.IGNORECASE).strip()
        c_name = c_name.replace('\ufffdm', "'m").replace('\ufffds', "'s").replace('Fianc\ufffd', 'Fiance')
        c_name = re.sub(r'&#8211;|–|—', '-', c_name)
        c_name = re.sub(r'&#8217;|’|\ufffd', "'", c_name)
        slug = re.sub(r'[^a-z0-9]+', '-', c_name.lower()).strip('-')

        candidates = [slug, f"{slug}-hindi"]
        if direct_url and ("/series-hindi/" in direct_url or "/series/" in direct_url):
            dir_slug = direct_url.strip("/").split("/")[-1]
            if dir_slug not in candidates:
                candidates.insert(0, dir_slug)
        if "thousand-year" in slug or "tybw" in slug or "blood-war" in slug:
            candidates.insert(0, "bleach-thousand-year-blood-war")
        elif "bleach" in slug and "bleach" not in candidates:
            candidates.append("bleach")
        if "lord-of-mysteries" in slug and "lord-of-mysteries" not in candidates:
            candidates.append("lord-of-mysteries")
        if "fiance" in slug or "fianc" in slug:
            candidates.append("even-though-im-a-super-timid-noble-girl-i-accepted-the-bet-from-my-cunning-fiance")
            candidates.append("even-though-im-a-super-timid-noble-girl-i-accepted-the-bet-from-my-cunning-fianc")
        if "dating-sim" in slug:
            candidates.insert(0, "trapped-in-a-dating-sim-the-world-of-otome-games-is-tough-for-mobs")

        for s in candidates:
            seasons_to_test = [season]
            if "thousand-year" in s or "tybw" in s:
                if 2 not in seasons_to_test:
                    seasons_to_test.append(2)
            for sn in seasons_to_test:
                candidate = f"{base}/epi/{s}-{sn}x{episode}/"
                try:
                    r_test = _animedekho_session.get(candidate, timeout=10)
                    if r_test.status_code == 200 and len(r_test.text) > 1000 and "<title>404" not in r_test.text:
                        ep_url = candidate
                        break
                except Exception:
                    pass
            if ep_url:
                break

    if not ep_url:
        return {"ravok_url": None, "vidaara_url": None, "vidmoly_url": None}

    sources = fetch_animedekho_sources(ep_url)
    res: dict[str, Optional[str]] = {"ravok_url": None, "vidaara_url": None, "vidmoly_url": None}

    for s in sources:
        srv = str(s.get("server", "")).lower()
        emb = s.get("embed", "")
        if not emb:
            continue
        if "vidstream" in srv:
            resolved = _resolve_animedekho_embed(emb, ep_url)
            # Only set ravok_url if resolved is a real ravok/as-cdn URL (not a get2.php relay)
            # and don't overwrite an already-good ravok_url with a bad one
            if resolved and any(k in resolved for k in ("ravok.buzz", "as-cdn", "vexal.top")):
                if not res["ravok_url"] or "get2.php" in res["ravok_url"] or not any(k in res["ravok_url"] for k in ("ravok.buzz", "as-cdn", "vexal.top")):
                    res["ravok_url"] = resolved
        elif "vidara" in srv or "vidaara" in srv:
            resolved = _resolve_animedekho_embed(emb, ep_url)
            if resolved:
                res["vidaara_url"] = resolved
        elif "vidmoly" in srv:
            resolved = _resolve_animedekho_embed(emb, ep_url)
            if resolved:
                res["vidmoly_url"] = resolved

    # Fallback scan across all sources for direct links
    for s in sources:
        emb = s.get("embed", "")
        if "ravok.buzz" in emb and not res["ravok_url"]:
            res["ravok_url"] = emb
        if ("vidara." in emb or "vidaara." in emb) and not res["vidaara_url"]:
            res["vidaara_url"] = emb
        if "vidmoly." in emb and not res["vidmoly_url"]:
            res["vidmoly_url"] = emb

    return res


def extract_animedekho_stream(
    show_name: str,
    season: int = 1,
    episode: int = 1,
    direct_url: str = "",
) -> Optional[str]:
    """
    Extract direct AS-CDN video stream (Server UG / ravok.buzz or vidaara) from AnimeDekho.
    Handles dgt_gate verification bypass, watch REST API, and iframe resolution.
    """
    streams = extract_animedekho_all_streams(show_name, season=season, episode=episode, direct_url=direct_url)
    if streams.get("ravok_url"):
        return streams["ravok_url"]
    if streams.get("vidaara_url"):
        return streams["vidaara_url"]

    _animedekho_verify_bypass()
    base = ANIMEDEKHO_URL.rstrip("/")

    ep_url = direct_url.strip() if direct_url else ""
    if not ep_url or "/epi/" not in ep_url:
        ep_map = get_animedekho_episode_map(show_name, season)
        ep_url = ep_map.get(episode, "")

    if not ep_url:
        c_name = re.sub(r'[\s\ufffd\u2013\u2014\-–—]+(Episode\s*\d+\s*Added!|All\s*Episodes\s*Added!).*$', '', show_name, flags=re.IGNORECASE)
        c_name = re.sub(r'\s*(season\s*\d+|s\d+)\s*$', '', c_name, flags=re.IGNORECASE).strip()
        c_name = c_name.replace('\ufffdm', "'m").replace('\ufffds', "'s").replace('Fianc\ufffd', 'Fiance')
        c_name = re.sub(r'&#8211;|–|—', '-', c_name)
        c_name = re.sub(r'&#8217;|’|\ufffd', "'", c_name)
        slug = re.sub(r'[^a-z0-9]+', '-', c_name.lower()).strip('-')

        candidates = [slug, f"{slug}-hindi"]
        if direct_url and ("/series-hindi/" in direct_url or "/series/" in direct_url):
            dir_slug = direct_url.strip("/").split("/")[-1]
            if dir_slug not in candidates:
                candidates.insert(0, dir_slug)
        if "thousand-year" in slug or "tybw" in slug or "blood-war" in slug:
            candidates.insert(0, "bleach-thousand-year-blood-war")
        elif "bleach" in slug and "bleach" not in candidates:
            candidates.append("bleach")
        if "lord-of-mysteries" in slug and "lord-of-mysteries" not in candidates:
            candidates.append("lord-of-mysteries")
        if "fiance" in slug or "fianc" in slug:
            candidates.append("even-though-im-a-super-timid-noble-girl-i-accepted-the-bet-from-my-cunning-fiance")
            candidates.append("even-though-im-a-super-timid-noble-girl-i-accepted-the-bet-from-my-cunning-fianc")
        if "dating-sim" in slug:
            candidates.insert(0, "trapped-in-a-dating-sim-the-world-of-otome-games-is-tough-for-mobs")

        for s in candidates:
            seasons_to_test = [season]
            if "thousand-year" in s or "tybw" in s:
                if 2 not in seasons_to_test:
                    seasons_to_test.append(2)
            for sn in seasons_to_test:
                candidate = f"{base}/epi/{s}-{sn}x{episode}/"
                try:
                    r_test = _animedekho_session.get(candidate, timeout=20)
                    if r_test.status_code == 200 and len(r_test.text) > 1000 and "<title>404" not in r_test.text:
                        ep_url = candidate
                        break
                except Exception:
                    pass
            if ep_url:
                break

    if not ep_url:
        return None

    try:
        r = _animedekho_session.get(ep_url, timeout=20)
        if r.status_code != 200:
            return None

        soup = BeautifulSoup(r.text, "html.parser")

        def _norm_ug(u: str) -> str:
            return re.sub(r'https?://(?:as-cdn\d*\.top|vexal\.top)', 'https://ravok.buzz', u, flags=re.IGNORECASE)

        # 0. Fast regex check in raw HTML — avoids extra round-trips when stream is inline
        m_direct = re.search(r'https?://(?:as-cdn\d*\.top|vexal\.top|ravok\.buzz)/video/[a-f0-9]+', r.text)
        if m_direct:
            return _norm_ug(m_direct.group(0))

        # 1. Check iframes directly on page (both src and data-src)
        for ifr in soup.find_all("iframe"):
            src = ifr.get("src") or ifr.get("data-src") or ""
            if any(k in src for k in ("as-cdn", "vexal.top", "ravok.buzz")):
                return _norm_ug(src)
            elif "/embed/" in src:
                emb_url = src if src.startswith("http") else f"{base}{src}"
                try:
                    emb_r = _animedekho_session.get(emb_url, headers={"Referer": ep_url}, timeout=20)
                    emb_soup = BeautifulSoup(emb_r.text, "html.parser")
                    for e_ifr in emb_soup.find_all("iframe"):
                        e_src = e_ifr.get("src") or e_ifr.get("data-src") or ""
                        if any(k in e_src for k in ("as-cdn", "vexal.top", "ravok.buzz")):
                            return _norm_ug(e_src)
                    # Also check raw text in embed page
                    m_emb = re.search(r'https?://(?:as-cdn\d*\.top|vexal\.top|ravok\.buzz)/video/[a-f0-9]+', emb_r.text)
                    if m_emb:
                        return _norm_ug(m_emb.group(0))
                except Exception:
                    pass

        # 2. Check base64 data-src options (e.g. VidStream server tab)
        for a in soup.find_all("a", attrs={"data-src": True}):
            raw_b64 = a["data-src"]
            try:
                padded = raw_b64 + "=" * (4 - len(raw_b64) % 4)
                dec = base64.b64decode(padded).decode("utf-8")
                if "/embed/" in dec:
                    emb_url = dec if dec.startswith("http") else f"{base}{dec}"
                    emb_r = _animedekho_session.get(emb_url, headers={"Referer": ep_url}, timeout=20)
                    emb_soup = BeautifulSoup(emb_r.text, "html.parser")
                    for e_ifr in emb_soup.find_all("iframe"):
                        e_src = e_ifr.get("src") or e_ifr.get("data-src") or ""
                        if any(k in e_src for k in ("as-cdn", "vexal.top", "ravok.buzz")):
                            return _norm_ug(e_src)
            except Exception:
                pass

        # 3. Text/script regex fallback
        m = re.search(r'https?://(?:as-cdn\d*\.top|vexal\.top|ravok\.buzz)/video/[a-f0-9]+', r.text)
        if m:
            return _norm_ug(m.group(0))

    except Exception as e:
        logger.debug("extract_animedekho_stream error for %s S%dE%d: %s", show_name, season, episode, e)

    return None


def extract_animedekho_vidmoly_stream(
    show_name: str,
    season: int = 1,
    episode: int = 1,
    direct_url: str = "",
) -> Optional[str]:
    """
    Extract direct Vidmoly stream URL (e.g. https://vidmoly.biz/embed-....html) from AnimeDekho.
    Handles verification bypass, episode page resolution, Vidmoly data-src option resolution,
    and returns the vidmoly iframe URL.
    """
    streams = extract_animedekho_all_streams(show_name, season=season, episode=episode, direct_url=direct_url)
    if streams.get("vidmoly_url"):
        return streams["vidmoly_url"]

    _animedekho_verify_bypass()
    base = ANIMEDEKHO_URL.rstrip("/")

    ep_url = ""
    direct_url_clean = direct_url.strip() if direct_url else ""

    if direct_url_clean:
        if "/epi/" in direct_url_clean:
            # Check if this direct_url matches the requested episode
            ep_match = re.search(rf"-{season}x(\d+)/?", direct_url_clean, re.IGNORECASE)
            if ep_match and int(ep_match.group(1)) == episode:
                ep_url = direct_url_clean
            elif ep_match:
                # Replace episode number in pattern to match requested episode
                ep_url = re.sub(rf"-{season}x\d+/?", f"-{season}x{episode}/", direct_url_clean)
            else:
                ep_url = direct_url_clean
        elif "/series" in direct_url_clean:
            ep_map = get_animedekho_episode_map(show_name, season, series_url=direct_url_clean)
            ep_url = ep_map.get(episode, "")

    if not ep_url:
        ep_map = get_animedekho_episode_map(show_name, season)
        ep_url = ep_map.get(episode, "")

    if not ep_url:
        c_name = re.sub(r'[\s\ufffd\u2013\u2014\-–—]+(Episode\s*\d+\s*Added!|All\s*Episodes\s*Added!).*$', '', show_name, flags=re.IGNORECASE)
        c_name = re.sub(r'\s*(season\s*\d+|s\d+)\s*$', '', c_name, flags=re.IGNORECASE).strip()
        c_name = c_name.replace('\ufffdm', "'m").replace('\ufffds', "'s").replace('Fianc\ufffd', 'Fiance')
        c_name = re.sub(r'&#8211;|–|—', '-', c_name)
        c_name = re.sub(r'&#8217;|’|\ufffd', "'", c_name)
        slug = re.sub(r'[^a-z0-9]+', '-', c_name.lower()).strip('-')

        candidates = [slug, f"{slug}-hindi"]
        if direct_url and ("/series-hindi/" in direct_url or "/series/" in direct_url):
            dir_slug = direct_url.strip("/").split("/")[-1]
            if dir_slug not in candidates:
                candidates.insert(0, dir_slug)
        if "thousand-year" in slug or "tybw" in slug or "blood-war" in slug:
            candidates.insert(0, "bleach-thousand-year-blood-war")
        elif "bleach" in slug and "bleach" not in candidates:
            candidates.append("bleach")
        if "lord-of-mysteries" in slug and "lord-of-mysteries" not in candidates:
            candidates.append("lord-of-mysteries")
        if "fiance" in slug or "fianc" in slug:
            candidates.append("even-though-im-a-super-timid-noble-girl-i-accepted-the-bet-from-my-cunning-fiance")
            candidates.append("even-though-im-a-super-timid-noble-girl-i-accepted-the-bet-from-my-cunning-fianc")
        if "dating-sim" in slug:
            candidates.insert(0, "trapped-in-a-dating-sim-the-world-of-otome-games-is-tough-for-mobs")

        for s in candidates:
            seasons_to_test = [season]
            if "thousand-year" in s or "tybw" in s:
                if 2 not in seasons_to_test:
                    seasons_to_test.append(2)
            for sn in seasons_to_test:
                candidate = f"{base}/epi/{s}-{sn}x{episode}/"
                try:
                    r_test = _animedekho_session.get(candidate, timeout=20)
                    if r_test.status_code == 200 and len(r_test.text) > 1000 and "<title>404" not in r_test.text:
                        ep_url = candidate
                        break
                except Exception:
                    pass
            if ep_url:
                break

    if not ep_url:
        return None

    try:
        headers = {"Referer": ep_url}
        r = _animedekho_session.get(ep_url, headers=headers, timeout=20)
        if r.status_code != 200:
            return None

        # 0. Check direct regex in raw HTML
        m_direct = re.search(r'https?://(?:www\.)?vidmoly\.[a-z0-9]+/embed-[a-zA-Z0-9]+\.html', r.text, re.I)
        if m_direct:
            return m_direct.group(0)

        soup = BeautifulSoup(r.text, "html.parser")

        # 1. Check direct iframes
        for ifr in soup.find_all("iframe"):
            src = ifr.get("src") or ifr.get("data-src") or ""
            if "vidmoly" in src.lower():
                return src

        # 2. Check data-src server options (Vidmoly tab)
        for el in soup.find_all(attrs={"data-src": True}):
            txt = el.get_text(strip=True).lower()
            d_opt = str(el.get("data-option", "")).lower()
            if "vidmoly" in txt or "moly" in txt or "vidmoly" in d_opt:
                raw_b64 = el["data-src"]
                try:
                    padded = raw_b64 + "=" * (-len(raw_b64) % 4)
                    dec = base64.b64decode(padded).decode("utf-8", errors="ignore")
                    target_url = dec if dec.startswith("http") else f"{base}{dec}"
                    r_sub = _animedekho_session.get(target_url, headers=headers, timeout=20)
                    if r_sub.status_code == 200:
                        sub_soup = BeautifulSoup(r_sub.text, "html.parser")
                        for s_ifr in sub_soup.find_all("iframe"):
                            s_src = s_ifr.get("src") or s_ifr.get("data-src") or ""
                            if "vidmoly" in s_src.lower():
                                return s_src
                        sub_m = re.search(r'https?://(?:www\.)?vidmoly\.[a-z0-9]+/embed-[a-zA-Z0-9]+\.html', r_sub.text, re.I)
                        if sub_m:
                            return sub_m.group(0)
                        sub_m2 = re.search(r'https?://[^\s"\'<>]*vidmoly\.[a-z0-9]+/(?:embed-|w/|d/)?[a-zA-Z0-9]+(?:\.html)?', r_sub.text, re.I)
                        if sub_m2:
                            return sub_m2.group(0)
                except Exception as e:
                    logger.debug("Error decoding vidmoly option data-src: %s", e)

        # 3. Check download buttons (class buttondl / button45 with Vidmoly)
        for a_dl in soup.find_all("a", href=True):
            if "vidmoly" in a_dl.get_text(strip=True).lower() and "url=" in a_dl["href"]:
                m_dl_b64 = re.search(r'url=([A-Za-z0-9+/=]+)', a_dl["href"])
                if m_dl_b64:
                    try:
                        raw_b64 = m_dl_b64.group(1)
                        padded = raw_b64 + "=" * (-len(raw_b64) % 4)
                        dec = base64.b64decode(padded).decode("utf-8", errors="ignore")
                        target_url = dec if dec.startswith("http") else f"{base}{dec}"
                        r_sub = _animedekho_session.get(target_url, headers=headers, timeout=20)
                        if r_sub.status_code == 200:
                            sub_m = re.search(r'https?://(?:www\.)?vidmoly\.[a-z0-9]+/embed-[a-zA-Z0-9]+\.html', r_sub.text, re.I)
                            if sub_m:
                                return sub_m.group(0)
                    except Exception:
                        pass

        # 4. Fallback permissive regex in raw HTML
        m_fallback = re.search(r'https?://[^\s"\'<>]*vidmoly\.[a-z0-9]+/(?:embed-|w/|d/)?[a-zA-Z0-9]+(?:\.html)?', r.text, re.I)
        if m_fallback:
            return m_fallback.group(0)

    except Exception as e:
        logger.debug("extract_animedekho_vidmoly_stream error for %s S%dE%d: %s", show_name, season, episode, e)

    return None


def count_toonstream_episodes(show_name: str, season: int = 1) -> int:
    """
    Fetch the toonstream/animedekho show page and count episode links for the specific season.
    Checks the Toonstream episode map first (most reliable), then AnimeDekho as fallback.
    """
    # Try Toonstream episode map first — most reliable source
    ep_map = _get_or_fetch_toonstream_episode_map(show_name, season)
    if ep_map:
        max_ep = max(ep_map.keys())
        logger.info(
            "Toonstream: max ep S%dE%02d (found %d links) for %s via episode map",
            season, max_ep, len(ep_map), show_name
        )
        return max_ep

    # Fall back to AnimeDekho if Toonstream map is empty
    if SERVER_UG_SOURCE == "animedekho":
        try:
            ad_cnt = count_animedekho_episodes(show_name, season)
            if ad_cnt > 0:
                logger.info("AnimeDekho: found %d episodes for %s S%d", ad_cnt, show_name, season)
                return ad_cnt
        except Exception as exc:
            logger.debug("AnimeDekho count check failed for %s: %s", show_name, exc)

    season_pattern = re.compile(rf"-{season}x(\d+)", re.IGNORECASE)

    def _count_from_html(html: str, source_url: str) -> int:
        soup = BeautifulSoup(html, "html.parser")
        ep_nums = [
            int(m.group(1))
            for a in soup.find_all("a", href=True)
            if "/episode/" in a["href"]
            for m in [season_pattern.search(a["href"])] if m
        ]
        if ep_nums:
            max_ep = max(ep_nums)
            logger.info(
                "Toonstream: max ep S%dE%02d (found %d links) for %s on %s",
                season, max_ep, len(set(ep_nums)), show_name, source_url,
            )
            return max_ep
        return 0

    slug_candidates = _get_toonstream_slug_candidates(show_name)
    primary_domain = TOONSTREAM_DOMAINS[0] if TOONSTREAM_DOMAINS else "toon-stream.site"

    clean_name = re.sub(r'\s*(season\s*\d+|s\d+)\s*$', '', show_name, flags=re.IGNORECASE).strip()
    slug_base = re.sub(r'[^a-z0-9]+', '-', clean_name.lower()).strip('-')
    expanded_slugs = list(slug_candidates)
    if f"{slug_base}-{season}" not in expanded_slugs:
        expanded_slugs.insert(0, f"{slug_base}-{season}")
    if f"{slug_base}" not in expanded_slugs:
        expanded_slugs.insert(0, f"{slug_base}")

    candidate_urls = []
    for slug in expanded_slugs[:15]:
        candidate_urls.extend([
            f"https://{primary_domain}/series/{slug}/season/{season}",
            f"https://{primary_domain}/series/{slug}/",
            f"https://{primary_domain}/anime/{slug}/",
        ])

    # Direct requests (FlareSolverr disabled — sites no longer need CF bypass)
    for show_url in candidate_urls:
        try:
            r = _toonstream_get(show_url, timeout=6)
            if r.status_code == 200:
                cnt = _count_from_html(r.text, show_url)
                if cnt > 0:
                    return cnt
        except Exception:
            pass

    # If Toonstream returned 0 and primary was toonstream, check AnimeDekho as fallback
    if SERVER_UG_SOURCE != "animedekho":
        try:
            ad_cnt = count_animedekho_episodes(show_name, season)
            if ad_cnt > 0:
                logger.info("Toonstream returned 0 eps, AnimeDekho fallback found %d for %s S%d", ad_cnt, show_name, season)
                return ad_cnt
        except Exception:
            pass

    return 0


# Alias for unified Server UG episode counting
count_ug_episodes = count_toonstream_episodes



_toonstream_series_cache: dict[tuple[str, int], str] = {}
_toonstream_episodes_map_cache: dict[tuple[str, int], dict[int, str]] = {}


def _get_or_fetch_toonstream_episode_map(show_name: str, season: int) -> dict[int, str]:
    """
    Fetch the series/season page once and extract exact URL links for all episodes of the season.
    Caches the mapping {ep_num: full_episode_url} so all subsequent episode extractions
    hit the exact episode page directly without guessing or probing.
    Handles paginated episode lists (page=2, page=3...) for long-running shows.
    """
    cache_key = (show_name.lower().strip(), season)
    if cache_key in _toonstream_episodes_map_cache:
        return _toonstream_episodes_map_cache[cache_key]

    ep_map: dict[int, str] = {}
    base_urls = list(_toonstream_search_series_urls(show_name))
    if not base_urls:
        slug_cands = _get_toonstream_slug_candidates(show_name)
        for domain in TOONSTREAM_DOMAINS:
            for slug in slug_cands[:3]:
                base_urls.append(f"https://{domain}/series/{slug}/")
                base_urls.append(f"https://{domain}/anime/{slug}/")

    ep_pattern = re.compile(rf"-{season}x(\d+)(?:[^0-9]|$)", re.I)

    def _scrape_ep_links(html: str, domain: str):
        """Extract episode links from HTML and add to ep_map."""
        found_any = False
        pg_soup = BeautifulSoup(html, "html.parser")
        for a in pg_soup.find_all("a", href=True):
            href = a["href"]
            if "/episode/" in href:
                m = ep_pattern.search(href)
                if m:
                    ep_num = int(m.group(1))
                    full_ep = href if href.startswith("http") else f"https://{domain}{href}"
                    if ep_num not in ep_map:
                        ep_map[ep_num] = full_ep
                        found_any = True
        return found_any

    for base_url in base_urls:
        domain = urlparse(base_url).netloc or (TOONSTREAM_DOMAINS[0] if TOONSTREAM_DOMAINS else "toon-stream.site")
        season_url = base_url.rstrip("/") + f"/season/{season}"
        for s_url in [season_url, base_url]:
            try:
                sr = _toonstream_get(s_url, timeout=10)
                if sr.status_code != 200:
                    continue
                _scrape_ep_links(sr.text, domain)
                if ep_map:
                    _toonstream_series_cache[cache_key] = s_url
                    # Check for additional pages (page=2, page=3...) for long shows
                    for page_num in range(2, 50):
                        sep = "&" if "?" in s_url else "?"
                        page_url = f"{s_url.rstrip('/')}{sep}page={page_num}"
                        try:
                            pg_r = _toonstream_get(page_url, timeout=10)
                            if pg_r.status_code != 200:
                                break
                            if not _scrape_ep_links(pg_r.text, domain):
                                break  # No new episodes on this page
                        except Exception:
                            break
                    _toonstream_episodes_map_cache[cache_key] = ep_map
                    return ep_map
            except Exception:
                pass

    _toonstream_episodes_map_cache[cache_key] = ep_map
    return ep_map



def _resolve_toonstream_option_iframes(
    option_divs: list, ep_url: str
) -> tuple[list, list]:
    """
    Given a list of BeautifulSoup divs (or the full soup) from a Toonstream episode page,
    fetch all /embed/ option iframes in parallel, resolve the inner AS-CDN / rubystm / satoru.one
    URLs, and return (as_cdn_urls, mg_urls).

    Priority for Server UG:
      1. AS-CDN (as-cdn*.top) — preferred
      2. RubyStm / RubyVidHub — secondary fallback, converted to rubyvidhub format
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
    from urllib.parse import urlparse as _urlparse

    as_cdn_urls: list = []  # primary Server UG (AS-CDN)
    ruby_urls: list = []    # secondary Server UG (rubystm -> rubyvidhub)
    mg_urls: list = []      # Server MG (satoru.one)
    pg_urls: list = []      # Server PG (vidara.to / vidara.so)

    domain = _urlparse(ep_url).netloc if ep_url.startswith("http") else "toon-stream.site"

    iframe_srcs: list = []
    for div in option_divs:
        for iframe in div.find_all("iframe"):
            src = iframe.get("src") or iframe.get("data-src") or ""
            if src and src not in iframe_srcs:
                iframe_srcs.append(src)

    if not iframe_srcs:
        return (as_cdn_urls, mg_urls, pg_urls)

    def _classify_and_add(url: str, skip_alive_check: bool = False):
        u_clean = url.strip()
        if "as-cdn" in u_clean or "vexal.top" in u_clean or "ravok.buzz" in u_clean:
            # Normalize old as-cdn / vexal domains to ravok.buzz
            norm_u = re.sub(r'https?://(?:as-cdn\d*\.top|vexal\.top)', 'https://ravok.buzz', u_clean, flags=re.IGNORECASE)
            norm_u = re.sub(r'(?:as-cdn\d*\.top|vexal\.top)', 'ravok.buzz', norm_u, flags=re.IGNORECASE)
            # When URL comes directly from data-src on the episode page, skip the alive
            # check — it's always valid and the check adds unnecessary latency.
            if skip_alive_check or is_as_cdn_alive(norm_u):
                return ("as_cdn", norm_u)
        elif "rubystm.com" in u_clean or "rubyvidhub.com" in u_clean:
            converted = convert_ruby_url(u_clean)
            if converted:
                return ("ruby", converted)
        elif "satoru.one" in u_clean or "embed.satoru" in u_clean:
            return ("mg", u_clean)
        elif "vidara.to" in u_clean or "vidara.so" in u_clean or re.search(r'vidara\.[a-z]{2,}/[ev]/', u_clean, re.I):
            norm_vid = re.sub(r'/v/([a-zA-Z0-9]+)', r'/e/\1', u_clean)
            return ("pg", norm_vid)
        return (None, None)

    def _fetch_one(src: str):
        full_url = src if src.startswith("http") else f"https://{domain}{src}"

        # 1. Direct check: src itself is already an AS-CDN, Ruby, Satoru, or Vidara player URL
        #    Skip alive check when the URL comes directly from data-src (always valid).
        kind, res = _classify_and_add(full_url, skip_alive_check=("as-cdn" in full_url or "vexal.top" in full_url or "ravok.buzz" in full_url))
        if kind and res:
            return (kind, res)

        # 2. Intermediate embed wrapper: fetch page and extract inner iframe or script player
        try:
            r = requests.get(full_url, headers=HEADERS, timeout=10)
            if r.status_code == 200:
                inner_soup = BeautifulSoup(r.text, "html.parser")
                for inner_iframe in inner_soup.find_all("iframe"):
                    isrc = inner_iframe.get("src") or inner_iframe.get("data-src") or ""
                    kind, res = _classify_and_add(isrc)
                    if kind and res:
                        return (kind, res)

                # Check for URLs directly embedded in page text / scripts
                for m in re.finditer(r'https?://[^\s"\'<>]*(?:as-cdn[^\s"\'<>]*|vexal\.top[^\s"\'<>]*|ravok\.buzz[^\s"\'<>]*|rubystm\.com[^\s"\'<>]*|rubyvidhub\.com[^\s"\'<>]*|satoru\.one[^\s"\'<>]*|vidara\.[^\s"\'<>]*)', r.text):
                    kind, res = _classify_and_add(m.group(0))
                    if kind and res:
                        return (kind, res)
        except Exception:
            pass
        return (None, None)

    # Limit concurrency to 4 — Toonstream rate-limits under high parallel load,
    # causing episodes to fail when too many requests are sent simultaneously.
    with ThreadPoolExecutor(max_workers=min(len(iframe_srcs), 4)) as executor:
        futures = [executor.submit(_fetch_one, s) for s in iframe_srcs]
        for fut in _as_completed(futures):
            kind, url = fut.result()
            if kind == "as_cdn" and url and url not in as_cdn_urls:
                as_cdn_urls.append(url)
            elif kind == "ruby" and url and url not in ruby_urls:
                ruby_urls.append(url)
            elif kind == "mg" and url and url not in mg_urls:
                mg_urls.append(url)
            elif kind == "pg" and url and url not in pg_urls:
                pg_urls.append(url)

    # Return as_cdn_urls as primary; if empty use ruby_urls as fallback Server UG
    final_ug = as_cdn_urls if as_cdn_urls else ruby_urls
    return (final_ug, mg_urls, pg_urls)




def extract_watchanimeworld_stream(
    show_name: str,
    season: int = 1,
    episode: int = 1,
    is_movie: bool = False,
) -> Optional[str]:
    """
    Extract Zephyrix video ID from watchanimeworld.one, construct worker URL,
    and return base64 encrypted frosty URL for Server UG.
    """
    slug_candidates = _get_toonstream_slug_candidates(show_name)
    candidate_urls = []

    for slug in slug_candidates[:4]:
        if is_movie:
            candidate_urls.extend([
                f"https://watchanimeworld.one/movie/{slug}/",
                f"https://watchanimeworld.one/anime/{slug}/",
                f"https://watchanimeworld.one/{slug}/",
            ])
        else:
            candidate_urls.extend([
                f"https://watchanimeworld.one/episode/{slug}-{season}x{episode}/",
                f"https://watchanimeworld.one/episode/{slug}-s{season}-e{episode}/",
                f"https://watchanimeworld.one/episode/{slug}-season-{season}-episode-{episode}/",
                f"https://watchanimeworld.one/episode/{slug}-episode-{episode}/",
            ])

    headers = {"User-Agent": HEADERS["User-Agent"]}
    for ep_url in candidate_urls:
        try:
            resp = requests.get(ep_url, headers=headers, timeout=5)
            if resp.status_code == 200:
                zeph_ids = re.findall(r'zephyrix\.org/video/([a-f0-9]+)', resp.text)
                if zeph_ids:
                    video_id = zeph_ids[0]
                    worker_url = f"{WORKER_BASE}video/{video_id}"
                    logger.info("WatchAnimeWorld extracted Server UG video ID %s for %s S%dE%02d -> %s", video_id, show_name, season, episode, worker_url)
                    return encrypt_iframe_url(f"{show_name} S{season}E{episode} - Server UG", worker_url)
        except Exception as exc:
            logger.debug("WatchAnimeWorld attempt failed for %s: %s", ep_url, exc)

    logger.warning("WatchAnimeWorld search returned None for %s S%dE%02d", show_name, season, episode)
    return None


_animesalt_show_cache: dict[str, str | bool] = {}

def extract_as_cdn_from_animesalt(
    show_name: str,
    season: int = 1,
    episode: int = 1,
    is_movie: bool = False,
) -> Optional[str]:
    """
    Fallback extractor: fetch AS-CDN stream URL from animesalt.me.
    Only returns actual as-cdn embed URLs (Server UG). Returns None otherwise.
    """
    cache_key = show_name.lower().strip()
    if _animesalt_show_cache.get(cache_key) is False:
        return None

    # Check cached show URL
    cached_href = _animesalt_show_cache.get(cache_key)
    series_url = cached_href if isinstance(cached_href, str) and cached_href else None

    if not series_url:
        kw = re.sub(r'\s*(season\s*\d+|s\d+)\s*$', '', show_name, flags=re.IGNORECASE).strip()
        search_url = f"https://animesalt.me/?s={kw.replace(' ', '+')}"
        try:
            r = requests.get(search_url, headers=HEADERS, timeout=6)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                series_links = []
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if "animesalt.me" in href and ("/tv/" in href or "/series/" in href or "/movie/" in href or "/movies/" in href):
                        title = a.get_text(strip=True) or a.get("title", "")
                        if (href, title) not in series_links:
                            series_links.append((href, title))

                if series_links:
                    from rapidfuzz import fuzz as _fuzz
                    best_href = None
                    best_score = 0
                    for href, title in series_links:
                        score = _fuzz.token_sort_ratio(show_name.lower(), title.lower()) if title else 50
                        if score > best_score:
                            best_score = score
                            best_href = href
                    series_url = best_href or series_links[0][0]
                    _animesalt_show_cache[cache_key] = series_url
                else:
                    _animesalt_show_cache[cache_key] = False
                    return None
        except Exception as exc:
            logger.debug("AnimeSalt.me search error for %s: %s", show_name, exc)
            return None

    if not series_url:
        return None

    # Fetch show page and look for AS-CDN links in triggerEpisode JSON or iframes
    try:
        sr = requests.get(series_url, headers=HEADERS, timeout=6)
        if sr.status_code == 200:
            ssoup = BeautifulSoup(sr.text, "html.parser")
            target_slug = f"ep-{episode}"

            for div in ssoup.find_all("div", class_="ep-tile"):
                data_slug = div.get("data-slug", "")
                onclick = div.get("onclick", "")
                if data_slug == target_slug or f"Episode {episode}" in onclick:
                    m = re.search(r'triggerEpisode\((\[.*?\])\s*,', onclick)
                    if m:
                        json_str = m.group(1).replace(r'\/', '/')
                        try:
                            servers = json.loads(json_str)
                            for s in servers:
                                u = s.get("url", "")
                                # ONLY return AS-CDN URLs — reject all other players
                                if u and "as-cdn" in u:
                                    if is_as_cdn_alive(u):
                                        logger.info("AnimeSalt.me: extracted AS-CDN for %s S%dE%02d -> %s", show_name, season, episode, u)
                                        return u
                        except Exception:
                            pass

            # iframe fallback — AS-CDN only
            for iframe in ssoup.find_all("iframe"):
                src = iframe.get("src") or iframe.get("data-src") or ""
                if src and "as-cdn" in src:
                    if is_as_cdn_alive(src):
                        logger.info("AnimeSalt.me (iframe): extracted AS-CDN for %s S%dE%02d -> %s", show_name, season, episode, src)
                        return src
    except Exception as exc:
        logger.debug("AnimeSalt.me show page error for %s S%dE%d: %s", show_name, season, episode, exc)

    return None


def extract_ruby_url_from_toonstream(show_name: str, season: int = 1, episode: int = 1, direct_url: str = "", is_movie: bool = False) -> Optional[str]:
    """Extract Ruby server iframe from Toonstream/AnimeDekho episode or movie page."""
    if SERVER_UG_SOURCE == "animedekho" or ("animedekho" in direct_url):
        try:
            ad_stream = extract_animedekho_stream(
                show_name, season=season, episode=episode,
                direct_url=direct_url if ("animedekho" in direct_url) else ""
            )
            if ad_stream:
                logger.info("Extracted Server UG stream URL from AnimeDekho: %s", ad_stream)
                return ad_stream
        except Exception as exc:
            logger.debug("AnimeDekho stream extraction failed for %s S%dE%d: %s", show_name, season, episode, exc)

    slug_candidates = _get_toonstream_slug_candidates(show_name)

    candidate_urls = []
    if direct_url:
        direct_url = direct_url.strip()
        # If user passed a specific episode URL with matching -SxE format, use directly
        if "/episode/" in direct_url:
            m_ep = re.search(r'-(\d+)x(\d+)(?:[^0-9]|$)', direct_url, re.I)
            if m_ep:
                # If the episode URL matches target episode, put at front
                if int(m_ep.group(1)) == season and int(m_ep.group(2)) == episode:
                    candidate_urls.append(direct_url)
                else:
                    # Target episode is different — construct URL with target episode number
                    target_ep_url = re.sub(r'-\d+x\d+(?=[^0-9]|$)', f'-{season}x{episode}', direct_url)
                    candidate_urls.append(target_ep_url)
            else:
                candidate_urls.append(direct_url)
        elif "/series/" in direct_url or "/season/" in direct_url or "/anime/" in direct_url:
            # User passed a series/season URL — extract target episode link from series page
            try:
                sr = _toonstream_get(direct_url, timeout=5)
                if sr.status_code == 200:
                    sr_soup = BeautifulSoup(sr.text, "html.parser")
                    ep_pat = re.compile(rf"-{season}x{episode}(?:[^0-9]|$)", re.IGNORECASE)
                    for a in sr_soup.find_all("a", href=True):
                        href = a["href"]
                        if "/episode/" in href and ep_pat.search(href):
                            domain = urlparse(direct_url).netloc
                            full_url = href if href.startswith("http") else f"https://{domain}{href}"
                            candidate_urls.append(full_url)
                            break
            except Exception:
                pass
        else:
            candidate_urls.append(direct_url)

    # ── Pre-pass: discover actual episode URL from series page ───────────────────────
    if not is_movie:
        ep_pattern = re.compile(rf"-{season}x{episode}(?:[^0-9]|$)", re.IGNORECASE)
        cache_key = (show_name.lower(), season)

        # Check if we already found the working series base URL for this show
        cached_series_url = _toonstream_series_cache.get(cache_key)
        if cached_series_url:
            try:
                sr = _toonstream_get(cached_series_url, timeout=4)
                if sr.status_code == 200:
                    sr_soup = BeautifulSoup(sr.text, "html.parser")
                    for a in sr_soup.find_all("a", href=True):
                        href = a["href"]
                        if "/episode/" in href and ep_pattern.search(href):
                            domain = urlparse(cached_series_url).netloc
                            full = href if href.startswith("http") else f"https://{domain}{href}"
                            if full not in candidate_urls:
                                candidate_urls.insert(0, full)
                            break
            except Exception:
                pass

        # ── Live-search pre-pass via AJAX API (handles colons, subtitles, suffix words) ─
        if not candidate_urls:
            for _base_url in _toonstream_search_series_urls(show_name):
                _d = urlparse(_base_url).netloc
                for _s_url in [f"{_base_url.rstrip('/')}/season/{season}", _base_url]:
                    try:
                        _sr = _toonstream_get(_s_url, timeout=4)
                        if _sr.status_code != 200:
                            continue
                        _sr_soup = BeautifulSoup(_sr.text, "html.parser")
                        _found_ep = False
                        for _a in _sr_soup.find_all("a", href=True):
                            _href = _a["href"]
                            if "/episode/" in _href and ep_pattern.search(_href):
                                _full_ep = _href if _href.startswith("http") else f"https://{_d}{_href}"
                                if _full_ep not in candidate_urls:
                                    candidate_urls.insert(0, _full_ep)
                                _found_ep = True
                                break
                        if _found_ep:
                            _toonstream_series_cache[cache_key] = _s_url
                            break
                    except Exception:
                        pass
                if candidate_urls:
                    break

        # ── Slug-guessing fallback ────────────────────────────────────────────────────
        if not candidate_urls:
            for domain in TOONSTREAM_DOMAINS:
                if not _domain_ok(domain):
                    continue
                for slug in slug_candidates[:4]:  # limit to top 4 slugs for speed
                    for series_path in [
                        f"/series/{slug}/season/{season}",
                        f"/series/{slug}/",
                        f"/anime/{slug}/season/{season}",
                        f"/anime/{slug}/",
                    ]:
                        s_url = f"https://{domain}{series_path}"
                        try:
                            sr = _toonstream_get(s_url, timeout=4)
                            if sr.status_code != 200:
                                continue
                            sr_soup = BeautifulSoup(sr.text, "html.parser")
                            found_ep = False
                            for a in sr_soup.find_all("a", href=True):
                                href = a["href"]
                                if "/episode/" in href and ep_pattern.search(href):
                                    full = href if href.startswith("http") else f"https://{domain}{href}"
                                    if full not in candidate_urls:
                                        candidate_urls.insert(0, full)
                                    found_ep = True
                                    break
                            if found_ep:
                                _toonstream_series_cache[cache_key] = s_url
                                break
                        except Exception:
                            pass
                    if candidate_urls:
                        break
                if candidate_urls:
                    break

    seen_urls = set(candidate_urls)
    for domain in TOONSTREAM_DOMAINS:
        if not _domain_ok(domain):
            continue
        for slug in slug_candidates[:6]:
            if is_movie:
                cands = [
                    f"https://{domain}/movie/{slug}/",
                    f"https://{domain}/movies/{slug}/",
                    f"https://{domain}/anime/{slug}/",
                    f"https://{domain}/{slug}/",
                ]
            else:
                ep_slug = f"{slug}-{season}x{episode}"
                cands = [f"https://{domain}/episode/{ep_slug}/"]

            for u in cands:
                if u not in seen_urls:
                    seen_urls.add(u)
                    candidate_urls.append(u)

    for ep_url in candidate_urls:
        # Derive domain from URL for dead-domain tracking
        _ep_domain = ep_url.split("/")[2] if ep_url.startswith("https://") else ""
        try:
            resp = _toonstream_get(ep_url, timeout=10)
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            option_divs = soup.find_all("div", id=re.compile(r"option|player", re.I))
            if not option_divs:
                option_divs = [soup]

            final_ug_urls, mg_urls, pg_urls = _resolve_toonstream_option_iframes(option_divs, ep_url)
            ug_url = final_ug_urls[0] if final_ug_urls else None
            mg_url = mg_urls[0] if mg_urls else None
            pg_url = pg_urls[0] if pg_urls else None

            if ug_url or mg_url or pg_url:
                if ug_url:
                    logger.info("Extracted Server UG stream URL: %s", ug_url)
                if mg_url:
                    logger.info("Extracted Server MG (satoru.one) stream URL: %s", mg_url)
                if pg_url:
                    logger.info("Extracted Server PG (vidara.to) stream URL: %s", pg_url)
                return ug_url
        except requests.exceptions.ConnectTimeout:
            continue
        except Exception as exc:
            logger.debug("Toonstream attempt failed for %s: %s", ep_url, exc)

    # If Toonstream failed to find a working AS-CDN link, fall back to AnimeDekho then AnimeSalt.me
    if SERVER_UG_SOURCE != "animedekho":
        logger.info("Toonstream AS-CDN search returned None for %s S%dE%02d, checking AnimeDekho fallback...", show_name, season, episode)
        try:
            ad_stream = extract_animedekho_stream(show_name, season=season, episode=episode)
            if ad_stream:
                return ad_stream
        except Exception:
            pass

    logger.info("Toonstream AS-CDN search returned None for %s S%dE%02d, checking AnimeSalt.me fallback...", show_name, season, episode)
    return extract_as_cdn_from_animesalt(show_name, season=season, episode=episode, is_movie=is_movie)


def extract_toonstream_all_servers(
    show_name: str,
    season: int = 1,
    episode: int = 1,
    direct_url: str = "",
    is_movie: bool = False,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Extract Server UG (as-cdn/ruby), Server MG (satoru.one), and Server PG (vidara.to) stream URLs from Toonstream.
    Returns (ug_url, mg_url, pg_url) tuple.
    """
    slug_candidates = _get_toonstream_slug_candidates(show_name)
    candidate_urls = []
    if direct_url:
        direct_url = direct_url.strip()
        if "/episode/" in direct_url:
            m_ep = re.search(r'-(\d+)x(\d+)(?:[^0-9]|$)', direct_url, re.I)
            if m_ep:
                if int(m_ep.group(1)) == season and int(m_ep.group(2)) == episode:
                    candidate_urls.append(direct_url)
                else:
                    target_ep_url = re.sub(r'-\d+x\d+(?=[^0-9]|$)', f'-{season}x{episode}', direct_url)
                    candidate_urls.append(target_ep_url)
            else:
                candidate_urls.append(direct_url)
        elif "/series/" in direct_url or "/season/" in direct_url or "/anime/" in direct_url:
            try:
                sr = _toonstream_get(direct_url, timeout=5)
                if sr.status_code == 200:
                    sr_soup = BeautifulSoup(sr.text, "html.parser")
                    ep_pat = re.compile(rf"-{season}x{episode}(?:[^0-9]|$)", re.IGNORECASE)
                    for a in sr_soup.find_all("a", href=True):
                        href = a["href"]
                        if "/episode/" in href and ep_pat.search(href):
                            domain = urlparse(direct_url).netloc
                            full_url = href if href.startswith("http") else f"https://{domain}{href}"
                            candidate_urls.append(full_url)
                            break
            except Exception:
                pass
        else:
            candidate_urls.append(direct_url)

    if not is_movie:
        # 1. Use pre-built episode map if available (direct hit, zero guessing)
        ep_map = _get_or_fetch_toonstream_episode_map(show_name, season)
        if ep_map and episode in ep_map:
            candidate_urls.insert(0, _encode_url_path(ep_map[episode]))

        ep_pattern = re.compile(rf"-{season}x{episode}(?:[^0-9]|$)", re.IGNORECASE)
        cache_key = (show_name.lower(), season)
        cached_series_url = _toonstream_series_cache.get(cache_key)
        if cached_series_url:
            try:
                sr = _toonstream_get(cached_series_url, timeout=4)
                if sr.status_code == 200:
                    sr_soup = BeautifulSoup(sr.text, "html.parser")
                    for a in sr_soup.find_all("a", href=True):
                        href = a["href"]
                        if "/episode/" in href and ep_pattern.search(href):
                            domain = urlparse(cached_series_url).netloc
                            full = href if href.startswith("http") else f"https://{domain}{href}"
                            if full not in candidate_urls:
                                candidate_urls.insert(0, full)
                            break
            except Exception:
                pass

        if not candidate_urls:
            for _base_url in _toonstream_search_series_urls(show_name):
                _d = urlparse(_base_url).netloc
                for _s_url in [f"{_base_url.rstrip('/')}/season/{season}", _base_url]:
                    try:
                        _sr = _toonstream_get(_s_url, timeout=4)
                        if _sr.status_code != 200:
                            continue
                        _sr_soup = BeautifulSoup(_sr.text, "html.parser")
                        _found_ep = False
                        for _a in _sr_soup.find_all("a", href=True):
                            _href = _a["href"]
                            if "/episode/" in _href and ep_pattern.search(_href):
                                _full_ep = _href if _href.startswith("http") else f"https://{_d}{_href}"
                                if _full_ep not in candidate_urls:
                                    candidate_urls.insert(0, _full_ep)
                                _found_ep = True
                                break
                        if _found_ep:
                            _toonstream_series_cache[cache_key] = _s_url
                            break
                    except Exception:
                        pass
                if candidate_urls:
                    break

        if not candidate_urls:
            for domain in TOONSTREAM_DOMAINS:
                if not _domain_ok(domain):
                    continue
                for slug in slug_candidates[:4]:
                    for series_path in [
                        f"/series/{slug}/season/{season}",
                        f"/series/{slug}/",
                        f"/anime/{slug}/season/{season}",
                        f"/anime/{slug}/",
                    ]:
                        s_url = f"https://{domain}{series_path}"
                        try:
                            sr = _toonstream_get(s_url, timeout=4)
                            if sr.status_code != 200:
                                continue
                            sr_soup = BeautifulSoup(sr.text, "html.parser")
                            found_ep = False
                            for a in sr_soup.find_all("a", href=True):
                                href = a["href"]
                                if "/episode/" in href and ep_pattern.search(href):
                                    full = href if href.startswith("http") else f"https://{domain}{href}"
                                    if full not in candidate_urls:
                                        candidate_urls.insert(0, full)
                                    found_ep = True
                                    break
                            if found_ep:
                                _toonstream_series_cache[cache_key] = s_url
                                break
                        except Exception:
                            pass
                    if candidate_urls:
                        break
                if candidate_urls:
                    break

    for domain in TOONSTREAM_DOMAINS:
        if not _domain_ok(domain):
            continue
        for slug in slug_candidates:
            if is_movie:
                candidate_urls.extend([
                    f"https://{domain}/movie/{slug}/",
                    f"https://{domain}/movies/{slug}/",
                    f"https://{domain}/anime/{slug}/",
                    f"https://{domain}/{slug}/",
                ])
            else:
                ep_slug = f"{slug}-{season}x{episode}"
                candidate_urls.append(f"https://{domain}/episode/{ep_slug}/")

    ug_found = None
    mg_found = None
    pg_found = None

    if SERVER_UG_SOURCE == "animedekho" or ("animedekho" in direct_url):
        try:
            ad_stream = extract_animedekho_stream(
                show_name,
                season=season,
                episode=episode,
                direct_url=direct_url if ("animedekho" in direct_url) else "",
            )
            if ad_stream:
                logger.info("Extracted Server UG stream URL from AnimeDekho: %s", ad_stream)
                ug_found = ad_stream
        except Exception as exc:
            logger.debug("AnimeDekho stream extraction failed for %s S%dE%d: %s", show_name, season, episode, exc)

    from concurrent.futures import ThreadPoolExecutor, as_completed as _outer_as_completed

    def _try_ep_url(ep_url: str):
        """Fetch one candidate episode URL and resolve its iframes. Returns (ug_url, mg_url, pg_url) or (None, None, None)."""
        try:
            resp = _toonstream_get(ep_url, timeout=10)
            if resp.status_code != 200:
                return (None, None, None)

            soup = BeautifulSoup(resp.text, "html.parser")
            option_divs = soup.find_all("div", id=re.compile(r"option|player", re.I))
            if not option_divs:
                option_divs = [soup]
            final_ug_urls, mg_urls, pg_urls = _resolve_toonstream_option_iframes(option_divs, ep_url)
            return (final_ug_urls[0] if final_ug_urls else None,
                    mg_urls[0]      if mg_urls      else None,
                    pg_urls[0]      if pg_urls      else None)
        except requests.exceptions.ConnectTimeout:
            pass
        except Exception as exc:
            logger.debug("Toonstream attempt failed for %s: %s", ep_url, exc)
        return (None, None, None)

    # Deduplicate while preserving order
    seen_urls: set = set()
    unique_candidates = []
    for u in candidate_urls:
        if u not in seen_urls:
            seen_urls.add(u)
            unique_candidates.append(u)

    max_workers = min(len(unique_candidates), 8)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_try_ep_url, u): u for u in unique_candidates}
        for fut in _outer_as_completed(futures):
            ug_url, mg_url, pg_url = fut.result()
            if ug_url and not ug_found:
                ug_found = ug_url
            if mg_url and not mg_found:
                mg_found = mg_url
            if pg_url and not pg_found:
                pg_found = pg_url
            if ug_found and mg_found and pg_found:
                # Cancel remaining futures early
                for f in futures:
                    f.cancel()
                break

    if not ug_found:
        if SERVER_UG_SOURCE != "animedekho":
            logger.info("Toonstream AS-CDN search returned None for %s S%dE%02d, checking AnimeDekho fallback...", show_name, season, episode)
            try:
                ad_stream = extract_animedekho_stream(show_name, season=season, episode=episode)
                if ad_stream:
                    ug_found = ad_stream
            except Exception:
                pass

    if not ug_found:
        logger.info("Toonstream & AnimeDekho returned None for %s S%dE%02d, checking AnimeSalt.me fallback...", show_name, season, episode)
        ug_found = extract_as_cdn_from_animesalt(show_name, season=season, episode=episode, is_movie=is_movie)

    return (ug_found, mg_found, pg_found)



# ── Satoru.one (Server MG) direct resolver (CF Clearance Session) ──────────────

_satoru_show_cache: dict[str, tuple[int, str] | None] = {}
_SATORU_SESSION = None
_SATORU_SESSION_EXPIRY = 0
_SATORU_SESSION_LOCK = __import__('threading').Lock()


def _satoru_get_session():
    """
    Acquire an authenticated curl_cffi session for satoru.one with cf_clearance & sv_pass.
    Launches headless browser once off-screen to solve Turnstile & visitor challenges,
    then caches the session for 30 minutes.
    """
    global _SATORU_SESSION, _SATORU_SESSION_EXPIRY
    import time
    from curl_cffi import requests

    now = time.time()
    if _SATORU_SESSION is not None and now < _SATORU_SESSION_EXPIRY:
        return _SATORU_SESSION

    with _SATORU_SESSION_LOCK:
        if _SATORU_SESSION is not None and now < _SATORU_SESSION_EXPIRY:
            return _SATORU_SESSION

        logger.info("Satoru.one: initializing local CF clearance session...")
        cookie_dict = {}
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

        # 1. Try Playwright first (fast & reliable)
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                b = p.chromium.launch(headless=True)
                page = b.new_page()
                page.goto('https://satoru.one/home', timeout=15000)
                page.wait_for_timeout(3500)
                cookies = page.context.cookies()
                ua = page.evaluate("navigator.userAgent")
                b.close()
                cookie_dict = {c['name']: c['value'] for c in cookies}
        except Exception as exc:
            logger.debug("Satoru.one Playwright session error: %s", exc)

        # 2. Try undetected_chromedriver fallback
        if not cookie_dict:
            try:
                import undetected_chromedriver as uc
                options = uc.ChromeOptions()
                options.add_argument("--headless=new")
                driver = uc.Chrome(options=options)
                driver.get("https://satoru.one/home")
                time.sleep(5)
                cookies = driver.get_cookies()
                ua = driver.execute_script("return navigator.userAgent;")
                driver.quit()
                cookie_dict = {c['name']: c['value'] for c in cookies}
            except Exception as exc:
                logger.debug("Satoru.one undetected_chromedriver session error: %s", exc)

        if not cookie_dict:
            logger.warning("Satoru.one: failed to acquire CF clearance cookies.")
            return None

        s = requests.Session(impersonate="chrome124")
        for k, v in cookie_dict.items():
            s.cookies.set(k, v, domain=".satoru.one")
        s.headers.update({
            "User-Agent": ua,
            "Referer": "https://satoru.one/home",
        })
        _SATORU_SESSION = s
        _SATORU_SESSION_EXPIRY = now + 1800  # 30 minutes
        logger.info("Satoru.one: CF session established successfully (sv_pass=%s)", 'sv_pass' in cookie_dict)
        return s


def _get_satoru_slug_candidates(show_name: str, season: int = 1) -> list[str]:
    cands = []

    variations = [show_name]
    if ":" in show_name:
        variations.append(show_name.split(":")[0])
    if any(sep in show_name for sep in ["–", "—", "-"]):
        variations.append(re.split(r"[\–\—\-]", show_name)[0])

    for var in variations:
        clean = re.sub(r'[\–\—\-:]+', ' ', var)
        clean = re.sub(r'\s+', ' ', clean).strip()

        s1 = re.sub(r'[^a-z0-9]+', '-', clean.lower()).strip('-')
        s2 = clean.lower().replace('re: zero', 'rezero').replace('re-zero', 'rezero').replace('re zero', 'rezero')
        s2 = re.sub(r'[^a-z0-9]+', '-', s2).strip('-')

        base_slugs = []
        for s in [s1, s2]:
            if s and s not in base_slugs:
                base_slugs.append(s)

        for b in base_slugs:
            if season > 1:
                cands.append(f"{b}-season-{season}")
                cands.append(f"{b}-s{season}")
            # Only try the bare slug (no season suffix) for S1.
            # For S2+, a bare-slug hit would return Season 1 content — wrong show entirely.
            if season == 1:
                cands.append(b)

    res = []
    for c in cands:
        if c and c not in res:
            res.append(c)
    return res


def _satoru_search_show(show_name: str, season: int = 1) -> tuple[int, str] | None:
    """
    Search satoru.one for a show by name / season.
    Returns (movie_id, slug) or None if not found.
    """
    cache_key = f"{show_name.lower()}_s{season}"
    if cache_key in _satoru_show_cache:
        return _satoru_show_cache[cache_key]

    s = _satoru_get_session()
    fetcher = s.get if s else requests.get

    # ── 1. Direct watch URL pre-pass (Fastest & 100% reliable) ──────────────────────
    slug_candidates = _get_satoru_slug_candidates(show_name, season=season)
    for slug in slug_candidates:
        watch_url = f"https://satoru.one/watch/{slug}"
        try:
            rw = fetcher(watch_url, timeout=8)
            if rw.status_code == 200:
                m = re.search(r'const\s+movieId\s*=\s*(\d+)', rw.text)
                if m:
                    movie_id = int(m.group(1))
                    logger.info("Satoru.one direct match: '%s' S%d (id=%d, slug=%s)", show_name, season, movie_id, slug)
                    _satoru_show_cache[cache_key] = (movie_id, slug)
                    return (movie_id, slug)
        except Exception:
            pass

    # ── 2. Suggest API search fallback ─────────────────────────────────────────────
    from rapidfuzz import fuzz as _fuzz
    clean_show = re.sub(r'\s*(season\s*\d+|s\d+)\s*$', '', show_name, flags=re.IGNORECASE).strip()
    clean_show = re.sub(r'[\–\—\-:]+', ' ', clean_show).strip()
    keywords = [clean_show, show_name]

    best_match = None
    best_score = 0

    for kw in keywords:
        try:
            kw_enc = urllib.parse.quote_plus(kw)
            r = fetcher(
                f"https://satoru.one/ajax/movie/search/suggest?keyword={kw_enc}",
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": "https://satoru.one/home"},
                timeout=8,
            )
            if r.status_code != 200:
                continue
            html = ""
            try:
                html = r.json().get("html", "")
            except Exception:
                html = r.text
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/watch/" in href:
                    slug = href.rstrip("/").split("/watch/")[-1]
                    title = a.get("title", "") or a.get_text(strip=True) or slug
                    score = _fuzz.token_sort_ratio(clean_show.lower(), title.lower())
                    if score > best_score:
                        best_score = score
                        best_match = (href, slug, title)
        except Exception as exc:
            logger.debug("Satoru suggest search error for %r: %s", kw, exc)

    if not best_match or best_score < 40:
        _satoru_show_cache[cache_key] = None
        return None

    href, slug, title = best_match

    # When looking for season > 1, reject slugs that don't contain the season number.
    if season > 1:
        season_indicators = [f"season-{season}", f"s{season}", f"season{season}"]
        if not any(ind in slug.lower() for ind in season_indicators):
            logger.debug(
                "Satoru.one: slug %r doesn't contain season %d indicator — rejecting for S%d search",
                slug, season, season,
            )
            _satoru_show_cache[cache_key] = None
            return None

    full_href = href if href.startswith("http") else f"https://satoru.one/watch/{slug}"
    try:
        rw = fetcher(full_href, timeout=8)
        m = re.search(r'const\s+movieId\s*=\s*(\d+)', rw.text)
        if not m:
            _satoru_show_cache[cache_key] = None
            return None
        movie_id = int(m.group(1))
        logger.info("Satoru.one suggest match: '%s' (id=%d, slug=%s, score=%d)", title, movie_id, slug, best_score)
        _satoru_show_cache[cache_key] = (movie_id, slug)
        return (movie_id, slug)
    except Exception as exc:
        logger.debug("Satoru watch page error for %r: %s", full_href, exc)
        _satoru_show_cache[cache_key] = None
        return None


def count_satoru_episodes(show_name: str, season: int = 1, direct_url: str = "") -> int:
    """Return total episode count on Satoru.one for show/season."""
    s = _satoru_get_session()
    fetcher = s.get if s else requests.get

    movie_id = None
    if direct_url and "satoru.one" in direct_url:
        m_slug = re.search(r'/watch/([^/?#]+)', direct_url)
        if m_slug:
            slug = m_slug.group(1)
            try:
                rw = fetcher(f"https://satoru.one/watch/{slug}", timeout=8)
                m = re.search(r'const\s+movieId\s*=\s*(\d+)', rw.text)
                if m:
                    movie_id = int(m.group(1))
            except Exception:
                pass

    if not movie_id:
        res = _satoru_search_show(show_name, season=season)
        if res:
            movie_id, _ = res

    if not movie_id:
        return 0

    try:
        r_eps = fetcher(
            f"https://satoru.one/ajax/episode/list/{movie_id}",
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": "https://satoru.one/home"},
            timeout=10,
        )
        if r_eps.status_code == 200:
            html = r_eps.json().get("html", "")
            soup = BeautifulSoup(html, "html.parser")
            items = soup.find_all("a", class_="ep-item")
            return len(items)
    except Exception:
        pass
    return 0


def resolve_satoru_episode(
    show_name: str,
    season: int = 1,
    episode: int = 1,
    direct_url: str = "",
) -> Optional[str]:
    """
    Fetch the Server MG embed URL directly from satoru.one.
    Returns the iframe embed URL (e.g. https://cdn.storox.in/v/...) or None.
    """
    s = _satoru_get_session()
    fetcher = s.get if s else requests.get

    result = None
    if direct_url and "satoru.one" in direct_url:
        m_slug = re.search(r'/watch/([^/?#]+)', direct_url)
        if m_slug:
            slug = m_slug.group(1)
            try:
                rw = fetcher(f"https://satoru.one/watch/{slug}", timeout=8)
                m = re.search(r'const\s+movieId\s*=\s*(\d+)', rw.text)
                if m:
                    movie_id = int(m.group(1))
                    result = (movie_id, slug)
            except Exception:
                pass

    if not result:
        result = _satoru_search_show(show_name, season=season)
    if not result and season > 1:
        result = _satoru_search_show(f"{show_name} Season {season}", season=season)
    # NOTE: Do NOT fall back to season=1 when season > 1.
    # satoru.one may only host S1 of a show; falling back would add wrong-season
    # content to S2/S3/S4 posts.
    if not result and season == 1:
        result = _satoru_search_show(show_name, season=1)

    if not result:
        logger.debug("Satoru.one: show not found for %r S%d", show_name, season)
        return None

    movie_id, slug = result

    ajax_headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"https://satoru.one/watch/{slug}",
    }

    try:
        # Step 3: get episode list
        r_eps = fetcher(
            f"https://satoru.one/ajax/episode/list/{movie_id}",
            headers=ajax_headers,
            timeout=10,
        )
        if r_eps.status_code != 200:
            return None

        ep_soup = BeautifulSoup(r_eps.json().get("html", ""), "html.parser")
        ep_items = ep_soup.find_all("a", class_="ep-item")

        # Find the episode matching our season+episode number
        ep_data_id = None
        for ep in ep_items:
            num = ep.get("data-number", "")
            if str(num) == str(episode):
                ep_data_id = ep.get("data-id")
                break

        if not ep_data_id:
            logger.debug("Satoru.one: ep %d not found in list for %r (id=%d)", episode, show_name, movie_id)
            return None

        # Step 4: get servers for this episode
        r_srv = fetcher(
            f"https://satoru.one/ajax/episode/servers?episodeId={ep_data_id}",
            headers=ajax_headers,
            timeout=10,
        )
        if r_srv.status_code != 200:
            return None

        srv_soup = BeautifulSoup(r_srv.json().get("html", ""), "html.parser")
        server_items = srv_soup.find_all(attrs={"data-id": True, "data-server-id": True})
        if not server_items:
            return None

        # Use the first available server
        server_data_id = server_items[0].get("data-id")

        # Step 5: get embed link
        r_src = fetcher(
            f"https://satoru.one/ajax/episode/sources?id={server_data_id}",
            headers=ajax_headers,
            timeout=10,
        )
        if r_src.status_code != 200:
            return None

        link = r_src.json().get("link", "")
        if link:
            logger.info("Satoru.one: resolved S%dE%02d for %r -> %s", season, episode, show_name, link)
            return link

    except Exception as exc:
        logger.debug("Satoru.one resolve error for %r S%dE%d: %s", show_name, season, episode, exc)

    return None


# ─────────────────────────────────────────────────────────────────
#  AniNeko (anineko.to) Embed Extractor
# ─────────────────────────────────────────────────────────────────
#  KotoTV.to — Sub/Dub iframe extractor (replaces AniNeko)
# ─────────────────────────────────────────────────────────────────

_KOTOTV_SERIES_CACHE: Dict[str, Optional[str]] = {}

_KOTOTV_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://kototv.to/",
    "Accept-Language": "en-US,en;q=0.9",
}

def _kototv_get(url: str, timeout: int = 15) -> str:
    """Simple requests fetcher for kototv.to."""
    try:
        r = requests.get(url, headers=_KOTOTV_HEADERS, timeout=timeout)
        return r.text if r.status_code == 200 else ""
    except Exception as exc:
        logger.debug("KotoTV fetch error for %s: %s", url, exc)
        return ""


def search_kototv_series(show_name: str, season: int = 1) -> Optional[str]:
    """
    Search kototv.to for an anime and return its slug.
    E.g. 'solo-leveling-season-2-arise-from-the-shadow'
    """
    import urllib.parse
    try:
        from rapidfuzz import fuzz
    except ImportError:
        fuzz = None

    cache_key = f"{show_name.lower()}_s{season}"
    if cache_key in _KOTOTV_SERIES_CACHE:
        return _KOTOTV_SERIES_CACHE[cache_key]

    clean_name = re.sub(r'\s*\(?\s*(?:season|s)\s*\d+\s*\)?', '', show_name, flags=re.I).strip()
    search_query = f"{clean_name} Season {season}" if season > 1 else clean_name

    def _extract_candidates(html_text: str):
        if not html_text:
            return []
        sp = BeautifulSoup(html_text, "html.parser")
        items = []
        seen_slugs = set()
        for c in sp.find_all("div", class_="anime-card"):
            a = c.find("a", class_="card-title") or c.find("a", href=lambda h: h and "/anime/" in h and "/watch/" not in h)
            if not a:
                continue
            href = a.get("href", "")
            m = re.search(r'/anime/([a-z0-9][a-z0-9\-]+)', href)
            if not m:
                continue
            sl = m.group(1)
            if sl in seen_slugs or sl in ("", "anime"):
                continue
            seen_slugs.add(sl)
            t = a.get("title") or a.get_text(strip=True) or sl
            items.append((sl, t))

        if not items:
            for a in sp.find_all("a", href=lambda h: h and "/anime/" in h and "/watch/" not in h):
                href = a.get("href", "")
                m = re.search(r'/anime/([a-z0-9][a-z0-9\-]+)', href)
                if not m:
                    continue
                sl = m.group(1)
                if sl in seen_slugs or sl in ("", "anime"):
                    continue
                seen_slugs.add(sl)
                t = a.get("title") or a.get_text(strip=True) or sl
                items.append((sl, t))
        return items

    search_url = f"https://kototv.to/anime?q={urllib.parse.quote_plus(search_query)}"
    html_doc = _kototv_get(search_url)
    candidates = _extract_candidates(html_doc)

    if not candidates and season > 1:
        fallback_url = f"https://kototv.to/anime?q={urllib.parse.quote_plus(clean_name)}"
        html_doc = _kototv_get(fallback_url)
        candidates = _extract_candidates(html_doc)

    if not candidates:
        _KOTOTV_SERIES_CACHE[cache_key] = None
        return None

    target_clean = _clean_title_for_slug(clean_name)
    best_slug = None
    best_score = -1

    for idx, (slug, title) in enumerate(candidates):
        slug_clean = slug.lower()
        title_clean = title.lower()

        if fuzz:
            s_title = fuzz.token_sort_ratio(search_query.lower(), title_clean)
            s_slug = fuzz.token_sort_ratio(target_clean, slug_clean)
            score = max(s_title, s_slug)
        else:
            score = sum(1 for w in target_clean.split("-") if w in slug_clean or w in title_clean) * 20

        # Position bonus (earlier items are more relevant)
        score += max(0, 15 - idx * 2)

        # Penalize side-story arcs, movies, specials unless requested
        for penalty_word in ['movie', 'arc', 'special', 'compilation', 'reawakening']:
            if penalty_word in slug_clean or penalty_word in title_clean:
                if penalty_word not in clean_name.lower():
                    score -= 20

        # Prioritize English slug if target words are directly in slug
        if all(w in slug_clean for w in target_clean.split("-") if len(w) > 2):
            score += 10

        if season > 1:
            season_patterns = [
                f"season-{season}", f"s{season}", f"season{season}",
                f"{season}nd-season", f"{season}rd-season", f"{season}th-season",
                f"part-{season}", f"part{season}", f"season {season}"
            ]
            if any(sp in slug_clean or sp in title_clean for sp in season_patterns):
                score += 30
            else:
                score -= 30
        else:
            if re.search(r'season-[2-9]|s[2-9]|part-[2-9]|season [2-9]', slug_clean + " " + title_clean):
                score -= 30

        if score > best_score:
            best_score = score
            best_slug = slug

    if best_slug and best_score >= 35:
        logger.info("KotoTV matched '%s' S%d -> slug '%s' (score %d)", show_name, season, best_slug, best_score)
        _KOTOTV_SERIES_CACHE[cache_key] = best_slug
        return best_slug

    _KOTOTV_SERIES_CACHE[cache_key] = None
    return None


def count_kototv_episodes(show_name: str, season: int = 1, direct_url: str = "") -> int:
    """Return total episode count on KotoTV for show/season."""
    if direct_url:
        u = direct_url.strip()
        slug = u.rstrip("/").split("/anime/")[-1].split("/")[0] if "/anime/" in u else ""
    else:
        slug = search_kototv_series(show_name, season)

    if not slug:
        return 0

    url = f"https://kototv.to/anime/{slug}"
    html_doc = _kototv_get(url)
    if not html_doc:
        return 0

    sp = BeautifulSoup(html_doc, "html.parser")
    eps = []
    for li in sp.find_all("li", attrs={"data-ep-int": True}):
        try:
            eps.append(int(li["data-ep-int"]))
        except ValueError:
            pass
    if not eps:
        for li in sp.find_all("li", attrs={"data-num": True}):
            try:
                eps.append(int(li["data-num"]))
            except ValueError:
                pass
    if eps:
        return max(eps)

    # Watch page fallback
    watch_url = f"https://kototv.to/anime/{slug}/watch/1"
    watch_doc = _kototv_get(watch_url)
    if watch_doc:
        sp_w = BeautifulSoup(watch_doc, "html.parser")
        for li in sp_w.find_all("li", attrs={"data-ep-int": True}):
            try:
                eps.append(int(li["data-ep-int"]))
            except ValueError:
                pass
    return max(eps) if eps else 0


def get_available_kototv_servers(
    show_name: str,
    season: int = 1,
    episode: int = 1,
    lang: str = "sub",
    direct_url: str = "",
) -> Dict[str, str]:
    """
    Fetch all available server embed URLs for a KotoTV episode.
    lang: 'sub' or 'dub'
    Returns: {"Server 1": "https://player.kototv.to/embed/{code}/sub", "Server 2": "...?server=2"}
    """
    lang = lang.lower().strip()
    if lang not in ("sub", "dub"):
        lang = "sub"

    if direct_url:
        u = direct_url.strip()
        if "/watch/" in u:
            ep_url = re.sub(r'/watch/\d+(?:/(?:sub|dub))?', f'/watch/{episode}/{lang}', u)
        elif "/anime/" in u:
            slug = u.rstrip("/").split("/anime/")[-1].split("/")[0]
            ep_url = f"https://kototv.to/anime/{slug}/watch/{episode}/{lang}"
        else:
            ep_url = u
    else:
        slug = search_kototv_series(show_name, season)
        if not slug:
            return {}
        ep_url = f"https://kototv.to/anime/{slug}/watch/{episode}/{lang}"

    html_doc = _kototv_get(ep_url)
    if not html_doc:
        return {}

    # Extract server embed URLs from onclick="switchServer(N, 'URL', this)"
    servers: Dict[str, str] = {}
    for m in re.finditer(r'onclick="switchServer\(\d+,\s*[\'"]([^\'"]+)[\'"]', html_doc):
        embed_url = m.group(1).strip()
        servers[f"Server {len(servers) + 1}"] = embed_url

    # Fallback: pick iframe src directly
    if not servers:
        m = re.search(r'<iframe[^>]+src=["\']([^"\']*player\.kototv\.to[^"\']+)["\']', html_doc)
        if m:
            servers["Server 1"] = m.group(1).strip()

    if servers:
        logger.info("KotoTV: %s S%dE%02d [%s] -> %d server(s): %s",
                    show_name, season, episode, lang.upper(), len(servers), list(servers.values())[:1])

    return servers


def extract_kototv_episode(
    show_name: str,
    season: int = 1,
    episode: int = 1,
    lang: str = "sub",
    preferred_server: str = "Server 1",
    direct_url: str = "",
) -> Optional[str]:
    """
    Extract single KotoTV player embed URL for a show/season/episode.
    lang: 'sub' or 'dub'
    Returns: https://player.kototv.to/embed/{code}/{lang}
    """
    servers = get_available_kototv_servers(show_name, season, episode, lang=lang, direct_url=direct_url)
    if not servers:
        return None

    for sname, url in servers.items():
        if preferred_server.lower() == sname.lower() or preferred_server.lower() in sname.lower():
            return url

    return next(iter(servers.values()))


# ─────────────────────────────────────────────────────────────────
#  AniHosts.xyz — Sub/Dub iframe extractor
# ─────────────────────────────────────────────────────────────────

_ANIHOSTS_SERIES_CACHE: Dict[str, Optional[str]] = {}

_ANIHOSTS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://anihosts.xyz/",
    "Accept-Language": "en-US,en;q=0.9",
}

def _anihosts_get(url: str, timeout: int = 15) -> str:
    """Simple requests fetcher for anihosts.xyz."""
    try:
        r = requests.get(url, headers=_ANIHOSTS_HEADERS, timeout=timeout)
        return r.text if r.status_code == 200 else ""
    except Exception as exc:
        logger.debug("AniHosts fetch error for %s: %s", url, exc)
        return ""


def search_anihosts_series(show_name: str, season: int = 1) -> Optional[str]:
    """
    Search anihosts.xyz for an anime and return its slug.
    E.g. 'naruto-shippuden-1735', 'solo-leveling-52299'
    """
    import urllib.parse
    try:
        from rapidfuzz import fuzz
    except ImportError:
        fuzz = None

    cache_key = f"{show_name.lower()}_s{season}"
    if cache_key in _ANIHOSTS_SERIES_CACHE:
        return _ANIHOSTS_SERIES_CACHE[cache_key]

    clean_name = re.sub(r'\s*\(?\s*(?:season|s)\s*\d+\s*\)?', '', show_name, flags=re.I).strip()
    search_query = f"{clean_name} Season {season}" if season > 1 else clean_name

    def _extract_candidates(html_text: str):
        if not html_text:
            return []
        sp = BeautifulSoup(html_text, "html.parser")
        items = []
        seen_slugs = set()
        for a in sp.find_all("a", href=re.compile(r'/anime/[a-z0-9\-]+')):
            href = a.get("href", "")
            m = re.search(r'/anime/([a-z0-9][a-z0-9\-]+)', href)
            if not m:
                continue
            sl = m.group(1).rstrip("/")
            if sl in seen_slugs or sl in ("", "anime"):
                continue
            seen_slugs.add(sl)
            t = a.get("title") or a.get_text(" ", strip=True) or sl
            items.append((sl, t))
        return items

    search_url = f"https://anihosts.xyz/search?q={urllib.parse.quote_plus(search_query)}"
    html_doc = _anihosts_get(search_url)
    candidates = _extract_candidates(html_doc)

    if not candidates and season > 1:
        fallback_url = f"https://anihosts.xyz/search?q={urllib.parse.quote_plus(clean_name)}"
        html_doc = _anihosts_get(fallback_url)
        candidates = _extract_candidates(html_doc)

    if not candidates:
        _ANIHOSTS_SERIES_CACHE[cache_key] = None
        return None

    target_clean = _clean_title_for_slug(clean_name)
    best_slug = None
    best_score = -1

    for idx, (slug, title) in enumerate(candidates):
        slug_clean = slug.lower()
        title_clean = title.lower()

        if fuzz:
            s_title = fuzz.token_sort_ratio(search_query.lower(), title_clean)
            s_slug = fuzz.token_sort_ratio(target_clean, slug_clean)
            score = max(s_title, s_slug)
        else:
            score = sum(1 for w in target_clean.split("-") if w in slug_clean or w in title_clean) * 20

        # Position bonus (earlier items are more relevant)
        score += max(0, 15 - idx * 2)

        # Penalize side-story arcs, movies, specials unless requested
        for penalty_word in ['movie', 'special', 'compilation', 'reawakening']:
            if penalty_word in slug_clean or penalty_word in title_clean:
                if penalty_word not in clean_name.lower():
                    score -= 20

        # Prioritize slug if target words are directly in slug
        if all(w in slug_clean for w in target_clean.split("-") if len(w) > 2):
            score += 10

        if season > 1:
            season_patterns = [
                f"season-{season}", f"s{season}", f"season{season}",
                f"{season}nd-season", f"{season}rd-season", f"{season}th-season",
                f"part-{season}", f"part{season}", f"season {season}"
            ]
            if any(sp in slug_clean or sp in title_clean for sp in season_patterns):
                score += 30
            else:
                score -= 30
        else:
            if re.search(r'season-[2-9]|s[2-9]|part-[2-9]|season [2-9]', slug_clean + " " + title_clean):
                score -= 30

        if score > best_score:
            best_score = score
            best_slug = slug

    if best_slug and best_score >= 35:
        logger.info("AniHosts matched '%s' S%d -> slug '%s' (score %d)", show_name, season, best_slug, best_score)
        _ANIHOSTS_SERIES_CACHE[cache_key] = best_slug
        return best_slug

    _ANIHOSTS_SERIES_CACHE[cache_key] = None
    return None


_ANIHOSTS_SEASONS_CACHE: Dict[str, List[Tuple[int, int, int]]] = {}


def get_anihosts_season_for_episode(slug: str, episode: int, default_season: int = 1) -> int:
    """
    Look up the AniHosts season ID covering `episode` for multi-season continuous shows
    (e.g., One Piece, Bleach, Naruto Shippuden).
    """
    if slug not in _ANIHOSTS_SEASONS_CACHE:
        try:
            url = f"https://anihosts.xyz/anime/{slug}"
            html_doc = _anihosts_get(url)
            chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html_doc)
            seasons_map = []
            for ch in chunks:
                decoded = ch.encode().decode('unicode-escape', errors='ignore')
                if '"seasons":[' in decoded:
                    start = decoded.find('"seasons":[') + len('"seasons":')
                    depth = 0
                    end = start
                    while end < len(decoded):
                        if decoded[end] == '[':
                            depth += 1
                        elif decoded[end] == ']':
                            depth -= 1
                            if depth == 0:
                                end += 1
                                break
                        end += 1
                    raw_json = decoded[start:end]
                    clean_json = re.sub(r':"\$undefined"', ':null', raw_json)
                    s_list = json.loads(clean_json)
                    for item in s_list:
                        s_id = item.get("id")
                        name = item.get("season", "")
                        m = re.search(r'\((\d+)[–\-](\d+)\)', name)
                        if m:
                            seasons_map.append((s_id, int(m.group(1)), int(m.group(2))))
            _ANIHOSTS_SEASONS_CACHE[slug] = seasons_map
        except Exception:
            _ANIHOSTS_SEASONS_CACHE[slug] = []

    for s_id, min_ep, max_ep in _ANIHOSTS_SEASONS_CACHE.get(slug, []):
        if min_ep <= episode <= max_ep:
            return s_id

    return default_season


def count_anihosts_episodes(show_name: str, season: int = 1, direct_url: str = "") -> int:
    """Return total episode count on AniHosts for show/season."""
    if direct_url:
        u = direct_url.strip()
        if "/watch/" in u:
            parts = u.split("/watch/")[1].split("?")[0].strip("/").split("/")
            slug = parts[0]
        elif "/anime/" in u:
            slug = u.rstrip("/").split("/anime/")[-1].split("/")[0].split("?")[0]
        else:
            slug = u.rstrip("/").split("/")[-1].split("?")[0]

        m_s = re.search(r'[?&]season=(\d+)', u)
        if m_s:
            season = int(m_s.group(1))
    else:
        slug = search_anihosts_series(show_name, season)

    if not slug:
        return 0

    url = f"https://anihosts.xyz/anime/{slug}"
    if season > 1:
        url += f"?season={season}"

    html_doc = _anihosts_get(url)
    if not html_doc:
        return 0

    # Try JSON Next.js chunks first (most accurate totalEpisodes)
    m = re.search(r'"totalEpisodes":\s*(\d+)', html_doc)
    if m:
        return int(m.group(1))

    # Fallback to counting /watch/ links on page
    sp = BeautifulSoup(html_doc, "html.parser")
    ep_links = sp.find_all("a", href=lambda h: h and f"/watch/{slug}/" in h)
    if ep_links:
        max_num = 0
        for a in ep_links:
            href = a.get("href", "")
            m_ep = re.search(rf"/watch/{re.escape(slug)}/(\d+)", href)
            if m_ep:
                num = int(m_ep.group(1))
                if num > max_num:
                    max_num = num
        if max_num > 0:
            return max_num

    return 0


def get_available_anihosts_servers(
    show_name: str,
    season: int = 1,
    episode: int = 1,
    lang: str = "sub",
    direct_url: str = "",
) -> Dict[str, str]:
    """
    Fetch all available server embed URLs for an AniHosts episode.
    lang: 'sub' or 'dub'
    Automatically switches season parameters for continuous-episode shows like One Piece.
    Returns: {"Server 1 (Kaze)": "https://...", "Server 2 (Edge)": "...", "Server 3 (Megaplay)": "..."}
    """
    lang = lang.lower().strip()
    if lang not in ("sub", "dub"):
        lang = "sub"

    if direct_url:
        u = direct_url.strip()
        if "/watch/" in u:
            parts = u.split("/watch/")[1].split("?")[0].strip("/").split("/")
            slug = parts[0]
        elif "/anime/" in u:
            slug = u.rstrip("/").split("/anime/")[-1].split("/")[0].split("?")[0]
        else:
            slug = u.rstrip("/").split("/")[-1].split("?")[0]
    else:
        slug = search_anihosts_series(show_name, season)
        if not slug:
            return {}

    m_id = re.search(r'-(\d+)$', slug)
    mal_id = m_id.group(1) if m_id else slug

    # Dynamically determine the correct season for this episode
    actual_season = season
    if direct_url and "season=" in direct_url:
        m_s = re.search(r'[?&]season=(\d+)', direct_url)
        if m_s and season == 1:
            actual_season = int(m_s.group(1))

    actual_season = get_anihosts_season_for_episode(slug, episode, default_season=actual_season)

    watch_url = f"https://anihosts.xyz/watch/{slug}/{episode}?audio={lang}&season={actual_season}"
    html_doc = _anihosts_get(watch_url)

    sp = BeautifulSoup(html_doc, "html.parser") if html_doc else None
    iframes = [ifr.get("src", "").strip() for ifr in sp.find_all("iframe") if ifr.get("src")] if sp else []

    servers: Dict[str, str] = {}
    primary_iframe = iframes[0] if iframes else ""
    if primary_iframe:
        servers["Server 1 (Kaze)"] = primary_iframe

    has_edge = False
    has_mega = False
    if sp:
        for a in sp.find_all("a", href=True):
            h = a["href"]
            if "server=edge" in h:
                has_edge = True
            elif "server=mega" in h:
                has_mega = True

        if not has_edge or not has_mega:
            if "server=edge" in html_doc:
                has_edge = True
            if "server=mega" in html_doc:
                has_mega = True

    # Build or fallback to verified players if mal_id exists
    if primary_iframe or has_edge or has_mega or (mal_id and str(mal_id).isdigit()):
        if not servers.get("Server 1 (Kaze)"):
            if lang == "dub":
                servers["Server 1 (Kaze)"] = f"https://embed.animetvplus.site/{mal_id}/{episode}?audio=dub"
            else:
                servers["Server 1 (Kaze)"] = f"https://embed.animetvplus.site/home/embed/mal/{mal_id}/{episode}"

        if has_edge or primary_iframe or (mal_id and str(mal_id).isdigit()):
            if lang == "dub":
                servers["Server 2 (Edge)"] = f"https://embed.animetvplus.site/{mal_id}/{episode}?audio=dub"
            else:
                servers["Server 2 (Edge)"] = f"https://embed.animetvplus.site/{mal_id}/{episode}"

        if has_mega or primary_iframe or (mal_id and str(mal_id).isdigit()):
            servers["Server 3 (Megaplay)"] = f"https://megaplay.buzz/stream/mal/{mal_id}/{episode}/{lang}"

    if servers:
        logger.info("AniHosts: %s S%dE%02d [%s] (season=%d) -> %d server(s): %s",
                    show_name, season, episode, lang.upper(), actual_season, len(servers), list(servers.keys()))

    return servers


def extract_anihosts_episode(
    show_name: str,
    season: int = 1,
    episode: int = 1,
    lang: str = "sub",
    preferred_server: str = "Server 1",
    direct_url: str = "",
) -> Optional[str]:
    """
    Extract single AniHosts player embed URL for a show/season/episode.
    lang: 'sub' or 'dub'
    Returns embed URL, e.g. https://embed.animetvplus.site/home/embed/mal/1735/1
    """
    servers = get_available_anihosts_servers(show_name, season, episode, lang=lang, direct_url=direct_url)
    if not servers:
        return None

    pref_lower = preferred_server.lower().strip()
    for sname, url in servers.items():
        s_low = sname.lower()
        if pref_lower == s_low:
            return url
        if pref_lower in s_low or any(k in pref_lower for k in ("kaze", "server 1", "server1") if "kaze" in s_low or "server 1" in s_low):
            return url
        if any(k in pref_lower for k in ("edge", "server 2", "server2") if "edge" in s_low or "server 2" in s_low):
            return url
        if any(k in pref_lower for k in ("mega", "server 3", "server3", "megaplay") if "mega" in s_low or "server 3" in s_low):
            return url

    return next(iter(servers.values()))


# ─────────────────────────────────────────────────────────────────
#  BlisseyHusband.net Extractor (Multi-Audio / Hindi Streams)
# ─────────────────────────────────────────────────────────────────

_BH_SERIES_CACHE: Dict[str, dict[int, str]] = {}
_BH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://blisseyhusband.net/",
}

def _blisseyhusband_get(url: str, timeout: int = 15) -> str:
    """Fetch HTML content from blisseyhusband.net."""
    try:
        r = requests.get(url, headers=_BH_HEADERS, timeout=timeout)
        return r.text if r.status_code == 200 else ""
    except Exception as exc:
        logger.debug("BlisseyHusband fetch error for %s: %s", url, exc)
        return ""

def get_blisseyhusband_episode_map(series_url: str) -> dict[int, str]:
    """
    Scrapes a BlisseyHusband series page and returns {ep_num: ep_url}.
    Handles webmaster typos in anchor text / href (e.g. Epispde, epiosde).
    """
    if not series_url:
        return {}
    clean_url = series_url.strip()
    if clean_url in _BH_SERIES_CACHE:
        return _BH_SERIES_CACHE[clean_url]

    html_text = _blisseyhusband_get(clean_url)
    if not html_text:
        return {}

    soup = BeautifulSoup(html_text, "html.parser")
    ep_map: dict[int, str] = {}

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(" ", strip=True)
        if "blisseyhusband.net" not in href:
            continue

        text_m = re.search(r'epis[o]?[dp]e?[-:\s]*(\d+)', text, re.I) or re.search(r'\bep[-:\s]*(\d+)', text, re.I)
        href_m = re.search(r'epis[o]?[dp]e?[-:\s]*(\d+)', href, re.I)

        if text_m:
            ep_num = int(text_m.group(1))
            target_url = href
            # Correct link if href has wrong episode number typo (e.g. Epispde 23 linked to episode-22)
            if href_m and int(href_m.group(1)) != ep_num:
                target_url = re.sub(r'epis[o]?[dp]e?-?\d+', f'episode-{ep_num}', href, flags=re.I)
            if ep_num not in ep_map:
                ep_map[ep_num] = target_url
        elif href_m:
            ep_num = int(href_m.group(1))
            if ep_num not in ep_map:
                ep_map[ep_num] = href

    _BH_SERIES_CACHE[clean_url] = ep_map
    return ep_map

def resolve_blisseyhusband_series_url(show_name: str, season: int = 1) -> Optional[str]:
    """Search blisseyhusband.net for series URL matching show_name and season."""
    import urllib.parse
    clean_q = re.sub(r'\s*\(?\s*(?:season|s)\s*\d+\s*\)?', '', show_name, flags=re.I).strip()
    if season > 1:
        query = f"{clean_q} Season {season}"
    else:
        query = clean_q
    search_url = f"https://blisseyhusband.net/?s={urllib.parse.quote_plus(query)}"
    html_text = _blisseyhusband_get(search_url)
    if not html_text:
        return None

    soup = BeautifulSoup(html_text, "html.parser")
    ignore_paths = [
        "/page/", "/author/", "/contact", "/privacy", "/credits", "/about",
        "/games-blogs", "/hindi-stuff", "/pokemon-seasons", "/blissey-husband-music-work"
    ]
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if "blisseyhusband.net" not in href:
            continue
        if any(ign in href for ign in ignore_paths):
            continue
        # Avoid individual episode pages in search results
        if not re.search(r'epis[o]?[dp]e?[-:\s]*\d+', href, re.I):
            return href
    return None

def count_blisseyhusband_episodes(show_name: str, season: int = 1, direct_url: str = "") -> int:
    """Return the total number of episodes available on BlisseyHusband."""
    url = direct_url.strip() if direct_url else (resolve_blisseyhusband_series_url(show_name, season) or "")
    if not url:
        return 0
    ep_map = get_blisseyhusband_episode_map(url)
    return max(ep_map.keys()) if ep_map else 0

def extract_blisseyhusband_stream(ep_url: str, worker_base: str = "") -> Optional[str]:
    """
    Extract iframe stream URL from a BlisseyHusband episode page.
    If the stream is hosted on Videas (e.g. app.videas.fr/embed/UUID or /embed/media/UUID),
    automatically routes it through the Cloudflare Worker player endpoint:
      https://<worker-domain>/embed/<uuid>/
    """
    if not ep_url:
        return None
    html_text = _blisseyhusband_get(ep_url.strip())
    if not html_text:
        return None

    raw_url = None
    soup = BeautifulSoup(html_text, "html.parser")
    for iframe in soup.find_all("iframe"):
        src = (iframe.get("src") or iframe.get("data-src") or "").strip()
        if not src:
            continue
        if any(ign in src.lower() for ign in ["google", "recaptcha", "youtube.com/subscribe", "facebook", "disqus", "doubleclick"]):
            continue
        if src.startswith("//"):
            src = "https:" + src
        raw_url = src
        break

    if not raw_url:
        for v in soup.find_all(["video", "source"]):
            src = (v.get("src") or v.get("data-src") or "").strip()
            if src:
                if src.startswith("//"):
                    src = "https:" + src
                raw_url = src
                break

    if not raw_url:
        return None

    # Check if raw_url is a Videas embed (standard on BlisseyHusband)
    vm = re.search(r'videas\.fr/embed/(?:media/)?([0-9a-fA-F-]{36})', raw_url, re.I)
    if vm:
        videas_id = vm.group(1)
        # Use secondary worker (DORABASH_WORKER_URL) for extraction/embed serving
        wb = (worker_base or DORABASH_WORKER_URL or WORKER_BASE).strip().rstrip('/') + '/'
        worker_embed = f"{wb}embed/{videas_id}/"
        logger.info("BlisseyHusband Videas embed mapped to Extractor Worker: %s -> %s", raw_url, worker_embed)
        return worker_embed

    return raw_url

def extract_blisseyhusband_episode(
    show_name: str, season: int = 1, episode: int = 1, direct_url: str = ""
) -> Optional[str]:
    """Extract stream URL for a specific episode from BlisseyHusband."""
    series_url = direct_url.strip() if direct_url else ""
    if not series_url:
        series_url = resolve_blisseyhusband_series_url(show_name, season=season) or ""
    if not series_url:
        return None

    # If direct_url is already an episode page
    if re.search(r'epis[o]?[dp]e?[-:\s]*\d+', series_url, re.I):
        m = re.search(r'epis[o]?[dp]e?[-:\s]*(\d+)', series_url, re.I)
        if m and int(m.group(1)) == episode:
            return extract_blisseyhusband_stream(series_url)
        target_ep_url = re.sub(r'epis[o]?[dp]e?-?\d+', f'episode-{episode}', series_url, flags=re.I)
        res = extract_blisseyhusband_stream(target_ep_url)
        if res:
            return res

    ep_map = get_blisseyhusband_episode_map(series_url)
    if not ep_map:
        return None

    ep_url = ep_map.get(episode)
    if not ep_url:
        first_url = next(iter(ep_map.values()), None)
        if first_url:
            ep_url = re.sub(r'epis[o]?[dp]e?-?\d+', f'episode-{episode}', first_url, flags=re.I)

    if ep_url:
        return extract_blisseyhusband_stream(ep_url)
    return None

# ─────────────────────────────────────────────────────────────────
#  DoraBash / Byse Stream Resolver (Worker Captcha Bypass)
# ─────────────────────────────────────────────────────────────────

def extract_byse_code(url_or_code: str) -> Optional[str]:
    """Extract Byse video alphanumeric code from URL, embed, or raw code."""
    if not url_or_code:
        return None
    s = url_or_code.strip()
    m = re.search(r'/(?:e|embed|r8y7v|byse-extract|mx3)/([a-zA-Z0-9_-]{6,})', s)
    if m:
        return m.group(1)
    m2 = re.match(r'^[a-zA-Z0-9_-]{8,24}$', s)
    if m2:
        return m2.group(0)
    return None

def resolve_byse_stream_url(url_or_code: str, worker_base: str = "") -> Optional[str]:
    """Convert any Byse/DoraBash URL or code into the Cloudflare Worker captcha bypass embed URL."""
    code = extract_byse_code(url_or_code)
    if not code:
        return None
    wb = (worker_base or DORABASH_WORKER_URL or WORKER_BASE).strip().rstrip('/') + '/'
    return f"{wb}e/{code}"

def get_byse_video_details(code_or_url: str) -> Optional[dict]:
    """Fetch video details metadata directly from Byse API."""
    code = extract_byse_code(code_or_url)
    if not code:
        return None
    api_url = f"https://bysevepoin.com/api/videos/{code}/details"
    try:
        req = urllib.request.Request(
            api_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://bysevepoin.com/",
            }
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            if r.status == 200:
                data = json.loads(r.read().decode("utf-8", "ignore"))
                if isinstance(data, dict) and data.get("code"):
                    return data
    except Exception as exc:
        logger.debug("Failed to get Byse video details for %s: %s", code, exc)
    return None

# ─────────────────────────────────────────────────────────────────
#  DoraBash Site Scraper (undetected_chromedriver CF bypass)
#  dorabash.in is behind Cloudflare — we use a real Chrome instance
#  to bypass the challenge, then parse the page HTML directly.
# ─────────────────────────────────────────────────────────────────
_DORABASH_EP_CACHE: Dict[str, str] = {}
_DORABASH_SERIES_LINKS_CACHE: Dict[str, Dict[int, str]] = {}
_DORABASH_SESSION = None
_DORABASH_SESSION_EXPIRY = 0
_DORABASH_SESSION_LOCK = __import__('threading').Lock()

def _get_chrome_major_version() -> int:
    """Detect the installed Chrome major version from the Windows registry."""
    import subprocess, re as _re
    for reg_path in (
        r'reg query "HKCU\Software\Google\Chrome\BLBeacon" /v version',
        r'reg query "HKLM\Software\Google\Chrome\BLBeacon" /v version'
    ):
        try:
            res = subprocess.run(reg_path, capture_output=True, text=True, shell=True, timeout=5)
            m = _re.search(r'(\d+)\.\d+\.\d+\.\d+', res.stdout or '')
            if m:
                return int(m.group(1))
        except Exception:
            pass
    return 0

def _extract_dorabash_via_worker(clean_url: str, episode_num: int = 1) -> Optional[str]:
    """
    Call the dedicated DoraBash Collector Worker (DORABASH_WORKER_URL, e.g.
    https://new-frosty.nsr-developer78.workers.dev/dorabash/extract) to fetch
    and parse the episode iframe directly from Cloudflare's edge with 0 browser overhead.
    """
    import urllib.parse
    wb = DORABASH_WORKER_URL.rstrip('/')
    api_url = f"{wb}/dorabash/extract?url={urllib.parse.quote(clean_url)}&ep={episode_num}"
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "AutoPoster/1.0"})
        with urllib.request.urlopen(req, timeout=12) as r:
            if r.status == 200:
                data = json.loads(r.read().decode("utf-8", "ignore"))
                if isinstance(data, dict):
                    code = data.get("byse_code")
                    if code:
                        return code
                    iframe = data.get("iframe_url")
                    if iframe:
                        c = extract_byse_code(iframe)
                        return c if c else iframe
    except Exception as exc:
        logger.debug("DoraBash worker endpoint error for %s: %s", api_url, exc)
    return None

def _dorabash_get_session():
    """
    Fallback: Acquire an authenticated curl_cffi session for dorabash.in.
    Chrome is launched once off-screen to get cf_clearance, then closed immediately.
    """
    global _DORABASH_SESSION, _DORABASH_SESSION_EXPIRY
    import time
    from curl_cffi import requests

    now = time.time()
    if _DORABASH_SESSION is not None and now < _DORABASH_SESSION_EXPIRY:
        return _DORABASH_SESSION

    with _DORABASH_SESSION_LOCK:
        if _DORABASH_SESSION is not None and now < _DORABASH_SESSION_EXPIRY:
            return _DORABASH_SESSION

        logger.info("DoraBash: initializing local CF session (off-screen Chrome)...")
        driver = None
        try:
            import undetected_chromedriver as uc

            options = uc.ChromeOptions()
            options.add_argument("--window-size=1280,900")
            options.add_argument("--window-position=-2000,-2000")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")

            chrome_ver = _get_chrome_major_version()
            uc_kwargs = {"options": options, "use_subprocess": True}
            if chrome_ver:
                uc_kwargs["version_main"] = chrome_ver

            driver = uc.Chrome(**uc_kwargs)
            driver.set_page_load_timeout(45)
            driver.get("https://dorabash.in/")

            for _ in range(15):
                time.sleep(1)
                if "just a moment" not in driver.title.lower():
                    break
            time.sleep(2)

            cookies = driver.get_cookies()
            ua = driver.execute_script("return navigator.userAgent")
            cookie_dict = {c['name']: c['value'] for c in cookies}

            s = requests.Session(impersonate="chrome120")
            for k, v in cookie_dict.items():
                s.cookies.set(k, v, domain="dorabash.in")
            s.headers.update({
                "User-Agent": ua,
                "Referer": "https://dorabash.in/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            })

            _DORABASH_SESSION = s
            _DORABASH_SESSION_EXPIRY = time.time() + 1800  # valid for 30 minutes
            logger.info("DoraBash: local CF session established! cf_clearance=%s", "cf_clearance" in cookie_dict)
            return _DORABASH_SESSION

        except Exception as exc:
            logger.warning("DoraBash: Failed to establish CF cookie session: %s", exc)
            return None
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass

def _dorabash_extract_episode_embed(
    series_or_watch_url: str,
    episode_num: int = 1,
    show_name: str = "",
    season: int = 1,
    encrypt: bool = False
) -> Optional[str]:
    """
    Extract Byse / DoraBash episode embed.
    1. First tries the dedicated DoraBash Collector Worker (DORABASH_WORKER_URL).
    2. If needed, falls back to the local curl_cffi cookie session.
    3. Returns the direct collector worker embed URL (e.g. https://new-frosty.nsr-developer78.workers.dev/e/CODE)
       or encrypts it with Setting Worker (WORKER_BASE) if encrypt=True.
    """
    import re, base64
    from bs4 import BeautifulSoup

    # 1. Sanitize input URL
    m = re.search(r"https?://(?:www\.)?dorabash\.in/[^\s'\"<>]+", series_or_watch_url, re.I)
    if m:
        clean_url = m.group(0).rstrip(".,;)")
    else:
        raw = series_or_watch_url.strip()
        if raw.startswith("http://") or raw.startswith("https://"):
            clean_url = raw
        else:
            slug = re.sub(r'[^a-z0-9]+', '-', raw.lower()).strip('-')
            clean_url = f"https://dorabash.in/series/{slug}/"

    cache_key = f"{clean_url}::ep{episode_num}::enc{int(encrypt)}"
    if cache_key in _DORABASH_EP_CACHE:
        return _DORABASH_EP_CACHE[cache_key]

    byse_code = None

    # ── Step 1: Try dedicated DoraBash Collector Worker ───────────────────────
    logger.info("DoraBash: querying collector worker (%s) for ep %d", DORABASH_WORKER_URL, episode_num)
    worker_res = _extract_dorabash_via_worker(clean_url, episode_num)
    if worker_res:
        byse_code = extract_byse_code(worker_res) or worker_res
        logger.info("DoraBash: collector worker returned code '%s'", byse_code)

    # ── Step 2: Fallback to fast local cookie session ─────────────────────────
    if not byse_code:
        s = _dorabash_get_session()
        if s:
            watch_url = None
            if "/watch/" in clean_url:
                watch_url = clean_url
            else:
                if clean_url in _DORABASH_SERIES_LINKS_CACHE:
                    watch_url = _DORABASH_SERIES_LINKS_CACHE[clean_url].get(episode_num)

                if not watch_url:
                    try:
                        r_series = s.get(clean_url, timeout=15)
                        soup = BeautifulSoup(r_series.text, "html.parser")
                        ep_links = {}
                        for a in soup.find_all("a", href=re.compile(r'/watch/[^/]+/')):
                            href = a.get("href")
                            m_ep = re.search(r'episode-(\d+)', href)
                            if m_ep:
                                ep_n = int(m_ep.group(1))
                                if ep_n not in ep_links:
                                    ep_links[ep_n] = href

                        if ep_links:
                            _DORABASH_SERIES_LINKS_CACHE[clean_url] = ep_links
                            watch_url = ep_links.get(episode_num)
                    except Exception as exc:
                        logger.debug("DoraBash series page error: %s", exc)

                if not watch_url:
                    slug = clean_url.rstrip("/").split("/")[-1]
                    watch_url = f"https://dorabash.in/watch/{slug}-episode-{episode_num}/"

            if watch_url:
                try:
                    r_ep = s.get(watch_url, timeout=15)
                    html = r_ep.text
                    if r_ep.status_code == 403 or "just a moment" in html.lower():
                        global _DORABASH_SESSION
                        _DORABASH_SESSION = None
                        s = _dorabash_get_session()
                        if s:
                            r_ep = s.get(watch_url, timeout=15)
                            html = r_ep.text

                    m_code = re.findall(r'(?:bysevepoin|bysetayico)\.com/e/([a-zA-Z0-9_-]{6,})', html)
                    if m_code:
                        byse_code = m_code[0]
                    else:
                        embeds = re.findall(r'data-embed-id=["\']([^"\']+)["\']', html)
                        for em in embeds:
                            for part in em.split(":"):
                                try:
                                    dec = base64.b64decode(part).decode("utf-8", "ignore")
                                    m_c = re.findall(r'(?:bysevepoin|bysetayico)\.com/e/([a-zA-Z0-9_-]{6,})', dec)
                                    if m_c:
                                        byse_code = m_c[0]
                                        break
                                except Exception:
                                    pass
                            if byse_code:
                                break
                except Exception as exc:
                    logger.debug("DoraBash watch page error: %s", exc)

    if not byse_code:
        logger.warning("DoraBash: Byse code not found for %s ep %d", clean_url, episode_num)
        return None

    # ── Step 3: Format with DoraBash Collector Worker & Encrypt with Setting Worker ─
    wb_collector = DORABASH_WORKER_URL.rstrip('/') + '/'
    inner_player_url = f"{wb_collector}e/{byse_code}"

    if encrypt:
        title = f"{show_name} S{season:02d}E{episode_num:02d} - Server DB" if show_name else f"Episode {episode_num} - Server DB"
        final_url = encrypt_iframe_url(title, inner_player_url)
    else:
        final_url = inner_player_url

    _DORABASH_EP_CACHE[cache_key] = final_url
    logger.info("DoraBash: resolved ep %d -> %s", episode_num, final_url)
    return final_url

def get_dorabash_episode_map(show_name: str, season: int = 1, direct_url: str = "") -> Dict[int, str]:
    """Compatibility wrapper returning {episode: worker_url}."""
    ep_map: Dict[int, str] = {}
    return ep_map

def extract_byse_episode(
    show_name: str = "", season: int = 1, episode: int = 1, direct_url: str = "", encrypt: bool = False
) -> Optional[str]:
    """
    Extract Byse / DoraBash stream URL for a specific episode.
    direct_url can be:
      - A dorabash.in series or watch URL (collected via DORABASH_WORKER_URL)
      - A single Byse embed URL/code (bysevepoin.com/e/CODE)
      - Multiple Byse embed URLs (one per line)
    Returns the direct collector worker embed URL (e.g. https://new-frosty.nsr-developer78.workers.dev/e/CODE)
    unless encrypt=True (which encrypts via Setting Worker WORKER_BASE).
    """
    if not direct_url:
        return None

    text = direct_url.strip()

    # ── Case 1: DoraBash series or watch URL (dorabash.in) ────────────────────
    m_db = re.search(r"https?://(?:www\.)?dorabash\.in/[^\s'\"<>]+", text, re.I)
    if m_db or "dorabash.in" in text:
        db_target_url = m_db.group(0).rstrip(".,;)") if m_db else text.strip()
        return _dorabash_extract_episode_embed(
            db_target_url, episode_num=episode, show_name=show_name, season=season, encrypt=encrypt
        )

    # ── Case 2: Multiple Byse URLs/codes (one per line) ───────────────────────
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    matched_code = None
    if len(lines) > 1:
        for ln in lines:
            m = re.search(r'S(?:eason\s*)?0*(\d+)\s*E(?:p(?:isode)?\s*)?0*(\d+)', ln, re.I)
            if m and int(m.group(1)) == season and int(m.group(2)) == episode:
                matched_code = extract_byse_code(ln)
                break
            m_ep = re.search(r'(?:^|[\s\-_/\[(])(?:EP|E|Episode)[\s\-_.:#]*0*(\d+)', ln, re.I)
            if m_ep and int(m_ep.group(1)) == episode:
                matched_code = extract_byse_code(ln)
                break
        if not matched_code and (1 <= episode <= len(lines)):
            matched_code = extract_byse_code(lines[episode - 1])

        if matched_code:
            wb_collector = DORABASH_WORKER_URL.rstrip('/') + '/'
            inner_url = f"{wb_collector}e/{matched_code}"
            if encrypt:
                title = f"{show_name} S{season:02d}E{episode:02d} - Server DB" if show_name else f"Episode {episode} - Server DB"
                return encrypt_iframe_url(title, inner_url)
            return inner_url
        return None

    # ── Case 3: Single Byse embed URL/code ────────────────────────────────────
    code = extract_byse_code(text)
    if code:
        details = get_byse_video_details(code)
        if details and details.get("title"):
            t = details["title"]
            m_s_ep = re.search(r'S(\d+)E(\d+)', t, re.I)
            if m_s_ep:
                s_num, ep_num = int(m_s_ep.group(1)), int(m_s_ep.group(2))
                if s_num != season or ep_num != episode:
                    return None
        wb_collector = DORABASH_WORKER_URL.rstrip('/') + '/'
        inner_url = f"{wb_collector}e/{code}"
        if encrypt:
            title = f"{show_name} S{season:02d}E{episode:02d} - Server DB" if show_name else f"Episode {episode} - Server DB"
            return encrypt_iframe_url(title, inner_url)
        return inner_url

    return None

# ─────────────────────────────────────────────────────────────────
#  AniNeko.to (legacy — site currently down)
# ─────────────────────────────────────────────────────────────────

_ANINEKO_SERIES_CACHE: Dict[str, Optional[str]] = {}

def anineko_curl_get(url: str, timeout: int = 15) -> str:
    """Robust IPv4 fetcher for anineko.to."""
    import subprocess
    cmd = [
        "curl.exe", "-4", "-s", "-L",
        "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "-H", "Referer: https://anineko.to/",
        "-H", "Accept-Language: en-US,en;q=0.9",
        "--max-time", str(timeout),
        url
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
        return res.stdout or ""
    except Exception as exc:
        logger.debug("AniNeko curl error for %s: %s", url, exc)
        return ""

def _clean_title_for_slug(text: str) -> str:
    t = text.lower()
    t = re.sub(r'[\–\—\-:]+', ' ', t)
    t = re.sub(r'[^a-z0-9\s]+', '', t)
    return re.sub(r'\s+', '-', t).strip('-')

def search_anineko_series(show_name: str, season: int = 1) -> Optional[str]:
    """Search anineko.to for anime show and return the watch slug (e.g. 'one-piece', 'solo-leveling-season-2-arise-from-the-shadow')."""
    import urllib.parse
    from rapidfuzz import fuzz

    cache_key = f"{show_name.lower()}_s{season}"
    if cache_key in _ANINEKO_SERIES_CACHE:
        return _ANINEKO_SERIES_CACHE[cache_key]

    clean_name = re.sub(r'\s*\(?\s*(?:season|s)\s*\d+\s*\)?', '', show_name, flags=re.I).strip()
    search_query = clean_name
    if season > 1:
        search_query = f"{clean_name} Season {season}"

    search_url = f"https://anineko.to/browser?keyword={urllib.parse.quote_plus(search_query)}"
    html_doc = anineko_curl_get(search_url)
    if not html_doc:
        _ANINEKO_SERIES_CACHE[cache_key] = None
        return None

    soup = BeautifulSoup(html_doc, "html.parser")
    links = soup.find_all("a", href=lambda h: h and "/watch/" in h)

    candidates = []
    seen = set()
    for a in links:
        href = a.get("href", "")
        slug = href.rstrip("/").split("/watch/")[-1].split("/")[0].split("?")[0]
        if not slug or slug in seen:
            continue
        seen.add(slug)
        title = a.get("title") or a.get_text(strip=True) or slug
        candidates.append((slug, title))

    if not candidates and season > 1:
        # Fallback search with just base name
        search_url = f"https://anineko.to/browser?keyword={urllib.parse.quote_plus(clean_name)}"
        html_doc = anineko_curl_get(search_url)
        if html_doc:
            soup = BeautifulSoup(html_doc, "html.parser")
            links = soup.find_all("a", href=lambda h: h and "/watch/" in h)
            for a in links:
                href = a.get("href", "")
                slug = href.rstrip("/").split("/watch/")[-1].split("/")[0].split("?")[0]
                if slug and slug not in seen:
                    seen.add(slug)
                    title = a.get("title") or a.get_text(strip=True) or slug
                    candidates.append((slug, title))

    if not candidates:
        _ANINEKO_SERIES_CACHE[cache_key] = None
        return None

    # Score candidates
    target_clean = _clean_title_for_slug(clean_name)
    best_slug = None
    best_score = -1

    for slug, title in candidates:
        slug_clean = slug.lower()
        score = fuzz.token_sort_ratio(target_clean, slug_clean)
        
        # Check season match
        if season > 1:
            season_patterns = [
                f"season-{season}", f"s{season}", f"season{season}",
                f"{season}nd-season", f"{season}rd-season", f"{season}th-season",
                f"part-{season}", f"part{season}"
            ]
            if any(sp in slug_clean for sp in season_patterns):
                score += 30
            else:
                score -= 30
        else:
            # For Season 1, penalize later seasons
            if re.search(r'season-[2-9]|s[2-9]|part-[2-9]', slug_clean):
                score -= 30

        if score > best_score:
            best_score = score
            best_slug = slug

    if best_slug and best_score >= 40:
        logger.info("AniNeko matched '%s' S%d -> slug '%s' (score %d)", show_name, season, best_slug, best_score)
        _ANINEKO_SERIES_CACHE[cache_key] = best_slug
        return best_slug

    _ANINEKO_SERIES_CACHE[cache_key] = None
    return None

def count_anineko_episodes(show_name: str, season: int = 1) -> int:
    """Return total episode count on AniNeko for show/season."""
    slug = search_anineko_series(show_name, season)
    if not slug:
        return 0
    ep1_url = f"https://anineko.to/watch/{slug}/ep-1"
    html_doc = anineko_curl_get(ep1_url)
    if not html_doc:
        return 0
    soup = BeautifulSoup(html_doc, "html.parser")
    ep_links = soup.find_all("a", href=lambda h: h and f"/watch/{slug}/ep-" in h)
    nums = []
    for el in ep_links:
        href = el.get("href", "")
        m = re.search(r'/ep-(\d+)', href)
        if m:
            nums.append(int(m.group(1)))
    return max(nums) if nums else (1 if ep_links else 0)

def get_available_anineko_servers(show_name: str, season: int = 1, episode: int = 1, direct_url: str = "") -> Dict[str, Dict[str, str]]:
    """
    Fetch all available servers and embed URLs for a specific episode on AniNeko.
    Returns:
    {
        "dub": {"HD-1": "https://...", "StreamHG": "https://...", ...},
        "sub": {"HD-1": "https://...", ...},
        "hsub": {"HD-1": "https://...", ...}
    }
    """
    if direct_url:
        u = direct_url.strip()
        if "/watch/" in u:
            if re.search(r'/ep-\d+', u):
                ep_url = re.sub(r'/ep-\d+', f'/ep-{episode}', u)
            else:
                ep_url = u.rstrip('/') + f'/ep-{episode}'
        else:
            ep_url = u
    else:
        slug = search_anineko_series(show_name, season)
        if not slug:
            return {}
        ep_url = f"https://anineko.to/watch/{slug}/ep-{episode}"

    html_doc = anineko_curl_get(ep_url)
    if not html_doc:
        return {}

    soup = BeautifulSoup(html_doc, "html.parser")
    panels = soup.find_all("div", class_=lambda c: c and "nv-server-panel" in c)

    result: Dict[str, Dict[str, str]] = {}

    for panel in panels:
        lang_id = (panel.get("data-id") or "").lower().strip() # 'hsub', 'sub', 'dub'
        if not lang_id or lang_id == "unknown":
            head = panel.find("strong")
            htext = head.get_text(strip=True).lower() if head else ""
            if "hard" in htext:
                lang_id = "hsub"
            elif "sort" in htext or "soft" in htext:
                lang_id = "sub"
            elif "dub" in htext:
                lang_id = "dub"
            else:
                continue

        if lang_id not in result:
            result[lang_id] = {}

        for btn in panel.find_all("button", class_=lambda c: c and "nv-server-btn" in c):
            video_url = btn.get("data-video", "").strip()
            if not video_url:
                continue

            span = btn.find("span")
            span_text = span.get_text(strip=True) if span else ""
            btn_text = btn.get_text(strip=True)
            if span_text and btn_text.endswith(span_text):
                server_name = btn_text[:-len(span_text)].strip()
            else:
                server_name = btn_text.strip()

            if server_name:
                result[lang_id][server_name] = video_url

    return result

def extract_anineko_episode(
    show_name: str,
    season: int = 1,
    episode: int = 1,
    lang: str = "dub",
    preferred_server: str = "HD-1",
    direct_url: str = "",
) -> Optional[str]:
    """
    Extract single video embed URL from AniNeko for show/season/episode.
    lang: 'dub', 'sub', 'hsub'
    preferred_server: 'HD-1', 'HD-2', 'StreamHG', 'Earnvids', 'Doodstream'
    """
    all_servers = get_available_anineko_servers(show_name, season, episode, direct_url=direct_url)
    if not all_servers:
        return None

    lang_key = (lang or "dub").lower().strip()
    if lang_key not in all_servers:
        # Fallback to dub, sub, or hsub in order of preference
        if "dub" in all_servers:
            lang_key = "dub"
        elif "sub" in all_servers:
            lang_key = "sub"
        elif "hsub" in all_servers:
            lang_key = "hsub"
        else:
            return None

    servers_dict = all_servers[lang_key]
    if not servers_dict:
        return None

    # Try preferred server first
    if preferred_server:
        for sname, url in servers_dict.items():
            if preferred_server.lower() == sname.lower() or preferred_server.lower() in sname.lower():
                logger.info("AniNeko: extracted %s S%dE%02d [%s/%s] -> %s", show_name, season, episode, lang_key.upper(), sname, url)
                return url

    # Preferred fallback order
    for pref in ["HD-1", "HD-2", "StreamHG", "Earnvids", "Doodstream"]:
        for sname, url in servers_dict.items():
            if pref.lower() == sname.lower() or pref.lower() in sname.lower():
                logger.info("AniNeko: extracted %s S%dE%02d [%s/%s] -> %s", show_name, season, episode, lang_key.upper(), sname, url)
                return url

    # Default to first available
    sname, url = next(iter(servers_dict.items()))
    logger.info("AniNeko: extracted %s S%dE%02d [%s/%s] -> %s", show_name, season, episode, lang_key.upper(), sname, url)
    return url


def _parse_direct_embed_code(text: str) -> Optional[str]:
    """
    Extract stream URL from raw HTML snippet, iframe src, or URL.
    Supports smoothpre, hanerix, gdmirrorbot, nxsha, screenscape, modiplay, etc.
    """
    if not text:
        return None

    text = re.sub(r'[\t\n\r]', '', text).replace(r'\/', '/')

    matches = re.findall(r"(?:https?:)?//[^\s\"'<>]+", text)
    codes = []
    direct_valid_embeds = []

    for u in matches:
        full_u = u if u.startswith("http") else f"https:{u}"
        m = re.search(r"/(?:e|embed|v)/([A-Za-z0-9_-]{6,})", full_u)
        if m:
            code = m.group(1)
            if code not in codes:
                codes.append(code)

        if any(dom in full_u for dom in [
            "smoothpre.com", "hanerix.com", "vibuxer.com", "morencius.com",
            "minochinos.com", "multimoviesshg.com", "bysetayico.com"
        ]):
            if full_u not in direct_valid_embeds:
                direct_valid_embeds.append(full_u)

    # Pass 1: Prioritize Morencius / Smoothpre across all extracted codes
    for code in codes:
        for domain in ("morencius.com", "smoothpre.com"):
            u = f"https://{domain}/v/{code}"
            if is_stream_alive(u):
                return u

    # Pass 2: Fall back to Hanerix
    for code in codes:
        hanerix_url = f"https://hanerix.com/e/{code}"
        if is_stream_alive(hanerix_url):
            return hanerix_url

    # Pass 3: Direct embeds ONLY if they are morencius, smoothpre, or hanerix
    for emb in direct_valid_embeds:
        if any(d in emb for d in ("morencius.com", "smoothpre.com", "hanerix.com")):
            if is_stream_alive(emb):
                return emb

    return None


_tmdb_id_cache: dict[tuple[str, bool], Optional[int]] = {}

def get_tmdb_id(show_name: str, is_movie: bool = False) -> Optional[int]:
    """Fetch TMDB ID via web search or cache."""
    cache_key = (show_name.lower().strip(), is_movie)
    if cache_key in _tmdb_id_cache:
        return _tmdb_id_cache[cache_key]

    clean = re.sub(r"\(Season \d+\)|\bSeason \d+\b|\bS\d{1,2}\b", "", show_name, flags=re.IGNORECASE).strip()
    clean = re.sub(r"[-–,\s]+$", "", clean).strip()

    media_type = "movie" if is_movie else "tv"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    url = f"https://www.themoviedb.org/search/{media_type}"
    try:
        r = requests.get(url, params={"query": clean}, headers=headers, timeout=5)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            pattern = re.compile(rf"^/{media_type}/(\d+)")
            for a in soup.find_all("a", href=True):
                m = pattern.match(a["href"])
                if m:
                    tmdb_id = int(m.group(1))
                    _tmdb_id_cache[cache_key] = tmdb_id
                    return tmdb_id
    except Exception as exc:
        logger.debug("TMDB ID lookup failed for '%s': %s", show_name, exc)

    _tmdb_id_cache[cache_key] = None
    return None


def extract_modiplay_stream(show_name: str, season: int = 1, episode: int = 1, is_movie: bool = False, tmdb_id: Optional[str] = None) -> Optional[str]:
    """
    Extract stream from Cineverse / Modiplay embed server.
    Parses switchServer(...) calls from the CineVerse page HTML and maps:
      platform="streamhg"  → hanerix.com/e/{fileCode}
      platform="earnvids"  → smoothpre.com/v/{fileCode}
    Pass tmdb_id directly to skip the TMDB search lookup.
    """
    if not tmdb_id:
        tmdb_id = get_tmdb_id(show_name, is_movie)
    if not tmdb_id:
        return None

    if is_movie:
        url = f"https://rozgarlelo.modiplay.xyz/embed/tmdb/movie?id={tmdb_id}"
    else:
        url = f"https://rozgarlelo.modiplay.xyz/embed/tmdb/tv?id={tmdb_id}&s={season}&e={episode}"

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    # CineVerse platform → our stream domain mapping
    # earnvids = morencius; streamhg = hanerix
    _PLATFORM_MAP = {
        "earnvids":  "https://morencius.com/v/",
        "morencius": "https://morencius.com/v/",
        "smoothpre": "https://morencius.com/v/",
        "streamhg":  "https://hanerix.com/e/",
        "hanerix":   "https://hanerix.com/e/",
    }
    # Priority order: morencius (earnvids) first, then hanerix (streamhg)
    _PRIORITY = ["earnvids", "morencius", "smoothpre", "streamhg", "hanerix"]

    import time, html as _html
    for attempt in range(1, 4):
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code != 200:
                break

            # First try: parse switchServer calls to find streamhg / earnvids
            # Pattern: switchServer('URL','platform','PlatformName','fileCode','Title',this)
            sw_pattern = re.compile(
                r"switchServer\(\s*['\"]([^'\"]*)['\"],\s*['\"]([^'\"]*)['\"],\s*['\"]([^'\"]*)['\"],\s*['\"]([^'\"]*)['\"]",
                re.IGNORECASE,
            )
            servers_found: dict[str, str] = {}  # platform -> fileCode
            for m in sw_pattern.finditer(r.text):
                embed_url, platform, _pname, file_code = m.group(1), m.group(2), m.group(3), m.group(4)
                platform = platform.lower()
                if platform not in servers_found and file_code:
                    servers_found[platform] = file_code
                    logger.debug("CineVerse server: platform=%s fileCode=%s", platform, file_code)

            for plat in _PRIORITY:
                if plat in servers_found and servers_found[plat]:
                    final_url = _PLATFORM_MAP[plat] + servers_found[plat]
                    if is_stream_alive(final_url):
                        logger.info("Resolved Server UG2 via Cineverse (%s) → %s", plat, final_url)
                        return final_url

            # Second try: fallback to _parse_direct_embed_code (hanerix/smoothpre inline)
            direct_result = _parse_direct_embed_code(r.text)
            if direct_result:
                logger.info("Resolved Server UG2 via Cineverse (direct embed) → %s", direct_result)
                return direct_result

            break  # No usable server found on this page
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            if attempt < 3:
                time.sleep(1.0)
                continue
            logger.debug("Modiplay stream extraction timeout for %s S%dE%d (attempt %d/3): %s", show_name, season, episode, attempt, exc)
        except Exception as exc:
            logger.debug("Modiplay stream extraction failed for %s S%dE%d: %s", show_name, season, episode, exc)
            break

    return None


# ─────────────────────────────────────────────────────────────────
#  Multimovies episode & movie resolver
# ─────────────────────────────────────────────────────────────────

_multimovies_show_cache: dict[tuple[str, bool], str] = {}

# Cache for multimovies.garden (new theme) show URLs and TMDB IDs
_multimovies_garden_show_cache: dict[str, str] = {}   # clean_name -> "/series/{slug}-{id}"

MULTIMOVIES_GARDEN_DOMAIN = "m1.multimovies.courses"
MULTIMOVIES_GARDEN_DOMAINS = ("m1.multimovies.courses", "multimovies.courses", "multimovies.garden")
_multimovies_doh_cache: dict[str, str] = {}

def _resolve_multimovies_doh(hostname: str) -> Optional[str]:
    """Resolve hostname using Cloudflare / Google DoH to bypass ISP DNS blocks in India."""
    if hostname in _multimovies_doh_cache:
        return _multimovies_doh_cache[hostname]
    for doh_endpoint in ("https://1.1.1.1/dns-query", "https://dns.google/resolve"):
        try:
            r = requests.get(
                doh_endpoint,
                params={"name": hostname, "type": "A"},
                headers={"accept": "application/dns-json"},
                timeout=4,
            )
            data = r.json()
            if data.get("Status") == 0 and "Answer" in data:
                for ans in data["Answer"]:
                    if ans.get("type") == 1:
                        ip = ans.get("data")
                        if ip:
                            _multimovies_doh_cache[hostname] = ip
                            return ip
        except Exception:
            pass
    ip_fallback = "172.67.135.46" if "courses" in hostname else "172.67.163.1"
    _multimovies_doh_cache[hostname] = ip_fallback
    return ip_fallback

def _garden_http_get(url: str, session: Optional[requests.Session] = None, timeout: int = 10):
    """
    HTTP GET for Multimovies that automatically falls back to curl_cffi with DoH resolution
    if local ISP DNS blocks the domain (e.g. Airtel/Jio RPZ hijacking in India).
    """
    s = session or requests
    try:
        r = s.get(url, verify=False, timeout=timeout)
        if r.status_code == 200 or r.status_code == 404:
            return r
    except Exception as exc:
        logger.debug("multimovies requests.get failed for %s (%s) — trying curl_cffi DoH fallback...", url, exc)

    try:
        from urllib.parse import urlparse
        from curl_cffi import CurlOpt, requests as cffi_requests
        u = urlparse(url)
        hostname = u.netloc
        ip = _resolve_multimovies_doh(hostname)
        opts = {CurlOpt.CONNECTTIMEOUT: 5}
        if ip:
            opts[CurlOpt.RESOLVE] = [f"{hostname}:443:{ip}"]
        r = cffi_requests.get(
            url,
            impersonate="chrome124",
            curl_options=opts,
            timeout=timeout,
        )
        return r
    except Exception as exc2:
        logger.debug("multimovies curl_cffi DoH fallback also failed for %s: %s", url, exc2)
    return None


def resolve_multimovies_garden_episode(
    show_name: str,
    season: int = 1,
    episode: int = 1,
    is_movie: bool = False,
    direct_url: str = "",
) -> Optional[str]:
    """
    Extract video embed from Multimovies (new theme, m1.multimovies.courses / multimovies.garden):
    1. Search via /api/search?q={name} → get /series/{slug}-{id} URL
    2. Fetch episode page: /series/{slug}-{id}/season/{s}/episode/{e}
    3. Parse `watchConfig` JSON from the raw HTML (prefer IQSmart server)
    4. Return the IQSmart embed URL (streams.iqsmartgames.com/embed/tv/{tmdb_id}/{s}/{e}?key=...)
       which is usable directly as Server UG2 embed.

    direct_url: if provided, skip search and use this Multimovies show URL directly.
    """
    import html as _html

    garden_base = f"https://{MULTIMOVIES_GARDEN_DOMAIN}"

    show_query = re.sub(r"\(Season \d+\)", "", show_name, flags=re.IGNORECASE).strip()
    show_query = re.sub(r"Season \d+", "", show_query, flags=re.IGNORECASE).strip()
    show_query = _STRIP_TAGS.sub("", show_query).strip()
    show_query = re.sub(r'\s{2,}', ' ', show_query).strip()
    clean_query = re.sub(r'\s*\([^)]*\)', '', show_query).strip()
    clean_query = clean_query.replace('\u2019', "'").replace('\u2018', "'").replace('\u201c', '"').replace('\u201d', '"').replace('\u2013', '-').replace('\u2014', '-')

    cache_key = clean_query.lower()

    garden_session = requests.Session()
    garden_session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })

    try:
        # Step 1: Find series/movie URL
        series_path = None

        # Check cache or direct_url first
        if direct_url and any(d in direct_url for d in MULTIMOVIES_GARDEN_DOMAINS):
            from urllib.parse import urlparse
            p = urlparse(direct_url).path
            series_path = re.sub(r'/season/\d+/episode/\d+.*', '', p).rstrip('/')
        elif cache_key in _multimovies_garden_show_cache:
            series_path = _multimovies_garden_show_cache[cache_key]
            if series_path == "NOT_FOUND":
                return None

        if not series_path:
            # Search via API
            import unicodedata
            clean_norm = unicodedata.normalize('NFKD', clean_query).encode('ASCII', 'ignore').decode('utf-8').strip()
            clean_no_punct = re.sub(r'[:–—\-\|\!\?\.]', ' ', clean_query).strip()
            clean_no_punct = re.sub(r'\s+', ' ', clean_no_punct)

            search_queries = [clean_query]
            if clean_no_punct.lower() != clean_query.lower() and clean_no_punct not in search_queries:
                search_queries.append(clean_no_punct)
            if clean_norm.lower() not in [q.lower() for q in search_queries]:
                search_queries.append(clean_norm)

            base_part = re.split(r'[-–—:,]', clean_query)[0].strip()
            if base_part and base_part.lower() != clean_query.lower():
                search_queries.append(base_part)
            words = clean_query.split()
            if len(words) > 1 and words[0].lower() not in [q.lower() for q in search_queries]:
                search_queries.append(words[0])

            from rapidfuzz import fuzz as _fuzz
            for q in search_queries:
                search_url = f"{garden_base}/api/search?q={requests.utils.quote(q)}"
                try:
                    r_search = _garden_http_get(search_url, session=garden_session, timeout=10)
                    if r_search and r_search.status_code == 200:
                        search_data = r_search.json()
                        candidates = []
                        if is_movie:
                            candidates = search_data.get("movies", []) + search_data.get("results", [])
                            candidates = [c for c in candidates if c.get("type") == "movie"]
                        else:
                            candidates = search_data.get("tv_shows", []) + search_data.get("results", [])
                            candidates = [c for c in candidates if c.get("type") == "tv"]

                        best_score = 0
                        for cand in candidates:
                            cand_title = cand.get("title", "")
                            c_norm = unicodedata.normalize('NFKD', cand_title).encode('ASCII', 'ignore').decode('utf-8').lower()
                            score = max(
                                _fuzz.token_sort_ratio(clean_norm.lower(), c_norm),
                                _fuzz.token_sort_ratio(clean_query.lower(), cand_title.lower())
                            )
                            if score > best_score:
                                best_score = score
                                series_path = cand.get("url", "")

                        if best_score >= 60 and series_path:
                            logger.debug("multimovies matched '%s' -> '%s' (score=%d)", q, series_path, best_score)
                            _multimovies_garden_show_cache[cache_key] = series_path
                            break
                        else:
                            series_path = None
                except Exception as e:
                    logger.debug("multimovies search error for '%s': %s", q, e)

        if not series_path:
            _multimovies_garden_show_cache[cache_key] = "NOT_FOUND"
            return None

        # Step 2: Build episode page URL
        if is_movie:
            ep_page_url = f"{garden_base}{series_path}"
        else:
            # Ensure series_path is just the /series/... part (no trailing episode)
            series_path_clean = re.sub(r'/season/\d+/episode/\d+.*', '', series_path)
            ep_page_url = f"{garden_base}{series_path_clean}/season/{season}/episode/{episode}"

        r_ep = _garden_http_get(ep_page_url, session=garden_session, timeout=12)
        if not r_ep or r_ep.status_code != 200:
            logger.debug("multimovies episode page %s returned error or non-200", ep_page_url)
            return None

        # Step 3: Parse watchConfig from raw HTML (use r.text, NOT BeautifulSoup .string)
        raw_text = r_ep.text
        m_cfg = re.search(r'watchConfig\s*=\s*\{', raw_text)
        if not m_cfg:
            logger.debug("multimovies: watchConfig not found in %s", ep_page_url)
            return None

        # Extract JSON block by brace counting
        brace_start = m_cfg.end() - 1
        depth = 0
        block_end = brace_start
        for i, c in enumerate(raw_text[brace_start:], brace_start):
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    block_end = i
                    break

        raw_block = raw_text[brace_start:block_end + 1]
        # Fix HTML entities (&amp; -> &) before JSON parsing
        raw_block_fixed = _html.unescape(raw_block)

        try:
            watch_cfg = json.loads(raw_block_fixed)
        except json.JSONDecodeError as e:
            logger.debug("multimovies: watchConfig JSON parse error: %s", e)
            return None

        servers = watch_cfg.get("initialServers", [])
        if not servers:
            logger.debug("multimovies.garden: no servers in watchConfig for %s", ep_page_url)
            return None

        # Step 4: Pick and resolve server to final stream
        # Strict priority: MORENCIUS (or smoothpre) FIRST, and HANERIX as secondary fallback!
        hanerix_fallback = None

        # Extract TMDB ID from IQSmart or Cineverse URL if available
        _garden_tmdb_id = None
        for sv in servers:
            _u = sv.get("url", "")
            _m = re.search(r'iqsmartgames\.com/embed/(?:tv|movie)/(\d+)/', _u)
            if _m:
                _garden_tmdb_id = _m.group(1)
                break
            _m2 = re.search(r'modiplay\.xyz/embed/tmdb/(?:tv|movie)\?id=(\d+)', _u)
            if _m2:
                _garden_tmdb_id = _m2.group(1)
                break

        # Priority 1: Custom mirrors (e.g. GDMIRROR/filesforever)
        for sv in servers:
            url = sv.get("url", "")
            if not url:
                continue
            url_clean = _html.unescape(url)
            if sv.get("is_custom") or _GDMIRROR_EMBED_HOSTS.search(url_clean):
                result = resolve_gdmirrorbot_embed(url_clean)
                if result:
                    if any(d in result for d in ("morencius.com", "smoothpre.com")):
                        logger.info("multimovies.garden resolved '%s' S%dE%d via GDMirror/custom (morencius) → %s", show_query, season, episode, result)
                        return result
                    elif "hanerix.com" in result and not hanerix_fallback:
                        hanerix_fallback = result

        # Priority 2: Check Cineverse / Modiplay for MORENCIUS first!
        for sv in servers:
            url = sv.get("url", "")
            if url:
                url_clean = _html.unescape(url)
                if "modiplay" in url_clean or "cineverse" in url_clean.lower() or "moviesnexus" in url_clean.lower():
                    modiplay_result = extract_modiplay_stream(
                        show_name, season=season, episode=episode,
                        tmdb_id=_garden_tmdb_id,
                    )
                    if modiplay_result:
                        if any(d in modiplay_result for d in ("morencius.com", "smoothpre.com")):
                            logger.info(
                                "multimovies.garden resolved '%s' S%dE%d via Modiplay/CineVerse (preferred morencius) → %s",
                                show_query, season, episode, modiplay_result,
                            )
                            return modiplay_result
                        elif "hanerix.com" in modiplay_result and not hanerix_fallback:
                            hanerix_fallback = modiplay_result

        # Priority 3: IQSmart server
        for sv in servers:
            url = sv.get("url", "")
            if "iqsmartgames.com" in url:
                url_clean = _html.unescape(url)
                final_stream = resolve_iqsmartgames_embed(url_clean)
                if final_stream:
                    if any(d in final_stream for d in ("morencius.com", "smoothpre.com")):
                        logger.info("multimovies.garden resolved '%s' S%dE%d via IQSmart (morencius) → %s", show_query, season, episode, final_stream)
                        return final_stream
                    elif "hanerix.com" in final_stream and not hanerix_fallback:
                        hanerix_fallback = final_stream

        # Priority 4: Any direct embed code
        for sv in servers:
            url = sv.get("url", "")
            if url:
                url_clean = _html.unescape(url)
                parsed = _parse_direct_embed_code(url_clean)
                if parsed:
                    if any(d in parsed for d in ("morencius.com", "smoothpre.com")):
                        return parsed
                    elif "hanerix.com" in parsed and not hanerix_fallback:
                        hanerix_fallback = parsed

        # Fallback to secondary hanerix stream if morencius was not present on any server
        if hanerix_fallback:
            logger.info("multimovies.garden resolved '%s' S%dE%d via secondary hanerix → %s", show_query, season, episode, hanerix_fallback)
            return hanerix_fallback

    except Exception as exc:
        logger.debug("multimovies.garden error for '%s' S%dE%d: %s", show_name, season, episode, exc)

    return None


def count_multimovies_garden_episodes(show_name: str, season: int = 1, direct_url: str = "") -> int:
    """
    Count how many episodes Multimovies (m1.multimovies.courses / multimovies.garden) has for a given show/season.
    Uses /api/search to find the series, then fetches the series page and reads seasonEpisodeNumbers.
    """
    import html as _html

    garden_base = f"https://{MULTIMOVIES_GARDEN_DOMAIN}"

    show_query = re.sub(r"\(Season \d+\)", "", show_name, flags=re.IGNORECASE).strip()
    show_query = re.sub(r"Season \d+", "", show_query, flags=re.IGNORECASE).strip()
    show_query = _STRIP_TAGS.sub("", show_query).strip()
    show_query = re.sub(r'\s{2,}', ' ', show_query).strip()
    clean_query = re.sub(r'\s*\([^)]*\)', '', show_query).strip()
    clean_query = clean_query.replace('\u2019', "'").replace('\u2018', "'").replace('\u201c', '"').replace('\u201d', '"').replace('\u2013', '-').replace('\u2014', '-')
    cache_key = clean_query.lower()

    garden_session = requests.Session()
    garden_session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    })

    try:
        series_path = _multimovies_garden_show_cache.get(cache_key)
        if series_path == "NOT_FOUND":
            return 0

        if not series_path and direct_url and any(d in direct_url for d in MULTIMOVIES_GARDEN_DOMAINS):
            from urllib.parse import urlparse
            p = urlparse(direct_url).path
            series_path = re.sub(r'/season/\d+/episode/\d+.*', '', p).rstrip('/')

        if not series_path:
            from rapidfuzz import fuzz as _fuzz
            import unicodedata
            clean_norm = unicodedata.normalize('NFKD', clean_query).encode('ASCII', 'ignore').decode('utf-8').strip()
            clean_no_punct = re.sub(r'[:–—\-\|\!\?\.]', ' ', clean_query).strip()
            clean_no_punct = re.sub(r'\s+', ' ', clean_no_punct)

            search_queries = [clean_query]
            if clean_no_punct.lower() != clean_query.lower() and clean_no_punct not in search_queries:
                search_queries.append(clean_no_punct)
            if clean_norm.lower() not in [q.lower() for q in search_queries]:
                search_queries.append(clean_norm)

            base_part = re.split(r'[-–—:,]', clean_query)[0].strip()
            if base_part and base_part.lower() != clean_query.lower():
                search_queries.append(base_part)
            words = clean_query.split()
            if len(words) > 1 and words[0].lower() not in [q.lower() for q in search_queries]:
                search_queries.append(words[0])

            for q in search_queries:
                try:
                    r_search = _garden_http_get(
                        f"{garden_base}/api/search?q={requests.utils.quote(q)}",
                        session=garden_session, timeout=10
                    )
                    if r_search and r_search.status_code == 200:
                        candidates = r_search.json().get("tv_shows", [])
                        best_score = 0
                        for cand in candidates:
                            cand_title = cand.get("title", "")
                            c_norm = unicodedata.normalize('NFKD', cand_title).encode('ASCII', 'ignore').decode('utf-8').lower()
                            score = max(
                                _fuzz.token_sort_ratio(clean_norm.lower(), c_norm),
                                _fuzz.token_sort_ratio(clean_query.lower(), cand_title.lower())
                            )
                            if score > best_score:
                                best_score = score
                                series_path = cand.get("url", "")
                        if best_score >= 60 and series_path:
                            _multimovies_garden_show_cache[cache_key] = series_path
                            break
                        else:
                            series_path = None
                except Exception:
                    pass

        if not series_path:
            return 0

        # 1. Fetch series page with ?season={season} to handle any season (including absolute episode numbered shows like S18)
        season_url = f"{garden_base}{series_path}?season={season}"
        r_ep = _garden_http_get(season_url, session=garden_session, timeout=12)

        # 2. Fallback to /season/{season}/episode/1 if season_url failed
        if not r_ep or r_ep.status_code != 200:
            ep1_url = f"{garden_base}{series_path}/season/{season}/episode/1"
            r_ep = _garden_http_get(ep1_url, session=garden_session, timeout=12)

        if not r_ep or r_ep.status_code != 200:
            return 0

        # Parse seasonEpisodeNumbers from raw script
        m = re.search(r'seasonEpisodeNumbers\s*=\s*(\[[^\]]+\])', r_ep.text)
        if m:
            ep_nums = json.loads(m.group(1))
            if ep_nums:
                max_ep = max(ep_nums)
                logger.info("count_multimovies_garden_episodes: %s S%d -> %d (max_ep=%d, %d eps)", show_name, season, max_ep, max_ep, len(ep_nums))
                return max_ep

        # Check for <option value="{season}">Season {season} · (\d+) episodes</option>
        m_opt = re.search(rf'Season\s*{season}\s*·\s*(\d+)\s*episodes', r_ep.text, re.I)
        if m_opt:
            cnt = int(m_opt.group(1))
            logger.info("count_multimovies_garden_episodes: %s S%d -> %d episodes from option text", show_name, season, cnt)
            return cnt

    except Exception as exc:
        logger.debug("count_multimovies_garden_episodes error for '%s' S%d: %s", show_name, season, exc)

    return 0


def resolve_multimovies_episode(show_name: str, season: int = 1, episode: int = 1, is_movie: bool = False, direct_url: str = "") -> Optional[str]:
    """
    Extract video embed from Multimovies for episodes or movies.
    First tries Multimovies new theme (m1.multimovies.courses / multimovies.garden), then falls back to the
    old WordPress/Dooplay-based domains (multimovies.casa, etc.).

    direct_url: if provided, skip search and use this Multimovies show/movie page URL directly.
    """
    # Try multimovies (new theme) first unless direct_url points explicitly to old domains
    if not direct_url or any(d in direct_url for d in MULTIMOVIES_GARDEN_DOMAINS):
        garden_result = resolve_multimovies_garden_episode(
            show_name, season=season, episode=episode, is_movie=is_movie, direct_url=direct_url
        )
        if garden_result and any(d in garden_result for d in ("morencius.com", "smoothpre.com", "hanerix.com")):
            return garden_result
        # If garden failed and direct_url was for garden, try Cineverse/Modiplay for morencius/hanerix
        if direct_url and any(d in direct_url for d in MULTIMOVIES_GARDEN_DOMAINS):
            modi = extract_modiplay_stream(show_name, season=season, episode=episode, is_movie=is_movie)
            if modi and any(d in modi for d in ("morencius.com", "smoothpre.com", "hanerix.com")):
                return modi
            return None

    from rapidfuzz import fuzz

    show_query = re.sub(r"\(Season \d+\)", "", show_name, flags=re.IGNORECASE).strip()
    show_query = re.sub(r"Season \d+", "", show_query, flags=re.IGNORECASE).strip()
    show_query = _STRIP_TAGS.sub("", show_query).strip()
    show_query = re.sub(r'\s{2,}', ' ', show_query).strip()

    clean_query = re.sub(r'\s*\([^)]*\)', '', show_query).strip()
    clean_query = clean_query.replace('\u2019', "'").replace('\u2018', "'").replace('\u201c', '"').replace('\u201d', '"').replace('\u2013', '-').replace('\u2014', '-')
    target_slug = re.sub(r'[^a-z0-9]+', '-', clean_query.lower()).strip('-')

    link_prefix = "/movies/" if is_movie else "/tvshows/"
    cache_key = (clean_query.lower(), is_movie)

    # Check cached Multimovies show page URL
    cached_show_url = _multimovies_show_cache.get(cache_key)
    if cached_show_url == "NOT_FOUND":
        return extract_modiplay_stream(show_name, season=season, episode=episode, is_movie=is_movie)
    if cached_show_url and not direct_url:
        direct_url = cached_show_url

    # If a manual Multimovies URL was provided, prepend it to the domain list with its own domain
    override_domains = []
    if direct_url:
        for d in MULTIMOVIES_DOMAINS:
            if d in direct_url:
                override_domains = [d]
                break

    domains_to_try = override_domains + [d for d in MULTIMOVIES_DOMAINS if d not in override_domains]

    for domain in domains_to_try:
        base_url = f"https://{domain}"
        try:
            # If a manual or cached URL was given for this domain, skip search and use it directly
            if direct_url and domain in direct_url:
                m_ep_slug = re.search(r'/episodes/([^/]+)-(\d+)x(\d+)(?:-\d+)?/?', direct_url)
                if m_ep_slug:
                    target_url = f"{base_url}/tvshows/{m_ep_slug.group(1)}/"
                else:
                    target_url = direct_url
                logger.debug("Multimovies using target URL: %s", target_url)
            else:
                target_url = None

            if not target_url:
                search_url = f"{base_url}/?s={requests.utils.quote(clean_query)}"
                r_search = requests.get(search_url, headers=HEADERS, timeout=5)

                if r_search.status_code == 200:
                    soup_search = BeautifulSoup(r_search.text, "html.parser")

                    candidates = []
                    for a in soup_search.find_all("a", href=True):
                        if link_prefix in a["href"]:
                            href = a["href"]
                            text = a.get_text(strip=True)
                            if href not in [c["href"] for c in candidates]:
                                candidates.append({"href": href, "text": text})

                    best_score = 0
                    for cand in candidates:
                        m_slug = re.search(rf'{link_prefix}([^/]+)', cand["href"])
                        cand_slug = m_slug.group(1) if m_slug else ""

                        score_slug = fuzz.token_sort_ratio(target_slug, cand_slug)
                        clean_text = re.sub(r'watch|online|free|hd|full|series|movie|multimovies|\(\d{4}\)', '', cand["text"], flags=re.IGNORECASE).strip()
                        score_text = fuzz.token_sort_ratio(clean_query.lower(), clean_text.lower())
                        score = max(score_slug, score_text)

                        if score > best_score:
                            best_score = score
                            target_url = cand["href"]

                    if best_score >= 65 and target_url:
                        _multimovies_show_cache[cache_key] = target_url
                        logger.debug("Multimovies matched '%s' -> '%s' (score %d)", show_query, target_url, best_score)
                    else:
                        target_url = None

            if not target_url:
                # Progressive slug shortening: try full slug, then remove trailing word(s) one-by-one
                path_prefix = "/movies/" if is_movie else "/tvshows/"
                slug_parts = target_slug.split("-")
                for n_parts in range(len(slug_parts), max(2, len(slug_parts) - 3), -1):
                    short_slug = "-".join(slug_parts[:n_parts])
                    fallback_url = f"{base_url}{path_prefix}{short_slug}/"
                    try:
                        r_fallback = requests.get(fallback_url, headers=HEADERS, timeout=5)
                        if r_fallback.status_code == 200:
                            fb_soup = BeautifulSoup(r_fallback.text, "html.parser")
                            fb_title = fb_soup.find("title").get_text() if fb_soup.find("title") else ""
                            if _is_title_match(show_name, fb_title, min_score=65):
                                target_url = fallback_url
                                logger.debug("Multimovies fallback matched '%s' -> '%s'", show_query, fallback_url)
                                break
                    except Exception:
                        pass

            if not target_url:
                # For TV shows: try direct episode URL using slug candidates (bypasses show page search)
                if not is_movie:
                    slug_parts = target_slug.split("-")
                    found_direct = False
                    for n_parts in range(len(slug_parts), max(2, len(slug_parts) - 3), -1):
                        short_slug = "-".join(slug_parts[:n_parts])
                        base_ep = f"{base_url}/episodes/{short_slug}-{season}x{episode}"
                        for suffix in ["", "-2", "-3", "-4"]:
                            try_url = f"{base_ep}{suffix}/"
                            try:
                                r_direct = requests.get(try_url, headers=HEADERS, timeout=7)
                                if (r_direct.status_code == 200 and
                                    len(r_direct.text) > 1000 and
                                    "404 Not Found" not in r_direct.text and
                                    "Page not found" not in r_direct.text and
                                    "Hostname.one" not in r_direct.text):
                                    soup_direct = BeautifulSoup(r_direct.text, "html.parser")
                                    dir_title = soup_direct.find("title").get_text() if soup_direct.find("title") else ""
                                    if not _is_title_match(show_name, dir_title, min_score=65):
                                        logger.debug("Multimovies direct ep title mismatch: '%s' vs '%s'", show_name, dir_title)
                                        continue

                                    # Found a valid episode page — resolve it
                                    direct_embed = _parse_direct_embed_code(r_direct.text)
                                    if direct_embed:
                                        logger.info("Resolved UG2 via direct episode URL %s -> %s", try_url, direct_embed)
                                        return direct_embed
                                    pl_direct = soup_direct.select_one("#playeroptionsul li[data-post], .server-item[data-post]")
                                    pid_direct = pl_direct.get("data-post") if pl_direct else None
                                    if pid_direct:
                                        ajax_url = f"{base_url}/wp-admin/admin-ajax.php"
                                        for nu in range(1, 6):
                                            try:
                                                r_ajax = requests.post(
                                                    ajax_url,
                                                    data={"action": "doo_player_ajax", "post": pid_direct, "nume": str(nu), "type": "tv"},
                                                    headers={**HEADERS, "Referer": try_url, "X-Requested-With": "XMLHttpRequest"},
                                                    timeout=6,
                                                )
                                                if r_ajax.status_code == 200:
                                                    emb = r_ajax.json().get("embed_url", "")
                                                    if "gdmirrorbot.nl" in emb:
                                                        result = resolve_gdmirrorbot_embed(emb)
                                                        if result:
                                                            return result
                                                    elif "iqsmartgames.com" in emb:
                                                        result = resolve_iqsmartgames_embed(emb)
                                                        if result:
                                                            return result
                                                    parsed = _parse_direct_embed_code(emb)
                                                    if parsed:
                                                        return parsed
                                            except Exception:
                                                pass
                                    found_direct = True
                                    break  # Exit suffix loop
                            except Exception:
                                pass
                        if found_direct:
                            break  # Exit slug parts loop
                logger.debug("Multimovies no match for '%s' on %s", show_query, domain)
                continue

            # For Movies: target_url IS the movie page containing the post_id & player options!
            if is_movie:
                r_movie = requests.get(target_url, headers=HEADERS, timeout=7)
                if r_movie.status_code != 200:
                    continue

                soup_movie = BeautifulSoup(r_movie.text, "html.parser")
                mov_title = soup_movie.find("title").get_text() if soup_movie.find("title") else ""
                if not _is_title_match(show_name, mov_title, min_score=65):
                    logger.warning("Multimovies movie title mismatch: '%s' vs page '%s' — skipping", show_name, mov_title)
                    continue

                direct_page_result = _parse_direct_embed_code(r_movie.text)
                if direct_page_result:
                    logger.info("Resolved Server UG2 via direct movie embed -> %s", direct_page_result)
                    return direct_page_result

                soup_movie = BeautifulSoup(r_movie.text, "html.parser")
                player_li = soup_movie.select_one("#playeroptionsul li[data-post], .server-item[data-post]")
                post_id = player_li.get("data-post") if player_li else None
                if not post_id:
                    m_pid = re.search(r'\"post_id\"[^\d]*(\d+)', r_movie.text)
                    post_id = m_pid.group(1) if m_pid else None
                if not post_id:
                    body = soup_movie.find("body")
                    if body:
                        for cls in body.get("class", []):
                            if cls.startswith("postid-"):
                                post_id = cls.replace("postid-", "")
                                break

                if not post_id:
                    continue

                ajax_url = f"{base_url}/wp-admin/admin-ajax.php"
                for nume in range(1, 6):
                    try:
                        r_ajax = requests.post(
                            ajax_url,
                            data={"action": "doo_player_ajax", "post": post_id, "nume": str(nume), "type": "movie"},
                            headers={**HEADERS, "Referer": target_url, "X-Requested-With": "XMLHttpRequest"},
                            timeout=6,
                        )
                        if r_ajax.status_code == 200:
                            j = r_ajax.json()
                            embed_raw = j.get("embed_url", "")
                            if "gdmirrorbot.nl" in embed_raw:
                                final_url = resolve_gdmirrorbot_embed(embed_raw)
                                if final_url:
                                    return final_url
                            elif "iqsmartgames" in embed_raw:
                                final_url = resolve_iqsmartgames_embed(embed_raw)
                                if final_url:
                                    return final_url
                            parsed_direct = _parse_direct_embed_code(embed_raw)
                            if parsed_direct:
                                return parsed_direct
                    except Exception:
                        pass
                continue

            # For TV Shows:
            r_show = requests.get(target_url, headers=HEADERS, timeout=7)
            if r_show.status_code != 200:
                continue

            soup_show = BeautifulSoup(r_show.text, "html.parser")
            ep_pattern = f"{season}x{episode}"
            ep_page_url = None
            for a in soup_show.find_all("a", href=True):
                if "/episodes/" in a["href"] and ep_pattern in a["href"]:
                    ep_page_url = a["href"]
                    break

            if not ep_page_url:
                slug_match = re.search(r'/(?:tvshows|episodes)/([^/]+?)(?:-\d+x\d+)?/', target_url)
                if slug_match:
                    slug = slug_match.group(1)
                    base_ep = f"{base_url}/episodes/{slug}-{season}x{episode}"
                    # Try bare URL first, then disambiguation suffixes (-2, -3, -4)
                    for suffix in ["", "-2", "-3", "-4"]:
                        try_url = f"{base_ep}{suffix}/"
                        try:
                            r_try = requests.get(try_url, headers=HEADERS, timeout=5)
                            if r_try.status_code == 200:
                                ep_page_url = try_url
                                break
                        except Exception:
                            pass
                if not ep_page_url:
                    continue

            r_ep = requests.get(ep_page_url, headers=HEADERS, timeout=7)
            if r_ep.status_code != 200:
                continue

            direct_page_result = _parse_direct_embed_code(r_ep.text)
            if direct_page_result:
                logger.info("Resolved Server UG2 via direct episode embed -> %s", direct_page_result)
                return direct_page_result

            soup_ep = BeautifulSoup(r_ep.text, "html.parser")
            player_li = soup_ep.select_one("#playeroptionsul li[data-post], .server-item[data-post]")
            post_id = player_li.get("data-post") if player_li else None
            if not post_id:
                m_pid = re.search(r'\"post_id\"[^\d]*(\d+)', r_ep.text)
                post_id = m_pid.group(1) if m_pid else None

            if not post_id:
                continue

            ajax_url = f"{base_url}/wp-admin/admin-ajax.php"
            for nume in range(1, 6):
                try:
                    r_ajax = requests.post(
                        ajax_url,
                        data={"action": "doo_player_ajax", "post": post_id, "nume": str(nume), "type": "tv"},
                        headers={**HEADERS, "Referer": ep_page_url, "X-Requested-With": "XMLHttpRequest"},
                        timeout=6,
                    )
                    if r_ajax.status_code == 200:
                        j = r_ajax.json()
                        embed_raw = j.get("embed_url", "")
                        if "gdmirrorbot.nl" in embed_raw:
                            final_url = resolve_gdmirrorbot_embed(embed_raw)
                            if final_url:
                                return final_url
                        elif "iqsmartgames" in embed_raw:
                            final_url = resolve_iqsmartgames_embed(embed_raw)
                            if final_url:
                                return final_url
                        parsed_direct = _parse_direct_embed_code(embed_raw)
                        if parsed_direct:
                            return parsed_direct
                except Exception:
                    pass

        except Exception as exc:
            logger.debug("Multimovies attempt failed on %s: %s", domain, exc)

    _multimovies_show_cache[cache_key] = "NOT_FOUND"
    modi_fallback = extract_modiplay_stream(show_name, season=season, episode=episode, is_movie=is_movie)
    if modi_fallback and any(d in modi_fallback for d in ("morencius.com", "smoothpre.com", "hanerix.com")):
        return modi_fallback
    return None


_multimovies_ep_count_cache: dict[tuple, int] = {}


def count_multimovies_episodes(show_name: str, season: int = 1, direct_url: str = "") -> int:
    """
    Count how many episodes Multimovies has for a given show/season.
    First tries multimovies.garden (new theme), then falls back to old WordPress domains.
    Returns 0 if the show isn't found.
    """
    from rapidfuzz import fuzz

    cache_key = (show_name.lower().strip(), season)
    if cache_key in _multimovies_ep_count_cache:
        return _multimovies_ep_count_cache[cache_key]

    # Try Multimovies (new theme) first (unless direct_url explicitly points to old domains)
    if not direct_url or any(d in direct_url for d in MULTIMOVIES_GARDEN_DOMAINS):
        garden_count = count_multimovies_garden_episodes(show_name, season=season, direct_url=direct_url)
        if garden_count > 0:
            _multimovies_ep_count_cache[cache_key] = garden_count
            return garden_count
        # If direct_url pointed to garden specifically, don't fall through
        if direct_url and any(d in direct_url for d in MULTIMOVIES_GARDEN_DOMAINS):
            _multimovies_ep_count_cache[cache_key] = 0
            return 0

    show_query = re.sub(r"\(Season \d+\)", "", show_name, flags=re.IGNORECASE).strip()
    show_query = re.sub(r"Season \d+", "", show_query, flags=re.IGNORECASE).strip()
    show_query = _STRIP_TAGS.sub("", show_query).strip()
    show_query = re.sub(r'\s{2,}', ' ', show_query).strip()
    clean_query = re.sub(r'\s*\([^)]*\)', '', show_query).strip()
    clean_query = clean_query.replace('\u2019', "'").replace('\u2018', "'").replace('\u201c', '"').replace('\u201d', '"').replace('\u2013', '-').replace('\u2014', '-')
    target_slug = re.sub(r'[^a-z0-9]+', '-', clean_query.lower()).strip('-')

    # Check if resolve_multimovies_episode already cached the show URL
    cache_key_show = (clean_query.lower(), False)
    cached_show_url = _multimovies_show_cache.get(cache_key_show)
    if cached_show_url and cached_show_url != "NOT_FOUND":
        direct_url = cached_show_url

    ep_pattern = re.compile(rf'/episodes/[^/]+-{season}x(\d+)', re.I)
    max_ep = 0

    for domain in MULTIMOVIES_DOMAINS:
        base_url = f"https://{domain}"
        target_url = None

        try:
            # Use direct/cached URL for this domain if available
            if direct_url and domain in direct_url:
                target_url = direct_url
            elif direct_url and not target_url:
                # Try the cached URL on this domain by swapping the domain
                from urllib.parse import urlparse as _up
                parsed = _up(direct_url)
                target_url = f"https://{domain}{parsed.path}"

            # Search if we don't have a show URL
            if not target_url:
                search_url = f"{base_url}/?s={requests.utils.quote(clean_query)}"
                r_search = requests.get(search_url, headers=HEADERS, timeout=8)
                if r_search.status_code == 200:
                    soup_s = BeautifulSoup(r_search.text, "html.parser")
                    best_score = 0
                    for a in soup_s.find_all("a", href=True):
                        if "/tvshows/" in a["href"]:
                            m_slug = re.search(r'/tvshows/([^/]+)', a["href"])
                            cand_slug = m_slug.group(1) if m_slug else ""
                            score = fuzz.token_sort_ratio(target_slug, cand_slug)
                            if score > best_score:
                                best_score = score
                                target_url = a["href"]
                    if best_score < 65:
                        target_url = None

            # If still not found, try direct slug
            if not target_url:
                target_url = f"{base_url}/tvshows/{target_slug}/"

            if not target_url:
                continue

            # Fetch the show page and count episode links for the season
            r_show = requests.get(target_url, headers=HEADERS, timeout=8)
            if r_show.status_code != 200:
                continue
            soup_show = BeautifulSoup(r_show.text, "html.parser")
            for a in soup_show.find_all("a", href=True):
                m = ep_pattern.search(a["href"])
                if m:
                    ep_num = int(m.group(1))
                    if ep_num > max_ep:
                        max_ep = ep_num

            # Check for paginated show pages (page 2, 3...) for long shows
            if max_ep > 0:
                for page_num in range(2, 30):
                    pg_url = f"{target_url.rstrip('/')}?page={page_num}"
                    try:
                        pg_r = requests.get(pg_url, headers=HEADERS, timeout=8)
                        if pg_r.status_code != 200:
                            break
                        found_new = False
                        for a in BeautifulSoup(pg_r.text, "html.parser").find_all("a", href=True):
                            m = ep_pattern.search(a["href"])
                            if m:
                                ep_num = int(m.group(1))
                                if ep_num > max_ep:
                                    max_ep = ep_num
                                    found_new = True
                        if not found_new:
                            break
                    except Exception:
                        break

            if max_ep > 0:
                # Cache the show URL for future calls to resolve_multimovies_episode
                if not _multimovies_show_cache.get(cache_key_show):
                    _multimovies_show_cache[cache_key_show] = target_url
                break

        except Exception as exc:
            logger.debug("count_multimovies_episodes error on %s for %s: %s", domain, show_name, exc)

    _multimovies_ep_count_cache[cache_key] = max_ep
    logger.info("count_multimovies_episodes: %s S%d → %d episodes", show_name, season, max_ep)
    return max_ep


# ─────────────────────────────────────────────────────────────────
#  All-episodes extractor
# ─────────────────────────────────────────────────────────────────

from concurrent.futures import ThreadPoolExecutor, as_completed


def _fetch_single_episode_streams(
    ep_num: int,
    show_name: str,
    season: int,
    toonstream_url: str = "",
    multimovies_url: str = "",
    satoru_url: str = "",
    extract_mg: bool = True,
) -> Optional[Dict]:
    logger.info("Extracting streams for %s S%dE%02d…", show_name, season, ep_num)

    # Server UG: WatchAnimeWorld (Zephyrix via Frosty Worker) or Toonstream AS-CDN
    pg_url = None
    if USE_WATCHANIMEWORLD:
        logger.info("Using WatchAnimeWorld.one for Server UG: %s S%dE%02d", show_name, season, ep_num)
        ug_url = extract_watchanimeworld_stream(show_name, season=season, episode=ep_num)
        if not ug_url:
            logger.info("WatchAnimeWorld returned None for %s S%dE%02d, falling back to Toonstream", show_name, season, ep_num)
            ug_url, _, pg_url = extract_toonstream_all_servers(
                show_name, season=season, episode=ep_num, direct_url=toonstream_url
            )
    else:
        ug_url, _, pg_url = extract_toonstream_all_servers(
            show_name, season=season, episode=ep_num, direct_url=toonstream_url
        )

    # Server UG2: Multimovies
    multi_url = resolve_multimovies_episode(
        show_name, season=season, episode=ep_num, direct_url=multimovies_url
    )
    # Server MG: fetch directly from satoru.one
    mg_url = resolve_satoru_episode(show_name, season=season, episode=ep_num, direct_url=satoru_url) if extract_mg else None

    if ug_url or multi_url or mg_url or pg_url:
        return {
            "episode_num": ep_num,
            "ruby_url":    ug_url,
            "multi_url":   multi_url,
            "mg_url":      mg_url,
            "pg_url":      pg_url,
        }
    return None


def fetch_all_episodes_data(
    show_name: str,
    season: int,
    total_episodes: int,
    start_from: int = 1,
    toonstream_url: str = "",
    multimovies_url: str = "",
    satoru_url: str = "",
    extract_mg: bool = True,
) -> List[Dict]:
    """
    Fetch Ruby + Server 2 + Satoru stream URLs for ALL episodes of a show/season in parallel.
    Only includes episodes where at least one stream was found.
    Returns list of dicts sorted by episode_num: [{episode_num, ruby_url, multi_url, mg_url}]
    """
    # Pre-fetch episode map once to prevent thread races and rate-limiting
    ts_map = None
    try:
        ts_map = _get_or_fetch_toonstream_episode_map(show_name, season)
        if ts_map and not multimovies_url:
            min_ts = min(ts_map.keys())
            if start_from < min_ts:
                start_from = min_ts
    except Exception:
        pass

    ep_nums = list(range(start_from, total_episodes + 1))
    if not ep_nums:
        return []

    episodes_data = []
    max_workers = min(5, len(ep_nums))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_ep = {
            executor.submit(
                _fetch_single_episode_streams,
                ep, show_name, season, toonstream_url, multimovies_url, satoru_url,
                extract_mg
            ): ep
            for ep in ep_nums
        }
        for future in as_completed(future_to_ep):
            try:
                res = future.result()
                if res:
                    episodes_data.append(res)
            except Exception as exc:
                ep = future_to_ep[future]
                logger.warning("Error fetching streams for ep %d: %s", ep, exc)

    episodes_data.sort(key=lambda x: x["episode_num"])
    return episodes_data



# ─────────────────────────────────────────────────────────────────
#  Pixeldrain & HubCloud Direct Download Resolver
# ─────────────────────────────────────────────────────────────────

def convert_pixeldrain_to_direct(url: str) -> str:
    """Convert Pixeldrain URL (dev/com/net/cx) to direct download API link."""
    if not url:
        return ""
    m = re.search(r'https?://(?:www\.)?(?:pixeldrain\.(?:dev|com|net)|pd\.cx)/(?:u|api/file|file)?/?([a-zA-Z0-9_-]+)', url, re.IGNORECASE)
    if m:
        file_id = m.group(1)
        return f"https://pixeldrain.com/api/file/{file_id}?download"
    return url


def resolve_hubcloud_to_pixeldrain(hc_url: str) -> Optional[str]:
    """
    Resolve a HubCloud link to a direct Pixeldrain download URL.

    The step-2 page uses a JS override trick:
      HTML <a> href contains a decoy/dummy Pixeldrain ID
      A <script> tag then overwrites the href with the REAL URL:
        var pxl = "https://pixeldrain.dev/u/REAL_ID";
        document.getElementById("pxl-1").href = pxl;

    So we extract from the JS variable first, then fall back to <a> hrefs.
    Returns None if no valid Pixeldrain link found (episode will be skipped).
    """
    if not hc_url:
        return None
    try:
        r_hc = requests.get(hc_url, headers=HEADERS, timeout=12)
        if r_hc.status_code != 200:
            return None
        soup_hc = BeautifulSoup(r_hc.text, "html.parser")
        gen_btn = soup_hc.find("a", id="download") or soup_hc.find("a", href=re.compile(r"hubcloud\.php"))
        if not gen_btn:
            return None
        step2_url = gen_btn["href"]
        r_step2 = requests.get(step2_url, headers=HEADERS, timeout=12)
        if r_step2.status_code != 200:
            return None

        page_text = r_step2.text

        # --- Priority 1: JS variable override (the real URL, not the decoy href) ---
        # Matches:  var pxl = "https://pixeldrain.dev/u/ABC123";
        js_match = re.search(
            r'var\s+pxl\s*=\s*["\']([^"\']+pixeldrain[^"\']+)["\']',
            page_text, re.IGNORECASE
        )
        if js_match:
            return convert_pixeldrain_to_direct(js_match.group(1))

        # --- Priority 2: Any JS string assignment containing pixeldrain ---
        js_any = re.search(
            r'["\']( https?://(?:www\.)?pixeldrain\.(?:dev|com|net)/[a-zA-Z0-9/_-]+)["\']',
            page_text, re.IGNORECASE
        )
        if js_any:
            return convert_pixeldrain_to_direct(js_any.group(1).strip())

        # --- Fallback: static <a> href (may be a decoy, but better than nothing) ---
        soup_step2 = BeautifulSoup(page_text, "html.parser")
        for a in soup_step2.find_all("a", href=True):
            if "pixeldrain" in a["href"].lower():
                return convert_pixeldrain_to_direct(a["href"])

        return None
    except Exception as exc:
        logger.debug("HubCloud resolution error for %s: %s", hc_url, exc)
def _resolve_toonworld4all_redirect_hubcloud(ep_soup: BeautifulSoup) -> Optional[str]:
    """Parse redirect links from archive.toonworld4all.me episode page.

    Filters out exe.io / exeygo.com shortener ad links to prevent posting ad popups to WordPress.
    Returns clean direct URLs (Filepress, GDFlix, Mega, or non-ad Hubcloud links).
    """
    SHORTENER_DOMAINS = ["exe.io", "exeygo.com", "exeygo", "gplinks", "droplink", "clyx"]

    for red_a in ep_soup.find_all("a", href=True):
        r_href = red_a["href"]
        if "/redirect/" in r_href:
            full_r = "https://archive.toonworld4all.me" + r_href if r_href.startswith("/") else r_href
            try:
                res = requests.get(full_r, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, timeout=7)
                real_url = ""
                m_props = re.search(r'window\.__PROPS__\s*=\s*(\{.*?\});', res.text)
                if m_props:
                    props = json.loads(m_props.group(1))
                    link_obj = props.get("link", {})
                    dom = link_obj.get("domain", "")
                    hidden = link_obj.get("hidden", "")
                    if dom and hidden:
                        real_url = f"{dom.rstrip('/')}/{hidden.lstrip('/')}"
                    elif props.get("destination"):
                        real_url = props.get("destination", "")

                if not real_url or not real_url.startswith("http"):
                    soup_res = BeautifulSoup(res.text, "html.parser")
                    code_tag = soup_res.find("code")
                    if code_tag and code_tag.get_text().strip().startswith("http"):
                        real_url = code_tag.get_text().strip()

                if real_url and real_url.startswith("http"):
                    # Reject any shortener ad links
                    if any(s_domain in real_url.lower() for s_domain in SHORTENER_DOMAINS):
                        continue
                    return real_url
            except Exception:
                pass
    return None


def _fetch_soup(target_url: str) -> Optional[BeautifulSoup]:
    """Helper to fetch a URL and return BeautifulSoup instance."""
    try:
        r = requests.get(target_url, headers=HEADERS, timeout=8)
        if r.status_code == 200:
            return BeautifulSoup(r.text, "html.parser")
    except Exception:
        pass
    return None


def extract_hubcloud_from_toonworld4all(url: str, target_season: Optional[int] = None) -> dict[int, str]:
    """
    Extract Hubcloud links directly from Toonworld4All post page (e.g., https://toonworld4all.me/tomb-raider-king-season-1-multi-audio-hindi/)
    or direct Archive Toonworld4All episode page (e.g., https://archive.toonworld4all.me/episode/tomb-raider-king-1x1).
    Returns dict mapping episode_num -> Hubcloud URL.
    """
    if not url:
        return {}

    url = url.strip()
    episodes_map = {}

    # Case A: Direct episode page on archive.toonworld4all.me
    if "archive.toonworld4all.me/episode/" in url:
        m_ep = re.search(r'(\d+)x(\d+)', url)
        ep_num = int(m_ep.group(2)) if m_ep else 1
        ep_soup = _fetch_soup(url)
        if ep_soup:
            h_link = _resolve_toonworld4all_redirect_hubcloud(ep_soup)
            if h_link:
                episodes_map[ep_num] = h_link
        return episodes_map

    # Case B: Main series/season post on toonworld4all.me
    soup = _fetch_soup(url)
    if not soup:
        return {}

    found_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "archive.toonworld4all.me/episode/" in href:
            m_ep = re.search(r'(\d+)x(\d+)', href)
            if m_ep:
                s_num = int(m_ep.group(1))
                ep_num = int(m_ep.group(2))
                if target_season is not None and s_num != target_season:
                    continue
                if (ep_num, href) not in found_links:
                    found_links.append((ep_num, href))

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _fetch_ep_hubcloud(ep_num, href):
        ep_soup = _fetch_soup(href)
        if ep_soup:
            h_link = _resolve_toonworld4all_redirect_hubcloud(ep_soup)
            if h_link:
                return ep_num, h_link
        return ep_num, None

    if found_links:
        with ThreadPoolExecutor(max_workers=min(10, len(found_links))) as executor:
            futures = [executor.submit(_fetch_ep_hubcloud, ep, href) for ep, href in found_links]
            for fut in as_completed(futures):
                try:
                    ep_num, h_link = fut.result()
                    if h_link:
                        episodes_map[ep_num] = h_link
                except Exception as exc:
                    logger.warning("Error resolving TW4All ep hubcloud: %s", exc)

    return dict(sorted(episodes_map.items()))


def extract_downloads_from_url(archive_or_post_url: str, target_season: Optional[int] = None):
    """
    Extract Pixeldrain or Hubcloud direct download URLs from m4ulinks, mdrive, hubcloud, moviesdrive, movies4u, or Toonworld4All URL.
    If target_season is provided, filters download sections to only match that specific season (e.g. Season 1 vs Season 2).
    Returns dict mapping quality -> list of direct URLs.
    """
    if not archive_or_post_url:
        return {}

    if "toonworld4all" in archive_or_post_url.lower():
        tw_map = extract_hubcloud_from_toonworld4all(archive_or_post_url, target_season=target_season)
        if tw_map:
            max_ep = max(tw_map.keys())
            links_list = [tw_map.get(i, "") for i in range(1, max_ep + 1)]
            return {"Hubcloud": links_list}

    _headers = {**HEADERS, "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    quality_map = {}

    try:
        r = requests.get(archive_or_post_url, headers=_headers, timeout=12)
        if r.status_code != 200:
            return {}

        soup = BeautifulSoup(r.text, "html.parser")

        # --- Stage 1: Parse quality -> m4ulinks/hubcloud/mdrive from the source page -----------
        if "m4ulinks.site" in archive_or_post_url or "hubcloud" in archive_or_post_url.lower():
            # URL was already a direct m4ulinks or hubcloud page
            quality_map["720p"] = [archive_or_post_url]
        else:
            # movies4u / moviesdrive / mdrive post page
            dl_div = soup.find("div", class_="download-links-div")
            if dl_div:
                # Parse quality and season from the h3/h4 tags INSIDE the download-links-div block
                current_quality = "720p"
                current_season = None
                for tag in dl_div.find_all(["h4", "h3", "a"]):
                    if tag.name in ("h3", "h4"):
                        text = tag.get_text(separator=" ", strip=True)
                        m_s = re.search(r"\b(?:season|s)\s*(\d+)\b", text, re.IGNORECASE)
                        if m_s:
                            current_season = int(m_s.group(1))
                        m_q = re.search(r"\b(480p|720p|1080p|2160p|4k)\b", text, re.IGNORECASE)
                        if m_q:
                            current_quality = m_q.group(1).lower()
                    elif tag.name == "a":
                        href = tag.get("href", "")
                        if (
                            "m4ulinks" in href.lower()
                            or "hubcloud" in href.lower()
                            or "mdrive" in href.lower()
                        ):
                            if target_season is not None and current_season is not None and current_season != target_season:
                                # Skip download section meant for a different season (e.g. S2 when target is S1)
                                continue
                            quality_map.setdefault(current_quality, [])
                            if href not in quality_map[current_quality]:
                                quality_map[current_quality].append(href)
            else:
                # Fallback: scan whole page for hubcloud/m4ulinks/mdrive links using h-tag quality hints
                current_quality = "720p"
                current_season = None
                for tag in soup.find_all(["h3", "h4", "h5", "a"]):
                    if tag.name == "a":
                        href = tag.get("href", "")
                        if (
                            "hubcloud" in href.lower()
                            or "m4ulinks" in href.lower()
                            or "mdrive" in href.lower()
                        ):
                            if target_season is not None and current_season is not None and current_season != target_season:
                                continue
                            quality_map.setdefault(current_quality, [])
                            if href not in quality_map[current_quality]:
                                quality_map[current_quality].append(href)
                    else:
                        if not tag.find("a"):
                            text = tag.get_text(strip=True)
                            m_s = re.search(r"\b(?:season|s)\s*(\d+)\b", text, re.IGNORECASE)
                            if m_s:
                                current_season = int(m_s.group(1))
        # Filter out 2160p / 4K qualities so only 480p, 720p, 1080p are retained
        for q_key in ["2160p", "4k"]:
            quality_map.pop(q_key, None)

        if not quality_map:
            return {}

        # --- Stage 2: Follow m4ulinks/mdrive pages to collect HubCloud links -----------------
        def _expand_to_hubcloud(url):
            """If url is an m4ulinks/mdrive index page, fetch it and return HubCloud links inside."""
            if "m4ulinks.site" not in url and "mdrive.lol" not in url:
                return [url]  # already a hubcloud or pixeldrain link
            try:
                r2 = requests.get(url, headers=_headers, timeout=12)
                if r2.status_code != 200:
                    return []
                soup2 = BeautifulSoup(r2.text, "html.parser")
                hc_links = [
                    a["href"] for a in soup2.find_all("a", href=True)
                    if "hubcloud" in a["href"].lower()
                ]
                return hc_links if hc_links else []
            except Exception as e:
                logger.debug("m4ulinks expand error for %s: %s", url, e)
                return []

        expanded_map = {}
        for quality, links in quality_map.items():
            hc_links = []
            for link in links:
                hc_links.extend(_expand_to_hubcloud(link))
            if hc_links:
                expanded_map[quality] = hc_links

        if not expanded_map:
            return {}

        # --- Stage 3: Resolve HubCloud links to direct Pixeldrain download URLs ---------------
        resolved_dict = {}
        for quality, hc_links in expanded_map.items():
            pd_links = []
            with ThreadPoolExecutor(max_workers=min(10, max(1, len(hc_links)))) as executor:
                futures = [executor.submit(resolve_hubcloud_to_pixeldrain, link) for link in hc_links]
                for fut in futures:
                    res = fut.result()
                    if res:
                        pd_links.append(res)
            if pd_links:
                resolved_dict[quality] = pd_links

        return resolved_dict
    except Exception as exc:
        logger.debug("Download extraction failed for %s: %s", archive_or_post_url, exc)
        return {}


# ─────────────────────────────────────────────────────────────────
#  Episode Toggle HTML generator
# ─────────────────────────────────────────────────────────────────

def generate_episode_toggle_html(
    episodes_data,
    series_name="Series",
    season_num=1,
):
    """
    Build the WordPress [toggle title="Episode List" state="open"] shortcode block.
    - Skips episodes with no streams and no downloads.
    - Server UG (Ruby) | Server UG2 (Smoothpre/Hanerix) on one line.
    - Download Options button & modal bottom sheet if downloads are present.
    """
    abbr = abbreviate_title(series_name)
    html = '[toggle title="Episode List" state="open"]\n'

    active_eps = [e for e in episodes_data if e.get("ruby_url") or e.get("ag_url") or e.get("multi_url") or e.get("mg_url") or e.get("pg_url") or e.get("downloads")]
    latest_ep_num = max((e.get("episode_num", 1) for e in active_eps), default=None)

    for ep in episodes_data:
        ruby_url  = ep.get("ruby_url")
        ag_url    = ep.get("ag_url")
        multi_url = ep.get("multi_url")
        mg_url    = ep.get("mg_url")
        pg_url    = ep.get("pg_url")
        downloads = ep.get("downloads", {})

        if not ruby_url and not ag_url and not multi_url and not mg_url and not pg_url and not downloads:
            continue

        ep_num      = ep.get("episode_num", 1)
        ep_padded   = str(ep_num).zfill(2)
        dropdown_id = f"dropdown{ep_num}"

        ruby_enc  = encrypt_iframe_url(
            f"{abbr}_S{season_num}_E{ep_padded}_-_Server_UG",  ruby_url
        ) if ruby_url  else None
        ag_enc    = encrypt_iframe_url(
            f"{abbr}_S{season_num}_E{ep_padded}_-_Server_AG",  ag_url
        ) if ag_url   else None
        multi_enc = encrypt_iframe_url(
            f"{abbr}_S{season_num}_E{ep_padded}_-_Server_UG2", multi_url
        ) if multi_url else None
        mg_enc    = encrypt_iframe_url(
            f"{abbr}_S{season_num}_E{ep_padded}_-_Server_MG",  mg_url
        ) if mg_url else None
        pg_enc    = encrypt_iframe_url(
            f"{abbr}_S{season_num}_E{ep_padded}_-_Server_PG",  pg_url
        ) if pg_url else None

        html += f'<div class="episode" onclick="toggleDropdown(\'{dropdown_id}\')">\n'
        html += f'    <span class="episode-number">{ep_num}</span>\n'
        if ep_num == latest_ep_num:
            html += '    <h3>Multiquality <span style="color: #00ffff;"><strong>𝙽𝚎𝚠!</strong></span></h3>\n'
        else:
            html += '    <h3>Multiquality</h3>\n'
        html += '    <i class="fa fa-eye"></i>\n'
        html += '</div>\n'
        html += f'<div class="episode-dropdown" id="{dropdown_id}">\n'

        line = ""
        if ruby_enc:
            line += f'<a href="{ruby_enc}"><button>Server UG <i class="fas fa-play"></i></button></a>'
        if ag_enc:
            line += f'<a href="{ag_enc}"><button>Server AG <i class="fas fa-play"></i></button></a>'
        if multi_enc:
            line += f'<a href="{multi_enc}"><button>Server UG2 <i class="fas fa-play"></i></button></a>'
        if mg_enc:
            line += f'<a href="{mg_enc}"><button>Server MG <i class="fas fa-play"></i></button></a>'
        if pg_enc:
            line += f'<a href="{pg_enc}"><button>Server PG <i class="fas fa-play"></i></button></a>'
        if line:
            html += line

        if downloads:
            dl_dict_lines = [f"        '{q}': '{u}'" for q, u in sorted(downloads.items())]
            dl_dict_str = ",\n".join(dl_dict_lines)
            html += '<div class="separator"></div>\n'
            html += f'<button class="download-toggle-btn" onclick="event.stopPropagation(); openDownloadModal(\'Episode {ep_padded}\', {{\n'
            html += f'{dl_dict_str}\n'
            html += '    })">\n'
            html += '        <i class="fas fa-download"></i> Download Options</button>\n'
        else:
            html += "\n"

        html += "</div>\n"

    MODAL = (
        "\n"
        '<div class="modal-overlay" id="modalOverlay" onclick="closeDownloadModal()"></div>\n'
        "\n"
        "<!-- Download Modal (Bottom Sheet) -->\n"
        '<div class="download-modal" id="downloadModal">\n'
        '    <div class="modal-header">\n'
        '        <div class="modal-title" id="modalTitle">Episode 01</div>\n'
        '        <button class="close-btn" onclick="closeDownloadModal()">&times;</button>\n'
        "    </div>\n"
        "\n"
        '    <div class="download-options-list">\n'
        '        <div class="download-option">\n'
        '            <div class="download-info">\n'
        '                <div class="quality-name">480p - Server UG</div>\n'
        "            </div>\n"
        '            <div class="download-buttons">\n'
        '                <a href="#"><button class="download-btn">Download <i class="fas fa-download"></i></button></a>\n'
        "            </div>\n"
        "        </div>\n"
        "\n"
        '        <div class="download-option">\n'
        '            <div class="download-info">\n'
        '                <div class="quality-name">720p - Server UG</div>\n'
        "            </div>\n"
        '            <div class="download-buttons">\n'
        '                <a href="#"><button class="download-btn">Download <i class="fas fa-download"></i></button></a>\n'
        "            </div>\n"
        "        </div>\n"
        "\n"
        '        <div class="download-option">\n'
        '            <div class="download-info">\n'
        '                <div class="quality-name">1080p - Server UG</div>\n'
        "            </div>\n"
        '            <div class="download-buttons">\n'
        '                <a href="#"><button class="download-btn">Download <i class="fas fa-download"></i></button></a>\n'
        "            </div>\n"
        "        </div>\n"
        "    </div>\n"
        "</div>\n"
        "\n"
        "[/toggle]"
    )
    html += MODAL
    return html

