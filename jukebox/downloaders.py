"""Asking Lidarr or deemix to fetch songs that aren't in Plex yet.

- deemix downloads the single song (from Deezer).
- Lidarr works in albums: it adds the song's album (and its artist, without monitoring the
  artist's other albums) and searches for it.

Once the files land in your music library and Plex scans them, Jukebox notices on its next
check and adds the song to the playlist that asked for it.
"""
from __future__ import annotations

import logging

import requests

from .external import deezer_lookup
from .matching import artists, similar

log = logging.getLogger("jukebox.download")
TIMEOUT = 30


class DownloadError(Exception):
    pass


def from_config(cfg):
    """The configured downloader, or None. deemix wins if both are set (it gets single songs)."""
    if cfg.get("deemix_url") and cfg.get("deemix_arl"):
        return Deemix(cfg["deemix_url"], cfg["deemix_arl"], cfg.get("deemix_bitrate"))
    if cfg.get("lidarr_url") and cfg.get("lidarr_api_key"):
        return Lidarr(cfg["lidarr_url"], cfg["lidarr_api_key"], cfg.get("lidarr_root_folder"),
                      cfg.get("lidarr_quality_profile"), cfg.get("lidarr_metadata_profile"))
    return None


def _need_deezer(song):
    """Make sure the song has an album and Deezer link (looked up by title and artist)."""
    if song.get("link") and song.get("album"):
        return song
    hit = deezer_lookup(song["title"], song.get("artist", ""))
    if hit:
        song = dict(song, link=song.get("link") or hit["link"], album=song.get("album") or hit["album"])
    return song


class Deemix:
    name = "deemix"

    def __init__(self, url, arl, bitrate=None):
        self.url, self.arl = url.rstrip("/"), arl
        self.bitrate = int(bitrate) if str(bitrate or "").isdigit() else None
        self.s = requests.Session()
        self._logged_in = False

    def _login(self):
        r = self.s.post(f"{self.url}/api/loginArl", json={"arl": self.arl, "force": True}, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json() if r.content else {}
        if d.get("status") in (0, False) or d.get("error"):
            raise DownloadError("deemix didn't accept the ARL (DEEMIX_ARL). It may have expired.")
        self._logged_in = True

    def request(self, song) -> dict:
        song = _need_deezer(song)
        if not song.get("link"):
            raise DownloadError("Couldn't find this song on Deezer")
        try:
            if not self._logged_in:
                self._login()
            body = {"url": song["link"], "bitrate": self.bitrate}
            r = self.s.post(f"{self.url}/api/addToQueue", json=body, timeout=TIMEOUT)
            if r.status_code in (401, 403) or (r.ok and r.content and r.json().get("errid") == "NotLoggedIn"):
                self._login()
                r = self.s.post(f"{self.url}/api/addToQueue", json=body, timeout=TIMEOUT)
            r.raise_for_status()
            d = r.json() if r.content else {}
        except requests.RequestException as e:
            raise DownloadError(f"Couldn't reach deemix: {e}")
        if d.get("result") is False:
            raise DownloadError(f"deemix refused it: {d.get('errid') or d.get('data') or 'unknown error'}")
        return {"link": song["link"], "album": song.get("album", ""), "note": "Sent to deemix"}


class Lidarr:
    name = "Lidarr"

    def __init__(self, url, api_key, root=None, quality=None, metadata=None):
        self.url = url.rstrip("/")
        self.h = {"X-Api-Key": api_key}
        self.root, self.quality, self.metadata = root, quality, metadata
        self._defaults = None

    def _api(self, method, path, **kw):
        try:
            r = requests.request(method, f"{self.url}/api/v1{path}", headers=self.h, timeout=TIMEOUT, **kw)
        except requests.RequestException as e:
            raise DownloadError(f"Couldn't reach Lidarr: {e}")
        if r.status_code == 401:
            raise DownloadError("Lidarr didn't accept LIDARR_API_KEY")
        if not r.ok:
            raise DownloadError(f"Lidarr said {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else None

    def _pick(self, items, wanted, what):
        if wanted:
            for it in items:
                if str(it.get("id")) == str(wanted) or (it.get("name") or it.get("path")) == wanted:
                    return it
            raise DownloadError(f"Lidarr has no {what} called {wanted!r}")
        if not items:
            raise DownloadError(f"Lidarr has no {what} set up yet")
        return items[0]

    def defaults(self):
        if self._defaults is None:
            root = self._pick(self._api("GET", "/rootfolder"), self.root, "root folder")
            qual = self._pick(self._api("GET", "/qualityprofile"), self.quality, "quality profile")
            meta = self._pick(self._api("GET", "/metadataprofile"), self.metadata, "metadata profile")
            self._defaults = {"rootFolderPath": root["path"], "qualityProfileId": qual["id"],
                              "metadataProfileId": meta["id"]}
        return self._defaults

    def request(self, song) -> dict:
        song = _need_deezer(song)
        album_name = song.get("album") or ""
        if not album_name:
            raise DownloadError("Don't know which album this song is on, so Lidarr can't get it")
        found = self._api("GET", "/album/lookup", params={"term": f"{song.get('artist', '')} {album_name}"}) or []
        want = artists(song.get("artist", ""))
        best, best_s = None, 0.0
        for a in found:
            art = (a.get("artist") or {}).get("artistName", "")
            if want and not (artists(art) & want) and max((similar(art, w) for w in want), default=0) < 0.8:
                continue
            s = similar(a.get("title", ""), album_name)
            if s > best_s:
                best, best_s = a, s
        if not best or best_s < 0.6:
            raise DownloadError(f"Lidarr couldn't find the album {album_name!r}")
        title = f"{best.get('title')} by {(best.get('artist') or {}).get('artistName', '?')}"
        if best.get("id"):  # already in Lidarr: make sure it's wanted, then search
            self._api("PUT", "/album/monitor", json={"albumIds": [best["id"]], "monitored": True})
            self._api("POST", "/command", json={"name": "AlbumSearch", "albumIds": [best["id"]]})
            return {"album": album_name, "link": song.get("link"), "note": f"Lidarr is searching for {title}"}
        d = self.defaults()
        best["monitored"] = True
        best["addOptions"] = {"searchForNewAlbum": True}
        artist = best.get("artist") or {}
        artist.update(d, monitored=True, monitorNewItems="none",
                      addOptions={"monitor": "none", "searchForMissingAlbums": False})
        best["artist"] = artist
        self._api("POST", "/album", json=best)
        return {"album": album_name, "link": song.get("link"), "note": f"Added {title} to Lidarr"}


def tidy(note) -> str:
    return str(note)[:300]


class DemoDownloader:
    """For --demo: the song "arrives" in the fake library straight away."""
    name = "the demo downloader"

    def __init__(self, backend):
        self.backend = backend

    def request(self, song) -> dict:
        key = str(900000 + len(self.backend.library))
        self.backend.library[key] = {"key": key, "title": song["title"], "artist": song.get("artist") or "?",
                                     "album": song.get("album") or "Downloads", "duration": song.get("duration") or 180000}
        return {"album": song.get("album") or "", "link": song.get("link"),
                "note": "Downloaded (demo). It'll appear on the next check"}
