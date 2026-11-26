"""Talking to Plex.

`PlexBackend` is the real thing. `DemoBackend` fakes a Plex server in memory so the app
can be tried (and tested) without one. Both share `_reconcile`, which turns a user's
Plex playlist into an exact copy of the shared master using small edits (add, remove,
move) rather than deleting and recreating it.
"""
from __future__ import annotations

import bisect
import hashlib
import itertools
import re
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import quote

log = logging.getLogger("jukebox.backend")

CHUNK = 100  # tracks per add request, keeps URLs a sane length


@dataclass(frozen=True)
class Member:
    id: str          # plex.tv account id
    username: str    # username (or title for managed users)
    title: str       # display name
    is_owner: bool = False

    def public(self):
        return {"id": self.id, "title": self.title, "is_owner": self.is_owner}


def chunks(seq, n=CHUNK):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def plan_moves(current: list, target: list) -> list:
    """Moves turning `current` into `target` (same items, different order).

    Returns [(key, after_key_or_None)], to apply in order. Keeps the longest run of
    items already in the right relative order and moves only the rest, so moving one
    song costs one request.
    """
    pos = {k: i for i, k in enumerate(current)}
    seq = [pos[k] for k in target]
    tails, tails_idx, prev = [], [], [-1] * len(seq)
    for i, v in enumerate(seq):
        j = bisect.bisect_left(tails, v)
        if j == len(tails):
            tails.append(v)
            tails_idx.append(i)
        else:
            tails[j] = v
            tails_idx[j] = i
        prev[i] = tails_idx[j - 1] if j > 0 else -1
    keep = set()
    i = tails_idx[-1] if tails_idx else -1
    while i != -1:
        keep.add(i)
        i = prev[i]
    return [(target[i], target[i - 1] if i > 0 else None)
            for i in range(len(target)) if i not in keep]


_TOKEN_RE = re.compile(r"((?:X-Plex-Token|token)=)[^&\s'\"]+", re.I)


def redact(text) -> str:
    """Strip Plex tokens out of text that may end up in a log or on the web page."""
    return _TOKEN_RE.sub(r"\1REDACTED", str(text))


def is_key(key) -> bool:
    """Plex item keys are plain numbers; anything else never reaches a Plex URL."""
    return isinstance(key, str) and key.isdigit() and len(key) < 20


class BaseBackend:
    demo = False

    # Subclasses provide: members(), search(), album_tracks(), get_tracks(), thumb(),
    # _with_server(member, fn) and the _p_* playlist primitives.

    def user_playlists(self, member: Member):
        """The member's own music playlists in Plex: [{key, title, count, duration, smart}]."""
        return self._with_server(member, self._p_list)

    def user_playlist_keys(self, member: Member, key: str):
        """Track keys in one of the member's playlists (smart ones too), in order; None if gone."""
        return self._with_server(member, lambda srv: self._p_item_keys(srv, key))

    def mirror_get(self, member: Member, key: str):
        """Track keys in the user's Plex copy, in order; None if it no longer exists."""
        items = self._with_server(member, lambda srv: self._p_items(srv, key))
        return None if items is None else [rk for rk, _ in items]

    def mirror_delete(self, member: Member, key: str):
        self._with_server(member, lambda srv: self._p_delete(srv, key))

    def mirror_sync(self, member: Member, key, title: str, target: list, rename: bool):
        """Make the user's Plex copy match `target`. Returns (plex_key, keys_now_in_plex)."""
        return self._with_server(member, lambda srv: self._reconcile(srv, key, title, target, rename))

    def _reconcile(self, srv, key, title, target, rename):
        if not target:  # Plex can't create an empty playlist, so an empty one has no copy
            if key:
                self._p_delete(srv, key)
            return None, []
        items = self._p_items(srv, key) if key else None
        if items is None:
            key = self._p_create(srv, title, target[:CHUNK])
            for c in chunks(target[CHUNK:]):
                self._p_add(srv, key, c)
            items = self._p_items(srv, key) or []
        elif rename:
            self._p_rename(srv, key, title)

        wanted = set(target)
        seen, extra = set(), []
        for rk, iid in items:
            if rk in wanted and rk not in seen:
                seen.add(rk)
            else:
                extra.append(iid)  # not in the master, or a duplicate
        for iid in extra:
            self._p_remove(srv, key, iid)
        missing = [k for k in target if k not in seen]
        for c in chunks(missing):
            self._p_add(srv, key, c)
        if extra or missing:
            items = self._p_items(srv, key) or []

        ids, order = {}, []
        for rk, iid in items:
            if rk in wanted and rk not in ids:
                ids[rk] = iid
                order.append(rk)
        # Tracks this user can't see (e.g. a library not shared with them) never make it in.
        goal = [k for k in target if k in ids]
        if order != goal:
            for k, after in plan_moves(order, goal):
                self._p_move(srv, key, ids[k], ids[after] if after else None)
        return key, goal


# --------------------------------------------------------------------------- real Plex

class PlexBackend(BaseBackend):
    def __init__(self, url: str, token: str, allowed_users=None):
        from plexapi.server import PlexServer

        self._PlexServer = PlexServer
        self.url = url.rstrip("/")
        self.server = PlexServer(self.url, token, timeout=20)
        self.machine_id = self.server.machineIdentifier
        self.account = self.server.myPlexAccount()
        self.allowed = {a.strip().lower() for a in (allowed_users or []) if a.strip()}
        self._lock = threading.Lock()
        self._members, self._members_at = None, 0.0
        self._servers = {}
        self._thumb_cache = OrderedDict()
        log.info("Connected to Plex server %r as %s", self.server.friendlyName, self.account.username)

    # ---- users -----------------------------------------------------------

    def members(self, refresh=False):
        with self._lock:
            if self._members is not None and not refresh and time.time() - self._members_at < 300:
                return self._members
        acct = self.account
        out = [Member(str(acct.id), acct.username or acct.title, acct.title or acct.username, True)]
        try:
            users = acct.users()
        except Exception:
            log.exception("Couldn't list Plex users")
            if self._members is not None:
                return self._members
            users = []
        for u in users:
            if not any(getattr(s, "machineIdentifier", None) == self.machine_id for s in (u.servers or [])):
                continue  # has no access to this server
            names = {(u.username or "").lower(), (u.title or "").lower(), (u.email or "").lower()}
            if self.allowed and not (names & self.allowed):
                continue
            name = u.username or u.title
            out.append(Member(str(u.id), name, u.title or name))
        with self._lock:
            self._members, self._members_at = out, time.time()
        return out

    def _server_for(self, member: Member):
        if member.is_owner:
            return self.server
        srv = self._servers.get(member.id)
        if srv is None:
            token = self.account.user(member.username).get_token(self.machine_id)
            if not token:
                raise RuntimeError(f"Plex gave no server token for {member.title}")
            srv = self._PlexServer(self.url, token, timeout=20)
            self._servers[member.id] = srv
        return srv

    def _with_server(self, member, fn):
        from plexapi.exceptions import Unauthorized

        try:
            return fn(self._server_for(member))
        except Unauthorized:
            self._servers.pop(member.id, None)  # token went stale; fetch a fresh one once
            return fn(self._server_for(member))

    # ---- library ---------------------------------------------------------

    @staticmethod
    def _track(t):
        return {
            "key": str(t.ratingKey),
            "title": t.title,
            "artist": getattr(t, "originalTitle", None) or t.grandparentTitle,
            "album": t.parentTitle,
            "duration": t.duration or 0,
        }

    def _as(self, member, fn):
        """Run fn with the member's own Plex access (so they only see libraries shared with
        them), or with the owner's when member is None (the sync engine)."""
        return fn(self.server) if member is None else self._with_server(member, fn)

    def search(self, q: str, member=None):
        tracks, albums = self._as(member, lambda srv: (
            srv.search(q, mediatype="track", limit=30), srv.search(q, mediatype="album", limit=8)))
        return {
            "tracks": [self._track(t) for t in tracks],
            "albums": [{"key": str(a.ratingKey), "title": a.title, "artist": a.parentTitle,
                        "year": a.year, "count": a.leafCount} for a in albums],
        }

    def album_tracks(self, key: str, member=None):
        if not is_key(key):
            return []
        def fetch(srv):
            album = srv.fetchItem(int(key))
            return album.tracks() if getattr(album, "TYPE", "") == "album" else []
        return [self._track(t) for t in self._as(member, fetch)]

    def get_tracks(self, keys: list, member=None):
        keys = [k for k in keys if is_key(k)]
        def fetch(srv):
            found = {}
            for c in chunks(keys):
                for t in srv.fetchItems("/library/metadata/" + ",".join(c)):
                    if getattr(t, "TYPE", "") == "track":
                        found[str(t.ratingKey)] = self._track(t)
            return found
        found = self._as(member, fetch) if keys else {}
        return [found[k] for k in keys if k in found]

    def thumb(self, key: str, member=None):
        """Cover art for a track or album, only if this member can see it in Plex."""
        if not is_key(key):
            return None
        ck = (member.id if member else None, key)
        if ck in self._thumb_cache:
            self._thumb_cache.move_to_end(ck)
            return self._thumb_cache[ck]

        def fetch(srv):
            it = srv.fetchItem(int(key))  # fails if it's not in a library they have
            kind = getattr(it, "TYPE", "")
            if kind == "track":
                path = getattr(it, "parentThumb", None) or it.thumb
            elif kind == "album":
                path = it.thumb
            else:
                return None  # only music art, never photos or videos
            if not path:
                return None
            # Token goes in a header, not the URL, so it can't turn up in an error message.
            r = srv._session.get(
                srv.url(f"/photo/:/transcode?width=160&height=160&minSize=1&upscale=1&url={quote(path)}"),
                headers=srv._headers(), timeout=15)
            r.raise_for_status()
            ctype = r.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
            return (r.content, ctype) if ctype in ("image/jpeg", "image/png", "image/webp") else None

        res = self._as(member, fetch)
        self._thumb_cache[ck] = res
        if len(self._thumb_cache) > 1000:
            self._thumb_cache.popitem(last=False)
        return res

    # ---- playlist primitives (raw Plex HTTP API) -------------------------

    def _uri(self, keys):
        return f"server://{self.machine_id}/com.plexapp.plugins.library/library/metadata/{','.join(keys)}"

    def _p_items(self, srv, key):
        from plexapi.exceptions import NotFound

        try:
            el = srv.query(f"/playlists/{key}/items")
        except NotFound:
            return None
        if el is None:
            return []
        return [(e.attrib["ratingKey"], e.attrib["playlistItemID"])
                for e in el if "playlistItemID" in e.attrib]

    def _p_list(self, srv):
        el = srv.query("/playlists?playlistType=audio")
        if el is None:
            return []
        return [{"key": e.attrib["ratingKey"], "title": e.attrib.get("title", ""),
                 "count": int(e.attrib.get("leafCount") or 0), "duration": int(e.attrib.get("duration") or 0),
                 "smart": e.attrib.get("smart") == "1"}
                for e in el if e.tag == "Playlist"]

    def _p_item_keys(self, srv, key):
        # Unlike _p_items this doesn't need playlistItemID, which smart playlists don't have.
        from plexapi.exceptions import NotFound

        try:
            el = srv.query(f"/playlists/{key}/items")
        except NotFound:
            return None
        return [] if el is None else [e.attrib["ratingKey"] for e in el if "ratingKey" in e.attrib]

    def _p_create(self, srv, title, keys):
        el = srv.query(f"/playlists?type=audio&smart=0&title={quote(title)}&uri={quote(self._uri(keys))}",
                       method=srv._session.post)
        pl = el.find("Playlist") if el is not None else None
        if pl is None:
            raise RuntimeError("Plex didn't create the playlist (none of the tracks are visible to this user?)")
        return pl.attrib["ratingKey"]

    def _p_add(self, srv, key, keys):
        srv.query(f"/playlists/{key}/items?uri={quote(self._uri(keys))}", method=srv._session.put)

    def _p_remove(self, srv, key, item_id):
        srv.query(f"/playlists/{key}/items/{item_id}", method=srv._session.delete)

    def _p_move(self, srv, key, item_id, after_id):
        q = f"/playlists/{key}/items/{item_id}/move"
        if after_id:
            q += f"?after={after_id}"
        srv.query(q, method=srv._session.put)

    def _p_rename(self, srv, key, title):
        srv.query(f"/playlists/{key}?title={quote(title)}", method=srv._session.put)

    def _p_delete(self, srv, key):
        from plexapi.exceptions import NotFound

        try:
            srv.query(f"/playlists/{key}", method=srv._session.delete)
        except NotFound:
            pass


# --------------------------------------------------------------------------- demo

ARTISTS = {
    "The Lanterns": ["Night Market", "Paper Boats"],
    "Mira Sol": ["Low Tide", "Glasshouse"],
    "Orbit Kids": ["Satellite Summer", "Static"],
    "Velvet Hours": ["After Midnight", "Slow Burn"],
    "June Atlas": ["Northbound", "Wildflower Roads"],
    "Copper Fox": ["Ember", "Analog Heart"],
}
WORDS = ["Golden", "Echo", "Neon", "River", "Hollow", "Runaway", "Signal", "Daylight",
         "Ghost", "Honey", "Midnight", "Paper", "Silver", "Wild", "Velvet", "Static"]
NOUNS = ["Lights", "Heart", "Road", "Sky", "Waves", "Dreams", "Fire", "Lines", "Town", "Rain"]


class DemoBackend(BaseBackend):
    """In-memory stand-in for a Plex server with four users."""

    demo = True

    def __init__(self, seed_playlists=False):
        self._members = [
            Member("1", "owner", "You (owner)", True),
            Member("2", "sam", "Sam"),
            Member("3", "riley", "Riley"),
            Member("4", "jordan", "Jordan"),
        ]
        self.library, self.albums = {}, {}
        n = itertools.count(1000)
        rng = itertools.count()
        for artist, albums in ARTISTS.items():
            for album in albums:
                akey = str(next(n))
                tracks = []
                for _ in range(7):
                    i = next(rng)
                    t = {"key": str(next(n)), "title": f"{WORDS[(i * 7) % 16]} {NOUNS[(i * 3) % 10]}",
                         "artist": artist, "album": album, "duration": 150000 + (i * 7919) % 150000}
                    self.library[t["key"]] = t
                    tracks.append(t["key"])
                self.albums[akey] = {"key": akey, "title": album, "artist": artist, "year": 2019 + len(album) % 6,
                                     "count": len(tracks), "tracks": tracks}
        self.plex = {m.id: {} for m in self._members}  # member -> playlist key -> {title, items}
        self.hidden = {}                                # member -> track keys they can't see
        self.fail = set()                               # member ids whose requests fail (tests)
        self._ids = itertools.count(1)
        self.calls = 0
        if seed_playlists:  # some personal playlists to try importing
            keys = list(self.library)
            self.add_personal("1", "Gym Rotation", keys[0:84:7])
            self.add_personal("1", "Chill Evenings", keys[14:22])
            self.add_personal("1", "Recently Played", keys[40:50], smart=True)
            self.add_personal("2", "Sam's Faves", keys[3:60:9])

    def members(self, refresh=False):
        return self._members

    def search(self, q: str, member=None):
        q = q.lower()
        tracks = [t for t in self.library.values()
                  if q in t["title"].lower() or q in t["artist"].lower() or q in t["album"].lower()]
        albums = [{k: v for k, v in a.items() if k != "tracks"} for a in self.albums.values()
                  if q in a["title"].lower() or q in a["artist"].lower()]
        return {"tracks": tracks[:30], "albums": albums[:8]}

    def album_tracks(self, key, member=None):
        return [self.library[k] for k in self.albums.get(key, {}).get("tracks", [])]

    def get_tracks(self, keys, member=None):
        return [self.library[k] for k in keys if k in self.library]

    def thumb(self, key, member=None):
        t = self.library.get(key)
        album = t["album"] if t else self.albums.get(key, {}).get("title", "?")
        hue = int(hashlib.md5(album.encode()).hexdigest()[:4], 16) % 360
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 80">'
               f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
               f'<stop offset="0" stop-color="hsl({hue},65%,55%)"/>'
               f'<stop offset="1" stop-color="hsl({(hue + 50) % 360},70%,30%)"/></linearGradient></defs>'
               f'<rect width="80" height="80" fill="url(#g)"/>'
               f'<circle cx="40" cy="40" r="14" fill="none" stroke="rgba(255,255,255,.55)" stroke-width="3"/></svg>')
        return svg.encode(), "image/svg+xml"

    # ---- fake Plex playlist API -------------------------------------------

    def _with_server(self, member, fn):
        self.calls += 1
        if member.id in self.fail:
            raise ConnectionError(f"demo: {member.title}'s server is unreachable")
        return fn(member.id)

    def _visible(self, srv, keys):
        return [k for k in keys if k in self.library and k not in self.hidden.get(srv, set())]

    def _p_items(self, srv, key):
        pl = self.plex[srv].get(key)
        return None if pl is None else list(pl["items"])

    def _p_list(self, srv):
        return [{"key": k, "title": p["title"], "count": len(p["items"]),
                 "duration": sum(self.library[rk]["duration"] for rk, _ in p["items"] if rk in self.library),
                 "smart": p.get("smart", False)} for k, p in self.plex[srv].items()]

    def _p_item_keys(self, srv, key):
        pl = self.plex[srv].get(key)
        return None if pl is None else [rk for rk, _ in pl["items"]]

    def _p_create(self, srv, title, keys):
        keys = self._visible(srv, keys)
        if not keys:
            raise RuntimeError("Plex didn't create the playlist")
        key = f"pl{next(self._ids)}"
        self.plex[srv][key] = {"title": title, "items": [(k, str(next(self._ids))) for k in keys]}
        return key

    def _p_add(self, srv, key, keys):
        self.plex[srv][key]["items"] += [(k, str(next(self._ids))) for k in self._visible(srv, keys)]

    def _p_remove(self, srv, key, item_id):
        items = self.plex[srv][key]["items"]
        items[:] = [it for it in items if it[1] != item_id]

    def _p_move(self, srv, key, item_id, after_id):
        items = self.plex[srv][key]["items"]
        it = next(x for x in items if x[1] == item_id)
        items.remove(it)
        at = 0 if after_id is None else next(i for i, x in enumerate(items) if x[1] == after_id) + 1
        items.insert(at, it)

    def _p_rename(self, srv, key, title):
        self.plex[srv][key]["title"] = title

    def _p_delete(self, srv, key):
        self.plex[srv].pop(key, None)

    # ---- helpers to imitate someone editing inside Plex/Plexamp ------------

    def add_personal(self, member_id, title, keys, smart=False):
        """A playlist the member made themselves in Plex."""
        key = f"pl{next(self._ids)}"
        self.plex[member_id][key] = {"title": title, "smart": smart,
                                     "items": [(k, str(next(self._ids))) for k in keys]}
        return key

    def plex_edit(self, member_id, title, add=(), remove=()):
        pl = next(p for p in self.plex[member_id].values() if p["title"] == title)
        pl["items"] = [it for it in pl["items"] if it[0] not in set(remove)]
        pl["items"] += [(k, str(next(self._ids))) for k in add]
