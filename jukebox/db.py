"""SQLite storage: the master copy of every shared playlist, plus per-user mirror state."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS playlists(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL,
  created_by  TEXT NOT NULL,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS items(
  playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
  track_key   TEXT NOT NULL,
  pos         INTEGER NOT NULL,
  title       TEXT,
  artist      TEXT,
  album       TEXT,
  duration    INTEGER,
  added_by    TEXT NOT NULL,
  added_at    REAL NOT NULL,
  PRIMARY KEY(playlist_id, track_key)
);
-- One row per (shared playlist, Plex user): the user's copy of the playlist in Plex.
CREATE TABLE IF NOT EXISTS mirrors(
  playlist_id   INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
  member_id     TEXT NOT NULL,
  plex_key      TEXT,
  synced_keys   TEXT NOT NULL DEFAULT '[]',  -- what the Plex copy held after our last push
  synced_target TEXT NOT NULL DEFAULT '[]',  -- what the master held at our last push
  synced_title  TEXT,
  synced_at     REAL,
  error         TEXT,
  PRIMARY KEY(playlist_id, member_id)
);
-- Who each playlist is shared with.
CREATE TABLE IF NOT EXISTS playlist_members(
  playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
  member_id   TEXT NOT NULL,
  added_by    TEXT,
  added_at    REAL,
  PRIMARY KEY(playlist_id, member_id)
);
-- Songs a playlist should have that aren't in Plex yet (from an import, or requested to
-- download). Checked against the library now and then; added to the playlist on arrival.
CREATE TABLE IF NOT EXISTS wanted(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
  title       TEXT NOT NULL,
  artist      TEXT NOT NULL DEFAULT '',
  album       TEXT NOT NULL DEFAULT '',
  duration    INTEGER NOT NULL DEFAULT 0,
  link        TEXT,              -- e.g. the Deezer track page, for deemix
  after_key   TEXT,              -- goes after this track when it arrives
  status      TEXT NOT NULL DEFAULT 'missing',  -- missing | requested | failed
  note        TEXT,
  added_by    TEXT NOT NULL,
  added_at    REAL NOT NULL,
  checked_at  REAL
);
-- Plex copies that must be deleted (their shared playlist was deleted).
CREATE TABLE IF NOT EXISTS mirror_deletes(
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  member_id TEXT NOT NULL,
  plex_key  TEXT NOT NULL
);
"""


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def _all(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def _one(self, sql, args=()):
        with self.lock:
            r = self.conn.execute(sql, args).fetchone()
            return dict(r) if r else None

    # ---- playlists -------------------------------------------------------

    def list_playlists(self, member_id=None):
        """All playlists, or only the ones shared with `member_id`."""
        where, args = "", ()
        if member_id is not None:
            where = "WHERE p.id IN (SELECT playlist_id FROM playlist_members WHERE member_id=?)"
            args = (member_id,)
        return self._all(
            f"""SELECT p.id, p.name, p.created_by, p.updated_at,
                      COUNT(i.track_key) AS count, COALESCE(SUM(i.duration), 0) AS duration,
                      (SELECT track_key FROM items WHERE playlist_id = p.id ORDER BY pos LIMIT 1) AS first_key,
                      (SELECT COUNT(*) FROM playlist_members WHERE playlist_id = p.id) AS people,
                      (SELECT COUNT(*) FROM wanted WHERE playlist_id = p.id) AS waiting
               FROM playlists p LEFT JOIN items i ON i.playlist_id = p.id
               {where}
               GROUP BY p.id ORDER BY p.name COLLATE NOCASE""",
            args,
        )

    # ---- members -----------------------------------------------------------

    def member_ids(self, pid: int) -> set:
        return {r["member_id"] for r in self._all(
            "SELECT member_id FROM playlist_members WHERE playlist_id=?", (pid,))}

    def is_member(self, pid: int, member_id: str) -> bool:
        return self._one("SELECT 1 AS x FROM playlist_members WHERE playlist_id=? AND member_id=?",
                         (pid, member_id)) is not None

    def add_member(self, pid: int, member_id: str, who: str) -> bool:
        with self.tx() as c:
            if not c.execute("SELECT 1 FROM playlists WHERE id=?", (pid,)).fetchone():
                return False
            return c.execute(
                "INSERT OR IGNORE INTO playlist_members(playlist_id, member_id, added_by, added_at) VALUES (?,?,?,?)",
                (pid, member_id, who, time.time())).rowcount > 0

    def remove_member(self, pid: int, member_id: str) -> bool:
        with self.tx() as c:
            return c.execute("DELETE FROM playlist_members WHERE playlist_id=? AND member_id=?",
                             (pid, member_id)).rowcount > 0

    def give_unshared_playlists_to(self, member_id: str) -> int:
        """Upgrade from the version without sharing: playlists with nobody in them go to the owner."""
        with self.tx() as c:
            return c.execute(
                """INSERT INTO playlist_members(playlist_id, member_id, added_by, added_at)
                   SELECT id, ?, 'upgrade', ? FROM playlists
                   WHERE id NOT IN (SELECT playlist_id FROM playlist_members)""",
                (member_id, time.time())).rowcount

    def playlist_ids(self):
        return [r["id"] for r in self._all("SELECT id FROM playlists ORDER BY id")]

    def get_playlist(self, pid: int, with_items=True):
        p = self._one("SELECT * FROM playlists WHERE id=?", (pid,))
        if p and with_items:
            p["items"] = self._all(
                """SELECT track_key AS key, title, artist, album, duration, added_by, added_at
                   FROM items WHERE playlist_id=? ORDER BY pos""",
                (pid,),
            )
        return p

    def playlist_keys(self, pid: int):
        return [r["track_key"] for r in self._all(
            "SELECT track_key FROM items WHERE playlist_id=? ORDER BY pos", (pid,))]

    def create_playlist(self, name: str, who: str, member_id: str) -> int:
        """New playlist, shared with just its creator to start with."""
        now = time.time()
        with self.tx() as c:
            pid = c.execute(
                "INSERT INTO playlists(name, created_by, created_at, updated_at) VALUES (?,?,?,?)",
                (name, who, now, now),
            ).lastrowid
            c.execute("INSERT INTO playlist_members(playlist_id, member_id, added_by, added_at) VALUES (?,?,?,?)",
                      (pid, member_id, who, now))
            return pid

    def rename_playlist(self, pid: int, name: str) -> bool:
        with self.tx() as c:
            return c.execute("UPDATE playlists SET name=?, updated_at=? WHERE id=?",
                             (name, time.time(), pid)).rowcount > 0

    def delete_playlist(self, pid: int) -> bool:
        with self.tx() as c:
            c.execute(
                """INSERT INTO mirror_deletes(member_id, plex_key)
                   SELECT member_id, plex_key FROM mirrors WHERE playlist_id=? AND plex_key IS NOT NULL""",
                (pid,),
            )
            return c.execute("DELETE FROM playlists WHERE id=?", (pid,)).rowcount > 0

    # ---- items -----------------------------------------------------------

    def add_tracks(self, pid: int, tracks: list, who: str, index: int | None = None) -> list:
        """Insert tracks (skipping ones already present) at `index`, or at the end."""
        now = time.time()
        with self.tx() as c:
            if not c.execute("SELECT 1 FROM playlists WHERE id=?", (pid,)).fetchone():
                return []
            keys = [r[0] for r in c.execute(
                "SELECT track_key FROM items WHERE playlist_id=? ORDER BY pos", (pid,))]
            present = set(keys)
            new = []
            for t in tracks:
                if t["key"] not in present:
                    present.add(t["key"])
                    new.append(t)
            if not new:
                return []
            at = len(keys) if index is None else max(0, min(index, len(keys)))
            for t in new:
                c.execute(
                    """INSERT INTO items(playlist_id, track_key, pos, title, artist, album, duration, added_by, added_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (pid, t["key"], 0, t.get("title"), t.get("artist"), t.get("album"),
                     t.get("duration") or 0, who, now),
                )
            order = keys[:at] + [t["key"] for t in new] + keys[at:]
            self._renumber(c, pid, order)
            return new

    # ---- wanted (not in Plex yet) --------------------------------------------

    def add_wanted(self, pid: int, items: list, who: str) -> int:
        now = time.time()
        with self.tx() as c:
            for w in items:
                c.execute(
                    """INSERT INTO wanted(playlist_id, title, artist, album, duration, link, after_key,
                                          added_by, added_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (pid, w["title"][:300], (w.get("artist") or "")[:300], (w.get("album") or "")[:300],
                     int(w.get("duration") or 0), w.get("link"), w.get("after_key"), who, now))
        return len(items)

    def wanted_for(self, pid: int):
        return self._all("SELECT * FROM wanted WHERE playlist_id=? ORDER BY id", (pid,))

    def all_wanted(self):
        return self._all("SELECT * FROM wanted ORDER BY checked_at IS NOT NULL, checked_at, id")

    def get_wanted(self, wid: int):
        return self._one("SELECT * FROM wanted WHERE id=?", (wid,))

    def set_wanted(self, wid: int, **fields):
        allowed = {"status", "note", "link", "album", "checked_at"}
        cols = [k for k in fields if k in allowed]
        if cols:
            with self.tx() as c:
                c.execute(f"UPDATE wanted SET {', '.join(k + '=?' for k in cols)} WHERE id=?",
                          [fields[k] for k in cols] + [wid])

    def delete_wanted(self, wid: int) -> bool:
        with self.tx() as c:
            return c.execute("DELETE FROM wanted WHERE id=?", (wid,)).rowcount > 0

    def remove_tracks(self, pid: int, keys: list) -> int:
        with self.tx() as c:
            n = 0
            for k in keys:
                n += c.execute("DELETE FROM items WHERE playlist_id=? AND track_key=?", (pid, k)).rowcount
            if n:
                c.execute("UPDATE playlists SET updated_at=? WHERE id=?", (time.time(), pid))
            return n

    def move_track(self, pid: int, key: str, index: int) -> bool:
        with self.tx() as c:
            keys = [r[0] for r in c.execute(
                "SELECT track_key FROM items WHERE playlist_id=? ORDER BY pos", (pid,))]
            if key not in keys:
                return False
            keys.remove(key)
            keys.insert(max(0, min(index, len(keys))), key)
            self._renumber(c, pid, keys)
            return True

    def _renumber(self, c, pid, order):
        c.executemany("UPDATE items SET pos=? WHERE playlist_id=? AND track_key=?",
                      [(i, pid, k) for i, k in enumerate(order)])
        c.execute("UPDATE playlists SET updated_at=? WHERE id=?", (time.time(), pid))

    # ---- mirrors ---------------------------------------------------------

    def get_mirror(self, pid: int, member_id: str):
        m = self._one("SELECT * FROM mirrors WHERE playlist_id=? AND member_id=?", (pid, member_id))
        if m:
            m["synced_keys"] = json.loads(m["synced_keys"])
            m["synced_target"] = json.loads(m["synced_target"])
        return m

    def mirrors_for(self, pid: int):
        rows = self._all("SELECT * FROM mirrors WHERE playlist_id=?", (pid,))
        for m in rows:
            m["synced_keys"] = json.loads(m["synced_keys"])
            m["synced_target"] = json.loads(m["synced_target"])
        return rows

    def mirror_keys(self, member_id) -> set:
        """Plex playlist keys that are this member's synced copies."""
        return {r["plex_key"] for r in self._all(
            "SELECT plex_key FROM mirrors WHERE member_id=? AND plex_key IS NOT NULL", (member_id,))}

    def save_mirror(self, pid, member_id, plex_key, synced_keys, synced_target, title):
        """Raises sqlite3.IntegrityError if the playlist was deleted meanwhile."""
        with self.tx() as c:
            c.execute(
                """INSERT INTO mirrors(playlist_id, member_id, plex_key, synced_keys, synced_target,
                                       synced_title, synced_at, error)
                   VALUES (?,?,?,?,?,?,?,NULL)
                   ON CONFLICT(playlist_id, member_id) DO UPDATE SET
                     plex_key=excluded.plex_key, synced_keys=excluded.synced_keys,
                     synced_target=excluded.synced_target, synced_title=excluded.synced_title,
                     synced_at=excluded.synced_at, error=NULL""",
                (pid, member_id, plex_key, json.dumps(synced_keys), json.dumps(synced_target),
                 title, time.time()),
            )

    def set_mirror_error(self, pid, member_id, error: str):
        with self.tx() as c:
            c.execute(
                """INSERT INTO mirrors(playlist_id, member_id, error) VALUES (?,?,?)
                   ON CONFLICT(playlist_id, member_id) DO UPDATE SET error=excluded.error""",
                (pid, member_id, error[:500]),
            )

    def retire_mirror(self, pid, member_id):
        """Stop syncing to someone: forget their copy and queue it for deletion from their Plex."""
        with self.tx() as c:
            c.execute(
                """INSERT INTO mirror_deletes(member_id, plex_key)
                   SELECT member_id, plex_key FROM mirrors
                   WHERE playlist_id=? AND member_id=? AND plex_key IS NOT NULL""",
                (pid, member_id))
            c.execute("DELETE FROM mirrors WHERE playlist_id=? AND member_id=?", (pid, member_id))

    def queue_mirror_delete(self, member_id, plex_key):
        with self.tx() as c:
            c.execute("INSERT INTO mirror_deletes(member_id, plex_key) VALUES (?,?)", (member_id, plex_key))

    def pending_mirror_deletes(self):
        return self._all("SELECT id, member_id, plex_key FROM mirror_deletes ORDER BY id")

    def done_mirror_delete(self, row_id):
        with self.tx() as c:
            c.execute("DELETE FROM mirror_deletes WHERE id=?", (row_id,))
