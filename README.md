# Jukebox: shared Plex music playlists

Playlists you build together with people on your Plex server. You choose who's in each
playlist, and it's copied into each of their own Plex accounts (shown as `👥 Playlist name`),
so it plays in Plex, Plexamp and any other Plex app. When someone changes it, everyone's
copy updates.

There are two ways to edit:

- **In the Jukebox web page.** Search, add songs or whole albums, drag to reorder, remove.
  Everyone with the page open sees the change straight away, and their Plex copies follow
  within a second or two.
- **In Plex or Plexamp.** Songs added to or removed from your `👥` copy are picked up on the
  next check (every 20 seconds by default) and passed on to everyone. A song added in
  Plex keeps the spot it was added at.

It runs in Docker next to Plex. Your playlists are kept in a Docker volume, so updating
never touches them.

## What you need

- **Docker** on the Plex machine (on Windows: Docker Desktop, with "Start Docker Desktop
  when you sign in" turned on in its settings).
- The **server owner's Plex token** (see below).
- Everyone else just needs a Plex account you already share the server with.

## Set up

1. Make a `jukebox` folder on the Plex machine and put two files from this repository in
   it: [`docker-compose.yml`](docker-compose.yml) and [`.env.example`](.env.example)
   (open each, then **Download raw file**). Or download the whole repository as a zip.
2. Copy `.env.example` to `.env` (exactly that name) and paste your token after `PLEX_TOKEN=`.
3. Open a terminal in the folder (on Windows: right-click the folder > **Open in Terminal**)
   and run:
   ```
   docker compose up -d
   ```
   The first time takes a minute while it downloads.
4. Open `http://<plex-machine-ip>:8765` on any device on your network, and sign in with Plex.

That's it. Docker starts Jukebox again after a reboot or a crash. To see what it's doing:
`docker compose logs -f` (Ctrl+C to stop watching).

If Plex is on a different machine than Docker, set `PLEX_URL` in `.env`, e.g.
`PLEX_URL=http://192.168.1.20:32400`. The other settings in `.env` are explained there.

### Updating

From a terminal in the folder:
```
docker compose pull
docker compose up -d
```
That's all: no files to copy. Your playlists, sharing and sign-in settings stay as they are.
What changed in each version is in [CHANGELOG.md](CHANGELOG.md) and on the
[Releases](https://github.com/slinky34/jukebox/releases) page.

To stay on one version instead of always taking the newest, change `:latest` in
`docker-compose.yml` to a version number, e.g. `ghcr.io/slinky34/jukebox:0.1.0`.

**Don't** run `docker compose down -v` or delete the `jukebox-data` volume: that is where
your playlists live. Plain `docker compose down` is fine.

To back up: `docker compose cp jukebox:/data/jukebox.db jukebox-backup.db`

### Moving over from the Windows installer

1. Run `uninstall-windows.bat` first, so the old copy stops. Two copies running at once
   would fight over everyone's playlists, and the old one holds port 8765.
2. Do steps 2 and 3 of "Set up" above.

Your shared playlists come back by themselves: when Jukebox starts with an empty database,
it looks through everyone's Plex for `👥` playlists and turns them back into shared
playlists, with the same songs and the same people, and keeps everyone's existing copies
(no duplicates). The log lists what it found.

What that can't bring back: playlists that had no songs, people who had deleted their copy
from Plex, and who added each song. If you still have the old `data` folder and want those
too, bring the old database in **before** step 3, from a terminal in the jukebox folder:
```
docker compose run --rm --no-deps --user root -v "${PWD}/data:/old" jukebox sh -c "cp /old/* /data/ && chown -R jukebox /data"
```
then `docker compose up -d`.

The server owner can also run the same search any time from the web page: **+** >
**…or bring back shared playlists from an earlier install**. It only adds playlists that
aren't there yet.

### Finding your Plex token

The easiest way: in Plex Web, open any song, choose **⋯ → Get Info → View XML**. The URL
that opens ends in `X-Plex-Token=xxxxxxxx`. That last part is the token.

Or read it from the server's settings:
- Windows: Registry Editor → `HKEY_CURRENT_USER\Software\Plex, Inc.\Plex Media Server` → `PlexOnlineToken`
- macOS: `defaults read com.plexapp.plexmediaserver PlexOnlineToken`
- Linux: `PlexOnlineToken` in `/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Preferences.xml`

Keep the token private. It gives full control of your Plex account. It stays in
`.env` (or `config.toml`) on the Plex machine and is never sent to the browser.

## Sharing a playlist you already have

Click **+** next to "Shared playlists", then **…or share one of your Plex playlists**. You'll
see your own music playlists from Plex. Pick one, then choose who it's for.

- **Normally your playlist itself becomes the shared one.** It's renamed with `👥` in
  front, keeps its place in Plex, and from then on it shows everyone's changes. Duplicate
  songs in it are removed, because a shared playlist holds each song once.
- **Tick "Keep my original as well"** if you'd rather leave yours alone. The shared
  playlist is then a separate copy, and later changes to your original don't carry over.
- **Smart playlists** (rule-based ones, like Plexamp's liked tracks) are always shared as
  a copy of the songs in them right now. Rules can't be shared.
- If you later leave or delete the shared playlist, your `👥` copy is removed from Plex.
  That includes one that started out as your own playlist. Tick "Keep my original" if you
  want to be sure you keep it.

Anyone signed in can do this with their own playlists.

## Who sees what

**Each playlist has its own list of people.** A new playlist starts with just you, and the
"+ Add people" button under its name lets you pick who else is in it. Only those people:
- see it in the web page
- get a copy in their Plex
- can edit it, including adding or removing people

Click the × on someone's name to remove them. Their copy is deleted from their Plex on the
next sync. You can remove yourself the same way to leave a playlist. A playlist always keeps
at least one person.

**Who can sign in at all:** you (the owner) and anyone you share your Plex server with.
To keep some of those people out of Jukebox entirely, list the ones who *are* allowed in
`ALLOWED_USERS` in `.env`, separated by commas. If you stop sharing the server with someone, they lose
access to Jukebox and their copies stop updating.

Managed Home users (ones without their own Plex login) can be added to playlists and get
copies, but they can't sign in to the web page.

## Good to know

- **Reordering happens in the web page.** Moving songs around inside Plex gets put back on the
  next check. Adds and removes made in Plex do carry over.
- **Deleting your `👥` copy in Plex doesn't leave the playlist.** It's recreated on the next
  check, and the shared playlist isn't affected. Use `ALLOWED_USERS` to leave someone out.
- **Removing a large part of a playlist in one go inside Plex is ignored as a probable glitch.**
  "Large" means more than half the songs, and more than 5. This protects against Plex briefly
  returning an empty list. Do big clear-outs in the web page.
- **Songs from a library a member can't see** are left out of that member's copy only. Their
  chip in the web page shows how many songs they're missing.
- Drag-to-reorder needs a mouse or trackpad. On phones you can add and remove songs, but not
  reorder them.
- Want web-only editing? Set `PULL_FROM_PLEX=false` in `.env`. Changes made inside Plex then get undone.

## Without Docker

Jukebox is a plain Python program too (Python 3.9 or newer). Copy `config.example.toml` to
`config.toml`, fill in `plex_token`, then run `start.bat` (Windows) or `./start.sh`
(macOS/Linux). The first start installs what it needs into `.venv`. Data goes in the `data`
folder next to it: keep that folder when you update. `install-windows.bat` sets it up to
start with Windows.

## Using it away from home

Out of the box, Jukebox can only be reached on your home network. Adding songs from Plexamp
works from anywhere, because that goes through Plex. To open the web page from outside your
network, put it behind something like Tailscale, or a reverse proxy with HTTPS. If you use
a proxy, set `PUBLIC_URL` in `.env` to the outside address.

## Files

- `.env`: your settings (Docker). `config.toml` without Docker.
- In the `jukebox-data` volume (or the `data` folder without Docker):
  - `jukebox.db`: the shared playlists. Back this file up.
  - `session.key`, `client.id`: created automatically.

## Developing

```
python run.py --demo                  # try the UI without Plex (fake server, four pretend users)
docker compose run --rm --no-deps jukebox python run.py --check   # test the Plex connection
pip install httpx && python -m unittest discover tests            # includes web security tests
```
