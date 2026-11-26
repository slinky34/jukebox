"""Security checks for the web layer.  Needs httpx:  pip install httpx"""
import unittest

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jukebox.app import Hub, create_app
from jukebox.backend import DemoBackend, redact
from jukebox.db import DB
from jukebox.sync import SyncEngine


class AppSecurityTest(unittest.TestCase):
    def setUp(self):
        cfg = {"secret": "x" * 64, "client_id": "test", "playlist_prefix": "👥 ", "public_url": ""}
        self.backend = DemoBackend(seed_playlists=True)
        self.db = DB(":memory:")
        hub = Hub()
        sync = SyncEngine(self.db, self.backend, cfg, notify=hub.notify)
        self.app = create_app(cfg, self.backend, self.db, sync, hub)

    def client(self, member=None):
        c = TestClient(self.app, base_url="http://jukebox.lan:8765")
        if member:
            self.assertEqual(c.post("/auth/demo", json={"member": member}).status_code, 200)
        return c

    def test_everything_needs_sign_in(self):
        c = self.client()
        for path in ("/api/me", "/api/playlists", "/api/members", "/api/search?q=ab",
                     "/api/thumb/1000", "/api/status", "/api/plex-playlists"):
            self.assertEqual(c.get(path).status_code, 401, path)
        self.assertEqual(c.post("/api/playlists", json={"name": "x"}).status_code, 401)

    def test_other_peoples_playlists_are_invisible(self):
        sam = self.client("2")
        pid = sam.post("/api/playlists", json={"name": "Sam only"}).json()["id"]
        riley = self.client("3")
        self.assertEqual(riley.get("/api/playlists").json(), [])
        for method, path, body in [("get", f"/api/playlists/{pid}", None),
                                   ("patch", f"/api/playlists/{pid}", {"name": "mine"}),
                                   ("delete", f"/api/playlists/{pid}", None),
                                   ("post", f"/api/playlists/{pid}/members", {"member": "3"}),
                                   ("post", f"/api/playlists/{pid}/tracks", {"keys": ["1001"]})]:
            kw = {"json": body} if body else {}
            self.assertEqual(getattr(riley, method)(path, **kw).status_code, 404, (method, path))
        self.assertEqual(self.db.member_ids(pid), {"2"})

    def test_cross_site_requests_are_refused(self):
        c = self.client("2")
        r = c.post("/api/playlists", json={"name": "x"}, headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 403)
        r = c.post("/api/playlists", json={"name": "x"}, headers={"Origin": "http://jukebox.lan:8765"})
        self.assertEqual(r.status_code, 200)
        with self.assertRaises(WebSocketDisconnect):
            with c.websocket_connect("/ws", headers={"Origin": "http://evil.example"}) as ws:
                ws.receive_text()

    def test_security_headers(self):
        r = self.client().get("/")
        self.assertIn("script-src 'self'", r.headers["content-security-policy"])
        self.assertEqual(r.headers["x-frame-options"], "DENY")
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")
        self.assertNotIn("<script>", r.text.replace('<script src="/static/app.js"></script>', ""))
        self.assertNotIn("onclick=", r.text)

    def test_only_the_owner_can_recover_or_clean_up(self):
        sam, owner = self.client("2"), self.client("1")
        self.assertEqual(sam.post("/api/recover").status_code, 403)
        self.assertEqual(sam.get("/api/leftovers").status_code, 403)
        self.assertEqual(sam.post("/api/leftovers/delete", json={"name": "x"}).status_code, 403)
        self.assertEqual(sam.post("/api/leftovers/restore", json={"name": "x"}).status_code, 403)
        self.assertEqual(owner.post("/api/recover").status_code, 200)
        self.backend.add_personal("2", "👥 Test123", list(self.backend.library)[:2])
        self.assertEqual([l["name"] for l in owner.get("/api/leftovers").json()], ["Test123"])
        self.assertEqual(owner.post("/api/leftovers/delete", json={"name": "Test123"}).json(),
                         {"deleted": 1, "queued": 0})
        self.assertEqual(owner.get("/api/leftovers").json(), [])

    def test_owner_can_see_join_and_delete_playlists_they_are_not_in(self):
        sam, owner = self.client("2"), self.client("1")
        pid = sam.post("/api/playlists", json={"name": "Test123"}).json()["id"]
        self.assertEqual(sam.get("/api/others").status_code, 403)
        self.assertEqual(sam.delete(f"/api/others/{pid}").status_code, 403)
        self.assertEqual([p["name"] for p in owner.get("/api/others").json()], ["Test123"])
        self.assertEqual(owner.post(f"/api/others/{pid}/join").status_code, 200)
        self.assertEqual(self.db.member_ids(pid), {"1", "2"})
        self.assertEqual(owner.get("/api/others").json(), [])
        pid2 = sam.post("/api/playlists", json={"name": "Gone"}).json()["id"]
        self.assertEqual(owner.delete(f"/api/others/{pid2}").status_code, 200)
        self.assertIsNone(self.db.get_playlist(pid2))
        self.assertEqual(owner.delete(f"/api/others/{pid2}").status_code, 404)

    def test_import_and_download_endpoints_are_protected(self):
        c = self.client()
        self.assertEqual(c.get("/api/catalog?q=abc").status_code, 401)
        self.assertEqual(c.post("/api/import", json={"url": "https://open.spotify.com/playlist/abcdefghij"}).status_code, 401)
        sam, riley = self.client("2"), self.client("3")
        self.assertEqual(sam.post("/api/import", json={"url": "http://169.254.169.254/latest"}).status_code, 400)
        pid = sam.post("/api/playlists", json={"name": "x"}).json()["id"]
        r = sam.post(f"/api/playlists/{pid}/want",
                     json={"title": "Not Here", "artist": "Nobody", "link": "http://evil.example/track/1"})
        self.assertEqual(r.json(), {"added": False, "waiting": True})
        w = self.db.wanted_for(pid)[0]
        self.assertIsNone(w["link"])  # only real Deezer track links are kept (they go to deemix)
        # Not in the playlist: can't see, download or remove its waiting songs.
        self.assertEqual(riley.post(f"/api/playlists/{pid}/waiting/{w['id']}/download").status_code, 404)
        self.assertEqual(riley.delete(f"/api/playlists/{pid}/waiting/{w['id']}").status_code, 404)
        # In it, but downloads are owner-only by default (and none is set up here).
        self.assertEqual(sam.post(f"/api/playlists/{pid}/waiting/{w['id']}/download").status_code, 403)
        self.assertEqual(sam.post(f"/api/playlists/{pid}/waiting/download-all").status_code, 403)
        me = sam.get("/api/me").json()
        self.assertFalse(me["can_download"])
        self.assertEqual(sam.get(f"/api/playlists/{pid}").json()["waiting"][0]["title"], "Not Here")
        self.assertEqual(sam.delete(f"/api/playlists/{pid}/waiting/{w['id']}").status_code, 200)

    def test_secrets_never_reach_the_browser(self):
        self.app_cfg_secret_check = True
        c = self.client("1")
        for path in ("/api/config", "/api/me", "/api/status"):
            self.assertNotIn("x" * 64, c.get(path).text, path)

    def test_bad_keys_never_reach_plex(self):
        c = self.client("2")
        self.assertEqual(c.get("/api/thumb/..%2F..%2Faccounts").status_code, 404)
        pid = c.post("/api/playlists", json={"name": "x"}).json()["id"]
        r = c.post(f"/api/playlists/{pid}/album", json={"album": "../../accounts"})
        self.assertEqual(r.json(), {"added": 0})

    def test_no_api_docs_exposed(self):
        c = self.client()
        for path in ("/docs", "/redoc", "/openapi.json"):
            self.assertEqual(c.get(path).status_code, 404, path)

    def test_demo_login_is_off_for_real_servers(self):
        self.backend.demo = False
        self.assertEqual(self.client().post("/auth/demo", json={"member": "1"}).status_code, 404)

    def test_tokens_are_redacted(self):
        self.assertNotIn("sekrit", redact("GET http://plex:32400/x?X-Plex-Token=sekrit&a=1"))


if __name__ == "__main__":
    unittest.main()
