import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
DATA_ROOT = Path(os.environ.get("DATA_ROOT", "/data"))
TV_DIR = DATA_ROOT / "torrents" / "tv"
MOVIE_DIR = DATA_ROOT / "torrents" / "movies"

TG_API_ID = int(os.environ.get("TG_API_ID") or 0)
TG_API_HASH = os.environ.get("TG_API_HASH", "")
SESSION = str(CONFIG_DIR / "tgarr")

SONARR_URL = os.environ.get("SONARR_URL", "http://sonarr:8989")
SONARR_KEY = os.environ.get("SONARR_KEY", "")
SONARR_ROOT = os.environ.get("SONARR_ROOT", "/data/media/tv")
SONARR_PROFILE = int(os.environ.get("SONARR_PROFILE", "6"))

RADARR_URL = os.environ.get("RADARR_URL", "http://radarr:7878")
RADARR_KEY = os.environ.get("RADARR_KEY", "")
RADARR_ROOT = os.environ.get("RADARR_ROOT", "/data/media/movies")
RADARR_PROFILE = int(os.environ.get("RADARR_PROFILE", "6"))

PLEX_URL = os.environ.get("PLEX_URL", "")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
# arr path prefix = plex path prefix (Plex mounts /mnt/storage, arrs mount /mnt/storage/data)
PLEX_PATH_MAP = os.environ.get("PLEX_PATH_MAP", "/data/media=/data/data/media")

WORKERS = int(os.environ.get("WORKERS", "2"))
SCAN_LIMIT = int(os.environ.get("SCAN_LIMIT", "3000"))

# ---- Sonarr/Prowlarr integration (Newznab indexer + SABnzbd-compatible download client) ----
TELEGRAM_DIR = DATA_ROOT / "telegram"  # where downloads grabbed by Sonarr land; Sonarr moves them into the library
INDEX_REFRESH = int(os.environ.get("INDEX_REFRESH", "600"))  # seconds between channel index refreshes


def api_key() -> str:
    """Shared key for the Newznab and SABnzbd endpoints; generated once and kept in /config/apikey."""
    import secrets
    f = CONFIG_DIR / "apikey"
    if not f.exists():
        f.write_text(secrets.token_hex(16))
        f.chmod(0o600)
    return f.read_text().strip()
