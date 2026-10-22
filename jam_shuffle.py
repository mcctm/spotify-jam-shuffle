#!/usr/bin/env python3
"""
Spotify Jam Queue Shuffler

Reshuffles the songs waiting in a Spotify Jam so participants take turns.
Run it on the Jam host's computer:  python3 jam_shuffle.py
Try the shuffle without Spotify:    python3 jam_shuffle.py --demo

Spotify's Web API doesn't say who queued a song, so the host tags each song
with a keypress. Tags are remembered, so re-runs mostly need just Enter.
"""

import json
import os
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

STATE_DIR = Path(os.environ.get("JAM_SHUFFLE_HOME", Path.home() / ".jam_shuffle"))
CONFIG_FILE = STATE_DIR / "config.json"      # Spotify client ID
TOKEN_FILE = STATE_DIR / "token.json"        # sign-in, refreshed automatically
TAGS_FILE = STATE_DIR / "tags.json"          # track URI -> who added it
RESTORE_FILE = STATE_DIR / "restore.json"    # songs still to re-add after a failure

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "user-read-playback-state user-modify-playback-state"
QUEUE_API_CAP = 20  # the queue endpoint only shows roughly this many upcoming songs


# ---------------------------------------------------------------- helpers

def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)  # atomic, so a crash never leaves a half-written file


def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        return "q"


def describe(item):
    """'Title — Artist' for a track or podcast episode."""
    name = item.get("name", "Unknown")
    if item.get("artists"):
        by = ", ".join(a["name"] for a in item["artists"])
    elif item.get("show"):
        by = item["show"].get("name", "")
    else:
        by = ""
    return f"{name} — {by}" if by else name


# ---------------------------------------------------------------- fair shuffle

def fair_shuffle(songs, rng=random):
    """
    songs: list of dicts with an 'added_by' key. Returns a new list where,
    whenever possible, no two neighbours were added by the same person.
    Each person's own songs are shuffled too. Duplicates stay separate.
    """
    groups = defaultdict(list)
    for s in songs:
        groups[s["added_by"]].append(s)
    for g in groups.values():
        rng.shuffle(g)

    total = len(songs)
    if total == 0:
        return []

    top, top_count = max(((p, len(g)) for p, g in groups.items()), key=lambda x: x[1])
    others = total - top_count

    if top_count > others + 1:
        # Repeats can't be avoided. Lay out everyone else's songs fairly, then
        # spread the dominant person's songs evenly across every gap so the
        # unavoidable repeats are scattered, not bunched at the end.
        rest = fair_shuffle([s for s in songs if s["added_by"] != top], rng)
        gaps = len(rest) + 1
        base, extra = divmod(top_count, gaps)
        sizes = [base + 1] * extra + [base] * (gaps - extra)
        rng.shuffle(sizes)
        mine = groups[top]
        out, i = [], 0
        for g in range(gaps):
            out.extend(mine[i:i + sizes[g]])
            i += sizes[g]
            if g < len(rest):
                out.append(rest[g])
        return out

    # No repeats needed: pick a random eligible person each step, weighted by
    # how many songs they have left, but only if the rest can still be laid
    # out without repeats.
    remaining = {p: len(g) for p, g in groups.items()}
    out, last = [], None
    while len(out) < total:
        options = []
        for p, c in remaining.items():
            if c == 0 or p == last:
                continue
            remaining[p] -= 1
            if _still_feasible(remaining, p):
                options.append(p)
            remaining[p] += 1
        if not options:  # can't happen given the check above; be safe anyway
            options = [p for p, c in remaining.items() if c > 0]
        pick = rng.choices(options, weights=[remaining[p] for p in options])[0]
        out.append(groups[pick].pop())
        remaining[pick] -= 1
        last = pick
    return out


def _still_feasible(remaining, last):
    left = sum(remaining.values())
    if left == 0:
        return True
    p, c = max(remaining.items(), key=lambda x: x[1])
    if p == last:
        # can't start the rest with `last`, so it needs one extra separator
        return c <= left - c
    return c <= left - c + 1 and remaining[last] <= left - remaining[last] + 1


# ---------------------------------------------------------------- Spotify

def connect():
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyPKCE
        from spotipy.cache_handler import CacheFileHandler
    except ImportError:
        sys.exit("This script needs spotipy. Install it with:  pip install spotipy")

    config = load_json(CONFIG_FILE, {})
    client_id = os.environ.get("SPOTIFY_CLIENT_ID") or config.get("client_id")
    if not client_id:
        print("First run: paste the Client ID of your Spotify developer app.")
        print(f"(Its Redirect URI must be set to {REDIRECT_URI} — see README.)")
        client_id = ask("Client ID: ")
        if not client_id or client_id == "q":
            sys.exit("No Client ID given. Nothing was changed.")
        config["client_id"] = client_id
        save_json(CONFIG_FILE, config)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    auth = SpotifyPKCE(
        client_id=client_id,
        redirect_uri=REDIRECT_URI,
        scope=SCOPES,
        cache_handler=CacheFileHandler(cache_path=str(TOKEN_FILE)),
        open_browser=True,
    )
    if not auth.validate_token(auth.cache_handler.get_cached_token()):
        print("Opening your browser to sign in to Spotify...")
    auth.get_access_token()  # opens the browser only when needed, then continues
    return spotipy.Spotify(auth_manager=auth, retries=3, status_retries=3)


def read_queue(sp):
    data = sp.queue() or {}
    return data.get("currently_playing"), data.get("queue") or []


def add_with_retry(sp, uri, attempts=3):
    for n in range(attempts):
        try:
            sp.add_to_queue(uri)
            return None
        except Exception as e:  # network blip, device asleep, etc.
            err = e
            if n + 1 < attempts:
                time.sleep(1.5 * (n + 1))
    return str(err)


def confirm_cleared(sp, songs, already_added=()):
    """
    Read the queue and, while any of `songs` still look queued, warn and let
    the host clear them and check again. Returns False if the host cancels.
    """
    while True:
        _, after = read_queue(sp)
        waiting = Counter(i["uri"] for i in after)
        waiting.subtract(s["uri"] for s in already_added)  # those belong there
        leftover = []
        for s in songs:
            if waiting[s["uri"]] > 0:
                waiting[s["uri"]] -= 1
                leftover.append(s)
        if not leftover:
            return True
        print(f"\nWarning: {len(leftover)} of these songs still look queued in Spotify:")
        for s in leftover[:5]:
            print(f"  - {s['title']}")
        print("If you go on, they may play twice.")
        answer = ask("Clear the queue and press Enter to check again, type 'go' to "
                     "continue anyway, or q to cancel: ").lower()
        if answer == "q":
            return False
        if answer == "go":
            return True


# ---------------------------------------------------------------- steps

def tag_songs(items, tags):
    """Ask who added each song. Returns songs up to where the host typed '.'."""
    people = list(dict.fromkeys(tags.values()))  # known names, first-seen order
    print("\nWho added each song? For each one:")
    print("  Enter = suggested name   number or new name = that person")
    print("  .     = this and everything after is NOT from the Jam (autoplay/playlist)")
    print("  q     = cancel without changing anything\n")

    songs, previous = [], None
    for i, item in enumerate(items, 1):
        suggested = tags.get(item["uri"]) or previous
        menu = "  ".join(f"{n}) {p}" for n, p in enumerate(people, 1))
        print(f"{i:>2}. {describe(item)}")
        if menu:
            print(f"    {menu}")
        while True:
            hint = f" [Enter = {suggested}]" if suggested else ""
            answer = ask(f"    added by{hint}: ")
            if answer == "q":
                return None
            if answer == ".":
                return songs
            if answer == "" and suggested:
                who = suggested
            elif answer.isdigit() and 1 <= int(answer) <= len(people):
                who = people[int(answer) - 1]
            elif answer and not answer.isdigit():
                who = answer
            else:
                print("    Type a number from the list, a name, '.' or 'q'.")
                continue
            break
        if who not in people:
            people.append(who)
        songs.append({"uri": item["uri"], "title": describe(item), "added_by": who})
        previous = who
    return songs


def restore(sp, state):
    """Add back songs from state['songs'] starting at state['next']."""
    songs = state["songs"]
    total = len(songs)
    failed = state.setdefault("failed", [])
    print(f"\nAdding {total - state['next']} songs back to the queue...")
    while state["next"] < total:
        song = songs[state["next"]]
        err = add_with_retry(sp, song["uri"])
        if err:
            failed.append({**song, "error": err})
            mark = "FAILED"
        else:
            mark = "ok"
        state["next"] += 1
        save_json(RESTORE_FILE, state)  # progress survives a crash
        print(f"  [{state['next']}/{total}] {mark:<6} {song['title']}  ({song['added_by']})")

    added = total - len(failed) - state.get("skipped", 0)
    print(f"\nDone. {added} song{'s' if added != 1 else ''} added in the new order.")
    if failed:
        print("These couldn't be added (add them in Spotify by hand):")
        for s in failed:
            print(f"  - {s['title']}  ({s['added_by']})  — {s['error']}")

    # Re-added songs now show the host as the adder, so remember the real names.
    tags = load_json(TAGS_FILE, {})
    for s in songs:
        tags[s["uri"]] = s["added_by"]
    save_json(TAGS_FILE, tags)
    RESTORE_FILE.unlink(missing_ok=True)


def offer_resume(sp):
    state = load_json(RESTORE_FILE, None)
    if not state:
        return False
    left = len(state["songs"]) - state["next"]
    print(f"A previous run stopped with {left} song(s) still to add back.")
    if state["next"] == 0 and not state.get("cleared"):
        print("(It stopped before you confirmed clearing the queue.)")
    answer = ask("Finish restoring them now? [Y/n/discard] ").lower()
    if answer in ("", "y", "yes"):
        # If the queue was never cleared, the songs are still there; adding
        # them now would queue them twice.
        done = state["songs"][:state["next"]]
        if not confirm_cleared(sp, state["songs"][state["next"]:], done):
            sys.exit("Left it for later. Run again to restore those songs.")
        restore(sp, state)
        return True
    if answer == "discard":
        RESTORE_FILE.unlink(missing_ok=True)
        print("Discarded. Starting fresh.\n")
        return False
    sys.exit("Left it for later. Run again to restore those songs.")


def main():
    if "--demo" in sys.argv:
        return demo()

    sp = connect()
    if offer_resume(sp):
        return

    # Story 2: read the queue
    current, items = read_queue(sp)
    if not current:
        sys.exit("Nothing is playing right now. Start the Jam and run this again.")
    print(f"\nNow playing (won't be touched): {describe(current)}")
    if len(items) < 2:
        sys.exit("The queue has fewer than two songs, so there's nothing to shuffle.")
    if len(items) >= QUEUE_API_CAP:
        print(f"Note: Spotify only shows the next {len(items)} songs. Any songs "
              "beyond those stay where they are and won't be shuffled.")

    tags = load_json(TAGS_FILE, {})
    songs = tag_songs(items, tags)
    if songs is None:
        sys.exit("Cancelled. Nothing was changed.")
    if len(songs) < 2:
        sys.exit("Fewer than two Jam songs, so there's nothing to shuffle.")

    # Story 3: fair order
    shuffled = fair_shuffle(songs)
    counts = Counter(s["added_by"] for s in songs)
    print("\nNew order:  (" + ", ".join(f"{p} {c}" for p, c in counts.items()) + ")")
    for i, s in enumerate(shuffled, 1):
        print(f"  {i:>2}. {s['added_by']:<12} {s['title']}")

    # Save before asking, so songs survive anything that happens from here (story 6)
    state = {"started_with": current["uri"], "songs": shuffled, "next": 0,
             "cleared": False, "failed": []}
    save_json(RESTORE_FILE, state)

    # Story 4: host clears the queue
    print("\nNow clear the queue in the Spotify app (Queue → Clear queue).")
    print("Leave the current song playing.")
    if ask("Press Enter once it's cleared, or type q to cancel: ").lower() == "q":
        RESTORE_FILE.unlink(missing_ok=True)
        sys.exit("Cancelled. Your queue was not touched.")
    state["cleared"] = True
    save_json(RESTORE_FILE, state)

    now, _ = read_queue(sp)
    # If a queued song started while the host was clearing, don't add it again.
    if now and now["uri"] != current["uri"]:
        for i, s in enumerate(shuffled):
            if s["uri"] == now["uri"]:
                print(f"'{s['title']}' started playing meanwhile, so it won't be re-added.")
                del shuffled[i]
                save_json(RESTORE_FILE, state)
                break
    if not confirm_cleared(sp, shuffled):
        sys.exit("Stopped. Run again to restore the songs if you already "
                 "cleared some of them.")

    # Story 5: add back
    restore(sp, state)


def demo():
    songs = ([{"uri": f"a{i}", "title": f"Alice song {i}", "added_by": "Alice"} for i in range(4)]
             + [{"uri": f"b{i}", "title": f"Ben song {i}", "added_by": "Ben"} for i in range(2)]
             + [{"uri": "c0", "title": "Cara song", "added_by": "Cara"}])
    print("Spec example (Alice 4, Ben 2, Cara 1):")
    for _ in range(3):
        print("  " + " → ".join(s["added_by"] for s in fair_shuffle(songs)))
    heavy = ([{"uri": f"a{i}", "title": "", "added_by": "Alice"} for i in range(8)]
             + [{"uri": "b", "title": "", "added_by": "Ben"},
                {"uri": "c", "title": "", "added_by": "Cara"}])
    print("Alice dominates (Alice 8, Ben 1, Cara 1) — repeats spread out:")
    for _ in range(3):
        print("  " + " → ".join(s["added_by"] for s in fair_shuffle(heavy)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        msg = "\nStopped."
        if RESTORE_FILE.exists():
            msg += " Run again to finish restoring your songs."
        sys.exit(msg)
