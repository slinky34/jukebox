"""Background sync between the master playlists and each user's copy in Plex.

Every cycle, for every shared playlist:
  1. pull  - read each user's Plex copy; tracks they added or removed inside Plex/Plexamp
             since our last push are applied to the master.
  2. push  - bring every user's Plex copy in line with the master.

A cycle runs every `poll_seconds`, and right away (after a short pause to batch edits)
whenever someone changes a playlist in the web app.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time

from .backend import redact

log = logging.getLogger("jukebox.sync")

FAILED = object()  # marks a Plex copy we couldn't read this cycle


class SyncEngine:
    def __init__(self, db, backend, cfg, notify=lambda msg: None):
        self.db, self.backend, self.notify = db, backend, notify
        self.poll = max(5, int(cfg.get("poll_seconds", 10)))
        self.pull_enabled = bool(cfg.get("pull_from_plex", True))
        self.prefix = cfg.get("playlist_prefix", "")
        self._wake, self._stop = threading.Event(), threading.Event()
        self._run_lock = threading.Lock()
        self.last_run = None
        self.last_error = None

    # ---- loop --------------------------------------------------------------

    def start(self):
        self._wake.set()  # run once right away
        threading.Thread(target=self._loop, name="sync", daemon=True).start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def kick(self):
        self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            if self._wake.wait(self.poll):
                time.sleep(0.5)  # let a burst of edits land first
            if self._stop.is_set():
                break
            self._wake.clear()
            try:
                self.run_once()
            except Exception as e:
                log.exception("Sync cycle failed")
                self.last_error = redact(e)

    def run_once(self):
        with self._run_lock:
            members = self.backend.members()
            by_id = {m.id: m for m in members}
            self._process_deletes(by_id)
            for pid in self.db.playlist_ids():
                shared_with = self.db.member_ids(pid)
                pl_members = [m for m in members if m.id in shared_with]
                keep = {m.id for m in pl_members}
                for mir in self.db.mirrors_for(pid):
                    if mir["member_id"] not in keep:  # removed from the playlist (or the server)
                        self.db.retire_mirror(pid, mir["member_id"])
                self._sync_playlist(pid, pl_members)
            self._process_deletes(by_id)
            self.last_run, self.last_error = time.time(), None

    # ---- importing ----------------------------------------------------------

    def import_playlist(self, member, plex_key, keep_original=False):
        """Turn one of `member`'s own Plex playlists into a shared playlist (shared with just them).

        Unless keep_original is set, their playlist itself becomes their synced copy: it gets
        renamed with the prefix and from then on follows the shared playlist. Smart playlists
        can't be edited that way, so those are always copied.
        Returns (playlist id, number of songs, whether the original was adopted).
        """
        with self._run_lock:  # no sync cycle may see the playlist half-made
            info = next((p for p in self.backend.user_playlists(member) if p["key"] == plex_key), None)
            if info is None:
                raise LookupError("That playlist isn't in your Plex any more")
            keys = self.backend.user_playlist_keys(member, plex_key) or []
            tracks = self.backend.get_tracks(list(dict.fromkeys(keys)))
            if not tracks:
                raise ValueError("That playlist has no songs in it")
            name = info["title"]
            if self.prefix and name.startswith(self.prefix):  # e.g. a copy left from an old install
                name = name[len(self.prefix):].strip() or name
            pid = self.db.create_playlist(name, member.title, member.id)
            self.db.add_tracks(pid, tracks, member.title)
            adopt = not keep_original and not info["smart"]
            if adopt:
                target = self.db.playlist_keys(pid)
                # Next cycle renames it and drops any duplicates; nothing to pull from it.
                self.db.save_mirror(pid, member.id, plex_key, target, target, info["title"])
        log.info("%s turned their playlist %r into a shared one (%d songs, %s)", member.title,
                 info["title"], len(tracks), "adopted" if adopt else "copied")
        self.kick()
        return pid, len(tracks), adopt

    # ---- recovering playlists from an earlier install ---------------------------

    def _scan_leftovers(self, members, names=None):
        """Prefixed playlists in people's Plex that Jukebox isn't syncing ("leftovers").

        Returns ({name: [(member, plex key, title, song count), ...]}, people whose Plex
        didn't answer). Every copy is included: someone can have several with the same name.
        Must hold _run_lock.
        """
        missed = []
        if not self.prefix:
            return {}, missed
        pending = {}
        for row in self.db.pending_mirror_deletes():
            pending.setdefault(row["member_id"], set()).add(row["plex_key"])
        found = {}
        for m in members:
            skip = self.db.mirror_keys(m.id) | pending.get(m.id, set())
            try:
                pls = self.backend.user_playlists(m)
            except Exception as e:
                log.warning("Couldn't look for leftover shared playlists in %s's Plex: %s", m.title, e)
                missed.append(m)
                continue
            for p in pls:
                if p["smart"] or p["key"] in skip or not p["title"].startswith(self.prefix):
                    continue
                name = p["title"][len(self.prefix):].strip()
                if not name or (names is not None and name not in names):
                    continue
                found.setdefault(name, []).append((m, p["key"], p["title"], p["count"]))
        return found, missed

    def leftovers(self):
        """Summary of leftover copies, for the owner to delete or bring back."""
        with self._run_lock:
            found, _ = self._scan_leftovers(self.backend.members())
            existing = {p["name"] for p in self.db.list_playlists()}
        out = []
        for name, copies in sorted(found.items(), key=lambda kv: kv[0].lower()):
            people = list(dict.fromkeys(m.title for m, *_ in copies))
            out.append({"name": name, "copies": len(copies), "people": people,
                        "songs": max(c for *_, c in copies),
                        # Same name as a playlist Jukebox has: these are stray duplicates.
                        "in_jukebox": name in existing})
        return out

    def delete_leftovers(self, name):
        """Delete every leftover copy called <prefix><name> from everyone's Plex.

        Shared playlists Jukebox knows about are never touched: only copies it isn't syncing.
        Returns (copies deleted, people whose Plex didn't answer: run it again later for those).
        """
        deleted = 0
        with self._run_lock:
            found, missed = self._scan_leftovers(self.backend.members(), names={name})
            queued = len(missed)
            for m, key, title, _ in found.get(name, []):
                try:
                    self.backend.mirror_delete(m, key)
                    deleted += 1
                except Exception as e:
                    log.warning("Couldn't delete %r from %s's Plex (will retry): %s", title, m.title, e)
                    self.db.queue_mirror_delete(m.id, key)  # sync retries it
        log.info("Deleted %d leftover copies of %r%s", deleted, name,
                 f" ({queued} people's Plex didn't answer)" if queued else "")
        return deleted, queued

    def recover(self, names=None):
        """Rebuild shared playlists from the leftover copies in people's Plex.

        For when the database was lost or this is a fresh install: every "<prefix>Name"
        playlist that isn't a known copy becomes (part of) shared playlist "Name", shared with
        everyone who has a copy, and their existing copies are kept and synced from then on
        (no duplicates). Names that already exist here are left alone. `names` limits it to
        those playlists. Returns the names of the playlists rebuilt.
        """
        with self._run_lock:
            members = self.backend.members()
            existing = {p["name"] for p in self.db.list_playlists()}
            found = {}  # name -> {member id: (plex key, title, track keys)}
            for name, copies in self._scan_leftovers(members, names)[0].items():
                if name in existing:
                    continue
                for m, key, title, _ in copies:
                    try:
                        keys = self.backend.user_playlist_keys(m, key) or []
                    except Exception as e:
                        log.warning("Couldn't read %s's %r: %s", m.title, title, e)
                        continue
                    have = found.setdefault(name, {}).get(m.id)
                    if keys and (not have or len(keys) > len(have[2])):  # two copies: keep the bigger
                        found[name][m.id] = (key, title, keys)

            rebuilt = []
            order = {m.id: i for i, m in enumerate(sorted(members, key=lambda m: not m.is_owner))}
            by_id = {m.id: m for m in members}
            for name, copies in sorted(found.items()):
                if not copies:
                    continue
                who = sorted(copies, key=lambda mid: order[mid])
                # Owner's copy first (else the biggest), then anything only others still have.
                base = who[0] if by_id[who[0]].is_owner else max(who, key=lambda mid: len(copies[mid][2]))
                keys = list(dict.fromkeys(
                    k for mid in [base] + [w for w in who if w != base] for k in copies[mid][2]))
                tracks = self.backend.get_tracks(keys)
                if not tracks:
                    continue
                creator = by_id[who[0]]
                pid = self.db.create_playlist(name, creator.title, creator.id)
                for mid in who[1:]:
                    self.db.add_member(pid, mid, creator.title)
                self.db.add_tracks(pid, tracks, creator.title)
                for mid in who:
                    key, title, theirs = copies[mid]
                    # Their copy as it is now counts as synced, so nothing in it reads as an edit.
                    self.db.save_mirror(pid, mid, key, theirs, theirs, title)
                rebuilt.append(name)
                log.info("Recovered shared playlist %r (%d songs) from the copies in %s's Plex",
                         name, len(tracks), ", ".join(by_id[mid].title for mid in who))
        if rebuilt:
            self.kick()
        return rebuilt

    # ---- per playlist -----------------------------------------------------

    def _sync_playlist(self, pid, members):
        current = {}
        for m in members:
            mir = self.db.get_mirror(pid, m.id)
            if not mir or not mir["plex_key"]:
                continue
            try:
                current[m.id] = self.backend.mirror_get(m, mir["plex_key"])
            except Exception as e:
                log.warning("Couldn't read %s's copy of playlist %s: %s", m.title, pid, e)
                self.db.set_mirror_error(pid, m.id, f"Couldn't read from Plex: {redact(e)}")
                current[m.id] = FAILED
        if self.pull_enabled:
            self._pull(pid, members, current)
        self._push(pid, members, current)

    def _pull(self, pid, members, current):
        for m in members:
            cur = current.get(m.id)
            if not isinstance(cur, list):
                continue
            mir = self.db.get_mirror(pid, m.id)
            if not mir:  # playlist deleted mid-cycle
                return
            last = mir["synced_keys"]
            last_set, cur_set = set(last), set(cur)
            master = self.db.playlist_keys(pid)
            master_set = set(master)

            added = [k for k in dict.fromkeys(cur) if k not in last_set and k not in master_set]
            removed = [k for k in last if k not in cur_set and k in master_set]
            if len(removed) > max(5, len(last) // 2):
                # Most likely a glitch on Plex's side; don't wipe everyone's playlist.
                # The push below puts the tracks back into this user's copy.
                log.warning("Ignoring %d removals from %s's copy of playlist %s (looks like a glitch)",
                            len(removed), m.title, pid)
                removed = []

            n_added = 0
            if added:
                info = {t["key"]: t for t in self.backend.get_tracks(added)}
                for k in added:
                    if k not in info:
                        continue
                    # Keep the spot they put it in: right after the track before it.
                    i = cur.index(k)
                    prev = next((p for p in reversed(cur[:i]) if p in master_set), None)
                    index = master.index(prev) + 1 if prev else 0
                    if self.db.add_tracks(pid, [info[k]], m.title, index):
                        master.insert(index, k)
                        master_set.add(k)
                        n_added += 1
            n_removed = self.db.remove_tracks(pid, removed) if removed else 0

            if n_added or n_removed:
                parts = []
                if n_added:
                    parts.append(f"added {n_added} track{'s' * (n_added != 1)}")
                if n_removed:
                    parts.append(f"removed {n_removed} track{'s' * (n_removed != 1)}")
                log.info("%s %s in Plex (playlist %s)", m.title, " and ".join(parts), pid)
                self.notify({"type": "playlist", "id": pid, "who": m.title,
                             "action": " and ".join(parts) + " in Plex"})

    def _push(self, pid, members, current):
        p = self.db.get_playlist(pid, with_items=False)
        if not p:
            return
        title = self.prefix + p["name"]
        target = self.db.playlist_keys(pid)
        changed = False
        for m in members:
            cur = current.get(m.id)
            if cur is FAILED:
                continue
            mir = self.db.get_mirror(pid, m.id) or {
                "plex_key": None, "synced_keys": [], "synced_target": [], "synced_title": None, "error": None}
            key = mir["plex_key"] if cur is not None else None  # None: never made, or deleted in Plex
            same_title = mir["synced_title"] == title
            if key and same_title and (cur == target or (cur == mir["synced_keys"] and mir["synced_target"] == target)):
                if mir["synced_target"] != target or mir["error"]:
                    self._save(pid, m, key, cur, target, title)
                continue
            if not key and not target:
                if mir["synced_title"] != title or mir["error"]:
                    self._save(pid, m, None, [], target, title)
                continue
            try:
                new_key, now = self.backend.mirror_sync(m, key, title, target, rename=not same_title)
            except Exception as e:
                log.warning("Couldn't update %s's copy of playlist %s: %s", m.title, pid, e)
                self.db.set_mirror_error(pid, m.id, f"Couldn't update Plex: {redact(e)}")
                changed = True
                continue
            self._save(pid, m, new_key, now, target, title)
            changed = True
        if changed:
            self.notify({"type": "sync", "id": pid})

    def _save(self, pid, m, key, now, target, title):
        try:
            self.db.save_mirror(pid, m.id, key, now, target, title)
        except sqlite3.IntegrityError:
            # The shared playlist was deleted while we were syncing it.
            if key:
                self.db.queue_mirror_delete(m.id, key)

    def _process_deletes(self, by_id):
        for row in self.db.pending_mirror_deletes():
            m = by_id.get(row["member_id"])
            try:
                if m:
                    self.backend.mirror_delete(m, row["plex_key"])
                    log.info("Removed a playlist copy from %s's Plex", m.title)
                self.db.done_mirror_delete(row["id"])
            except Exception as e:
                who = m.title if m else row["member_id"]
                log.warning("Couldn't remove a playlist copy from %s's Plex (will retry): %s", who, e)
