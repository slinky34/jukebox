"""Run with:  python -m unittest discover tests"""
import random
import unittest

from jukebox.backend import DemoBackend, plan_moves
from jukebox.db import DB
from jukebox.sync import SyncEngine


def apply_moves(items, moves):
    items = list(items)
    for k, after in moves:
        items.remove(k)
        items.insert(0 if after is None else items.index(after) + 1, k)
    return items


class PlanMovesTest(unittest.TestCase):
    def test_random_permutations(self):
        rng = random.Random(1)
        for n in range(0, 40):
            for _ in range(20):
                cur = [str(i) for i in range(n)]
                tgt = cur[:]
                rng.shuffle(tgt)
                self.assertEqual(apply_moves(cur, plan_moves(cur, tgt)), tgt)

    def test_single_move_is_one_request(self):
        cur = list("abcdefgh")
        tgt = list("abfcdegh")
        self.assertEqual(len(plan_moves(cur, tgt)), 1)


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.b = DemoBackend()
        self.db = DB(":memory:")
        self.events = []
        self.sync = SyncEngine(self.db, self.b, {"playlist_prefix": "👥 "}, notify=self.events.append)
        self.keys = list(self.b.library)
        self.pid = self.db.create_playlist("Road Trip", "Sam", "2")
        for m in self.b.members():
            self.db.add_member(self.pid, m.id, "Sam")

    def plex(self, member_id):
        """Track keys in that member's Plex copy."""
        pls = [p for p in self.b.plex[member_id].values() if p["title"] == "👥 Road Trip"]
        self.assertLessEqual(len(pls), 1)
        return [k for k, _ in pls[0]["items"]] if pls else None

    def master(self):
        return self.db.playlist_keys(self.pid)

    def add(self, keys, who="Sam"):
        self.db.add_tracks(self.pid, self.b.get_tracks(keys), who)

    def assert_everyone(self, keys):
        for m in self.b.members():
            self.assertEqual(self.plex(m.id), keys, m.title)

    def test_empty_playlist_has_no_plex_copy(self):
        self.sync.run_once()
        for m in self.b.members():
            self.assertIsNone(self.plex(m.id))

    def test_web_edits_reach_everyone(self):
        self.add(self.keys[:5])
        self.sync.run_once()
        self.assert_everyone(self.keys[:5])

        self.db.move_track(self.pid, self.keys[4], 0)
        self.db.remove_tracks(self.pid, [self.keys[2]])
        self.add(self.keys[10:12])
        self.sync.run_once()
        expected = [self.keys[4], self.keys[0], self.keys[1], self.keys[3], self.keys[10], self.keys[11]]
        self.assertEqual(self.master(), expected)
        self.assert_everyone(expected)

    def test_quiet_cycle_makes_no_writes(self):
        self.add(self.keys[:5])
        self.sync.run_once()
        before = self.b.calls
        self.sync.run_once()
        self.assertEqual(self.b.calls - before, 4)  # one read per member, nothing else

    def test_edits_made_in_plex_flow_to_everyone(self):
        self.add(self.keys[:4])
        self.sync.run_once()
        # Riley adds a song in Plexamp between tracks 2 and 3, Jordan removes track 1.
        pl = next(iter(self.b.plex["3"].values()))
        pl["items"].insert(2, (self.keys[20], "x1"))
        self.b.plex_edit("4", "👥 Road Trip", remove=[self.keys[0]])
        self.sync.run_once()
        expected = [self.keys[1], self.keys[20], self.keys[2], self.keys[3]]
        self.assertEqual(self.master(), expected)
        self.assert_everyone(expected)
        added = self.db.get_playlist(self.pid)["items"][1]
        self.assertEqual(added["added_by"], "Riley")
        self.assertTrue(any(e.get("who") == "Riley" for e in self.events))

    def test_plex_and_web_edits_in_the_same_cycle(self):
        self.add(self.keys[:3])
        self.sync.run_once()
        self.b.plex_edit("2", "👥 Road Trip", add=[self.keys[30]])
        self.add([self.keys[31]], who="Riley")          # via the web app
        self.db.remove_tracks(self.pid, [self.keys[0]])  # via the web app
        self.sync.run_once()
        self.assertEqual(set(self.master()), {self.keys[1], self.keys[2], self.keys[30], self.keys[31]})
        self.assert_everyone(self.master())

    def test_user_deleting_their_copy_gets_it_back_without_losing_songs(self):
        self.add(self.keys[:3])
        self.sync.run_once()
        self.b.plex["2"].clear()
        self.sync.run_once()
        self.assertEqual(self.master(), self.keys[:3])
        self.assert_everyone(self.keys[:3])

    def test_mass_removal_is_treated_as_a_glitch(self):
        self.add(self.keys[:12])
        self.sync.run_once()
        self.b.plex_edit("2", "👥 Road Trip", remove=self.keys[:12])
        self.sync.run_once()
        self.assertEqual(self.master(), self.keys[:12])
        self.assert_everyone(self.keys[:12])

    def test_tracks_hidden_from_a_user_are_not_removed_for_everyone(self):
        self.b.hidden["3"] = {self.keys[1]}
        self.add(self.keys[:3])
        self.sync.run_once()
        self.sync.run_once()
        self.sync.run_once()
        self.assertEqual(self.master(), self.keys[:3])
        self.assertEqual(self.plex("3"), [self.keys[0], self.keys[2]])
        self.assertEqual(self.plex("2"), self.keys[:3])
        before = self.b.calls
        self.sync.run_once()
        self.assertEqual(self.b.calls - before, 4)  # doesn't retry the hidden track forever

    def test_unreachable_user_catches_up_later(self):
        self.add(self.keys[:2])
        self.sync.run_once()
        self.b.fail.add("4")
        self.add(self.keys[2:4])
        self.sync.run_once()
        self.assertEqual(self.plex("2"), self.keys[:4])
        self.assertEqual(self.plex("4"), self.keys[:2])
        self.assertIn("unreachable", self.db.get_mirror(self.pid, "4")["error"])
        self.b.fail.clear()
        self.sync.run_once()
        self.assert_everyone(self.keys[:4])
        self.assertIsNone(self.db.get_mirror(self.pid, "4")["error"])

    def test_rename_and_delete(self):
        self.add(self.keys[:2])
        self.sync.run_once()
        self.db.rename_playlist(self.pid, "Night Drive")
        self.sync.run_once()
        titles = {p["title"] for m in self.b.members() for p in self.b.plex[m.id].values()}
        self.assertEqual(titles, {"👥 Night Drive"})
        self.db.delete_playlist(self.pid)
        self.sync.run_once()
        self.assertTrue(all(not self.b.plex[m.id] for m in self.b.members()))

    def test_duplicates_added_in_plex_are_cleaned_up(self):
        self.add(self.keys[:3])
        self.sync.run_once()
        self.b.plex_edit("2", "👥 Road Trip", add=[self.keys[0]])
        self.sync.run_once()
        self.assert_everyone(self.keys[:3])

    def test_only_members_get_a_copy(self):
        pid = self.db.create_playlist("Just Us", "Sam", "2")
        self.db.add_member(pid, "3", "Sam")
        self.db.add_tracks(pid, self.b.get_tracks(self.keys[:2]), "Sam")
        self.sync.run_once()
        has = {m.id for m in self.b.members()
               if any(p["title"] == "👥 Just Us" for p in self.b.plex[m.id].values())}
        self.assertEqual(has, {"2", "3"})

    def test_removing_someone_deletes_their_copy_and_ignores_their_edits(self):
        self.add(self.keys[:3])
        self.sync.run_once()
        self.db.remove_member(self.pid, "4")
        self.sync.run_once()
        self.assertIsNone(self.plex("4"))
        self.assertIsNone(self.db.get_mirror(self.pid, "4"))
        self.assertEqual(self.plex("2"), self.keys[:3])
        # Adding them back gives them a fresh copy.
        self.db.add_member(self.pid, "4", "Sam")
        self.sync.run_once()
        self.assertEqual(self.plex("4"), self.keys[:3])

    def test_upgrade_gives_unshared_playlists_to_owner(self):
        db = DB(":memory:")
        with db.tx() as c:  # a playlist from before sharing existed: no members
            c.execute("INSERT INTO playlists(name, created_by, created_at, updated_at) VALUES ('Old','x',0,0)")
        self.assertEqual(db.give_unshared_playlists_to("1"), 1)
        self.assertEqual(db.member_ids(1), {"1"})
        self.assertEqual(db.give_unshared_playlists_to("1"), 0)
        self.assertEqual([p["name"] for p in db.list_playlists("1")], ["Old"])
        self.assertEqual(db.list_playlists("2"), [])

    # ---- turning an existing Plex playlist into a shared one ----

    def _owner(self):
        return self.b.members()[0]

    def test_import_adopts_the_original(self):
        k = self.keys
        key = self.b.add_personal("1", "Gym", [k[5], k[6], k[5], k[7]])  # has a duplicate
        pid, count, adopted = self.sync.import_playlist(self._owner(), key)
        self.assertTrue(adopted)
        self.assertEqual(count, 3)
        self.assertEqual(self.db.playlist_keys(pid), [k[5], k[6], k[7]])
        self.assertEqual(self.db.member_ids(pid), {"1"})
        self.db.add_member(pid, "3", "x")
        self.sync.run_once()
        own = self.b.plex["1"][key]  # same Plex playlist, now the synced copy
        self.assertEqual(own["title"], "👥 Gym")
        self.assertEqual([rk for rk, _ in own["items"]], [k[5], k[6], k[7]])
        self.assertEqual(len(self.b.plex["1"]), 1)  # no second copy made
        riley = [p for p in self.b.plex["3"].values() if p["title"] == "👥 Gym"]
        self.assertEqual([rk for rk, _ in riley[0]["items"]], [k[5], k[6], k[7]])
        # Later edits to it inside Plex still flow to everyone.
        self.b.plex_edit("1", "👥 Gym", add=[k[9]])
        self.sync.run_once()
        self.assertEqual(self.db.playlist_keys(pid), [k[5], k[6], k[7], k[9]])

    def test_import_keeping_the_original(self):
        k = self.keys
        key = self.b.add_personal("1", "Chill", [k[1], k[2]])
        pid, _, adopted = self.sync.import_playlist(self._owner(), key, keep_original=True)
        self.assertFalse(adopted)
        self.sync.run_once()
        self.assertEqual(self.b.plex["1"][key]["title"], "Chill")
        titles = sorted(p["title"] for p in self.b.plex["1"].values())
        self.assertEqual(titles, ["Chill", "👥 Chill"])

    def test_import_smart_playlist_is_copied(self):
        key = self.b.add_personal("1", "Recently Played", self.keys[:3], smart=True)
        _, _, adopted = self.sync.import_playlist(self._owner(), key)
        self.assertFalse(adopted)
        self.sync.run_once()
        self.assertEqual(self.b.plex["1"][key]["title"], "Recently Played")

    def test_import_errors(self):
        with self.assertRaises(LookupError):
            self.sync.import_playlist(self._owner(), "nope")
        key = self.b.add_personal("1", "Ghosts", ["not-a-track"])
        with self.assertRaises(ValueError):
            self.sync.import_playlist(self._owner(), key)

    # ---- bringing back playlists after the database was lost ----

    def test_recover_rebuilds_from_existing_copies(self):
        k = self.keys
        self.add(k[:4])
        self.b.hidden["3"] = {k[1]}
        self.sync.run_once()
        other = self.db.create_playlist("Just Us", "Sam", "2")
        self.db.add_member(other, "4", "Sam")
        self.db.add_tracks(other, self.b.get_tracks(k[10:12]), "Sam")
        self.sync.run_once()
        self.b.add_personal("1", "Mine", k[20:22])  # not a shared copy: left alone

        # Lose the database and start fresh.
        self.db = DB(":memory:")
        self.sync = SyncEngine(self.db, self.b, {"playlist_prefix": "👥 "})
        before = {m.id: dict(self.b.plex[m.id]) for m in self.b.members()}
        self.assertEqual(self.sync.recover(), ["Just Us", "Road Trip"])
        by_name = {p["name"]: p["id"] for p in self.db.list_playlists()}
        road, us = by_name["Road Trip"], by_name["Just Us"]
        self.assertEqual(self.db.member_ids(road), {"1", "2", "3", "4"})
        self.assertEqual(self.db.member_ids(us), {"2", "4"})
        self.assertEqual(self.db.playlist_keys(road), k[:4])  # Riley's copy lacked a hidden song
        self.assertEqual(self.db.playlist_keys(us), k[10:12])

        before_calls = self.b.calls
        self.sync.run_once()
        self.sync.run_once()
        # No new copies: everyone keeps the playlists they had, untouched.
        for m in self.b.members():
            self.assertEqual(set(self.b.plex[m.id]), set(before[m.id]), m.title)
        self.assertEqual(self.plex("2"), k[:4])
        self.assertEqual(self.plex("3"), [k[0], k[2], k[3]])
        self.assertEqual(self.db.playlist_keys(road), k[:4])
        # And it's live again: an edit in Plex reaches everyone.
        self.b.plex_edit("4", "👥 Road Trip", add=[k[30]])
        self.sync.run_once()
        self.assertEqual(self.plex("2"), k[:4] + [k[30]])
        # Running it again finds nothing new.
        self.assertEqual(self.sync.recover(), [])
        self.assertGreater(self.b.calls, before_calls)

    def test_recover_skips_copies_waiting_to_be_deleted(self):
        self.add(self.keys[:2])
        self.sync.run_once()
        self.b.fail.add("4")
        self.db.remove_member(self.pid, "4")
        self.sync.run_once()  # Jordan's copy can't be deleted yet
        self.db.delete_playlist(self.pid)
        self.assertEqual(self.sync.recover(), [])

    def test_leftovers_can_be_deleted_everywhere(self):
        k = self.keys
        self.add(k[:3])
        self.sync.run_once()
        # "Test123" was deleted while records were lost: copies stay in everyone's Plex.
        for mid in ("1", "2", "3"):
            self.b.add_personal(mid, "👥 Test123", k[5:7])
        self.b.add_personal("2", "👥 Test123", k[5:6])   # a duplicate copy too
        self.b.add_personal("2", "👥 Road Trip", k[:1])  # stray extra copy of a live playlist
        mine = self.b.add_personal("3", "Riley's own", k[:2])

        left = {l["name"]: l for l in self.sync.leftovers()}
        self.assertEqual(set(left), {"Test123", "Road Trip"})
        self.assertEqual(left["Test123"]["copies"], 4)
        self.assertEqual(len(left["Test123"]["people"]), 3)
        self.assertFalse(left["Test123"]["in_jukebox"])
        self.assertTrue(left["Road Trip"]["in_jukebox"])

        self.b.fail.add("3")
        self.assertEqual(self.sync.delete_leftovers("Test123"), (3, 1))  # Riley didn't answer
        self.b.fail.clear()
        self.assertEqual(self.sync.delete_leftovers("Test123"), (1, 0))
        for m in self.b.members():
            self.assertFalse(any(p["title"] == "👥 Test123" for p in self.b.plex[m.id].values()), m.title)

        self.assertEqual(self.sync.delete_leftovers("Road Trip"), (1, 0))
        self.sync.run_once()
        self.assert_everyone(k[:3])  # the real shared copies are untouched
        self.assertIn(mine, self.b.plex["3"])
        self.assertEqual(self.sync.leftovers(), [])

    def test_bring_back_one_leftover(self):
        k = self.keys
        self.b.add_personal("1", "👥 Old", k[1:3])
        self.b.add_personal("1", "👥 Other", k[4:5])
        self.assertEqual(self.sync.recover(names={"Old"}), ["Old"])
        self.assertEqual([l["name"] for l in self.sync.leftovers()], ["Other"])

    def test_importing_an_old_copy_drops_the_prefix(self):
        key = self.b.add_personal("1", "👥 Old Times", self.keys[:2])
        pid, _, _ = self.sync.import_playlist(self._owner(), key)
        self.assertEqual(self.db.get_playlist(pid)["name"], "Old Times")
        self.sync.run_once()
        self.assertEqual(self.b.plex["1"][key]["title"], "👥 Old Times")

    def test_big_playlist(self):
        self.add(self.keys[:84])
        self.sync.run_once()
        self.assert_everyone(self.keys[:84])


if __name__ == "__main__":
    unittest.main()
