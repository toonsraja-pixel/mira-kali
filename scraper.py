"""
scraper.py — Scrapes toonworld4all.me for all post listings and individual post data.
"""

import re
import time
import logging
import requests
from bs4 import BeautifulSoup
from typing import Optional, Generator

import socket
import urllib.request
import json

logger = logging.getLogger(__name__)

# ── DNS-Over-HTTPS (DoH) Resolver for bypassing ISP DNS blocks ───────────────
import ssl
from urllib.parse import urlparse

_doh_cache = {}
_orig_getaddrinfo = socket.getaddrinfo

def _is_wp_or_excluded_domain(host: str) -> bool:
    if not host or not isinstance(host, str):
        return True
    h_lower = host.lower()
    if h_lower in ("1.1.1.1", "cloudflare-dns.com", "dns.google", "localhost"):
        return True
    if any(k in h_lower for k in ["googleapis.com", "themoviedb.org", "anilist.co", "myanimelist.net"]):
        return True
    try:
        from pathlib import Path
        sf = Path(__file__).parent / "settings.json"
        if sf.exists():
            with open(sf, encoding="utf-8") as f:
                s = json.load(f)
                wpu = s.get("wp_url", "")
                if wpu:
                    wph = urlparse(wpu).netloc.split(":")[0].lower()
                    if wph and (h_lower == wph or h_lower.endswith("." + wph)):
                        return True
    except Exception:
        pass
    return False

def _resolve_host_doh(host: str):
    if host in _doh_cache:
        return _doh_cache[host]
    try:
        url = f"https://1.1.1.1/dns-query?name={host}&type=A"
        req = urllib.request.Request(url, headers={"accept": "application/dns-json", "User-Agent": "Mozilla/5.0"})
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, context=ctx, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            answers = [ans["data"] for ans in data.get("Answer", []) if ans.get("type") == 1]
            if answers:
                ip = answers[0]
                _doh_cache[host] = ip
                return ip
    except Exception:
        pass
    return None

def custom_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if isinstance(host, str) and not host.replace(".", "").isdigit():
        if not _is_wp_or_excluded_domain(host):
            ip = _resolve_host_doh(host)
            if ip:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port))]
    return _orig_getaddrinfo(host, port, family, type, proto, flags)

socket.getaddrinfo = custom_getaddrinfo
# ──────────────────────────────────────────────────────────────────────────────

BASE_URL = "https://toonworld4all.me"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Patterns to strip from toonworld titles to get the clean show name + season
_QUALITY_PATTERN = re.compile(
    r"\s*[\[\(]?"
    r"(?:Hindi|Tamil|Telugu|Mal|Eng|Jap|Japanese|English|Multi Audio|Dual Audio)[^\]]*"
    r"[\]\)]?"
    r".*$",
    re.IGNORECASE,
)
_RES_PATTERN = re.compile(
    r"\s*\d{3,4}p.*$",
    re.IGNORECASE,
)
_PIPE_PATTERN = re.compile(r"\s*\|.*$")
_BRACKETS_QUALITY = re.compile(r"\s*\[.*?\]")

# Season aliases
_SEASON_WORD = re.compile(
    r"\bSeason\s+(\d+)\b|\bS(\d{1,2})\b",
    re.IGNORECASE,
)
_SEASON_PAREN = re.compile(r"\(Season\s+(\d+)\)", re.IGNORECASE)
# Season range pattern e.g. "Season 1-6" or "Seasons 1-13"
_SEASON_RANGE = re.compile(r"\bSeasons?\s+\d+\s*[-–]\s*(\d+)\b", re.IGNORECASE)


def _get_soup(url: str, retries: int = 3, delay: float = 2.0) -> Optional[BeautifulSoup]:
    """Fetch a URL and return a BeautifulSoup object. Retries on failure."""
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "lxml")
        except Exception as exc:
            logger.warning("Attempt %d failed for %s: %s", attempt + 1, url, exc)
            if attempt < retries - 1:
                time.sleep(delay * (attempt + 1))
    return None


def get_total_pages() -> int:
    """Return the total number of listing pages on the site."""
    soup = _get_soup(BASE_URL)
    if not soup:
        return 1
    # Look for pagination — last page number link
    pagination = soup.select("a.page-numbers")
    nums = []
    for a in pagination:
        try:
            nums.append(int(a.get_text(strip=True)))
        except ValueError:
            pass
    return max(nums) if nums else 1


def iter_post_links(page: int) -> list[dict]:
    """
    Return a list of {title, url} dicts from a single listing page.
    Excludes top featured sliders and popular posts grid.
    """
    url = f"{BASE_URL}/page/{page}/" if page > 1 else BASE_URL
    soup = _get_soup(url)
    if not soup:
        return []

    # Explicitly remove Herald featured area list (.herald-fa-list) and slider items
    for fa in soup.select(".herald-fa-list, .herald-fa-item, [class*='herald-fa']"):
        fa.decompose()

    results = []

    # Target main content container to avoid featured slider / header / sidebar
    main_area = soup.select_one("#content, #main, .site-main, .post-listing, #the-post-list, .posts-list, main")
    search_root = main_area if main_area else soup

    for heading in search_root.select("article h2 a, article h3 a, .entry-title a, .post-title a"):
        # Check ancestors up to 5 levels for slider/featured/carousel/herald-fa containers
        is_slider = False
        curr = heading.parent
        for _ in range(5):
            if not curr or not hasattr(curr, "get"):
                break
            cls = " ".join(curr.get("class", [])) if isinstance(curr.get("class"), list) else str(curr.get("class", ""))
            cid = str(curr.get("id", ""))
            if any(skip in (cls + " " + cid).lower() for skip in ["slider", "featured", "carousel", "td-big-grid", "popular", "herald-fa"]):
                is_slider = True
                break
            curr = curr.parent

        if is_slider:
            continue

        href = heading.get("href", "")
        text = heading.get_text(strip=True)
        if href and text and "toonworld4all.me" in href:
            results.append({"title": text, "url": href})

    # Deduplicate preserving order
    seen = set()
    unique = []
    for item in results:
        if item["url"] not in seen:
            seen.add(item["url"])
            unique.append(item)
    return unique


def clean_title(raw_title: str) -> tuple[str, int, bool]:
    """
    Clean a toonworld4all post title into (show_name, season_number, has_explicit_season).
    Strips format tags (BluRay, WEB-DL), language/audio tags, resolution, parenthesized years, etc.
    """
    title = raw_title
    # Normalize Unicode smart quotes, apostrophes, and dashes to standard ASCII
    title = title.replace('\u2019', "'").replace('\u2018', "'").replace('\u201c', '"').replace('\u201d', '"').replace('\u2013', '-').replace('\u2014', '-')

    # Extract season number & check if season was explicitly stated
    season = 1
    has_explicit_season = False

    m_range = _SEASON_RANGE.search(title)
    if m_range:
        season = int(m_range.group(1))
        has_explicit_season = True
    else:
        m = _SEASON_WORD.search(title)
        if m:
            season = int(m.group(1) or m.group(2))
            has_explicit_season = True

    # Strip quality / resolution / language info FIRST
    title = _RES_PATTERN.sub("", title)
    title = _PIPE_PATTERN.sub("", title)
    title = _BRACKETS_QUALITY.sub("", title)
    title = _QUALITY_PATTERN.sub("", title)

    # Comprehensive junk tag stripper (BluRay, WEB-DL, Dual Audio, HEVC, 1080p, etc.)
    title = re.sub(
        r'\b(?:'
        r'bl?u[\-\s]?ray|bdrip|brrip|webrip|web[\-\s]?dl|hdrip|dvdrip|dvd|hdtv|10bit|hevc|x264|x265|ddp\d\.\d|aac|esub|msub|subbed|sub|dubbed|dub'
        r'|multi[\s\-]?audio|dual[\s\-]?audio|hindi|english|japanese|tamil|telugu|malayalam|kannada|korean|chinese'
        r'|480p|720p|1080p|2160p|4k|fhd|uhd|hd'
        r'|complete(?:\s+(?:series|collection|pack|season))?|batch|uncensored|censored|remastered|extended|uncut'
        r'|episodes?|all[\s\-]?episodes'
        r')\b',
        '',
        title,
        flags=re.IGNORECASE,
    )

    # Strip season range patterns & single season words
    title = _SEASON_RANGE.sub("", title)
    title = re.sub(r"\bComplete\s+(?:Series|Collection|Pack|Season)\b", "", title, flags=re.IGNORECASE)
    title = _SEASON_WORD.sub("", title)
    title = _SEASON_PAREN.sub("", title)

    # Strip parenthesized year tags e.g. (2014), (2002), (2012), (2020), (2022), (1995)
    title = re.sub(r'\s*\((?:19|20)\d{2}\)', '', title)

    # Clean up stray punctuation / trailing dashes / commas / brackets / colons
    title = re.sub(r'[\s\-\|\[\]\(\):,]+$', '', title).strip()
    title = re.sub(r'^\s*[\s\-\|\[\]\(\):,]+', '', title).strip()
    title = re.sub(r'\s{2,}', ' ', title)

    return title, season, has_explicit_season


def clean_wp_title(wp_title: str) -> tuple[str, int]:
    """
    Clean a WordPress post title into (show_name, season_number).
    E.g. "Naruto Shippuden (Season 2)" → ("Naruto Shippuden", 2)
    """
    title = wp_title
    title = title.replace('\u2019', "'").replace('\u2018', "'").replace('\u201c', '"').replace('\u201d', '"').replace('\u2013', '-').replace('\u2014', '-')
    season = 1
    m = _SEASON_PAREN.search(title)
    if m:
        season = int(m.group(1))
        title = _SEASON_PAREN.sub("", title).strip()
    else:
        m2 = _SEASON_WORD.search(title)
        if m2:
            season = int(m2.group(1) or m2.group(2))
            title = _SEASON_WORD.sub("", title).strip()

    title = re.sub(r"[-–,\s]+$", "", title).strip()
    return title, season


def parse_bollyflix_post(soup: BeautifulSoup, url: str) -> dict:
    """
    Parse a BollyFlix post page.
    Does NOT extract/set image_url or body_image_url (per user preference for manual image management).
    Extracts Full Name, Language, Season, Quality, Source, Genres, Synopsis, and Categories.
    """
    data: dict = {
        "source_url": url,
        "image_url": "",      # User requested: no featured image upload
        "body_image_url": "", # User requested: no post body image
    }

    details = {}
    content_div = soup.select_one(".thecontent, .entry-content, .post-content, article") or soup
    for el in content_div.find_all(["li", "p", "div"]):
        txt = el.get_text(" ", strip=True)
        if ":" in txt and len(txt) < 400:
            # Handle multiple key-value pairs per line or element
            for line in re.split(r'(?=\b(?:Full Name|Movie Title|Title|Language|Released Year|Release Date|Year|Size|File Size|Quality|Source|Genre|Genres|Run Time|Running Time|Runtime|Duration|Cast|Format|Subtitle|Subtitles|Audio)\s*:)', txt, flags=re.IGNORECASE):
                line = line.strip()
                if ":" in line:
                    parts = line.split(":", 1)
                    k = parts[0].replace("•", "").replace("*", "").strip().lower()
                    v = parts[1].strip()
                    if k and v and k not in details:
                        details[k] = v

    full_name = details.get("full name") or details.get("movie title") or details.get("title") or ""
    if not full_name:
        og_title = soup.find("meta", property="og:title")
        raw_og = og_title["content"] if og_title and og_title.get("content") else ""
        raw_og = re.sub(r"\s*[|–~]\s*BollyFlix.*$", "", raw_og, flags=re.IGNORECASE).strip()
        raw_og = re.sub(r"^Download\s+", "", raw_og, flags=re.IGNORECASE).strip()

        m_name = re.search(r"^(.*?)(?:\(Season|Multi|Dual|480p|720p|WEB|~|$)", raw_og, re.IGNORECASE)
        if m_name and m_name.group(1).strip():
            full_name = m_name.group(1).strip()
        else:
            full_name = raw_og

    show_name, season, has_explicit_season = clean_title(full_name)

    season_val = details.get("season", "")
    if season_val:
        m_s = re.search(r"(\d+)", season_val)
        if m_s:
            season = int(m_s.group(1))
            has_explicit_season = True

    data["show_name"] = show_name
    data["season"] = season
    data["has_explicit_season"] = has_explicit_season
    data["raw_toon_title"] = f"{show_name} (Season {season})" if season > 1 else show_name

    raw_lang = details.get("language") or "Multi Audio {Hindi-English-Japanese}"
    if "quality" in raw_lang.lower():
        m_lang = re.split(r"(?i)\bquality\b\s*:?", raw_lang, maxsplit=1)
        raw_lang = m_lang[0].strip()
        if len(m_lang) > 1 and not details.get("quality"):
            details["quality"] = m_lang[1].strip()

    data["language"] = raw_lang
    data["org_run"] = details.get("released year") or details.get("release date") or details.get("year") or ""
    # Runtime: ONLY set if actual duration is present in post — NEVER assign file size!
    data["runtime"] = (
        details.get("run time")
        or details.get("running time")
        or details.get("runtime")
        or details.get("duration")
        or ""
    )
    data["quality"] = details.get("quality") or "480p | 720p | 1080p"
    source_val = details.get("source") or ""
    if "disclaimer" in source_val.lower() or "imdb" in source_val.lower() or len(source_val) > 40:
        source_val = "WEB-DL"
    data["source"] = source_val or "WEB-DL"
    data["genre"] = details.get("genres") or details.get("genre") or ""

    synopsis = ""
    summary_span = content_div.find(id="summary")
    if summary_span:
        synopsis = summary_span.get_text(" ", strip=True).replace("Summary:", "").replace("<b>Summary: </b>", "").strip()

    if not synopsis:
        for heading in content_div.find_all(["h2", "h3", "h4", "strong"]):
            h_text = heading.get_text(strip=True).lower()
            if "storyline" in h_text or "synopsis" in h_text:
                nxt = heading.find_next_sibling(["p", "div"])
                if nxt:
                    synopsis = nxt.get_text(strip=True)
                break

    if not synopsis:
        og_desc = soup.find("meta", property="og:description")
        if og_desc and og_desc.get("content"):
            synopsis = og_desc["content"]

    data["synopsis"] = synopsis.strip()

    raw_cats = [g.strip() for g in data["genre"].split(",") if g.strip()]
    content_html = str(content_div).lower()
    if "anime" not in [c.lower() for c in raw_cats] and ("animation" in data["genre"].lower() or "japan" in content_html or "oshi no ko" in show_name.lower()):
        raw_cats.append("Anime")
    data["categories"] = raw_cats

    is_movie = False
    url_lower = url.lower()
    if "/movie/" in url_lower or "/movies/" in url_lower or re.search(r"-movie(?:/|$)", url_lower):
        is_movie = True
    elif "movie" in data["genre"].lower() or "movie" in full_name.lower() or "movie" in show_name.lower():
        is_movie = True
    elif not has_explicit_season and not details.get("season"):
        is_movie = True

    data["is_movie"] = is_movie
    if is_movie:
        data["content_type"] = "movie"
    elif "anime" in [c.lower() for c in raw_cats] or _is_anime_by_genre(data["genre"]):
        data["content_type"] = "anime"
    else:
        data["content_type"] = "show"

    return data


def scrape_post(url: str) -> dict:
    """
    Scrape a single post page and return structured data.

    Returns a dict with keys:
        title, image_url, genre, org_run, runtime, language,
        episodes, quality, synopsis, source_url, raw_toon_title
    """
    soup = _get_soup(url)
    if not soup:
        return {}

    # Check for BollyFlix page
    if "bollyflix" in url.lower() or soup.select_one(".thecontent") or (soup.title and "bollyflix" in soup.title.string.lower()):
        return parse_bollyflix_post(soup, url)

    data: dict = {"source_url": url}

    # ── Featured image URL from og:image ──────────────────────────────────
    og_img = soup.find("meta", property="og:image")
    data["image_url"] = og_img["content"] if og_img and og_img.get("content") else ""

    # ── Title ─────────────────────────────────────────────────────────────
    og_title = soup.find("meta", property="og:title")
    raw_title = og_title["content"] if og_title and og_title.get("content") else ""
    # Strip site name suffix
    raw_title = re.sub(r"\s*[|–]\s*ToonWorld4All.*$", "", raw_title, flags=re.IGNORECASE).strip()
    # Also strip "Download " prefix
    raw_title = re.sub(r"^Download\s+", "", raw_title, flags=re.IGNORECASE).strip()
    data["raw_toon_title"] = raw_title

    show_name, season, has_explicit_season = clean_title(raw_title)
    data["show_name"] = show_name
    data["season"] = season
    data["has_explicit_season"] = has_explicit_season

    # ── Description for Taqyeem synopsis ──────────────────────────────────
    og_desc = soup.find("meta", property="og:description")
    og_desc_text = og_desc["content"] if og_desc and og_desc.get("content") else ""

    content_div = soup.select_one(".entry-content, .post-content, article .content")

    body_image_url = ""
    if content_div:
        img_el = content_div.find("img")
        if img_el:
            body_image_url = img_el.get("src") or img_el.get("data-src") or img_el.get("data-lazy-src") or ""

    if not body_image_url:
        body_image_url = data.get("image_url", "")
    data["body_image_url"] = body_image_url

    # ── Categories ONLY from post header/meta area (ignore header/nav/sidebar) ─
    raw_cats = []
    meta_box = soup.select_one(".entry-categories, .post-cat, .meta-cat, .entry-meta, .cat-links, .post-meta, article header, .entry-header")
    cat_root = meta_box if meta_box else soup.select_one("article")

    if cat_root:
        for cat_a in cat_root.select("a[href*='/category/']"):
            c_text = cat_a.get_text(strip=True)
            if c_text and c_text.lower() not in [c.lower() for c in raw_cats]:
                if not any(skip in c_text.lower() for skip in ["home", "contact", "dmca", "author"]):
                    raw_cats.append(c_text)
    data["categories"] = raw_cats

    info_text = ""
    synopsis = ""

    if content_div:
        # Grab all text paragraphs
        paras = content_div.find_all(["p", "div"], recursive=True)
        for para in paras:
            text = para.get_text(" ", strip=True)
            if "Genre:" in text and "Language:" in text:

                info_text = text
            if "Synopsis:" in text or "synopsis" in text.lower():
                synopsis = re.sub(r"(?i)Synopsis:\s*", "", text).strip()

        # If no explicit synopsis paragraph, use og:description
        if not synopsis:
            synopsis = og_desc_text

    def _extract(pattern: str, text: str) -> str:
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1).strip() if m else ""

    data["genre"] = _extract(r"Genre:\s*(.+?)(?:Org\.|$)", info_text)
    data["org_run"] = _extract(r"Org\.\s*run:\s*(.+?)(?:Running|$)", info_text)
    data["runtime"] = _extract(r"Running\s*time:\s*(.+?)(?:Language|$)", info_text)
    data["language"] = _extract(r"Language:\s*(.+?)(?:Episodes|$)", info_text)
    data["episodes"] = _extract(r"Episodes:\s*(.+?)(?:Quality|Subtitle|$)", info_text)
    data["quality"] = _extract(r"Quality:\s*(.+?)(?:Encoder|$)", info_text)
    data["synopsis"] = synopsis

    # Detect content type & is_movie
    title_lower = raw_title.lower()
    cat_string = " ".join(raw_cats).lower()
    ep_val = str(data.get("episodes", "")).strip().lower()
    runtime_val = str(data.get("runtime", "")).lower()
    url_lower = url.lower()

    is_movie = False
    # Check URL path first — most reliable signal (e.g. /movie/, /movies/, or slug ends with -movie)
    if (
        "/movie/" in url_lower
        or "/movies/" in url_lower
        or re.search(r"-movie(?:/|$)", url_lower)
    ):
        is_movie = True
    elif "movie" in title_lower or "film" in title_lower or "movie" in cat_string or "film" in cat_string:
        is_movie = True
    elif not has_explicit_season:
        if ep_val in ["1", "movie", "film"] or ("hr" in runtime_val or "min" in runtime_val and "eps" not in runtime_val):
            is_movie = True

    data["is_movie"] = is_movie
    if is_movie:
        data["content_type"] = "movie"
    elif any(k in title_lower for k in ["anime", "manga"]) or _is_anime_by_genre(data.get("genre", "")):
        data["content_type"] = "anime"
    else:
        data["content_type"] = "show"  # cartoon / TV show

    return data


def _is_anime_by_genre(genre: str) -> bool:
    anime_keywords = ["animation", "anime", "fantasy", "isekai", "shounen", "seinen"]
    return any(k in genre.lower() for k in anime_keywords)


def iter_all_posts(
    stop_flag: list,
    progress_callback=None,
    page_start: int = 1,
    page_end: Optional[int] = None,
) -> Generator[dict, None, None]:
    """
    Generator that yields post dicts one at a time, scraping every page.

    Args:
        stop_flag:          A mutable list [False]; set [True] to stop the generator.
        progress_callback:  Called with (current_page, total_pages) after each page.
        page_start:         First page to scrape (default 1).
        page_end:           Last page to scrape (None = auto-detect).
    """
    total = page_end or get_total_pages()
    logger.info("Total pages detected: %d", total)

    for page_num in range(page_start, total + 1):
        if stop_flag[0]:
            logger.info("Stop flag set — halting scraper at page %d", page_num)
            return

        links = iter_post_links(page_num)
        logger.info("Page %d/%d — found %d posts", page_num, total, len(links))

        for link_info in links:
            if stop_flag[0]:
                return
            post_data = scrape_post(link_info["url"])
            if post_data:
                post_data.setdefault("raw_toon_title", link_info["title"])
                yield post_data
            time.sleep(0.3)  # polite crawl delay

        if progress_callback:
            progress_callback(page_num, total)

        time.sleep(1.0)  # page-level delay
