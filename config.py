import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

MUSIC_DIR = os.environ.get('MUSIC_DIR', '/media/asriver/data/Music')
DEVICE = os.environ.get('CD_DEVICE', '/dev/sr0')
PROCESSED_DB = os.path.expanduser('~/.music_ripper_processed.json')
TEMP_DIR = '/tmp/music_ripper'

# LLM provider: "minimax" | "openai" | "claude"
# Auto-detected from whichever API key is set; MINIMAX takes priority if multiple are set.
LLM_PROVIDER = os.environ.get('LLM_PROVIDER', '')

MINIMAX_API_KEY = os.environ.get('MINIMAX_API_KEY', '')
MINIMAX_API_URL = os.environ.get('MINIMAX_API_URL', 'https://api.minimax.io/v1/text/chatcompletion_v2')
MINIMAX_MODEL   = os.environ.get('MINIMAX_MODEL', 'abab6.5s-chat')

OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')
OPENAI_API_URL = os.environ.get('OPENAI_API_URL', 'https://api.openai.com/v1/chat/completions')
OPENAI_MODEL   = os.environ.get('OPENAI_MODEL', 'gpt-4o-mini')

CLAUDE_API_KEY = os.environ.get('CLAUDE_API_KEY', '')
CLAUDE_API_URL = os.environ.get('CLAUDE_API_URL', 'https://api.anthropic.com/v1/messages')
CLAUDE_MODEL   = os.environ.get('CLAUDE_MODEL', 'claude-haiku-4-5-20251001')

# LLM request timeout in seconds
LLM_TIMEOUT = int(os.environ.get('LLM_TIMEOUT', '90'))

# AcoustID acoustic fingerprinting (https://acoustid.org)
ACOUSTID_API_KEY = os.environ.get('ACOUSTID_API_KEY', '')

# ACRCloud audio recognition — better Asian music coverage (https://acrcloud.com)
ACRCLOUD_HOST   = os.environ.get('ACRCLOUD_HOST', '')
ACRCLOUD_KEY    = os.environ.get('ACRCLOUD_KEY', '')
ACRCLOUD_SECRET = os.environ.get('ACRCLOUD_SECRET', '')

# Discogs metadata enrichment (https://www.discogs.com/settings/developers)
DISCOGS_TOKEN = os.environ.get('DISCOGS_TOKEN', '')

# Ripping options
# Paranoia level: 0=off (fastest, no error correction), 1=checksums only, 2=full (default)
RIP_PARANOIA = int(os.environ.get('RIP_PARANOIA', '2'))
# Seconds to wait for a single track before killing cdparanoia and retrying
RIP_TRACK_TIMEOUT = int(os.environ.get('RIP_TRACK_TIMEOUT', '300'))
# Retry timeout (seconds) with paranoia disabled when the first attempt times out
RIP_RETRY_TIMEOUT = int(os.environ.get('RIP_RETRY_TIMEOUT', '120'))

MUSICBRAINZ_APP = 'MusicRipper'
MUSICBRAINZ_VERSION = '1.0'
MUSICBRAINZ_CONTACT = 'local'


def active_llm_provider() -> str:
    """Return the effective LLM provider based on config."""
    if LLM_PROVIDER:
        return LLM_PROVIDER.lower()
    if MINIMAX_API_KEY:
        return 'minimax'
    if OPENAI_API_KEY:
        return 'openai'
    if CLAUDE_API_KEY:
        return 'claude'
    return ''
