"""
AcoustID acoustic fingerprinting — identifies tracks by their audio content.

Requires:
  - fpcalc binary:  sudo apt install libchromaprint-tools
  - Free API key:   https://acoustid.org/login (register, then create an app)
  - Set ACOUSTID_API_KEY in .env
"""

import json
import logging
import subprocess

import requests

import config

log = logging.getLogger(__name__)

_API_URL = "https://api.acoustid.org/v2/lookup"
_MIN_SCORE = 0.75


def fingerprint(wav_path: str) -> tuple[int, str] | None:
    """Run fpcalc on a WAV file. Returns (duration_secs, fingerprint) or None."""
    try:
        result = subprocess.run(
            ["fpcalc", "-json", wav_path],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            log.warning("fpcalc error: %s", result.stderr.strip())
            return None
        data = json.loads(result.stdout)
        return int(data["duration"]), data["fingerprint"]
    except FileNotFoundError:
        log.warning("fpcalc not found — install: sudo apt install libchromaprint-tools")
        return None
    except Exception as e:
        log.warning("Fingerprint failed for %s: %s", wav_path, e)
        return None


def lookup_track(wav_path: str) -> dict | None:
    """
    Fingerprint a WAV file and query AcoustID.
    Returns {"title", "artist", "album"} for the best match, or None.
    """
    if not config.ACOUSTID_API_KEY:
        return None

    fp_data = fingerprint(wav_path)
    if not fp_data:
        return None
    duration, fp = fp_data

    data = {
        "client": config.ACOUSTID_API_KEY,
        "duration": duration,
        "fingerprint": fp,
        "meta": "recordings releases releasegroups",
    }
    try:
        resp = requests.post(_API_URL, data=data, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "ok" or not data.get("results"):
            return None

        # Best score result
        result = max(data["results"], key=lambda r: r.get("score", 0))
        if result.get("score", 0) < _MIN_SCORE:
            log.debug("AcoustID score %.2f below threshold — skipping", result.get("score", 0))
            return None

        recordings = result.get("recordings", [])
        if not recordings:
            return None

        rec = recordings[0]
        title = rec.get("title", "")
        artists = rec.get("artists", [])
        artist = artists[0].get("name", "") if artists else ""

        # Prefer Album type release group
        rgs = rec.get("releasegroups", [])
        album_rg = next((r for r in rgs if r.get("type") == "Album"), rgs[0] if rgs else {})
        album = album_rg.get("title", "")

        log.info("AcoustID: score=%.2f  '%s' by %s  [%s]",
                 result["score"], title, artist, album)
        return {"title": title, "artist": artist, "album": album}

    except Exception as e:
        log.warning("AcoustID lookup failed: %s", e)
        return None
