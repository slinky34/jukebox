"""Importing from other services, matching, waiting songs and downloaders (no network)."""
import unittest
from unittest import mock

from jukebox import downloaders, external
from jukebox.backend import DemoBackend
from jukebox.db import DB
from jukebox.importer import Importer
from jukebox.matching import best_match, clean_title, score


class MatchingTest(unittest.TestCase):
    def test_decorations_are_ignored(self):
        self.assertEqual(clean_title("Flower In A Hurricane (feat. Tame Impala, SZA) [From GTAVI: Original Score]"),
                         "Flower In A Hurricane")
        self.assertEqual(clean_title("Here Comes the Sun - Remastered 2009"), "Here Comes the Sun")
        self.assertEqual(clean_title("Bad Guy feat. Justin Bieber"), "Bad Guy")
        self.assertEqual(clean_title("Hey Jude"), "Hey Jude")

    def test_scores(self):
        want = {"title": "Blinding Lights", "artist": "The Weeknd", "duration": 200040}
        self.assertGreater(score(want, {"title": "Blinding Lights", "artist": "The Weeknd", "duration": 201000}), 0.95)
        self.assertGreater(score(want, {"title": "Blinding Lights", "artist": "Weeknd", "duration": 0}), 0.82)
        self.assertEqual(score(want, {"title": "Blinding Lights", "artist": "Some Cover Band", "duration": 200000}), 0)
        self.assertEqual(score(want, {"title": "Save Your Tears", "artist": "The Weeknd", "duration": 200000}), 0)
        many = {"title": "Señorita", "artist": "Shawn Mendes, Camila Cabello", "duration": 190000}
        self.assertTrue(best_match(many, [{"title": "Senorita", "artist": "Camila Cabello", "duration": 191000}]))
        # Same song, very different length (a live or extended version): not a match.
        self.assertIsNone(best_match(want, [{"title": "Blinding Lights", "artist": "The Weeknd", "duration": 400000}]))


class LinkTest(unittest.TestCase):
    def test_links(self):
        self.assertEqual(external.parse_link("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc"),
                         ("spotify", "37i9dQZF1DXcBWIGoYBM5M"))
        self.assertEqual(external.parse_link("https://open.spotify.com/intl-de/playlist/37i9dQZF1DXcBWIGoYBM5M"),
                         ("spotify", "37i9dQZF1DXcBWIGoYBM5M"))
        self.assertEqual(external.parse_link("https://music.apple.com/gb/playlist/todays-hits/pl.f4d106fed2bd41149aaacabb233eb5eb"),
                         ("apple", ("gb", "pl.f4d106fed2bd41149aaacabb233eb5eb")))
        self.assertEqual(external.parse_link("https://www.deezer.com/en/playlist/908622995"), ("deezer", "908622995"))
        for bad in ("https://evil.example/playlist/1", "http://169.254.169.254/", "", "open.spotify.com/track/abc"):
            with self.assertRaises(external.ImportError_):
                external.parse_link(bad)

    def test_requests_only_go_to_the_services(self):
        seen = []

        def fake_get(url, **kw):
            seen.append(url)
            raise external.ImportError_("stop")
        with mock.patch.object(external.requests, "get", side_effect=fake_get):
            for url in ("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M@evil.example",
                        "https://music.apple.com/us/playlist/x/pl.abcdef123456?u=http://evil.example",
                        "https://www.deezer.com/playlist/123#http://evil.example"):
                with self.assertRaises(external.ImportError_):
                    external.fetch_playlist(url, {})
        hosts = {u.split("/")[2] for u in seen}
        self.assertEqual(hosts, {"open.spotify.com", "music.apple.com", "api.deezer.com"})


class ImporterTest(unittest.TestCase):
    def setUp(self):
        self.b = DemoBackend()
        self.db = DB(":memory:")
        self.imp = Importer(self.db, self.b, {"downloads": "owner"})
        self.owner, self.sam = self.b.members()[0], self.b.members()[1]
        self.lib = list(self.b.library.values())

    def run_import(self, tracks):
        pl = {"name": "Road Songs", "source": "Spotify", "note": None, "tracks": tracks}
        with mock.patch.object(external, "fetch_playlist", return_value=pl), \
             mock.patch("threading.Thread") as th:
            jid = self.imp.start_import(self.sam, "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M")
            th.call_args.kwargs["target"](*th.call_args.kwargs["args"])
        return jid

    def test_import_matches_and_keeps_missing_songs_waiting(self):
        a, b = self.lib[0], self.lib[5]
        jid = self.run_import([
            {"title": a["title"] + " - Remastered", "artist": a["artist"], "album": "", "duration": a["duration"]},
            {"title": "Not In Plex", "artist": "Nobody", "album": "Nowhere", "duration": 0},
            {"title": b["title"], "artist": b["artist"], "album": "", "duration": 0},
        ])
        self.assertIsNone(self.imp.job(jid, self.owner))  # other people can't see your import
        j = self.imp.job(jid, self.sam)
        self.assertEqual(j["state"], "done")
        self.assertEqual([bool(i["match"]) for i in j["result"]["items"]], [True, False, True])
        pid, added, waiting = self.imp.create_from_job(jid, self.sam, "Mine")
        self.assertEqual((added, waiting), (2, 1))
        self.assertEqual(self.db.playlist_keys(pid), [a["key"], b["key"]])
        self.assertEqual(self.db.member_ids(pid), {"2"})
        w = self.db.wanted_for(pid)[0]
        self.assertEqual((w["title"], w["after_key"]), ("Not In Plex", a["key"]))

        # Later it turns up in Plex: it's added in its place, and stops waiting.
        self.b.library["9999"] = {"key": "9999", "title": "Not In Plex", "artist": "Nobody",
                                  "album": "Nowhere", "duration": 180000}
        self.assertEqual(self.imp.check_wanted(), 1)
        self.assertEqual(self.db.playlist_keys(pid), [a["key"], "9999", b["key"]])
        self.assertEqual(self.db.wanted_for(pid), [])

    def test_dropping_missing_songs(self):
        jid = self.run_import([{"title": "Not In Plex", "artist": "Nobody", "album": "", "duration": 0}])
        pid, added, waiting = self.imp.create_from_job(jid, self.sam, "", keep_missing=False)
        self.assertEqual((added, waiting), (0, 0))
        self.assertEqual(self.db.get_playlist(pid)["name"], "Road Songs")

    def test_downloads_permission(self):
        self.assertFalse(self.imp.can_download(self.owner))  # nothing configured
        self.imp.downloader = object()
        self.assertTrue(self.imp.can_download(self.owner))
        self.assertFalse(self.imp.can_download(self.sam))
        self.imp.cfg["downloads"] = "everyone"
        self.assertTrue(self.imp.can_download(self.sam))
        self.imp.cfg["downloads"] = "off"
        self.assertFalse(self.imp.can_download(self.owner))


class FakeResponse:
    def __init__(self, data=None, status=200):
        self._data, self.status_code = data, status
        self.ok = status < 400
        self.content = b"x" if data is not None else b""
        self.text = str(data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if not self.ok:
            raise downloaders.requests.HTTPError(str(self.status_code))


class DownloaderTest(unittest.TestCase):
    song = {"title": "stupid song", "artist": "Olivia Rodrigo", "album": "", "link": None}
    deezer = {"title": "stupid song", "artist": "Olivia Rodrigo", "album": "you seem pretty sad",
              "duration": 209000, "link": "https://www.deezer.com/track/4078104881"}

    def test_deemix_logs_in_and_queues_the_deezer_link(self):
        d = downloaders.Deemix("http://deemix:6595/", "arl-secret")
        calls = []

        def post(url, json=None, **kw):
            calls.append((url, json))
            return FakeResponse({"status": 1} if url.endswith("loginArl") else {"result": True})
        with mock.patch.object(downloaders, "deezer_lookup", return_value=self.deezer), \
             mock.patch.object(d.s, "post", side_effect=post):
            res = d.request(dict(self.song))
        self.assertEqual(calls[0], ("http://deemix:6595/api/loginArl", {"arl": "arl-secret", "force": True}))
        self.assertEqual(calls[1][1]["url"], self.deezer["link"])
        self.assertEqual(res["link"], self.deezer["link"])

    def test_lidarr_adds_the_album_with_its_artist_unmonitored(self):
        lid = downloaders.Lidarr("http://lidarr:8686", "key")
        sent = []

        def api(method, url, json=None, params=None, **kw):
            path = url.split("/api/v1")[1]
            sent.append((method, path, json))
            if path == "/album/lookup":
                return FakeResponse([
                    {"title": "Other Album", "artist": {"artistName": "Olivia Rodrigo"}},
                    {"title": "you seem pretty sad", "foreignAlbumId": "x1",
                     "artist": {"artistName": "Olivia Rodrigo", "foreignArtistId": "a1"}},
                ])
            if path == "/rootfolder":
                return FakeResponse([{"path": "/music", "id": 1}])
            if path in ("/qualityprofile", "/metadataprofile"):
                return FakeResponse([{"id": 7, "name": "Any"}])
            return FakeResponse({})
        with mock.patch.object(downloaders, "deezer_lookup", return_value=self.deezer), \
             mock.patch.object(downloaders.requests, "request", side_effect=api):
            res = lid.request(dict(self.song))
        add = next(j for m, p, j in sent if m == "POST" and p == "/album")
        self.assertEqual(add["title"], "you seem pretty sad")
        self.assertTrue(add["addOptions"]["searchForNewAlbum"])
        self.assertEqual(add["artist"]["addOptions"]["monitor"], "none")
        self.assertEqual(add["artist"]["rootFolderPath"], "/music")
        self.assertIn("Added", res["note"])

    def test_lidarr_refuses_a_different_artists_album(self):
        lid = downloaders.Lidarr("http://lidarr:8686", "key")
        with mock.patch.object(downloaders, "deezer_lookup", return_value=self.deezer), \
             mock.patch.object(downloaders.requests, "request",
                               return_value=FakeResponse([{"title": "you seem pretty sad",
                                                           "artist": {"artistName": "Cover Band"}}])):
            with self.assertRaises(downloaders.DownloadError):
                lid.request(dict(self.song))


if __name__ == "__main__":
    unittest.main()
