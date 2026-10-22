# Spotify Jam Queue Shuffler

Reshuffles the songs waiting in a Spotify Jam so participants take turns, and no one person's songs play back-to-back.

> **Status:** tested only against a simulated Spotify so far, not a real account. Expect rough edges on a real Jam.

## What you need

- Python 3.8 or newer. The commands below are for Mac (Homebrew Python is fine) and Linux.
- A Spotify **Premium** account. This is the Jam host's account and the only one that signs in.
- Your own free Spotify developer app (step 1 below). There's no shared app to sign in to.
- About 5 minutes for the one-time setup

## One-time setup

### 1. Create a Spotify developer app

1. Go to https://developer.spotify.com/dashboard and sign in with your Premium account.
2. Click **Create app**.
   - **App name / description:** anything you like.
   - **Redirect URI:** `http://127.0.0.1:8888/callback`. Type it exactly like this, and click **Add**.
   - **Which API/SDKs are you planning to use?** Tick **Web API**.
3. Save, then open the app's **Settings** and copy its **Client ID**. You'll paste it on the first run. You don't need the client secret.

### 2. Install the script

In Terminal:

```bash
git clone https://github.com/mcctm/spotify-jam-shuffle.git
cd spotify-jam-shuffle
python3 -m venv .venv
source .venv/bin/activate
pip install spotipy
```

Each time you open a new Terminal window, run `source .venv/bin/activate` again before using the script.

## Running it

1. On your account, **start the Jam and play a song**. The script stops if nothing is playing.
2. Let people add songs, then run:

   ```bash
   python jam_shuffle.py
   ```

3. **First run only:** paste your Client ID. A browser window opens. Sign in, click **Agree**, and go back to Terminal, where the script carries on. Your Client ID and sign-in are saved, so you won't be asked again.
4. **Tag who added each song.** Spotify doesn't tell scripts who queued a song, so the script lists the waiting songs and asks for each one. To see who added a song, look at the queue in the Spotify app: each Jam song shows the profile picture of the person who added it.

   | Type | Meaning |
   |------|---------|
   | `Enter` | Accept the suggestion in brackets (remembered from earlier runs) |
   | a number | The person with that number in the list shown |
   | a name | A new person (they're added to the list) |
   | `.` | This song and everything after it came from autoplay or a playlist, not the Jam. Stop here. |
   | `q` | Cancel. Nothing is changed. |

   Spotify's list mixes Jam songs with upcoming autoplay/playlist songs, so press `.` at the first song nobody in the Jam added.

   On the first run you type each person's name once. After that they're numbered in a list, and songs you've tagged before are remembered, so later runs are mostly Enter.
5. **Check the new order** the script prints. If it looks wrong, type `q` at the next prompt. Nothing has been changed yet.
6. **Clear the queue in the Spotify app** (open the queue, then **Clear queue**). Leave the current song playing. Back in Terminal, press **Enter**, or type `q` to cancel and leave the queue as it was.
   - The script then checks the queue. If songs still show as queued, it warns you. Clear them and press Enter to check again, type `go` to continue anyway (those songs may play twice), or type `q` to stop.
   - Ask the Jam to hold off adding songs during a run. Songs added after the script reads the queue are removed when you clear it and aren't added back.
7. The script adds the songs back one by one, showing progress, then tells you how many were added and lists any that failed so you can add them by hand.

### Try it without Spotify

```bash
python jam_shuffle.py --demo
```

This shows the shuffle on sample data. It doesn't need spotipy or a Spotify account.

## How the shuffle works

- No two songs in a row come from the same person whenever the mix allows it.
- If one person added more songs than everyone else combined (plus one), some repeats can't be avoided. Their songs are spread evenly through the queue instead of bunched at the end.
- Each person's own songs are shuffled too, and running it twice can give different orders.

## If something goes wrong

The shuffled list is saved to `~/.jam_shuffle/restore.json` before you clear the queue, and progress is saved after every song is added back. If the script stops partway (an error, lost connection, closed Terminal), run it again. It asks:

- `Enter` / `y`: finish adding the remaining songs
- `discard`: forget them and start fresh
- `n`: leave it for later

Before adding anything back, it checks the Spotify queue. If the songs are still there (for example, it stopped before you cleared the queue), it warns you, the same way as during a normal run, so nothing gets added twice.

## Good to know

- **About 20 songs max.** Spotify only shows scripts roughly the next 20 songs. Songs beyond that aren't shuffled and stay where they are. The script warns you when this happens.
- **Re-added songs show you as the adder** in the Spotify app. The script remembers the real names, so later shuffles still work.
- **The current song is never touched.** It isn't removed or re-added.

## Saved files

Everything lives in `~/.jam_shuffle/`. Delete the folder to start over completely.

| File | What it holds |
|------|---------|
| `config.json` | Your Spotify Client ID |
| `token.json` | Your sign-in (refreshed automatically) |
| `tags.json` | Who added each song, remembered across runs |
| `restore.json` | Songs still to add back after an interrupted run (deleted when done) |

Optional environment variables: `SPOTIFY_CLIENT_ID` overrides the saved Client ID, and `JAM_SHUFFLE_HOME` uses a different folder instead of `~/.jam_shuffle`.

## Troubleshooting

- **"INVALID_CLIENT: Invalid redirect URI" in the browser:** the Redirect URI in your developer app doesn't exactly match `http://127.0.0.1:8888/callback`. Fix it in the app's Settings.
- **"This script needs spotipy":** activate the virtual environment (`source .venv/bin/activate`) or run `pip install spotipy`.
- **Wrong Client ID saved:** delete `~/.jam_shuffle/config.json` and `~/.jam_shuffle/token.json`, then run again.
- **Songs fail to add:** Spotify needs an active device. Keep the Spotify app open and playing.

## Running the tests

```bash
python3 -m unittest -v
```

The tests use a fake Spotify, so they need no account, no network, and no spotipy.
