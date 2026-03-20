"""
Metadata lookup: MusicBrainz disc ID lookup with LLM fallback.
Supported LLM providers: minimax, openai, claude.
"""

import logging
import json
import time
import requests
import discid
import musicbrainzngs

import config
import gnudb

log = logging.getLogger(__name__)

musicbrainzngs.set_useragent(
    config.MUSICBRAINZ_APP,
    config.MUSICBRAINZ_VERSION,
    config.MUSICBRAINZ_CONTACT,
)


def read_disc(device=config.DEVICE, retries=5, retry_delay=3):
    """Read disc TOC, retrying if the drive hasn't finished spinning up."""
    log.info("Reading disc ID from %s", device)
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            return discid.read(device)
        except discid.DiscError as e:
            last_err = e
            if attempt < retries:
                log.warning("TOC read failed (attempt %d/%d): %s — retrying in %ds",
                            attempt, retries, e, retry_delay)
                time.sleep(retry_delay)
    raise last_err


def _format_duration(ms):
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d}"


_SYSTEM_PROMPT = (
    "You are a music expert helping to identify CD albums. "
    "Respond only with a JSON object as instructed."
)


def _ask_minimax(prompt: str) -> str:
    headers = {
        "Authorization": f"Bearer {config.MINIMAX_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.MINIMAX_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }
    resp = requests.post(config.MINIMAX_API_URL, headers=headers, json=payload, timeout=config.LLM_TIMEOUT)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _ask_openai(prompt: str) -> str:
    headers = {
        "Authorization": f"Bearer {config.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }
    resp = requests.post(config.OPENAI_API_URL, headers=headers, json=payload, timeout=config.LLM_TIMEOUT)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _ask_claude(prompt: str) -> str:
    headers = {
        "x-api-key": config.CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.CLAUDE_MODEL,
        "max_tokens": 1024,
        "system": _SYSTEM_PROMPT,
        "messages": [
            {"role": "user", "content": prompt},
        ],
    }
    resp = requests.post(config.CLAUDE_API_URL, headers=headers, json=payload, timeout=config.LLM_TIMEOUT)
    resp.raise_for_status()
    return resp.json()["content"][0]["text"].strip()


def _ask_llm(prompt: str) -> str:
    """Dispatch to the configured LLM provider."""
    provider = config.active_llm_provider()
    if provider == 'minimax':
        return _ask_minimax(prompt)
    if provider == 'openai':
        return _ask_openai(prompt)
    if provider == 'claude':
        return _ask_claude(prompt)
    raise RuntimeError(
        "No LLM API key configured. Set MINIMAX_API_KEY, OPENAI_API_KEY, or CLAUDE_API_KEY in .env"
    )


def _release_to_album(release, disc_index=0) -> dict:
    """Convert a MusicBrainz release dict to our internal album format."""
    # Artist
    artist_credits = release.get("artist-credit", [])
    artist = "".join(
        c.get("name", c.get("artist", {}).get("name", "")) if isinstance(c, dict) else c
        for c in artist_credits
    ).strip() or "Unknown Artist"

    album_title = release.get("title", "Unknown Album")

    # Find the medium matching our disc
    tracks = []
    medium_list = release.get("medium-list", [])
    medium = medium_list[disc_index] if disc_index < len(medium_list) else (medium_list[0] if medium_list else {})
    for t in medium.get("track-list", []):
        recording = t.get("recording", {})
        tracks.append({
            "number": int(t.get("position", t.get("number", 0))),
            "title": recording.get("title", t.get("title", f"Track {t.get('position', '?')}")),
            "length_ms": int(recording.get("length") or t.get("length") or 0),
        })

    return {
        "artist": artist,
        "album": album_title,
        "tracks": tracks,
        "mbid": release.get("id", ""),
    }


def _disambiguate_with_llm(candidates: list, disc: discid.Disc) -> dict:
    """Use Minimax to pick the best match among several MusicBrainz candidates."""
    track_durations = [_format_duration(int(t.length * 1000 / 75)) for t in disc.tracks]
    candidate_summaries = []
    for i, c in enumerate(candidates):
        artist_credits = c.get("artist-credit", [])
        artist = "".join(
            cr.get("name", cr.get("artist", {}).get("name", "")) if isinstance(cr, dict) else cr
            for cr in artist_credits
        )
        date = c.get("date", c.get("first-release-date", "unknown year"))
        country = c.get("country", "unknown country")
        status = c.get("status", "")
        disambiguation = c.get("disambiguation", "")
        label_list = c.get("label-info-list", [])
        label = label_list[0].get("label", {}).get("name", "") if label_list else ""
        parts = [
            f'artist="{artist}"',
            f'album="{c.get("title", "")}"',
            f'date={date}',
            f'country={country}',
        ]
        if status:
            parts.append(f'status={status}')
        if label:
            parts.append(f'label="{label}"')
        if disambiguation:
            parts.append(f'note="{disambiguation}"')
        candidate_summaries.append(f"{i}: " + ", ".join(parts))

    prompt = (
        f"A CD has disc ID '{disc.id}' with {len(disc.tracks)} audio tracks.\n"
        f"Track durations: {', '.join(track_durations)}\n\n"
        f"MusicBrainz returned these release candidates (all match the disc ID):\n"
        + "\n".join(candidate_summaries)
        + "\n\nPick the most canonical original release (prefer earliest official "
        "release over reissues/compilations/promos). "
        "Reply ONLY with a JSON object: "
        '{"index": <number>, "reason": "<brief reason>"}'
    )

    if not config.active_llm_provider():
        log.warning("No LLM API key set — using first MusicBrainz candidate as fallback")
        return candidates[0]

    log.info("Asking Minimax to disambiguate %d candidates", len(candidates))
    raw = _ask_llm(prompt)
    # Strip possible markdown fences
    raw = raw.strip().strip("`").strip()
    if raw.startswith("json"):
        raw = raw[4:].strip()
    data = json.loads(raw)
    chosen = int(data.get("index", 0))
    log.info("Minimax chose candidate %d: %s", chosen, data.get("reason", ""))
    return candidates[chosen]


def _identify_with_llm(disc: discid.Disc) -> dict:
    """Ask Minimax to identify an unknown disc from TOC information only."""
    track_info = []
    for i, t in enumerate(disc.tracks, start=1):
        duration_ms = int(t.length * 1000 / 75)
        track_info.append(f"Track {i}: {_format_duration(duration_ms)}")

    prompt = (
        f"I have an audio CD with disc ID '{disc.id}'.\n"
        f"It has {len(disc.tracks)} tracks:\n"
        + "\n".join(track_info)
        + "\n\nPlease identify the album. "
        "Reply ONLY with a JSON object:\n"
        "{\n"
        '  "artist": "<artist name>",\n'
        '  "album": "<album title>",\n'
        '  "tracks": [\n'
        '    {"number": 1, "title": "<title>"},\n'
        '    ...\n'
        "  ]\n"
        "}\n"
        'If you cannot identify it, use "Unknown Artist" and "Unknown Album" '
        'with titles like "Track 1", "Track 2", etc.'
    )

    log.info("Asking %s to identify unknown disc", config.active_llm_provider())
    raw = _ask_llm(prompt)
    raw = raw.strip().strip("`").strip()
    if raw.startswith("json"):
        raw = raw[4:].strip()
    data = json.loads(raw)

    tracks = [
        {"number": int(t.get("number", i + 1)), "title": t.get("title", f"Track {i+1}"), "length_ms": 0}
        for i, t in enumerate(data.get("tracks", []))
    ]
    return {
        "artist": data.get("artist", "Unknown Artist"),
        "album": data.get("album", "Unknown Album"),
        "tracks": tracks,
        "mbid": "",
    }


def get_album_metadata(device=config.DEVICE) -> dict:
    """
    Main entry point. Returns an album dict:
      {
        "artist": str,
        "album": str,
        "tracks": [{"number": int, "title": str, "length_ms": int}, ...],
        "mbid": str,
        "disc_id": str,
      }
    """
    disc = read_disc(device)
    log.info("Disc ID: %s  tracks: %d", disc.id, len(disc.tracks))

    # 1. MusicBrainz
    releases = []
    try:
        result = musicbrainzngs.get_releases_by_discid(
            disc.id, includes=["artist-credits", "recordings"]
        )
        if "disc" in result:
            releases = result["disc"].get("release-list", [])
        elif "release-list" in result:
            releases = result["release-list"]
        log.info("MusicBrainz returned %d release(s)", len(releases))
    except musicbrainzngs.ResponseError as e:
        if "404" in str(e):
            log.info("Disc not found in MusicBrainz")
        else:
            log.warning("MusicBrainz error: %s", e)

    if len(releases) == 1:
        album = _release_to_album(releases[0])
    elif len(releases) > 1:
        try:
            chosen = _disambiguate_with_llm(releases, disc)
        except Exception:
            log.exception("LLM disambiguation failed — falling back to first candidate")
            chosen = releases[0]
        album = _release_to_album(chosen)
    else:
        # 2. GnuDB / CDDB
        album = gnudb.lookup(disc)
        if album:
            album["disc_id"] = disc.id
            return album

        # 3. LLM
        try:
            album = _identify_with_llm(disc)
        except Exception:
            log.exception("LLM identification failed — using unknown metadata")
            album = {
                "artist": "Unknown Artist",
                "album": "Unknown Album",
                "tracks": [
                    {"number": i + 1, "title": f"Track {i + 1}", "length_ms": 0}
                    for i in range(len(disc.tracks))
                ],
                "mbid": "",
            }

    album["disc_id"] = disc.id
    return album
