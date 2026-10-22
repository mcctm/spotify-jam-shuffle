"""
Tests for jam_shuffle.py. Uses only the standard library, and never talks to
Spotify: a FakeSpotify stands in for the API and a fake spotipy module stands
in for sign-in.

Run from this folder:   python3 -m unittest -v
"""

import io
import json
import os
import random
import subprocess
import sys
import tempfile
import types
import unittest
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import jam_shuffle as js

HERE = Path(__file__).resolve().parent


# ---------------------------------------------------------------- helpers

def track(uri, artist="Artist"):
    return {"uri": uri, "name": f"Song {uri}", "artists": [{"name": artist}]}


def songs_for(counts):
    """{'Alice': 3, 'Ben': 1} -> list of song dicts, each with a unique uri."""
    return [{"uri": f"{p}-{i}", "title": f"{p} {i}", "added_by": p}
            for p, n in counts.items() for i in range(n)]


def adjacent_repeats(order):
    return sum(a["added_by"] == b["added_by"] for a, b in zip(order, order[1:]))


def runs_of(order, person):
    """Lengths of each back-to-back run of `person`'s songs."""
    runs, n = [], 0
    for s in order:
        if s["added_by"] == person:
            n += 1
        elif n:
            runs.append(n)
            n = 0
    if n:
        runs.append(n)
    return runs


class FakeSpotify:
    """Just the parts of spotipy.Spotify the script uses."""

    def __init__(self, current, queue, autoplay=(), fail_uris=()):
        self.current = current
        self.items = list(queue) + list(autoplay)
        self.autoplay = list(autoplay)
        self.fail_uris = set(fail_uris)
        self.added = []

    def queue(self):
        return {"currently_playing": self.current, "queue": list(self.items)}

    def add_to_queue(self, uri):
        if uri in self.fail_uris:
            raise Exception("device not found")
        self.added.append(uri)

    def clear(self):
        """What the host does by hand: queued songs go, autoplay stays."""
        self.items = list(self.autoplay)


class Inputs:
    """Stands in for input(). Each answer is a string, or a function that is
    called first (to simulate the host doing something) and returns one."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        a = self.answers.pop(0)
        return a() if callable(a) else a


class StateDirTest(unittest.TestCase):
    """Points every state file at a fresh temp folder and silences sleeps."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name) / "state"
        for name, value in [("STATE_DIR", d),
                            ("CONFIG_FILE", d / "config.json"),
                            ("TOKEN_FILE", d / "token.json"),
                            ("TAGS_FILE", d / "tags.json"),
                            ("RESTORE_FILE", d / "restore.json")]:
            p = mock.patch.object(js, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(js.time, "sleep")
        p.start()
        self.addCleanup(p.stop)

    def run_quietly(self, fn, *args, inputs=None):
        """Call fn with input() scripted; return (result, printed output).
        The output is also kept in self.printed, for runs that exit early."""
        out = io.StringIO()
        self.printed = ""
        try:
            with mock.patch("builtins.input", inputs or Inputs()), redirect_stdout(out):
                result = fn(*args)
        finally:
            self.printed = out.getvalue()
        return result, self.printed


# ---------------------------------------------------------------- fair shuffle

class FairShuffleTest(unittest.TestCase):

    def test_empty_and_single_song(self):
        self.assertEqual(js.fair_shuffle([]), [])
        one = songs_for({"Alice": 1})
        self.assertEqual(js.fair_shuffle(one), one)

    def test_spec_example_has_no_repeats(self):
        songs = songs_for({"Alice": 4, "Ben": 2, "Cara": 1})
        for seed in range(200):
            order = js.fair_shuffle(songs, random.Random(seed))
            self.assertEqual(adjacent_repeats(order), 0)
            # Alice 4 of 7 with no repeats means Alice in every odd slot
            self.assertEqual([s["added_by"] for s in order[::2]], ["Alice"] * 4)

    def test_random_queues_keep_every_song_and_use_fewest_repeats(self):
        rng = random.Random(1234)
        for _ in range(3000):
            counts = {f"P{i}": rng.randint(0, 8) for i in range(rng.randint(1, 5))}
            songs = songs_for(counts)
            order = js.fair_shuffle(songs, rng)
            # Nothing lost, nothing added: the very same song objects come back
            self.assertCountEqual(map(id, order), map(id, songs))
            top = max(counts.values())
            fewest = max(0, top - (len(songs) - top) - 1)
            self.assertEqual(adjacent_repeats(order), fewest, counts)

    def test_dominant_persons_repeats_are_spread_evenly(self):
        rng = random.Random(99)
        for _ in range(1000):
            top = rng.randint(3, 15)
            counts = {"Top": top}
            counts.update({f"P{i}": 1 for i in range(rng.randint(0, top - 2))})
            others = sum(counts.values()) - top
            order = js.fair_shuffle(songs_for(counts), rng)
            runs = runs_of(order, "Top")
            # Top's songs fill every gap: before, between and after the others
            self.assertEqual(len(runs), others + 1, counts)
            # ...in runs that differ by at most one song, so not bunched up
            self.assertLessEqual(max(runs) - min(runs), 1, counts)

    def test_orders_are_random(self):
        def orders(counts, person=None):
            songs = songs_for(counts)
            return {tuple(s["uri"] for s in js.fair_shuffle(songs, random.Random(seed))
                          if person in (None, s["added_by"]))
                    for seed in range(200)}

        # One participant: a plain shuffle (120 possible orders)
        self.assertGreater(len(orders({"Alice": 5})), 50)
        # Each person's own songs are shuffled: every ordering of Alice's 3
        self.assertEqual(len(orders({"Alice": 3, "Ben": 3}, "Alice")), 6)

    def test_duplicate_tracks_stay_separate(self):
        dup = {"uri": "same", "title": "Same", "added_by": "Alice"}
        songs = [dup, dict(dup), {"uri": "b", "title": "B", "added_by": "Ben"},
                 {"uri": "same", "title": "Same", "added_by": "Ben"}]
        order = js.fair_shuffle(songs, random.Random(0))
        self.assertEqual(Counter(s["uri"] for s in order), {"same": 3, "b": 1})


# ---------------------------------------------------------------- small helpers

class HelperTest(StateDirTest):

    def test_save_and_load_json(self):
        self.assertEqual(js.load_json(js.TAGS_FILE, {"d": 1}), {"d": 1})  # missing
        js.save_json(js.TAGS_FILE, {"uri": "Alice"})
        self.assertEqual(js.load_json(js.TAGS_FILE, None), {"uri": "Alice"})
        self.assertEqual([p.name for p in js.STATE_DIR.iterdir()], ["tags.json"])
        js.TAGS_FILE.write_text("{not json")
        self.assertEqual(js.load_json(js.TAGS_FILE, {}), {})  # corrupt

    def test_add_with_retry(self):
        sp = mock.Mock()
        sp.add_to_queue.side_effect = [Exception("blip"), None]
        self.assertIsNone(js.add_with_retry(sp, "u"))

        sp.add_to_queue.side_effect = Exception("gone")
        sp.add_to_queue.reset_mock()
        js.time.sleep.reset_mock()
        self.assertEqual(js.add_with_retry(sp, "u"), "gone")
        self.assertEqual(sp.add_to_queue.call_count, 3)
        self.assertEqual(js.time.sleep.call_count, 2)  # no wait after the last try


# ---------------------------------------------------------------- tagging

class TagSongsTest(StateDirTest):

    def tag(self, items, tags, *answers):
        inputs = Inputs(*answers)
        songs, _ = self.run_quietly(js.tag_songs, items, tags, inputs=inputs)
        return songs, inputs

    def test_enter_number_and_new_name(self):
        items = [track("1"), track("2"), track("3"), track("4")]
        # Enter = remembered name, 2 = Ben from the list, Cara = new name,
        # Enter on an unknown song = previous person
        songs, inputs = self.tag(items, {"1": "Alice", "x": "Ben"}, "", "2", "Cara", "")
        self.assertEqual([s["added_by"] for s in songs], ["Alice", "Ben", "Cara", "Cara"])
        self.assertIn("[Enter = Cara]", inputs.prompts[3])
        self.assertEqual(songs[0], {"uri": "1", "title": "Song 1 — Artist", "added_by": "Alice"})

    def test_dot_stops_and_q_cancels(self):
        items = [track("1"), track("2"), track("3")]
        songs, _ = self.tag(items, {}, "Alice", ".")
        self.assertEqual([s["uri"] for s in songs], ["1"])
        songs, _ = self.tag(items, {}, "Alice", "q")
        self.assertIsNone(songs)

    def test_bad_answers_ask_again(self):
        # Enter with nothing to suggest, then a number that isn't on the list
        songs, inputs = self.tag([track("1")], {"a": "Alice"}, "", "7", "1")
        self.assertEqual(len(inputs.prompts), 3)
        self.assertEqual(songs[0]["added_by"], "Alice")


# ---------------------------------------------------------------- restoring

class RestoreTest(StateDirTest):

    def state(self, next_=0):
        return {"songs": songs_for({"Alice": 2, "Ben": 2}),
                "next": next_, "cleared": True, "failed": []}

    def test_adds_in_order_remembers_names_and_cleans_up(self):
        js.save_json(js.TAGS_FILE, {"old": "Dee"})
        sp, state = FakeSpotify(None, []), self.state()
        js.save_json(js.RESTORE_FILE, state)
        _, out = self.run_quietly(js.restore, sp, state)
        self.assertEqual(sp.added, [s["uri"] for s in state["songs"]])
        self.assertIn("[4/4]", out)
        self.assertIn("Done. 4 songs added", out)
        self.assertFalse(js.RESTORE_FILE.exists())
        # Re-added songs show the host in Spotify, so the real names are kept
        tags = js.load_json(js.TAGS_FILE, {})
        self.assertEqual(tags["old"], "Dee")
        self.assertEqual(tags["Ben-1"], "Ben")

    def test_failed_songs_are_listed_and_the_rest_still_added(self):
        sp = FakeSpotify(None, [], fail_uris={"Alice-1"})
        _, out = self.run_quietly(js.restore, sp, self.state())
        self.assertEqual(len(sp.added), 3)
        self.assertIn("Done. 3 songs added", out)
        self.assertIn("Alice 1  (Alice)  — device not found", out)

    def test_crash_midway_then_resume(self):
        state = self.state()
        sp = FakeSpotify(None, [])
        real_add = sp.add_to_queue

        def crash_on_third(uri):
            if len(sp.added) == 2:
                raise KeyboardInterrupt  # terminal closed mid-restore
            real_add(uri)

        sp.add_to_queue = crash_on_third
        with self.assertRaises(KeyboardInterrupt):
            self.run_quietly(js.restore, sp, state)
        saved = js.load_json(js.RESTORE_FILE, None)
        self.assertEqual(saved["next"], 2)

        # The next run picks up the last two songs, so each is added once
        sp2 = FakeSpotify(None, [])
        self.run_quietly(js.restore, sp2, saved)
        self.assertEqual(sp.added + sp2.added, [s["uri"] for s in state["songs"]])


class OfferResumeTest(StateDirTest):

    def setUp(self):
        super().setUp()
        self.state = {"songs": songs_for({"Alice": 2, "Ben": 1}), "next": 1,
                      "cleared": True, "failed": []}
        js.save_json(js.RESTORE_FILE, self.state)

    def test_yes_finishes_the_restore(self):
        # Alice-0 was added back before the stop, so seeing it queued is fine
        sp = FakeSpotify(None, [track("Alice-0")])
        result, out = self.run_quietly(js.offer_resume, sp, inputs=Inputs(""))
        self.assertTrue(result)
        self.assertIn("2 song(s) still to add back", out)
        self.assertNotIn("Warning", out)
        self.assertEqual(sp.added, ["Alice-1", "Ben-0"])
        self.assertFalse(js.RESTORE_FILE.exists())

    def test_no_keeps_it_and_discard_drops_it(self):
        sp = FakeSpotify(None, [])
        with self.assertRaises(SystemExit):
            self.run_quietly(js.offer_resume, sp, inputs=Inputs("n"))
        self.assertTrue(js.RESTORE_FILE.exists())
        result, _ = self.run_quietly(js.offer_resume, sp, inputs=Inputs("discard"))
        self.assertFalse(result)
        self.assertFalse(js.RESTORE_FILE.exists())
        self.assertEqual(sp.added, [])

    def test_warns_if_queue_was_never_cleared(self):
        self.state.update(next=0, cleared=False)
        js.save_json(js.RESTORE_FILE, self.state)
        sp = FakeSpotify(None, [track(s["uri"]) for s in self.state["songs"]])

        def clear():
            sp.clear()
            return ""

        # Enter to resume, warned that the songs are still queued, clear, Enter
        result, out = self.run_quietly(js.offer_resume, sp, inputs=Inputs("", clear))
        self.assertIn("before you confirmed clearing", out)
        self.assertIn("Warning: 3 of these songs still look queued", out)
        self.assertTrue(result)
        self.assertEqual(sp.added, [s["uri"] for s in self.state["songs"]])


# ---------------------------------------------------------------- full runs

class MainTest(StateDirTest):
    """Whole runs of main() against a FakeSpotify."""

    def setUp(self):
        super().setUp()
        self.jam = [track(f"t{i}") for i in range(6)]
        # Alice 3, Ben 2, Cara 1, remembered from a previous run
        js.save_json(js.TAGS_FILE, {"t0": "Alice", "t1": "Alice", "t2": "Alice",
                                    "t3": "Ben", "t4": "Ben", "t5": "Cara"})
        self.sp = FakeSpotify(track("now"), self.jam, [track("auto1"), track("auto2")])
        p = mock.patch.object(js, "connect", return_value=self.sp)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(sys, "argv", ["jam_shuffle.py"])
        p.start()
        self.addCleanup(p.stop)

    def clear_queue(self):
        self.sp.clear()
        return ""

    def run_main(self, *answers):
        return self.run_quietly(js.main, inputs=Inputs(*answers))[1]

    def tag_all_then(self, *answers):
        """Enter for each Jam song, '.' at autoplay, then the given answers."""
        return [""] * len(self.jam) + ["."] + list(answers)

    def test_happy_path(self):
        out = self.run_main(*self.tag_all_then(self.clear_queue))
        self.assertCountEqual(self.sp.added, [t["uri"] for t in self.jam])
        self.assertNotIn("now", self.sp.added)  # playing song untouched, autoplay too
        tags = js.load_json(js.TAGS_FILE, {})
        self.assertEqual(adjacent_repeats([{"added_by": tags[u]} for u in self.sp.added]), 0)
        self.assertIn("Done. 6 songs added", out)
        self.assertFalse(js.RESTORE_FILE.exists())

    def test_stops_without_changing_anything(self):
        cases = [
            ("Nothing is playing", lambda: setattr(self.sp, "current", None), []),
            ("fewer than two", lambda: setattr(self.sp, "items", [track("t0")]), []),
            ("Fewer than two Jam songs", None, ["", "."]),
            ("Nothing was changed", None, ["", "q"]),           # cancel while tagging
            ("not touched", None, self.tag_all_then("q")),      # cancel instead of clearing
        ]
        for message, setup, answers in cases:
            with self.subTest(message):
                self.setUp()
                if setup:
                    setup()
                with self.assertRaises(SystemExit) as e:
                    self.run_main(*answers)
                self.assertIn(message, str(e.exception.code))
                self.assertEqual(self.sp.added, [])
                self.assertFalse(js.RESTORE_FILE.exists())

    def test_restore_file_is_saved_before_asking_to_clear(self):
        def check_then_cancel():
            state = js.load_json(js.RESTORE_FILE, None)
            self.assertEqual(len(state["songs"]), 6)
            self.assertFalse(state["cleared"])
            return "q"

        with self.assertRaises(SystemExit):
            self.run_main(*self.tag_all_then(check_then_cancel))

    def test_warns_until_queue_is_cleared(self):
        # Forgets to clear, warned, presses Enter again, warned, then clears
        out = self.run_main(*self.tag_all_then("", "", self.clear_queue))
        self.assertEqual(out.count("Warning: 6 of these songs still look queued"), 2)
        self.assertEqual(len(self.sp.added), 6)

    def test_go_continues_and_q_stops_at_warning(self):
        with self.assertRaises(SystemExit):
            self.run_main(*self.tag_all_then("", "q"))
        self.assertEqual(self.sp.added, [])
        self.run_main(*self.tag_all_then("", "go"))
        self.assertEqual(len(self.sp.added), 6)

    def test_song_that_started_while_clearing_is_not_re_added(self):
        def clear_and_skip_ahead():
            self.sp.clear()
            self.sp.current = track("t3")
            return ""

        out = self.run_main(*self.tag_all_then(clear_and_skip_ahead))
        self.assertIn("started playing meanwhile", out)
        self.assertCountEqual(self.sp.added, ["t0", "t1", "t2", "t4", "t5"])

    def test_queue_cap_warning(self):
        self.sp.items = [track(f"t{i}") for i in range(js.QUEUE_API_CAP)]
        with self.assertRaises(SystemExit):
            self.run_main("q")
        self.assertIn(f"Spotify only shows the next {js.QUEUE_API_CAP} songs", self.printed)

    def test_offers_resume_before_reading_queue(self):
        js.save_json(js.RESTORE_FILE, {"songs": songs_for({"Alice": 1, "Ben": 1}),
                                       "next": 0, "cleared": True, "failed": []})
        self.run_main("y")
        self.assertEqual(self.sp.added, ["Alice-0", "Ben-0"])


# ---------------------------------------------------------------- sign-in

class ConnectTest(StateDirTest):
    """connect() with a fake spotipy, so no browser or network is used."""

    def setUp(self):
        super().setUp()
        self.auth_calls = []
        test = self

        class FakePKCE:
            def __init__(self, **kwargs):
                test.auth_calls.append(kwargs)
                self.cache_handler = kwargs["cache_handler"]

            def validate_token(self, token):
                return token

            def get_access_token(self):
                pass

        class FakeCache:
            def __init__(self, cache_path):
                self.cache_path = cache_path

            def get_cached_token(self):
                p = Path(self.cache_path)
                return json.loads(p.read_text()) if p.exists() else None

        spotipy = types.ModuleType("spotipy")
        spotipy.Spotify = lambda auth_manager, **kw: auth_manager
        oauth2 = types.ModuleType("spotipy.oauth2")
        oauth2.SpotifyPKCE = FakePKCE
        cache = types.ModuleType("spotipy.cache_handler")
        cache.CacheFileHandler = FakeCache
        for patch in (mock.patch.dict(sys.modules, {"spotipy": spotipy,
                                                    "spotipy.oauth2": oauth2,
                                                    "spotipy.cache_handler": cache}),
                      mock.patch.dict(os.environ)):
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("SPOTIFY_CLIENT_ID", None)

    def test_signs_in_once_then_remembers(self):
        _, out = self.run_quietly(js.connect, inputs=Inputs("abc123"))
        self.assertEqual(js.load_json(js.CONFIG_FILE, {}), {"client_id": "abc123"})
        self.assertEqual(self.auth_calls[0]["redirect_uri"], "http://127.0.0.1:8888/callback")
        self.assertIn("Opening your browser", out)

        js.save_json(js.TOKEN_FILE, {"access_token": "x"})  # what spotipy caches
        inputs = Inputs()
        _, out = self.run_quietly(js.connect, inputs=inputs)
        self.assertEqual(inputs.prompts, [])
        self.assertEqual(self.auth_calls[1]["client_id"], "abc123")
        self.assertNotIn("Opening your browser", out)

    def test_exits_without_client_id_or_spotipy(self):
        with self.assertRaises(SystemExit):
            self.run_quietly(js.connect, inputs=Inputs(""))
        self.assertFalse(js.CONFIG_FILE.exists())
        with mock.patch.dict(sys.modules, {"spotipy": None}):
            with self.assertRaises(SystemExit) as e:
                js.connect()
        self.assertIn("pip install spotipy", str(e.exception.code))


# ---------------------------------------------------------------- command line

class DemoTest(unittest.TestCase):

    def test_demo_runs_without_spotify(self):
        with tempfile.TemporaryDirectory() as home:
            result = subprocess.run(
                [sys.executable, str(HERE / "jam_shuffle.py"), "--demo"],
                capture_output=True, text=True, timeout=30,
                env={**os.environ, "JAM_SHUFFLE_HOME": home})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Spec example", result.stdout)
            self.assertEqual(os.listdir(home), [])  # demo writes no state


if __name__ == "__main__":
    unittest.main()
