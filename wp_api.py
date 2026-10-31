"""
wp_api.py — WordPress REST API wrapper.
Handles: auth, fetching existing posts/slugs/categories, creating draft posts.
"""

import re
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)


# ── PHP serializer (for Taqyeem criteria array) ────────────────────────────

def _php_serialize_taqyeem(criteria: list[dict]) -> str:
    """
    Build a PHP-serialized string for Taqyeem's `taqyeem_review` meta.

    Expected input:  [{"criteria": "MAL", "score": "75"}]
    Output example:  a:1:{i:0;a:2:{s:8:"criteria";s:3:"MAL";s:5:"score";s:2:"75";}}
    """
    def _str(s: str) -> str:
        s = str(s)
        return f's:{len(s.encode("utf-8"))}:"{s}";'

    outer = f"a:{len(criteria)}:{{"
    for i, item in enumerate(criteria):
        inner = f"a:{len(item)}:{{"
        for k, v in item.items():
            inner += _str(k) + _str(v)
        inner += "}"
        outer += f"i:{i};" + inner
    outer += "}"
    return outer



def _parse_airing_title(title: str) -> Optional[dict]:
    """Parse airing/completed post titles in any variation:
    - 'Show Name (Season X) - Episode Y Added!'
    - 'Show Name Season X - Episode Y Added!'
    - 'Show Name - Episode Y Added! (Season X)'
    - 'Show Name - Episode Y Added!'
    - 'Show Name (Season X) - All Episode(s) Added!'
    - 'Show Name Season X - All Episode(s) Added!'
    - 'Show Name - All Episode(s) Added! (Season X)'
    - 'Show Name - All Episode(s) Added!'
    - 'Show Name (Season X)' or 'Show Name Season X' or 'Show Name S0X'
    - 'Show Name' (plain title, defaults season=1)
    Handles both hyphen (-) and em-dash (–/—) separators, optional exclamation marks, and tildes (~).
    """
    import html
    if not title:
        return None
    t = html.unescape(title).strip()

    # 1. Pattern: Episode Y Added!
    m_ep = re.search(r'[-–—]\s*Episode\s*(\d+)\s*Added!?\s*(?:\(?\s*(?:Season|S)\s*(\d+)\s*\)?)?', t, re.IGNORECASE)
    if m_ep:
        ep_num = int(m_ep.group(1))
        s_after = int(m_ep.group(2)) if m_ep.group(2) else None
        left = t[:m_ep.start()].strip()
        m_s_left = re.search(r'(?:\(?\s*(?:Season|S)\s*(\d+)\s*\)?)\s*$', left, re.IGNORECASE)
        if m_s_left:
            season = int(m_s_left.group(1))
            show_name = left[:m_s_left.start()].strip(' (-–—')
        elif s_after:
            season = s_after
            show_name = left.strip(' (-–—')
        else:
            season = 1
            show_name = left.strip(' (-–—')
        return {
            "show_name": show_name,
            "season": season,
            "current_ep": ep_num,
            "is_airing_title": True,
        }

    # 2. Pattern: All Episode(s) Added!
    m_all = re.search(r'[-–—]\s*All\s+Episodes?\s+Added!?[~]?\s*(?:\(?\s*(?:Season|S)\s*(\d+)\s*\)?)?', t, re.IGNORECASE)
    if m_all:
        s_after = int(m_all.group(1)) if m_all.group(1) else None
        left = t[:m_all.start()].strip()
        m_s_left = re.search(r'(?:\(?\s*(?:Season|S)\s*(\d+)\s*\)?)\s*$', left, re.IGNORECASE)
        if m_s_left:
            season = int(m_s_left.group(1))
            show_name = left[:m_s_left.start()].strip(' (-–—')
        elif s_after:
            season = s_after
            show_name = left.strip(' (-–—')
        else:
            season = 1
            show_name = left.strip(' (-–—')
        return {
            "show_name": show_name,
            "season": season,
            "current_ep": 999,
            "is_airing_title": False,
            "is_all_episodes_title": True,
        }

    # 3. Use clean_title for posts with raw titles (e.g. drafts with quality/audio tags, or plain titles)
    try:
        from scraper import clean_title
        c_show, c_season, _ = clean_title(t)
        if c_show:
            c_show = c_show.replace('\u2019', "'").replace('\u2018', "'").replace('\u2013', '-').replace('\u2014', '-')
            return {
                "show_name": c_show,
                "season": c_season,
                "current_ep": 0,
                "is_airing_title": False,
            }
    except Exception:
        pass

    # 4. Fallback: plain regex or raw title
    m_s = re.search(r'(?:\(?\s*(?:Season|S)\s*(\d+)\s*\)?)\s*$', t, re.IGNORECASE)
    if m_s:
        season = int(m_s.group(1))
        show_name = t[:m_s.start()].strip(' (-–—')
    else:
        season = 1
        show_name = t.strip()

    return {
        "show_name": show_name,
        "season": season,
        "current_ep": 0,
        "is_airing_title": False,
    }



def parse_existing_post_streams(content: str) -> dict:
    """
    Parse HTML content of an existing post to detect which episodes and servers are already added.
    Also decodes the Cloudflare worker base64 payloads to recover the original stream URLs,
    so they can be reused without re-fetching from Toonstream/Multimovies.

    Returns dict: {ep_num: {has_ruby, has_multi, ruby_url, multi_url}}
    """
    import base64
    import json as _json

    # Recognize both old and new Cloudflare Worker domains
    WORKER_DOMAINS = [
        "frosty-river-7490.orekihautaro78.workers.dev",
        "new-frosty.toonsraja-df3.workers.dev",
    ]

    def _is_worker_href(href: str) -> bool:
        return any(d in href for d in WORKER_DOMAINS)

    def _decode_worker_url(href: str) -> str:
        """Decode a worker ?data=<base64> href back to the original stream URL."""
        try:
            m = re.search(r'[?&]data=([A-Za-z0-9+/=]+)', href)
            if not m:
                return ""
            padded = m.group(1) + "=" * (4 - len(m.group(1)) % 4)
            payload = _json.loads(base64.b64decode(padded).decode("utf-8", errors="ignore"))
            return payload.get("u", "")
        except Exception:
            return ""

    ep_data = {}
    dropdowns = re.findall(
        r'<div\s+class=["\']episode-dropdown["\']\s+id=["\']dropdown(\d+)["\']\>(.*?)</div>',
        content, re.DOTALL
    )
    for ep_num_str, inner_html in dropdowns:
        ep_num = int(ep_num_str)
        has_ruby = ("Server UG " in inner_html or "Server UG<" in inner_html)
        has_ag = ("Server AG" in inner_html or "Server_AG" in inner_html)
        has_multi = "Server UG2" in inner_html
        has_mg = "Server MG" in inner_html
        has_pg = "Server PG" in inner_html

        ruby_url = ""
        ag_url = ""
        multi_url = ""
        mg_url = ""
        pg_url = ""

        # Extract hrefs from all <a> tags in this dropdown
        a_matches = re.findall(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', inner_html, re.DOTALL)
        for href, inner_a in a_matches:
            if "openDownloadModal" in inner_a or "download-toggle-btn" in inner_a:
                continue

            label_clean = re.sub(r'<[^>]+>', '', inner_a).strip()
            url = _decode_worker_url(href) if _is_worker_href(href) else href

            if "Server MG" in label_clean or "Server_MG" in label_clean:
                mg_url = url
                has_mg = True
            elif "Server PG" in label_clean or "Server_PG" in label_clean:
                pg_url = url
                has_pg = True
            elif "Server AG" in label_clean or "Server_AG" in label_clean:
                ag_url = url
                has_ag = True
            elif "Server UG2" in label_clean or "Server UG 2" in label_clean:
                multi_url = url
                has_multi = True
            elif "Server UG" in label_clean:
                ruby_url = url
                has_ruby = True

        ep_data[ep_num] = {
            "has_ruby":  has_ruby,
            "has_ag":    has_ag,
            "has_multi": has_multi,
            "has_mg":    has_mg,
            "has_pg":    has_pg,
            "ruby_url":  ruby_url,
            "ag_url":    ag_url,
            "multi_url": multi_url,
            "mg_url":    mg_url,
            "pg_url":    pg_url,
        }

    # 2. Shortcodes fallback (if post uses [toggle title="Episode X"] ... [button text="Server MG" link="..."])
    if not ep_data:
        toggles = re.findall(r'\[toggle\s+title=["\']Episode\s*(\d+)["\'][^\]]*\](.*?)\[/toggle\]', content, re.DOTALL | re.IGNORECASE)
        for ep_num_str, inner_txt in toggles:
            ep_num = int(ep_num_str)
            has_ruby, has_ag, has_multi, has_mg, has_pg = False, False, False, False, False
            ruby_url, ag_url, multi_url, mg_url, pg_url = "", "", "", "", ""

            btns = re.findall(r'\[button\s+[^\]]*link=["\']([^"\']+)["\'][^\]]*text=["\']([^"\']+)["\'][^\]]*\]', inner_txt, re.IGNORECASE)
            if not btns:
                btns = re.findall(r'\[button\s+[^\]]*text=["\']([^"\']+)["\'][^\]]*link=["\']([^"\']+)["\'][^\]]*\]', inner_txt, re.IGNORECASE)
                btns = [(l, t) for t, l in btns]

            for href, label in btns:
                url = _decode_worker_url(href) if _is_worker_href(href) else href
                if "Server MG" in label or "Server_MG" in label:
                    mg_url = url
                    has_mg = True
                elif "Server PG" in label or "Server_PG" in label:
                    pg_url = url
                    has_pg = True
                elif "Server AG" in label or "Server_AG" in label:
                    ag_url = url
                    has_ag = True
                elif "Server UG2" in label or "Server UG 2" in label:
                    multi_url = url
                    has_multi = True
                elif "Server UG" in label:
                    ruby_url = url
                    has_ruby = True

            ep_data[ep_num] = {
                "has_ruby":  has_ruby,
                "has_ag":    has_ag,
                "has_multi": has_multi,
                "has_mg":    has_mg,
                "has_pg":    has_pg,
                "ruby_url":  ruby_url,
                "ag_url":    ag_url,
                "multi_url": multi_url,
                "mg_url":    mg_url,
                "pg_url":    pg_url,
            }

    return ep_data


def parse_existing_post_downloads(content: str) -> dict[int, dict[str, str]]:
    """
    Parse HTML content of an existing post to detect existing Pixeldrain download URLs per episode and quality.
    Returns dict: {ep_num: {"480p": "url", "720p": "url", ...}}
    """
    ep_downloads = {}
    matches = re.findall(
        r"openDownloadModal\s*\(\s*['\"]Episode\s*(\d+)['\"]\s*,\s*(\{.*?\})\s*\)",
        content,
        re.DOTALL
    )
    for ep_num_str, dict_str in matches:
        ep_num = int(ep_num_str)
        q_map = {}
        for q_match in re.finditer(r"['\"]([\w\s]+)['\"]\s*:\s*['\"](https?://[^'\"]+)['\"]", dict_str):
            q_map[q_match.group(1).lower().strip()] = q_match.group(2).strip()
        if q_map:
            ep_downloads[ep_num] = q_map
    return ep_downloads


class WordPressAPI:
    """Thin wrapper around the WordPress REST API v2."""

    def __init__(self, site_url: str, username: str, app_password: str):
        self.site_url = site_url
        self.username = username
        self.app_password = app_password
        self.base = site_url.rstrip("/") + "/wp-json/wp/v2"
        self.auth = (username, app_password.replace(" ", ""))
        self.session = requests.Session()
        self.session.auth = self.auth
        self.session.headers.update({
            "User-Agent": "WP-AutoPoster/1.0",
            "Accept": "application/json",
        })
        self._categories_cache: Optional[dict[str, int]] = None  # name→id
        self._existing_titles_cache: Optional[set[str]] = None

    @staticmethod
    def parse_existing_post_streams(content: str) -> dict:
        return parse_existing_post_streams(content)

    @staticmethod
    def parse_existing_post_downloads(content: str) -> dict:
        return parse_existing_post_downloads(content)

    # ── Connection test ────────────────────────────────────────────────────

    def test_connection(self) -> tuple[bool, str]:
        """Returns (success, message)."""
        try:
            resp = self.session.get(f"{self.base}/users/me", timeout=15)
            if resp.status_code == 200:
                name = resp.json().get("name", "Unknown")
                return True, f"Connected as: {name}"
            elif resp.status_code == 401:
                return False, "Authentication failed — check username/app password."
            else:
                return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        except requests.exceptions.ConnectionError:
            return False, "Cannot reach the site. Check the URL."
        except Exception as exc:
            return False, str(exc)

    # ── Categories ─────────────────────────────────────────────────────────

    def get_all_categories(self) -> dict[str, int]:
        """Return dict of {category_name_lower: id} for all existing categories."""
        if self._categories_cache is not None:
            return self._categories_cache

        categories: dict[str, int] = {}
        page = 1
        while True:
            try:
                resp = self.session.get(
                    f"{self.base}/categories",
                    params={"per_page": 100, "page": page},
                    timeout=15,
                )
                data = resp.json()
                if not data or not isinstance(data, list):
                    break
                for cat in data:
                    categories[cat["name"].lower()] = cat["id"]
                if len(data) < 100:
                    break
                page += 1
            except Exception as exc:
                logger.error("Failed to fetch categories page %d: %s", page, exc)
                break

        self._categories_cache = categories
        logger.info("Loaded %d categories from WordPress", len(categories))
        return categories

    def match_categories(self, keywords: list[str]) -> list[int]:
        """
        Given a list of candidate category names/keywords from toonworld,
        return the IDs of any existing WP categories that match.
        Uses exact match, synonym alias mapping, and high-confidence fuzzy matching.
        ONLY returns category IDs that ALREADY exist on the user's WordPress site.
        """
        from rapidfuzz import fuzz

        all_cats = self.get_all_categories()
        matched_ids: list[int] = []
        seen: set[int] = set()

        # Common category synonyms between toonworld and user site
        synonyms = {
            "animated series": ["animated series", "animated shows", "cartoon shows", "animated cartoons"],
            "cartoon shows": ["cartoon shows", "animated shows", "animated series"],
            "animated movies": ["animated movies", "movies", "hollywood movies"],
            "hindi dub": ["hindi dub", "hindi dubbed", "hindi cartoons"],
            "eng dub": ["eng dub", "english dub", "eng cartoons"],
            "tamil dub": ["tamil dub", "tamil"],
            "telugu dub": ["telugu dub", "telugu"],
            "airing": ["airing", "ongoing", "currently airing", "airing series"],
            "completed": ["completed", "complete", "finished", "completed series"],
        }

        for keyword in keywords:
            kw_lower = keyword.lower().strip()
            if not kw_lower:
                continue

            # Build list of variants to check
            variants = [kw_lower]
            if kw_lower in synonyms:
                variants.extend(synonyms[kw_lower])

            # Check variants against user's existing categories
            found = False
            for var in variants:
                # 1. Exact match
                if var in all_cats:
                    cat_id = all_cats[var]
                    if cat_id not in seen:
                        matched_ids.append(cat_id)
                        seen.add(cat_id)
                    found = True
                    break

                # 2. Fuzzy match (score >= 82)
                for cat_name, cat_id in all_cats.items():
                    if cat_id in seen:
                        continue
                    if fuzz.ratio(var, cat_name) >= 82:
                        matched_ids.append(cat_id)
                        seen.add(cat_id)
                        found = True
                        break
                if found:
                    break

        return matched_ids

    def infer_category_keywords(self, post_data: dict) -> list[str]:
        """
        Build candidate category names prioritized by scraped toonworld page categories.
        """
        keywords: list[str] = []

        # Scraped categories directly from toonworld post page
        page_cats = post_data.get("categories", [])
        for c in page_cats:
            if c and c.lower() not in [k.lower() for k in keywords]:
                keywords.append(c)

        # Status category (airing vs completed)
        if post_data.get("is_airing"):
            keywords.append("airing")
        elif post_data.get("is_completed"):
            keywords.append("completed")

        # Fallbacks based on content_type and title if page_cats was empty
        if not keywords:
            c_type = post_data.get("content_type", "")
            if c_type == "anime":
                keywords.extend(["anime", "animated series", "animated shows"])
            elif c_type == "movie":
                keywords.extend(["animated movies", "movies"])
            else:
                keywords.extend(["animated series", "animated shows", "cartoon shows"])

        return keywords


        # Quality
        if "1080p" in raw_title:
            keywords.append("1080p")
        if "720p" in raw_title:
            keywords.append("720p")
        if "480p" in raw_title:
            keywords.append("480p")

        return keywords

    # ── Existing posts ─────────────────────────────────────────────────────

    def get_all_post_titles(self) -> set[tuple[str, int]]:
        """
        Fetch all post titles from the WP site (both published + draft).
        Returns a set of tuples: (normalized_core_title, season_number).
        """
        import html

        if self._existing_titles_cache is not None:
            return self._existing_titles_cache

        titles: set[tuple[str, int, bool]] = set()
        for status in ("publish", "draft", "pending", "private"):
            page = 1
            while True:
                try:
                    resp = self.session.get(
                        f"{self.base}/posts",
                        params={
                            "per_page": 100,
                            "page": page,
                            "status": status,
                            "fields": "title,slug",
                        },
                        timeout=20,
                    )
                    data = resp.json()
                    if not data or not isinstance(data, list):
                        break
                    for post in data:
                        raw_title = post.get("title", {}).get("rendered", "")
                        if raw_title:
                            norm_core, norm_season, norm_is_movie = _normalize_title_for_matching(raw_title)
                            if norm_core:
                                titles.add((norm_core, norm_season, norm_is_movie))
                    if len(data) < 100:
                        break
                    page += 1
                except Exception as exc:
                    logger.error("Failed to fetch posts (status=%s, page=%d): %s", status, page, exc)
                    break

        self._existing_titles_cache = titles
        logger.info("Loaded %d existing post titles from WordPress", len(titles))
        return titles

    def post_exists(self, show_name: str, season: int, is_movie: bool = False) -> bool:
        """
        Check whether a post for the given show + season already exists.
        Uses strict exact-match or high-threshold fuzzy matching (≥88).
        Content type (movie vs series) must agree to prevent cross-type overwrites.
        """
        from rapidfuzz import fuzz

        existing = self.get_all_post_titles()
        target_core, _, _ = _normalize_title_for_matching(f"{show_name} Season {season}")
        if not target_core:
            target_core = show_name.lower().strip()

        for (ex_core, ex_season, ex_is_movie) in existing:
            # Content type must match (movie can't match series and vice-versa)
            if ex_is_movie != is_movie:
                continue
            # Must match season number (movies always season=1)
            if ex_season != season:
                continue
            # Only exact match OR strict fuzzy (no loose substring containment)
            if target_core == ex_core:
                return True
            if fuzz.token_sort_ratio(target_core, ex_core) >= 88:
                return True

        return False

    # ── Single-post lookup (any status) ────────────────────────────────────

    def lookup_post_for_update(self, show_name: str, season: int, is_movie: bool = False) -> dict | None:
        """
        Search all posts (publish, draft, pending, private) for one matching
        show_name + season (or season range, e.g. Season 8-11).
        Returns a dict compatible with check_and_update's post structure, or None if not found.
        Uses strict matching to avoid overwriting similarly named but different posts.
        """
        import html as _html
        from rapidfuzz import fuzz

        target_seasons = set()
        if isinstance(season, str):
            m_range = re.search(r'(\d+)\s*[-–—to]+\s*(\d+)', season)
            if m_range:
                target_seasons = set(range(int(m_range.group(1)), int(m_range.group(2)) + 1))
            else:
                m_s = re.search(r'\d+', season)
                if m_s:
                    target_seasons = {int(m_s.group(0))}
        elif isinstance(season, (list, tuple, set)) and season:
            target_seasons = {int(x) for x in season if str(x).isdigit()}
        elif isinstance(season, int):
            target_seasons = {season}

        if not target_seasons:
            target_seasons = {1}

        # For matching, normalize the core show name
        target_core, _, _ = _normalize_title_for_matching(show_name)
        if not target_core:
            target_core = show_name.lower().strip()

        for status in ("publish", "draft", "pending", "private"):
            page = 1
            while True:
                try:
                    resp = self.session.get(
                        f"{self.base}/posts",
                        params={
                            "per_page": 100,
                            "page": page,
                            "status": status,
                            "context": "edit",
                        },
                        timeout=20,
                    )
                    data = resp.json()
                    if not data or not isinstance(data, list):
                        break
                    for post in data:
                        raw_title = _html.unescape(
                            post.get("title", {}).get("rendered", "")
                        )
                        ex_core, ex_season, ex_is_movie = _normalize_title_for_matching(raw_title)

                        # Determine all seasons covered by this post (supports ranges like Season 8-11)
                        m_post_range = re.search(r'[\(\[\{]?\s*seasons?\s*(\d+)\s*[-–—to]+\s*(\d+)\s*[\)\]\}]?', raw_title, re.IGNORECASE)
                        if m_post_range:
                            post_seasons = set(range(int(m_post_range.group(1)), int(m_post_range.group(2)) + 1))
                        else:
                            post_seasons = {ex_season}

                        # Check if this post has an explicit season marker (Season X / SXX)
                        has_explicit_season = bool(
                            re.search(r'\bseasons?\s+\d+\b|\bS\d{1,2}\b|\(seasons?\s+\d+', raw_title, re.IGNORECASE)
                        )
                        if is_movie:
                            # Movie lookups: skip any post that explicitly has a season number
                            # (those are TV series posts, not movie posts)
                            if has_explicit_season:
                                continue
                        else:
                            # Series lookups: skip movie-tagged posts and wrong seasons
                            if ex_is_movie:
                                continue
                            # Season check: must match exact season or overlap post's season range
                            if not (target_seasons & post_seasons):
                                continue
                        # Strict matching only — no loose substring containment
                        if (
                            target_core == ex_core
                            or fuzz.token_sort_ratio(target_core, ex_core) >= 88
                        ):
                            parsed = _parse_airing_title(raw_title) or {}
                            return {
                                "id":           post.get("id"),
                                "show_name":    show_name,
                                "season":       min(target_seasons),
                                "current_ep":   parsed.get("current_ep", 0),
                                "raw_title":    raw_title,
                                "content":      post.get("content", {}).get("rendered", ""),
                                "content_raw":  post.get("content", {}).get("raw", ""),
                                "categories":   post.get("categories", []),
                            }
                    if len(data) < 100:
                        break
                    page += 1
                except Exception as exc:
                    logger.error(
                        "lookup_post_for_update error (status=%s, page=%d): %s",
                        status, page, exc,
                    )
                    break
        return None

    # ── Airing posts updater helper ────────────────────────────────────────

    def get_airing_posts(self) -> list[dict]:
        """
        Fetch all WordPress posts (draft, publish, pending, private) that are airing/ongoing
        (either title contains '- Episode X Added!' or post has 'Airing' category).
        Returns list of dicts: [{id, show_name, season, current_ep, raw_title, content, categories}]
        """
        import html

        airing_posts = []
        airing_cat_ids = set()

        all_cats = self.get_all_categories()
        for cat_name, cat_id in all_cats.items():
            if any(k in cat_name for k in ["airing", "ongoing"]):
                airing_cat_ids.add(cat_id)

        for status in ("publish", "draft", "pending", "private"):
            page = 1
            while True:
                try:
                    resp = self.session.get(
                        f"{self.base}/posts",
                        params={
                            "per_page": 100,
                            "page": page,
                            "status": status,
                            "context": "edit",   # needed to get content.raw (shortcodes)
                        },
                        timeout=20,
                    )
                    data = resp.json()
                    if not data or not isinstance(data, list):
                        break
                    for post in data:
                        raw_title = html.unescape(post.get("title", {}).get("rendered", ""))
                        # rendered = processed HTML (for parsing existing stream URLs)
                        content_rendered = post.get("content", {}).get("rendered", "")
                        # raw = original shortcode text (for pattern replacement when updating)
                        content_raw = post.get("content", {}).get("raw", "") or content_rendered
                        post_id = post.get("id")
                        cats = post.get("categories", [])

                        parsed = _parse_airing_title(raw_title)
                        is_airing_cat = any(c in airing_cat_ids for c in cats)

                        if parsed and (parsed["is_airing_title"] or is_airing_cat):
                            parsed.update({
                                "id":               post_id,
                                "raw_title":        raw_title,
                                "content":          content_rendered,   # used for parsing URLs
                                "content_raw":      content_raw,        # used for shortcode replacement
                                "categories":       cats,
                            })
                            airing_posts.append(parsed)

                    if len(data) < 100:
                        break
                    page += 1
                except Exception as exc:
                    logger.error("Failed to fetch posts for airing check (status=%s, page=%d): %s", status, page, exc)
                    break

        logger.info("Found %d airing/ongoing posts on WordPress", len(airing_posts))
        return airing_posts

    def get_all_published_posts(self) -> list[dict]:
        """
        Fetch all published and draft WordPress posts.
        Returns list of dicts: [{id, show_name, season, raw_title, content, categories}]
        """
        import html
        posts = []
        for status in ("publish", "draft"):
            page = 1
            while True:
                try:
                    resp = self.session.get(
                        f"{self.base}/posts",
                        params={
                            "per_page": 100,
                            "page": page,
                            "status": status,
                            "context": "edit",
                        },
                        timeout=20,
                    )
                    data = resp.json()
                    if not data or not isinstance(data, list):
                        break
                    for p in data:
                        raw_title = html.unescape(p.get("title", {}).get("rendered", ""))
                        content_rendered = p.get("content", {}).get("rendered", "")
                        content_raw = p.get("content", {}).get("raw", "") or content_rendered
                        post_id = p.get("id")
                        cats = p.get("categories", [])

                        # Extract show_name and season from title
                        clean_title = re.sub(r'\s*-\s*(Episode\s*\d+\s*Added!|All\s*Episodes\s*Added!)', '', raw_title, flags=re.IGNORECASE).strip()
                        m_season = re.search(r'\(Season\s*(\d+)\)', clean_title, re.IGNORECASE)
                        if m_season:
                            season = int(m_season.group(1))
                            show_name = re.sub(r'\s*\(Season\s*\d+\)', '', clean_title, flags=re.IGNORECASE).strip()
                        else:
                            season = 1
                            show_name = clean_title

                        posts.append({
                            "id":          post_id,
                            "show_name":   show_name,
                            "season":      season,
                            "raw_title":   raw_title,
                            "content":     content_rendered,
                            "content_raw": content_raw,
                            "categories":  cats,
                        })

                    if len(data) < 100:
                        break
                    page += 1
                except Exception as exc:
                    logger.error("Failed to fetch all posts (status=%s, page=%d): %s", status, page, exc)
                    break

        logger.info("Found %d published/draft posts on WordPress", len(posts))
        return posts


    # ── Create post ────────────────────────────────────────────────────────

    def build_post_content(self, post_data: dict) -> str:
        """
        Assemble the WordPress post body HTML using the exact toggle shortcode format.
        Presents each Info attribute cleanly on its own single line with single break (<br/>).
        """

        img_url = post_data.get("body_image_url") or post_data.get("image_url", "")
        season = post_data.get("season")
        genre = post_data.get("genre", "")
        org_run = post_data.get("org_run", "")
        runtime = post_data.get("runtime", "")
        language = post_data.get("language", "")
        episodes = post_data.get("episodes", "")
        quality = post_data.get("quality", "")
        is_movie = post_data.get("is_movie", False) or post_data.get("content_type") == "movie"

        lines = []

        if img_url:
            lines.append(
                f'<img src="{img_url}" width="679" height="960" '
                f'class="alignnone size-medium" />'
            )
            lines.append("")  # Blank space after image

        if season and not is_movie:
            lines.append(f"<strong>Season:</strong> {season}")
        if genre:
            lines.append(f"<strong>Genre:</strong> {genre}")
        if org_run:
            lines.append(f"<strong>Release Date:</strong> {org_run}")
        if runtime:
            lines.append(f"<strong>Running time:</strong> {runtime}")
        if language:
            lines.append(f"<strong>Language:</strong> {language}")
        if episodes and not is_movie:
            lines.append(f"<strong>Episodes:</strong> {episodes}")
        if quality:
            lines.append(f"<strong>Quality:</strong> {quality}")

        # Join metadata items with <br /> so each metadata item appears on a clean new line
        info_inner = "<br />\n".join(lines)

        content = (
            f'[toggle title="Info" state="open"]\n'
            f"{info_inner}\n"
            f'[/toggle]\n\n'
            f'[toggle title="Episode List" state="open"]\n'
            f"<!-- Episodes will be added manually -->\n"
            f"[/toggle]"
        )
        return content

    def set_fifu_image_url(self, post_id: int, image_url: str, title: str = "") -> bool:
        """
        Set FIFU featured image URL directly via XML-RPC custom fields.
        Bypasses WP REST API sanitization and avoids downloading/uploading images to WP media storage.
        """
        if not image_url or not post_id:
            return False

        try:
            import xmlrpc.client
            site_url = self.base.replace("/wp-json/wp/v2", "").rstrip("/")
            proxy = xmlrpc.client.ServerProxy(f"{site_url}/xmlrpc.php")

            post_struct = {
                "custom_fields": [
                    {"key": "fifu_image_url", "value": image_url},
                    {"key": "fifu_image_alt", "value": title},
                    {"key": "_fifu_image_url", "value": image_url},
                    {"key": "_fifu_image_alt", "value": title},
                ]
            }

            res = proxy.wp.editPost(1, self.username, self.app_password, post_id, post_struct)
            if res:
                logger.info("Set FIFU image URL via XML-RPC for post %s -> %s", post_id, image_url)
                return True
        except Exception as exc:
            logger.warning("Failed to set FIFU image URL via XML-RPC for post %s: %s", post_id, exc)

        return False

    def create_draft_post(
        self,
        post_data: dict,
        taqyeem_score: Optional[int],
        tmdb_api_key: str = "",
        subtitle_meta_key: str = "tie_post_sub_title",
    ) -> tuple[bool, str, str, Optional[int]]:
        """
        Create a WordPress draft post from scraped post_data.

        Returns (success, post_edit_url, message, post_id).
        """
        show_name = post_data.get("show_name", "")
        season    = post_data.get("season", 1)
        synopsis  = post_data.get("synopsis", "")
        image_url = post_data.get("image_url") or post_data.get("body_image_url") or ""
        c_type    = post_data.get("content_type", "show")
        is_movie  = post_data.get("is_movie", False) or c_type == "movie"

        # ── Title & subtitle ──────────────────────────────────────────────
        if is_movie:
            m_year = post_data.get("org_run", "")
            if m_year and not re.search(r'\b(19|20)\d{2}\b', show_name):
                wp_title = f"{show_name} ({m_year})"
            else:
                wp_title = show_name
        else:
            wp_title = f"{show_name} (Season {season})"

        # Build language string for subtitle e.g. "Multi Audio [Hindi-English-Japanese]"
        lang_raw  = post_data.get("language", "")
        lang_tags = _extract_lang_tags(lang_raw, post_data.get("raw_toon_title", ""))
        wp_subtitle = f"Multi Audio [{lang_tags}]" if lang_tags else "Multi Audio"

        # ── Content ───────────────────────────────────────────────────────
        content = self.build_post_content(post_data)

        # ── Categories ────────────────────────────────────────────────────
        keywords     = self.infer_category_keywords(post_data)
        category_ids = self.match_categories(keywords)

        # ── Post meta ─────────────────────────────────────────────────────
        meta: dict = {}

        if image_url:
            meta["fifu_image_url"]  = image_url
            meta["fifu_image_alt"]  = wp_title
            meta["_fifu_image_url"] = image_url
            meta["_fifu_image_alt"] = wp_title

        # Subtitle — tie_post_sub_title is the exact meta key used by TieLabs themes (Jannah, Sahifa, etc.)
        if wp_subtitle:
            meta["tie_post_sub_title"] = wp_subtitle
            meta[subtitle_meta_key] = wp_subtitle
            for alias in ("_subtitle", "wps_subtitle", "post_subtitle"):
                meta[alias] = wp_subtitle

        # ── Taqyeem review meta ───────────────────────────────────────────
        if taqyeem_score is not None:
            is_anime = (c_type == "anime")
            criteria_name = "MAL Rating" if is_anime else "TMDB Ratings"
            predefined_val = "MAL" if is_anime else "IMDB|TMDB|MAL"
            score_str = str(taqyeem_score)

            meta["taq_review_position"]         = "top"
            meta["taq_review_style"]            = "points"
            meta["taq_review_title"]            = wp_title
            meta["taq_review_total"]            = "Total Ratings"
            meta["taq_review_summary"]          = synopsis
            meta["taq_review_score"]            = score_str
            meta["taq_review_structured_data"]  = "Default"

            # Predefined criteria set selection keys (all possible variations TieLabs uses)
            meta["select-pre-defined-criteria"] = predefined_val
            meta["taq_review_predefined"]       = predefined_val
            meta["taq_predefined_criteria"]     = predefined_val
            meta["taq_review_criteria_set"]     = predefined_val
            meta["taq_review_item"]             = predefined_val

            # Criteria lists: PHP serialized string as required by WP REST API schema
            criteria_list = [{"name": criteria_name, "score": score_str}]
            meta["taq_review_criteria"]         = _php_serialize_taqyeem(criteria_list)
            meta["taqyeem_review"]              = _php_serialize_taqyeem(criteria_list)

            # Flat keys fallback
            meta["taq_criteria_name_1"]         = criteria_name
            meta["taq_criteria_score_1"]        = score_str

        # ── Payload ───────────────────────────────────────────────────────
        payload: dict = {
            "title":   wp_title,
            "content": content,
            "status":  "draft",
            "meta":    meta,
        }
        if category_ids:
            payload["categories"] = category_ids

        try:
            resp = self.session.post(
                f"{self.base}/posts",
                json=payload,
                timeout=30,
            )
            if resp.status_code in (200, 201):
                post_id = resp.json().get("id")
                edit_url = f"{self.base.replace('/wp-json/wp/v2', '')}/wp-admin/post.php?post={post_id}&action=edit"
                logger.info("Created draft: %s (ID %s)", wp_title, post_id)

                # Set FIFU image URL directly via XML-RPC custom fields to bypass REST API sanitization
                if image_url and post_id:
                    self.set_fifu_image_url(post_id, image_url, wp_title)

                if self._existing_titles_cache is not None:
                    try:
                        self._existing_titles_cache.add(_normalize_title_for_matching(wp_title))
                    except Exception:
                        pass

                return True, edit_url, f"Draft created: {wp_title}", post_id
            else:
                err_text = resp.text
                try:
                    err = resp.json().get("message", err_text[:200])
                except Exception:
                    err = err_text[:200]
                logger.error("Failed to create post '%s': %s", wp_title, err)
                return False, "", f"API error ({resp.status_code}): {err}", None
        except Exception as exc:
            logger.error("Exception creating post '%s': %s", wp_title, exc)
            return False, "", str(exc), None

    def update_post_content(
        self,
        post_id: int,
        new_content: str,
        new_title: str = "",
        new_category_ids: Optional[list[int]] = None,
        update_date: bool = True,
        orig_date: str = "",
        orig_date_gmt: str = "",
    ) -> bool:
        """
        Update the content, title, categories, and published timestamp of an existing WordPress post.
        Setting update_date=True updates post date & date_gmt to the current time so the post bumps up on WordPress.
        When update_date=False and orig_date is provided, explicitly passes original date to prevent WordPress from bumping timestamps.
        """
        import datetime

        try:
            payload: dict = {"content": new_content}
            if new_title:
                payload["title"] = new_title
            if new_category_ids is not None:
                payload["categories"] = new_category_ids

            if update_date:
                now_dt = datetime.datetime.now(datetime.timezone.utc)
                payload["date_gmt"] = now_dt.strftime("%Y-%m-%dT%H:%M:%S")
                payload["date"] = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            elif orig_date:
                payload["date"] = orig_date
                if orig_date_gmt:
                    payload["date_gmt"] = orig_date_gmt

            resp = self.session.post(
                f"{self.base}/posts/{post_id}",
                json=payload,
                timeout=30,
            )
            if resp.status_code in (200, 201):
                logger.info("Updated post ID %s (title=%s, timestamp_updated=%s)", post_id, new_title or "unchanged", update_date)
                return True
            else:
                logger.error("Failed to update post %s: %s", post_id, resp.text[:200])
                return False
        except Exception as exc:
            logger.error("Exception updating post %s: %s", post_id, exc)
            return False

    def patch_post_with_streams(
        self,
        post_id: int,
        toggle_html: str,
        is_airing: Optional[bool] = None,
        latest_episode: int = 1,
        airing_title_suffix: str = "",
        update_date: bool = False,
        category_ids: Optional[list] = None,
        custom_title: Optional[str] = None,
    ) -> bool:
        """
        Fetch post by ID, insert or replace the episode toggle HTML, update title/category if needed,
        and update content on WordPress.
        When update_date=False (default), original post date & time are preserved.

        Optional kwargs:
          category_ids  – if provided, override the computed category list entirely.
          custom_title  – if provided, override the computed title entirely.
          toggle_html   – if None, leave post content untouched (only title/cats updated).
        """
        try:
            resp = self.session.get(f"{self.base}/posts/{post_id}", params={"context": "edit"}, timeout=15)
            if resp.status_code != 200:
                logger.error("patch_post_with_streams: failed to fetch post ID %s", post_id)
                return False

            post_data = resp.json()
            raw_title = post_data.get("title", {}).get("raw", "") or post_data.get("title", {}).get("rendered", "")
            raw_content = post_data.get("content", {}).get("raw", "") or post_data.get("content", {}).get("rendered", "")
            categories = post_data.get("categories", [])

            def _replace_episode_html_block(content: str, replacement: str) -> str:
                """
                Find the outer <div class="toggle..."> wrapping the Episode List h3,
                then replace the entire block. Uses a stack-based approach to properly
                track nested divs so we don't accidentally eat the Info section.
                """
                # Find the h3 that contains "Episode List"
                ep_h3 = re.search(r'<h3[^>]*>\s*Episode List', content, re.IGNORECASE)
                if not ep_h3:
                    return content  # No HTML episode list found

                # Walk backwards from ep_h3 to find the opening <div class="toggle...">
                before = content[:ep_h3.start()]
                # Find the last <div class="toggle that opens before our h3
                div_opens = list(re.finditer(r'<div\s+class=["\']toggle[^"\']*["\']', before, re.IGNORECASE))
                if not div_opens:
                    return content

                block_start = div_opens[-1].start()

                # Now walk forward from block_start counting <div> / </div> to find the matching close
                pos = block_start
                depth = 0
                block_end = -1
                while pos < len(content):
                    open_m = re.search(r'<div[\s>]', content[pos:], re.IGNORECASE)
                    close_m = re.search(r'</div>', content[pos:], re.IGNORECASE)
                    if open_m and (close_m is None or open_m.start() < close_m.start()):
                        depth += 1
                        pos += open_m.start() + 1
                    elif close_m:
                        depth -= 1
                        pos += close_m.start() + len('</div>')
                        if depth == 0:
                            block_end = pos
                            break
                    else:
                        break

                if block_end == -1:
                    return content  # Couldn't find matching close

                return content[:block_start] + replacement + content[block_end:]

            # Pattern replacement for toggle - try shortcode first, then HTML div
            sc_pattern = r'\[toggle title="Episode List"[^\]]*\].*?\[/toggle\]'

            import re
            if not toggle_html:
                # No toggle HTML provided (no streams yet) — keep content as-is
                new_content = raw_content
            elif re.search(sc_pattern, raw_content, flags=re.DOTALL | re.IGNORECASE):
                # Post uses shortcode format - simple replace
                new_content = re.sub(sc_pattern, toggle_html, raw_content, flags=re.DOTALL | re.IGNORECASE)
            else:
                # Try HTML div-based episode list (robust nested-div aware replacement)
                replaced = _replace_episode_html_block(raw_content, toggle_html)
                if replaced != raw_content:
                    new_content = replaced
                else:
                    # Fallback: append at end
                    new_content = raw_content + "\n\n" + toggle_html

            # Title update
            new_title = ""
            if airing_title_suffix:
                # Strip existing "- Episode X Added!" suffix first
                clean_title = re.sub(r'\s*-\s*(Episode\s*\d+\s*Added!|All\s*Episodes?\s*Added!)', '', raw_title, flags=re.IGNORECASE).strip()
                # Extract and strip the "(Season X)" part so we can reattach it at the END
                season_match = re.search(r'\s*\(Season\s*(\d+)\)', clean_title, re.IGNORECASE)
                if season_match:
                    season_suffix = season_match.group(0).strip()  # e.g. "(Season 1)"
                    base_name = clean_title[:season_match.start()].strip()
                    # Format: "Show Name (Season 1) - Episode X Added!"
                    new_title = f"{base_name} {season_suffix} - {airing_title_suffix}"
                else:
                    new_title = f"{clean_title} - {airing_title_suffix}"

            # Category update (swap Airing / Completed while preserving rest)
            all_cats_map = self.get_all_categories()
            cat_id_to_name = {v: k.lower() for k, v in all_cats_map.items()}
            airing_status_ids = {cid for cid, nm in cat_id_to_name.items() if any(k in nm for k in ("airing", "ongoing"))}
            completed_status_ids = {cid for cid, nm in cat_id_to_name.items() if "completed" in nm}

            existing_cat_ids = set(categories)
            if is_airing is None:
                is_airing = not any(cid in completed_status_ids for cid in categories)

            if is_airing:
                existing_cat_ids -= completed_status_ids
                if airing_status_ids:
                    existing_cat_ids |= airing_status_ids
            else:
                existing_cat_ids -= airing_status_ids
                if completed_status_ids:
                    existing_cat_ids |= completed_status_ids

            new_category_ids = sorted(existing_cat_ids)

            # Allow callers to fully override the computed categories / title
            if category_ids is not None:
                new_category_ids = sorted(category_ids)
            if custom_title is not None:
                new_title = custom_title

            return self.update_post_content(
                post_id,
                new_content,
                new_title=new_title,
                new_category_ids=new_category_ids,
                update_date=update_date,
                orig_date=post_data.get("date", ""),
                orig_date_gmt=post_data.get("date_gmt", ""),
            )
        except Exception as exc:
            logger.error("Exception in patch_post_with_streams for post %s: %s", post_id, exc)
            return False




# ── Helpers ────────────────────────────────────────────────────────────────

def _extract_lang_tags(language_field: str, raw_title: str) -> str:
    """
    Build a clean language tag string like 'Hindi-English-Japanese'
    from the language field or raw title.
    """
    langs = []
    seen: set[str] = set()
    combined = (language_field + " " + raw_title).lower()
    tag_map = {
        "hindi": "Hindi",
        "tamil": "Tamil",
        "telugu": "Telugu",
        "mal": "Mal",
        "malayalam": "Mal",
        "english": "English",
        "eng": "English",
        "japanese": "Japanese",
        "jap": "Japanese",
    }
    for key, label in tag_map.items():
        if key in combined and label not in seen:
            langs.append(label)
            seen.add(label)

    return "-".join(langs)




def _normalize_title_for_matching(title_str: str) -> tuple[str, int, bool]:
    """
    Normalize any post title into (clean_core_name, season_number, is_movie).
    Handles:
    - HTML entities: &#8211;, &amp;, &ndash;, etc.
    - Rating/age prefixes: (18+), [18+], 18+
    - Episode update suffixes: – Episode 7 Added!, - Ep. 5 Added, Added!
    - Season numbers: (Season 1), Season 1, S1
    - Resolution/language tags: 1080p, Multi Audio, Dual Audio, WEB-DL, etc.
    """
    import html

    title = html.unescape(title_str).lower().strip()

    # Normalize Unicode smart quotes/apostrophes to plain ASCII equivalents
    title = title.replace('\u2019', "'").replace('\u2018', "'")
    title = title.replace('\u201c', '"').replace('\u201d', '"')
    title = title.replace('\u2013', '-').replace('\u2014', '-')

    # Detect movie from raw title BEFORE stripping
    is_movie = bool(
        re.search(r"\bmovie\b|\bfilm\b", title, re.IGNORECASE)
    )

    # Extract season number (handles Season 2, Seasons 1 - 21, Season 2-3, S1, etc.)
    season = 1
    m = re.search(r"\bseasons?\s+(\d+)(?:\s*[-–—to]+\s*\d+)?\b|\bS(\d{1,2})\b|[\(\[\{]seasons?\s+(\d+)(?:\s*[-–—to]+\s*\d+)?[\)\]\}]", title, re.IGNORECASE)
    if m:
        season = int(m.group(1) or m.group(2) or m.group(3))

    # Strip rating prefixes like (18+), [18+], 18+
    title = re.sub(r"[\(\[\{]?\s*18\+\s*[\)\]\}]?", "", title, flags=re.IGNORECASE)

    # Strip episode update suffixes like – Episode 7 Added!, - Ep. 5 Added, Added!, – All Episodes Added!
    title = re.sub(r"[-–]\s*all\s+episodes?.*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"[-–]\s*(?:episode|ep\.?)\s*\d+.*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s*(?:ep\.?|episode)\s*\d+.*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+added!?", "", title, flags=re.IGNORECASE)

    # Strip season indicators (including multi-season ranges like Season 2-3, Seasons 1 - 21, Season 2 to 3)
    title = re.sub(r"[\(\[\{]\s*seasons?\s*[\d\s\-–—to]+[\)\]\}]", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\bseasons?\s*[\d\s\-–—to]+\b", "", title, flags=re.IGNORECASE)
    # Strip multi-season range tags like S01-21, S1-S21, S01–S13 BEFORE stripping standalone S\d
    title = re.sub(r"\bS\d{1,2}\s*[-–—to]+\s*S?\d{1,2}\b", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\bS\d{1,2}(?:\s*[-–—to]+\s*S?\d{1,2})?\b", "", title, flags=re.IGNORECASE)

    # Strip curly-brace language/audio tags like {English-Japanese}, {Hindi-English}
    title = re.sub(r"\{[^}]*\}", "", title)

    # Strip resolution / language / quality tags
    title = re.sub(r"\b(?:multi|dual)\s+audio\b.*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\b\d{3,4}p\b.*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\[.*?\]", "", title)
    title = re.sub(r"\(.*?\)", "", title)

    # Strip generic descriptor suffixes like "Anime Series", "Web Series" that BollyFlix appends
    title = re.sub(r"\b(?:anime|web|tv|mini|limited)\s+series\b.*$", "", title, flags=re.IGNORECASE)

    # Clean up non-alphanumeric noise at edges
    title = re.sub(r"^[^\w]+|[^\w]+$", "", title).strip()
    title = re.sub(r"\s{2,}", " ", title)

    return title, season, is_movie

