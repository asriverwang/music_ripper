"""
ACRCloud audio recognition — identifies tracks by audio fingerprint.
Better Asian music coverage than AcoustID.

Requires: ACRCLOUD_HOST, ACRCLOUD_KEY, ACRCLOUD_SECRET in .env
Register free at: https://console.acrcloud.com
"""

import base64
import hashlib
import hmac
import logging
import os
import time

import requests

import config

log = logging.getLogger(__name__)

_TIMEOUT = 15
_READ_BYTES = 800 * 1024  # ~20 seconds of 320kbps MP3 / enough WAV for fingerprinting


def lookup_track(wav_path: str) -> dict | None:
    """
    Identify a WAV file via ACRCloud.
    Returns {"title", "artist", "album"} or None.
    """
    if not all([config.ACRCLOUD_HOST, config.ACRCLOUD_KEY, config.ACRCLOUD_SECRET]):
        return None

    try:
        with open(wav_path, "rb") as f:
            sample = f.read(_READ_BYTES)
    except OSError as e:
        log.warning("ACRCloud: could not read %s: %s", wav_path, e)
        return None

    timestamp = str(int(time.time()))
    string_to_sign = "\n".join([
        "POST",
        "/v1/identify",
        config.ACRCLOUD_KEY,
        "audio",
        "1",
        timestamp,
    ])
    signature = base64.b64encode(
        hmac.new(
            config.ACRCLOUD_SECRET.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha1,
        ).digest()
    ).decode("utf-8")

    url = f"https://{config.ACRCLOUD_HOST}/v1/identify"
    files = {"sample": ("sample.wav", sample, "audio/wav")}
    data = {
        "access_key": config.ACRCLOUD_KEY,
        "sample_bytes": len(sample),
        "timestamp": timestamp,
        "signature": signature,
        "data_type": "audio",
        "signature_version": "1",
    }

    try:
        resp = requests.post(url, files=files, data=data, timeout=_TIMEOUT)
        resp.raise_for_status()
        import json as _json
        body = _json.loads(resp.content.decode("utf-8"))
    except Exception as e:
        log.warning("ACRCloud request failed: %s", e)
        return None

    status = body.get("status", {})
    if status.get("code") != 0:
        log.info("ACRCloud: no match — %s", status.get("msg", ""))
        return None

    try:
        music = body["metadata"]["music"][0]
        title  = music.get("title", "")
        artist = (music.get("artists") or [{}])[0].get("name", "")
        album  = music.get("album", {}).get("name", "")
        score  = music.get("score", 0)

        def _clean(s):
            """Strip garbled suffix — remove any '- ???...' or '/???...' segment."""
            import re
            return re.sub(r'\s*[-–/]\s*\?+$', '', s).strip()

        title  = _clean(title)
        artist = _clean(artist)
        album  = _clean(album)

        log.info("ACRCloud: score=%d  '%s' by %s  [%s]", score, title, artist, album)
        return {"title": title, "artist": artist, "album": album}
    except (KeyError, IndexError) as e:
        log.warning("ACRCloud: unexpected response structure: %s", e)
        return None
