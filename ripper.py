"""
Core ripping and encoding logic.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, Future

from mutagen.id3 import ID3, TPE1, TALB, TDRC, TCON, TPUB

import acoustid_lookup
import acrcloud_lookup
import config
import discogs_lookup

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Processed-disc database
# ---------------------------------------------------------------------------

def _load_db() -> dict:
    if os.path.exists(config.PROCESSED_DB):
        with open(config.PROCESSED_DB) as f:
            return json.load(f)
    return {}


def _save_db(db: dict):
    with open(config.PROCESSED_DB, "w") as f:
        json.dump(db, f, indent=2)


def is_processed(disc_id: str) -> bool:
    return disc_id in _load_db()


def mark_processed(disc_id: str, album: dict):
    db = _load_db()
    db[disc_id] = {"artist": album.get("artist"), "album": album.get("album")}
    _save_db(db)


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------

def _sanitize(name: str) -> str:
    """Remove characters that are invalid in filenames/directory names."""
    name = name.strip()
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    name = re.sub(r'\s+', ' ', name)
    name = name.rstrip(".")  # Windows can't open paths with trailing periods
    return name or "Unknown"


def get_output_path(artist: str, album: str, track_number: int, title: str) -> str:
    """
    Return the full output path for a track.
    If the album directory already exists, append " (New Copy)" (incrementing
    the suffix until the directory is new).
    """
    artist_dir = os.path.join(config.MUSIC_DIR, _sanitize(artist))
    album_dir_base = _sanitize(album)

    # Find a free album directory name
    album_dir = os.path.join(artist_dir, album_dir_base)
    if os.path.exists(album_dir):
        suffix = 2
        candidate = os.path.join(artist_dir, f"{album_dir_base} (New Copy)")
        while os.path.exists(candidate):
            suffix += 1
            candidate = os.path.join(artist_dir, f"{album_dir_base} (New Copy {suffix})")
        album_dir = candidate

    filename = f"{track_number:02d}_{_sanitize(title)}.mp3"
    return os.path.join(album_dir, filename)


def _album_dir(artist: str, album: str) -> str:
    """Return the consistent album dir for this rip session (cached by album identity)."""
    artist_dir = os.path.join(config.MUSIC_DIR, _sanitize(artist))
    album_dir_base = _sanitize(album)
    album_dir = os.path.join(artist_dir, album_dir_base)
    if os.path.exists(album_dir):
        suffix = 2
        candidate = os.path.join(artist_dir, f"{album_dir_base} (New Copy)")
        while os.path.exists(candidate):
            suffix += 1
            candidate = os.path.join(artist_dir, f"{album_dir_base} (New Copy {suffix})")
        return candidate
    return album_dir


# ---------------------------------------------------------------------------
# Ripping
# ---------------------------------------------------------------------------

def _paranoia_flags(level: int) -> list:
    """Return cdparanoia flags for the requested paranoia level."""
    if level == 0:
        return ["-Z"]          # disable all error correction — fastest
    if level == 1:
        return ["-Y"]          # checksums only, no scratch/jitter recovery
    return []                  # level 2: full paranoia (default)


def rip_track(track_number: int, output_wav: str, device=config.DEVICE,
              paranoia= None, timeout = None):
    """
    Rip a single track from the CD to a WAV file using cdparanoia.

    On timeout, raises subprocess.TimeoutExpired.
    On non-zero exit, raises RuntimeError.
    """
    if paranoia is None:
        paranoia = config.RIP_PARANOIA
    if timeout is None:
        timeout = config.RIP_TRACK_TIMEOUT

    log.info("Ripping track %d (paranoia=%d, timeout=%ds)", track_number, paranoia, timeout)
    cmd = ["cdparanoia", "-d", device] + _paranoia_flags(paranoia) + [str(track_number), output_wav]

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        _, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise

    if proc.returncode != 0:
        raise RuntimeError(f"cdparanoia failed for track {track_number}:\n{stderr}")


def encode_to_mp3(wav_path: str, mp3_path: str, tags: dict):
    """Encode WAV to 320kbps MP3 with lame, then write ID3 tags."""
    os.makedirs(os.path.dirname(mp3_path), exist_ok=True)
    log.info("Encoding %s -> %s", wav_path, mp3_path)

    cmd = [
        "lame",
        "--preset", "insane",   # 320kbps CBR
        "--id3v2-only",
        "--ta", tags.get("artist", ""),
        "--tl", tags.get("album", ""),
        "--tt", tags.get("title", ""),
        "--tn", str(tags.get("track_number", 1)),
        wav_path,
        mp3_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"lame failed:\n{result.stderr}")


# ---------------------------------------------------------------------------
# Main ripping session
# ---------------------------------------------------------------------------

def eject(device=config.DEVICE):
    log.info("Ejecting %s", device)
    subprocess.run(["eject", device], check=False)


def _rip_with_retry(track_number: int, wav_path: str, device: str) -> bool:
    """
    Rip a track, retrying once at paranoia=0 if the first attempt times out.
    Returns True on success, False if both attempts fail.
    """
    try:
        rip_track(track_number, wav_path, device)
        return True
    except subprocess.TimeoutExpired:
        log.warning(
            "Track %d timed out after %ds — retrying with paranoia disabled",
            track_number, config.RIP_TRACK_TIMEOUT,
        )
        if os.path.exists(wav_path):
            os.remove(wav_path)
    except RuntimeError as e:
        log.error("Track %d rip error: %s", track_number, e)
        return False

    # Retry with no error correction and a shorter timeout
    try:
        rip_track(track_number, wav_path, device,
                  paranoia=0, timeout=config.RIP_RETRY_TIMEOUT)
        log.info("Track %d recovered on retry (no paranoia)", track_number)
        return True
    except subprocess.TimeoutExpired:
        log.error("Track %d timed out again on retry — skipping", track_number)
    except RuntimeError as e:
        log.error("Track %d retry failed: %s", track_number, e)
    return False


def _encode_and_clean(wav_path: str, mp3_path: str, tags: dict):
    """Encode WAV to MP3 then delete the WAV regardless of outcome."""
    try:
        encode_to_mp3(wav_path, mp3_path, tags)
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)


def _update_id3(mp3_path: str, fields: dict):
    """Update arbitrary ID3 tags on an existing MP3 file."""
    try:
        tags = ID3(mp3_path)
        if "artist" in fields:
            tags["TPE1"] = TPE1(encoding=3, text=fields["artist"])
        if "album" in fields:
            tags["TALB"] = TALB(encoding=3, text=fields["album"])
        if "year" in fields and fields["year"]:
            tags["TDRC"] = TDRC(encoding=3, text=fields["year"])
        if "genre" in fields and fields["genre"]:
            tags["TCON"] = TCON(encoding=3, text=fields["genre"])
        if "label" in fields and fields["label"]:
            tags["TPUB"] = TPUB(encoding=3, text=fields["label"])
        tags.save(mp3_path)
    except Exception as e:
        log.warning("Failed to update ID3 tags on %s: %s", mp3_path, e)


def _resolve_unknown_from_acoustid(album_dir: str, acoustid_results: list) -> tuple[str, str] | None:
    """
    Examine AcoustID results for all tracks. If a majority agree on artist+album,
    move the album directory and retag all MP3s. Returns (new_artist, new_album) or None.
    """
    artist_votes = Counter(r["artist"] for r in acoustid_results if r.get("artist"))
    album_votes  = Counter(r["album"]  for r in acoustid_results if r.get("album"))
    if not artist_votes or not album_votes:
        return None

    best_artist, a_count = artist_votes.most_common(1)[0]
    best_album,  b_count  = album_votes.most_common(1)[0]
    threshold = len(acoustid_results) * 0.5

    if a_count < threshold or b_count < threshold:
        return None
    if best_artist in ("", "Unknown Artist") or best_album in ("", "Unknown Album"):
        return None

    new_dir = _album_dir(best_artist, best_album)
    if new_dir == album_dir:
        return (best_artist, best_album)

    log.info("AcoustID resolved album: '%s – %s' — moving to %s", best_artist, best_album, new_dir)
    os.makedirs(new_dir, exist_ok=True)
    for fname in os.listdir(album_dir):
        shutil.move(os.path.join(album_dir, fname), os.path.join(new_dir, fname))

    # Retag all moved MP3s
    for fname in os.listdir(new_dir):
        if fname.endswith(".mp3"):
            _update_id3(os.path.join(new_dir, fname),
                        {"artist": best_artist, "album": best_album})

    # Remove now-empty old directories
    try:
        os.rmdir(album_dir)
        os.rmdir(os.path.dirname(album_dir))  # artist dir if empty
    except OSError:
        pass

    return (best_artist, best_album)


def process_disc(album: dict, device=config.DEVICE):
    """
    Rip all tracks from the disc and encode to MP3.

    Pipeline:
      1. Rip track N  →  AcoustID fingerprint (if key set)  →  encode in background
      2. Repeat for all tracks
      3. If album was Unknown, try to resolve artist/album from AcoustID majority vote
      4. Enrich all tracks with year/label/genre from Discogs (if token set)
    """
    artist = album["artist"]
    album_title = album["album"]
    tracks = album["tracks"]
    disc_id = album.get("disc_id", "")
    is_unknown = artist == "Unknown Artist" or album_title == "Unknown Album"

    dest_album_dir = _album_dir(artist, album_title)
    log.info("Ripping '%s – %s' (%d tracks) -> %s", artist, album_title, len(tracks), dest_album_dir)

    tmpdir = tempfile.mkdtemp(prefix="music_ripper_")
    acoustid_results = []

    try:
        with ThreadPoolExecutor(max_workers=1) as encoder:
            pending: "Future | None" = None

            for track in tracks:
                track_num = track["number"]
                title = track["title"]
                wav_path = os.path.join(tmpdir, f"track{track_num:02d}.wav")

                ok = _rip_with_retry(track_num, wav_path, device)

                if ok and is_unknown:
                    aid = None
                    if config.ACOUSTID_API_KEY:
                        aid = acoustid_lookup.lookup_track(wav_path)
                    if not aid and config.ACRCLOUD_KEY:
                        aid = acrcloud_lookup.lookup_track(wav_path)
                    if aid:
                        acoustid_results.append(aid)
                        if aid.get("title") and title.startswith("Track "):
                            title = aid["title"]
                            log.info("Track %d identified: %s", track_num, title)

                mp3_path = os.path.join(dest_album_dir, f"{track_num:02d}_{_sanitize(title)}.mp3")
                tags = {
                    "artist": artist,
                    "album": album_title,
                    "title": title,
                    "track_number": track_num,
                }

                if pending is not None:
                    try:
                        pending.result()
                    except Exception as e:
                        log.error("Encode error: %s", e)

                pending = encoder.submit(_encode_and_clean, wav_path, mp3_path, tags) if ok else None

            if pending is not None:
                try:
                    pending.result()
                except Exception as e:
                    log.error("Encode error (last track): %s", e)

        # --- Post-rip: resolve Unknown via AcoustID majority vote ---
        final_artist, final_album = artist, album_title
        if is_unknown and acoustid_results:
            resolved = _resolve_unknown_from_acoustid(dest_album_dir, acoustid_results)
            if resolved:
                final_artist, final_album = resolved
                dest_album_dir = _album_dir(final_artist, final_album)

        # --- Post-rip: Discogs enrichment (year, label, genre) ---
        if config.DISCOGS_TOKEN:
            enrichment = discogs_lookup.enrich(final_artist, final_album)
            if enrichment:
                for fname in os.listdir(dest_album_dir):
                    if fname.endswith(".mp3"):
                        _update_id3(os.path.join(dest_album_dir, fname), enrichment)

        log.info("Rip complete: %s", dest_album_dir)
        if disc_id:
            mark_processed(disc_id, {"artist": final_artist, "album": final_album})

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
