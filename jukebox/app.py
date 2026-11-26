"""Web app: JSON API, Plex sign-in and a WebSocket that pushes changes to every open browser."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from . import plexauth
from .backend import redact

log = logging.getLogger("jukebox.app")
STATIC = Path(__file__).resolve().parent.parent / "static"


class Hub:
    """Fan-out of live updates to connected browsers. `notify` is safe to call from any thread."""

    def __init__(self):
        self.clients = {}  # socket -> member id
        self.loop = None
        self.can_see = lambda member_id, pid: True

    async def broadcast(self, msg):
        data = json.dumps(msg)
        private = msg.get("type") in ("playlist", "sync")  # only for people in that playlist
        for ws, member_id in list(self.clients.items()):
            if private and not self.can_see(member_id, msg["id"]):
                continue
            try:
                await ws.send_text(data)
            except Exception:
                self.clients.pop(ws, None)

    def notify(self, msg):
        if self.loop:
            asyncio.run_coroutine_threadsafe(self.broadcast(msg), self.loop)


class NameIn(BaseModel):
    name: str


class KeysIn(BaseModel):
    keys: List[str]


class AlbumIn(BaseModel):
    album: str


class MoveIn(BaseModel):
    key: str
    index: int


class DemoLoginIn(BaseModel):
    member: str


class MemberIn(BaseModel):
    member: str


class ImportIn(BaseModel):
    key: str
    keep_original: bool = False


def create_app(cfg, backend, db, sync, hub: Hub):
    @asynccontextmanager
    async def lifespan(app):
        hub.loop = asyncio.get_running_loop()
        sync.start()
        yield
        sync.stop()

    app = FastAPI(title="Jukebox", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    public = (cfg.get("public_url") or "").strip()
    app.add_middleware(SessionMiddleware, secret_key=cfg["secret"], session_cookie="jukebox",
                       max_age=60 * 86400, same_site="lax", https_only=public.startswith("https://"))

    def same_origin(headers) -> bool:
        """True unless the request plainly comes from a page on another site."""
        origin = headers.get("origin")
        if not origin:
            return True  # not sent by a browser page (or a same-origin GET)
        host = urlparse(origin).netloc.lower()
        allowed = {(headers.get("host") or "").lower()}
        if public:
            allowed.add(urlparse(public).netloc.lower())
        return host in allowed

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # Another website can't make changes on behalf of someone who's signed in here.
        if request.method not in ("GET", "HEAD", "OPTIONS") and not same_origin(request.headers):
            return Response("Cross-site request refused", status_code=403)
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'self' https://app.plex.tv")
        return resp
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def member_map():
        return {m.id: m for m in backend.members()}

    def user(request: Request):
        u = request.session.get("user")
        if not u or u["id"] not in member_map():  # also drops people you stop sharing with
            raise HTTPException(401, "Sign in first")
        return u

    hub.can_see = lambda member_id, pid: db.is_member(pid, member_id)

    def changed(pid, who, action):
        sync.kick()
        hub.notify({"type": "playlist", "id": pid, "who": who, "action": action})

    def get_or_404(pid, u):
        """The playlist, if it's shared with this person. Others get the same 404 as a missing one."""
        p = db.get_playlist(pid) if db.is_member(pid, u["id"]) else None
        if not p:
            raise HTTPException(404, "No such playlist")
        return p

    # ---- pages & sign-in ------------------------------------------------

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/config")
    def config():
        out = {"demo": backend.demo}
        if backend.demo:
            out["members"] = [m.public() for m in backend.members()]
        return out

    @app.get("/auth/login")
    def login(request: Request):
        if backend.demo:
            return RedirectResponse("/")
        pin = plexauth.create_pin(cfg["client_id"])
        request.session["pin"] = pin
        base = (cfg.get("public_url") or str(request.base_url)).rstrip("/")
        return RedirectResponse(plexauth.auth_url(cfg["client_id"], pin["code"], base + "/auth/callback"))

    @app.get("/auth/callback")
    def callback(request: Request):
        pin = request.session.pop("pin", None)
        if not pin:
            return RedirectResponse("/?error=expired")
        token = plexauth.check_pin(cfg["client_id"], pin["id"], pin["code"])
        if not token:
            return RedirectResponse("/?error=notapproved")
        info = plexauth.user_info(cfg["client_id"], token)
        m = {m.id: m for m in backend.members(refresh=True)}.get(str(info["id"]))
        if not m:
            log.info("Sign-in refused for %s: no access to this server", info.get("username"))
            return RedirectResponse("/?error=noaccess")
        request.session["user"] = {"id": m.id, "title": m.title}
        log.info("%s signed in", m.title)
        return RedirectResponse("/")

    @app.post("/auth/demo")
    def demo_login(body: DemoLoginIn, request: Request):
        if not backend.demo:
            raise HTTPException(404)
        m = member_map().get(body.member)
        if not m:
            raise HTTPException(400, "Unknown member")
        request.session["user"] = {"id": m.id, "title": m.title}
        return {"ok": True}

    @app.get("/auth/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/")

    # ---- API ------------------------------------------------------------

    @app.get("/api/me")
    def me(u=Depends(user)):
        return {**u, "is_owner": member_map()[u["id"]].is_owner}

    @app.get("/api/members")
    def members(u=Depends(user)):
        return [m.public() for m in backend.members()]

    @app.get("/api/playlists")
    def playlists(u=Depends(user)):
        return db.list_playlists(u["id"])

    @app.post("/api/playlists")
    def create(body: NameIn, u=Depends(user)):
        name = body.name.strip()[:120]
        if not name:
            raise HTTPException(400, "Give it a name")
        pid = db.create_playlist(name, u["title"], u["id"])
        changed(pid, u["title"], f"created “{name}”")
        return {"id": pid}

    @app.get("/api/plex-playlists")
    def plex_playlists(u=Depends(user)):
        """Your own Plex music playlists that could be turned into shared ones."""
        m = member_map()[u["id"]]
        try:
            pls = backend.user_playlists(m)
        except Exception as e:
            log.warning("Couldn't list %s's Plex playlists: %s", m.title, e)
            raise HTTPException(502, "Couldn't read your playlists from Plex. Try again in a moment.")
        synced = db.mirror_keys(m.id)  # already shared copies
        pls = [p for p in pls if p["key"] not in synced and p["count"] > 0]
        return sorted(pls, key=lambda p: p["title"].lower())

    @app.post("/api/playlists/import")
    def import_playlist(body: ImportIn, u=Depends(user)):
        m = member_map()[u["id"]]
        if body.key in db.mirror_keys(m.id):
            raise HTTPException(400, "That one is already shared")
        try:
            pid, count, adopted = sync.import_playlist(m, body.key, body.keep_original)
        except (LookupError, ValueError) as e:
            raise HTTPException(400, str(e))
        changed(pid, u["title"], "shared one of their Plex playlists")
        return {"id": pid, "count": count, "adopted": adopted}

    @app.post("/api/recover")
    def recover(u=Depends(user)):
        """Owner only: rebuild shared playlists from 👥 copies left from an earlier install."""
        if not member_map()[u["id"]].is_owner:
            raise HTTPException(403, "Only the server owner can do this")
        names = sync.recover()
        if names:
            hub.notify({"type": "list"})
        return {"names": names}

    @app.get("/api/playlists/{pid}")
    def playlist(pid: int, u=Depends(user)):
        p = get_or_404(pid, u)
        keys = [it["key"] for it in p["items"]]
        mirrors = {m["member_id"]: m for m in db.mirrors_for(pid)}
        shared_with = db.member_ids(pid)
        title = cfg["playlist_prefix"] + p["name"]
        p["mirrors"] = []
        for m in backend.members():
            if m.id not in shared_with:
                continue
            mir = mirrors.get(m.id)
            synced = bool(mir and mir["synced_target"] == keys and mir["synced_title"] == title)
            p["mirrors"].append({
                **m.public(),
                "state": "error" if mir and mir["error"] else ("synced" if synced else "pending"),
                "error": redact(mir["error"]) if mir and mir["error"] else None,
                "missing": len(keys) - len(mir["synced_keys"]) if synced and mir["plex_key"] else 0,
                "synced_at": mir["synced_at"] if mir else None,
            })
        return p

    @app.post("/api/playlists/{pid}/members")
    def add_member(pid: int, body: MemberIn, u=Depends(user)):
        get_or_404(pid, u)
        m = member_map().get(body.member)
        if not m:
            raise HTTPException(400, "That person doesn't have access to this server")
        if db.add_member(pid, m.id, u["title"]):
            changed(pid, u["title"], f"shared it with {m.title}")
        return {"ok": True}

    @app.delete("/api/playlists/{pid}/members/{member_id}")
    def remove_member(pid: int, member_id: str, u=Depends(user)):
        get_or_404(pid, u)
        if db.member_ids(pid) == {member_id}:
            raise HTTPException(400, "A playlist needs at least one person. Delete it instead.")
        m = member_map().get(member_id)
        if db.remove_member(pid, member_id):
            who = "themselves" if member_id == u["id"] else (m.title if m else "someone")
            changed(pid, u["title"], f"removed {who}")
            hub.notify({"type": "removed", "id": pid})  # lets the removed person's page update
        return {"ok": True}

    @app.patch("/api/playlists/{pid}")
    def rename(pid: int, body: NameIn, u=Depends(user)):
        get_or_404(pid, u)
        name = body.name.strip()[:120]
        if not name:
            raise HTTPException(400, "Give it a name")
        if not db.rename_playlist(pid, name):
            raise HTTPException(404, "No such playlist")
        changed(pid, u["title"], f"renamed it to “{name}”")
        return {"ok": True}

    @app.delete("/api/playlists/{pid}")
    def delete(pid: int, u=Depends(user)):
        get_or_404(pid, u)
        db.delete_playlist(pid)
        sync.kick()
        hub.notify({"type": "removed", "id": pid})
        return {"ok": True}

    def _add(pid, tracks, u):
        get_or_404(pid, u)
        added = db.add_tracks(pid, tracks, u["title"])
        if added:
            what = f"“{added[0]['title']}”" if len(added) == 1 else f"{len(added)} tracks"
            changed(pid, u["title"], f"added {what}")
        return {"added": len(added)}

    @app.post("/api/playlists/{pid}/tracks")
    def add_tracks(pid: int, body: KeysIn, u=Depends(user)):
        return _add(pid, backend.get_tracks(body.keys[:500], member_map()[u["id"]]), u)

    @app.post("/api/playlists/{pid}/album")
    def add_album(pid: int, body: AlbumIn, u=Depends(user)):
        return _add(pid, backend.album_tracks(body.album, member_map()[u["id"]]), u)

    @app.delete("/api/playlists/{pid}/tracks/{key}")
    def remove_track(pid: int, key: str, u=Depends(user)):
        p = get_or_404(pid, u)
        t = next((it for it in p["items"] if it["key"] == key), None)
        if t and db.remove_tracks(pid, [key]):
            changed(pid, u["title"], f"removed “{t['title']}”")
        return {"ok": True}

    @app.post("/api/playlists/{pid}/move")
    def move(pid: int, body: MoveIn, u=Depends(user)):
        get_or_404(pid, u)
        if not db.move_track(pid, body.key, body.index):
            raise HTTPException(404, "Track isn't in this playlist")
        changed(pid, u["title"], "reordered tracks")
        return {"ok": True}

    @app.get("/api/search")
    def search(q: str = "", u=Depends(user)):
        q = q.strip()
        if len(q) < 2:
            return {"tracks": [], "albums": []}
        return backend.search(q[:200], member_map()[u["id"]])

    @app.get("/api/thumb/{key}")
    def thumb(key: str, u=Depends(user)):
        try:
            res = backend.thumb(key, member_map()[u["id"]])
        except Exception:  # not found, or not in a library this person has
            res = None
        if not res:
            return Response(status_code=404)
        return Response(res[0], media_type=res[1], headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/api/status")
    def status(u=Depends(user)):
        return {"last_sync": sync.last_run, "error": redact(sync.last_error) if sync.last_error else None, "poll_seconds": sync.poll,
                "pull_from_plex": sync.pull_enabled}

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        u = socket.session.get("user")
        if not u or u["id"] not in member_map() or not same_origin(socket.headers):
            await socket.close(code=4401)
            return
        await socket.accept()
        hub.clients[socket] = u["id"]
        try:
            while True:
                await socket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.pop(socket, None)

    return app
