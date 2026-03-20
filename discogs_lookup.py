"""
Discogs API — enriches confirmed metadata with year, label, and genre.

Requires a free Discogs personal access token:
  https://www.discogs.com/settings/developers  →  "Generate new token"
Set DISCOGS_TOKEN in .env.
"""

import logging
import requests
import config

log = logging.getLogger(__name__)

_SEARCH_URL = "https://api.discogs.com/database/search"
_HEADERS = {"User-Agent": "MusicRipper/1.0 +local"}


def enrich(artist: str, album: str) -> dict:
    """
    Search Discogs for the release and return enrichment fields:
      {"year": str, "label": str, "genre": str, "country": str}
    Any missing fields are omitted from the returned dict.
    """
    if not config.DISCOGS_TOKEN:
        return {}
    if artist in ("Unknown Artist", "") or album in ("Unknown Album", ""):
        return {}

    headers = {**_HEADERS, "Authorization": f"Discogs token={config.DISCOGS_TOKEN}"}
    params = {
        "artist": artist,
        "release_title": album,
        "type": "release",
        "per_page": 5,
    }
    try:
        resp = requests.get(_SEARCH_URL, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            log.info("Discogs: no results for '%s – %s'", artist, album)
            return {}

        best = results[0]
        info = {}
        if best.get("year"):
            info["year"] = str(best["year"])
        labels = best.get("label") or []
        if labels:
            info["label"] = labels[0]
        genres = best.get("genre") or []
        if genres:
            info["genre"] = genres[0]
        if best.get("country"):
            info["country"] = best["country"]

        log.info("Discogs enrichment: %s", info)
        return info

    except Exception as e:
        log.warning("Discogs lookup failed: %s", e)
        return {}
