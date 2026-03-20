# AI-Powered Music Ripper

A fully automated CD ripping daemon for Linux. Insert a disc, walk away — it identifies the album, rips every track to 320 kbps MP3, tags the files, and ejects the disc when done.

## Features

- **Automatic detection** via udev — no manual commands needed
- **Multi-source metadata lookup** (in priority order):
  1. GnuDB / CDDB
  2. MusicBrainz disc ID
  3. LLM fallback (Minimax, OpenAI, or Claude) — identifies disc from track count and durations
- **LLM disambiguation** — when MusicBrainz returns multiple releases, the LLM picks the most canonical one
- **AcoustID + ACRCloud fingerprinting** ⭐ — per-track audio fingerprinting for discs not found in any database; resolves Unknown Artist/Album via majority vote. **Strongly recommended** — see [Optional integrations](#optional-integrations)
- **Discogs enrichment** (optional) — adds year, label, and genre tags after ripping
- **Duplicate detection** — previously ripped discs are ejected immediately
- **Rip + encode pipeline** — encoding runs in a background thread while the next track is being ripped
- **Timeout + retry** — stuck tracks are retried without error correction; still-stuck tracks are skipped
- **Output structure**: `{MUSIC_DIR}/{Artist}/{Album}/{NN}_{Title}.mp3`
- If an album directory already exists, the new rip is saved under `{Album} (New Copy)`

## Requirements

### System packages

```bash
sudo apt-get install -y cdparanoia lame libdiscid0 eject libchromaprint-tools
```

| Package | Purpose |
|---|---|
| `cdparanoia` | Audio ripping from CD |
| `lame` | MP3 encoding at 320 kbps |
| `libdiscid0` | Read disc TOC / compute MusicBrainz disc ID |
| `eject` | Eject the disc tray when done |
| `libchromaprint-tools` | `fpcalc` binary for AcoustID fingerprinting (optional) |

### Python dependencies

```bash
python3 -m venv venv
venv/bin/pip install musicbrainzngs discid pyudev mutagen requests python-dotenv
```

Or just run the installer:

```bash
bash install.sh
```

## Configuration

Copy the example env file and fill in your values:

```bash
cp .env.example .env
```

Then edit `.env`. The only required field is `MUSIC_DIR` (and at least one LLM API key if you want LLM fallback). Everything else has sensible defaults.

### All variables

| Variable | Default | Description |
|---|---|---|
| `MUSIC_DIR` | `/mnt/music` | Root directory for ripped files |
| `CD_DEVICE` | `/dev/sr0` | CD/DVD device node |
| `RIP_PARANOIA` | `2` | `0` = fastest (no correction), `1` = checksums only, `2` = full paranoia |
| `RIP_TRACK_TIMEOUT` | `300` | Seconds before killing a stuck cdparanoia process |
| `RIP_RETRY_TIMEOUT` | `120` | Timeout for the paranoia-off retry attempt |
| `LLM_PROVIDER` | *(auto)* | Force a provider: `minimax`, `openai`, or `claude`. Auto-detected from whichever key is set (Minimax wins if multiple keys are set). |
| `LLM_TIMEOUT` | `90` | HTTP timeout in seconds for LLM API calls |
| `MINIMAX_API_KEY` | — | Minimax API key |
| `MINIMAX_MODEL` | `abab6.5-chat` | Minimax model name — verify current names at [minimaxi.com](https://www.minimaxi.com) |
| `OPENAI_API_KEY` | — | OpenAI API key |
| `OPENAI_MODEL` | `gpt-4o-mini` | OpenAI model name |
| `CLAUDE_API_KEY` | — | Anthropic Claude API key |
| `CLAUDE_MODEL` | `claude-haiku-4-5-20251001` | Claude model name |
| `ACOUSTID_API_KEY` | — | AcoustID API key — Western music fingerprinting |
| `ACRCLOUD_HOST` | — | ACRCloud host (from project dashboard) |
| `ACRCLOUD_KEY` | — | ACRCloud access key |
| `ACRCLOUD_SECRET` | — | ACRCloud access secret |
| `DISCOGS_TOKEN` | — | Discogs personal access token |

## Usage

```bash
venv/bin/python main.py
```

The daemon runs in the foreground and logs everything to stdout. Insert any audio CD and the pipeline starts automatically. Press `Ctrl+C` to stop.

### Run as a systemd service (optional)

Create `/etc/systemd/system/music-ripper.service`:

```ini
[Unit]
Description=Music Ripper CD daemon
After=multi-user.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/path/to/music_ripper
ExecStart=/path/to/music_ripper/venv/bin/python main.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now music-ripper
```

## Metadata lookup pipeline

```
Insert disc
    │
    ▼
MusicBrainz disc ID
    ├── 1 result ────────────────────────────────────────────────────┐
    ├── multiple results → LLM picks most canonical release ─────────┤
    └── 0 results                                                    │
            │                                                        │
            ▼                                                        │
        GnuDB / CDDB ──── found (not garbled) ───────────────────────┤
            │ not found / garbled encoding                           │
            ▼                                                        │
        LLM identifies from track count + durations ─────────────────┤
                                                                     │
                                                                     ▼
                                                                Rip + encode
                                                                     │
                                                 AcoustID fingerprint (if key set)
                                                 → majority vote resolves Unknown
                                                                     │
                                                 Discogs enrichment (if token set)
                                                 → year / label / genre tags
                                                                     │
                                                                Eject disc
```

MusicBrainz is tried first because it uses a precise SHA-1 disc ID with no encoding ambiguity. GnuDB covers pressings not in MusicBrainz but its entries are sometimes stored in legacy encodings (Shift-JIS, GBK, EUC-KR, etc.) — the ripper auto-detects the encoding and discards results that look garbled, falling through to the LLM instead.

## LLM providers

Set the API key for whichever provider you have access to. Only one is needed. If multiple keys are present, Minimax is used unless `LLM_PROVIDER` overrides it.

| Provider | Key variable | Notes |
|---|---|---|
| Minimax | `MINIMAX_API_KEY` | Default model: `abab6.5-chat` — check [minimaxi.com](https://www.minimaxi.com) for current model names |
| OpenAI | `OPENAI_API_KEY` | Default model: `gpt-4o-mini` |
| Claude | `CLAUDE_API_KEY` | Default model: `claude-haiku-4-5-20251001` |

## Optional integrations

> **For the best identification results, set up at least one audio fingerprinting service.**
> MusicBrainz and GnuDB work from the disc's table of contents — if your pressing isn't in their database, the disc will be saved as Unknown Artist. AcoustID and ACRCloud fingerprint the actual audio waveform and can identify discs that no TOC database knows about. ACRCloud is especially important for Asian music.

### AcoustID

Acoustic fingerprinting for Western music. Only runs when a disc is unidentified (Unknown Artist / Unknown Album). If a majority of tracks match, the directory is renamed and all files are retagged.

**Setup:**
1. Sign in or register at [acoustid.org](https://acoustid.org/login) — a MusicBrainz account works
2. Go to **Your applications** → **Register an application** → copy the API key
3. Install the fingerprinting tool:
   ```bash
   sudo apt install libchromaprint-tools
   ```
4. Add to `.env`:
   ```
   ACOUSTID_API_KEY=your_key_here
   ```

> Note: AcoustID covers mainly Western music. For Asian releases, use ACRCloud instead.

### ACRCloud

Audio fingerprinting with significantly better Asian music coverage (used by TikTok, SoundHound, and major streaming platforms). Runs as a fallback when AcoustID finds nothing.

**Setup:**
1. Sign up at [acrcloud.com](https://www.acrcloud.com) → **Console** → **Create Project**
2. Choose **Audio & Video Recognition**
3. Copy the three credentials from the project dashboard:
   - **Host** (e.g. `identify-us-west-2.acrcloud.com`)
   - **Access Key**
   - **Access Secret**
4. Add to `.env`:
   ```
   ACRCLOUD_HOST=identify-us-west-2.acrcloud.com
   ACRCLOUD_KEY=your_access_key
   ACRCLOUD_SECRET=your_access_secret
   ```

Free tier: 1,000 recognitions/day.

### Discogs

After ripping, the album is searched on Discogs to enrich MP3 tags with year, label, and genre.

1. Create a personal access token at [discogs.com/settings/developers](https://www.discogs.com/settings/developers)
2. Set `DISCOGS_TOKEN` in `.env`

## Troubleshooting

**Permission denied writing to `MUSIC_DIR`**

```bash
sudo chown -R $(whoami) /mnt/music
```

**"Cannot read table of contents"**

The drive may not have finished spinning up. The daemon retries 5 times with a 3-second delay automatically. If it still fails, check that the disc is readable and the device path (`CD_DEVICE`) is correct.

**Disc not detected**

Verify your device node: `ls /dev/sr*`. Update `CD_DEVICE` in `.env` if needed.

**Ripping very slow**

Set `RIP_PARANOIA=0` in `.env` for maximum speed (no error correction). Suitable for discs in good condition.

**LLM fallback not working**

Ensure at least one API key is set in `.env` and that the key is valid. Test manually with `curl` if needed.

## File structure

```
music_ripper/
├── main.py            # udev monitor + entry point
├── config.py          # all configuration, loaded from .env
├── metadata.py        # disc ID lookup pipeline (MusicBrainz → GnuDB → LLM)
├── gnudb.py           # GnuDB/CDDB lookup
├── ripper.py          # ripping, encoding, AcoustID, Discogs, duplicate DB
├── acoustid_lookup.py # per-track AcoustID fingerprinting (Western music)
├── acrcloud_lookup.py # per-track ACRCloud fingerprinting (Asian music)
├── discogs_lookup.py  # album-level Discogs enrichment
├── install.sh         # dependency installer
├── .env               # your local configuration (not committed)
└── .env.example       # template with all variables documented
```

The processed-disc database is stored at `~/.music_ripper_processed.json`.
