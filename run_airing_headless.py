"""
run_airing_headless.py
Headless (no GUI) entry point for the airing-post updater.
Called by GitHub Actions. Reads settings from environment variables
(injected as GitHub Secrets) so no credentials are committed to the repo.
"""
import json
import logging
import os
import sys
from pathlib import Path

# ── Patch settings.json from environment variables ──────────────────────────
# GitHub Actions injects secrets as env vars.
# We write them into settings.json so stream_extractor.py can load them
# exactly the same way it does on the desktop.

SETTINGS_FILE = Path(__file__).parent / "settings.json"


def _build_settings() -> dict:
    """Load base settings.json and override/fill from environment variables."""
    base: dict = {}
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                base = json.load(f)
        except Exception:
            pass

    # Map: env-var name → settings.json key
    env_map = {
        "WP_URL":              "wp_url",
        "WP_USERNAME":         "wp_username",
        "WP_APP_PASSWORD":     "wp_app_password",
        "TMDB_API_KEY":        "tmdb_api_key",
        "TOONSTREAM_DOMAINS":  "toonstream_domains",
        "MULTIMOVIES_DOMAINS": "multimovies_domains",
        "WORKER_BASE":         "worker_base",
        "ANIMEDEKHO_URL":      "animedekho_url",
    }
    # Note: CF_CLEARANCE / TW4ALL_COOKIE not needed — site has no bot protection
    for env_key, setting_key in env_map.items():
        val = os.environ.get(env_key, "").strip()
        if val:
            base[setting_key] = val

    return base


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    log = logging.getLogger("headless")

    settings = _build_settings()

    # Write merged settings back so stream_extractor.py's module-level
    # import block picks up the correct domains and cookies.
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    log.info("settings.json written from environment.")

    # Now import the app module (GUI is already mocked for headless environments)
    from auto_poster import check_and_update_airing_posts
    from wp_api import WordPressAPI
    from stream_extractor import sync_domains_from_cloudstream

    # ── Auto-sync domains from phisher98's CloudStream domains.json ──────────
    # Same source used by Toonstream.cs3 and MultiMoviesProvider.cs3 at runtime.
    # This keeps our domain list current without manual secret updates.
    log.info("Syncing domains from CloudStream domains.json…")
    synced = sync_domains_from_cloudstream(save_to_settings=True)
    if synced["toonstream"]:
        log.info("  Toonstream  → %s", synced["toonstream"])
    if synced["multimovies"]:
        log.info("  MultiMovies → %s", synced["multimovies"])

    api = WordPressAPI(
        url=settings["wp_url"],
        username=settings["wp_username"],
        app_password=settings["wp_app_password"],
    )
    ok, msg = api.test_connection()
    if not ok:
        log.error("WordPress connection failed: %s", msg)
        sys.exit(1)
    log.info("WordPress connected: %s", msg)

    def log_fn(msg, style="info"):
        level = logging.WARNING if style == "warn" else (
                logging.ERROR   if style == "error" else logging.INFO)
        log.log(level, msg)

    total, updated = check_and_update_airing_posts(
        api, settings, log_fn=log_fn
    )
    log.info("Done — %d posts checked, %d updated.", total, updated)


if __name__ == "__main__":
    main()
