#!/usr/bin/env python3
"""
CD Ripper daemon.

Monitors for disc insertion via udev, looks up metadata (MusicBrainz +
LLM fallback), rips tracks to 320kbps MP3, then ejects the disc.
Previously processed discs are detected and ejected immediately.

Usage:
    python main.py
"""

import logging
import os
import sys
import time

import pyudev

import config
import metadata as meta
import ripper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# musicbrainzngs emits many INFO messages for unrecognised XML attributes; suppress them
logging.getLogger("musicbrainzngs").setLevel(logging.WARNING)
log = logging.getLogger("main")

# Seconds to wait after a disc-insertion event before reading TOC.
# Gives the drive time to spin up and the kernel time to settle.
DISC_SPINUP_DELAY = 3

# Minimum seconds between two handled events on the same device.
# Prevents the eject-triggered udev burst from re-entering the pipeline.
DEBOUNCE_SECONDS = 15


def _check_output_dir():
    """Warn early if the music directory isn't writable."""
    music_dir = config.MUSIC_DIR
    # Walk up until we find an existing ancestor
    check = music_dir
    while check and not os.path.exists(check):
        check = os.path.dirname(check)
    if check and not os.access(check, os.W_OK):
        log.error(
            "Output directory '%s' is not writable by the current user. "
            "Fix with: sudo chown -R %s %s",
            check, os.environ.get("USER", "$(whoami)"), music_dir,
        )
        return False
    return True


def handle_disc_inserted(device_path: str):
    """Full pipeline for a newly inserted disc."""
    log.info("=== Disc inserted in %s — waiting %ds for spin-up ===",
             device_path, DISC_SPINUP_DELAY)
    time.sleep(DISC_SPINUP_DELAY)

    # --- Metadata ---
    try:
        album = meta.get_album_metadata(device_path)
    except Exception as e:
        if "no actual audio tracks" in str(e):
            log.info("Data disc detected (no audio tracks) — ejecting")
        else:
            log.exception("Could not read disc metadata")
        ripper.eject(device_path)
        return

    disc_id = album.get("disc_id", "")
    log.info("Identified: %s – %s  (%d tracks)",
             album["artist"], album["album"], len(album["tracks"]))

    # --- Duplicate check ---
    if disc_id and ripper.is_processed(disc_id):
        log.info("Disc '%s' was already processed. Ejecting.", disc_id)
        ripper.eject(device_path)
        return

    # --- Permission pre-check ---
    if not _check_output_dir():
        log.error("Aborting rip — fix directory permissions and re-insert the disc.")
        ripper.eject(device_path)
        return

    # --- Rip ---
    try:
        ripper.process_disc(album, device_path)
    except PermissionError as e:
        log.error(
            "Permission denied writing to output directory: %s\n"
            "Fix with: sudo chown -R %s %s",
            e, os.environ.get("USER", "$(whoami)"), config.MUSIC_DIR,
        )
    except Exception as e:
        log.error("Ripping failed: %s", e)
    finally:
        ripper.eject(device_path)


def monitor():
    """Watch udev for disc insertion events and handle them."""
    context = pyudev.Context()
    mon = pyudev.Monitor.from_netlink(context)
    mon.filter_by("block")

    log.info("CD Ripper started. Waiting for disc insertion on %s …", config.DEVICE)
    log.info("Music output: %s", config.MUSIC_DIR)
    _check_output_dir()

    last_handled: float = 0.0

    for device in iter(mon.poll, None):
        if device.action != "change":
            continue
        if device.device_node != config.DEVICE:
            continue
        # ID_CDROM_MEDIA=1 means media is present
        if device.get("ID_CDROM_MEDIA") != "1":
            continue
        # Skip blank recordable discs (no tracks written yet)
        if (device.get("ID_CDROM_MEDIA_CD_R") or device.get("ID_CDROM_MEDIA_CD_RW")) \
                and not device.get("ID_CDROM_MEDIA_TRACK_COUNT_AUDIO"):
            log.info("Detected blank recordable disc — skipping")
            continue
        if not device.get("ID_CDROM_MEDIA_TRACK_COUNT_AUDIO"):
            log.info("Detected data disc (no audio tracks) — skipping")
            continue

        # Debounce: ignore events that arrive within DEBOUNCE_SECONDS of the last one
        now = time.monotonic()
        if now - last_handled < DEBOUNCE_SECONDS:
            log.debug("Ignoring udev event — within debounce window")
            continue
        last_handled = now

        try:
            handle_disc_inserted(config.DEVICE)
        except Exception as e:
            log.exception("Unexpected error handling disc: %s", e)


if __name__ == "__main__":
    if not config.active_llm_provider():
        log.warning(
            "No LLM API key configured. "
            "Set MINIMAX_API_KEY, OPENAI_API_KEY, or CLAUDE_API_KEY in .env"
        )
    try:
        monitor()
    except KeyboardInterrupt:
        log.info("Stopped.")
        sys.exit(0)
