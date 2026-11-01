"""
fix_dead_ravok_streams.py
Scans WordPress posts for Server UG streams using ravok.buzz that return 'Video not found.'
Replaces only the verified dead ravok.buzz streams with working AnimeDekho ravok.buzz streams.
- Ignores other servers (Server UG2, Server MG, Server PG, etc.)
- Ignores Server UG streams that are not ravok.buzz or are already working
- Never touches post dates (preserves orig_date and orig_date_gmt, update_date=False)
- Strictly matches show_name, season, and episode number
"""

import sys
import os
import json
import re
import base64
import logging
import requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fix_ravok")

# Load settings
SETTINGS_FILE = Path(__file__).parent / "settings.json"
with open(SETTINGS_FILE, encoding="utf-8") as f:
    settings = json.load(f)

from wp_api import WordPressAPI
from stream_extractor import (
    search_animedekho_series,
    get_animedekho_episode_map,
    extract_animedekho_stream,
    extract_animedekho_all_streams,
)

_URL_CACHE = {}

def is_ravok_dead(url: str) -> bool:
    """Check if a ravok.buzz URL returns 'Video not found.' or error."""
    if not url or "ravok.buzz" not in url:
        return False
    if url in _URL_CACHE:
        return _URL_CACHE[url]
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        # If status != 200 or body says 'Video not found.'
        text_lower = r.text.lower()
        dead = (r.status_code != 200) or ("video not found" in text_lower)
        _URL_CACHE[url] = dead
        return dead
    except Exception as e:
        logger.debug("Error checking %s: %s", url, e)
        _URL_CACHE[url] = True
        return True

def decode_worker_token(token: str) -> dict:
    """Decode base64 worker token."""
    pad = len(token) % 4
    if pad:
        token += "=" * (4 - pad)
    try:
        return json.loads(base64.b64decode(token).decode("utf-8"))
    except Exception:
        return {}

def encode_worker_token(payload: dict) -> str:
    """Encode payload to base64 worker token."""
    raw = json.dumps(payload, separators=(',', ':'))
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")

def find_dead_streams_in_posts(api: WordPressAPI, max_posts: int = 0) -> list[dict]:
    """Scan all posts and list dead ravok.buzz streams."""
    logger.info("Fetching posts from WordPress...")
    posts = api.get_all_published_posts()
    logger.info("Total posts retrieved: %d", len(posts))
    
    dead_items = []
    
    for p in posts:
        post_id = p["id"]
        title = p["raw_title"]
        show_name = p["show_name"]
        season = p["season"]
        content = p["content_raw"]
        
        # Look for worker URLs in post content
        # Pattern matches data-url or src or href with ?data=
        matches = list(re.finditer(r'(\?data=([A-Za-z0-9+/=]+))', content))
        if not matches:
            continue
            
        for m in matches:
            full_match = m.group(0)
            token = m.group(2)
            dec = decode_worker_token(token)
            if not dec:
                continue
            
            target_url = dec.get("u", "")
            stream_title = dec.get("t", "")
            
            # Must strictly be Server UG
            if "Server_UG" not in stream_title and "Server UG" not in stream_title:
                continue
            
            # Must strictly be a ravok.buzz stream
            if "ravok.buzz" not in target_url:
                continue
            
            # Extract episode number from stream title (e.g. JWM_S1_E02_-_Server_UG or Episode 2)
            ep_match = re.search(r'E0*(\d+)', stream_title, re.IGNORECASE) or re.search(r'Episode\s*0*(\d+)', stream_title, re.IGNORECASE)
            if not ep_match:
                continue
            episode = int(ep_match.group(1))
            
            # Check if this ravok URL is actually dead
            if is_ravok_dead(target_url):
                logger.warning(
                    "Found DEAD ravok stream in Post %d (%s) S%dE%d: %s",
                    post_id, show_name, season, episode, target_url
                )
                dead_items.append({
                    "post_id": post_id,
                    "title": title,
                    "show_name": show_name,
                    "season": season,
                    "episode": episode,
                    "stream_title": stream_title,
                    "old_url": target_url,
                    "old_token": token,
                    "payload": dec,
                })
                
        if max_posts and len(dead_items) >= max_posts:
            break
            
    return dead_items

if __name__ == "__main__":
    api = WordPressAPI(
        site_url=settings["wp_url"],
        username=settings["wp_username"],
        app_password=settings["wp_app_password"],
    )
    dead = find_dead_streams_in_posts(api)
    logger.info("Scan complete: found %d dead ravok streams.", len(dead))
    for d in dead[:20]:
        print(f"Post {d['post_id']} | {d['show_name']} S{d['season']}E{d['episode']} | {d['old_url']}")
