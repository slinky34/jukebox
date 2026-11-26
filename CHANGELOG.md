# Changelog

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
