# Changelog

## 0.3.0 - 2026-11-25

- **Import playlists from Spotify, Apple Music and Deezer.** Paste a public playlist link;
  Jukebox finds the songs in your Plex library and makes a shared playlist from them.
  Spotify needs a free app key for playlists over 100 songs.
- **Waiting songs.** Songs not in your Plex yet stay listed under the playlist and are
  added in their place as soon as they appear in Plex (checked every 10 minutes, or
  **Check Plex now**).
- **Download missing songs with deemix or Lidarr.** **Download** / **Download all** on
  waiting songs. Only the server owner by default (`DOWNLOADS=everyone` to let others).
- **Search Deezer** from the song search for things that aren't in Plex, and add or
  download them.
- Security: pasted links are only used to read the playlist from Spotify, Apple Music or
  Deezer themselves; deemix and Lidarr keys stay on the server; only real Deezer track
  links are passed to deemix.

## 0.2.0 - 2026-11-25

- **Clean up 👥 playlists (server owner).** Under **+ > …or clean up 👥 playlists**:
  - **Leftovers:** 👥 playlists still in people's Plex that Jukebox isn't keeping in
    sync, for example one deleted while two copies of Jukebox were running. **Delete**
    removes every copy from everyone's Plex; **Bring back** turns it into a shared
    playlist again. Your own playlists and the shared playlists Jukebox manages are never
    touched.
  - **Shared playlists you're not in**, such as ones rebuilt from other people's copies.
    **Join** to see and edit one, or **Delete** it for everyone.

## 0.1.0 - 2026-11-25

First published release, now as a Docker image: `ghcr.io/slinky34/jukebox`.

- **Runs with docker compose.** Settings go in `.env`; playlists are kept in the
  `jukebox-data` volume, so updating (`docker compose pull && docker compose up -d`)
  never touches them.
- **Brings back shared playlists after a fresh install.** On an empty database Jukebox
  finds the 👥 playlists already in everyone's Plex and rebuilds them with the same songs
  and people, keeping each person's existing copy. The server owner can also run this
  from the web page.
- **Share a playlist you already have** in Plex, by turning it into a shared one or
  sharing a copy.
- **Pick who each playlist is shared with.**
- Security: search, album lookups and cover art now use each person's own Plex access,
  so nobody can see items from libraries you haven't shared with them. Requests from
  other websites are refused, the page sends standard security headers, and Plex tokens
  are kept out of URLs, error messages and logs.
