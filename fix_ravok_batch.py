"""
fix_ravok_batch.py
Reads discovered_dead_ravok.json, resolves working AnimeDekho ravok.buzz stream,
replaces them in the WordPress post content, and saves fixed status to fixed_ravok_streams.json.
- Strictly keeps orig_date and orig_date_gmt (update_date=False)
- Only replaces Server UG ravok.buzz streams with working AnimeDekho ravok.buzz streams
- Never touches any other stream server or post metadata
"""

import sys
import os
import json
import re
import base64
import logging
import requests
from typing import Optional
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fix_batch")

SETTINGS_FILE = Path(__file__).parent / "settings.json"
with open(SETTINGS_FILE, encoding="utf-8") as f:
    settings = json.load(f)

from wp_api import WordPressAPI
from stream_extractor import (
    ANIMEDEKHO_URL,
    _animedekho_session,
    search_animedekho_series,
    extract_animedekho_all_streams,
    extract_animedekho_stream,
)

DISCOVERED_FILE = Path(__file__).parent / "discovered_dead_ravok.json"
FIXED_FILE = Path(__file__).parent / "fixed_ravok_streams.json"

def decode_worker_token(token: str) -> dict:
    pad = len(token) % 4
    if pad:
        token += "=" * (4 - pad)
    try:
        return json.loads(base64.b64decode(token).decode("utf-8"))
    except Exception:
        return {}

def encode_worker_token(payload: dict) -> str:
    raw = json.dumps(payload, separators=(',', ':'))
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")

def is_working_ravok(url: str) -> bool:
    """Verify that a replacement ravok URL is active and not returning 'Video not found.'"""
    if not url or "ravok.buzz" not in url:
        return False
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        return (r.status_code == 200) and ("video not found" not in r.text.lower())
    except Exception:
        return False

def clean_show_name(raw_title: str) -> str:
    name = re.sub(r'[\s\ufffd\u2013\u2014\-–—]+(Episode\s*\d+\s*Added!|All\s*Episodes\s*Added!).*$', '', raw_title, flags=re.IGNORECASE)
    name = re.sub(r'\s*\(Season\s*\d+\)', '', name, flags=re.IGNORECASE)
    name = name.replace('\ufffdm', "'m").replace('\ufffds', "'s").replace('Fianc\ufffd', 'Fiance')
    name = re.sub(r'&#8211;|–|—', '-', name)
    name = re.sub(r'&#8217;|’|\ufffd', "'", name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name

def resolve_series_url(clean_name: str) -> Optional[str]:
    base = ANIMEDEKHO_URL.rstrip("/")
    slug = re.sub(r'[^a-z0-9]+', '-', clean_name.lower()).strip('-')

    # Known specific slug aliases - prioritize these to avoid fuzzy mismatch
    alias_probes = []
    if "bleach" in slug:
        alias_probes.extend(["bleach", "bleach-hindi"])
    if "dating-sim" in slug:
        alias_probes.append("trapped-in-a-dating-sim-the-world-of-otome-games-is-tough-for-mobs")
    if "fiance" in slug or "fianc" in slug:
        alias_probes.extend([
            "even-though-im-a-super-timid-noble-girl-i-accepted-the-bet-from-my-cunning-fiance",
            "even-though-im-a-super-timid-noble-girl-i-accepted-the-bet-from-my-cunning-fianc"
        ])
    if "captain-tsubasa" in slug:
        alias_probes.append("captain-tsubasa")
    if "lord-of-mysteries" in slug:
        alias_probes.append("lord-of-mysteries")

    for p in alias_probes:
        for path in [f"/series-hindi/{p}/", f"/series/{p}/"]:
            test_url = f"{base}{path}"
            try:
                r_try = _animedekho_session.get(test_url, timeout=10)
                if r_try.status_code == 200 and len(r_try.text) > 1000 and "<title>404" not in r_try.text:
                    return test_url
            except Exception:
                pass

    # 1. Direct search with full clean name
    s_url = search_animedekho_series(clean_name)
    if s_url:
        return s_url
    
    # 2. Try short name before colon or dash
    short_name = clean_name.split(":")[0].split("-")[0].strip()
    if short_name and short_name.lower() != clean_name.lower():
        s_url = search_animedekho_series(short_name)
        if s_url:
            return s_url

    # 3. Try first 3-4 words (useful for very long light novel titles)
    words = clean_name.split()
    if len(words) > 4:
        first_few = " ".join(words[:4])
        s_url = search_animedekho_series(first_few)
        if s_url:
            return s_url

    # 4. Direct slug probes on AnimeDekho
    probes = [slug, f"{slug}-hindi"]
    for p in probes:
        for path in [f"/series-hindi/{p}/", f"/series/{p}/"]:
            test_url = f"{base}{path}"
            try:
                r_try = _animedekho_session.get(test_url, timeout=10)
                if r_try.status_code == 200 and len(r_try.text) > 1000 and "<title>404" not in r_try.text:
                    return test_url
            except Exception:
                pass
    return None

def run_fix():
    if not DISCOVERED_FILE.exists():
        logger.error("No discovered_dead_ravok.json found!")
        return

    with open(DISCOVERED_FILE, "r", encoding="utf-8") as f:
        discovered = json.load(f)

    # Load already fixed items
    fixed_data = {}
    if FIXED_FILE.exists():
        try:
            with open(FIXED_FILE, "r", encoding="utf-8") as f:
                fixed_data = json.load(f)
        except Exception:
            pass

    api = WordPressAPI(
        site_url=settings["wp_url"],
        username=settings["wp_username"],
        app_password=settings["wp_app_password"],
    )

    # Group by post_id
    by_post = {}
    for item in discovered:
        by_post.setdefault(item["post_id"], []).append(item)

    logger.info("Starting fix for %d posts with %d dead streams...", len(by_post), len(discovered))

    total_fixed_count = 0

    for post_id, items in by_post.items():
        first = items[0]
        raw_title = first["title"]
        season = first["season"]
        clean_name = clean_show_name(raw_title)
        
        logger.info("--------------------------------------------------")
        logger.info("Processing Post %d: '%s' (Season %d) [%d dead streams]", post_id, clean_name, season, len(items))

        # Check if AnimeDekho has this series
        series_url = resolve_series_url(clean_name)
        if series_url:
            logger.info("Found AnimeDekho series URL: %s", series_url)
        else:
            logger.warning("AnimeDekho series NOT found for '%s'. Skipping for now.", clean_name)
            continue

        # Fetch latest post data from WordPress with retries
        resp = None
        for attempt in range(4):
            try:
                resp = api.session.get(f"{api.base}/posts/{post_id}", params={"context": "edit"}, timeout=20)
                if resp.status_code == 200:
                    break
            except Exception as e:
                logger.warning("Attempt %d fetching post %d from WP failed: %s", attempt + 1, post_id, e)
                import time; time.sleep(2)
        if not resp or resp.status_code != 200:
            logger.error("Failed to fetch post %d from WP: %s", post_id, getattr(resp, "status_code", "None"))
            continue

        p_data = resp.json()
        content = p_data.get("content", {}).get("raw", "") or p_data.get("content", {}).get("rendered", "")
        orig_date = p_data.get("date", "")
        orig_date_gmt = p_data.get("date_gmt", "")

        post_modified = False
        replacements_in_post = 0

        needed_items = [
            it for it in items
            if f"{post_id}_S{season}E{it['episode']}" not in fixed_data
        ]

        for it in items:
            k = f"{post_id}_S{season}E{it['episode']}"
            if k in fixed_data:
                logger.info("Already fixed in previous run: %s -> %s", k, fixed_data[k]["new_url"])

        if not needed_items:
            continue

        def resolve_single_item(item):
            ep = item["episode"]
            dead_url = item["dead_url"]
            ad_streams = extract_animedekho_all_streams(clean_name, season=season, episode=ep, direct_url=series_url)
            ravok_url = ad_streams.get("ravok_url")
            if not ravok_url or not is_working_ravok(ravok_url):
                ravok_url = extract_animedekho_stream(clean_name, season=season, episode=ep, direct_url=series_url)
            if ravok_url and is_working_ravok(ravok_url):
                return item, ravok_url
            return item, None

        logger.info("Resolving %d episodes concurrently with 8 workers...", len(needed_items))
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(resolve_single_item, needed_items))

        for item, ravok_url in results:
            ep = item["episode"]
            dead_url = item["dead_url"]
            fixed_key = f"{post_id}_S{season}E{ep}"

            if not ravok_url:
                logger.warning("No WORKING ravok.buzz stream on AnimeDekho for S%dE%d", season, ep)
                continue

            logger.info("SUCCESS: Found working AnimeDekho ravok stream for S%dE%d: %s", season, ep, ravok_url)

            # Find and replace in content
            matches = list(re.finditer(r'(\?data=([A-Za-z0-9+/=]+))', content))
            matched_and_replaced = False

            for m in matches:
                token = m.group(2)
                dec = decode_worker_token(token)
                if not dec:
                    continue
                
                # Check if this token matches our dead URL and Server UG
                if dec.get("u") == dead_url:
                    dec["u"] = ravok_url
                    new_token = encode_worker_token(dec)
                    
                    old_data_str = f"?data={token}"
                    new_data_str = f"?data={new_token}"
                    
                    content = content.replace(old_data_str, new_data_str, 1)
                    matched_and_replaced = True
                    break

            if matched_and_replaced:
                fixed_data[fixed_key] = {
                    "post_id": post_id,
                    "show_name": clean_name,
                    "season": season,
                    "episode": ep,
                    "old_url": dead_url,
                    "new_url": ravok_url,
                }
                post_modified = True
                replacements_in_post += 1
                total_fixed_count += 1
                logger.info("Replaced dead URL in content for S%dE%d", season, ep)
            else:
                logger.warning("Could not locate token for dead URL %s in post content", dead_url)

        # Update WordPress if modified
        if post_modified:
            ok = api.update_post_content(
                post_id=post_id,
                new_content=content,
                update_date=False,
                orig_date=orig_date,
                orig_date_gmt=orig_date_gmt,
            )
            if ok:
                logger.info("SAVED Post %d to WordPress with %d fixed streams (date preserved: %s)", post_id, replacements_in_post, orig_date)
                with open(FIXED_FILE, "w", encoding="utf-8") as f:
                    json.dump(fixed_data, f, indent=2)
            else:
                logger.error("Failed to update Post %d on WordPress!", post_id)

    logger.info("Batch fix complete. Total streams fixed this run: %d", total_fixed_count)

if __name__ == "__main__":
    run_fix()
