"""
GnuDB (CDDB) lookup — fallback when MusicBrainz has no disc ID match.

CDDB uses a different disc ID scheme (CRC-based, not SHA-1) and a different
database, so it often covers pressings that MusicBrainz doesn't know about.
"""

import logging
import requests

log = logging.getLogger(__name__)

_GNUDB_URL = "http://gnudb.gnudb.org/~cddb/cddb.cgi"
_HELLO = "anonymous localhost music_ripper 1.0"
_PROTO = 6
_TIMEOUT = 15


def _leadout(disc) -> int:
    """Return the leadout sector position (last track offset + length)."""
    last = disc.tracks[-1]
    return last.offset + last.length


def cddb_disc_id(disc) -> str:
    """Compute the 8-hex-digit CDDB disc ID from a discid.Disc object."""
    def digit_sum(n: int) -> int:
        s = 0
        while n > 0:
            s += n % 10
            n //= 10
        return s

    leadout = _leadout(disc)
    n = sum(digit_sum(t.offset // 75) for t in disc.tracks)
    t = leadout // 75 - disc.tracks[0].offset // 75
    cddb_id = ((n % 0xFF) << 24) | (t << 8) | len(disc.tracks)
    return f"{cddb_id:08x}"


def _query(disc, cddb_id: str):
    """Query GnuDB. Returns (category, matched_id) or None."""
    offsets = " ".join(str(t.offset) for t in disc.tracks)
    total_secs = _leadout(disc) // 75
    params = {
        "cmd": f"cddb query {cddb_id} {len(disc.tracks)} {offsets} {total_secs}",
        "hello": _HELLO,
        "proto": _PROTO,
    }
    resp = requests.get(_GNUDB_URL, params=params, timeout=_TIMEOUT)
    resp.raise_for_status()

    lines = _decode(resp.content).strip().splitlines()
    if not lines:
        return None
    code = int(lines[0].split()[0])
    if code == 200:
        # Exact match: "200 category disc_id title"
        parts = lines[0].split(None, 3)
        return parts[1], parts[2]
    if code in (211, 210):
        # Multiple matches — take the first entry line
        for line in lines[1:]:
            if line == ".":
                break
            parts = line.split(None, 2)
            if len(parts) >= 2:
                return parts[0], parts[1]
    return None


def _decode(content: bytes) -> str:
    """Decode CDDB response bytes, trying common encodings for Asian entries."""
    for enc in ("utf-8", "shift-jis", "euc-jp", "gbk", "big5", "euc-kr", "latin-1"):
        try:
            return content.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return content.decode("latin-1", errors="replace")


def _read(category: str, cddb_id: str) -> dict | None:
    """Fetch and parse a full CDDB entry."""
    params = {
        "cmd": f"cddb read {category} {cddb_id}",
        "hello": _HELLO,
        "proto": _PROTO,
    }
    resp = requests.get(_GNUDB_URL, params=params, timeout=_TIMEOUT)
    resp.raise_for_status()

    data = {}
    for line in _decode(resp.content).splitlines():
        if line.startswith("#") or line.strip() == ".":
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            data[key.strip()] = val.strip()

    if "DTITLE" not in data:
        return None

    dtitle = data["DTITLE"]
    if " / " in dtitle:
        artist, album = dtitle.split(" / ", 1)
    else:
        artist, album = "Unknown Artist", dtitle

    tracks = []
    i = 0
    while f"TTITLE{i}" in data:
        tracks.append({
            "number": i + 1,
            "title": data[f"TTITLE{i}"],
            "length_ms": 0,
        })
        i += 1

    return {
        "artist": artist.strip(),
        "album": album.strip(),
        "tracks": tracks,
        "mbid": "",
    }


def _is_garbled(text: str) -> bool:
    """Return True if the text looks like an encoding corruption (mostly '?' chars)."""
    if not text:
        return False
    q = text.count("?")
    return q > 0 and q / len(text) > 0.3


def lookup(disc) -> dict | None:
    """Full GnuDB lookup. Returns album dict or None if not found."""
    cddb_id = cddb_disc_id(disc)
    log.info("GnuDB CDDB ID: %s", cddb_id)
    try:
        match = _query(disc, cddb_id)
        if not match:
            log.info("Disc not found in GnuDB")
            return None
        category, matched_id = match
        log.info("GnuDB match: category=%s id=%s", category, matched_id)
        result = _read(category, matched_id)
        if result:
            if _is_garbled(result["artist"]) or _is_garbled(result["album"]):
                log.warning(
                    "GnuDB result looks garbled (encoding corruption in database entry) "
                    "— falling through to MusicBrainz"
                )
                return None
            log.info("GnuDB identified: %s – %s (%d tracks)",
                     result["artist"], result["album"], len(result["tracks"]))
        return result
    except Exception as e:
        log.warning("GnuDB lookup failed: %s", e)
        return None
