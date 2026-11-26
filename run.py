"""Start Jukebox.   python run.py            (uses config.toml)
                  python run.py --demo     (fake Plex server, nothing to set up)"""
from __future__ import annotations

import argparse
import logging
import os
import secrets
import socket
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib

import uvicorn

from jukebox import __version__
from jukebox.app import Hub, create_app
from jukebox.backend import DemoBackend, PlexBackend, redact
from jukebox.db import DB
from jukebox.importer import Importer
from jukebox.sync import SyncEngine

ROOT = Path(__file__).resolve().parent

DEFAULTS = {
    "plex_url": "http://127.0.0.1:32400",
    "plex_token": "",
    "host": "0.0.0.0",
    "port": 8765,
    "public_url": "",
    "playlist_prefix": "👥 ",
    "poll_seconds": 20,
    "pull_from_plex": True,
    "allowed_users": [],
    # Importing from Spotify (optional keys: without them, only the first 100 songs).
    "spotify_client_id": "",
    "spotify_client_secret": "",
    # Downloading songs that aren't in Plex: deemix, or Lidarr. Who may: owner | everyone | off.
    "deemix_url": "",
    "deemix_arl": "",
    "deemix_bitrate": "",
    "lidarr_url": "",
    "lidarr_api_key": "",
    "lidarr_root_folder": "",
    "lidarr_quality_profile": "",
    "lidarr_metadata_profile": "",
    "downloads": "owner",
    "wanted_check_minutes": 10,
}


# Settings that can also come from the environment (used by docker-compose).
ENV = {
    "PLEX_URL": ("plex_url", str),
    "PLEX_TOKEN": ("plex_token", str),
    "PUBLIC_URL": ("public_url", str),
    "PLAYLIST_PREFIX": ("playlist_prefix", str),
    "POLL_SECONDS": ("poll_seconds", int),
    "PULL_FROM_PLEX": ("pull_from_plex", lambda v: v.strip().lower() in ("1", "true", "yes", "on")),
    "ALLOWED_USERS": ("allowed_users", lambda v: [u.strip() for u in v.split(",") if u.strip()]),
    "SPOTIFY_CLIENT_ID": ("spotify_client_id", str),
    "SPOTIFY_CLIENT_SECRET": ("spotify_client_secret", str),
    "DEEMIX_URL": ("deemix_url", str),
    "DEEMIX_ARL": ("deemix_arl", str),
    "DEEMIX_BITRATE": ("deemix_bitrate", str),
    "LIDARR_URL": ("lidarr_url", str),
    "LIDARR_API_KEY": ("lidarr_api_key", str),
    "LIDARR_ROOT_FOLDER": ("lidarr_root_folder", str),
    "LIDARR_QUALITY_PROFILE": ("lidarr_quality_profile", str),
    "LIDARR_METADATA_PROFILE": ("lidarr_metadata_profile", str),
    "DOWNLOADS": ("downloads", str),
    "WANTED_CHECK_MINUTES": ("wanted_check_minutes", int),
}


def persistent_secret(path: Path) -> str:
    if not path.exists():
        path.write_text(secrets.token_hex(32))
    return path.read_text().strip()


def main():
    ap = argparse.ArgumentParser(description="Shared, live-synced Plex music playlists")
    ap.add_argument("--config", default=os.environ.get("JUKEBOX_CONFIG", str(ROOT / "config.toml")))
    ap.add_argument("--demo", action="store_true", help="run against a fake Plex server")
    ap.add_argument("--port", type=int)
    ap.add_argument("--log-file", help="also write the log to this file (rotated at 1 MB)")
    ap.add_argument("--check", action="store_true", help="test the Plex connection and exit")
    args = ap.parse_args()

    # Windows consoles can't always print emoji/accents; never let that crash logging.
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")

    class RedactingFormatter(logging.Formatter):  # tracebacks included
        def format(self, record):
            return redact(super().format(record))

    handlers = [logging.StreamHandler()]
    if args.log_file:
        handlers.append(RotatingFileHandler(args.log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
    fmt = RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    for h in handlers:
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=handlers)
    log = logging.getLogger("jukebox")

    cfg = dict(DEFAULTS)
    cfg_path = Path(args.config)
    txt = cfg_path.with_name(cfg_path.name + ".txt")
    if not cfg_path.exists() and txt.exists():  # Notepad + hidden file extensions
        log.warning("Using %s. Rename it to %s when you get a chance.", txt.name, cfg_path.name)
        cfg_path = txt
    if cfg_path.exists():
        try:
            # utf-8-sig: Notepad sometimes adds an invisible BOM at the start.
            cfg.update(tomllib.loads(cfg_path.read_text(encoding="utf-8-sig")))
        except tomllib.TOMLDecodeError as e:
            sys.exit(f"config.toml has a mistake: {e}\n"
                     f'Text values need straight double quotes, e.g.  plex_token = "abc123"')
    elif not args.demo and not os.environ.get("PLEX_TOKEN"):
        sys.exit(f"No config file at {cfg_path}. Copy config.example.toml to config.toml and fill it in "
                 f"(or try `python run.py --demo`). With Docker, set PLEX_TOKEN in .env instead.")
    for var, (key, conv) in ENV.items():
        if os.environ.get(var, "").strip():
            cfg[key] = conv(os.environ[var])
    if args.port or os.environ.get("PORT"):
        cfg["port"] = args.port or int(os.environ["PORT"])
    # Claim the port before anything else runs, so two copies can never sync at once.
    sock = None if args.check else claim_port(cfg["host"], int(cfg["port"]))

    data = Path(os.environ.get("JUKEBOX_DATA") or ROOT / "data")
    if args.demo:
        data = data.with_name(data.name + "-demo")
    data.mkdir(parents=True, exist_ok=True)
    cfg["secret"] = persistent_secret(data / "session.key")
    cfg["client_id"] = persistent_secret(data / "client.id")

    if args.demo:
        backend = DemoBackend(seed_playlists=True)
        db_path = ":memory:"
    else:
        if not cfg["plex_token"]:
            sys.exit("Set plex_token in config.toml, or PLEX_TOKEN in .env for Docker "
                     "(see README for how to find it).")
        backend = connect_to_plex(cfg, log, wait_seconds=0 if args.check else 600)
        db_path = str(data / "jukebox.db")
    if args.check:
        print(f"OK: connected to Plex server '{getattr(getattr(backend, 'server', None), 'friendlyName', 'demo')}'.")
        print("Members:", ", ".join(m.title for m in backend.members()))
        return

    db = DB(db_path)
    owner = next(m for m in backend.members() if m.is_owner)
    moved = db.give_unshared_playlists_to(owner.id)
    if moved:
        log.info("Upgrade: %d existing playlist(s) are now shared with %s only. "
                 "Add people from the web page.", moved, owner.title)
    hub = Hub()
    sync = SyncEngine(db, backend, cfg, notify=hub.notify)
    if not db.playlist_ids() and not args.demo:
        # Fresh install or lost database: pick up the shared playlists already in people's Plex.
        try:
            found = sync.recover()
            log.info("Recovered %d shared playlist(s) from Plex", len(found)) if found else None
        except Exception:
            log.exception("Couldn't look for earlier shared playlists")
    importer = Importer(db, backend, cfg, notify=hub.notify, kick=sync.kick)
    if args.demo:
        from jukebox.downloaders import DemoDownloader

        importer.downloader, cfg["downloads"] = DemoDownloader(backend), "everyone"
    if importer.downloader:
        log.info("Downloads go to %s (allowed for: %s)", importer.downloader.name, cfg["downloads"])
    app = create_app(cfg, backend, db, sync, hub, importer)
    log.info("Jukebox %s", __version__)
    log.info("Members: %s", ", ".join(m.title for m in backend.members()))
    log.info("Open http://localhost:%s", cfg["port"])
    # log_config=None: uvicorn's own messages go through the handlers set up above.
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", log_config=None))
    server.run(sockets=[sock])


def claim_port(host, port):
    """Open the listening socket ourselves, exclusively.

    uvicorn's default socket uses SO_REUSEADDR, which on Windows lets a second copy bind the
    same port without any error. Two copies would then fight over everyone's playlists.
    """
    def already_running():
        sys.exit(f"Port {port} is already in use, so Jukebox is probably already running "
                 f"(an open start.bat window, or the startup task). Close it first, "
                 f"or run install-windows.bat, which stops old copies.")

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            already_running()
    except OSError:
        pass  # nothing answering: good
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
    except OSError:
        already_running()
    s.listen(2048)
    s.set_inheritable(True)
    return s


def connect_to_plex(cfg, log, wait_seconds=600):
    """Connect, waiting up to 10 minutes for Plex to come up (it may start after us at boot)."""
    from plexapi.exceptions import Unauthorized

    deadline = time.time() + wait_seconds
    while True:
        try:
            return PlexBackend(cfg["plex_url"], cfg["plex_token"], cfg["allowed_users"])
        except Unauthorized:
            sys.exit("Plex rejected plex_token. Check it in config.toml (see README).")
        except Exception as e:
            if time.time() > deadline:
                sys.exit(f"Can't reach Plex at {cfg['plex_url']}: {e}\n"
                         f"Is Plex running, and is plex_url in config.toml (PLEX_URL in .env for Docker) right?")
            log.warning("Can't reach Plex at %s yet (%s); retrying in 15 s", cfg["plex_url"], e)
            time.sleep(15)


if __name__ == "__main__":
    main()
