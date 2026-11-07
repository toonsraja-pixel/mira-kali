"""
auto_poster.py - Main GUI application for the WordPress Auto-Poster tool.
Built with CustomTkinter for a modern dark-themed interface.

Run with:  python auto_poster.py
"""

import json
import logging
import os
import queue
import re
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Tuple

# Try to import GUI components. If we are running on a headless server (like Render),
# mock them to prevent import-time exceptions.
try:
    import tkinter as tk
    from tkinter import messagebox, ttk, simpledialog
    import customtkinter as ctk
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    HAS_GUI = True
except Exception:
    HAS_GUI = False
    # Mock CTk class to prevent subclassing error when headless
    class MockCTk:
        def __init__(self, *args, **kwargs): pass
        def mainloop(self): pass
    ctk = type("MockCustomTkinter", (object,), {
        "CTk": MockCTk,
        "CTkFrame": object,
        "CTkLabel": object,
        "CTkEntry": object,
        "CTkButton": object,
        "CTkOptionMenu": object,
        "CTkCheckBox": object,
        "CTkTextbox": object,
        "CTkScrollableFrame": object,
        "set_appearance_mode": lambda mode: None,
        "set_default_color_theme": lambda theme: None,
    })
    # Mock tk variables to prevent NameError
    tk = type("MockTkinter", (object,), {
        "StringVar": object,
        "BooleanVar": object,
        "DoubleVar": object,
        "IntVar": object,
    })
    messagebox = ttk = simpledialog = object

from rating_api import fetch_rating, get_show_metadata
from scraper import iter_all_posts, get_total_pages, clean_title
from wp_api import WordPressAPI
from stream_extractor import (
    extract_ruby_url_from_toonstream,
    extract_toonstream_all_servers,
    resolve_multimovies_episode,
    resolve_satoru_episode,
    extract_downloads_from_url,
)

SETTINGS_FILE = Path(__file__).parent / "settings.json"
APP_VERSION = "1.0.0"

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
#  Settings helpers
# ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    "wp_url": "",
    "wp_username": "",
    "wp_app_password": "",
    "tmdb_api_key": "",
    "default_taqyeem_score": 70,
    "test_mode": False,
    "test_pages": 1,
    "delay_between_posts": 0.5,
    "settings_password": "",
    # Source domains — comma-separated, first entry is primary
    "toonstream_domains": "toon-stream.site",
    "multimovies_domains": "m1.multimovies.courses, multimovies.garden, multimovies.casa",
    "bollyflix_url": "https://bollyflix.af",
    "tw4all_cookie": "",
    "cf_clearance": "",
    "cf_user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "flaresolverr_url": "http://localhost:8191",
    "use_watchanimeworld": False,
    "worker_base": "https://new-frosty.toonsraja-df3.workers.dev/",
    "server_ug_source": "toonstream",
    "animedekho_url": "https://animedekho.tv",
    "colab_url": "https://flinch-unlighted-viewpoint.ngrok-free.dev/",
}


def get_installed_browser_cf_cookie() -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Scan Chrome, Edge, and Brave cookie databases for an active cf_clearance cookie."""
    import os, json, sqlite3, shutil, base64, ctypes
    from ctypes import wintypes
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        return None, None, None

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_char))]

    def dpapi_decrypt(b):
        in_blob = DATA_BLOB(len(b), ctypes.cast(ctypes.create_string_buffer(b), ctypes.POINTER(ctypes.c_char)))
        out_blob = DATA_BLOB()
        if ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)):
            res = ctypes.string_at(out_blob.pbData, out_blob.cbData)
            ctypes.windll.kernel32.LocalFree(out_blob.pbData)
            return res
        return b''

    def decrypt_cookie_v10(master_key, encrypted_value):
        try:
            nonce = encrypted_value[3:15]
            ciphertext = encrypted_value[15:]
            aesgcm = AESGCM(master_key)
            decrypted = aesgcm.decrypt(nonce, ciphertext, None)
            return decrypted.decode('utf-8', errors='ignore')
        except Exception:
            return ''

    appdata = os.environ.get('LOCALAPPDATA', '')
    browsers = [
        ('Edge', os.path.join(appdata, r'Microsoft\Edge\User Data')),
        ('Chrome', os.path.join(appdata, r'Google\Chrome\User Data')),
        ('Brave', os.path.join(appdata, r'BraveSoftware\Brave-Browser\User Data')),
    ]
    for b_name, b_dir in browsers:
        ls_path = os.path.join(b_dir, 'Local State')
        if not os.path.exists(ls_path):
            continue
        try:
            with open(ls_path, 'r', encoding='utf-8') as f:
                ls = json.load(f)
            master_key = dpapi_decrypt(base64.b64decode(ls['os_crypt']['encrypted_key'])[5:])
            import glob
            cookie_files = glob.glob(os.path.join(b_dir, '*', 'Network', 'Cookies')) + glob.glob(os.path.join(b_dir, 'Default', 'Network', 'Cookies'))
            for c_file in set(cookie_files):
                tmp_db = 'tmp_auto_detect.sqlite'
                shutil.copy2(c_file, tmp_db)
                conn = sqlite3.connect(tmp_db)
                cur = conn.cursor()
                cur.execute('SELECT host_key, name, value, encrypted_value FROM cookies')
                rows = cur.fetchall()
                conn.close()
                if os.path.exists(tmp_db): os.remove(tmp_db)
                for host, name, val, enc in rows:
                    if name == 'cf_clearance' and ('toon' in host or 'stream' in host or 'gplinks' in host or 'toonworld' in host):
                        clearance = val if val else decrypt_cookie_v10(master_key, enc)
                        if clearance:
                            return clearance, host, b_name
        except Exception:
            pass
    return None, None, None


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            return {**DEFAULT_SETTINGS, **saved}
        except Exception:
            pass
    return dict(DEFAULT_SETTINGS)


def save_settings(settings: dict):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


def check_and_update_airing_posts(
    api, settings: dict, log_fn=None,
    filter_show: tuple | None = None,
    downloads_map: dict | None = None,
    skip_fn=None,
) -> tuple[int, int]:
    """
    Scan existing airing/ongoing posts on WordPress.
    Checks for:
    1. Newly released episodes on Toonstream/Multimovies.
    2. Missing stream servers (e.g., Server UG2 added later on Multimovies for an existing episode).
    Updates title (- Episode X Added!), moves New! badge, and updates categories (Airing vs Completed).
    Returns (total_airing_checked, total_posts_updated).

    filter_show: optional (show_name, season) tuple - if provided, only that specific post is processed.
    """
    from rating_api import get_show_metadata
    from stream_extractor import (
        fetch_all_episodes_data,
        generate_episode_toggle_html,
        count_toonstream_episodes,
        count_multimovies_episodes,
        extract_ruby_url_from_toonstream,
        extract_toonstream_all_servers,
        resolve_multimovies_episode,
        resolve_satoru_episode,
        _get_or_fetch_toonstream_episode_map,
    )
    from wp_api import parse_existing_post_streams

    def log(msg, style="info"):
        if log_fn:
            log_fn(msg, style)
        else:
            logger.info(msg)

    processed_new_shows = set()
    newly_added_anime_cnt = 0

    # ── Step 1: Scan ToonWorld4All (pages 1-2) for newly added anime ────────
    if not filter_show:
        log("🌐 Checking ToonWorld4All (pages 1-2) for newly added anime…", "info")
        try:
            from scraper import iter_post_links, scrape_post, clean_title
            import time as _time

            # iter_post_links automatically decomposes .herald-fa-list and skips herald-fa elements
            tw_links = []
            for p_num in (1, 2):
                if skip_fn and skip_fn():
                    break
                p_items = iter_post_links(p_num)
                tw_links.extend(p_items)

            seen_tw_urls = set()
            unique_tw_links = []
            for item in tw_links:
                u = item.get("url", "")
                if u and u not in seen_tw_urls:
                    seen_tw_urls.add(u)
                    unique_tw_links.append(item)

            tmdb_key = settings.get("tmdb_api_key", "")
            default_score = int(settings.get("default_taqyeem_score", 70))
            subtitle_key = settings.get("subtitle_meta_key", "tie_post_sub_title")
            delay = float(settings.get("delay_between_posts", 0.5))

            seen_new_shows = set()  # in-loop dedup guard (catches same-show across pages)

            for item in unique_tw_links:
                if skip_fn and skip_fn():
                    log("   ⏭️ Skipping remaining ToonWorld4All anime checks (user requested skip)", "warn")
                    break

                raw_title = item.get("title", "")
                post_url = item.get("url", "")
                show_name, season, _ = clean_title(raw_title)
                if not show_name:
                    continue

                # In-loop dedup: skip if already processed in this run
                show_key = (show_name.lower(), season)
                if show_key in seen_new_shows:
                    continue

                # Check if this show already exists on WordPress
                if api.post_exists(show_name, season, is_movie=False):
                    continue

                # Scrape post details to verify if it is an anime series
                post_data = scrape_post(post_url)
                if not post_data:
                    continue

                # Skip movies — only anime series
                if post_data.get("is_movie", False):
                    continue

                # Anime check: content_type == "anime" or "anime" in categories
                c_type = post_data.get("content_type", "")
                cats = [str(c).lower() for c in post_data.get("categories", [])]
                is_anime = (c_type == "anime") or ("anime" in cats)
                if not is_anime:
                    continue

                log(f"   ✨ Newly added anime detected on ToonWorld4All: {show_name} (Season {season})", "info")

                meta = get_show_metadata(show_name, "anime", tmdb_key)
                score = meta.get("score") if meta.get("score") is not None else default_score
                expected_eps = meta.get("expected_episodes")
                anilist_status = meta.get("status")

                success, edit_url, message, new_post_id = api.create_draft_post(
                    post_data,
                    taqyeem_score=score,
                    tmdb_api_key=tmdb_key,
                    subtitle_meta_key=subtitle_key,
                )
                if not success or not new_post_id:
                    log(f"      Draft creation failed: {message}", "warn")
                    continue

                log(f"      Draft created on WordPress [ID {new_post_id}]", "success")
                seen_new_shows.add(show_key)  # ← mark as created to prevent duplicate drafts

                if settings.get("extract_episode_streams", True):
                    try:
                        ts_ep_map = _get_or_fetch_toonstream_episode_map(show_name, season)
                        first_ep = min(ts_ep_map.keys()) if ts_ep_map else 1

                        # Fetch all three servers independently — none blocks the others
                        ep1_ruby, _ep1_mg_ts, ep1_pg = extract_toonstream_all_servers(show_name, season=season, episode=first_ep, is_movie=False)
                        ep1_multi = resolve_multimovies_episode(show_name, season=season, episode=first_ep, is_movie=False)
                        ep1_mg = _ep1_mg_ts or resolve_satoru_episode(show_name, season=season, episode=first_ep)

                        # Count available episodes from ALL sources, use the highest
                        total_eps_toon = count_toonstream_episodes(show_name, season)
                        total_eps_multi = count_multimovies_episodes(show_name, season=season)
                        total_eps = max(total_eps_toon, total_eps_multi, expected_eps or 0)

                        # If counts are unreliable (returned 0/1) but any stream for ep1 WAS found,
                        # probe generously — count functions can fail due to fuzzy search misses
                        ep1_found = bool(ep1_ruby or ep1_multi or ep1_mg or ep1_pg)
                        if ep1_found and total_eps <= first_ep:
                            total_eps = first_ep

                        episodes_data = []
                        if ep1_found:
                            episodes_data.append({"episode_num": first_ep, "ruby_url": ep1_ruby, "multi_url": ep1_multi, "mg_url": ep1_mg, "pg_url": ep1_pg})
                        if total_eps > first_ep:
                            remaining = fetch_all_episodes_data(show_name, season, total_eps, start_from=first_ep + 1)
                            episodes_data.extend(remaining)

                        active_eps = [e for e in episodes_data if e.get("ruby_url") or e.get("ag_url") or e.get("multi_url") or e.get("mg_url") or e.get("pg_url")]
                        if active_eps:
                            latest_ep_num = max((e.get("episode_num", 1) for e in active_eps), default=1)

                            # Airing logic for newly detected TW4All shows:
                            # presume airing unless ALL episodes are found AND anilist confirms finished
                            _anilist_fin = (anilist_status is not None and anilist_status.upper() in ("FINISHED", "CANCELLED"))
                            if anilist_status and anilist_status.upper() == "RELEASING":
                                is_airing = True
                            elif _anilist_fin and expected_eps and expected_eps > 0 and latest_ep_num >= expected_eps:
                                # All expected eps present + anilist confirmed finished
                                is_airing = False
                            else:
                                # Default: newly posted on TW4All → assume still airing
                                is_airing = True

                            toggle_html = generate_episode_toggle_html(
                                active_eps,
                                series_name=show_name,
                                season_num=season,
                            )
                            airing_title_suffix = f"Episode {latest_ep_num} Added!" if is_airing else ""
                            patch_ok = api.patch_post_with_streams(
                                new_post_id,
                                toggle_html=toggle_html,
                                is_airing=is_airing,
                                latest_episode=latest_ep_num,
                                airing_title_suffix=airing_title_suffix,
                            )
                            if patch_ok:
                                log(f"      Streams patched - {len(active_eps)} ep(s) active | Airing: {is_airing}", "success")
                                newly_added_anime_cnt += 1
                                processed_new_shows.add((show_name.lower(), season))
                        else:
                            # No streams available yet — mark post as airing so future
                            # "Update Airing Posts" runs will add streams when they appear
                            log(f"      ⚠️ No streams found yet for {show_name} S{season} — keeping as airing draft for future update", "warn")
                            patch_ok = api.patch_post_with_streams(
                                new_post_id,
                                toggle_html="",
                                is_airing=True,
                                latest_episode=0,
                                airing_title_suffix="",
                            )
                            newly_added_anime_cnt += 1
                            processed_new_shows.add((show_name.lower(), season))
                    except Exception as stream_err:
                        log(f"      Stream extraction error: {stream_err}", "warn")
                else:
                    newly_added_anime_cnt += 1
                    processed_new_shows.add((show_name.lower(), season))

                _time.sleep(delay)
        except Exception as tw_err:
            logger.exception("Error checking ToonWorld4All newly added anime")
            log(f"   ⚠️ Could not check ToonWorld4All newly added anime: {tw_err}", "warn")

    airing_posts = api.get_airing_posts()
    if not airing_posts:
        if newly_added_anime_cnt > 0:
            log(f"   Airing update done — {newly_added_anime_cnt} newly added anime post(s) created.", "success")
            return newly_added_anime_cnt, newly_added_anime_cnt
        log("   No airing/ongoing posts found to check.", "muted")
        return 0, 0

    # Apply filter if provided
    if filter_show:
        fs_name, fs_season = filter_show
        airing_posts = [
            p for p in airing_posts
            if p.get("show_name", "").lower() == fs_name.lower()
            and p.get("season", 1) == fs_season
        ]
        if not airing_posts:
            log(f"   Post for '{fs_name}' S{fs_season} not found in airing list.", "muted")
            return 0, 0

    # Apply permanent skip list from settings
    raw_skip = settings.get("airing_skip_list", [])
    if raw_skip:
        # Normalize helper: strips colons, dashes, special quotes, extra spaces
        def _norm_skip_str(s: str) -> str:
            clean = re.sub(r'[:–—\-\|\!\?\.\'\"]', ' ', s)
            return re.sub(r'\s+', ' ', clean).strip().lower()

        skip_entries = [_norm_skip_str(s) for s in raw_skip if s.strip()]
        if skip_entries:
            skipped_before = []
            kept = []
            for p in airing_posts:
                name_clean = _norm_skip_str(p.get("show_name", ""))
                season_num = p.get("season", 1)
                season_tag = f"s{season_num}"
                season_long = f"season {season_num}"

                # Match against "Show Name", "Show Name S1", "Show Name Season 1"
                is_match = False
                for entry in skip_entries:
                    if entry == name_clean:
                        is_match = True
                        break
                    if entry.endswith(season_tag):
                        base_entry = entry[:-len(season_tag)].strip()
                        if base_entry == name_clean:
                            is_match = True
                            break
                    if entry.endswith(season_long):
                        base_entry = entry[:-len(season_long)].strip()
                        if base_entry == name_clean:
                            is_match = True
                            break

                if is_match:
                    skipped_before.append(f"{p.get('show_name', '?')} (S{season_num})")
                else:
                    kept.append(p)

            if skipped_before:
                log(f"   ⏭️ Skip list: skipping {len(skipped_before)} post(s): {', '.join(skipped_before)}", "warn")
            airing_posts = kept

    log(f"-  Monitoring {len(airing_posts)} airing post(s) on WordPress for updates¦", "info")
    updated_cnt = 0

    for idx, post in enumerate(airing_posts, 1):
        # --- Skip check (user pressed Skip button) ---
        if skip_fn and skip_fn():
            log(f"   © Skipped: {post.get('show_name', '?')} (user requested skip)", "warn")
            # Reset the skip signal for the next post
            if hasattr(skip_fn, '_reset'):
                skip_fn._reset()
            continue

        post_id    = post["id"]
        show_name  = post["show_name"]
        season     = post["season"]
        if (show_name.lower(), season) in processed_new_shows:
            log(f"   [{idx}/{len(airing_posts)}] {show_name} (S{season}) already newly added and up-to-date.", "muted")
            continue
        current_ep = post["current_ep"]
        # content = rendered HTML (for parsing existing stream URLs from dropdown divs)
        content         = post["content"]
        # content_raw = raw shortcode text (for [toggle] pattern replacement)
        content_raw     = post.get("content_raw", "")
        categories      = post.get("categories", [])

        # Detect content type and completed status from category names
        all_cats_map   = api.get_all_categories()
        cat_id_to_name = {v: k.lower() for k, v in all_cats_map.items()}
        post_cat_names = " ".join(cat_id_to_name.get(cid, "") for cid in categories).lower()
        post_has_completed = any("completed" in cat_id_to_name.get(cid, "") for cid in categories)

        if "anime" in post_cat_names:
            content_type = "anime"
        elif "movie" in post_cat_names:
            content_type = "movie"
        else:
            content_type = "show"

        log(f"   [{idx}/{len(airing_posts)}] Checking {show_name} (S{season}) [{content_type}]¦", "muted")

        # ------ Parse what's already in the post (from rendered HTML) ------------------------------
        existing_streams = parse_existing_post_streams(content)
        max_ex = max(existing_streams.keys()) if existing_streams else 0

        # Query metadata passing exact season so Season 2/3/4 get exact episode counts
        meta           = get_show_metadata(show_name, content_type, season=season)
        expected_eps   = meta.get("expected_episodes")
        anilist_status = meta.get("status")

        # ------ How many episodes to check: Toonstream + Multimovies ---------------------
        ts_ep_map = _get_or_fetch_toonstream_episode_map(show_name, season)
        ts_count = count_toonstream_episodes(show_name, season)
        mm_count = count_multimovies_episodes(show_name, season=season)
        avail_count = max(ts_count, mm_count)

        # Floor avail_count so we never miss a recently-aired episode that the sources
        # haven't indexed yet, or that AniList says exists but wasn't returned this run.
        #   1. max_ex + 1 : always check at least one ep beyond the post's current highest ep
        #      (catches episodes added to Toonstream between the last run and now)
        #   2. expected_eps (AniList total) ONLY when expected_eps is close to what we found
        #      (e.g. expected_eps <= avail_count + 3, like Black Torch where avail=9, exp=12).
        #      CRITICAL: For absolute-numbered long-running shows (like Naruto Shippuden S18
        #      where AniList reports total series eps = 500, but Season 18 only has eps ~373-393),
        #      we must NEVER bump avail_count to 500, or it will scan non-existent eps up to 500!
        if expected_eps and expected_eps > 0:
            if avail_count > 0 and expected_eps <= avail_count + 3:
                if expected_eps > avail_count:
                    avail_count = expected_eps
            elif avail_count == 0 and expected_eps <= 26:
                # Post is brand new and sources haven't indexed yet
                avail_count = expected_eps

        # Look at most 1 episode beyond the post's current highest episode
        if max_ex > 0 and (max_ex + 1) > avail_count:
            avail_count = max_ex + 1

        min_avail_ep = min(ts_ep_map.keys()) if ts_ep_map else (current_ep if current_ep > 0 else 1)
        if existing_streams:
            min_ex = min(existing_streams.keys())
        else:
            min_ex = min_avail_ep

        if avail_count > 0:
            total_search_eps = avail_count
            num_actual = len(ts_ep_map) if ts_ep_map else max(ts_count, mm_count)
            log(f"      Available: {num_actual} ep(s) (Toonstream: {ts_count}, Multimovies: {mm_count}) ➔ checking ep {min_ex}–{total_search_eps}.", "muted")
        else:
            total_search_eps = max(max_ex, current_ep, expected_eps if expected_eps else 1)
            log(f"      Toonstream/Multimovies 0 eps found ➔ checking fallback range ep {min_ex}–{total_search_eps}.", "muted")

        # For completed posts: skip only if ALL recent episodes already have all servers.
        # Don't hard-skip here — let the episodes_to_fetch loop below check the last few eps
        # for missing secondary servers (UG2/MG often lag behind Toonstream by days/weeks).
        # The hard-skip at the top is replaced by a softer check in the loop below.
        if post_has_completed and avail_count > 0 and avail_count <= max_ex and not downloads_map:
            # Do a quick scan of the last COMPLETED_BACKFILL_WINDOW eps for missing servers
            COMPLETED_BACKFILL_WINDOW = 5
            backfill_start = max(1, max_ex - COMPLETED_BACKFILL_WINDOW + 1)
            needs_backfill = False
            for _ep in range(backfill_start, max_ex + 1):
                _ex = existing_streams.get(_ep)
                if not _ex:
                    needs_backfill = True
                    break
                # Only consider missing if the source site actually has that episode
                if not _ex.get("has_multi") and mm_count >= _ep:
                    needs_backfill = True
                    break
                if not _ex.get("has_ruby") and (ts_count >= _ep or (ts_ep_map and _ep in ts_ep_map)):
                    needs_backfill = True
                    break
            if not needs_backfill:
                log(f"      ✔ {show_name} (S{season}) is marked Completed ({max_ex}/{avail_count} eps) — all available servers present.", "muted")
                continue
            log(f"      ↩ {show_name} (S{season}) is Completed but last {COMPLETED_BACKFILL_WINDOW} eps have missing servers — backfilling.", "muted")

        # --- Skip check after ts_count (may have taken a long time) ---
        if skip_fn and skip_fn():
            log(f"   ⏩ Skipped: {show_name} S{season} (user requested skip after ts_count)", "warn")
            continue

        # ------ Figure out which episodes actually need fetching ------------------------------------------------
        # 1. Brand new episode: always fetch all servers
        # 2. Completed posts (backfill mode): check last COMPLETED_BACKFILL_WINDOW eps for missing secondary servers
        # 3. Completed posts (all servers present): already continued above
        # 4. Airing posts: only check missing servers on recent episodes (latest 2) to avoid slow loops
        COMPLETED_BACKFILL_WINDOW = 5
        completed_backfill_start = max(1, max_ex - COMPLETED_BACKFILL_WINDOW + 1) if post_has_completed else 0

        # Smart range start: for shows where episodes use absolute numbering across seasons
        # (e.g. Naruto Shippuden S17 starts at ep 362), we must not try to fetch eps 1–361
        # which don't exist in the post and would be wrongly treated as "new" episodes.
        # Rule: start from min_ex (lowest ep already in the post), or min_avail_ep if post is empty.
        # For airing posts we further clamp to (max_ex - COMPLETED_BACKFILL_WINDOW) so we
        # don't re-scan the entire back-catalogue on every run.
        if existing_streams:
            if post_has_completed:
                range_start = completed_backfill_start
            else:
                # Airing: check from 5 eps before current max (for missing-server backfill)
                # AND from min_ex so we don't generate phantom eps below the season's start.
                range_start = max(min_ex, max_ex - COMPLETED_BACKFILL_WINDOW + 1)
        else:
            # Empty post: start from the lowest known available episode (or current_ep), not blindly 1
            range_start = min_avail_ep

        # Never search below min episode present in Toonstream's episode map for this season
        if ts_ep_map and min(ts_ep_map.keys()) > 1:
            range_start = max(range_start, min(ts_ep_map.keys()))

        episodes_to_fetch = []
        for ep_num in range(range_start, total_search_eps + 1):
            ex = existing_streams.get(ep_num)
            if ex is None:
                # If we know Toonstream's exact episode map, and Multimovies has 0 eps:
                # skip ep_nums that are not in ts_ep_map to avoid scanning phantom gaps
                if ts_ep_map and mm_count == 0 and ep_num not in ts_ep_map:
                    continue
                # Brand new episode — fetch all servers
                episodes_to_fetch.append((ep_num, "both"))
            elif post_has_completed:
                # Completed post backfill: check the last COMPLETED_BACKFILL_WINDOW episodes for missing servers
                if ep_num < completed_backfill_start:
                    continue  # skip older eps — secondary servers won't appear there now
                _needs = set()
                if not ex.get("has_ruby"):
                    _needs.add("ruby")
                if not ex.get("has_multi"):
                    _needs.add("multi")
                if not ex.get("has_pg"):
                    _needs.add("pg")
                if not ex.get("has_mg"):
                    _needs.add("mg")
                if _needs:
                    episodes_to_fetch.append((ep_num, _needs))
            elif ep_num >= max(1, total_search_eps - 4) or len(existing_streams) == 0:
                # For active airing shows, check missing servers on the 5 most recent episodes
                _needs = set()
                if not ex.get("has_ruby"):
                    _needs.add("ruby")
                if not ex.get("has_multi"):
                    _needs.add("multi")
                if not ex.get("has_pg"):
                    _needs.add("pg")
                if not ex.get("has_mg"):
                    _needs.add("mg")
                if _needs:
                    episodes_to_fetch.append((ep_num, _needs))
            # else: older existing episodes on an airing post already have working streams
        
        if not episodes_to_fetch:
            if not (downloads_map):
                # Even when nothing to fetch, check if the post can be auto-completed.
                _all_cats_etf    = api.get_all_categories()
                _cat_id_nm_etf   = {v: k.lower() for k, v in _all_cats_etf.items()}
                _post_comp_etf   = any("completed" in _cat_id_nm_etf.get(cid, "") for cid in categories)
                _meta_etf        = get_show_metadata(show_name, content_type, season=season)
                _alist_st_etf    = _meta_etf.get("status", "")
                _exp_eps_etf     = _meta_etf.get("expected_episodes") or expected_eps
                _alist_fin_etf   = bool(_alist_st_etf and _alist_st_etf.upper() in ("FINISHED", "CANCELLED"))
                _active_ep_etf   = max_ex

                _should_complete_etf = (
                    not _post_comp_etf
                    and _alist_fin_etf
                    and _exp_eps_etf
                    and _exp_eps_etf > 0
                    and _active_ep_etf >= _exp_eps_etf
                    and (avail_count >= _exp_eps_etf or _active_ep_etf >= avail_count)
                )
                if _should_complete_etf:
                    _all_cats_ec   = api.get_all_categories()
                    _cat_id_nm_ec  = {v: k.lower() for k, v in _all_cats_ec.items()}
                    _airing_ids_ec = {cid for cid, nm in _cat_id_nm_ec.items() if any(k in nm for k in ("airing", "ongoing"))}
                    _comp_ids_ec   = {cid for cid, nm in _cat_id_nm_ec.items() if "completed" in nm}
                    _new_cats_ec   = (set(categories) - _airing_ids_ec) | _comp_ids_ec
                    _new_title_ec  = f"{show_name} (Season {season})"
                    ok_ec = api.patch_post_with_streams(
                        post_id,
                        toggle_html=None,
                        is_airing=False,
                        latest_episode=_active_ep_etf,
                        airing_title_suffix="",
                        category_ids=sorted(_new_cats_ec),
                        custom_title=_new_title_ec,
                    )
                    if ok_ec:
                        log(f"   🎉 {show_name} (S{season}) auto-completed: AniList=FINISHED, {_active_ep_etf}/{_exp_eps_etf} eps.", "success")
                        updated_cnt += 1
                    else:
                        log(f"   ✔ All {total_search_eps} episode(s) up-to-date for {show_name} (S{season})", "muted")
                else:
                    log(f"   ✔ All {total_search_eps} episode(s) up-to-date for {show_name} (S{season})", "muted")
                continue
            else:
                # No new streams, but downloads were provided — still need to update the post
                log(f"   ✔ Streams up-to-date, but applying downloads for {show_name} (S{season})", "muted")

        fetch_new = [e for e in episodes_to_fetch if existing_streams.get(e[0]) is None]
        fetch_upd = [e for e in episodes_to_fetch if existing_streams.get(e[0]) is not None]
        if episodes_to_fetch:
            parts = []
            if fetch_new:
                parts.append(f"{len(fetch_new)} new ep(s)")
            if fetch_upd:
                parts.append(f"{len(fetch_upd)} ep(s) missing a server")
            log(f"      Need to fetch: {', '.join(parts)}", "muted")

        # ------ Fetch only what's needed ------------------------------------------------------------------------------------------------------------------------
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # --- Skip check before fetch (may have taken time building episodes_to_fetch list) ---
        if skip_fn and skip_fn():
            log(f"   ⏩ Skipped: {show_name} S{season} (user requested skip before fetch)", "warn")
            continue

        newly_fetched = {}

        def _fetch_ep_task(ep_info):
            ep_num, need = ep_info
            ruby_url = None
            multi_url = None
            mg_url = None
            pg_url = None

            # need can be "both", "ruby", "multi", "pg", "mg", or a set like {"multi", "mg"}
            _need_set = need if isinstance(need, set) else {need} if need != "both" else {"ruby", "multi", "pg", "mg"}
            _fetch_ug  = "ruby" in _need_set or "both" == need or "pg" in _need_set
            _fetch_mm  = "multi" in _need_set or "both" == need
            _fetch_mg  = "mg" in _need_set or "both" == need

            if _fetch_ug:
                logger.info("Extracting Server UG / PG for %s S%dE%02d…", show_name, season, ep_num)
                ruby_url, _, pg_url = extract_toonstream_all_servers(show_name, season=season, episode=ep_num)

            # NOTE: "multi" also fetches pg_url as a fallback — if Multimovies returns None for this
            # show/episode, pg_url from Toonstream acts as the secondary server so has_changes fires.
            if _fetch_mm:
                logger.info("Extracting Server UG2 for %s S%dE%02d…", show_name, season, ep_num)
                multi_url = resolve_multimovies_episode(show_name, season=season, episode=ep_num)
                if not multi_url and not pg_url:
                    # Multimovies had nothing — try Toonstream pg_url as a secondary-server fallback
                    logger.info("Multimovies empty for %s S%dE%02d — trying Toonstream PG fallback…", show_name, season, ep_num)
                    _, _, pg_url = extract_toonstream_all_servers(show_name, season=season, episode=ep_num)

            if _fetch_mg:
                logger.info("Extracting Server MG (satoru.one) for %s S%dE%02d…", show_name, season, ep_num)
                mg_url = resolve_satoru_episode(show_name, season=season, episode=ep_num)

            return ep_num, {"ruby_url": ruby_url, "multi_url": multi_url, "mg_url": mg_url, "pg_url": pg_url}

        if len(episodes_to_fetch) == 1:
            if not (skip_fn and skip_fn()):
                ep_num, res = _fetch_ep_task(episodes_to_fetch[0])
                newly_fetched[ep_num] = res
            else:
                log(f"   ⏩ Skipped: {show_name} S{season} (user requested skip during fetch)", "warn")
                continue
        elif len(episodes_to_fetch) > 1:
            max_w = min(10, len(episodes_to_fetch))
            _skip_during_fetch = False
            with ThreadPoolExecutor(max_workers=max_w) as executor:
                futures = [executor.submit(_fetch_ep_task, ep_info) for ep_info in episodes_to_fetch]
                for fut in as_completed(futures):
                    # Check skip signal between episode results
                    if skip_fn and skip_fn():
                        log(f"   ⏩ Skipped: {show_name} S{season} (user requested skip during fetch)", "warn")
                        # Cancel any pending futures
                        for f in futures:
                            f.cancel()
                        _skip_during_fetch = True
                        break
                    try:
                        ep_num, res = fut.result()
                        newly_fetched[ep_num] = res
                    except Exception as exc:
                        logger.warning("Error fetching airing ep: %s", exc)
            if _skip_during_fetch:
                continue


        # ------ Merge existing + newly fetched into final episodes list ---------------------------
        all_ep_nums   = sorted(set(list(existing_streams.keys()) + list(newly_fetched.keys())))
        episodes_data = []
        has_changes   = False

        # downloads_map: quality -> [url_ep1, url_ep2, ...]  (passed in from UI, or empty)
        _dl_map = downloads_map or {}

        # Parse existing Pixeldrain download links already on the post so we never wipe them
        from wp_api import parse_existing_post_downloads
        existing_dls = parse_existing_post_downloads(content)

        # If downloads were provided and the post doesn't already have them, that itself is a change
        if _dl_map:
            has_changes = True

        for ep_num in all_ep_nums:
            ex = existing_streams.get(ep_num, {})
            nw = newly_fetched.get(ep_num, {})

            ruby_url  = nw.get("ruby_url")  or ex.get("ruby_url",  "") or None
            ag_url    = nw.get("ag_url")    or ex.get("ag_url",    "") or None
            multi_url = nw.get("multi_url") or ex.get("multi_url", "") or None
            mg_url    = nw.get("mg_url")    or ex.get("mg_url",    "") or None
            pg_url    = nw.get("pg_url")    or ex.get("pg_url",    "") or None

            if ep_num in newly_fetched:
                if nw.get("ruby_url")  and not ex.get("has_ruby"):
                    has_changes = True
                if nw.get("ag_url")    and not ex.get("has_ag"):
                    has_changes = True
                if nw.get("multi_url") and not ex.get("has_multi"):
                    has_changes = True
                if nw.get("mg_url")    and not ex.get("has_mg"):
                    has_changes = True
                if nw.get("pg_url")    and not ex.get("has_pg"):
                    has_changes = True
                if not ex and (ruby_url or ag_url or multi_url or mg_url or pg_url):
                    has_changes = True  # brand-new episode

            # Attach per-episode download links (index = ep_num - 1)
            ep_downloads = {
                q: links[ep_num - 1]
                for q, links in _dl_map.items()
                if ep_num - 1 < len(links)
            }
            # If no fresh downloads provided via UI, fall back to existing Pixeldrain links
            if not ep_downloads and ep_num in existing_dls:
                ep_downloads = dict(existing_dls[ep_num])

            if ruby_url or ag_url or multi_url or mg_url or pg_url or ep_downloads:
                episodes_data.append({
                    "episode_num": ep_num,
                    "ruby_url":    ruby_url,
                    "ag_url":      ag_url,
                    "multi_url":   multi_url,
                    "mg_url":      mg_url,
                    "pg_url":      pg_url,
                    "downloads":   ep_downloads,
                })

        if not episodes_data:
            log(f"   ⚠️ No active streams at all for {show_name} (S{season})", "muted")
            continue

        if not has_changes:
            # Even if no new stream URLs were found, check whether the post can now be
            # marked Completed (AniList FINISHED + all episodes present) — and if so,
            # update the title/category without touching the episode toggle HTML.
            all_cats_check_nc   = api.get_all_categories()
            cat_id_to_name_nc   = {v: k.lower() for k, v in all_cats_check_nc.items()}
            _post_completed_nc  = any("completed" in cat_id_to_name_nc.get(cid, "") for cid in categories)
            _meta_nc            = get_show_metadata(show_name, content_type, season=season)
            _anilist_status_nc  = _meta_nc.get("status", "")
            _expected_eps_nc    = _meta_nc.get("expected_episodes") or expected_eps
            _anilist_fin_nc     = bool(_anilist_status_nc and _anilist_status_nc.upper() in ("FINISHED", "CANCELLED"))
            _active_ep_nc       = max_ex  # highest ep already in the post
            _post_title_nc      = post.get("raw_title", "") or f"{show_name} (Season {season})"
            _all_eps_title_nc   = bool(re.search(r'[-–—]\s*All\s+Episodes?\s+Added!', _post_title_nc, re.IGNORECASE))

            _should_complete = (
                not _post_completed_nc          # not already completed
                and _anilist_fin_nc              # AniList confirmed FINISHED
                and _expected_eps_nc             # known episode count
                and _expected_eps_nc > 0
                and _active_ep_nc >= _expected_eps_nc  # post has all episodes
                and (avail_count >= _expected_eps_nc or _active_ep_nc >= avail_count)
            )

            if _should_complete:
                # Convert post from Airing → Completed (title + categories only, no toggle change)
                _all_cats_cmp   = api.get_all_categories()
                _cat_id_nm_cmp  = {v: k.lower() for k, v in _all_cats_cmp.items()}
                _airing_ids     = {cid for cid, nm in _cat_id_nm_cmp.items() if any(k in nm for k in ("airing", "ongoing"))}
                _completed_ids  = {cid for cid, nm in _cat_id_nm_cmp.items() if "completed" in nm}
                _new_cats       = (set(categories) - _airing_ids) | _completed_ids
                _new_title      = f"{show_name} (Season {season})"
                ok = api.patch_post_with_streams(
                    post_id,
                    toggle_html=None,        # None = leave toggle HTML untouched
                    is_airing=False,
                    latest_episode=_active_ep_nc,
                    airing_title_suffix="",
                    category_ids=sorted(_new_cats),
                    custom_title=_new_title,
                )
                if ok:
                    log(f"   🎉 {show_name} (S{season}) auto-completed: AniList=FINISHED, {_active_ep_nc}/{_expected_eps_nc} eps.", "success")
                    updated_cnt += 1
                else:
                    log(f"   ⚠️ Auto-complete patch failed for {show_name} (S{season})", "warn")
            else:
                log(f"   ✅ No new streams for {show_name} (S{season})", "muted")
            continue

        active_eps      = [e for e in episodes_data if e.get("ruby_url") or e.get("ag_url") or e.get("multi_url") or e.get("mg_url") or e.get("pg_url") or e.get("downloads")]
        latest_active_ep = max((e.get("episode_num", 1) for e in active_eps), default=0)
        found_ruby      = sum(1 for e in episodes_data if e.get("ruby_url"))
        found_ag        = sum(1 for e in episodes_data if e.get("ag_url"))
        found_multi     = sum(1 for e in episodes_data if e.get("multi_url"))
        found_mg        = sum(1 for e in episodes_data if e.get("mg_url"))
        found_pg        = sum(1 for e in episodes_data if e.get("pg_url"))

        # ------ Check if post is already marked Completed by the user -------------------------
        # If the post currently has "Completed" category but NOT "Airing" title in its title,
        # the user has manually completed it — never flip it back to Airing.
        all_cats_check   = api.get_all_categories()
        cat_id_to_name_c = {v: k.lower() for k, v in all_cats_check.items()}
        post_has_completed = any("completed" in cat_id_to_name_c.get(cid, "") for cid in categories)
        post_has_airing_title = post.get("is_airing_title", False)
        post_title = post.get("raw_title", "") or f"{show_name} (Season {season})"
        post_has_all_episodes_title = post.get("is_all_episodes_title", False) or bool(re.search(r'[-–—]\s*All\s+Episodes?\s+Added!', post_title, re.IGNORECASE))

        target_total       = expected_eps if expected_eps else total_search_eps
        anilist_responded  = bool(anilist_status and str(anilist_status).strip())  # False if AniList timed out / returned nothing
        anilist_finished   = anilist_responded and anilist_status.upper() in ("FINISHED", "CANCELLED")

        # Check if source sites (Toonstream or Multimovies) have ALL expected episodes (crucial:
        # AniList marks the Japanese original as FINISHED even while Hindi dub is still ongoing).
        avail_has_all_episodes = (avail_count >= expected_eps) if (expected_eps and expected_eps > 0 and avail_count > 0) else False

        # ── Completion decision — conservative: when in doubt, stay Airing ──────────────────
        #
        # ONLY mark Completed when ALL of:
        #   1. AniList confirmed FINISHED/CANCELLED
        #   2. expected_eps is known (> 0)
        #   3. Our post has all expected episodes (latest_active_ep >= expected_eps)
        #   4. Source sites also have all expected episodes (or post is already caught up with sources)
        #
        # STAY AIRING when:
        #   • AniList says RELEASING
        #   • expected_eps is unknown (cannot know if show is done)
        #   • Post title still has "Episode X Added!" (user hasn't manually completed it)
        #   • We have fewer episodes than expected (e.g. Re:Zero S4: 13/19 eps)
        #   • Source sites still haven't released all expected episodes (dub lag)

        if post_has_completed and not fetch_new:
            # Post already manually marked Completed — never flip back unless a brand-new episode appeared
            is_airing = False

        elif post_has_all_episodes_title and not fetch_new:
            # Title says "All Episodes Added!" — treat as completed
            is_airing = False

        elif anilist_responded and anilist_status.upper() == "RELEASING":
            # AniList explicitly says still releasing → always stay Airing
            is_airing = True

        elif (
            anilist_finished                       # AniList confirmed done
            and expected_eps                       # we know the total episode count
            and expected_eps > 0
            and latest_active_ep >= expected_eps   # our post has all expected episodes
            and (avail_has_all_episodes or latest_active_ep >= avail_count)
        ):
            # All sources agree: AniList done + post has all expected episodes
            is_airing = False

        elif expected_eps and expected_eps > 0 and latest_active_ep < expected_eps:
            # We know the total and we don't have all episodes yet → still Airing
            is_airing = True

        elif not expected_eps or expected_eps <= 0:
            # Unknown total episode count — never auto-complete, always stay Airing
            # (This is the safe fallback: Re:Zero S4, ongoing seasonal anime, etc.)
            is_airing = True

        elif latest_active_ep <= 1 and not (anilist_finished and expected_eps == 1):
            # Only 1 or 0 episodes present and it's not a 1-episode special → Airing
            is_airing = True

        else:
            # Catch-all: preserve whatever the post currently has
            is_airing = not post_has_completed

        is_completed = not is_airing

        if is_airing and latest_active_ep > 0:
            final_title = f"{show_name} (Season {season}) - Episode {latest_active_ep} Added!"
        else:
            final_title = f"{show_name} (Season {season})"

        # ------ Build updated category list ---------------------------------------------------------------------------------------------------------------
        # Keep ALL existing categories but swap Airing <-> Completed.
        # Never strip genre/language/quality categories.
        all_cats_map    = api.get_all_categories()            # {name: id}
        cat_id_to_name  = {v: k.lower() for k, v in all_cats_map.items()}

        # Identify which IDs are "airing/ongoing" and which are "completed"
        airing_status_ids    = {cid for cid, nm in cat_id_to_name.items()
                                if any(k in nm for k in ("airing", "ongoing"))}
        completed_status_ids = {cid for cid, nm in cat_id_to_name.items()
                                if "completed" in nm}

        # Start from the post's current categories
        existing_cat_ids = set(categories)   # already fetched from WP post

        # Remove whichever status bucket is now wrong
        if is_completed:
            existing_cat_ids -= airing_status_ids      # remove Airing/Ongoing
            if completed_status_ids:
                existing_cat_ids |= completed_status_ids  # add Completed
        else:
            existing_cat_ids -= completed_status_ids   # remove Completed
            if airing_status_ids:
                existing_cat_ids |= airing_status_ids  # ensure Airing is present

        cat_ids = sorted(existing_cat_ids)

        from stream_extractor import generate_episode_toggle_html
        toggle_html = generate_episode_toggle_html(
            episodes_data,
            series_name=show_name,
            season_num=season,
        )

        # ------ Replace the episode toggle in the RAW shortcode content ---------------------------
        # Using content_raw ensures [toggle] shortcodes are matched correctly.
        # If raw content is unavailable, fall back to rendered HTML replacement.
        replace_in   = content_raw if content_raw else content
        sc_pattern   = r'\[toggle title="Episode List"[^\]]*\].*?\[/toggle\]'
        html_pattern = r'<div class="toggle[^"]*">[\s\S]*?<h3[^>]*>\s*Episode List[\s\S]*?</div>\s*</div>\s*</div>'

        if re.search(sc_pattern, replace_in, flags=re.DOTALL | re.IGNORECASE):
            updated_content = re.sub(sc_pattern, toggle_html, replace_in, flags=re.DOTALL | re.IGNORECASE)
        elif re.search(html_pattern, replace_in, flags=re.DOTALL | re.IGNORECASE):
            updated_content = re.sub(html_pattern, toggle_html, replace_in, flags=re.DOTALL | re.IGNORECASE)
        else:
            # No existing toggle found --- append (first-time update)
            updated_content = replace_in + "\n\n" + toggle_html

        # Only bump the post date when a brand-new episode was added.
        # If we only filled in a missing server for an existing episode, leave the timestamp alone.
        new_episode_added = bool(fetch_new and any(
            newly_fetched.get(ep_num, {}).get("ruby_url")
            or newly_fetched.get(ep_num, {}).get("multi_url")
            or newly_fetched.get(ep_num, {}).get("mg_url")
            or newly_fetched.get(ep_num, {}).get("pg_url")
            for ep_num, _ in fetch_new
        ))

        if api.update_post_content(
            post_id, updated_content,
            new_title=final_title,
            new_category_ids=cat_ids,
            update_date=new_episode_added,
        ):
            updated_cnt += 1
            status_str = "Airing" if is_airing else "Completed"
            date_note  = "date bumped" if new_episode_added else "date unchanged"
            log(
                f"   \U0001f504 UPDATED: '{final_title}' | Status: {status_str} | {date_note} | "
                f"Ruby: {found_ruby}/{total_search_eps}  "
                f"Server UG2: {found_multi}/{total_search_eps}  "
                f"Server MG: {found_mg}/{total_search_eps}  "
                f"Server PG: {found_pg}/{total_search_eps}",
                "success",
            )

    total_checked = len(airing_posts) + newly_added_anime_cnt
    total_updated = updated_cnt + newly_added_anime_cnt
    return total_checked, total_updated


# ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
#  Colours & font constants
# ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

C_BG        = "#0a0c14"   # near-black background
C_SURFACE   = "#111420"   # slightly lighter surface
C_CARD      = "#181c2e"   # card background
C_CARD2     = "#1e2235"   # slightly lifted card (hover)
C_ACCENT    = "#7c6ff7"   # primary violet
C_ACCENT2   = "#38bdf8"   # cyan highlight
C_ACCENT3   = "#a78bfa"   # soft purple
C_SUCCESS   = "#00FF7F"   # Spring/Vivid Green
C_ERROR     = "#FF3344"   # Bright Red
C_WARN      = "#FFA500"   # Vivid Orange
C_INFO      = "#00BFFF"   # Deep Sky Blue
C_CYAN      = "#00FFFF"   # Electric Cyan
C_PURPLE    = "#BD93F9"   # Bright Purple
C_TEXT      = "#F8F8F2"   # High-contrast text
C_MUTED     = "#8892B0"   # Soft Slate Blue
C_BORDER    = "#252840"   # card border
C_BORDER2   = "#2e3357"   # brighter border for focus
C_SIDEBAR   = "#0e1120"   # sidebar bg
C_NAV_SEL   = "#1a1f35"   # selected nav item bg

FONT_HEAD   = ("Segoe UI", 20, "bold")
FONT_SUB    = ("Segoe UI", 12, "bold")
FONT_SUBHEAD = FONT_SUB
FONT_BODY   = ("Segoe UI", 11)
FONT_MONO   = ("Cascadia Code", 10) if True else ("Consolas", 10)
FONT_SMALL  = ("Segoe UI", 9)
FONT_TINY   = ("Segoe UI", 8)


# ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
#  Stat card widget
# ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

class StatCard(ctk.CTkFrame):
    def __init__(self, parent, label: str, value: str = "0", color: str = C_ACCENT, icon: str = ""):
        super().__init__(parent, fg_color=C_CARD, corner_radius=14,
                         border_width=1, border_color=C_BORDER)
        self._color = color
        # Top accent strip
        strip = ctk.CTkFrame(self, fg_color=color, height=3, corner_radius=0)
        strip.pack(fill="x")
        inner = ctk.CTkFrame(self, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=14, pady=(8, 12))
        # Icon + Label row
        top = ctk.CTkFrame(inner, fg_color="transparent")
        top.pack(fill="x")
        if icon:
            ctk.CTkLabel(top, text=icon, font=("Segoe UI", 14),
                         text_color=color).pack(side="left", padx=(0, 6))
        ctk.CTkLabel(top, text=label, font=FONT_SMALL,
                     text_color=C_MUTED).pack(side="left", anchor="w")
        self._val_label = ctk.CTkLabel(inner, text=value,
                                       font=("Segoe UI", 28, "bold"),
                                       text_color=color)
        self._val_label.pack(anchor="w", pady=(2, 0))

    def set(self, value: str):
        self._val_label.configure(text=str(value))


# ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
#  Main Application
# ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

class AutoPosterApp(ctk.CTk):

    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self._stop_flag: list[bool] = [False]
        self._run_thread: threading.Thread | None = None
        self._log_queue: queue.Queue = queue.Queue()
        self._results: list[dict] = []          # created draft records
        self._dl_stop_event = threading.Event()
        self._dl_skip_event = threading.Event()

        # Counters
        self._cnt_total    = 0
        self._cnt_created  = 0
        self._cnt_skipped  = 0
        self._cnt_errors   = 0
        # Log widgets attributes initialization
        self._mini_log: ctk.CTkTextbox | None = None
        self._full_log: ctk.CTkTextbox | None = None

        self._build_window()
        self._build_ui()
        self._poll_log_queue()

        # Start ToonWorld4All cookie auto-sync bridge server
        try:
            from tw4all_resolver import start_cookie_bridge_server
            start_cookie_bridge_server(4545)
        except Exception:
            pass

        # Auto-sync domains from CloudStream extensions (phisher98 TVVVV/domains.json) in background
        def _bg_domain_sync():
            try:
                from stream_extractor import sync_domains_from_cloudstream
                synced = sync_domains_from_cloudstream(save_to_settings=True)
                if synced.get("toonstream") or synced.get("multimovies"):
                    self._log(f"🌐 CloudStream domains synced: Toonstream={synced.get('toonstream')} | MultiMovies={synced.get('multimovies')}", "info")
            except Exception as e:
                logger.debug("Background domain sync failed: %s", e)

        threading.Thread(target=_bg_domain_sync, daemon=True).start()

    # ------ Window setup ------------------------------------------------------------------------------------------------------------------------------------------------------------------

    def _build_window(self):
        self.title(f"WP Auto-Poster  v{APP_VERSION}")
        self.geometry("1160x800")
        self.minsize(800, 600)
        self.configure(fg_color=C_BG)
        try:
            self.iconbitmap(Path(__file__).parent / "icon.ico")
        except Exception:
            pass

    # ------ Full UI ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

    def _build_ui(self):
        # ------ Left sidebar ---------------------------------------------------------------------------------------------------------------------------------------------------
        sidebar = ctk.CTkFrame(self, width=230, fg_color=C_SIDEBAR, corner_radius=0)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        # Logo block
        logo_frame = ctk.CTkFrame(sidebar, fg_color="transparent")
        logo_frame.pack(fill="x", padx=20, pady=(28, 0))
        ctk.CTkLabel(logo_frame, text="⚡", font=("Segoe UI", 28),
                     text_color=C_ACCENT2).pack(side="left", padx=(0, 8))
        title_col = ctk.CTkFrame(logo_frame, fg_color="transparent")
        title_col.pack(side="left")
        ctk.CTkLabel(title_col, text="AutoPoster",
                     font=("Segoe UI", 16, "bold"),
                     text_color=C_TEXT).pack(anchor="w")
        ctk.CTkLabel(title_col, text=f"v{APP_VERSION}  •  WordPress",
                     font=FONT_TINY, text_color=C_MUTED).pack(anchor="w")

        # Divider
        ctk.CTkFrame(sidebar, height=1, fg_color=C_BORDER).pack(fill="x", padx=16, pady=20)

        # Nav buttons
        self._nav_btns: dict[str, ctk.CTkButton] = {}
        nav_items = [
            ("Auto Sync",        "▶",  "Automatic post sync & airing updater"),
            ("Post from URL",    "⚡",  "Post directly from Toonworld4all URLs"),
            ("Download Adder",   "📥", "Auto & manual BollyFlix to Pixeldrain download adder"),
            ("Link Extractor",   "🔗",  "Extract raw episode stream links"),
            ("TW4All Extractor", "🌍", "Dedicated ToonWorld4All Hubcloud extractor"),
            ("Fix Streams",      "🔄",  "Batch upgrade RubyVidHub/RubyStm links to AS-CDN"),
            ("Results",          "📋",  "View created draft posts"),
            ("Settings",         "⚙",  "Credentials & security settings"),
        ]
        for name, icon, tip in nav_items:
            btn = ctk.CTkButton(
                sidebar,
                text=f"  {icon}   {name}",
                anchor="w",
                fg_color="transparent",
                hover_color=C_NAV_SEL,
                text_color=C_MUTED,
                font=("Segoe UI", 12),
                height=44,
                corner_radius=10,
                command=lambda n=name: self._switch_tab(n),
            )
            btn.pack(fill="x", padx=12, pady=2)
            self._nav_btns[name] = btn

        # Bottom info
        ctk.CTkFrame(sidebar, height=1, fg_color=C_BORDER).pack(fill="x", padx=16, pady=20)
        ctk.CTkLabel(sidebar, text="Source: Neeraj",
                     font=FONT_TINY, text_color=C_MUTED,
                     wraplength=200).pack(padx=20, anchor="w")

        # Status dot
        self._status_dot = ctk.CTkLabel(sidebar, text="● Idle",
                                         font=FONT_SMALL, text_color=C_MUTED)
        self._status_dot.pack(padx=20, anchor="w", pady=(6, 0))

        # ------ Main content area ---------------------------------------------------------------------------------------------------------------------------------------
        self._main = ctk.CTkFrame(self, fg_color=C_BG, corner_radius=0)
        self._main.pack(side="left", fill="both", expand=True)

        self._pages: dict[str, ctk.CTkFrame] = {}
        for name in ("Auto Sync", "Post from URL", "Download Adder", "Link Extractor", "TW4All Extractor", "Fix Streams", "Results", "Settings"):
            frame = ctk.CTkFrame(self._main, fg_color=C_BG, corner_radius=0)
            frame.place(relx=0, rely=0, relwidth=1, relheight=1)
            self._pages[name] = frame

        self._build_auto_sync_page(self._pages["Auto Sync"])
        self._build_post_url_page(self._pages["Post from URL"])
        self._build_download_adder_page(self._pages["Download Adder"])
        self._build_link_extractor_page(self._pages["Link Extractor"])
        self._build_tw4all_extractor_page(self._pages["TW4All Extractor"])
        self._build_fix_streams_page(self._pages["Fix Streams"])
        self._build_results_page(self._pages["Results"])
        self._build_settings_page(self._pages["Settings"])

        self._switch_tab("Auto Sync")

    # ------ Navigation ------------------------------------------------------------------------------------------------------------------------------------------------------------------------

    def _switch_tab(self, name: str):
        # Password-protect the Settings tab
        if name == "Settings":
            pwd = self.settings.get("settings_password", "").strip()
            if pwd:
                entered = simpledialog.askstring(
                    "Settings Locked",
                    "Enter the settings password:",
                    show="*",
                    parent=self,
                )
                if entered is None or entered != pwd:
                    if entered is not None:
                        messagebox.showerror("Access Denied", "Incorrect password.", parent=self)
                    return

        for n, btn in self._nav_btns.items():
            is_sel = (n == name)
            btn.configure(
                fg_color=C_NAV_SEL if is_sel else "transparent",
                text_color=C_ACCENT2 if is_sel else C_MUTED,
                font=("Segoe UI", 12, "bold") if is_sel else ("Segoe UI", 12),
            )
        for n, page in self._pages.items():
            if n == name:
                page.lift()

    # ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
    #  RUN PAGE
    # ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

        # ══════════════════════════════════════════════════════════════════════
    #  PAGE 1: AUTO SYNC & AIRING UPDATER
    # ══════════════════════════════════════════════════════════════════════

    def _build_auto_sync_page(self, parent: ctk.CTkFrame):
        # Header
        hdr = ctk.CTkFrame(parent, fg_color="transparent")
        hdr.pack(fill="x", padx=24, pady=(20, 0))

        hdr_left = ctk.CTkFrame(hdr, fg_color="transparent")
        hdr_left.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(hdr_left, text="Auto Sync & Airing Posts", font=FONT_HEAD, text_color=C_TEXT, anchor="w").pack(anchor="w")
        ctk.CTkLabel(hdr_left, text="Scrape toonworld4all.me & auto-update airing posts", font=FONT_SMALL, text_color=C_MUTED, anchor="w", wraplength=500).pack(anchor="w", pady=(2, 0))

        ctk.CTkButton(
            hdr, text="🔓 Auto-Bypass Cloudflare",
            font=("Segoe UI", 11, "bold"),
            fg_color="#10b981", hover_color="#059669",
            height=34, width=190, corner_radius=8,
            command=self._launch_cf_autobypass,
        ).pack(side="right")

        # Stat cards row
        cards_row = ctk.CTkFrame(parent, fg_color="transparent")
        cards_row.pack(fill="x", padx=24, pady=(14, 0))
        self._stat_total   = StatCard(cards_row, "Total Checked",  "0", C_ACCENT2, "[?]")
        self._stat_created = StatCard(cards_row, "Drafts Created", "0", C_SUCCESS, "[+]")
        self._stat_skipped = StatCard(cards_row, "Already Exist",  "0", C_WARN,    "[=]")
        self._stat_errors  = StatCard(cards_row, "Errors",          "0", C_ERROR,   "[X]")
        for card in (self._stat_total, self._stat_created, self._stat_skipped, self._stat_errors):
            card.pack(side="left", expand=True, fill="both", padx=(0, 8), pady=2)

        # Progress & controls card
        ctrl_card = ctk.CTkFrame(parent, fg_color=C_CARD, corner_radius=12, border_width=1, border_color=C_BORDER)
        ctrl_card.pack(fill="x", padx=24, pady=(12, 0))
        ctrl_inner = ctk.CTkFrame(ctrl_card, fg_color="transparent")
        ctrl_inner.pack(fill="x", padx=16, pady=12)

        # Progress bar
        prog_row = ctk.CTkFrame(ctrl_inner, fg_color="transparent")
        prog_row.pack(fill="x", pady=(0, 8))
        self._progress_label = ctk.CTkLabel(prog_row, text="Ready to start", font=("Segoe UI", 11, "bold"), text_color=C_TEXT)
        self._progress_label.pack(side="left")
        self._progress_sub = ctk.CTkLabel(prog_row, text="", font=FONT_SMALL, text_color=C_MUTED)
        self._progress_sub.pack(side="right")
        self._progress_bar = ctk.CTkProgressBar(ctrl_inner, height=6, progress_color=C_ACCENT, fg_color=C_BORDER, corner_radius=3)
        self._progress_bar.pack(fill="x", pady=(0, 10))
        self._progress_bar.set(0)

        # Options row
        row2 = ctk.CTkFrame(ctrl_inner, fg_color="transparent")
        row2.pack(fill="x", pady=(0, 6))

        self._test_mode_var = ctk.BooleanVar(value=self.settings.get("test_mode", False))
        ctk.CTkCheckBox(row2, text="Test mode", variable=self._test_mode_var, fg_color=C_ACCENT, font=FONT_BODY, text_color=C_TEXT).pack(side="left")
        ctk.CTkLabel(row2, text="Pages:", font=FONT_SMALL, text_color=C_MUTED).pack(side="left", padx=(8, 4))
        self._test_pages_var = tk.StringVar(value=str(self.settings.get("test_pages", 1)))
        ctk.CTkEntry(row2, textvariable=self._test_pages_var, width=50, height=28, font=FONT_BODY, fg_color=C_SURFACE, border_color=C_BORDER2, text_color=C_TEXT).pack(side="left")

        self._extract_streams_var = ctk.BooleanVar(value=self.settings.get("extract_episode_streams", True))
        ctk.CTkCheckBox(row2, text="Extract stream servers", variable=self._extract_streams_var, command=self._on_extract_streams_toggled, fg_color=C_ACCENT, font=FONT_BODY, text_color=C_TEXT).pack(side="left", padx=(16, 0))

        # Buttons row (separate row so they're never hidden)
        btns_f = ctk.CTkFrame(ctrl_inner, fg_color="transparent")
        btns_f.pack(fill="x", pady=(0, 2))

        self._start_btn = ctk.CTkButton(btns_f, text="> Start Sync", font=("Segoe UI", 11, "bold"), fg_color=C_ACCENT, hover_color="#5a54c8", height=34, width=120, corner_radius=8, command=self._start_sync)
        self._start_btn.pack(side="left", padx=(0, 6))

        self._stop_btn = ctk.CTkButton(btns_f, text="[X] Stop", font=("Segoe UI", 11, "bold"), fg_color=C_ERROR, hover_color="#c0192f", height=34, width=90, corner_radius=8, state="disabled", command=self._stop_sync)
        self._stop_btn.pack(side="left", padx=(0, 6))

        self._reset_btn = ctk.CTkButton(btns_f, text="Reset", font=FONT_BODY, fg_color=C_SURFACE, hover_color=C_CARD2, height=34, width=80, corner_radius=8, command=self._reset_stats)
        self._reset_btn.pack(side="left", padx=(0, 6))

        self._update_airing_btn = ctk.CTkButton(btns_f, text="Update Airing Posts", font=("Segoe UI", 11, "bold"), fg_color="#0f3460", hover_color="#164080", height=34, width=150, corner_radius=8, command=self._start_update_airing)
        self._update_airing_btn.pack(side="left", padx=(0, 6))

        self._skip_airing_btn = ctk.CTkButton(
            btns_f,
            text="⏩ Skip Series",
            font=("Segoe UI", 11, "bold"),
            fg_color="#7a5800",
            hover_color="#9a7010",
            height=34,
            width=110,
            corner_radius=8,
            state="disabled",
            command=self._skip_current_airing,
        )
        self._skip_airing_btn.pack(side="left")

        # Live Log section — fills bottom half cleanly!
        log_hdr = ctk.CTkFrame(parent, fg_color="transparent")
        log_hdr.pack(fill="x", padx=24, pady=(12, 4))
        ctk.CTkLabel(log_hdr, text="Live Output Log", font=FONT_SUB, text_color=C_TEXT).pack(side="left")
        ctk.CTkButton(log_hdr, text="Clear Log", font=FONT_SMALL, fg_color=C_SURFACE, hover_color=C_CARD2, height=24, width=70, corner_radius=6, command=self._clear_log).pack(side="right")

        self._mini_log = ctk.CTkTextbox(
            parent, fg_color=C_SURFACE, text_color=C_TEXT,
            font=("Cascadia Code", 13),
            border_color=C_BORDER, border_width=1,
            corner_radius=10, wrap="word",
        )
        self._mini_log.pack(fill="both", expand=True, padx=24, pady=(0, 16))
        self._mini_log.configure(state="disabled")
        # Apply line-spacing and padding to the underlying Tk Text widget
        try:
            _tk = getattr(self._mini_log, "_textbox", self._mini_log)
            _tk.configure(spacing1=6, spacing2=2, spacing3=8, padx=10, pady=8)
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════════════
    #  PAGE 2: POST FROM URL
    # ══════════════════════════════════════════════════════════════════════

    def _build_post_url_page(self, parent: ctk.CTkFrame):
        scroll = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=0, pady=0)
        p = scroll

        hdr = ctk.CTkFrame(p, fg_color="transparent")
        hdr.pack(fill="x", padx=24, pady=(20, 0))
        ctk.CTkLabel(hdr, text="Post from URL / Download Adder", font=FONT_HEAD, text_color=C_TEXT).pack(side="left")
        ctk.CTkLabel(hdr, text="Create WP posts from URLs & add 480p/720p/1080p Pixeldrain downloads", font=FONT_SMALL, text_color=C_MUTED).pack(side="left", padx=(12, 0), pady=(4, 0))

        card = ctk.CTkFrame(p, fg_color=C_CARD, corner_radius=12, border_width=1, border_color=C_BORDER)
        card.pack(fill="x", padx=24, pady=(14, 0))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=18, pady=16)

        ctk.CTkLabel(inner, text="ToonWorld4All Post URLs (one per line):", font=FONT_BODY, text_color=C_MUTED).pack(anchor="w", pady=(0, 4))
        self._url_box = ctk.CTkTextbox(inner, height=100, fg_color=C_SURFACE, text_color=C_TEXT, font=FONT_MONO, border_color=C_BORDER2, border_width=1, corner_radius=8, wrap="none")
        self._url_box.pack(fill="x", pady=(0, 12))

        override_row = ctk.CTkFrame(inner, fg_color="transparent")
        override_row.pack(fill="x", pady=(0, 12))

        ts_box = ctk.CTkFrame(override_row, fg_color="transparent")
        ts_box.pack(side="left", fill="x", expand=True, padx=(0, 4))
        ctk.CTkLabel(ts_box, text="Toonstream URL (Optional):", font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w")
        self._manual_toonstream_var = tk.StringVar()
        ctk.CTkEntry(ts_box, textvariable=self._manual_toonstream_var, font=FONT_MONO, placeholder_text="https://toon-stream.site/series/...", fg_color=C_SURFACE, border_color=C_BORDER2, text_color=C_TEXT, height=34).pack(fill="x", pady=(2, 0))

        mm_box = ctk.CTkFrame(override_row, fg_color="transparent")
        mm_box.pack(side="left", fill="x", expand=True, padx=(4, 4))
        ctk.CTkLabel(mm_box, text="Multimovies URL (Optional):", font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w")
        self._manual_multimovies_var = tk.StringVar()
        ctk.CTkEntry(mm_box, textvariable=self._manual_multimovies_var, font=FONT_MONO, placeholder_text="https://m1.multimovies.courses/series/...", fg_color=C_SURFACE, border_color=C_BORDER2, text_color=C_TEXT, height=34).pack(fill="x", pady=(2, 0))

        dl_box = ctk.CTkFrame(override_row, fg_color="transparent")
        dl_box.pack(side="left", fill="x", expand=True, padx=(4, 0))
        ctk.CTkLabel(dl_box, text="ToonWorld4All / BollyFlix / Download URL (Optional):", font=FONT_SMALL, text_color=C_ACCENT2).pack(anchor="w")
        self._manual_download_var = tk.StringVar()
        ctk.CTkEntry(dl_box, textvariable=self._manual_download_var, font=FONT_MONO, placeholder_text="https://toonworld4all.me/... or https://bollyflix.af/...", fg_color=C_SURFACE, border_color=C_BORDER2, text_color=C_TEXT, height=34).pack(fill="x", pady=(2, 0))

        btn_row = ctk.CTkFrame(inner, fg_color="transparent")
        btn_row.pack(fill="x")
        self._post_url_btn = ctk.CTkButton(btn_row, text="⚡  Post from URLs & Downloads", font=("Segoe UI", 12, "bold"), fg_color="#1a5c2e", hover_color="#226b38", height=40, corner_radius=8, width=240, command=self._start_post_from_urls)
        self._post_url_btn.pack(side="left", padx=(0, 10))
        ctk.CTkButton(btn_row, text="Clear Box", font=FONT_BODY, fg_color=C_SURFACE, hover_color=C_CARD2, height=40, corner_radius=8, width=100, command=lambda: self._url_box.delete("1.0", "end")).pack(side="left")

    # ══════════════════════════════════════════════════════════════════════
    #  PAGE: DOWNLOAD ADDER (BollyFlix ➔ Pixeldrain)
    # ══════════════════════════════════════════════════════════════════════

    def _build_download_adder_page(self, parent: ctk.CTkFrame):
        scroll = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=0, pady=0)
        p = scroll

        hdr = ctk.CTkFrame(p, fg_color="transparent")
        hdr.pack(fill="x", padx=24, pady=(20, 0))
        ctk.CTkLabel(hdr, text="Download Option Adder", font=FONT_HEAD, text_color=C_TEXT, anchor="w").pack(anchor="w")
        ctk.CTkLabel(hdr, text="Scrape BollyFlix → Upload via Colab → Add Downloads to WP", font=FONT_SMALL, text_color=C_MUTED, anchor="w", wraplength=600).pack(anchor="w", pady=(2, 0))

        # ── Colab Server Card (GREEN) ─────────────────────────────────────────
        card_colab = ctk.CTkFrame(p, fg_color="#0f2a0f", corner_radius=12, border_width=2, border_color="#2e6b2e")
        card_colab.pack(fill="x", padx=24, pady=(14, 0))
        inner_colab = ctk.CTkFrame(card_colab, fg_color="transparent")
        inner_colab.pack(fill="x", padx=18, pady=14)

        colab_hdr = ctk.CTkFrame(inner_colab, fg_color="transparent")
        colab_hdr.pack(fill="x", pady=(0, 4))
        ctk.CTkLabel(colab_hdr, text="\U0001f30e Colab Server URL", font=FONT_SUBHEAD, text_color="#4ade80").pack(side="left")
        ctk.CTkLabel(colab_hdr, text="Google downloads the files \u2014 ZERO of your bandwidth used", font=FONT_SMALL, text_color="#86efac").pack(side="left", padx=(10, 0))

        colab_row = ctk.CTkFrame(inner_colab, fg_color="transparent")
        colab_row.pack(fill="x", pady=(4, 0))

        self._dl_colab_var = tk.StringVar(value=self.settings.get("colab_url", "https://flinch-unlighted-viewpoint.ngrok-free.dev/"))
        def _on_colab_change(*_):
            c_val = self._dl_colab_var.get().strip()
            if c_val and c_val != self.settings.get("colab_url"):
                self.settings["colab_url"] = c_val
                try:
                    save_settings(self.settings)
                except Exception:
                    pass
        self._dl_colab_var.trace_add("write", _on_colab_change)
        ctk.CTkEntry(
            colab_row,
            textvariable=self._dl_colab_var,
            font=FONT_MONO,
            placeholder_text="https://abc123.ngrok-free.app  \u2190 paste URL from Colab Cell 2 here",
            fg_color=C_SURFACE,
            border_color="#2e6b2e",
            text_color=C_TEXT,
            height=38
        ).pack(side="left", fill="x", expand=True, padx=(0, 8))

        ctk.CTkButton(
            colab_row,
            text="Test Connection",
            font=FONT_SMALL,
            fg_color="#1c4a1c",
            hover_color="#256325",
            height=38,
            width=140,
            command=self._test_colab_connection
        ).pack(side="left")

        ctk.CTkLabel(
            inner_colab,
            text="\U0001f4a1 Setup: Open Colab_Upload_Server.ipynb in Google Colab \u2192 Run Cell 1 \u2192 Run Cell 2 \u2192 Copy the URL it prints \u2192 Paste above",
            font=FONT_SMALL, text_color="#86efac", wraplength=680, justify="left"
        ).pack(anchor="w", pady=(8, 0))

        # ── Auto Mode Card ────────────────────────────────────────────────────
        card_auto = ctk.CTkFrame(p, fg_color=C_CARD, corner_radius=12, border_width=1, border_color=C_BORDER)
        card_auto.pack(fill="x", padx=24, pady=(14, 0))
        inner_auto = ctk.CTkFrame(card_auto, fg_color="transparent")
        inner_auto.pack(fill="x", padx=18, pady=16)

        top_row_auto = ctk.CTkFrame(inner_auto, fg_color="transparent")
        top_row_auto.pack(fill="x", pady=(0, 6))

        ctk.CTkLabel(top_row_auto, text="🚀 Automatic Mode (Category Index)", font=FONT_SUBHEAD, text_color=C_ACCENT2).pack(side="left")

        ctk.CTkLabel(top_row_auto, text="Source Provider:", font=FONT_SMALL, text_color=C_MUTED).pack(side="left", padx=(20, 6))
        self._dl_source_var = tk.StringVar(value="BollyFlix")

        def _on_dl_source_changed(choice):
            if choice == "ToonWorld4All":
                self._dl_cat_var.set("https://toonworld4all.me/category/anime/")
            elif choice == "MoviesMod":
                self._dl_cat_var.set("https://moviesmod.ai.in/category/anime-series/")
            else:
                bf_url = self.settings.get("bollyflix_url", "https://bollyflix.af").rstrip('/')
                self._dl_cat_var.set(f"{bf_url}/anime/")

        ctk.CTkOptionMenu(
            top_row_auto,
            variable=self._dl_source_var,
            values=["BollyFlix", "ToonWorld4All", "MoviesMod"],
            width=130,
            height=28,
            font=FONT_SMALL,
            command=_on_dl_source_changed
        ).pack(side="left")

        ctk.CTkLabel(inner_auto, text="Category URL:", font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w")

        bf_url = self.settings.get("bollyflix_url", "https://bollyflix.af").rstrip('/')
        self._dl_cat_var = tk.StringVar(value=f"{bf_url}/anime/")
        ctk.CTkEntry(inner_auto, textvariable=self._dl_cat_var, font=FONT_MONO, fg_color=C_SURFACE, border_color=C_BORDER2, text_color=C_TEXT, height=36).pack(fill="x", pady=(2, 8))

        # Options row 1: checkboxes
        row_opts1 = ctk.CTkFrame(inner_auto, fg_color="transparent")
        row_opts1.pack(fill="x", pady=(0, 4))

        self._dl_force_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(row_opts1, text="Force update (Overwrite existing download options)", variable=self._dl_force_var, font=FONT_SMALL, text_color=C_MUTED).pack(side="left")

        self._dl_airing_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(row_opts1, text="Update Airing Posts (Sync new episodes)", variable=self._dl_airing_var, font=FONT_SMALL, text_color=C_MUTED).pack(side="left", padx=(16, 0))

        # Options row 2: quality + workers + season
        row_opts2 = ctk.CTkFrame(inner_auto, fg_color="transparent")
        row_opts2.pack(fill="x", pady=(0, 8))

        self._dl_1080p_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(row_opts2, text="Include 1080p Quality", variable=self._dl_1080p_var, font=FONT_SMALL, text_color=C_ACCENT2).pack(side="left")

        self._dl_check_dead_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(row_opts2, text="Auto-Replace Dead Links", variable=self._dl_check_dead_var, font=FONT_SMALL, text_color=C_ACCENT2).pack(side="left", padx=(16, 0))

        ctk.CTkLabel(row_opts2, text="Workers:", font=FONT_SMALL, text_color=C_MUTED).pack(side="left", padx=(16, 4))
        self._dl_workers_var = tk.IntVar(value=4)
        ctk.CTkOptionMenu(
            row_opts2,
            variable=self._dl_workers_var,
            values=["1", "2", "3", "4", "5", "6", "8", "10"],
            width=70,
            height=28,
            font=FONT_SMALL
        ).pack(side="left")

        ctk.CTkLabel(row_opts2, text="WP Season:", font=FONT_SMALL, text_color=C_MUTED).pack(side="left", padx=(16, 4))
        self._dl_season_var = tk.StringVar(value="Auto")
        ctk.CTkComboBox(
            row_opts2,
            variable=self._dl_season_var,
            values=["Auto", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "2-3", "All"],
            width=85,
            height=28,
            font=FONT_SMALL
        ).pack(side="left")

        ctk.CTkLabel(row_opts2, text="Source Seasons:", font=FONT_SMALL, text_color=C_MUTED).pack(side="left", padx=(16, 4))
        self._dl_src_season_var = tk.StringVar(value="Auto")
        ctk.CTkComboBox(
            row_opts2,
            variable=self._dl_src_season_var,
            values=["Auto", "All", "1", "2", "3", "4", "5", "6", "7", "2-7"],
            width=95,
            height=28,
            font=FONT_SMALL
        ).pack(side="left")

        btn_row_auto = ctk.CTkFrame(inner_auto, fg_color="transparent")
        btn_row_auto.pack(anchor="w", pady=(4, 0))

        self._dl_auto_btn = ctk.CTkButton(
            btn_row_auto,
            text="\U0001f680  Start Auto Download Sync",
            font=("Segoe UI", 12, "bold"),
            fg_color="#1a5c2e",
            hover_color="#226b38",
            height=38,
            corner_radius=8,
            command=self._start_auto_download_adder
        )
        self._dl_auto_btn.pack(side="left", padx=(0, 10))

        self._dl_auto_skip_btn = ctk.CTkButton(
            btn_row_auto,
            text="⏩ Skip",
            font=("Segoe UI", 12, "bold"),
            fg_color="#d97706",
            hover_color="#b45309",
            height=38,
            corner_radius=8,
            state="disabled",
            command=self._skip_dl_post
        )
        self._dl_auto_skip_btn.pack(side="left", padx=(0, 10))

        self._dl_auto_stop_btn = ctk.CTkButton(
            btn_row_auto,
            text="[X] Stop",
            font=("Segoe UI", 12, "bold"),
            fg_color=C_ERROR,
            hover_color="#c0192f",
            height=38,
            corner_radius=8,
            state="disabled",
            command=lambda: self._dl_stop_event.set()
        )
        self._dl_auto_stop_btn.pack(side="left")

        # ── Manual Mode Card ──────────────────────────────────────────────────
        card_manual = ctk.CTkFrame(p, fg_color=C_CARD, corner_radius=12, border_width=1, border_color=C_BORDER)
        card_manual.pack(fill="x", padx=24, pady=(14, 0))
        inner_man = ctk.CTkFrame(card_manual, fg_color="transparent")
        inner_man.pack(fill="x", padx=18, pady=16)

        ctk.CTkLabel(inner_man, text="⚡ Manual Mode (BollyFlix / ToonWorld4All / MoviesMod URLs)", font=FONT_SUBHEAD, text_color=C_ACCENT2).pack(anchor="w", pady=(0, 4))
        ctk.CTkLabel(
            inner_man,
            text="Post URLs (one per line).  Syntax:  URL  |  WP_SEASON  |  SOURCE_SEASONS",
            font=FONT_SMALL,
            text_color=C_MUTED
        ).pack(anchor="w")
        ctk.CTkLabel(
            inner_man,
            text="  Supports BollyFlix, ToonWorld4All, and MoviesMod. Season ranges (e.g. 2-7, 2 to 7) are fully supported!",
            font=FONT_SMALL,
            text_color="#6a8fa0"
        ).pack(anchor="w", pady=(0, 2))
        ctk.CTkLabel(
            inner_man,
            text="  e.g.  https://bollyflix... | 2-3 | 2-7   ← WP Season 2-3 from BollyFlix Seasons 2 to 7 (62-200 eps)",
            font=FONT_SMALL,
            text_color="#6a8fa0"
        ).pack(anchor="w", pady=(0, 4))

        row_man_defaults = ctk.CTkFrame(inner_man, fg_color="transparent")
        row_man_defaults.pack(fill="x", pady=(0, 4))
        ctk.CTkLabel(row_man_defaults, text="Default WP Season:", font=FONT_SMALL, text_color=C_MUTED).pack(side="left", padx=(0, 4))
        self._dl_man_wp_season_var = tk.StringVar(value="Auto")
        ctk.CTkComboBox(
            row_man_defaults,
            variable=self._dl_man_wp_season_var,
            values=["Auto", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "2-3"],
            width=85,
            height=28,
            font=FONT_SMALL
        ).pack(side="left", padx=(0, 16))

        ctk.CTkLabel(row_man_defaults, text="Default Source Seasons:", font=FONT_SMALL, text_color=C_MUTED).pack(side="left", padx=(0, 4))
        self._dl_man_src_season_var = tk.StringVar(value="Auto")
        ctk.CTkComboBox(
            row_man_defaults,
            variable=self._dl_man_src_season_var,
            values=["Auto", "All", "1", "2", "3", "4", "5", "6", "7", "2-7"],
            width=95,
            height=28,
            font=FONT_SMALL
        ).pack(side="left")

        self._dl_manual_url_box = ctk.CTkTextbox(
            inner_man,
            height=90,
            font=FONT_MONO,
            fg_color=C_SURFACE,
            border_color=C_BORDER2,
            border_width=1,
            text_color=C_TEXT,
            corner_radius=8
        )
        self._dl_manual_url_box.pack(fill="x", pady=(2, 8))

        btn_row_man = ctk.CTkFrame(inner_man, fg_color="transparent")
        btn_row_man.pack(anchor="w", pady=(4, 0))

        self._dl_manual_btn = ctk.CTkButton(
            btn_row_man,
            text="⚡  Add Downloads from URLs",
            font=("Segoe UI", 12, "bold"),
            fg_color="#2b5278",
            hover_color="#366494",
            height=38,
            corner_radius=8,
            command=self._start_manual_download_adder
        )
        self._dl_manual_btn.pack(side="left", padx=(0, 10))

        self._dl_manual_skip_btn = ctk.CTkButton(
            btn_row_man,
            text="⏩ Skip",
            font=("Segoe UI", 12, "bold"),
            fg_color="#d97706",
            hover_color="#b45309",
            height=38,
            corner_radius=8,
            state="disabled",
            command=self._skip_dl_post
        )
        self._dl_manual_skip_btn.pack(side="left", padx=(0, 10))

        self._dl_manual_stop_btn = ctk.CTkButton(
            btn_row_man,
            text="[X] Stop",
            font=("Segoe UI", 12, "bold"),
            fg_color=C_ERROR,
            hover_color="#c0192f",
            height=38,
            corner_radius=8,
            state="disabled",
            command=lambda: self._dl_stop_event.set()
        )
        self._dl_manual_stop_btn.pack(side="left")

        # ── Successfully Added Downloads Tracker Card ─────────────────────────
        card_added = ctk.CTkFrame(p, fg_color="#0f2a1d", corner_radius=12, border_width=1, border_color="#1b5e3a")
        card_added.pack(fill="x", padx=24, pady=(14, 0))
        inner_added = ctk.CTkFrame(card_added, fg_color="transparent")
        inner_added.pack(fill="both", expand=True, padx=18, pady=14)

        hdr_added = ctk.CTkFrame(inner_added, fg_color="transparent")
        hdr_added.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(hdr_added, text="✨  Completed Posts (Downloads Added)", font=FONT_SUBHEAD, text_color="#4ade80").pack(side="left")
        self._dl_added_cnt_lbl = ctk.CTkLabel(hdr_added, text="(0 titles)", font=FONT_SMALL, text_color="#86efac")
        self._dl_added_cnt_lbl.pack(side="left", padx=(8, 0))
        ctk.CTkButton(hdr_added, text="Clear List", font=FONT_SMALL, fg_color="#163c29", hover_color="#225239", text_color="#86efac", height=28, width=80, command=self._clear_dl_added_list).pack(side="right")

        self._dl_added_box = ctk.CTkTextbox(inner_added, height=130, fg_color=C_SURFACE, text_color="#86efac", font=FONT_MONO, border_color="#1b5e3a", border_width=1, corner_radius=8)
        self._dl_added_box.pack(fill="both", expand=True)

        # ── Execution Console ─────────────────────────────────────────────────
        card_log = ctk.CTkFrame(p, fg_color=C_CARD, corner_radius=12, border_width=1, border_color=C_BORDER)
        card_log.pack(fill="x", padx=24, pady=(14, 20))
        inner_log = ctk.CTkFrame(card_log, fg_color="transparent")
        inner_log.pack(fill="both", expand=True, padx=18, pady=16)

        hdr_log = ctk.CTkFrame(inner_log, fg_color="transparent")
        hdr_log.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(hdr_log, text="Execution Console", font=FONT_SUBHEAD, text_color=C_TEXT).pack(side="left")
        ctk.CTkButton(hdr_log, text="Clear Log", font=FONT_SMALL, fg_color=C_SURFACE, hover_color=C_CARD2, height=28, width=80, command=lambda: self._dl_log_box.delete("1.0", "end")).pack(side="right")

        self._dl_log_box = ctk.CTkTextbox(inner_log, height=240, fg_color=C_SURFACE, text_color=C_TEXT, font=FONT_MONO, border_color=C_BORDER2, border_width=1, corner_radius=8)
        self._dl_log_box.pack(fill="both", expand=True)


    def _clear_dl_added_list(self):
        """Clear the Added Downloads list and reset count."""
        self._dl_added_count = 0
        if hasattr(self, "_dl_added_cnt_lbl"):
            self._dl_added_cnt_lbl.configure(text="(0 titles)")
        if hasattr(self, "_dl_added_box"):
            self._dl_added_box.delete("1.0", "end")

    def _log_dl_added(self, msg: str):
        """Record a title that successfully had download links added/updated."""
        def _append():
            if hasattr(self, "_dl_added_box"):
                if not hasattr(self, "_dl_added_count"):
                    self._dl_added_count = 0
                self._dl_added_count += 1
                if hasattr(self, "_dl_added_cnt_lbl"):
                    self._dl_added_cnt_lbl.configure(text=f"({self._dl_added_count} title{'s' if self._dl_added_count != 1 else ''})")
                from datetime import datetime
                ts = datetime.now().strftime("%H:%M:%S")
                line = f"[{self._dl_added_count}] [{ts}] ✅ {msg}\n"
                self._dl_added_box.insert("end", line)
                self._dl_added_box.see("end")
        self.after(0, _append)


    def _log_dl(self, msg: str):
        def _append():
            if hasattr(self, "_dl_log_box"):
                msg_str = str(msg)
                color = C_TEXT
                lower_msg = msg_str.lower()
                if "error" in lower_msg or "[x]" in lower_msg or "fail" in lower_msg or "exception" in lower_msg:
                    color = C_ERROR     # Bright Red (#FF3344)
                elif "warn" in lower_msg or "[!]" in lower_msg or "skipping" in lower_msg or "missing" in lower_msg:
                    color = C_WARN      # Bright Orange (#FFA500)
                elif "[ok]" in lower_msg or "success" in lower_msg or "completed" in lower_msg or "[+]" in lower_msg or "connected" in lower_msg:
                    color = C_SUCCESS   # Vivid Green (#00FF7F)
                elif "---" in lower_msg or "===" in lower_msg or "processing" in lower_msg:
                    color = C_CYAN      # Electric Cyan (#00FFFF)
                elif "[colab]" in lower_msg or "scraping" in lower_msg or "testing" in lower_msg:
                    color = C_INFO      # Deep Sky Blue (#00BFFF)
                
                try:
                    tk_text = getattr(self._dl_log_box, "_textbox", self._dl_log_box)
                    tk_text.configure(state="normal")
                    start_idx = tk_text.index("end-1c")
                    tk_text.insert("end", msg_str + "\n")
                    end_idx = tk_text.index("end-1c")
                    
                    tag = f"color_{color.replace('#', '')}"
                    tk_text.tag_config(tag, foreground=color)
                    tk_text.tag_add(tag, start_idx, end_idx)
                    
                    tk_text.see("end")
                    tk_text.configure(state="disabled")
                except Exception:
                    pass
        self.after(0, _append)

    def _test_colab_connection(self):
        colab_url = self._dl_colab_var.get().strip()
        if not colab_url:
            messagebox.showwarning("No URL", "Please enter your Colab Server URL first.\n\nGet it by running Colab_Upload_Server.ipynb in Google Colab.", parent=self)
            return

        def _do_test():
            self._log_dl(f"[COLAB] Testing connection to: {colab_url}")
            try:
                from bollyflix_uploader import ping_colab_server
                ok, msg = ping_colab_server(colab_url)
                self._log_dl(f"[COLAB] {'OK' if ok else 'FAIL'}: {msg}")
                if ok:
                    self.after(0, lambda: messagebox.showinfo("Connected!", f"Colab server is online!\n\n{msg}\n\nYou can now use the Download Adder.", parent=self))
                else:
                    self.after(0, lambda: messagebox.showerror("Connection Failed", f"Could not reach Colab server.\n\n{msg}\n\nMake sure Cell 2 is running in your Colab notebook.", parent=self))
            except Exception as e:
                self._log_dl(f"[COLAB] Connection test error: {e}")
                self.after(0, lambda: messagebox.showerror("Error", str(e), parent=self))

        threading.Thread(target=_do_test, daemon=True).start()

    def _start_auto_download_adder(self):
        api = self._get_wp_api()
        if not api:
            return
        cat_url = self._dl_cat_var.get().strip()
        if not cat_url:
            messagebox.showwarning("Missing URL", "Please enter a valid BollyFlix category URL.", parent=self)
            return

        self._dl_stop_event.clear()
        self._dl_skip_event.clear()
        self._dl_auto_btn.configure(state="disabled")
        self._dl_auto_skip_btn.configure(state="normal")
        self._dl_auto_stop_btn.configure(state="normal")

        # Attach skip_event to stop_event object so workers can check it
        self._dl_stop_event._skip_event = self._dl_skip_event

        def _worker():
            self._log_dl("=== Starting Auto Download Adder Sync ===")
            colab_url = self._dl_colab_var.get().strip()
            if not colab_url:
                self._log_dl("[WARN] No Colab Server URL set. Downloads will use YOUR bandwidth. Set one to use Google's servers.")
            else:
                self._log_dl(f"[COLAB] Using Colab server: {colab_url}")
            try:
                force = self._dl_force_var.get()
                airing = self._dl_airing_var.get()
                inc_1080p = self._dl_1080p_var.get()
                check_dead = self._dl_check_dead_var.get()
                try:
                    workers = int(self._dl_workers_var.get())
                except Exception:
                    workers = 4

                # If "Update Airing Posts" is checked, load AIRING posts directly from WordPress site (WP-First)
                if airing and not force:
                    from bollyflix_airing_updater import sync_airing_downloads_from_wp
                    sync_airing_downloads_from_wp(
                        api,
                        log_fn=self._log_dl,
                        on_added_fn=self._log_dl_added,
                        stop_event=self._dl_stop_event,
                        colab_url=colab_url,
                        max_workers=workers,
                        pixeldrain_api_key=self.settings.get("pixeldrain_api_key", ""),
                        settings=self._effective_settings(),
                        include_1080p=inc_1080p,
                        check_dead_links=check_dead,
                    )
                else:
                    # Category scan mode
                    source_choice = getattr(self, '_dl_source_var', None)
                    source_name = source_choice.get() if source_choice else "BollyFlix"
                    is_tw = "toonworld" in cat_url.lower() or source_name == "ToonWorld4All"

                    season_choice = self._dl_season_var.get().strip()
                    global_target_season = season_choice if season_choice.lower() not in ("auto", "none") else None

                    src_choice = getattr(self, '_dl_src_season_var', None)
                    global_src_season = src_choice.get().strip() if src_choice else None
                    if global_src_season and global_src_season.lower() in ("auto", "none", "all"):
                        global_src_season = None

                    if is_tw:
                        from toonworld4all_uploader import scrape_toonworld_category_posts, process_toonworld_post_and_update_wp
                        posts = scrape_toonworld_category_posts(cat_url, max_pages=2, log_fn=self._log_dl)
                        if not posts:
                            self._log_dl("[!] No posts found in ToonWorld4All category.")
                            return

                        for idx, url in enumerate(posts, 1):
                            if self._dl_stop_event.is_set():
                                self._log_dl("[!] Stop requested by user.")
                                break
                            if self._dl_skip_event.is_set():
                                self._dl_skip_event.clear()
                                self._log_dl(f"[⏩] Skipped post: {url}")
                                continue

                            self._log_dl(f"\n--- [{idx}/{len(posts)}] Processing ToonWorld post: {url} ---")
                            process_toonworld_post_and_update_wp(
                                url, api,
                                log_fn=self._log_dl,
                                on_added_fn=self._log_dl_added,
                                force_update=force,
                                update_airing=airing,
                                colab_url=colab_url,
                                max_workers=workers,
                                target_wp_season=global_target_season,
                                target_tw_season=global_src_season,
                                pixeldrain_api_key=self.settings.get("pixeldrain_api_key", ""),
                                include_1080p=inc_1080p,
                                check_dead_links=check_dead,
                                tw4all_cookie=self.settings.get("tw4all_cookie", ""),
                            )
                    else:
                        from bollyflix_uploader import scrape_bollyflix_category_posts, process_bollyflix_post_and_update_wp
                        posts = scrape_bollyflix_category_posts(cat_url, max_pages=2, log_fn=self._log_dl)
                        if not posts:
                            self._log_dl("[!] No posts found in category.")
                            return

                        for idx, url in enumerate(posts, 1):
                            if self._dl_stop_event.is_set():
                                self._log_dl("[!] Stop requested by user.")
                                break
                            if self._dl_skip_event.is_set():
                                self._dl_skip_event.clear()
                                self._log_dl(f"[⏩] Skipped post: {url}")
                                continue

                            self._log_dl(f"\n--- [{idx}/{len(posts)}] Processing post: {url} ---")
                            process_bollyflix_post_and_update_wp(
                                url, api,
                                log_fn=self._log_dl,
                                on_added_fn=self._log_dl_added,
                                force_update=force,
                                update_airing=airing,
                                colab_url=colab_url,
                                max_workers=workers,
                                target_wp_season=global_target_season,
                                target_bf_season=global_src_season,
                                pixeldrain_api_key=self.settings.get("pixeldrain_api_key", ""),
                                include_1080p=inc_1080p,
                                check_dead_links=check_dead,
                            )

                self._log_dl("\n🎉 Auto Download Sync Completed!")
            except Exception as e:
                self._log_dl(f"Error during auto download sync: {e}")
            finally:
                self.after(0, lambda: self._dl_auto_btn.configure(state="normal"))
                self.after(0, lambda: self._dl_auto_skip_btn.configure(state="disabled"))
                self.after(0, lambda: self._dl_auto_stop_btn.configure(state="disabled"))

        threading.Thread(target=_worker, daemon=True).start()

    def _skip_dl_post(self):
        """Signal worker to skip current post in Download Adder."""
        self._dl_skip_event.set()
        self._log_dl("[⏩] Skip requested — skipping after current step...")

    def _start_manual_download_adder(self):
        api = self._get_wp_api()
        if not api:
            return
        raw_text = self._dl_manual_url_box.get("1.0", "end").strip()

        # Parse each line: URL  or  URL | WP_SEASON  or  URL | WP_SEASON | BF_SEASON
        url_entries = []
        for line in raw_text.splitlines():
            line = line.strip()
            if not line or not line.startswith('http'):
                continue
            parts = [p.strip() for p in line.split('|')]
            entry_url = parts[0].strip()
            target_wp_season = None
            target_bf_season = None
            if len(parts) >= 2 and parts[1].strip():
                p1 = parts[1].strip()
                if p1.lower() not in ("auto", "none"):
                    target_wp_season = p1
            if len(parts) >= 3 and parts[2].strip():
                p2 = parts[2].strip()
                if p2.lower() not in ("auto", "none", "all"):
                    target_bf_season = p2

            # Fallback to manual default entries if not specified on the line
            if target_wp_season is None and hasattr(self, '_dl_man_wp_season_var'):
                def_wp = self._dl_man_wp_season_var.get().strip()
                if def_wp and def_wp.lower() not in ("auto", "none"):
                    target_wp_season = def_wp

            if target_bf_season is None and hasattr(self, '_dl_man_src_season_var'):
                def_src = self._dl_man_src_season_var.get().strip()
                if def_src and def_src.lower() not in ("auto", "none", "all"):
                    target_bf_season = def_src

            url_entries.append((entry_url, target_wp_season, target_bf_season))

        if not url_entries:
            messagebox.showwarning("Missing URLs", "Please enter at least one post URL (BollyFlix or MoviesMod).", parent=self)
            return

        self._dl_stop_event.clear()
        self._dl_skip_event.clear()
        self._dl_manual_btn.configure(state="disabled")
        self._dl_manual_skip_btn.configure(state="normal")
        self._dl_manual_stop_btn.configure(state="normal")

        def _worker():
            self._log_dl(f"=== Starting Manual Batch Download Adder ({len(url_entries)} URLs) ===")
            colab_url = self._dl_colab_var.get().strip()
            if not colab_url:
                self._log_dl("[WARN] No Colab Server URL set. Downloads will use YOUR bandwidth.")
            else:
                self._log_dl(f"[COLAB] Using Colab server: {colab_url}")
            try:
                from bollyflix_uploader import process_bollyflix_post_and_update_wp
                from moviesmod_uploader import process_moviesmod_post_and_update_wp
                try:
                    workers = int(self._dl_workers_var.get())
                except Exception:
                    workers = 4
                airing = self._dl_airing_var.get()
                inc_1080p = self._dl_1080p_var.get()

                season_choice = self._dl_season_var.get().strip()
                global_target_season = season_choice if season_choice.lower() not in ("auto", "none") else None

                src_choice = getattr(self, '_dl_src_season_var', None)
                global_src_season = src_choice.get().strip() if src_choice else None
                if global_src_season and global_src_season.lower() in ("auto", "none", "all"):
                    global_src_season = None

                for idx, (url, wp_season, src_season) in enumerate(url_entries, 1):
                    if self._dl_stop_event.is_set():
                        self._log_dl("[!] Stop requested by user.")
                        break
                    if self._dl_skip_event.is_set():
                        self._dl_skip_event.clear()
                        self._log_dl(f"[⏩] Skipped manual URL: {url}")
                        continue

                    eff_wp_season = wp_season if wp_season is not None else global_target_season
                    eff_src_season = src_season if src_season is not None else global_src_season

                    season_note = ""
                    if eff_wp_season is not None:
                        season_note += f" [WP Season {eff_wp_season}]"
                    if eff_src_season is not None:
                        season_note += f" [Source Season {eff_src_season}]"

                    is_tw = any(k in url.lower() for k in ['toonworld4all', 'archive.toonworld4all'])
                    is_mm = any(k in url.lower() for k in ['moviesmod', 'moviesleech', 'modpro', 'driveseed'])

                    if is_tw:
                        from toonworld4all_uploader import process_toonworld_post_and_update_wp
                        self._log_dl(f"\n--- [{idx}/{len(url_entries)}] Processing ToonWorld4All URL{season_note}: {url} ---")
                        process_toonworld_post_and_update_wp(
                            url, api,
                            log_fn=self._log_dl,
                            on_added_fn=self._log_dl_added,
                            force_update=True,
                            update_airing=airing,
                            colab_url=colab_url,
                            max_workers=workers,
                            target_wp_season=eff_wp_season,
                            target_tw_season=eff_src_season,
                            pixeldrain_api_key=self.settings.get("pixeldrain_api_key", ""),
                            include_1080p=inc_1080p,
                            check_dead_links=self._dl_check_dead_var.get(),
                            tw4all_cookie=self.settings.get("tw4all_cookie", ""),
                        )
                    elif is_mm:
                        self._log_dl(f"\n--- [{idx}/{len(url_entries)}] Processing MoviesMod URL{season_note}: {url} ---")
                        process_moviesmod_post_and_update_wp(
                            url, api,
                            log_fn=self._log_dl,
                            on_added_fn=self._log_dl_added,
                            force_update=True,
                            update_airing=airing,
                            colab_url=colab_url,
                            max_workers=workers,
                            target_wp_season=eff_wp_season,
                            target_mm_season=eff_src_season,
                            pixeldrain_api_key=self.settings.get("pixeldrain_api_key", ""),
                            include_1080p=inc_1080p,
                            check_dead_links=self._dl_check_dead_var.get(),
                        )
                    else:
                        self._log_dl(f"\n--- [{idx}/{len(url_entries)}] Processing BollyFlix URL{season_note}: {url} ---")
                        process_bollyflix_post_and_update_wp(
                            url, api,
                            log_fn=self._log_dl,
                            on_added_fn=self._log_dl_added,
                            force_update=True,
                            update_airing=airing,
                            colab_url=colab_url,
                            max_workers=workers,
                            target_wp_season=eff_wp_season,
                            target_bf_season=eff_src_season,
                            pixeldrain_api_key=self.settings.get("pixeldrain_api_key", ""),
                            include_1080p=inc_1080p,
                            check_dead_links=self._dl_check_dead_var.get(),
                        )

                self._log_dl("\n🎉 Manual Batch Download Adder Completed!")
            except Exception as e:
                self._log_dl(f"Error: {e}")
            finally:
                self.after(0, lambda: self._dl_manual_btn.configure(state="normal"))
                self.after(0, lambda: self._dl_manual_skip_btn.configure(state="disabled"))
                self.after(0, lambda: self._dl_manual_stop_btn.configure(state="disabled"))

        threading.Thread(target=_worker, daemon=True).start()

    # ══════════════════════════════════════════════════════════════════════
    #  PAGE 3: LINK EXTRACTOR ONLY
    # ══════════════════════════════════════════════════════════════════════

    def _build_link_extractor_page(self, parent: ctk.CTkFrame):
        scroll = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=0, pady=0)
        p = scroll

        hdr = ctk.CTkFrame(p, fg_color="transparent")
        hdr.pack(fill="x", padx=24, pady=(20, 0))
        ctk.CTkLabel(hdr, text="Extract Episode Links Only", font=FONT_HEAD, text_color=C_TEXT).pack(side="left")
        ctk.CTkLabel(hdr, text="Extract raw stream URLs directly without creating WP posts", font=FONT_SMALL, text_color=C_MUTED).pack(side="left", padx=(12, 0), pady=(4, 0))

        card = ctk.CTkFrame(p, fg_color=C_CARD, corner_radius=12, border_width=1, border_color=C_BORDER)
        card.pack(fill="x", padx=24, pady=(14, 0))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=18, pady=16)

        # Show / Season / Total eps
        inputs_row = ctk.CTkFrame(inner, fg_color="transparent")
        inputs_row.pack(fill="x", pady=(0, 10))

        sname_box = ctk.CTkFrame(inputs_row, fg_color="transparent")
        sname_box.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkLabel(sname_box, text="Show Title:", font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w")
        self._ext_show_var = tk.StringVar()
        ctk.CTkEntry(sname_box, textvariable=self._ext_show_var, font=FONT_BODY, placeholder_text="e.g. Captain Tsubasa", fg_color=C_SURFACE, border_color=C_BORDER2, text_color=C_TEXT, height=36).pack(fill="x", pady=(2, 0))

        seas_box = ctk.CTkFrame(inputs_row, fg_color="transparent")
        seas_box.pack(side="left", padx=(6, 6))
        ctk.CTkLabel(seas_box, text="Season:", font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w")
        self._ext_season_var = tk.StringVar(value="1")
        ctk.CTkEntry(seas_box, textvariable=self._ext_season_var, font=FONT_BODY, width=60, height=36, fg_color=C_SURFACE, border_color=C_BORDER2, text_color=C_TEXT).pack(pady=(2, 0))

        start_box = ctk.CTkFrame(inputs_row, fg_color="transparent")
        start_box.pack(side="left", padx=(6, 4))
        ctk.CTkLabel(start_box, text="Start Ep:", font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w")
        self._ext_start_ep_var = tk.StringVar(value="1")
        ctk.CTkEntry(start_box, textvariable=self._ext_start_ep_var, font=FONT_BODY, width=65, height=36, fg_color=C_SURFACE, border_color=C_BORDER2, text_color=C_TEXT).pack(pady=(2, 0))

        end_box = ctk.CTkFrame(inputs_row, fg_color="transparent")
        end_box.pack(side="left", padx=(0, 0))
        ctk.CTkLabel(end_box, text="End Ep:", font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w")
        self._ext_end_ep_var = tk.StringVar(value="12")
        ctk.CTkEntry(end_box, textvariable=self._ext_end_ep_var, font=FONT_BODY, width=65, height=36, fg_color=C_SURFACE, border_color=C_BORDER2, text_color=C_TEXT).pack(pady=(2, 0))

        # Direct URLs - Row 1 (Toonstream, Multimovies, Satoru.one)
        urls_row1 = ctk.CTkFrame(inner, fg_color="transparent")
        urls_row1.pack(fill="x", pady=(0, 6))

        ts_ext_box = ctk.CTkFrame(urls_row1, fg_color="transparent")
        ts_ext_box.pack(side="left", fill="x", expand=True, padx=(0, 4))
        ctk.CTkLabel(ts_ext_box, text="Toonstream URL (Optional):", font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w")
        self._ext_ts_url_var = tk.StringVar()
        ctk.CTkEntry(ts_ext_box, textvariable=self._ext_ts_url_var, font=FONT_MONO, placeholder_text="https://toon-stream.site/series/...", fg_color=C_SURFACE, border_color=C_BORDER2, text_color=C_TEXT, height=34).pack(fill="x", pady=(2, 0))

        mm_ext_box = ctk.CTkFrame(urls_row1, fg_color="transparent")
        mm_ext_box.pack(side="left", fill="x", expand=True, padx=(4, 4))
        ctk.CTkLabel(mm_ext_box, text="Multimovies URL (Optional):", font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w")
        self._ext_mm_url_var = tk.StringVar()
        ctk.CTkEntry(mm_ext_box, textvariable=self._ext_mm_url_var, font=FONT_MONO, placeholder_text="https://m1.multimovies.courses/series/...", fg_color=C_SURFACE, border_color=C_BORDER2, text_color=C_TEXT, height=34).pack(fill="x", pady=(2, 0))

        sat_ext_box = ctk.CTkFrame(urls_row1, fg_color="transparent")
        sat_ext_box.pack(side="left", fill="x", expand=True, padx=(4, 0))
        ctk.CTkLabel(sat_ext_box, text="Satoru.one URL (Optional):", font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w")
        self._ext_sat_url_var = tk.StringVar()
        ctk.CTkEntry(sat_ext_box, textvariable=self._ext_sat_url_var, font=FONT_MONO, placeholder_text="https://satoru.one/watch/...", fg_color=C_SURFACE, border_color=C_BORDER2, text_color=C_TEXT, height=34).pack(fill="x", pady=(2, 0))

        # Direct URLs - Row 2 (BlisseyHusband, AnimeDekho, AniHosts / KotoTV)
        urls_row2 = ctk.CTkFrame(inner, fg_color="transparent")
        urls_row2.pack(fill="x", pady=(0, 6))

        bh_ext_box = ctk.CTkFrame(urls_row2, fg_color="transparent")
        bh_ext_box.pack(side="left", fill="x", expand=True, padx=(0, 4))
        ctk.CTkLabel(bh_ext_box, text="BlisseyHusband URL (Optional):", font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w")
        self._ext_bh_url_var = tk.StringVar()
        ctk.CTkEntry(bh_ext_box, textvariable=self._ext_bh_url_var, font=FONT_MONO, placeholder_text="https://blisseyhusband.net/series/...", fg_color=C_SURFACE, border_color=C_BORDER2, text_color=C_TEXT, height=34).pack(fill="x", pady=(2, 0))

        ad_ext_box = ctk.CTkFrame(urls_row2, fg_color="transparent")
        ad_ext_box.pack(side="left", fill="x", expand=True, padx=(4, 4))
        ctk.CTkLabel(ad_ext_box, text="AnimeDekho URL (Optional):", font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w")
        self._ext_ad_url_var = tk.StringVar()
        ctk.CTkEntry(ad_ext_box, textvariable=self._ext_ad_url_var, font=FONT_MONO, placeholder_text="https://animedekho.tv/series-hindi/...", fg_color=C_SURFACE, border_color=C_BORDER2, text_color=C_TEXT, height=34).pack(fill="x", pady=(2, 0))

        an_ext_box = ctk.CTkFrame(urls_row2, fg_color="transparent")
        an_ext_box.pack(side="left", fill="x", expand=True, padx=(4, 0))
        ctk.CTkLabel(an_ext_box, text="AniHosts / KotoTV URL (Optional):", font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w")
        self._ext_an_url_var = tk.StringVar()
        ctk.CTkEntry(an_ext_box, textvariable=self._ext_an_url_var, font=FONT_MONO, placeholder_text="https://anihosts.xyz/anime/... or https://kototv.to/anime/...", fg_color=C_SURFACE, border_color=C_BORDER2, text_color=C_TEXT, height=34).pack(fill="x", pady=(2, 0))

        # Direct URLs - Row 3 (DoraBash / Byse & Download / Hubcloud URL)
        urls_row3 = ctk.CTkFrame(inner, fg_color="transparent")
        urls_row3.pack(fill="x", pady=(0, 10))

        db_ext_box = ctk.CTkFrame(urls_row3, fg_color="transparent")
        db_ext_box.pack(side="left", fill="x", expand=True, padx=(0, 4))
        ctk.CTkLabel(db_ext_box, text="DoraBash / Byse URL (Optional):", font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w")
        self._ext_db_url_var = tk.StringVar()
        ctk.CTkEntry(db_ext_box, textvariable=self._ext_db_url_var, font=FONT_MONO, placeholder_text="https://dorabash.in/... or https://bysevepoin.com/e/...", fg_color=C_SURFACE, border_color=C_BORDER2, text_color=C_TEXT, height=34).pack(fill="x", pady=(2, 0))

        dl_ext_box = ctk.CTkFrame(urls_row3, fg_color="transparent")
        dl_ext_box.pack(side="left", fill="x", expand=True, padx=(4, 0))
        ctk.CTkLabel(dl_ext_box, text="Download / Hubcloud URL (Optional):", font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w")
        self._ext_dl_url_var = tk.StringVar()
        ctk.CTkEntry(dl_ext_box, textvariable=self._ext_dl_url_var, font=FONT_MONO, placeholder_text="https://m4ulinks.site/... or https://mdrive.lol/...", fg_color=C_SURFACE, border_color=C_BORDER2, text_color=C_TEXT, height=34).pack(fill="x", pady=(2, 0))

        # --- Dedicated AniHosts / KotoTV Options Panel ---
        an_panel = ctk.CTkFrame(inner, fg_color=C_SURFACE, corner_radius=10, border_width=1, border_color=C_BORDER2)
        an_panel.pack(fill="x", pady=(0, 12))
        an_panel_inner = ctk.CTkFrame(an_panel, fg_color="transparent")
        an_panel_inner.pack(fill="x", padx=14, pady=10)

        an_top = ctk.CTkFrame(an_panel_inner, fg_color="transparent")
        an_top.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(an_top, text="🎬 AniHosts / KotoTV Chooser (Server AH / KT)", font=("Segoe UI", 12, "bold"), text_color=C_TEXT).pack(side="left")
        ctk.CTkLabel(an_top, text="Choose language (Sub / Dub) and preferred server:", font=FONT_SMALL, text_color=C_MUTED).pack(side="left", padx=(10, 0))

        an_controls = ctk.CTkFrame(an_panel_inner, fg_color="transparent")
        an_controls.pack(fill="x")

        # Sub/Dub version
        an_v_box = ctk.CTkFrame(an_controls, fg_color="transparent")
        an_v_box.pack(side="left", padx=(0, 20))
        ctk.CTkLabel(an_v_box, text="Language:", font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w", pady=(0, 2))

        self._ext_an_lang_var = tk.StringVar(value=self.settings.get("anihosts_language", self.settings.get("kototv_language", "sub")))
        _kt_v_map = {"SUB (Soft Sub)": "sub", "DUB": "dub", "Both SUB+DUB": "all"}
        _kt_v_rev = {v: k for k, v in _kt_v_map.items()}
        _curr_v_disp = _kt_v_rev.get(self.settings.get("anihosts_language", self.settings.get("kototv_language", "sub")), "SUB (Soft Sub)")

        def _on_an_v_select(choice):
            self._ext_an_lang_var.set(_kt_v_map.get(choice, "sub"))

        an_v_menu = ctk.CTkOptionMenu(
            an_v_box,
            values=["SUB (Soft Sub)", "DUB", "Both SUB+DUB"],
            command=_on_an_v_select,
            width=180, height=34,
            fg_color=C_CARD, button_color=C_ACCENT, button_hover_color="#5a54c8",
            dropdown_fg_color=C_CARD, text_color=C_TEXT, font=FONT_BODY,
        )
        an_v_menu.set(_curr_v_disp)
        an_v_menu.pack(anchor="w")

        # Server chooser
        an_s_box = ctk.CTkFrame(an_controls, fg_color="transparent")
        an_s_box.pack(side="left")
        ctk.CTkLabel(an_s_box, text="Host Server:", font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w", pady=(0, 2))

        self._ext_an_server_var = tk.StringVar(value=self.settings.get("anihosts_preferred_server", self.settings.get("kototv_preferred_server", "Server 1 (Kaze)")))
        an_s_menu = ctk.CTkOptionMenu(
            an_s_box,
            values=["Server 1 (Kaze)", "Server 2 (Edge)", "Server 3 (Megaplay)", "All Servers"],
            variable=self._ext_an_server_var,
            width=180, height=34,
            fg_color=C_CARD, button_color=C_ACCENT, button_hover_color="#5a54c8",
            dropdown_fg_color=C_CARD, text_color=C_TEXT, font=FONT_BODY,
        )
        an_s_menu.pack(anchor="w")

        # --- Responsive Server Selection Card ---
        mode_card = ctk.CTkFrame(inner, fg_color=C_SURFACE, corner_radius=10, border_width=1, border_color=C_BORDER2)
        mode_card.pack(fill="x", pady=(0, 12))
        mode_card_inner = ctk.CTkFrame(mode_card, fg_color="transparent")
        mode_card_inner.pack(fill="x", padx=14, pady=10)

        mode_header = ctk.CTkFrame(mode_card_inner, fg_color="transparent")
        mode_header.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(mode_header, text="🌐 Stream Server Source:", font=("Segoe UI", 12, "bold"), text_color=C_TEXT).pack(side="left")
        ctk.CTkLabel(mode_header, text="(Select specific server to extract, or 'All Servers')", font=FONT_SMALL, text_color=C_MUTED).pack(side="left", padx=(10, 0))

        self._ext_source_mode_var = tk.StringVar(value="all")

        # Responsive grid container for server radio buttons
        server_grid = ctk.CTkFrame(mode_card_inner, fg_color="transparent")
        server_grid.pack(fill="x", expand=True)

        server_opts = [
            ("All Servers", "all"),
            ("Server DB (DoraBash / Byse)", "byse"),
            ("Server BH (BlisseyHusband)", "blissey"),
            ("Server UG (Toonstream)", "toonstream"),
            ("Server UG2 (Multimovies)", "multimovies"),
            ("Server MG (Satoru)", "satoru"),
            ("Server AH (AniHosts)", "anihosts"),
            ("Server KT (KotoTV)", "kototv"),
            ("Vidmoly (AnimeDekho)", "vidmoly"),
            ("Server UG (AnimeDekho→TS)", "animedekho"),
        ]

        server_radios = []
        for text, val in server_opts:
            rb = ctk.CTkRadioButton(
                server_grid,
                text=text,
                variable=self._ext_source_mode_var,
                value=val,
                font=FONT_BODY,
                fg_color=C_ACCENT,
            )
            server_radios.append(rb)

        # Responsive layout without configure recursion:
        # Calculates column count based on container width (4, 3, or 2 columns)
        _last_grid_cols = [0]

        def _reflow_server_radios(event=None):
            if event and event.widget != server_grid:
                return
            w = event.width if event else server_grid.winfo_width()
            if w <= 100:
                target_cols = 4  # Default sensible columns
            else:
                target_cols = 4 if w >= 820 else (3 if w >= 600 else 2)

            if target_cols == _last_grid_cols[0]:
                return
            _last_grid_cols[0] = target_cols

            for idx, r_btn in enumerate(server_radios):
                r_btn.grid(row=idx // target_cols, column=idx % target_cols, padx=(0, 14), pady=5, sticky="w")

        server_grid.bind("<Configure>", _reflow_server_radios)
        _reflow_server_radios()

        # Output Box
        ctk.CTkLabel(inner, text="Extracted Stream Links:", font=FONT_BODY, text_color=C_MUTED).pack(anchor="w", pady=(0, 4))
        self._ext_box = ctk.CTkTextbox(inner, height=160, fg_color=C_SURFACE, text_color=C_TEXT, font=FONT_MONO, border_color=C_BORDER2, border_width=1, corner_radius=8, wrap="none")
        self._ext_box.pack(fill="x", pady=(0, 12))

        # Action Buttons
        ext_btn_row = ctk.CTkFrame(inner, fg_color="transparent")
        ext_btn_row.pack(fill="x")
        self._ext_extract_btn = ctk.CTkButton(ext_btn_row, text="Extract Links", font=("Segoe UI", 12, "bold"), fg_color=C_ACCENT, hover_color="#5a54c8", height=38, corner_radius=8, width=160, command=self._start_extract_links_only)
        self._ext_extract_btn.pack(side="left", padx=(0, 10))
        self._ext_copy_btn = ctk.CTkButton(ext_btn_row, text="Copy Links Only", font=("Segoe UI", 12, "bold"), fg_color="#5b21b6", hover_color="#6d28d9", height=38, corner_radius=8, width=180, command=self._copy_extracted_links_only)
        self._ext_copy_btn.pack(side="left", padx=(0, 10))
        ctk.CTkButton(ext_btn_row, text="Clear Box", font=FONT_BODY, fg_color=C_SURFACE, hover_color=C_CARD2, height=38, corner_radius=8, width=90, command=lambda: self._ext_box.delete("1.0", "end")).pack(side="left")


    # ══════════════════════════════════════════════════════════════════════
    #  PAGE: DEDICATED TOONWORLD4ALL HUBCLOUD EXTRACTOR
    # ══════════════════════════════════════════════════════════════════════

    def _build_tw4all_extractor_page(self, parent: ctk.CTkFrame):
        scroll = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=0, pady=0)
        p = scroll

        # Header Card
        hdr = ctk.CTkFrame(p, fg_color=C_CARD, corner_radius=12, border_width=1, border_color=C_BORDER)
        hdr.pack(fill="x", padx=24, pady=(20, 10))
        inner_hdr = ctk.CTkFrame(hdr, fg_color="transparent")
        inner_hdr.pack(fill="x", padx=20, pady=16)

        ctk.CTkLabel(inner_hdr, text="ToonWorld4All Hubcloud Extractor", font=FONT_HEAD, text_color=C_TEXT).pack(anchor="w")
        ctk.CTkLabel(
            inner_hdr,
            text="Dedicated extractor for ToonWorld4All show posts and archive episode pages.",
            font=FONT_BODY, text_color=C_MUTED
        ).pack(anchor="w", pady=(2, 0))

        # Main Input Card
        card = ctk.CTkFrame(p, fg_color=C_CARD, corner_radius=12, border_width=1, border_color=C_BORDER)
        card.pack(fill="x", padx=24, pady=10)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=20, pady=16)

        # URL Input
        ctk.CTkLabel(inner, text="ToonWorld4All Post or Archive Episode URL:", font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w")
        self._tw_url_var = tk.StringVar()
        ctk.CTkEntry(
            inner,
            textvariable=self._tw_url_var,
            font=FONT_MONO,
            placeholder_text="e.g. https://toonworld4all.me/tomb-raider-king-season-1-multi-audio-hindi/ or https://archive.toonworld4all.me/episode/...",
            fg_color=C_SURFACE,
            border_color=C_BORDER2,
            text_color=C_TEXT,
            height=38,
        ).pack(fill="x", pady=(4, 10))

        # Season
        row_s = ctk.CTkFrame(inner, fg_color="transparent")
        row_s.pack(fill="x", pady=(0, 12))
        s_box = ctk.CTkFrame(row_s, fg_color="transparent")
        s_box.pack(side="left")
        ctk.CTkLabel(s_box, text="Season Filter:", font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w")
        self._tw_season_var = tk.StringVar(value="1")
        ctk.CTkEntry(s_box, textvariable=self._tw_season_var, font=FONT_BODY, width=80, height=34, fg_color=C_SURFACE, border_color=C_BORDER2, text_color=C_TEXT).pack(pady=(2, 0))

        # Output Box
        ctk.CTkLabel(inner, text="Extracted Live Hubcloud Links:", font=FONT_BODY, text_color=C_MUTED).pack(anchor="w", pady=(8, 4))
        self._tw_box = ctk.CTkTextbox(
            inner,
            height=260,
            font=FONT_MONO,
            fg_color=C_SURFACE,
            border_color=C_BORDER2,
            border_width=1,
            text_color=C_TEXT,
            corner_radius=8,
            wrap="none"
        )
        self._tw_box.pack(fill="x", pady=(0, 12))

        # Action Buttons
        btn_row = ctk.CTkFrame(inner, fg_color="transparent")
        btn_row.pack(fill="x")

        self._tw_extract_btn = ctk.CTkButton(
            btn_row,
            text="⚡ Extract ToonWorld4All Links",
            font=("Segoe UI", 12, "bold"),
            fg_color=C_ACCENT,
            hover_color="#5a54c8",
            height=38,
            corner_radius=8,
            width=220,
            command=self._start_tw4all_extraction
        )
        self._tw_extract_btn.pack(side="left", padx=(0, 10))

        ctk.CTkButton(
            btn_row,
            text="📋 Copy Extracted Links",
            font=("Segoe UI", 12, "bold"),
            fg_color="#5b21b6",
            hover_color="#6d28d9",
            height=38,
            corner_radius=8,
            width=180,
            command=self._copy_tw4all_links
        ).pack(side="left", padx=(0, 10))

        ctk.CTkButton(
            btn_row,
            text="🧹 Clear Box",
            font=FONT_BODY,
            fg_color=C_SURFACE,
            hover_color=C_CARD2,
            height=38,
            corner_radius=8,
            width=90,
            command=lambda: self._tw_box.delete("1.0", "end")
        ).pack(side="left")

    def _start_tw4all_extraction(self):
        url = self._tw_url_var.get().strip()
        if not url:
            messagebox.showwarning("Missing URL", "Please paste a ToonWorld4All URL first.")
            return

        try:
            season = int(self._tw_season_var.get().strip() or 1)
        except ValueError:
            season = 1

        self._tw_extract_btn.configure(state="disabled", text="⏳ Extracting...")
        self._tw_box.delete("1.0", "end")
        self._tw_box.insert("end", f"# Extracting ToonWorld4All Hubcloud links from {url} [Season {season}]...\n\n")

        threading.Thread(
            target=self._tw4all_extract_worker,
            args=(url, season),
            daemon=True
        ).start()

    def _tw4all_extract_worker(self, url: str, season: int):
        try:
            from stream_extractor import extract_hubcloud_from_toonworld4all
            self._log(f"📥 Extracting ToonWorld4All links from {url}...", "info")
            ep_map = extract_hubcloud_from_toonworld4all(url, target_season=season)

            lines = []
            if ep_map:
                for ep_num in sorted(ep_map.keys()):
                    h_link = ep_map[ep_num]
                    lines.append(f"S{season}E{ep_num} [DL-Hubcloud]: {h_link}")

                self._log(f"✅ Extracted {len(ep_map)} ToonWorld4All link(s).", "success")
            else:
                lines.append("# No links found or failed to extract from page.")
                self._log("⚠️ No links extracted from ToonWorld4All URL.", "warn")

            out_text = "\n".join(lines)
            self.after(0, lambda: self._tw_box.delete("1.0", "end"))
            self.after(0, lambda: self._tw_box.insert("end", out_text))
        except Exception as exc:
            logger.exception("ToonWorld4All extraction failed")
            self._log(f"💥 Error extracting ToonWorld4All links: {exc}", "error")
        finally:
            self.after(0, lambda: self._tw_extract_btn.configure(state="normal", text="⚡ Extract ToonWorld4All Links"))

    def _copy_tw4all_links(self):
        txt = self._tw_box.get("1.0", "end").strip()
        if not txt:
            messagebox.showinfo("Empty Box", "Nothing to copy!")
            return
        self.clipboard_clear()
        self.clipboard_append(txt)
        messagebox.showinfo("Copied!", f"Copied ToonWorld4All links to clipboard ({len(txt.splitlines())} lines).")


    # ══════════════════════════════════════════════════════════════════════
    #  PAGE: FIX STREAMS (BATCH AS-CDN LINK MIGRATOR)
    # ══════════════════════════════════════════════════════════════════════

    def _build_fix_streams_page(self, parent: ctk.CTkFrame):
        scroll = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=0, pady=0)
        p = scroll

        hdr = ctk.CTkFrame(p, fg_color="transparent")
        hdr.pack(fill="x", padx=24, pady=(20, 0))
        ctk.CTkLabel(hdr, text="Fix & Upgrade Stream Links (AS-CDN)", font=FONT_HEAD, text_color=C_TEXT).pack(side="left")
        ctk.CTkLabel(hdr, text="Scan WordPress posts and upgrade legacy/dead Ruby stream links to AS-CDN", font=FONT_SMALL, text_color=C_MUTED).pack(side="left", padx=(12, 0), pady=(4, 0))

        card = ctk.CTkFrame(p, fg_color=C_CARD, corner_radius=12, border_width=1, border_color=C_BORDER)
        card.pack(fill="x", padx=24, pady=(14, 0))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=18, pady=16)

        ctk.CTkLabel(
            inner,
            text="This tool automatically scans all published and draft posts on your WordPress site.\nIt detects any Server UG links containing legacy 'rubyvidhub' or 'rubystm' embed URLs, extracts the fresh 'AS-CDN' stream URL from Toonstream, and updates the posts on WordPress automatically without changing the post date.",
            font=FONT_BODY,
            text_color=C_TEXT,
            justify="left",
        ).pack(anchor="w", pady=(0, 14))

        btn_row = ctk.CTkFrame(inner, fg_color="transparent")
        btn_row.pack(anchor="w", pady=(0, 4))

        self._fix_streams_start_btn = ctk.CTkButton(
            btn_row,
            text="🚀 Start Batch Stream Migration",
            font=FONT_BODY,
            fg_color=C_ACCENT,
            hover_color="#5a54c8",
            height=38,
            width=230,
            corner_radius=8,
            command=self._start_fix_streams_migration,
        )
        self._fix_streams_start_btn.pack(side="left", padx=(0, 10))

        self._fix_streams_stop_btn = ctk.CTkButton(
            btn_row,
            text="⏹ Stop",
            font=FONT_BODY,
            fg_color=C_ERROR,
            hover_color="#c0392b",
            height=38,
            width=100,
            corner_radius=8,
            state="disabled",
            command=self._stop_fix_streams_migration,
        )
        self._fix_streams_stop_btn.pack(side="left")

        # Log box
        log_hdr = ctk.CTkFrame(p, fg_color="transparent")
        log_hdr.pack(fill="x", padx=24, pady=(16, 4))
        ctk.CTkLabel(log_hdr, text="Migration Progress Log:", font=("Segoe UI", 12, "bold"), text_color=C_TEXT).pack(side="left")

        self._fix_streams_log_box = ctk.CTkTextbox(
            p,
            height=340,
            fg_color=C_SURFACE,
            border_color=C_BORDER2,
            border_width=1,
            text_color=C_TEXT,
            font=("Consolas", 11),
        )
        self._fix_streams_log_box.pack(fill="both", expand=True, padx=24, pady=(0, 20))

        # ── Blogspot Link Scanner card ─────────────────────────────────────────
        blog_card = ctk.CTkFrame(p, fg_color=C_CARD, corner_radius=12, border_width=1, border_color=C_BORDER)
        blog_card.pack(fill="x", padx=24, pady=(16, 0))
        blog_inner = ctk.CTkFrame(blog_card, fg_color="transparent")
        blog_inner.pack(fill="x", padx=18, pady=16)

        blog_hdr = ctk.CTkFrame(blog_inner, fg_color="transparent")
        blog_hdr.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(blog_hdr, text="Blogspot Link Scanner", font=("Segoe UI", 14, "bold"), text_color=C_TEXT).pack(side="left")
        ctk.CTkLabel(blog_hdr, text="bestembestfriend1.blogspot.com", font=FONT_SMALL, text_color=C_MUTED).pack(side="left", padx=(10, 0), pady=(3, 0))

        ctk.CTkLabel(
            blog_inner,
            text="Scans all WordPress posts and finds every post that contains a 'bestembestfriend1.blogspot.com' stream link,\nshowing the post title, ID and exact number of links per post.",
            font=FONT_BODY,
            text_color=C_MUTED,
            justify="left",
        ).pack(anchor="w", pady=(0, 12))

        # Summary bar
        self._blogspot_summary_lbl = ctk.CTkLabel(
            blog_inner,
            text="Posts with blogspot links: —    Total blogspot links: —",
            font=("Segoe UI", 12, "bold"),
            text_color=C_ACCENT2,
        )
        self._blogspot_summary_lbl.pack(anchor="w", pady=(0, 10))

        # Buttons row
        blog_btn_row = ctk.CTkFrame(blog_inner, fg_color="transparent")
        blog_btn_row.pack(anchor="w", pady=(0, 10))

        self._blogspot_scan_btn = ctk.CTkButton(
            blog_btn_row,
            text="🔍 Scan All Posts",
            font=("Segoe UI", 12, "bold"),
            fg_color="#0f3460",
            hover_color="#164080",
            height=38,
            width=180,
            corner_radius=8,
            command=self._start_scan_blogspot,
        )
        self._blogspot_scan_btn.pack(side="left", padx=(0, 10))

        ctk.CTkButton(
            blog_btn_row,
            text="Clear",
            font=FONT_BODY,
            fg_color=C_SURFACE,
            hover_color=C_CARD2,
            height=38,
            width=80,
            corner_radius=8,
            command=lambda: (
                self._blogspot_log_box.delete("1.0", "end"),
                self._blogspot_summary_lbl.configure(
                    text="Posts with blogspot links: —    Total blogspot links: —"
                ),
            ),
        ).pack(side="left")

        # Results log box
        self._blogspot_log_box = ctk.CTkTextbox(
            blog_inner,
            height=260,
            fg_color=C_SURFACE,
            border_color=C_BORDER2,
            border_width=1,
            text_color=C_TEXT,
            font=("Consolas", 11),
        )
        self._blogspot_log_box.pack(fill="x", pady=(0, 4))

        # -- Blogger Zephyr Migrator card --
        self._build_blogger_zephyr_card(p)

        # -- Auto Add Server MG (satoru.one) card --
        self._build_satoru_mg_card(p)

        # -- AS-CDN Domain Batch Replacer card --
        self._build_as_cdn_domain_card(p)

        # -- Restore Original Post Dates card --
        self._build_restore_dates_card(p)

    def _log_blogspot(self, msg: str):
        def _append():
            if hasattr(self, "_blogspot_log_box") and self._blogspot_log_box:
                self._blogspot_log_box.insert("end", msg + "\n")
                self._blogspot_log_box.see("end")
        self.after(0, _append)

    def _start_scan_blogspot(self):
        api = self._get_wp_api()
        if not api:
            return
        self._blogspot_scan_btn.configure(state="disabled")
        self._blogspot_log_box.delete("1.0", "end")
        self._blogspot_summary_lbl.configure(text="Scanning... please wait")

        def _worker():
            import re as _re
            BLOGSPOT_PATTERN = _re.compile(r'bestembestfriend1\.blogspot\.com', _re.IGNORECASE)
            try:
                self._log_blogspot("Fetching all posts from WordPress...")
                all_posts = []
                for status in ("publish", "draft", "pending", "private"):
                    page = 1
                    while True:
                        try:
                            resp = api.session.get(
                                f"{api.base}/posts",
                                params={
                                    "per_page": 100,
                                    "page": page,
                                    "status": status,
                                    "context": "edit",   # returns raw content
                                    "_fields": "id,title,content",
                                },
                                timeout=30,
                            )
                            batch = resp.json()
                            if not batch or not isinstance(batch, list):
                                break
                            all_posts.extend(batch)
                            if len(batch) < 100:
                                break
                            page += 1
                        except Exception as exc:
                            self._log_blogspot(f"  Warning: failed fetching {status} page {page}: {exc}")
                            break

                self._log_blogspot(f"Fetched {len(all_posts)} posts total. Searching for blogspot links...\n")

                posts_with_links = []
                total_links = 0

                for post in all_posts:
                    content = (
                        post.get("content", {}).get("raw", "")
                        or post.get("content", {}).get("rendered", "")
                    )
                    count = len(BLOGSPOT_PATTERN.findall(content))
                    if count > 0:
                        total_links += count
                        title = (
                            post.get("title", {}).get("raw", "")
                            or post.get("title", {}).get("rendered", "")
                            or f"Post #{post.get('id', '?')}"
                        )
                        posts_with_links.append((post.get("id"), title, count))

                if not posts_with_links:
                    self._log_blogspot("No posts found with bestembestfriend1.blogspot.com links.")
                else:
                    self._log_blogspot(f"{'ID':<8}  {'Links':>5}  Post Title")
                    self._log_blogspot(f"{'-'*8}  {'-'*5}  {'-'*60}")
                    for pid, title, count in sorted(posts_with_links, key=lambda x: -x[2]):
                        self._log_blogspot(f"#{pid:<7}  {count:>4}x   {title}")
                    self._log_blogspot(f"\n{'='*72}")
                    self._log_blogspot(f"  Posts with blogspot links  : {len(posts_with_links)}")
                    self._log_blogspot(f"  Total blogspot links found : {total_links}")
                    self._log_blogspot(f"{'='*72}")

                self.after(0, lambda lc=len(posts_with_links), tl=total_links: self._blogspot_summary_lbl.configure(
                    text=f"Posts with blogspot links: {lc}    Total blogspot links: {tl}"
                ))
            except Exception as exc:
                self._log_blogspot(f"Error during scan: {exc}")
                self.after(0, lambda: self._blogspot_summary_lbl.configure(text="Scan failed — see log"))
            finally:
                self.after(0, lambda: self._blogspot_scan_btn.configure(state="normal"))

        threading.Thread(target=_worker, daemon=True).start()

    # -- Blogger Zephyr Migrator card (built inline from _build_fix_streams_page) --
    def _build_blogger_zephyr_card(self, parent):
        """Add the Blogger Zephyr migrator card to the Fix Streams scroll frame."""
        card = ctk.CTkFrame(parent, fg_color=C_CARD, corner_radius=12, border_width=1, border_color=C_BORDER)
        card.pack(fill="x", padx=24, pady=(16, 20))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=18, pady=16)

        hdr = ctk.CTkFrame(inner, fg_color="transparent")
        hdr.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(hdr, text="Blogger Zephyr Link Migrator", font=("Segoe UI", 14, "bold"), text_color=C_TEXT).pack(side="left")
        ctk.CTkLabel(hdr, text="play.zephyrflick.top -> AS-CDN", font=FONT_SMALL, text_color=C_MUTED).pack(side="left", padx=(10, 0), pady=(3, 0))

        ctk.CTkLabel(
            inner,
            text="Scans all posts on bestembestfriend1.blogspot.com, finds every play.zephyrflick.top iframe,\nextracts the AS-CDN stream URL from Toonstream / AnimeSalt.me, and updates the post automatically.",
            font=FONT_BODY,
            text_color=C_MUTED,
            justify="left",
        ).pack(anchor="w", pady=(0, 12))

        btn_row = ctk.CTkFrame(inner, fg_color="transparent")
        btn_row.pack(anchor="w", pady=(0, 10))

        self._zephyr_start_btn = ctk.CTkButton(
            btn_row,
            text="Start Blogger Migration",
            font=("Segoe UI", 12, "bold"),
            fg_color=C_ACCENT,
            hover_color="#5a54c8",
            height=38, width=220, corner_radius=8,
            command=self._start_blogger_zephyr_migration,
        )
        self._zephyr_start_btn.pack(side="left", padx=(0, 10))

        self._zephyr_dry_btn = ctk.CTkButton(
            btn_row,
            text="Dry Run (no changes)",
            font=FONT_BODY,
            fg_color="#5b21b6",
            hover_color="#6d28d9",
            height=38, width=180, corner_radius=8,
            command=lambda: self._start_blogger_zephyr_migration(dry_run=True),
        )
        self._zephyr_dry_btn.pack(side="left", padx=(0, 10))

        self._zephyr_stop_btn = ctk.CTkButton(
            btn_row,
            text="Stop",
            font=FONT_BODY,
            fg_color=C_ERROR,
            hover_color="#c0392b",
            height=38, width=80, corner_radius=8,
            state="disabled",
            command=self._stop_blogger_zephyr_migration,
        )
        self._zephyr_stop_btn.pack(side="left")

        self._zephyr_log_box = ctk.CTkTextbox(
            inner,
            height=260,
            fg_color=C_SURFACE,
            border_color=C_BORDER2,
            border_width=1,
            text_color=C_TEXT,
            font=("Consolas", 11),
        )
        self._zephyr_log_box.pack(fill="x", pady=(0, 4))

    def _log_blogger_zephyr(self, msg: str):
        def _append():
            if hasattr(self, "_zephyr_log_box") and self._zephyr_log_box:
                self._zephyr_log_box.insert("end", msg + "\n")
                self._zephyr_log_box.see("end")
        self.after(0, _append)

    def _start_blogger_zephyr_migration(self, dry_run: bool = False):
        if not hasattr(self, "_zephyr_log_box"):
            return
        self._zephyr_stop_flag = [False]
        self._zephyr_start_btn.configure(state="disabled")
        self._zephyr_dry_btn.configure(state="disabled")
        self._zephyr_stop_btn.configure(state="normal")
        self._zephyr_log_box.delete("1.0", "end")

        def _worker():
            try:
                import batch_replace_blogger_zephyr as _brb
                svc = _brb.get_blogger_service()
                _brb.batch_replace_blogger_zephyr(
                    service=svc,
                    log_fn=self._log_blogger_zephyr,
                    stop_flag=self._zephyr_stop_flag,
                    dry_run=dry_run,
                )
            except SystemExit:
                self._log_blogger_zephyr("Setup required: place blogger_oauth_client.json next to auto_poster.py and try again.")
            except Exception as exc:
                self._log_blogger_zephyr(f"Error: {exc}")
            finally:
                self.after(0, lambda: self._zephyr_start_btn.configure(state="normal"))
                self.after(0, lambda: self._zephyr_dry_btn.configure(state="normal"))
                self.after(0, lambda: self._zephyr_stop_btn.configure(state="disabled"))

        threading.Thread(target=_worker, daemon=True).start()

    def _stop_blogger_zephyr_migration(self):
        if hasattr(self, "_zephyr_stop_flag"):
            self._zephyr_stop_flag[0] = True
            self._log_blogger_zephyr("Stopping Blogger migration...")

    # -- Auto Add Server MG (satoru.one) card & worker methods --
    def _build_satoru_mg_card(self, parent):
        """Add the Auto Add Server MG card to the Fix Streams scroll frame."""
        card = ctk.CTkFrame(parent, fg_color=C_CARD, corner_radius=12, border_width=1, border_color=C_BORDER)
        card.pack(fill="x", padx=24, pady=(16, 20))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=18, pady=16)

        hdr = ctk.CTkFrame(inner, fg_color="transparent")
        hdr.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(hdr, text="Auto Add Server MG (satoru.one)", font=("Segoe UI", 14, "bold"), text_color=C_TEXT).pack(side="left")
        ctk.CTkLabel(hdr, text="Adds missing Server MG • Preserves existing MG URLs", font=FONT_SMALL, text_color=C_MUTED).pack(side="left", padx=(10, 0), pady=(3, 0))

        ctk.CTkLabel(
            inner,
            text="Scans published WordPress posts for missing Server MG links.\nIf an episode already has a Server MG link (even a custom, encrypted, or different URL), it is strictly PRESERVED and never replaced.\nOnly missing Server MG links are fetched from satoru.one and merged into the post.",
            font=FONT_BODY,
            text_color=C_MUTED,
            justify="left",
        ).pack(anchor="w", pady=(0, 12))

        btn_row = ctk.CTkFrame(inner, fg_color="transparent")
        btn_row.pack(anchor="w", pady=(0, 10))

        self._mg_start_btn = ctk.CTkButton(
            btn_row,
            text="Start Adding Server MG",
            font=("Segoe UI", 12, "bold"),
            fg_color=C_ACCENT,
            hover_color="#5a54c8",
            height=38, width=200, corner_radius=8,
            command=lambda: self._start_auto_add_satoru_mg(dry_run=False),
        )
        self._mg_start_btn.pack(side="left", padx=(0, 10))

        self._mg_dry_btn = ctk.CTkButton(
            btn_row,
            text="Dry Run (Scan Only)",
            font=("Segoe UI", 12, "bold"),
            fg_color=C_SURFACE,
            hover_color=C_CARD2,
            height=38, width=160, corner_radius=8,
            command=lambda: self._start_auto_add_satoru_mg(dry_run=True),
        )
        self._mg_dry_btn.pack(side="left", padx=(0, 10))

        self._mg_stop_btn = ctk.CTkButton(
            btn_row,
            text="Stop",
            font=("Segoe UI", 12, "bold"),
            fg_color="#7a2020",
            hover_color="#a02828",
            height=38, width=80, corner_radius=8,
            state="disabled",
            command=self._stop_satoru_mg_worker,
        )
        self._mg_stop_btn.pack(side="left", padx=(0, 10))

        ctk.CTkButton(
            btn_row,
            text="Clear",
            font=FONT_SMALL,
            fg_color=C_SURFACE,
            hover_color=C_CARD2,
            height=38, width=70, corner_radius=8,
            command=lambda: (
                self._mg_log_box.delete("1.0", "end"),
                self._mg_summary_lbl.configure(text="Posts checked: —    Posts updated: —"),
            ),
        ).pack(side="left")

        self._mg_summary_lbl = ctk.CTkLabel(
            inner,
            text="Posts checked: —    Posts updated: —",
            font=FONT_SUB,
            text_color=C_CYAN,
        )
        self._mg_summary_lbl.pack(anchor="w", pady=(0, 8))

        self._mg_log_box = ctk.CTkTextbox(
            inner,
            height=260,
            fg_color=C_SURFACE,
            border_color=C_BORDER2,
            border_width=1,
            text_color=C_TEXT,
            font=("Consolas", 11),
        )
        self._mg_log_box.pack(fill="x", pady=(0, 4))

    def _log_satoru_mg(self, msg: str):
        def _append():
            if hasattr(self, "_mg_log_box") and self._mg_log_box:
                self._mg_log_box.insert("end", msg + "\n")
                self._mg_log_box.see("end")
        self.after(0, _append)

    def _stop_satoru_mg_worker(self):
        if hasattr(self, "_mg_stop_flag"):
            self._mg_stop_flag[0] = True
            self._log_satoru_mg("Stopping Server MG auto-add worker...")

    def _start_auto_add_satoru_mg(self, dry_run: bool = False):
        api = self._get_wp_api()
        if not api:
            return

        self._mg_stop_flag = [False]
        self._mg_start_btn.configure(state="disabled")
        self._mg_dry_btn.configure(state="disabled")
        self._mg_stop_btn.configure(state="normal")
        self._mg_log_box.delete("1.0", "end")

        mode_str = "[DRY RUN / SCAN ONLY]" if dry_run else "[LIVE UPDATE]"
        self._log_satoru_mg(f"{'='*72}\n  Starting Server MG Auto-Add Scanner {mode_str}\n{'='*72}\n")

        def _worker():
            from stream_extractor import resolve_satoru_episode, generate_episode_toggle_html
            from wp_api import parse_existing_post_streams, parse_existing_post_downloads

            total_checked = 0
            total_updated = 0

            try:
                self._log_satoru_mg("Fetching published posts from WordPress...")
                all_posts = api.get_all_published_posts()
                self._log_satoru_mg(f"Found {len(all_posts)} published post(s) to scan.\n")

                for idx, post in enumerate(all_posts, 1):
                    if self._mg_stop_flag[0]:
                        self._log_satoru_mg("Stopped by user.")
                        break

                    total_checked += 1
                    post_id   = post["id"]
                    show_name = post["show_name"]
                    season    = post["season"]
                    content   = post["content"]

                    self._log_satoru_mg(f"[{idx}/{len(all_posts)}] Scanning: {show_name} (Season {season}) [ID {post_id}]...")
                    existing_streams = parse_existing_post_streams(content)

                    if not existing_streams:
                        self._log_satoru_mg("  - No episode stream toggles found in post content.")
                        continue

                    all_ep_nums = sorted(existing_streams.keys())
                    episodes_data = []
                    post_has_changes = False

                    # Check which episodes need Server MG
                    for ep_num in all_ep_nums:
                        if self._mg_stop_flag[0]:
                            break

                        ex = existing_streams[ep_num]
                        ruby_url  = ex.get("ruby_url") or None
                        multi_url = ex.get("multi_url") or None
                        existing_mg = ex.get("mg_url") or None
                        has_mg = ex.get("has_mg", False)

                        if has_mg or existing_mg:
                            # SAFETY RULE: Existing Server MG present -> KEEP UNTOUCHED!
                            mg_url = existing_mg
                            self._log_satoru_mg(f"  - Ep {ep_num:02d}: Already has Server MG -> PRESERVED ({existing_mg})")
                        else:
                            # Missing Server MG -> Resolve from satoru.one
                            self._log_satoru_mg(f"  - Ep {ep_num:02d}: Missing Server MG -> Fetching from satoru.one...")
                            mg_url = resolve_satoru_episode(show_name, season=season, episode=ep_num)
                            if mg_url:
                                self._log_satoru_mg(f"    ✅ Ep {ep_num:02d}: Resolved Server MG -> {mg_url}")
                                post_has_changes = True
                            else:
                                self._log_satoru_mg(f"    ⚠️ Ep {ep_num:02d}: Server MG not found on satoru.one")

                        episodes_data.append({
                            "episode_num": ep_num,
                            "ruby_url":    ruby_url,
                            "ag_url":      ex.get("ag_url", ""),
                            "multi_url":   multi_url,
                            "mg_url":      mg_url,
                            "pg_url":      ex.get("pg_url", ""),
                            "downloads":   {},
                        })

                    if post_has_changes:
                        total_updated += 1
                        if dry_run:
                            self._log_satoru_mg(f"  [DRY RUN] Would update post {post_id} with new Server MG streams.\n")
                        else:
                            self._log_satoru_mg(f"  Updating post {post_id} on WordPress...")
                            existing_dls = parse_existing_post_downloads(content)
                            for ep_item in episodes_data:
                                ep_num = ep_item["episode_num"]
                                if ep_num in existing_dls:
                                    ep_item["downloads"] = dict(existing_dls[ep_num])

                            toggle_html = generate_episode_toggle_html(episodes_data, series_name=show_name, season_num=season)
                            ok = api.patch_post_with_streams(post_id, toggle_html, update_date=False)
                            if ok:
                                self._log_satoru_mg(f"  ✅ Post {post_id} successfully updated on WordPress!\n")
                            else:
                                self._log_satoru_mg(f"  ❌ Failed to update post {post_id} on WordPress.\n")
                    else:
                        self._log_satoru_mg("  ✔ All existing episodes already have Server MG or no new streams found.\n")

                    self.after(0, lambda tc=total_checked, tu=total_updated: self._mg_summary_lbl.configure(
                        text=f"Posts checked: {tc}    Posts updated: {tu}"
                    ))

                self._log_satoru_mg(f"\n{'='*72}")
                self._log_satoru_mg(f"  Finished: {total_checked} posts checked, {total_updated} posts updated.")
                self._log_satoru_mg(f"{'='*72}")

            except Exception as exc:
                self._log_satoru_mg(f"Error during scan: {exc}")
            finally:
                self.after(0, lambda: self._mg_start_btn.configure(state="normal"))
                self.after(0, lambda: self._mg_dry_btn.configure(state="normal"))
                self.after(0, lambda: self._mg_stop_btn.configure(state="disabled"))

        threading.Thread(target=_worker, daemon=True).start()

    # -- AS-CDN Domain Replacer card & worker methods --
    def _build_as_cdn_domain_card(self, parent):
        """Add the AS-CDN Domain Batch Replacer card to the Fix Streams scroll frame."""
        card = ctk.CTkFrame(parent, fg_color=C_CARD, corner_radius=12, border_width=1, border_color=C_BORDER)
        card.pack(fill="x", padx=24, pady=(16, 20))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=18, pady=16)

        hdr = ctk.CTkFrame(inner, fg_color="transparent")
        hdr.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(hdr, text="Batch Replace Stream Server Domain (vexal.top -> ravok.buzz)", font=("Segoe UI", 14, "bold"), text_color=C_TEXT).pack(side="left")
        ctk.CTkLabel(hdr, text="Decodes encrypted URLs (?data=...) & updates stream domains across all posts", font=FONT_SMALL, text_color=C_MUTED).pack(side="left", padx=(10, 0), pady=(3, 0))

        ctk.CTkLabel(
            inner,
            text="Scans all published and draft WordPress posts, decodes base64 encrypted stream link payloads (?data=...),\nand automatically updates old stream domains (e.g. vexal.top or as-cdn26.top) to the new domain (ravok.buzz).\nYou can run this anytime whenever server domains change!",
            font=FONT_BODY,
            text_color=C_MUTED,
            justify="left",
        ).pack(anchor="w", pady=(0, 12))

        inp_row = ctk.CTkFrame(inner, fg_color="transparent")
        inp_row.pack(anchor="w", pady=(0, 12))

        ctk.CTkLabel(inp_row, text="Old Domain / Pattern:", font=FONT_SMALL, text_color=C_TEXT).pack(side="left", padx=(0, 6))
        self._as_cdn_old_entry = ctk.CTkEntry(inp_row, width=160, font=FONT_BODY, placeholder_text="vexal.top")
        self._as_cdn_old_entry.insert(0, "vexal.top")
        self._as_cdn_old_entry.pack(side="left", padx=(0, 16))

        ctk.CTkLabel(inp_row, text="New Target Domain:", font=FONT_SMALL, text_color=C_TEXT).pack(side="left", padx=(0, 6))
        self._as_cdn_new_entry = ctk.CTkEntry(inp_row, width=160, font=FONT_BODY, placeholder_text="ravok.buzz")
        self._as_cdn_new_entry.insert(0, "ravok.buzz")
        self._as_cdn_new_entry.pack(side="left")

        btn_row = ctk.CTkFrame(inner, fg_color="transparent")
        btn_row.pack(anchor="w", pady=(0, 4))

        self._as_cdn_start_btn = ctk.CTkButton(
            btn_row,
            text="🚀 Migrate Domain Across All Posts",
            font=("Segoe UI", 12, "bold"),
            fg_color=C_ACCENT,
            hover_color="#5a54c8",
            height=38, width=240, corner_radius=8,
            command=self._start_as_cdn_domain_migration,
        )
        self._as_cdn_start_btn.pack(side="left", padx=(0, 10))

        self._as_cdn_stop_btn = ctk.CTkButton(
            btn_row,
            text="⏹ Stop",
            font=("Segoe UI", 12, "bold"),
            fg_color=C_ERROR,
            hover_color="#c0392b",
            height=38, width=90, corner_radius=8,
            state="disabled",
            command=self._stop_as_cdn_domain_migration,
        )
        self._as_cdn_stop_btn.pack(side="left")

    def _start_as_cdn_domain_migration(self):
        api = self._get_wp_api()
        if not api:
            return

        old_d = self._as_cdn_old_entry.get().strip() if hasattr(self, "_as_cdn_old_entry") else "as-cdn26.top"
        new_d = self._as_cdn_new_entry.get().strip() if hasattr(self, "_as_cdn_new_entry") else "vexal.top"

        if not old_d or not new_d:
            self._log_fix_streams("⚠️ Old Domain and New Domain must not be empty!")
            return

        self._as_cdn_stop_flag = [False]
        self._as_cdn_start_btn.configure(state="disabled")
        self._as_cdn_stop_btn.configure(state="normal")
        self._fix_streams_log_box.delete("1.0", "end")

        def _worker():
            try:
                import batch_replace_as_cdn_domain
                batch_replace_as_cdn_domain.batch_replace_as_cdn_domain(
                    api,
                    old_domain=old_d,
                    new_domain=new_d,
                    log_fn=self._log_fix_streams,
                    stop_flag=self._as_cdn_stop_flag,
                )
            except Exception as exc:
                self._log_fix_streams(f"Error during domain replacement: {exc}")
            finally:
                self.after(0, lambda: self._as_cdn_start_btn.configure(state="normal"))
                self.after(0, lambda: self._as_cdn_stop_btn.configure(state="disabled"))

        threading.Thread(target=_worker, daemon=True).start()

    def _stop_as_cdn_domain_migration(self):
        if hasattr(self, "_as_cdn_stop_flag"):
            self._as_cdn_stop_flag[0] = True
            self._log_fix_streams("Stopping AS-CDN domain migration...")

    # -- Restore Original Post Dates card & worker methods --
    def _build_restore_dates_card(self, parent):
        """Add the Restore Original Post Dates card to the Fix Streams scroll frame."""
        card = ctk.CTkFrame(parent, fg_color=C_CARD, corner_radius=12, border_width=1, border_color=C_BORDER)
        card.pack(fill="x", padx=24, pady=(16, 20))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=18, pady=16)

        hdr = ctk.CTkFrame(inner, fg_color="transparent")
        hdr.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(hdr, text="🕒 Restore Original Post Published Dates", font=("Segoe UI", 14, "bold"), text_color=C_TEXT).pack(side="left")
        ctk.CTkLabel(hdr, text="Undo accidental date changes using WordPress revision history", font=FONT_SMALL, text_color=C_MUTED).pack(side="left", padx=(10, 0), pady=(3, 0))

        ctk.CTkLabel(
            inner,
            text="If a bulk update accidentally changed your post published dates, this tool restores\neach post's original date by reading from WordPress's built-in revision history.",
            font=FONT_BODY,
            text_color=C_MUTED,
            justify="left",
        ).pack(anchor="w", pady=(0, 12))

        date_row = ctk.CTkFrame(inner, fg_color="transparent")
        date_row.pack(anchor="w", pady=(0, 12))
        ctk.CTkLabel(date_row, text="Affected Date (posts set to this date):", font=FONT_SMALL, text_color=C_TEXT).pack(side="left", padx=(0, 8))
        self._restore_date_entry = ctk.CTkEntry(date_row, width=120, font=FONT_BODY, placeholder_text="YYYY-MM-DD")
        import datetime as _dt
        self._restore_date_entry.insert(0, _dt.date.today().strftime("%Y-%m-%d"))
        self._restore_date_entry.pack(side="left")

        btn_row = ctk.CTkFrame(inner, fg_color="transparent")
        btn_row.pack(anchor="w", pady=(0, 4))

        self._restore_dates_start_btn = ctk.CTkButton(
            btn_row,
            text="🔄 Restore All Original Dates",
            font=("Segoe UI", 12, "bold"),
            fg_color="#2e7d32",
            hover_color="#1b5e20",
            height=38, width=220, corner_radius=8,
            command=self._start_restore_dates,
        )
        self._restore_dates_start_btn.pack(side="left", padx=(0, 10))

        self._restore_dates_stop_btn = ctk.CTkButton(
            btn_row,
            text="⏹ Stop",
            font=("Segoe UI", 12, "bold"),
            fg_color=C_ERROR,
            hover_color="#c0392b",
            height=38, width=90, corner_radius=8,
            state="disabled",
            command=self._stop_restore_dates,
        )
        self._restore_dates_stop_btn.pack(side="left")

    def _start_restore_dates(self):
        api = self._get_wp_api()
        if not api:
            return

        affected_since = self._restore_date_entry.get().strip() if hasattr(self, "_restore_date_entry") else ""
        if not affected_since:
            self._log_fix_streams("⚠️ Please enter the affected date (e.g. 2026-08-22)")
            return

        self._restore_dates_stop_flag = [False]
        self._restore_dates_start_btn.configure(state="disabled")
        self._restore_dates_stop_btn.configure(state="normal")
        self._fix_streams_log_box.delete("1.0", "end")

        def _worker():
            try:
                import restore_original_post_dates
                restore_original_post_dates.restore_original_post_dates(
                    api,
                    affected_since=affected_since,
                    log_fn=self._log_fix_streams,
                    stop_flag=self._restore_dates_stop_flag,
                )
            except Exception as exc:
                self._log_fix_streams(f"Error during date restoration: {exc}")
            finally:
                self.after(0, lambda: self._restore_dates_start_btn.configure(state="normal"))
                self.after(0, lambda: self._restore_dates_stop_btn.configure(state="disabled"))

        threading.Thread(target=_worker, daemon=True).start()

    def _stop_restore_dates(self):
        if hasattr(self, "_restore_dates_stop_flag"):
            self._restore_dates_stop_flag[0] = True
            self._log_fix_streams("Stopping date restoration...")

    def _log_fix_streams(self, msg: str):
        def _append():
            if hasattr(self, "_fix_streams_log_box") and self._fix_streams_log_box:
                self._fix_streams_log_box.insert("end", msg + "\n")
                self._fix_streams_log_box.see("end")
        self.after(0, _append)

    def _start_fix_streams_migration(self):
        api = self._get_wp_api()
        if not api:
            return

        self._fix_stop_flag = [False]
        self._fix_streams_start_btn.configure(state="disabled")
        self._fix_streams_stop_btn.configure(state="normal")
        self._fix_streams_log_box.delete("1.0", "end")

        def _worker():
            try:
                import batch_replace_ruby_streams
                batch_replace_ruby_streams.batch_replace_ruby_streams(
                    api,
                    log_fn=self._log_fix_streams,
                    stop_flag=self._fix_stop_flag,
                )
            except Exception as exc:
                self._log_fix_streams(f"Error: {exc}")
            finally:
                self.after(0, lambda: self._fix_streams_start_btn.configure(state="normal"))
                self.after(0, lambda: self._fix_streams_stop_btn.configure(state="disabled"))

        threading.Thread(target=_worker, daemon=True).start()

    def _stop_fix_streams_migration(self):
        if hasattr(self, "_fix_stop_flag"):
            self._fix_stop_flag[0] = True
            self._log_fix_streams("Stopping batch migration...")


    def _build_settings_page(self, parent: ctk.CTkFrame):
        # Header
        hdr = ctk.CTkFrame(parent, fg_color="transparent")
        hdr.pack(fill="x", padx=28, pady=(26, 0))
        ctk.CTkLabel(hdr, text="Settings",
                     font=FONT_HEAD, text_color=C_TEXT).pack(side="left")
        ctk.CTkLabel(hdr, text="Configure credentials and behaviour",
                     font=FONT_SMALL, text_color=C_MUTED).pack(side="left", padx=(14, 0), pady=(6, 0))

        scroll = ctk.CTkScrollableFrame(parent, fg_color="transparent",
                                        scrollbar_button_color=C_BORDER2,
                                        scrollbar_button_hover_color=C_ACCENT)
        scroll.pack(fill="both", expand=True, padx=28, pady=(12, 16))

        self._settings_entries: list[ctk.CTkEntry] = []

        def section(title: str, icon: str = ""):
            f = ctk.CTkFrame(scroll, fg_color=C_CARD, corner_radius=12,
                             border_width=1, border_color=C_BORDER)
            f.pack(fill="x", pady=(14, 0))
            hf = ctk.CTkFrame(f, fg_color="transparent")
            hf.pack(fill="x", padx=16, pady=(12, 0))
            ctk.CTkLabel(hf, text=f"{icon}  {title}" if icon else title,
                         font=FONT_SUB, text_color=C_ACCENT2).pack(anchor="w")
            ctk.CTkFrame(f, height=1, fg_color=C_BORDER).pack(fill="x", padx=16, pady=(8, 0))
            body = ctk.CTkFrame(f, fg_color="transparent")
            body.pack(fill="x", padx=16, pady=(8, 14))
            return body

        def field(parent_frame, label: str, var: tk.StringVar,
                  placeholder: str = "", show: str = "", width: int = 460) -> ctk.CTkEntry:
            ctk.CTkLabel(parent_frame, text=label, font=FONT_BODY,
                         text_color=C_MUTED).pack(anchor="w", pady=(6, 0))
            entry = ctk.CTkEntry(
                parent_frame, textvariable=var,
                placeholder_text=placeholder, show=show,
                width=width, height=38,
                fg_color=C_SURFACE, border_color=C_BORDER2,
                text_color=C_TEXT, font=FONT_BODY,
            )
            entry.pack(anchor="w", pady=(3, 0))
            self._settings_entries.append(entry)
            return entry

        # ------ WordPress ---------------------------------------------------------------------------------------------------------------------------------------------------------------
        wp_body = section("WordPress Connection", "Œ ")
        self._wp_url = tk.StringVar(value=self.settings.get("wp_url", ""))
        field(wp_body, "Site URL", self._wp_url, "https://yoursite.com")
        self._wp_user = tk.StringVar(value=self.settings.get("wp_username", ""))
        field(wp_body, "Username", self._wp_user, "admin")
        self._wp_pass = tk.StringVar(value=self.settings.get("wp_app_password", ""))
        field(wp_body, "Application Password", self._wp_pass,
              "xxxx xxxx xxxx xxxx xxxx xxxx", show="•")
        ctk.CTkLabel(wp_body,
                     text="Generate at: WP Admin †’ Users †’ Profile †’ Application Passwords",
                     font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w", pady=(4, 8))
        test_row = ctk.CTkFrame(wp_body, fg_color="transparent")
        test_row.pack(anchor="w", pady=(0, 4))
        ctk.CTkButton(
            test_row, text="-Œ  Test Connection", font=FONT_BODY,
            fg_color=C_ACCENT, hover_color="#5a54c8",
            height=36, width=170, corner_radius=8,
            command=self._test_wp_connection,
        ).pack(side="left", padx=(0, 12))
        self._conn_status = ctk.CTkLabel(test_row, text="",
                                          font=FONT_BODY, text_color=C_MUTED)
        self._conn_status.pack(side="left")

        # ------ Pixeldrain -----------------------------------------------------------------------------------------------------------------------------------------------------
        pd_body = section("Pixeldrain API Key", "☁️")
        ctk.CTkLabel(pd_body,
                     text="Pixeldrain API Key  (used to upload episode files — update here if you changed accounts)",
                     font=FONT_BODY, text_color=C_MUTED).pack(anchor="w", pady=(0, 2))
        ctk.CTkLabel(pd_body,
                     text="Get your key at: https://pixeldrain.com/user/api",
                     font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w", pady=(0, 4))
        self._pixeldrain_key_var = tk.StringVar(value=self.settings.get("pixeldrain_api_key", ""))
        _pd_entry = ctk.CTkEntry(
            pd_body,
            textvariable=self._pixeldrain_key_var,
            placeholder_text="Paste your Pixeldrain API key here (e.g. 4104e59a-182f-...)",
            width=460, height=38,
            fg_color=C_SURFACE, border_color=C_BORDER2,
            text_color=C_TEXT, font=FONT_BODY,
        )
        _pd_entry.pack(anchor="w", pady=(3, 0))
        self._settings_entries.append(_pd_entry)

        # ------ Rating APIs ---------------------------------------------------------------------------------------------------------------------------------------------------------
        api_body = section("Rating APIs", "­ ")
        ctk.CTkLabel(api_body,
                     text="MyAnimeList (Jikan) - No API key needed. Used automatically for anime.",
                     font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w", pady=(0, 6))
        self._tmdb_key = tk.StringVar(value=self.settings.get("tmdb_api_key", ""))
        field(api_body, "TMDB API Key  (movies & cartoons)",
              self._tmdb_key, "Paste your TMDB v3 API key here", show="•")
        ctk.CTkLabel(api_body,
                     text="Get free key at: https://www.themoviedb.org/settings/api",
                     font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w", pady=(4, 0))

        # ------ Taqyeem fallback ------------------------------------------------------------------------------------------------------------------------------------------
        taq_body = section("Taqyeem Defaults", "Ž¯")
        self._default_score_var = tk.StringVar(
            value=str(self.settings.get("default_taqyeem_score", 70)))
        ctk.CTkLabel(taq_body,
                     text="Fallback score if API lookup fails (0–100)",
                     font=FONT_BODY, text_color=C_MUTED).pack(anchor="w", pady=(0, 2))
        ctk.CTkEntry(
            taq_body, textvariable=self._default_score_var, width=100, height=36,
            fg_color=C_SURFACE, border_color=C_BORDER2,
            text_color=C_TEXT, font=FONT_BODY,
        ).pack(anchor="w", pady=(0, 4))

        # ------ Advanced ------------------------------------------------------------------------------------------------------------------------------------------------------------------
        adv_body = section("Advanced", "⚙")
        self._delay_var = tk.StringVar(
            value=str(self.settings.get("delay_between_posts", 0.5)))
        ctk.CTkLabel(adv_body, text="Delay between WP post creations (seconds)",
                     font=FONT_BODY, text_color=C_MUTED).pack(anchor="w", pady=(0, 2))
        ctk.CTkEntry(
            adv_body, textvariable=self._delay_var, width=100, height=36,
            fg_color=C_SURFACE, border_color=C_BORDER2,
            text_color=C_TEXT, font=FONT_BODY,
        ).pack(anchor="w", pady=(0, 8))
        self._subtitle_key_var = tk.StringVar(
            value=self.settings.get("subtitle_meta_key", "tie_post_sub_title"))
        ctk.CTkLabel(adv_body,
                     text="Subtitle meta key  (theme post meta field for subtitles)",
                     font=FONT_BODY, text_color=C_MUTED).pack(anchor="w", pady=(4, 0))
        ctk.CTkLabel(adv_body,
                     text="TieLabs / Jannah default: tie_post_sub_title",
                     font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w", pady=(0, 2))
        ctk.CTkEntry(
            adv_body, textvariable=self._subtitle_key_var, width=220, height=36,
            fg_color=C_SURFACE, border_color=C_BORDER2,
            text_color=C_TEXT, font=FONT_BODY,
        ).pack(anchor="w", pady=(0, 4))

        # ------ Source Domains -----------------------------------------------------------------------------------------
        dom_body = section("Source Domains", "🌐")
        ctk.CTkLabel(dom_body,
                     text="Comma-separated list of domains. First entry is the primary; others are fallbacks.",
                     font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w", pady=(0, 6))

        # Server UG Primary Provider
        ctk.CTkLabel(dom_body, text="Server UG Primary Provider (ravok.buzz / as-cdn):",
                     font=FONT_BODY, text_color=C_TEXT).pack(anchor="w", pady=(4, 2))
        ctk.CTkLabel(dom_body,
                     text="Select primary source for Server UG stream. Automatically falls back if an episode is not found.",
                     font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w", pady=(0, 4))
        ug_src_row = ctk.CTkFrame(dom_body, fg_color="transparent")
        ug_src_row.pack(anchor="w", pady=(0, 8))
        self._server_ug_source_var = tk.StringVar(
            value=self.settings.get("server_ug_source", "toonstream").strip().lower()
        )
        ctk.CTkRadioButton(
            ug_src_row, text="Toonstream", variable=self._server_ug_source_var,
            value="toonstream", font=FONT_BODY, fg_color=C_ACCENT
        ).pack(side="left", padx=(0, 16))
        ctk.CTkRadioButton(
            ug_src_row, text="AnimeDekho (Earlier Updates)", variable=self._server_ug_source_var,
            value="animedekho", font=FONT_BODY, fg_color=C_ACCENT
        ).pack(side="left")

        # AnimeDekho URL
        # Cloudflare Worker Base URL
        ctk.CTkLabel(dom_body, text="Cloudflare Worker Base URL (for Byse / WatchAnimeWorld proxy):",
                     font=FONT_BODY, text_color=C_MUTED).pack(anchor="w", pady=(4, 0))
        self._worker_base_var = tk.StringVar(
            value=self.settings.get("worker_base", "https://new-frosty.toonsraja-df3.workers.dev/"))
        _wb_entry = ctk.CTkEntry(
            dom_body, textvariable=self._worker_base_var,
            placeholder_text="https://new-frosty.toonsraja-df3.workers.dev/",
            width=560, height=36,
            fg_color=C_SURFACE, border_color=C_BORDER2,
            text_color=C_TEXT, font=FONT_BODY,
        )
        _wb_entry.pack(anchor="w", pady=(3, 0))
        self._settings_entries.append(_wb_entry)

        # DoraBash Collector Worker URL
        ctk.CTkLabel(dom_body, text="DoraBash Collector Worker URL (for scraping iframes from dorabash.in):",
                     font=FONT_BODY, text_color=C_MUTED).pack(anchor="w", pady=(8, 0))
        self._dorabash_worker_url_var = tk.StringVar(
            value=self.settings.get("dorabash_worker_url", "https://new-frosty.nsr-developer78.workers.dev/"))
        _db_worker_entry = ctk.CTkEntry(
            dom_body, textvariable=self._dorabash_worker_url_var,
            placeholder_text="https://new-frosty.nsr-developer78.workers.dev/",
            width=560, height=36,
            fg_color=C_SURFACE, border_color=C_BORDER2,
            text_color=C_TEXT, font=FONT_BODY,
        )
        _db_worker_entry.pack(anchor="w", pady=(3, 0))
        self._settings_entries.append(_db_worker_entry)

        ctk.CTkLabel(dom_body, text="AnimeDekho base URL (automatically bypasses 24h shortener verification)",
                     font=FONT_BODY, text_color=C_MUTED).pack(anchor="w", pady=(8, 0))
        self._animedekho_url_var = tk.StringVar(
            value=self.settings.get("animedekho_url", "https://animedekho.tv"))
        _ad_entry = ctk.CTkEntry(
            dom_body, textvariable=self._animedekho_url_var,
            width=560, height=36,
            fg_color=C_SURFACE, border_color=C_BORDER2,
            text_color=C_TEXT, font=FONT_BODY,
        )
        _ad_entry.pack(anchor="w", pady=(3, 0))
        self._settings_entries.append(_ad_entry)

        ctk.CTkLabel(dom_body, text="Toonstream domains",
                     font=FONT_BODY, text_color=C_MUTED).pack(anchor="w", pady=(8, 0))
        self._toonstream_domains_var = tk.StringVar(
            value=self.settings.get("toonstream_domains",
                                    "toon-stream.site, toonstream.dad, toonstream.vip"))
        _ts_entry = ctk.CTkEntry(
            dom_body, textvariable=self._toonstream_domains_var,
            width=560, height=36,
            fg_color=C_SURFACE, border_color=C_BORDER2,
            text_color=C_TEXT, font=FONT_BODY,
        )
        _ts_entry.pack(anchor="w", pady=(3, 0))
        self._settings_entries.append(_ts_entry)

        ctk.CTkLabel(dom_body, text="Multimovies domains",
                     font=FONT_BODY, text_color=C_MUTED).pack(anchor="w", pady=(10, 0))
        self._multimovies_domains_var = tk.StringVar(
            value=self.settings.get("multimovies_domains",
                                    "m1.multimovies.courses, multimovies.garden, multimovies.casa"))
        _mm_entry = ctk.CTkEntry(
            dom_body, textvariable=self._multimovies_domains_var,
            width=560, height=36,
            fg_color=C_SURFACE, border_color=C_BORDER2,
            text_color=C_TEXT, font=FONT_BODY,
        )
        _mm_entry.pack(anchor="w", pady=(3, 0))
        self._settings_entries.append(_mm_entry)

        ctk.CTkLabel(dom_body, text="Primary Download Provider  (for Airing Posts & Auto Sync)",
                     font=FONT_BODY, text_color=C_MUTED).pack(anchor="w", pady=(10, 0))
        cur_provider = self.settings.get("download_provider", "bollyflix").capitalize()
        if cur_provider not in ["BollyFlix", "MoviesMod"]:
            cur_provider = "BollyFlix"
        self._download_provider_var = tk.StringVar(value=cur_provider)
        _provider_seg = ctk.CTkSegmentedButton(
            dom_body,
            values=["BollyFlix", "MoviesMod"],
            variable=self._download_provider_var,
            font=FONT_BODY,
            height=34,
            selected_color=C_ACCENT,
            selected_hover_color=C_ACCENT2
        )
        _provider_seg.pack(anchor="w", pady=(3, 0))

        ctk.CTkLabel(dom_body, text="BollyFlix base URL  (source for download links)",
                     font=FONT_BODY, text_color=C_MUTED).pack(anchor="w", pady=(10, 0))
        self._bollyflix_url_var = tk.StringVar(
            value=self.settings.get("bollyflix_url", "https://bollyflix.af"))
        _bf_entry = ctk.CTkEntry(
            dom_body, textvariable=self._bollyflix_url_var,
            width=360, height=36,
            fg_color=C_SURFACE, border_color=C_BORDER2,
            text_color=C_TEXT, font=FONT_BODY,
        )
        _bf_entry.pack(anchor="w", pady=(3, 0))
        self._settings_entries.append(_bf_entry)

        ctk.CTkFrame(dom_body, height=1, fg_color=C_BORDER2).pack(fill="x", pady=(12, 8))
        ctk.CTkLabel(dom_body,
                     text="🔄  ToonWorld4All Auto-Sync (Tampermonkey Bridge)",
                     font=(FONT_BODY[0], 12, "bold"), text_color="#38bdf8").pack(anchor="w")
        ctk.CTkLabel(dom_body,
                     text="The Tampermonkey script in your browser automatically syncs your 24h verification "
                          "session to AutoPoster via http://127.0.0.1:4545. No copy-pasting required!",
                     font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w", pady=(2, 4))

        self._tw4all_cookie_var = tk.StringVar(value=self.settings.get("tw4all_cookie", ""))
        self._tw_status_var = tk.StringVar(
            value=f"🟢 Session Active ({len(self.settings.get('tw4all_cookie', ''))} chars)" if self.settings.get("tw4all_cookie")
            else "⚪ No session synced yet — solve shortener once in browser with Tampermonkey"
        )

        _tw_status_row = ctk.CTkFrame(dom_body, fg_color="transparent")
        _tw_status_row.pack(fill="x", pady=(2, 6))

        ctk.CTkLabel(_tw_status_row, textvariable=self._tw_status_var,
                     font=FONT_SMALL, text_color="#10b981" if self.settings.get("tw4all_cookie") else "#f59e0b").pack(side="left", padx=(0, 10))

        def _refresh_tw_cookie_status():
            from tw4all_resolver import load_saved_cookie
            fresh = load_saved_cookie()
            if fresh:
                self._tw4all_cookie_var.set(fresh)
                self._tw_status_var.set(f"🟢 Session Active ({len(fresh)} chars synced)")
            else:
                self._tw_status_var.set("⚪ No session synced yet — visit ToonWorld in browser")

        ctk.CTkButton(
            _tw_status_row, text="🔄 Check Status",
            font=FONT_SMALL, fg_color="#334155", hover_color="#475569",
            height=28, width=120, corner_radius=6,
            command=_refresh_tw_cookie_status,
        ).pack(side="left")

        # Manual entry row (as backup)
        ctk.CTkLabel(dom_body, text="Manual Cookie override (optional backup):", font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w", pady=(2, 2))
        _tw_cookie_entry = ctk.CTkEntry(
            dom_body, textvariable=self._tw4all_cookie_var,
            placeholder_text="Auto-synced from browser — or paste manual cookie hex here…",
            width=560, height=34,
            fg_color=C_SURFACE, border_color=C_BORDER2,
            text_color=C_TEXT, font=FONT_SMALL,
        )
        _tw_cookie_entry.pack(anchor="w", pady=(0, 4))
        self._settings_entries.append(_tw_cookie_entry)

        ctk.CTkLabel(dom_body,
                     text="Changes apply after clicking Save Settings (no restart needed).",
                     font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w", pady=(6, 0))

        # ------ Airing Skip List -------------------------------------------------------------------------------------
        skip_body = section("Airing Posts Skip List", "⏭️")
        ctk.CTkLabel(skip_body,
                     text="Shows listed here will be automatically skipped during Auto Sync and Airing Post updates.",
                     font=FONT_BODY, text_color=C_MUTED).pack(anchor="w", pady=(0, 2))
        ctk.CTkLabel(skip_body,
                     text="Enter one show name per line (e.g. Tokyo Ghoul). Season-specific: Tokyo Ghoul S2",
                     font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w", pady=(0, 6))
        # Load existing skip list from settings
        existing_skip = self.settings.get("airing_skip_list", [])
        existing_skip_text = "\n".join(existing_skip) if existing_skip else ""
        self._airing_skip_textbox = ctk.CTkTextbox(
            skip_body,
            width=560, height=140,
            fg_color=C_SURFACE, border_color=C_BORDER2,
            text_color=C_TEXT, font=FONT_BODY,
            corner_radius=8, border_width=1,
        )
        self._airing_skip_textbox.pack(anchor="w", pady=(0, 4))
        if existing_skip_text:
            self._airing_skip_textbox.insert("1.0", existing_skip_text)
        ctk.CTkLabel(skip_body,
                     text="These posts will be logged as ⏭️ Skipped (skip list) and not touched.",
                     font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w", pady=(2, 0))

        # ------ Cloudflare Protection & Toonstream Bypass -------------------------------------------------------------------------
        cf_body = section("Cloudflare Protection & Toonstream Bypass", "🔓")
        self._cf_clearance_var = tk.StringVar(value=self.settings.get("cf_clearance", ""))
        ctk.CTkLabel(cf_body, text="Cloudflare Clearance Cookie (cf_clearance)", font=FONT_BODY, text_color=C_MUTED).pack(anchor="w", pady=(0, 2))
        ctk.CTkLabel(cf_body, text="If Toonstream is protected by Cloudflare, click the bypass button below or paste your cf_clearance cookie.", font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w", pady=(0, 4))
        
        cf_entry_row = ctk.CTkFrame(cf_body, fg_color="transparent")
        cf_entry_row.pack(fill="x", pady=(0, 8))
        _cf_entry = ctk.CTkEntry(
            cf_entry_row, textvariable=self._cf_clearance_var,
            placeholder_text="e.g. 9b.XYZ123...",
            width=380, height=36,
            fg_color=C_SURFACE, border_color=C_BORDER2,
            text_color=C_TEXT, font=FONT_BODY,
        )
        _cf_entry.pack(side="left", padx=(0, 8))
        self._settings_entries.append(_cf_entry)

        ctk.CTkButton(
            cf_entry_row, text="🔓 Auto-Bypass Cloudflare",
            font=("Segoe UI", 11, "bold"), fg_color="#10b981", hover_color="#059669",
            height=36, width=190, corner_radius=8,
            command=self._launch_cf_autobypass,
        ).pack(side="left")

        self._cf_ua_var = tk.StringVar(value=self.settings.get("cf_user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"))
        ctk.CTkLabel(cf_body, text="Matching Browser User-Agent (from the browser that solved Cloudflare):", font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w", pady=(4, 2))
        _cf_ua_entry = ctk.CTkEntry(
            cf_body, textvariable=self._cf_ua_var,
            placeholder_text="Mozilla/5.0...",
            width=580, height=34,
            fg_color=C_SURFACE, border_color=C_BORDER2,
            text_color=C_TEXT, font=FONT_BODY,
        )
        _cf_ua_entry.pack(anchor="w", pady=(0, 8))
        self._settings_entries.append(_cf_ua_entry)

        # FlareSolverr section
        ctk.CTkFrame(cf_body, height=1, fg_color=C_BORDER2).pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(cf_body, text="🚀  FlareSolverr  (Recommended — bypasses Cloudflare automatically)", font=("Segoe UI", 12, "bold"), text_color=C_TEXT).pack(anchor="w", pady=(0, 2))
        ctk.CTkLabel(cf_body, text="FlareSolverr is a free tool that runs on your PC and uses a real browser to bypass Cloudflare for Toonstream.", font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w", pady=(0, 2))
        ctk.CTkLabel(cf_body, text="Download: github.com/FlareSolverr/FlareSolverr/releases  →  Extract and run flaresolverr.exe  →  Keep it running while using AutoPoster.", font=FONT_SMALL, text_color="#f59e0b").pack(anchor="w", pady=(0, 6))

        fs_row = ctk.CTkFrame(cf_body, fg_color="transparent")
        fs_row.pack(fill="x", pady=(0, 4))
        self._flaresolverr_url_var = tk.StringVar(value=self.settings.get("flaresolverr_url", "http://localhost:8191"))
        ctk.CTkLabel(fs_row, text="FlareSolverr URL:", font=FONT_BODY, text_color=C_MUTED, width=140, anchor="w").pack(side="left")
        _fs_entry = ctk.CTkEntry(
            fs_row, textvariable=self._flaresolverr_url_var,
            placeholder_text="http://localhost:8191",
            width=300, height=36,
            fg_color=C_SURFACE, border_color=C_BORDER2,
            text_color=C_TEXT, font=FONT_BODY,
        )
        _fs_entry.pack(side="left", padx=(0, 10))
        self._settings_entries.append(_fs_entry)
        ctk.CTkButton(
            fs_row, text="🧪 Test FlareSolverr",
            font=FONT_BODY, fg_color="#6366f1", hover_color="#4f46e5",
            height=36, width=160, corner_radius=8,
            command=self._test_flaresolverr,
        ).pack(side="left")

        # WatchAnimeWorld Section
        ctk.CTkFrame(cf_body, height=1, fg_color=C_BORDER2).pack(fill="x", pady=(10, 10))
        ctk.CTkLabel(cf_body, text="📺  WatchAnimeWorld.one (Server UG Source)", font=("Segoe UI", 12, "bold"), text_color=C_TEXT).pack(anchor="w", pady=(0, 2))
        ctk.CTkLabel(cf_body, text="Extract Server UG links from WatchAnimeWorld.one and route through Frosty Cloudflare Worker.", font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w", pady=(0, 4))

        self._use_waw_var = tk.BooleanVar(value=self.settings.get("use_watchanimeworld", False))
        ctk.CTkCheckBox(
            cf_body,
            text="Use WatchAnimeWorld.one for Server UG (instead of Toonstream)",
            variable=self._use_waw_var,
            font=FONT_BODY,
            text_color=C_TEXT,
            fg_color="#6366f1", hover_color="#4f46e5",
        ).pack(anchor="w", pady=(0, 4))

        # ------ Security ------------------------------------------------------------------------------------------------------------------------------------------------------------------
        sec_body = section("Security", "🔒")
        self._settings_pwd_var = tk.StringVar(
            value=self.settings.get("settings_password", ""))
        ctk.CTkLabel(sec_body,
                     text="Settings password  (leave blank to disable protection)",
                     font=FONT_BODY, text_color=C_MUTED).pack(anchor="w", pady=(0, 2))
        ctk.CTkLabel(sec_body,
                     text="Anyone who clicks the Settings tab will be asked for this password.",
                     font=FONT_SMALL, text_color=C_MUTED).pack(anchor="w", pady=(0, 4))
        _pwd_entry = ctk.CTkEntry(
            sec_body, textvariable=self._settings_pwd_var, show="•",
            width=280, height=36,
            fg_color=C_SURFACE, border_color=C_BORDER2,
            text_color=C_TEXT, font=FONT_BODY,
        )
        _pwd_entry.pack(anchor="w", pady=(0, 4))
        self._settings_entries.append(_pwd_entry)

        # ------ Save button ---------------------------------------------------------------------------------------------------------------------------------------------------------
        save_row = ctk.CTkFrame(scroll, fg_color="transparent")
        save_row.pack(anchor="w", pady=(20, 8))
        self._save_btn = ctk.CTkButton(
            save_row, text="💾  Save Settings",
            font=("Segoe UI", 13, "bold"),
            fg_color=C_ACCENT, hover_color="#5a54c8",
            height=44, width=200, corner_radius=10,
            command=self._save_settings_ui,
        )
        self._save_btn.pack(side="left", padx=(0, 12))
        self._save_status_label = ctk.CTkLabel(
            save_row,
            text="Changes take effect immediately after saving.",
            font=FONT_SMALL, text_color=C_MUTED
        )
        self._save_status_label.pack(side="left")

    # ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
    #  RESULTS PAGE
    # ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

    def _build_results_page(self, parent: ctk.CTkFrame):
        hdr_row = ctk.CTkFrame(parent, fg_color="transparent")
        hdr_row.pack(fill="x", padx=28, pady=(26, 8))
        ctk.CTkLabel(hdr_row, text="Created Drafts",
                     font=FONT_HEAD, text_color=C_TEXT).pack(side="left")
        ctk.CTkButton(
            hdr_row, text="¬‡  Export CSV", font=FONT_BODY,
            fg_color=C_SURFACE, hover_color=C_CARD2,
            height=36, width=130, corner_radius=8,
            command=self._export_csv,
        ).pack(side="right")

        tree_frame = ctk.CTkFrame(parent, fg_color=C_CARD, corner_radius=12,
                                   border_width=1, border_color=C_BORDER)
        tree_frame.pack(fill="both", expand=True, padx=28, pady=(0, 20))

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Dark.Treeview",
                         background=C_CARD, foreground=C_TEXT,
                         fieldbackground=C_CARD, rowheight=30,
                         font=("Segoe UI", 10))
        style.configure("Dark.Treeview.Heading",
                         background=C_SURFACE, foreground=C_ACCENT2,
                         font=("Segoe UI", 10, "bold"), relief="flat")
        style.map("Dark.Treeview",
                  background=[("selected", C_ACCENT)],
                  foreground=[("selected", "white")])

        cols = ("Title", "Season", "Type", "Score", "Status", "Edit Link")
        self._tree = ttk.Treeview(tree_frame, columns=cols, show="headings",
                                   style="Dark.Treeview")
        widths = [340, 70, 90, 70, 90, 260]
        for col, w in zip(cols, widths):
            self._tree.heading(col, text=col)
            self._tree.column(col, width=w, minwidth=60)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side="left", fill="both", expand=True, padx=8, pady=8)
        vsb.pack(side="right", fill="y", pady=8)
        self._tree.bind("<Double-1>", self._open_result_link)

        ctk.CTkLabel(parent,
                     text="Double-click a row to open the WP edit page",
                     font=FONT_SMALL, text_color=C_MUTED).pack(pady=(0, 8))

    # ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
    #  LOG PAGE
    # ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

    def _build_log_page(self, parent: ctk.CTkFrame):
        hdr_row = ctk.CTkFrame(parent, fg_color="transparent")
        hdr_row.pack(fill="x", padx=28, pady=(26, 8))
        ctk.CTkLabel(hdr_row, text="Full Log",
                     font=FONT_HEAD, text_color=C_TEXT).pack(side="left")
        ctk.CTkLabel(hdr_row,
                     text="Complete timestamped activity log",
                     font=FONT_SMALL, text_color=C_MUTED).pack(side="left", padx=(14, 0), pady=(6, 0))
        ctk.CTkButton(
            hdr_row, text="—‘  Clear", font=FONT_BODY,
            fg_color=C_SURFACE, hover_color=C_CARD2,
            height=34, width=90, corner_radius=8,
            command=self._clear_log,
        ).pack(side="right")

        self._full_log = ctk.CTkTextbox(
            parent, fg_color=C_SURFACE, text_color=C_TEXT,
            font=("Cascadia Code", 13),
            border_color=C_BORDER, border_width=1,
            corner_radius=10, wrap="word",
        )
        self._full_log.pack(fill="both", expand=True, padx=28, pady=(0, 20))
        self._full_log.configure(state="disabled")
        try:
            _tk = getattr(self._full_log, "_textbox", self._full_log)
            _tk.configure(spacing1=6, spacing2=2, spacing3=8, padx=10, pady=8)
        except Exception:
            pass

    # ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
    #  SYNC LOGIC
    # ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

    def _get_wp_api(self) -> WordPressAPI | None:
        url  = self._wp_url.get().strip()  if hasattr(self, "_wp_url")  else self.settings.get("wp_url", "")
        user = self._wp_user.get().strip() if hasattr(self, "_wp_user") else self.settings.get("wp_username", "")
        pwd  = self._wp_pass.get().strip() if hasattr(self, "_wp_pass") else self.settings.get("wp_app_password", "")
        if not url or not user or not pwd:
            messagebox.showerror("Missing credentials",
                                  "Please fill in WordPress URL, username, and app password in Settings.")
            return None
        return WordPressAPI(url, user, pwd)

    def _test_wp_connection(self):
        api = self._get_wp_api()
        if not api:
            return
        self._conn_status.configure(text="Testing¦", text_color=C_MUTED)
        self.update()
        ok, msg = api.test_connection()
        self._conn_status.configure(
            text=f"{'✅-' if ok else '❌'}  {msg}",
            text_color=C_SUCCESS if ok else C_ERROR,
        )
        # Update sidebar status dot
        if hasattr(self, "_status_dot"):
            self._status_dot.configure(
                text=f"•  {'Connected' if ok else 'Not connected'}",
                text_color=C_SUCCESS if ok else C_ERROR,
            )

    def _on_extract_streams_toggled(self):
        val = self._extract_streams_var.get() if hasattr(self, "_extract_streams_var") else True
        self.settings["extract_episode_streams"] = val

    def _apply_settings_lock(self, lock: bool, notice: str = ""):
        """Disable or re-enable the settings entry widgets while a job is running."""
        self._settings_locked = lock
        state = "disabled" if lock else "normal"
        for entry in getattr(self, "_settings_entries", []):
            try:
                entry.configure(state=state)
            except Exception:
                pass
        if hasattr(self, "_save_btn"):
            try:
                self._save_btn.configure(state=state)
            except Exception:
                pass

    def _save_settings_ui(self):
        try:
            # Safely parse numeric fields with fallbacks
            try:
                score = int(str(self._default_score_var.get()).strip() or 70) if hasattr(self, "_default_score_var") else 70
            except Exception:
                score = 70

            try:
                test_pages = int(str(self._test_pages_var.get()).strip() or 1) if hasattr(self, "_test_pages_var") else 1
            except Exception:
                test_pages = 1

            try:
                delay = float(str(self._delay_var.get()).strip() or 0.5) if hasattr(self, "_delay_var") else 0.5
            except Exception:
                delay = 0.5

            # Preserve all existing settings to avoid losing unrendered keys
            s = dict(self.settings)

            if hasattr(self, "_wp_url"):
                s["wp_url"] = self._wp_url.get().strip()
            if hasattr(self, "_wp_user"):
                s["wp_username"] = self._wp_user.get().strip()
            if hasattr(self, "_wp_pass"):
                s["wp_app_password"] = self._wp_pass.get().strip()
            if hasattr(self, "_tmdb_key"):
                s["tmdb_api_key"] = self._tmdb_key.get().strip()
            s["default_taqyeem_score"] = score
            if hasattr(self, "_test_mode_var"):
                s["test_mode"] = bool(self._test_mode_var.get())
            s["test_pages"] = test_pages
            if hasattr(self, "_extract_streams_var"):
                s["extract_episode_streams"] = bool(self._extract_streams_var.get())
            s["delay_between_posts"] = delay
            if hasattr(self, "_subtitle_key_var"):
                s["subtitle_meta_key"] = self._subtitle_key_var.get().strip() or "_subtitle"
            if hasattr(self, "_settings_pwd_var"):
                s["settings_password"] = self._settings_pwd_var.get()
            if hasattr(self, "_pixeldrain_key_var"):
                s["pixeldrain_api_key"] = self._pixeldrain_key_var.get().strip()
            if hasattr(self, "_toonstream_domains_var"):
                s["toonstream_domains"] = self._toonstream_domains_var.get().strip()
            if hasattr(self, "_multimovies_domains_var"):
                s["multimovies_domains"] = self._multimovies_domains_var.get().strip()
            if hasattr(self, "_bollyflix_url_var"):
                s["bollyflix_url"] = self._bollyflix_url_var.get().strip()

            if hasattr(self, "_tw4all_cookie_var"):
                val = self._tw4all_cookie_var.get().strip()
                if val and ("/verify/" in val or val.startswith("http")):
                    try:
                        import tw4all_resolver
                        val = tw4all_resolver.extract_cookie_from_verify_url(val)
                    except Exception:
                        pass
                s["tw4all_cookie"] = val

            if hasattr(self, "_cf_clearance_var"):
                s["cf_clearance"] = self._cf_clearance_var.get().strip()
            if hasattr(self, "_cf_ua_var"):
                s["cf_user_agent"] = self._cf_ua_var.get().strip()
            if hasattr(self, "_flaresolverr_url_var"):
                s["flaresolverr_url"] = self._flaresolverr_url_var.get().strip() or "http://localhost:8191"
            if hasattr(self, "_use_waw_var"):
                s["use_watchanimeworld"] = bool(self._use_waw_var.get())
            if hasattr(self, "_download_provider_var"):
                s["download_provider"] = self._download_provider_var.get().strip().lower() or "bollyflix"
            if hasattr(self, "_server_ug_source_var"):
                s["server_ug_source"] = self._server_ug_source_var.get().strip().lower() or "toonstream"
            if hasattr(self, "_animedekho_url_var"):
                s["animedekho_url"] = self._animedekho_url_var.get().strip().rstrip("/") or "https://animedekho.app"
            if hasattr(self, "_airing_skip_textbox"):
                s["airing_skip_list"] = [
                    line.strip() for line in self._airing_skip_textbox.get("1.0", "end").splitlines() if line.strip()
                ]
            if hasattr(self, "_dl_colab_var"):
                s["colab_url"] = self._dl_colab_var.get().strip()
            if hasattr(self, "_worker_base_var"):
                s["worker_base"] = self._worker_base_var.get().strip()
            if hasattr(self, "_dorabash_worker_url_var"):
                s["dorabash_worker_url"] = self._dorabash_worker_url_var.get().strip()

            save_settings(s)
            self.settings = s

            # Hot-patch stream_extractor domains immediately
            try:
                import sys
                if 'stream_extractor' in sys.modules:
                    se = sys.modules['stream_extractor']
                    ts = s.get("toonstream_domains", "")
                    mm = s.get("multimovies_domains", "")
                    cf = s.get("cf_clearance", "")
                    cf_ua = s.get("cf_user_agent", "")
                    fs_url = s.get("flaresolverr_url", "").strip().rstrip("/")
                    wb = s.get("worker_base", "").strip()
                    if ts:
                        se.TOONSTREAM_DOMAINS = [d.strip() for d in ts.split(",") if d.strip()]
                    if mm:
                        se.MULTIMOVIES_DOMAINS = [d.strip() for d in mm.split(",") if d.strip()]
                    if cf:
                        se.HEADERS["Cookie"] = f"cf_clearance={cf}"
                    elif "Cookie" in se.HEADERS:
                        del se.HEADERS["Cookie"]
                    if cf_ua:
                        se.HEADERS["User-Agent"] = cf_ua
                    if wb:
                        se.WORKER_BASE = wb.rstrip("/") + "/"
                    dw = s.get("dorabash_worker_url", "").strip()
                    if dw:
                        se.DORABASH_WORKER_URL = dw.rstrip("/") + "/"
                    se.FLARESOLVERR_URL = fs_url
                    se.USE_WATCHANIMEWORLD = s.get("use_watchanimeworld", False)
                    se.SERVER_UG_SOURCE = s.get("server_ug_source", "toonstream")
                    se.ANIMEDEKHO_URL = s.get("animedekho_url", "https://animedekho.app")
            except Exception as e:
                logger.warning("Hot-patch stream_extractor error: %s", e)

            # Instant Visual Feedback
            if hasattr(self, "_save_status_label"):
                self._save_status_label.configure(
                    text="🟢 Settings saved successfully to settings.json!",
                    text_color="#10b981"
                )
            if hasattr(self, "_save_btn"):
                orig_text = "💾  Save Settings"
                orig_color = C_ACCENT
                self._save_btn.configure(text="✅  Saved Successfully!", fg_color="#10b981")
                def _reset_btn():
                    try:
                        self._save_btn.configure(text=orig_text, fg_color=orig_color)
                        if hasattr(self, "_save_status_label"):
                            self._save_status_label.configure(
                                text="Changes take effect immediately after saving.",
                                text_color=C_MUTED
                            )
                    except Exception:
                        pass
                self.after(3000, _reset_btn)

            self._log("✅ Settings saved successfully to settings.json", "success")
            messagebox.showinfo("Settings Saved", "Your settings have been saved successfully!", parent=self)

        except Exception as exc:
            logger.exception("Error saving settings")
            if hasattr(self, "_save_status_label"):
                self._save_status_label.configure(
                    text=f"❌ Save error: {exc}",
                    text_color="#ef4444"
                )
            self._log(f"💥 Failed to save settings: {exc}", "error")
            messagebox.showerror("Save Failed", f"Could not save settings:\n{exc}", parent=self)

    def _test_flaresolverr(self):
        """Test FlareSolverr by sending a request to toon-stream.site and checking the response."""
        import threading
        fs_url = self._flaresolverr_url_var.get().strip().rstrip("/") if hasattr(self, "_flaresolverr_url_var") else "http://localhost:8191"
        if not fs_url:
            messagebox.showwarning("No URL", "Please enter the FlareSolverr URL first (e.g. http://localhost:8191)", parent=self)
            return

        def _run():
            try:
                import requests as _req
                # First check if FlareSolverr is running
                try:
                    ping = _req.get(f"{fs_url}/health", timeout=5)
                    if ping.status_code != 200:
                        self.after(0, lambda: messagebox.showerror(
                            "FlareSolverr Not Running",
                            f"FlareSolverr responded with HTTP {ping.status_code}.\n\n"
                            "Make sure flaresolverr.exe is running!\n"
                            "Download: github.com/FlareSolverr/FlareSolverr/releases",
                            parent=self
                        ))
                        return
                except Exception:
                    self.after(0, lambda: messagebox.showerror(
                        "FlareSolverr Not Running",
                        f"Cannot connect to FlareSolverr at {fs_url}.\n\n"
                        "Make sure flaresolverr.exe is running!\n"
                        "Download: github.com/FlareSolverr/FlareSolverr/releases",
                        parent=self
                    ))
                    return

                # Now test against a protected series page on toon-stream.site
                test_target_url = "https://toon-stream.site/series/captain-tsubasa-2/"
                payload = {"cmd": "request.get", "url": test_target_url, "maxTimeout": 45000}
                resp = _req.post(f"{fs_url}/v1", json=payload, timeout=55)
                data = resp.json()
                status = data.get("status", "")
                solution = data.get("solution", {})
                http_status = solution.get("status", 0)
                html = solution.get("response", "")

                if status == "ok" and http_status in (200, 301, 302) and len(html) > 500:
                    self.after(0, lambda: messagebox.showinfo(
                        "✅ FlareSolverr Works!",
                        f"FlareSolverr successfully bypassed Cloudflare on Toonstream!\n\n"
                        f"Target: {test_target_url}\n"
                        f"Response: HTTP {http_status}, {len(html)} chars received.\n\n"
                        "AutoPoster will now use FlareSolverr automatically to fetch new episodes from Toonstream.",
                        parent=self
                    ))
                else:
                    self.after(0, lambda: messagebox.showwarning(
                        "FlareSolverr Partial",
                        f"FlareSolverr responded but got unexpected result.\n"
                        f"Status: {status}, HTTP: {http_status}, HTML len: {len(html)}\n\n"
                        "Try again — Cloudflare challenges can take 15-30 seconds.",
                        parent=self
                    ))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("Test Error", f"Error testing FlareSolverr: {e}", parent=self))

        threading.Thread(target=_run, daemon=True).start()
        messagebox.showinfo("Testing...", "Testing FlareSolverr against toon-stream.site...\n\nThis may take up to 30 seconds. Please wait.", parent=self)

    def _launch_cf_autobypass(self):
        """Auto-detect and fetch fresh Cloudflare clearance tokens via FlareSolverr or browser."""
        fs_url = self.settings.get("flaresolverr_url", "http://localhost:8191").strip().rstrip("/")
        
        # 1. Try FlareSolverr first if configured
        if fs_url:
            try:
                import requests as _req
                target_url = "https://toon-stream.site/series/captain-tsubasa-2/"
                payload = {"cmd": "request.get", "url": target_url, "maxTimeout": 45000}
                r = _req.post(f"{fs_url}/v1", json=payload, timeout=60)
                data = r.json()
                sol = data.get("solution", {})
                ua = sol.get("userAgent", "")
                cookies = sol.get("cookies", [])
                cf_val = None
                for c in cookies:
                    if c.get("name") == "cf_clearance":
                        cf_val = c.get("value")
                        break

                if cf_val and ua:
                    if hasattr(self, "_cf_clearance_var"):
                        self._cf_clearance_var.set(cf_val)
                    if hasattr(self, "_cf_ua_var"):
                        self._cf_ua_var.set(ua)
                    self.settings["cf_clearance"] = cf_val
                    self.settings["cf_user_agent"] = ua
                    save_settings(self.settings)

                    import sys
                    if 'stream_extractor' in sys.modules:
                        sys.modules['stream_extractor'].HEADERS["Cookie"] = f"cf_clearance={cf_val}"
                        sys.modules['stream_extractor'].HEADERS["User-Agent"] = ua

                    messagebox.showinfo("Success!", "Successfully fetched fresh Cloudflare clearance tokens via FlareSolverr and saved to Settings!", parent=self)
                    return
            except Exception as e:
                logger.debug("FlareSolverr auto-bypass attempt failed: %s", e)

        # 2. Browser fallback
        import webbrowser
        webbrowser.open("https://toon-stream.site/")

        res = messagebox.askyesno(
            "Cloudflare Auto-Bypass",
            "Opening toon-stream.site in your browser...\n\n"
            "1. Complete the Cloudflare verification check in your browser window if prompted.\n"
            "2. Once the page loads, click YES below to auto-detect and save your Cloudflare cookie!\n\n"
            "Auto-detect and save cookie now?",
            parent=self
        )
        if res:
            try:
                cookie, host, browser = get_installed_browser_cf_cookie()
                if cookie:
                    if hasattr(self, "_cf_clearance_var"):
                        self._cf_clearance_var.set(cookie)
                    self.settings["cf_clearance"] = cookie
                    save_settings(self.settings)

                    import sys
                    if 'stream_extractor' in sys.modules:
                        sys.modules['stream_extractor'].HEADERS["Cookie"] = f"cf_clearance={cookie}"

                    messagebox.showinfo("Success!", f"Successfully auto-detected Cloudflare cookie for {host} from {browser} and saved to Settings!", parent=self)
                else:
                    messagebox.showwarning(
                        "Cookie Not Found / Copy Manually",
                        "Chrome 127+ locks encrypted cookie databases while Chrome is running.\n\n"
                        "To paste your cf_clearance cookie manually:\n"
                        "1. On toon-stream.site, press F12 (Developer Tools).\n"
                        "2. Go to Application tab ➔ Cookies ➔ toon-stream.site.\n"
                        "3. Copy the value of 'cf_clearance' and paste it into the field in Settings!",
                        parent=self
                    )
            except Exception as e:
                messagebox.showerror("Detection Error", f"Error detecting cookie: {e}", parent=self)


    def _get_live_skip_list(self) -> list[str]:
        """Read the airing skip list directly from the textbox (no Save required)."""
        if hasattr(self, "_airing_skip_textbox"):
            try:
                raw = self._airing_skip_textbox.get("1.0", "end")
                return [line.strip() for line in raw.splitlines() if line.strip()]
            except Exception:
                pass
        return self.settings.get("airing_skip_list", [])

    def _effective_settings(self) -> dict:
        """Return self.settings with live skip list and download provider settings merged in."""
        s = dict(self.settings)
        s["airing_skip_list"] = self._get_live_skip_list()
        if hasattr(self, "_download_provider_var"):
            s["download_provider"] = self._download_provider_var.get().strip().lower()
        if hasattr(self, "_bollyflix_url_var"):
            s["bollyflix_url"] = self._bollyflix_url_var.get().strip()
        if hasattr(self, "_tw4all_cookie_var"):
            s["tw4all_cookie"] = self._tw4all_cookie_var.get().strip()
        return s


    def _reset_stats(self):
        self._cnt_created = self._cnt_skipped = self._cnt_errors = self._cnt_total = 0
        self._stat_created.set("0")
        self._stat_skipped.set("0")
        self._stat_errors.set("0")
        self._stat_total.set("0")
        self._progress_bar.set(0)
        self._progress_label.configure(text="Ready to start")
        self._progress_sub.configure(text="")
        self._results.clear()
        for row in self._tree.get_children():
            self._tree.delete(row)
        self._clear_log()

    def _skip_current_airing(self):
        """Signal the airing updater to skip the currently-processing series."""
        if hasattr(self, '_airing_skip_event'):
            self._airing_skip_event.set()
            self._log("⏩ Skip requested — will skip after current episode check.", "warn")

    def _start_update_airing(self):
        """Manually trigger airing post updater without doing a full sync."""
        api = self._get_wp_api()
        if not api:
            return
        ok, msg = api.test_connection()
        if not ok:
            messagebox.showerror("Connection Failed", msg)
            return

        self._airing_skip_event = threading.Event()
        self._update_airing_btn.configure(state="disabled", text="🔄  Checking…")
        self._skip_airing_btn.configure(state="normal")
        self._log("🔄 Manual airing posts update triggered…", "info")
        self._apply_settings_lock(True, "Locked during airing update")

        skip_event = self._airing_skip_event

        def _skip_fn():
            """Returns True if skip was requested; auto-resets for the next post."""
            if skip_event.is_set():
                skip_event.clear()
                return True
            return False

        def _worker():
            try:
                total, updated = check_and_update_airing_posts(
                    api, self._effective_settings(), log_fn=self._log, skip_fn=_skip_fn
                )
                self._log(
                    f"✅ Airing check done — {total} posts checked, {updated} updated.",
                    "success" if updated > 0 else "info",
                )
            except Exception as exc:
                logger.exception("Airing update worker crashed")
                self._log(f"💥 Airing update error: {exc}", "error")
            finally:
                self.after(0, lambda: self._update_airing_btn.configure(
                    state="normal", text="🔄  Update Airing Posts"
                ))
                self.after(0, lambda: self._skip_airing_btn.configure(state="disabled"))
                self.after(0, lambda: self._apply_settings_lock(False))

        threading.Thread(target=_worker, daemon=True).start()

    def _start_sync(self):
        api = self._get_wp_api()
        if not api:
            return

        ok, msg = api.test_connection()
        if not ok:
            messagebox.showerror("Connection Failed", msg)
            return

        self._stop_flag[0] = False
        self._start_btn.configure(state="disabled")
        self._stop_btn.configure(state="normal")
        self.after(0, lambda: self._apply_settings_lock(True, "Locked during sync"))

        test_mode   = self._test_mode_var.get()
        test_pages  = int(self._test_pages_var.get() or 1)
        tmdb_key    = self._tmdb_key.get().strip() if hasattr(self, "_tmdb_key") else self.settings.get("tmdb_api_key", "")
        default_score = int(self._default_score_var.get() or 70) if hasattr(self, "_default_score_var") else self.settings.get("default_taqyeem_score", 70)
        delay = float(self._delay_var.get() or 0.5) if hasattr(self, "_delay_var") else self.settings.get("delay_between_posts", 0.5)
        subtitle_key = self._subtitle_key_var.get().strip() if hasattr(self, "_subtitle_key_var") else self.settings.get("subtitle_meta_key", "_subtitle")

        self._run_thread = threading.Thread(
            target=self._sync_worker,
            args=(api, test_mode, test_pages, tmdb_key, default_score, delay, subtitle_key),
            daemon=True,
        )
        self._run_thread.start()

    def _stop_sync(self):
        self._stop_flag[0] = True
        self._log("⏹ Stop requested - will halt after current post.", "warn")
        self._stop_btn.configure(state="disabled")

    # ------ Post from URL ---------------------------------------------------------------------------------------------------------------------------------------------------------------------

    def _start_post_from_urls(self):
        raw = self._url_box.get("1.0", "end").strip()
        urls = [u.strip() for u in raw.splitlines() if u.strip()]
        if not urls:
            messagebox.showwarning("No URLs", "Please paste at least one ToonWorld4All URL.")
            return

        api = self._get_wp_api()
        if not api:
            return
        ok, msg = api.test_connection()
        if not ok:
            messagebox.showerror("Connection Failed", msg)
            return

        self._stop_flag[0] = False
        self._post_url_btn.configure(state="disabled", text="³  Working¦")
        self._stop_btn.configure(state="normal")
        self._apply_settings_lock(True, "Locked during post import")

        tmdb_key     = self._tmdb_key.get().strip() if hasattr(self, "_tmdb_key") else self.settings.get("tmdb_api_key", "")
        default_score = int(self._default_score_var.get() or 70) if hasattr(self, "_default_score_var") else self.settings.get("default_taqyeem_score", 70)
        delay        = float(self._delay_var.get() or 0.5) if hasattr(self, "_delay_var") else self.settings.get("delay_between_posts", 0.5)
        subtitle_key = self._subtitle_key_var.get().strip() if hasattr(self, "_subtitle_key_var") else self.settings.get("subtitle_meta_key", "_subtitle")

        manual_toonstream  = self._manual_toonstream_var.get().strip() if hasattr(self, "_manual_toonstream_var") else ""
        manual_multimovies = self._manual_multimovies_var.get().strip() if hasattr(self, "_manual_multimovies_var") else ""

        threading.Thread(
            target=self._post_from_url_worker,
            args=(api, urls, tmdb_key, default_score, delay, subtitle_key, manual_toonstream, manual_multimovies),
            daemon=True,
        ).start()

    def _post_from_url_worker(
        self,
        api: WordPressAPI,
        urls: list,
        tmdb_key: str,
        default_score: int,
        delay: float,
        subtitle_key: str,
        manual_toonstream: str = "",
        manual_multimovies: str = "",
    ):
        import time as _time
        from scraper import scrape_post

        try:
            self._log(f"⚡ Starting manual import for {len(urls)} URL(s)¦", "info")
            if manual_toonstream:
                self._log(f"  ð Manual Toonstream override: {manual_toonstream}", "muted")
            if manual_multimovies:
                self._log(f"  ð Manual Multimovies override: {manual_multimovies}", "muted")

            for idx, url in enumerate(urls, 1):
                if self._stop_flag[0]:
                    self._log("¹ Stopped.", "warn")
                    break

                self._log(f"  [{idx}/{len(urls)}] Scraping: {url}", "muted")
                try:
                    post_data = scrape_post(url)
                except Exception as exc:
                    self._log(f"   Failed to scrape {url}: {exc}", "error")
                    continue

                if not post_data:
                    self._log(f"  ❌ No data returned from scraper for {url}", "error")
                    continue

                show_name = post_data.get("show_name", "")
                season    = post_data.get("season", 1)
                c_type    = post_data.get("content_type", "show")
                is_movie  = post_data.get("is_movie", False) or c_type == "movie"

                if not show_name:
                    self._log(f"  ❌ Could not determine show name for {url}", "error")
                    continue

                if is_movie:
                    self._log(f"  🎬 {show_name} [{c_type}]")
                else:
                    self._log(f"  🎬 {show_name} (Season {season}) [{c_type}]")

                # Fetch rating / episode metadata (passing exact season)
                meta = get_show_metadata(show_name, c_type, tmdb_key, season=season)
                score = meta.get("score")
                if score is None:
                    score = default_score
                    self._log(f"     Rating: fallback {score}", "muted")
                else:
                    self._log(f"     Rating: {score}/100", "muted")

                expected_eps   = meta.get("expected_episodes")
                anilist_status = meta.get("status")

                # Check if post already exists --- update streams only
                if api.post_exists(show_name, season, is_movie=is_movie):
                    self._log(f"  🔄 Post already exists: {show_name} S{season} - updating streams.", "warn")
                    
                    # Extract downloads first so we can merge them in
                    _manual_dl = self._manual_download_var.get().strip() if hasattr(self, "_manual_download_var") else ""
                    _exist_downloads_map = {}
                    if _manual_dl:
                        self._log(f"     Resolving downloads from: {_manual_dl}", "muted")
                        from stream_extractor import extract_downloads_from_url as _edl_fn
                        _exist_downloads_map = _edl_fn(_manual_dl, target_season=season)
                        if _exist_downloads_map:
                            qualities = ", ".join(f"{q.upper()} x{len(v)}" for q, v in _exist_downloads_map.items())
                            self._log(f"     Downloads resolved: {qualities}", "info")
                        else:
                            self._log(f"     No downloads could be resolved from that URL.", "warn")
                    else:
                        self._log(f"     (No download URL provided — skipping download merge)", "muted")

                    # 1st attempt: airing posts updater
                    try:
                        _tot, _upd = check_and_update_airing_posts(
                            api, self._effective_settings(),
                            log_fn=self._log,
                            filter_show=(show_name, season),
                            downloads_map=_exist_downloads_map,
                        )
                        if _tot > 0:
                            _time.sleep(delay)
                            continue
                    except Exception as exc_upd:
                        self._log(f"     (Airing-update error: {exc_upd})", "muted")

                    # 2nd attempt: direct lookup regardless of airing status
                    self._log(f"     Post not in airing list - looking up directly¦", "muted")
                    self._force_update_streams(
                        api, show_name, season, expected_eps, anilist_status, delay,
                        is_movie=is_movie, downloads_map=_exist_downloads_map,
                        manual_multimovies=manual_multimovies,
                    )
                    _time.sleep(delay)
                    continue

                # Create draft
                success, edit_url, message, post_id = api.create_draft_post(
                    post_data,
                    taqyeem_score=score,
                    tmdb_api_key=tmdb_key,
                    subtitle_meta_key=subtitle_key,
                )

                if not success:
                    self._log(f"  ❌ Draft creation failed: {message}", "error")
                    _time.sleep(delay)
                    continue

                self._cnt_created += 1
                if is_movie:
                    self._log(f"  ✅… Draft created: {show_name}", "success")
                else:
                    self._log(f"  ✅… Draft created: {show_name} S{season}", "success")

                # ------ Extract episode streams ------------------------------------------------------------------------------------------------
                if post_id and self.settings.get("extract_episode_streams", True):
                    try:
                        from stream_extractor import (
                            fetch_all_episodes_data,
                            generate_episode_toggle_html,
                            count_toonstream_episodes,
                            count_multimovies_episodes,
                            extract_ruby_url_from_toonstream,
                            extract_toonstream_all_servers,
                            resolve_multimovies_episode,
                            resolve_satoru_episode,
                        )

                        self._log(f"     -  Checking stream availability…", "muted")
                        
                        # Fetch downloads before deciding whether to bail
                        manual_download = self._manual_download_var.get().strip() if hasattr(self, "_manual_download_var") else ""
                        downloads_map = {}
                        if manual_download:
                            self._log(f"     Resolving Pixeldrain downloads from {manual_download}…", "info")
                            downloads_map = extract_downloads_from_url(manual_download, target_season=season)

                        ep1_ruby, ep1_mg, ep1_pg = extract_toonstream_all_servers(
                            show_name, season=season, episode=1, is_movie=is_movie,
                            direct_url=manual_toonstream,
                        )
                        ep1_multi = resolve_multimovies_episode(
                            show_name, season=season, episode=1, is_movie=is_movie,
                            direct_url=manual_multimovies,
                        )
                        if not ep1_mg and not is_movie:
                            ep1_mg = resolve_satoru_episode(show_name, season=season, episode=1)

                        if not ep1_ruby and not ep1_multi and not ep1_mg and not ep1_pg and not downloads_map:
                            self._log(f"     ⚠️ No streams found - removing orphan draft.", "warn")
                            try:
                                api.session.delete(
                                    f"{api.base}/posts/{post_id}",
                                    params={"force": True}, timeout=10,
                                )
                            except Exception:
                                pass
                            self._cnt_created -= 1
                            self._cnt_errors  += 1
                            self._queue_stat_update()
                            _time.sleep(delay)
                            continue

                        if is_movie:
                            total_eps = 1
                        else:
                            # Smart count: only call the counter for whichever source ep1 succeeded on.
                            # This avoids wasting 60-180s searching a source that clearly doesn't have the show.
                            total_eps = 0
                            if ep1_ruby:
                                # Show is on Toonstream — count from Toonstream (episode map already warm from ep1)
                                total_eps = count_toonstream_episodes(show_name, season)
                            if not total_eps and ep1_multi:
                                # Show is on Multimovies — count from there (show URL already cached from ep1)
                                total_eps = count_multimovies_episodes(show_name, season=season, direct_url=manual_multimovies)
                            if not total_eps:
                                # Neither source counted — fall back to TMDB, then scraped post data
                                if expected_eps and expected_eps > 0:
                                    total_eps = expected_eps
                                else:
                                    ep_raw = post_data.get("episodes", "")
                                    if ep_raw:
                                        nums = re.findall(r'\d+', str(ep_raw))
                                        total_eps = max(1, int(nums[0])) if nums else 1
                                    else:
                                        total_eps = 1
                            self._log(f"     📊 Episode count: {total_eps}", "muted")

                        self._log(f"     🎬 Extracting streams for {total_eps} ep(s)…", "muted")

                        # downloads_map already fetched above (before the no-stream bail check)
                        episodes_data = []
                        ep1_dls = {q: links[0] for q, links in downloads_map.items() if len(links) > 0}
                        if ep1_ruby or ep1_multi or ep1_mg or ep1_pg or ep1_dls:
                            episodes_data.append({"episode_num": 1, "ruby_url": ep1_ruby, "multi_url": ep1_multi, "mg_url": ep1_mg, "pg_url": ep1_pg, "downloads": ep1_dls})
                        if total_eps > 1 and not is_movie:
                            remaining = fetch_all_episodes_data(
                                show_name, season, total_eps, start_from=2,
                                toonstream_url=manual_toonstream,
                                multimovies_url=manual_multimovies,
                            )
                            for ep_item in remaining:
                                ep_idx = ep_item.get("episode_num", 1) - 1
                                ep_item["downloads"] = {q: links[ep_idx] for q, links in downloads_map.items() if ep_idx < len(links)}
                            episodes_data.extend(remaining)

                        # Include episodes that have streams OR downloads (not just streams)
                        active_eps = [e for e in episodes_data if e.get("ruby_url") or e.get("ag_url") or e.get("multi_url") or e.get("mg_url") or e.get("pg_url") or e.get("downloads")]
                        found_ruby     = sum(1 for e in episodes_data if e.get("ruby_url"))
                        found_ag       = sum(1 for e in episodes_data if e.get("ag_url"))
                        found_multi    = sum(1 for e in episodes_data if e.get("multi_url"))
                        found_mg       = sum(1 for e in episodes_data if e.get("mg_url"))
                        found_pg       = sum(1 for e in episodes_data if e.get("pg_url"))
                        latest_ep_num  = max((e.get("episode_num", 1) for e in active_eps), default=0)

                        _anilist_fin = (anilist_status is not None and anilist_status.upper() in ("FINISHED", "CANCELLED"))
                        if is_movie:
                            is_airing = False
                        elif anilist_status and anilist_status.upper() == "RELEASING":
                            # AniList explicitly says releasing → always Airing
                            is_airing = True
                        elif (
                            _anilist_fin
                            and expected_eps and expected_eps > 0
                            and latest_ep_num >= expected_eps
                        ):
                            # AniList done + we know the total + post has all eps → Completed
                            is_airing = False
                        elif expected_eps and expected_eps > 0 and latest_ep_num < expected_eps:
                            # Know total, don't have all → Airing
                            is_airing = True
                        elif not expected_eps or expected_eps <= 0:
                            # Unknown total → always Airing (safe fallback)
                            is_airing = True
                        elif latest_ep_num <= 1 and not (_anilist_fin and total_eps == 1):
                            is_airing = True
                        else:
                            is_airing = True  # When in doubt, stay Airing

                        toggle_html = generate_episode_toggle_html(
                            active_eps,
                            series_name=show_name,
                            season_num=season,
                        )

                        if toggle_html:
                            if is_movie:
                                airing_title_suffix = ""
                            elif is_airing and latest_ep_num > 0:
                                airing_title_suffix = f"Episode {latest_ep_num} Added!"
                            else:
                                airing_title_suffix = ""
                            patch_ok = api.patch_post_with_streams(
                                post_id,
                                toggle_html=toggle_html,
                                is_airing=is_airing,
                                latest_episode=latest_ep_num,
                                airing_title_suffix=airing_title_suffix,
                            )
                            if patch_ok:
                                self._log(
                                    f"     ✅ Streams patched - Toonstream:{found_ruby} AG:{found_ag} Multimovies:{found_multi} Server MG:{found_mg} Server PG:{found_pg}",
                                    "success",
                                )
                            else:
                                self._log("     š  Stream patch returned an error.", "warn")
                    except Exception as exc:
                        logger.exception("Stream extraction failed for manual URL post")
                        self._log(f"     š  Stream extraction error: {exc}", "warn")
                else:
                    self._log("     📝 Draft created without stream extraction (Draft Only mode).", "muted")

                self._queue_stat_update()
                _time.sleep(delay)

            self._log(f"🎉 Manual import done - {len(urls)} URL(s) processed.", "success")
        except Exception as exc:
            logger.exception("_post_from_url_worker crashed")
            self._log(f"💥¥ Error: {exc}", "error")
        finally:
            self.after(0, lambda: self._post_url_btn.configure(
                state="normal", text="⚡  Post from URLs"
            ))
            self.after(0, lambda: self._stop_btn.configure(state="disabled"))
            self.after(0, lambda: self._apply_settings_lock(False))

    # ------ Extract Episode Links Only ---------------------------------------------------------------------------------------------------------------------------

    def _start_extract_links_only(self):
        show_name = self._ext_show_var.get().strip() if hasattr(self, "_ext_show_var") else ""
        ts_url    = self._ext_ts_url_var.get().strip() if hasattr(self, "_ext_ts_url_var") else ""
        mm_url    = self._ext_mm_url_var.get().strip() if hasattr(self, "_ext_mm_url_var") else ""
        sat_url   = self._ext_sat_url_var.get().strip() if hasattr(self, "_ext_sat_url_var") else ""
        ad_url    = self._ext_ad_url_var.get().strip() if hasattr(self, "_ext_ad_url_var") else ""
        bh_url    = self._ext_bh_url_var.get().strip() if hasattr(self, "_ext_bh_url_var") else ""
        db_url    = self._ext_db_url_var.get().strip() if hasattr(self, "_ext_db_url_var") else ""
        if "dorabash.in" in db_url or "byse" in db_url:
            m_u = re.search(r"https?://[^\s'\"<>]+", db_url)
            if m_u:
                db_url = m_u.group(0).rstrip(".,;)")
        dl_url    = self._ext_dl_url_var.get().strip() if hasattr(self, "_ext_dl_url_var") else ""
        an_url    = self._ext_an_url_var.get().strip() if hasattr(self, "_ext_an_url_var") else ""
        an_lang   = self._ext_an_lang_var.get().strip() if hasattr(self, "_ext_an_lang_var") else self.settings.get("anihosts_language", self.settings.get("kototv_language", "sub"))
        an_server = self._ext_an_server_var.get().strip() if hasattr(self, "_ext_an_server_var") else self.settings.get("anihosts_preferred_server", self.settings.get("kototv_preferred_server", "Server 1 (Kaze)"))
        mode      = self._ext_source_mode_var.get() if hasattr(self, "_ext_source_mode_var") else "all"

        # Auto-derive show title from URL if not typed
        if not show_name:
            if ts_url:
                show_name = ts_url.rstrip("/").split("/")[-1].replace("-", " ").title()
            elif bh_url:
                clean_bh = re.sub(r'/(?:episode-\d+.*)$', '', bh_url.rstrip('/'), flags=re.I)
                clean_slug = clean_bh.split("/")[-1]
                clean_slug = re.sub(r'-(?:multi-audio|hindi|dub|tamil|telugu|season-\d+).*', '', clean_slug, flags=re.I)
                show_name = clean_slug.replace("-", " ").title()
            elif db_url:
                try:
                    from stream_extractor import extract_byse_code, get_byse_video_details
                    code = extract_byse_code(db_url)
                    if code:
                        det = get_byse_video_details(code)
                        if det and det.get("title"):
                            t = det["title"]
                            t = re.sub(r'\[.*?\]', '', t)
                            t = re.sub(r'S\d+E\d+.*', '', t, flags=re.I)
                            t = re.sub(r'-\s*\d+.*', '', t)
                            show_name = t.strip()
                except Exception:
                    pass
                if not show_name:
                    clean_db = re.sub(r'/(?:episode|e|watch|season-\d+.*)$', '', db_url.rstrip('/'), flags=re.I)
                    clean_slug = clean_db.split('/')[-1]
                    clean_slug = re.sub(r'-(?:hindi|dub|multi-audio|season-\d+).*', '', clean_slug, flags=re.I)
                    show_name = clean_slug.replace("-", " ").title()
            elif ad_url:
                show_name = ad_url.rstrip("/").split("/")[-1].replace("-", " ").title()
            elif an_url:
                clean_an = re.sub(r'/(?:watch|episode|ep).*$', '', an_url.rstrip('/'), flags=re.I)
                clean_slug = clean_an.rstrip('/').split('/')[-1]
                clean_slug = re.sub(r'-\d+$', '', clean_slug)
                show_name = clean_slug.replace("-", " ").title()
            elif mm_url:
                show_name = mm_url.rstrip("/").split("/")[-1].replace("-", " ").title()
            elif sat_url:
                show_name = sat_url.rstrip("/").split("/")[-1].replace("-", " ").title()
            elif dl_url:
                clean_dl = re.sub(r'/(?:episode|season-\d+.*)$', '', dl_url.rstrip('/'))
                show_name = clean_dl.split("/")[-1].replace("-", " ").title()

        if not show_name:
            messagebox.showwarning("Missing Title", "Please enter a Show Title or paste a post URL.")
            return

        try:
            season = int(self._ext_season_var.get().strip() or 1)
        except ValueError:
            season = 1

        try:
            start_ep = int(getattr(self, "_ext_start_ep_var", tk.StringVar(value="1")).get().strip() or 1)
        except ValueError:
            start_ep = 1

        try:
            end_ep = int(getattr(self, "_ext_end_ep_var", tk.StringVar(value="12")).get().strip() or 12)
        except ValueError:
            end_ep = 12

        if start_ep < 1:
            start_ep = 1
        if end_ep < start_ep:
            end_ep = start_ep

        # Auto-detect total episodes from BlisseyHusband page if bh_url is provided and end_ep is default
        if bh_url and (end_ep == 12 or end_ep <= 1):
            try:
                from stream_extractor import count_blisseyhusband_episodes
                bh_cnt = count_blisseyhusband_episodes(show_name, season=season, direct_url=bh_url)
                if bh_cnt > 0:
                    end_ep = bh_cnt
                    if hasattr(self, "_ext_end_ep_var"):
                        self._ext_end_ep_var.set(str(end_ep))
            except Exception:
                pass

        # Auto-detect total episodes from Satoru if sat_url or mode == "satoru" and end_ep is default
        if sat_url and (end_ep == 12 or end_ep <= 1):
            try:
                from stream_extractor import count_satoru_episodes
                sat_cnt = count_satoru_episodes(show_name, season=season, direct_url=sat_url)
                if sat_cnt > 0:
                    end_ep = sat_cnt
                    if hasattr(self, "_ext_end_ep_var"):
                        self._ext_end_ep_var.set(str(end_ep))
            except Exception:
                pass
        elif mode == "satoru" and not sat_url and (end_ep == 12 or end_ep <= 1):
            try:
                from stream_extractor import count_satoru_episodes
                sat_cnt = count_satoru_episodes(show_name, season=season)
                if sat_cnt > 0:
                    end_ep = sat_cnt
                    if hasattr(self, "_ext_end_ep_var"):
                        self._ext_end_ep_var.set(str(end_ep))
            except Exception:
                pass

        # Auto-detect total episodes from AniHosts / KotoTV if an_url or mode is selected and end_ep is default
        if an_url and (end_ep == 12 or end_ep <= 1):
            try:
                if "anihosts" in an_url.lower():
                    from stream_extractor import count_anihosts_episodes
                    ah_cnt = count_anihosts_episodes(show_name, season=season, direct_url=an_url)
                    if ah_cnt > 0:
                        end_ep = ah_cnt
                        if hasattr(self, "_ext_end_ep_var"):
                            self._ext_end_ep_var.set(str(end_ep))
                elif "kototv" in an_url.lower():
                    from stream_extractor import count_kototv_episodes
                    kt_cnt = count_kototv_episodes(show_name, season=season, direct_url=an_url)
                    if kt_cnt > 0:
                        end_ep = kt_cnt
                        if hasattr(self, "_ext_end_ep_var"):
                            self._ext_end_ep_var.set(str(end_ep))
            except Exception:
                pass
        elif mode == "anihosts" and not an_url and (end_ep == 12 or end_ep <= 1):
            try:
                from stream_extractor import count_anihosts_episodes
                ah_cnt = count_anihosts_episodes(show_name, season=season)
                if ah_cnt > 0:
                    end_ep = ah_cnt
                    if hasattr(self, "_ext_end_ep_var"):
                        self._ext_end_ep_var.set(str(end_ep))
            except Exception:
                pass

        # Auto-detect season & episode if single Byse URL/code is provided
        if db_url and (end_ep == 12 or end_ep <= 1):
            try:
                from stream_extractor import extract_byse_code, get_byse_video_details
                c = extract_byse_code(db_url)
                if c:
                    d = get_byse_video_details(c)
                    if d and d.get("title"):
                        m_se = re.search(r'S(\d+)E(\d+)', d["title"], re.I)
                        if m_se:
                            det_s = int(m_se.group(1))
                            det_ep = int(m_se.group(2))
                            if season == 1 and hasattr(self, "_ext_season_var"):
                                season = det_s
                                self._ext_season_var.set(str(season))
                            start_ep = det_ep
                            end_ep = det_ep
                            if hasattr(self, "_ext_start_ep_var"):
                                self._ext_start_ep_var.set(str(start_ep))
                            if hasattr(self, "_ext_end_ep_var"):
                                self._ext_end_ep_var.set(str(end_ep))
            except Exception:
                pass

        self._ext_extract_btn.configure(state="disabled", text="⏳ Extracting…")
        self._ext_box.delete("1.0", "end")
        self._ext_box.insert("end", f"# Extracting stream links for {show_name} S{season} E{start_ep}-E{end_ep} [{mode}]…\n\n")

        threading.Thread(
            target=self._extract_links_only_worker,
            args=(show_name, season, start_ep, end_ep, mode, ts_url, mm_url, dl_url, sat_url, an_url, an_lang, an_server, ad_url, bh_url, db_url),
            daemon=True,
        ).start()

    def _extract_links_only_worker(
        self,
        show_name: str,
        season: int,
        start_ep: int,
        end_ep: int,
        mode: str,
        ts_url: str,
        mm_url: str,
        dl_url: str = "",
        sat_url: str = "",
        an_url: str = "",
        an_lang: str = "dub",
        an_server: str = "HD-1",
        ad_url: str = "",
        bh_url: str = "",
        db_url: str = "",
    ):
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from stream_extractor import (
            get_available_anihosts_servers,
            extract_anihosts_episode,
            get_available_kototv_servers,
            extract_kototv_episode,
            extract_animedekho_stream,
            extract_animedekho_vidmoly_stream,
            extract_animedekho_all_streams,
            extract_blisseyhusband_episode,
            extract_byse_episode,
        )

        try:
            ep_nums = list(range(start_ep, end_ep + 1))
            self._log(f"--- Extracting S{season} E{start_ep}-E{end_ep} ({len(ep_nums)} eps) for {show_name} [{mode}] ---", "info")

            if mode == "animedekho":
                self._log("⚡ Extracting streams directly from AnimeDekho via dgt_gate auto-bypass…", "info")

            downloads_map = {}
            if dl_url:
                self._log(f"📥 Resolving Pixeldrain downloads from {dl_url}…", "info")
                downloads_map = extract_downloads_from_url(dl_url, target_season=season)


            def _worker_task(ep):
                ruby = None
                multi = None
                mg = None
                pg = None
                an = None
                vidmoly = None
                bh = None
                db = None
                ep_dls = {}

                if mode == "vidmoly":
                    vidmoly = extract_animedekho_vidmoly_stream(
                        show_name, season=season, episode=ep, direct_url=ad_url
                    )
                elif mode == "animedekho":
                    ad_streams = extract_animedekho_all_streams(
                        show_name, season=season, episode=ep, direct_url=ad_url
                    )
                    ruby = ad_streams.get("ravok_url")
                    if ad_streams.get("vidaara_url"):
                        if not ruby:
                            ruby = ad_streams["vidaara_url"]
                        else:
                            pg = ad_streams["vidaara_url"]
                    vidmoly = ad_streams.get("vidmoly_url")
                elif mode in ("all", "both", "toonstream"):
                    ruby, mg, pg = extract_toonstream_all_servers(
                        show_name, season=season, episode=ep, direct_url=ts_url or ad_url
                    )
                elif mode == "satoru":
                    mg = resolve_satoru_episode(
                        show_name, season=season, episode=ep, direct_url=sat_url
                    )
                if mode in ("all", "both", "multimovies"):
                    multi = resolve_multimovies_episode(
                        show_name, season=season, episode=ep, direct_url=mm_url
                    )
                if not mg and mode in ("all", "both"):
                    mg = resolve_satoru_episode(
                        show_name, season=season, episode=ep, direct_url=sat_url
                    )
                if mode in ("all", "both") and ad_url:
                    ad_streams = extract_animedekho_all_streams(
                        show_name, season=season, episode=ep, direct_url=ad_url
                    )
                    if not ruby and ad_streams.get("ravok_url"):
                        ruby = ad_streams["ravok_url"]
                    if not pg and ad_streams.get("vidaara_url"):
                        pg = ad_streams["vidaara_url"]
                    if not vidmoly and ad_streams.get("vidmoly_url"):
                        vidmoly = ad_streams["vidmoly_url"]
                if mode in ("all", "both", "blissey", "blisseyhusband") or (mode == "all" and bh_url):
                    bh = extract_blisseyhusband_episode(
                        show_name, season=season, episode=ep, direct_url=bh_url
                    )
                if mode in ("all", "both", "byse", "dorabash") or (mode == "all" and db_url):
                    db = extract_byse_episode(
                        show_name, season=season, episode=ep, direct_url=db_url, encrypt=False
                    )
                is_anihosts = False
                if mode in ("all", "both", "anihosts", "kototv", "anineko") or (mode == "all" and an_url):
                    use_kototv = (mode in ("kototv", "anineko")) or (an_url and "kototv" in an_url.lower())
                    if not use_kototv:
                        is_anihosts = True
                        if an_lang == "all" or an_server in ("All Servers", "all"):
                            all_ah = {}
                            langs_to_check = ["sub", "dub"] if an_lang == "all" else [an_lang]
                            for l_k in langs_to_check:
                                s_dict = get_available_anihosts_servers(show_name, season=season, episode=ep, lang=l_k, direct_url=an_url)
                                if s_dict:
                                    if an_server not in ("All Servers", "all"):
                                        pref_clean = an_server.lower().replace("server", "").strip()
                                        s_dict = {sn: sl for sn, sl in s_dict.items() if pref_clean in sn.lower()}
                                    if s_dict:
                                        all_ah[l_k] = s_dict
                            an = all_ah if all_ah else None
                        else:
                            an = extract_anihosts_episode(
                                show_name, season=season, episode=ep, lang=an_lang, preferred_server=an_server, direct_url=an_url
                            )
                        # Fallback to KotoTV if AniHosts returned nothing and no direct AniHosts URL was specified
                        if not an and mode in ("all", "both") and not an_url:
                            if an_lang == "all" or an_server in ("All Servers", "all"):
                                all_kt = {}
                                langs_to_check = ["sub", "dub"] if an_lang == "all" else [an_lang]
                                for l_k in langs_to_check:
                                    s_dict = get_available_kototv_servers(show_name, season=season, episode=ep, lang=l_k, direct_url=an_url)
                                    if s_dict:
                                        all_kt[l_k] = s_dict
                                if all_kt:
                                    an = all_kt
                                    is_anihosts = False
                            else:
                                kt_res = extract_kototv_episode(
                                    show_name, season=season, episode=ep, lang=an_lang, preferred_server=an_server, direct_url=an_url
                                )
                                if kt_res:
                                    an = kt_res
                                    is_anihosts = False
                    else:
                        is_anihosts = False
                        if an_lang == "all" or an_server in ("All Servers", "all"):
                            all_kt = {}
                            langs_to_check = ["sub", "dub"] if an_lang == "all" else [an_lang]
                            for l_k in langs_to_check:
                                s_dict = get_available_kototv_servers(show_name, season=season, episode=ep, lang=l_k, direct_url=an_url)
                                if s_dict:
                                    if an_server not in ("All Servers", "all"):
                                        s_dict = {sn: sl for sn, sl in s_dict.items() if an_server.lower() in sn.lower()}
                                    if s_dict:
                                        all_kt[l_k] = s_dict
                            an = all_kt if all_kt else None
                        else:
                            an = extract_kototv_episode(
                                show_name, season=season, episode=ep, lang=an_lang, preferred_server=an_server, direct_url=an_url
                            )

                for q, links in downloads_map.items():
                    if ep - 1 < len(links):
                        ep_dls[q] = links[ep - 1]
                return ep, ruby, multi, mg, pg, an, ep_dls, vidmoly, bh, db, is_anihosts

            results = {}
            done_count = 0
            total = len(ep_nums)
            # Limit to 4 outer workers — each triggers up to 4 inner iframe workers,
            # so max ~16 concurrent requests to Toonstream/AnimeDekho. More causes rate-limiting.
            max_w = min(4, total)
            with ThreadPoolExecutor(max_workers=max_w) as executor:
                futures = [executor.submit(_worker_task, ep) for ep in ep_nums]
                for fut in as_completed(futures):
                    try:
                        ep, ruby, multi, mg, pg, an, ep_dls, vidmoly, bh, db, is_ah = fut.result()
                        results[ep] = (ruby, multi, mg, pg, an, ep_dls, vidmoly, bh, db, is_ah)
                        done_count += 1
                        found = bool(ruby or multi or mg or pg or an or vidmoly or bh or db)
                        status = "✅" if found else "⚠️"
                        self._log(
                            f"{status} S{season}E{ep} - {done_count}/{total} extracted",
                            "success" if found else "warn",
                        )
                    except Exception as exc:
                        done_count += 1
                        logger.warning("Extract links worker error: %s", exc)
                        self._log(f"⚠️ Error on episode - {done_count}/{total}", "warn")

            output_lines = []
            found_count = 0
            for ep in sorted(results.keys()):
                ruby, multi, mg, pg, an, ep_dls, vidmoly, bh, db, is_ah = results[ep]
                label = f"S{season}E{ep}"

                if mode == "vidmoly":
                    if vidmoly:
                        output_lines.append(f"{label}: {vidmoly}")
                        found_count += 1
                    else:
                        output_lines.append(f"{label}: ❌ Not found")
                elif mode in ("toonstream", "animedekho"):
                    if ruby:
                        output_lines.append(f"{label} [Server UG]: {ruby}")
                        found_count += 1
                    if pg:
                        output_lines.append(f"{label} [Server PG]: {pg}")
                        found_count += 1
                    if not ruby and not pg:
                        output_lines.append(f"{label}: ❌ Not found")
                elif mode == "multimovies":
                    if multi:
                        output_lines.append(f"{label}: {multi}")
                        found_count += 1
                    else:
                        output_lines.append(f"{label}: ❌ Not found")
                elif mode == "satoru":
                    if mg:
                        output_lines.append(f"{label}: {mg}")
                        found_count += 1
                    else:
                        output_lines.append(f"{label}: ❌ Not found")
                elif mode in ("blissey", "blisseyhusband"):
                    if bh:
                        output_lines.append(f"{label} [Server BH (BlisseyHusband)]: {bh}")
                        found_count += 1
                    else:
                        output_lines.append(f"{label}: ❌ Not found")
                elif mode in ("byse", "dorabash"):
                    if db:
                        output_lines.append(f"{label} [Server DB (Byse)]: {db}")
                        found_count += 1
                    else:
                        output_lines.append(f"{label}: ❌ Not found")
                elif mode == "anihosts":
                    if isinstance(an, dict):
                        for l_key, s_dict in an.items():
                            for s_name, s_link in s_dict.items():
                                found_count += 1
                                output_lines.append(f"{label} [AH {l_key.upper()} - {s_name}]: {s_link}")
                    elif an:
                        found_count += 1
                        output_lines.append(f"{label} [AH {an_lang.upper()} - {an_server}]: {an}")
                    else:
                        output_lines.append(f"{label}: ❌ Not found")
                elif mode in ("kototv", "anineko"):
                    if isinstance(an, dict):
                        for l_key, s_dict in an.items():
                            for s_name, s_link in s_dict.items():
                                found_count += 1
                                output_lines.append(f"{label} [KT {l_key.upper()} - {s_name}]: {s_link}")
                    elif an:
                        found_count += 1
                        output_lines.append(f"{label} [KT {an_lang.upper()} - {an_server}]: {an}")
                    else:
                        output_lines.append(f"{label}: ❌ Not found")
                else:  # all or both
                    ts_part = ruby if ruby else "❌ Not found"
                    mm_part = multi if multi else "❌ Not found"
                    mg_part = mg if mg else "❌ Not found"
                    pg_part = pg if pg else "❌ Not found"
                    if ruby or multi or mg or pg:
                        found_count += 1
                    output_lines.append(f"{label} [Server UG]: {ts_part}")
                    output_lines.append(f"{label} [Server UG2]: {mm_part}")
                    if mg:
                        output_lines.append(f"{label} [Server MG (Satoru)]: {mg_part}")
                    if pg:
                        output_lines.append(f"{label} [Server PG (Vidara)]: {pg_part}")
                    if vidmoly:
                        found_count += 1
                        output_lines.append(f"{label} [Vidmoly (AnimeDekho)]: {vidmoly}")
                    if bh:
                        found_count += 1
                        output_lines.append(f"{label} [Server BH (BlisseyHusband)]: {bh}")
                    if db:
                        found_count += 1
                        output_lines.append(f"{label} [Server DB (Byse)]: {db}")
                    
                    if isinstance(an, dict):
                        tag_name = "Server AH" if is_ah else "Server KT"
                        for l_key, s_dict in an.items():
                            for s_name, s_link in s_dict.items():
                                found_count += 1
                                output_lines.append(f"{label} [{tag_name} {l_key.upper()} - {s_name}]: {s_link}")
                    elif an:
                        tag_name = "Server AH" if is_ah else "Server KT"
                        found_count += 1
                        output_lines.append(f"{label} [{tag_name} ({an_lang.upper()}/{an_server})]: {an}")
                    else:
                        output_lines.append(f"{label} [Server AH]: ❌ Not found")

                # Append download links if any
                for q, dl_link in ep_dls.items():
                    output_lines.append(f"{label} [DL-{q}]: {dl_link}")

            self.after(0, lambda: self._ext_box.delete("1.0", "end"))
            if output_lines:
                text_out = "\n".join(output_lines) + "\n"
                self.after(0, lambda: self._ext_box.insert("end", text_out))
                self._log(f"✅ Extracted {len(output_lines)} stream link(s) for {show_name}", "success")
            else:
                self.after(0, lambda: self._ext_box.insert("end", "# No streams found.\n"))
                self._log(f"⚠️ No stream links found for {show_name}", "warn")

        except Exception as exc:
            logger.exception("_extract_links_only_worker crashed")
            self._log(f"💥 Extract error: {exc}", "error")
        finally:
            self.after(0, lambda: self._ext_extract_btn.configure(
                state="normal", text="⚡  Extract Links"
            ))

    def _copy_extracted_links_only(self):
        """
        Copy ONLY raw stream URLs to system clipboard.
        Strips any episode tags, headers, or prefixes (S2E1: etc.)
        """
        raw_text = self._ext_box.get("1.0", "end").strip()
        if not raw_text:
            messagebox.showwarning("Empty", "No extracted links to copy.")
            return

        urls = re.findall(r"https?://[^\s]+", raw_text)
        if not urls:
            messagebox.showwarning("No URLs", "No stream URLs found in output box.")
            return

        clean_text = "\n".join(urls)
        self.clipboard_clear()
        self.clipboard_append(clean_text)
        self.update()

        messagebox.showinfo("Copied", f"Copied {len(urls)} link(s) to clipboard!\n(No episode tag prefixes)")

    def _force_update_streams(
        self,
        api,
        show_name: str,
        season: int,
        expected_eps: int | None,
        anilist_status: str | None,
        delay: float,
        is_movie: bool = False,
        downloads_map: dict | None = None,
        manual_multimovies: str = "",
    ):
        """Directly update streams for an existing WP post, even if it is not in the airing list."""
        import re
        from stream_extractor import (
            fetch_all_episodes_data,
            generate_episode_toggle_html,
            count_toonstream_episodes,
            count_multimovies_episodes,
            extract_toonstream_all_servers,
            resolve_multimovies_episode,
            resolve_satoru_episode,
        )
        from wp_api import parse_existing_post_streams, parse_existing_post_downloads
        if not is_movie:
            is_movie = bool(expected_eps and expected_eps <= 1 and season == 1) if expected_eps else False

        post = api.lookup_post_for_update(show_name, season, is_movie=is_movie)
        if not post:
            self._log(f"     Could not locate existing post on WordPress for {show_name} S{season}", "warn")
            return

        post_id = post["id"]
        content = post["content"]
        content_raw = post.get("content_raw", "") or content
        categories = post.get("categories", [])

        existing_streams = parse_existing_post_streams(content)
        existing_dls = parse_existing_post_downloads(content)

        # Extract ep1 FIRST — this warms up the show URL cache on both Toonstream and Multimovies,
        # so the count step below is fast (reads cached page, not a fresh search).
        ep1_ruby, ep1_mg, ep1_pg = extract_toonstream_all_servers(show_name, season=season, episode=1, is_movie=is_movie)
        if not ep1_mg and not is_movie:
            ep1_mg = resolve_satoru_episode(show_name, season=season, episode=1)
        ep1_multi = resolve_multimovies_episode(show_name, season=season, episode=1, is_movie=is_movie)

        if is_movie:
            total_eps = 1
        else:
            # Smart count: only count from the source that ep1 succeeded on — avoids 60-180s blind searches.
            total_eps = 0
            if ep1_ruby:
                total_eps = count_toonstream_episodes(show_name, season)
            if not total_eps and ep1_multi:
                # show URL is cached from ep1, so this is a fast page fetch (no search)
                total_eps = count_multimovies_episodes(show_name, season=season, direct_url=manual_multimovies)
            if not total_eps:
                total_eps = expected_eps if expected_eps else 1
            self._log(f"     📊 Episode count: {total_eps}", "muted")

        self._log(f"     🎬 Re-extracting streams for {show_name} S{season} (ep 1-{total_eps})…", "muted")

        _dl_map = downloads_map or {}
        newly_fetched = {}
        if ep1_ruby or ep1_multi or ep1_mg or ep1_pg:
            newly_fetched[1] = {
                "episode_num": 1,
                "ruby_url": ep1_ruby,
                "multi_url": ep1_multi,
                "mg_url": ep1_mg,
                "pg_url": ep1_pg,
            }
        if total_eps > 1:
            remaining = fetch_all_episodes_data(show_name, season, total_eps, start_from=2, multimovies_url=manual_multimovies)
            for item in remaining:
                newly_fetched[item.get("episode_num", 1)] = item

        # Merge newly fetched with existing streams and downloads
        all_ep_nums = sorted(set(list(existing_streams.keys()) + list(newly_fetched.keys()) + list(existing_dls.keys())))
        if not all_ep_nums and total_eps > 0:
            all_ep_nums = list(range(1, total_eps + 1))

        episodes_data = []
        for ep_num in all_ep_nums:
            ex = existing_streams.get(ep_num, {})
            nw = newly_fetched.get(ep_num, {})

            ruby_url  = nw.get("ruby_url")  or ex.get("ruby_url",  "") or None
            ag_url    = nw.get("ag_url")    or ex.get("ag_url",    "") or None
            multi_url = nw.get("multi_url") or ex.get("multi_url", "") or None
            mg_url    = nw.get("mg_url")    or ex.get("mg_url",    "") or None
            pg_url    = nw.get("pg_url")    or ex.get("pg_url",    "") or None

            # Downloads: fresh from downloads_map if provided, otherwise preserve existing
            ep_downloads = {
                q: links[ep_num - 1]
                for q, links in _dl_map.items()
                if ep_num - 1 < len(links)
            }
            if not ep_downloads and ep_num in existing_dls:
                ep_downloads = dict(existing_dls[ep_num])

            if ruby_url or ag_url or multi_url or mg_url or pg_url or ep_downloads:
                episodes_data.append({
                    "episode_num": ep_num,
                    "ruby_url":    ruby_url,
                    "ag_url":      ag_url,
                    "multi_url":   multi_url,
                    "mg_url":      mg_url,
                    "pg_url":      pg_url,
                    "downloads":   ep_downloads,
                })

        active_eps = [e for e in episodes_data if e.get("ruby_url") or e.get("ag_url") or e.get("multi_url") or e.get("mg_url") or e.get("pg_url") or e.get("downloads")]
        if not active_eps:
            self._log(f"     ⚠️ No streams or downloads found for {show_name} S{season}", "warn")
            return

        latest_ep_num = max((e.get("episode_num", 1) for e in active_eps), default=0)
        found_ruby  = sum(1 for e in episodes_data if e.get("ruby_url"))
        found_ag    = sum(1 for e in episodes_data if e.get("ag_url"))
        found_multi = sum(1 for e in episodes_data if e.get("multi_url"))
        found_mg    = sum(1 for e in episodes_data if e.get("mg_url"))
        found_pg    = sum(1 for e in episodes_data if e.get("pg_url"))

        all_cats_check   = api.get_all_categories()
        cat_id_to_name_c = {v: k.lower() for k, v in all_cats_check.items()}
        post_has_completed = any("completed" in cat_id_to_name_c.get(cid, "") for cid in categories)
        old_latest_ep = max(existing_streams.keys(), default=0)
        has_new_ep = latest_ep_num > old_latest_ep

        _anilist_fin = (anilist_status is not None and anilist_status.upper() in ("FINISHED", "CANCELLED"))
        if is_movie:
            is_airing = False
        elif post_has_completed and not has_new_ep:
            is_airing = False
        elif anilist_status and anilist_status.upper() == "RELEASING":
            is_airing = True
        elif (
            _anilist_fin
            and expected_eps and expected_eps > 0
            and latest_ep_num >= expected_eps
        ):
            is_airing = False
        elif expected_eps and expected_eps > 0 and latest_ep_num < expected_eps:
            is_airing = True
        elif not expected_eps or expected_eps <= 0:
            is_airing = True
        elif latest_ep_num <= 1 and not (_anilist_fin and total_eps == 1):
            is_airing = True
        else:
            is_airing = not post_has_completed
        is_completed = not is_airing

        if is_movie:
            final_title = show_name
        elif is_airing and latest_ep_num > 0:
            final_title = f"{show_name} (Season {season}) - Episode {latest_ep_num} Added!"
        else:
            final_title = f"{show_name} (Season {season})"

        all_cats_map = api.get_all_categories()
        cat_id_to_name = {v: k.lower() for k, v in all_cats_map.items()}
        airing_status_ids = {cid for cid, nm in cat_id_to_name.items() if any(k in nm for k in ("airing", "ongoing"))}
        completed_status_ids = {cid for cid, nm in cat_id_to_name.items() if "completed" in nm}

        existing_cat_ids = set(categories)
        if is_completed:
            existing_cat_ids -= airing_status_ids
            if completed_status_ids:
                existing_cat_ids |= completed_status_ids
        else:
            existing_cat_ids -= completed_status_ids
            if airing_status_ids:
                existing_cat_ids |= airing_status_ids

        cat_ids = sorted(existing_cat_ids)

        toggle_html = generate_episode_toggle_html(episodes_data, series_name=show_name, season_num=season)

        sc_pattern   = r'\[toggle title="Episode List"[^\]]*\].*?\[/toggle\]'
        html_pattern = r'<div class="toggle[^"]*">[\s\S]*?<h3[^>]*>\s*Episode List[\s\S]*?</div>\s*</div>\s*</div>'

        if re.search(sc_pattern, content_raw, flags=re.DOTALL | re.IGNORECASE):
            updated_content = re.sub(sc_pattern, toggle_html, content_raw, flags=re.DOTALL | re.IGNORECASE)
        elif re.search(html_pattern, content, flags=re.DOTALL | re.IGNORECASE):
            updated_content = re.sub(html_pattern, toggle_html, content_raw, flags=re.DOTALL | re.IGNORECASE)
        else:
            updated_content = content_raw + "\n\n" + toggle_html

        if api.update_post_content(post_id, updated_content, new_title=final_title, new_category_ids=cat_ids):
            status_str = "Airing" if is_airing else "Completed"
            self._log(
                f"     ✅ UPDATED: '{final_title}' | Status: {status_str} - "
                f"Ruby: {found_ruby}/{latest_ep_num} AG: {found_ag}/{latest_ep_num} UG2: {found_multi}/{latest_ep_num} MG: {found_mg}/{latest_ep_num} PG: {found_pg}/{latest_ep_num}",
                "success",
            )

    # ------ Worker thread ------------------------------------------------------------------------------------------------------------------------------------------------------------------

    def _sync_worker(
        self,
        api: WordPressAPI,
        test_mode: bool,
        test_pages: int,
        tmdb_key: str,
        default_score: int,
        delay: float,
        subtitle_key: str = "_subtitle",
    ):
        import time as _time

        try:
            self._log("📡 Loading existing WordPress post titles…")
            existing = api.get_all_post_titles()
            self._log(f"   Found {len(existing)} existing posts on your site.")

            # Determine page range
            if test_mode:
                total_pages = test_pages
                self._log(f"⚠️ Test mode: scanning only {test_pages} page(s).")
            else:
                self._log("📡 Detecting total pages on toonworld4all.me…")
                total_pages = get_total_pages()
                self._log(f"   Total pages: {total_pages}")

            def progress_cb(current, total):
                pct = current / total
                self._queue_progress(pct, f"Page {current}/{total}", "")

            for post_data in iter_all_posts(
                stop_flag=self._stop_flag,
                progress_callback=progress_cb,
                page_start=1,
                page_end=total_pages,
            ):
                if self._stop_flag[0]:
                    break

                show_name = post_data.get("show_name", "")
                season    = post_data.get("season", 1)
                c_type    = post_data.get("content_type", "show")
                is_movie  = post_data.get("is_movie", False) or c_type == "movie"

                if not show_name:
                    continue

                self._cnt_total += 1
                self._queue_stat_update()

                # Check existence
                if api.post_exists(show_name, season, is_movie=is_movie):
                    self._cnt_skipped += 1
                    self._queue_stat_update()
                    if is_movie:
                        self._log(f"   Exists: {show_name}", "muted")
                    else:
                        self._log(f"   Exists: {show_name} (Season {season})", "muted")
                    continue

                if is_movie:
                    self._log(f"📥 New: {show_name} [{c_type}]")
                else:
                    self._log(f"📥 New: {show_name} (Season {season}) [{c_type}]")

                # Fetch show metadata (passing exact season for accurate total episodes)
                meta = get_show_metadata(show_name, c_type, tmdb_key, season=season)
                score = meta.get("score")
                if score is None:
                    score = default_score
                    self._log(f"   Rating: using fallback {score}", "muted")
                else:
                    self._log(f"   Rating: {score}/100", "muted")

                expected_eps = meta.get("expected_episodes")
                anilist_status = meta.get("status")

                # Create draft
                success, edit_url, message, post_id = api.create_draft_post(
                    post_data,
                    taqyeem_score=score,
                    tmdb_api_key=tmdb_key,
                    subtitle_meta_key=subtitle_key,
                )

                if success:
                    self._cnt_created += 1
                    if is_movie:
                        self._log(f"✅ Draft created: {show_name}", "success")
                    else:
                        self._log(f"✅ Draft created: {show_name} (S{season})", "success")

                    # ------ Extract & Encrypt episode stream links ---------------------------------
                    if post_id and self.settings.get("extract_episode_streams", True):
                        try:
                            from stream_extractor import (
                                fetch_all_episodes_data,
                                generate_episode_toggle_html,
                                count_toonstream_episodes,
                                extract_toonstream_all_servers,
                                resolve_multimovies_episode,
                                resolve_satoru_episode,
                            )

                            # ------ Pre-check: verify at least episode 1 exists on either source ------
                            self._log(f"   🔍 Checking stream availability for {show_name}…", "muted")
                            ep1_ruby, ep1_mg, ep1_pg = extract_toonstream_all_servers(show_name, season=season, episode=1, is_movie=is_movie)
                            if not ep1_mg and not is_movie:
                                ep1_mg = resolve_satoru_episode(show_name, season=season, episode=1)
                            ep1_multi = resolve_multimovies_episode(show_name, season=season, episode=1, is_movie=is_movie)

                            if not ep1_ruby and not ep1_multi and not ep1_mg and not ep1_pg:
                                # No streams at all --- delete the draft we just created and skip
                                self._log(
                                    f"     ⚠️ No streams found on Toonstream or Multimovies for {show_name} (S{season}) - removing draft",
                                    "warn",
                                )
                                try:
                                    api.session.delete(
                                        f"{api.base}/posts/{post_id}",
                                        params={"force": True},
                                        timeout=10,
                                    )
                                except Exception:
                                    pass
                                self._cnt_created -= 1
                                self._cnt_errors  += 1
                                self._queue_stat_update()
                                continue

                            if is_movie:
                                total_eps = 1
                            else:
                                total_eps = count_toonstream_episodes(show_name, season)
                                if not total_eps or total_eps <= 0:
                                    if expected_eps and expected_eps > 0:
                                        total_eps = expected_eps
                                    else:
                                        ep_raw = post_data.get("episodes", "")
                                        if ep_raw:
                                            nums = re.findall(r'\d+', str(ep_raw))
                                            if nums:
                                                total_eps = max(1, int(nums[0]))
                                        else:
                                            total_eps = 1

                            self._log(
                                f"   🎬 Extracting streams for {total_eps} episode(s)…",
                                "muted",
                            )

                            episodes_data = []
                            if ep1_ruby or ep1_multi or ep1_mg or ep1_pg:
                                episodes_data.append({"episode_num": 1, "ruby_url": ep1_ruby, "multi_url": ep1_multi, "mg_url": ep1_mg, "pg_url": ep1_pg})

                            if total_eps > 1 and not is_movie:
                                remaining = fetch_all_episodes_data(show_name, season, total_eps, start_from=2)
                                episodes_data.extend(remaining)

                            active_eps = [e for e in episodes_data if e.get("ruby_url") or e.get("ag_url") or e.get("multi_url") or e.get("mg_url") or e.get("pg_url")]
                            found_ruby  = sum(1 for e in episodes_data if e.get("ruby_url"))
                            found_ag    = sum(1 for e in episodes_data if e.get("ag_url"))
                            found_multi = sum(1 for e in episodes_data if e.get("multi_url"))
                            found_mg    = sum(1 for e in episodes_data if e.get("mg_url"))
                            found_pg    = sum(1 for e in episodes_data if e.get("pg_url"))
                            latest_ep_num = max((e.get("episode_num", 1) for e in active_eps), default=0)

                            _anilist_fin = (anilist_status is not None and anilist_status.upper() in ("FINISHED", "CANCELLED"))
                            if is_movie:
                                is_airing = False
                            elif anilist_status and anilist_status.upper() == "RELEASING":
                                is_airing = True
                            elif (
                                _anilist_fin
                                and expected_eps and expected_eps > 0
                                and latest_ep_num >= expected_eps
                            ):
                                is_airing = False
                            elif expected_eps and expected_eps > 0 and latest_ep_num < expected_eps:
                                is_airing = True
                            elif not expected_eps or expected_eps <= 0:
                                is_airing = True
                            elif latest_ep_num <= 1 and not (_anilist_fin and total_eps == 1):
                                is_airing = True
                            else:
                                is_airing = True
                            is_completed = not is_airing

                            if is_movie:
                                m_year = post_data.get("org_run") or meta.get("year") or ""
                                if m_year:
                                    post_data["org_run"] = m_year
                                if m_year and not re.search(r'\b(19|20)\d{2}\b', show_name):
                                    final_title = f"{show_name} ({m_year})"
                                else:
                                    final_title = show_name
                            elif is_airing and latest_ep_num > 0:
                                final_title = f"{show_name} (Season {season}) - Episode {latest_ep_num} Added!"
                            else:
                                final_title = f"{show_name} (Season {season})"

                            post_data["is_airing"] = is_airing
                            post_data["is_completed"] = is_completed
                            cat_keywords = api.infer_category_keywords(post_data)
                            cat_ids = api.match_categories(cat_keywords)

                            toggle_html = generate_episode_toggle_html(
                                episodes_data,
                                series_name=show_name,
                                season_num=season,
                            )
                            base_content = api.build_post_content(post_data)

                            pattern = r'\[toggle title="Episode List".*?\[/toggle\]'
                            updated_content = re.sub(pattern, toggle_html, base_content, flags=re.DOTALL)

                            if api.update_post_content(
                                post_id,
                                updated_content,
                                new_title=final_title,
                                new_category_ids=cat_ids,
                            ):
                                status_str = "Airing" if is_airing else "Completed"
                                self._log(
                                    f"   📌 Title: '{final_title}' | Status: {status_str} - "
                                    f"Ruby: {found_ruby}/{total_eps}  "
                                    f"Server UG2: {found_multi}/{total_eps}  "
                                    f"Server MG: {found_mg}/{total_eps}  "
                                    f"Server PG: {found_pg}/{total_eps}",
                                    "success",
                                )
                        except Exception as exc:
                            self._log(f"   ⚠️ Stream extraction note: {exc}", "muted")

                    self._queue_result({
                        "title": show_name,
                        "season": season,
                        "type": c_type,
                        "score": score,
                        "status": "Draft",
                        "edit_url": edit_url,
                    })
                else:
                    self._cnt_errors += 1
                    self._log(f" Error: {message}", "error")

                self._queue_stat_update()
                _time.sleep(delay)

            # ------ Monitor existing airing posts for updates ------------------------------------------------------------
            if self.settings.get("extract_episode_streams", True):
                self._log("\nð- Checking existing Airing posts on WordPress for new episodes & server updates¦", "info")
                check_and_update_airing_posts(api, self._effective_settings(), log_fn=self._log)

        except Exception as exc:
            logger.exception("Sync worker crashed")
            self._log(f"ð¥ Fatal error: {exc}", "error")

        finally:
            self._queue_progress(1.0, "Sync complete", "")
            self._log(
                f"\nð Done - Created: {self._cnt_created} | "
                f"Skipped: {self._cnt_skipped} | Errors: {self._cnt_errors}"
            )
            self.after(0, self._on_sync_done)

    def _on_sync_done(self):
        self._start_btn.configure(state="normal")
        self._stop_btn.configure(state="disabled")
        self._apply_settings_lock(False)

    # ------ Queue helpers (thread-safe UI updates) ---------------------------------------------------------------------------------------

    def _log(self, msg: str, level: str = "info"):
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_queue.put(("log", f"[{ts}] {msg}", level))

    def _queue_stat_update(self):
        self._log_queue.put(("stats", None, None))

    def _queue_progress(self, pct: float, label: str, sub: str):
        self._log_queue.put(("progress", pct, label))

    def _queue_result(self, result: dict):
        self._results.append(result)
        self._log_queue.put(("result", result, None))

    def _poll_log_queue(self):
        """Drain the queue and update the UI. Runs every 80ms on the main thread."""
        try:
            while True:
                item = self._log_queue.get_nowait()
                kind = item[0]

                if kind == "log":
                    _, msg, level = item
                    color_map = {
                        "success": C_SUCCESS,   # Bright Green (#00FF7F)
                        "error":   C_ERROR,     # Bright Red (#FF3344)
                        "warn":    C_WARN,      # Bright Orange (#FFA500)
                        "info":    C_INFO,      # Deep Sky Blue (#00BFFF)
                        "cyan":    C_CYAN,      # Electric Cyan (#00FFFF)
                        "purple":  C_PURPLE,    # Bright Purple (#BD93F9)
                        "muted":   C_MUTED,     # Slate Blue (#8892B0)
                    }
                    color = color_map.get(level, C_TEXT)
                    self._append_log(msg, color)

                elif kind == "stats":
                    self._stat_total.set(str(self._cnt_total))
                    self._stat_created.set(str(self._cnt_created))
                    self._stat_skipped.set(str(self._cnt_skipped))
                    self._stat_errors.set(str(self._cnt_errors))

                elif kind == "progress":
                    _, pct, label = item
                    self._progress_bar.set(pct)
                    self._progress_label.configure(text=label)

                elif kind == "result":
                    _, res, _ = item
                    self._tree.insert(
                        "", "end",
                        values=(res["title"], f"S{res['season']}",
                                res["type"], res["score"],
                                res["status"], res["edit_url"]),
                    )

        except queue.Empty:
            pass
        finally:
            self.after(80, self._poll_log_queue)

    def _append_log(self, msg: str, color: str = C_TEXT):
        """Append a coloured, well-spaced line to all log widgets."""
        for attr in ("_mini_log", "_full_log", "_dl_log_box"):
            widget = getattr(self, attr, None)
            if widget is None:
                continue
            try:
                tk_text = getattr(widget, "_textbox", widget)
                tk_text.configure(state="normal")

                # ── Timestamp prefix vs body line ─────────────────────────
                # Lines that start with "[HH:MM:SS]" get a faint separator
                # blank line prepended so each group is visually distinct.
                import re as _re
                is_timestamp = bool(_re.match(r'^\[\d{2}:\d{2}:\d{2}\]', msg))

                if is_timestamp:
                    # Insert a small blank separator before every timestamped entry
                    sep_tag = "sep_blank"
                    tk_text.tag_config(sep_tag, font=("Cascadia Code", 4))
                    sep_start = tk_text.index("end-1c")
                    tk_text.insert("end", "\n")
                    sep_end = tk_text.index("end-1c")
                    tk_text.tag_add(sep_tag, sep_start, sep_end)

                start_idx = tk_text.index("end-1c")
                tk_text.insert("end", msg + "\n")
                end_idx = tk_text.index("end-1c")

                tag = f"color_{color.replace('#', '')}"
                tk_text.tag_config(tag, foreground=color)
                tk_text.tag_add(tag, start_idx, end_idx)

                tk_text.see("end")
                tk_text.configure(state="disabled")
            except Exception:
                try:
                    widget.configure(state="normal")
                    widget.insert("end", msg + "\n")
                    widget.see("end")
                    widget.configure(state="disabled")
                except Exception:
                    pass

    def _clear_log(self):
        for attr in ("_mini_log", "_full_log"):
            widget = getattr(self, attr, None)
            if widget is not None:
                try:
                    widget.configure(state="normal")
                    widget.delete("1.0", "end")
                    widget.configure(state="disabled")
                except Exception:
                    pass

    # ------ Results helpers ------------------------------------------------------------------------------------------------------------------------------------------------------------

    def _open_result_link(self, event):
        sel = self._tree.selection()
        if not sel:
            return
        vals = self._tree.item(sel[0], "values")
        url = vals[5] if len(vals) > 5 else ""
        if url and url.startswith("http"):
            webbrowser.open(url)

    def _export_csv(self):
        import csv
        from tkinter.filedialog import asksaveasfilename

        path = asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile="wp_drafts.csv",
        )
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Title", "Season", "Type", "Score", "Status", "Edit URL"])
            for res in self._results:
                writer.writerow([
                    res["title"], res["season"], res["type"],
                    res["score"], res["status"], res["edit_url"],
                ])
        messagebox.showinfo("Exported", f"Results saved to:\n{path}")


# ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
#  Entry point
# ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

if __name__ == "__main__":
    app = AutoPosterApp()
    app.mainloop()
