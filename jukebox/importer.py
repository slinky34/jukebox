"""Importing playlists from other services, and songs that aren't in Plex yet.

An import runs in the background (matching 100 songs takes a little while): read the
playlist, look for each song in Plex, then let the person create a shared playlist from the
matches. Songs not found are kept as "waiting" on the playlist. Every few minutes (and right
after a download is requested) the waiting songs are looked for again, and any that have
arrived in Plex are added in their place.
"""
from __future__ import annotations

import logging
import secrets
import threading
import time

from . import external
from .downloaders import DownloadError, from_config, tidy
from .matching import best_match, clean_title

log = logging.getLogger("jukebox.import")


class Importer:
    def __init__(self, db, backend, cfg, notify=lambda msg: None, kick=lambda: None):
        self.db, self.backend, self.cfg, self.notify, self.kick = db, backend, cfg, notify, kick
        self.downloader = from_config(cfg)
        self.jobs = {}
        self._lock = threading.Lock()
        self._wake, self._stop = threading.Event(), threading.Event()
        self.every = max(1, int(cfg.get("wanted_check_minutes", 10))) * 60

    # ---- background checks for waiting songs -------------------------------

    def start(self):
        threading.Thread(target=self._loop, name="wanted", daemon=True).start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def check_soon(self):
        self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            self._wake.wait(self.every)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.check_wanted()
            except Exception:
                log.exception("Checking waiting songs failed")

    def match(self, song, member=None):
        """The Plex track for an outside song, or None."""
        queries = [clean_title(song["title"])]
        if queries[0] != song["title"]:
            queries.append(song["title"])
        for q in queries:
            try:
                hit = best_match(song, self.backend.find_tracks(q, member, 20))
            except Exception as e:
                log.warning("Plex search failed for %r: %s", q, e)
                return None
            if hit:
                return hit
        return None

    def check_wanted(self):
        """Add waiting songs that have turned up in Plex. Returns how many were added."""
        added = 0
        for w in self.db.all_wanted():
            hit = self.match(w)  # owner's view of the library: every library counts
            self.db.set_wanted(w["id"], checked_at=time.time())
            if not hit:
                continue
            keys = self.db.playlist_keys(w["playlist_id"])
            index = keys.index(w["after_key"]) + 1 if w["after_key"] in keys else None
            if self.db.add_tracks(w["playlist_id"], [hit], w["added_by"], index) or hit["key"] in keys:
                self.db.delete_wanted(w["id"])
                added += 1
                log.info("%r arrived in Plex; added to playlist %s", hit["title"], w["playlist_id"])
                self.notify({"type": "playlist", "id": w["playlist_id"], "who": "Jukebox",
                             "action": f"added “{hit['title']}” (it's in Plex now)"})
        if added:
            self.kick()
        return added

    # ---- downloads -----------------------------------------------------------

    def can_download(self, member) -> bool:
        who = (self.cfg.get("downloads") or "owner").strip().lower()
        return bool(self.downloader) and (who == "everyone" or (who == "owner" and member.is_owner))

    def request_download(self, wid: int, who: str) -> dict:
        w = self.db.get_wanted(wid)
        if not w:
            raise LookupError("That song isn't waiting any more")
        if not self.downloader:
            raise DownloadError("No downloader is set up")
        try:
            res = self.downloader.request(w)
        except DownloadError as e:
            self.db.set_wanted(wid, status="failed", note=tidy(e))
            raise
        self.db.set_wanted(wid, status="requested", note=tidy(res["note"]), link=res.get("link"),
                           album=res.get("album") or w["album"])
        log.info("%s asked %s for %r by %s", who, self.downloader.name, w["title"], w["artist"])
        return res

    # ---- imports ---------------------------------------------------------------

    def start_import(self, member, url) -> str:
        kind, _ = external.parse_link(url)  # bad links fail right away, before a job exists
        with self._lock:
            now = time.time()
            for jid in [j for j, v in self.jobs.items() if now - v["started"] > 3600]:
                del self.jobs[jid]
            if any(v["member"] == member.id and v["state"] == "running" for v in self.jobs.values()):
                raise ValueError("You already have an import running")
            jid = secrets.token_urlsafe(12)
            self.jobs[jid] = {"member": member.id, "state": "running", "started": now, "done": 0,
                              "total": 0, "error": None, "result": None, "kind": kind}
        threading.Thread(target=self._run, args=(jid, member, url), daemon=True).start()
        return jid

    def job(self, jid, member):
        j = self.jobs.get(jid)
        return j if j and j["member"] == member.id else None

    def _run(self, jid, member, url):
        j = self.jobs[jid]
        try:
            pl = external.fetch_playlist(url, self.cfg)
            j["total"] = len(pl["tracks"])
            items = []
            for t in pl["tracks"]:
                hit = self.match(t, member)
                items.append({**t, "match": hit})
                j["done"] += 1
            j["result"] = {"name": pl["name"], "source": pl["source"], "note": pl["note"], "items": items}
            j["state"] = "done"
            log.info("%s looked up %s playlist %r: %d of %d songs in Plex", member.title, pl["source"],
                     pl["name"], sum(1 for i in items if i["match"]), len(items))
        except external.ImportError_ as e:
            j["state"], j["error"] = "error", str(e)
        except Exception as e:
            log.exception("Import failed")
            j["state"], j["error"] = "error", f"Couldn't read that playlist ({type(e).__name__})."

    def create_from_job(self, jid, member, name, keep_missing=True):
        """Make the shared playlist from a finished import. Returns (pid, added, waiting)."""
        j = self.job(jid, member)
        if not j or j["state"] != "done":
            raise LookupError("That import has expired. Paste the link again.")
        res = j["result"]
        name = (name or res["name"]).strip()[:120] or res["name"]
        pid = self.db.create_playlist(name, member.title, member.id)
        tracks, seen, wanted, last = [], set(), [], None
        for it in res["items"]:
            m = it["match"]
            if m:
                if m["key"] not in seen:
                    seen.add(m["key"])
                    tracks.append(m)
                    last = m["key"]
            elif keep_missing:
                wanted.append({**{k: it.get(k) for k in ("title", "artist", "album", "duration", "link")},
                               "after_key": last})
        self.db.add_tracks(pid, tracks, member.title)
        if wanted:
            self.db.add_wanted(pid, wanted, member.title)
        with self._lock:
            self.jobs.pop(jid, None)
        self.kick()
        return pid, len(tracks), len(wanted)

    def want(self, pid, song, who, after_key=None):
        """Add one outside song (e.g. a Deezer search result) to a playlist's waiting list,
        or straight to the playlist if it's already in Plex."""
        hit = self.match(song)
        if hit:
            self.db.add_tracks(pid, [hit], who)
            self.kick()
            return {"added": hit}
        keys = self.db.playlist_keys(pid)
        self.db.add_wanted(pid, [{**song, "after_key": after_key or (keys[-1] if keys else None)}], who)
        return {"waiting": self.db.wanted_for(pid)[-1]}
