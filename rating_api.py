"""
rating_api.py — Fetch real ratings (0–100) from AniList, TVMaze (IMDb), TMDB, and MyAnimeList (Jikan).
No API key required! (Uses TVMaze + AniList automatically; uses TMDB if API key is provided).
"""

import re
import time
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

ANILIST_URL = "https://graphql.anilist.co"
TVMAZE_BASE = "https://api.tvmaze.com"
JIKAN_BASE  = "https://api.jikan.moe/v4"
KITSU_BASE  = "https://kitsu.io/api/edge"
TMDB_BASE   = "https://api.themoviedb.org/3"

_jikan_last_call = 0.0
JIKAN_RATE_LIMIT = 0.5


def _clean_query_title(show_name: str) -> str:
    """Clean show title for external API search queries."""
    clean = re.sub(r"\(Season \d+\)", "", show_name, flags=re.IGNORECASE).strip()
    clean = re.sub(r"\bSeason \d+\b", "", clean, flags=re.IGNORECASE).strip()
    clean = re.sub(r"\bS\d{1,2}\b", "", clean, flags=re.IGNORECASE).strip()
    clean = re.sub(r"'", "'", clean)
    clean = re.sub(r"\s{2,}", " ", clean)
    return clean


def get_kitsu_details(show_name: str) -> dict:
    """
    Query Kitsu API (anime database) for episode count and status.
    Used as a fallback / cross-check when AniList returns wrong/missing data.
    Returns dict: {"episodes": Optional[int], "status": Optional[str]}
    """
    clean = _clean_query_title(show_name)
    res = {"episodes": None, "status": None}
    try:
        resp = requests.get(
            f"{KITSU_BASE}/anime",
            params={"filter[text]": clean, "page[limit]": 3},
            timeout=8,
            headers={"User-Agent": "WP-AutoPoster/1.0", "Accept": "application/vnd.api+json"},
        )
        if resp.status_code == 200:
            items = resp.json().get("data", [])
            for item in items:
                attr = item.get("attributes", {})
                eps = attr.get("episodeCount")
                st = attr.get("status", "")
                if eps:
                    res["episodes"] = int(eps)
                if st:
                    res["status"] = "FINISHED" if st.lower() in ("finished", "completed") else "RELEASING"
                if res["episodes"] and res["status"]:
                    logger.info("Kitsu details for '%s': eps=%s, status=%s", show_name, res["episodes"], res["status"])
                    return res
    except Exception as exc:
        logger.debug("Kitsu API error for '%s': %s", show_name, exc)
    return res


def get_anilist_details(show_name: str, season: int = 1, is_movie: bool = False) -> dict:
    """
    Query AniList GraphQL API for score (0-100), total expected episodes, status, and release year.
    If season > 1, queries with season tag first to fetch exact season metadata (e.g. Re:Zero Season 4).
    Returns dict: {"score": Optional[int], "episodes": Optional[int], "status": Optional[str], "year": Optional[str]}

    IMPORTANT: Always queries with type: ANIME to avoid accidentally matching manga entries.
    Paginates results to prefer TV formats (TV, TV_SHORT, ONA) over standalone MOVIE/OVA specials
    when querying TV series (e.g. Captain Tsubasa S2 won't match a 1985 1-ep movie).

    Kitsu cross-check: if AniList returns RELEASING with no episode count, Kitsu is queried to
    either confirm the episode count or override the status to FINISHED if Kitsu knows it's done.
    """
    clean = _clean_query_title(show_name)
    # Use Page to inspect multiple candidates so TV series don't accidentally match 1-ep movies
    query = """
    query ($search: String) {
      Page (perPage: 6) {
        media (search: $search, type: ANIME) {
          title { english romaji }
          format
          episodes
          status
          averageScore
          meanScore
          seasonYear
          startDate { year }
        }
      }
    }
    """
    res = {"score": None, "episodes": None, "status": None, "year": None}

    queries_to_try = []
    if season and season > 1:
        queries_to_try.append(f"{clean} Season {season}")
    queries_to_try.append(clean)

    for q_search in queries_to_try:
        try:
            resp = requests.post(
                ANILIST_URL,
                json={"query": query, "variables": {"search": q_search}},
                timeout=10,
                headers={"User-Agent": "WP-AutoPoster/1.0"},
            )
            if resp.status_code == 200:
                page_data = resp.json().get("data", {}).get("Page", {}) if resp.json() else {}
                media_list = page_data.get("media", [])
                if media_list:
                    # Pick best candidate
                    chosen = None
                    if is_movie:
                        for m in media_list:
                            if m.get("format") == "MOVIE":
                                chosen = m
                                break
                    else:
                        for m in media_list:
                            if m.get("format") in ("TV", "TV_SHORT", "ONA"):
                                chosen = m
                                break
                    if not chosen:
                        chosen = media_list[0]

                    score = chosen.get("averageScore") or chosen.get("meanScore")
                    if score and int(score) > 0:
                        res["score"] = int(score)
                    if chosen.get("episodes"):
                        res["episodes"] = int(chosen["episodes"])
                    if chosen.get("status"):
                        res["status"] = str(chosen["status"]).upper()
                    year = chosen.get("seasonYear") or (chosen.get("startDate", {}).get("year") if chosen.get("startDate") else None)
                    if year:
                        res["year"] = str(year)
                    logger.info("AniList details for '%s' (queried '%s'): score=%s, eps=%s, status=%s, year=%s",
                                show_name, q_search, res["score"], res["episodes"], res["status"], res["year"])
                    # If we found meaningful episode/status data from the season-specific query, stop early
                    if res["episodes"] or res["status"]:
                        break
            elif resp.status_code == 429:
                logger.warning("AniList rate-limited (HTTP 429) for '%s'", q_search)
                break
        except Exception as exc:
            logger.warning("AniList API error for '%s': %s", q_search, exc)

    # Kitsu cross-check: only used as a fallback when AniList returns RELEASING with no episode count.
    # We do NOT blindly trust Kitsu because its fuzzy search can return movies/specials or wrong-season
    # entries (e.g. One Piece→ picks up a 1-ep special; Blue Box→ picks up S1 instead of airing S2).
    #
    # Safety guards:
    #   1. Only call Kitsu if AniList returned RELEASING with no episode count — AniList FINISHED always wins.
    #   2. Kitsu override to FINISHED only accepted when kitsu_eps >= 2 (avoids movies/1-ep specials).
    #   3. Kitsu episode count fill-in only accepted when kitsu_eps >= 2 (same reason).
    if res["status"] == "RELEASING" and not res["episodes"]:
        try:
            kitsu = get_kitsu_details(show_name)
            kitsu_eps = kitsu.get("episodes") or 0
            kitsu_status = kitsu.get("status")
            # Only accept Kitsu data when episode count is plausible (>= 2) — avoids specials/movies
            if kitsu_eps >= 2:
                if not res["episodes"]:
                    res["episodes"] = kitsu_eps
                    logger.info("Kitsu filled in episode count for '%s': %d", show_name, res["episodes"])
                if kitsu_status == "FINISHED" and res["status"] == "RELEASING":
                    logger.info(
                        "Kitsu overrides AniList status for '%s': RELEASING -> FINISHED (eps=%s)",
                        show_name, res.get("episodes")
                    )
                    res["status"] = "FINISHED"
        except Exception as exc:
            logger.debug("Kitsu cross-check error for '%s': %s", show_name, exc)

    return res


def get_anilist_score(show_name: str) -> Optional[int]:
    """Query AniList GraphQL API (0-100 score)."""
    details = get_anilist_details(show_name)
    return details.get("score")


def get_tvmaze_score(show_name: str) -> tuple[Optional[int], Optional[str]]:
    """
    Query TVMaze API for cartoon shows & TV series (0-100 score based on IMDb ratings, and premiered year).
    Free, no API key needed.
    Returns (score, year).
    """
    clean = _clean_query_title(show_name)
    try:
        resp = requests.get(
            f"{TVMAZE_BASE}/singlesearch/shows",
            params={"q": clean},
            timeout=10,
            headers={"User-Agent": "WP-AutoPoster/1.0"},
        )
        if resp.status_code == 200:
            data = resp.json()
            rating = data.get("rating", {}).get("average")
            premiered = data.get("premiered", "")
            year = premiered[:4] if premiered and len(premiered) >= 4 else None
            taq_score = None
            if rating and float(rating) > 0:
                taq_score = round(float(rating) * 10)
                logger.info("TVMaze (IMDb) score for '%s': %.1f -> Taqyeem %d (year %s)", show_name, float(rating), taq_score, year)
            return taq_score, year
    except Exception as exc:
        logger.warning("TVMaze API error for '%s': %s", show_name, exc)

    return None, None


def get_tmdb_score(show_name: str, content_type: str, tmdb_api_key: str) -> tuple[Optional[int], Optional[str]]:
    """
    Search TMDB API for movies / TV shows (0-100 score and release year). Requires free TMDB API key.
    Returns (score, year).
    """
    if not tmdb_api_key:
        return None, None

    clean = _clean_query_title(show_name)
    endpoint = "search/movie" if content_type == "movie" else "search/tv"
    params = {
        "api_key": tmdb_api_key,
        "query": clean,
        "include_adult": "false",
    }

    try:
        resp = requests.get(
            f"{TMDB_BASE}/{endpoint}",
            params=params,
            timeout=10,
            headers={"User-Agent": "WP-AutoPoster/1.0"},
        )
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            if results:
                res0 = results[0]
                score = res0.get("vote_average", 0)
                rel = res0.get("release_date") or res0.get("first_air_date") or ""
                year = rel[:4] if rel and len(rel) >= 4 else None
                taq_score = None
                if score and float(score) > 0:
                    taq_score = round(float(score) * 10)
                    logger.info("TMDB score for '%s': %.1f -> Taqyeem %d (year %s)", show_name, score, taq_score, year)
                return taq_score, year
    except Exception as exc:
        logger.warning("TMDB API error for '%s': %s", show_name, exc)

    return None, None


def get_mal_score(show_name: str) -> Optional[int]:
    """
    Search MyAnimeList (via Jikan) for the show (0-100 score).
    """
    global _jikan_last_call
    elapsed = time.time() - _jikan_last_call
    if elapsed < JIKAN_RATE_LIMIT:
        time.sleep(JIKAN_RATE_LIMIT - elapsed)
    _jikan_last_call = time.time()

    clean = _clean_query_title(show_name)
    try:
        resp = requests.get(
            f"{JIKAN_BASE}/anime",
            params={"q": clean, "limit": 3},
            timeout=10,
            headers={"User-Agent": "WP-AutoPoster/1.0"},
        )
        if resp.status_code == 200:
            results = resp.json().get("data", [])
            if results:
                score = results[0].get("score")
                if score and float(score) > 0:
                    taq_score = round(float(score) * 10)
                    logger.info("MAL score for '%s': %.1f -> Taqyeem %d", show_name, score, taq_score)
                    return taq_score
    except Exception as exc:
        logger.warning("Jikan API error for '%s': %s", show_name, exc)

    return None


def fetch_rating(
    show_name: str,
    content_type: str,
    tmdb_api_key: str = "",
) -> Optional[int]:
    """
    Fetch the best available rating (0-100) for ANY title (Anime, Cartoons, Movies).
    """
    if tmdb_api_key:
        score, _ = get_tmdb_score(show_name, content_type, tmdb_api_key)
        if score is not None:
            return score

    score = get_anilist_score(show_name)
    if score is not None:
        return score

    score, _ = get_tvmaze_score(show_name)
    if score is not None:
        return score

    score = get_mal_score(show_name)
    if score is not None:
        return score

    return None


def get_show_metadata(
    show_name: str,
    content_type: str,
    tmdb_api_key: str = "",
    season: int = 1,
) -> dict:
    """
    Fetch rating score (0-100), expected total episode count, status, and release year.
    Returns dict: {"score": Optional[int], "expected_episodes": Optional[int], "status": Optional[str], "year": Optional[str]}
    """
    details = get_anilist_details(show_name, season=season, is_movie=(content_type.lower() == "movie"))
    score = details.get("score")
    year = details.get("year")

    if tmdb_api_key:
        t_score, t_year = get_tmdb_score(show_name, content_type, tmdb_api_key)
        if score is None:
            score = t_score
        if not year:
            year = t_year

    if score is None or not year:
        tv_score, tv_year = get_tvmaze_score(show_name)
        if score is None:
            score = tv_score
        if not year:
            year = tv_year

    if score is None:
        score = get_mal_score(show_name)

    return {
        "score": score,
        "expected_episodes": details.get("episodes"),
        "status": details.get("status"),
        "year": year,
    }
