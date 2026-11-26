"""Reading playlists from Spotify, Apple Music and Deezer, and looking songs up on Deezer.

Only public playlists. Requests only ever go to the services' own hosts, built from the IDs
in the pasted link (never to the link itself), so a pasted URL can't make the server fetch
anything else.

Each track comes back as {title, artist, album, duration (ms), link}.
"""
from __future__ import annotations

import base64
import json
import re
import time
from urllib.parse import quote

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
TIMEOUT = 20


class ImportError_(Exception):
    """Something the person importing should be told (bad link, private playlist, ...)."""


def parse_link(url: str):
    """('spotify', id) | ('apple', (storefront, id)) | ('deezer', id). Raises ImportError_."""
    url = (url or "").strip()
    m = re.search(r"(?:open\.spotify\.com/(?:intl-[\w-]+/)?(?:embed/)?playlist/|spotify:playlist:)([A-Za-z0-9]{10,40})", url)
    if m:
        return "spotify", m.group(1)
    m = re.search(r"music\.apple\.com/([a-z]{2})/playlist/(?:[^/?#]+/)?(pl\.[A-Za-z0-9.-]{6,80})", url)
    if m:
        return "apple", (m.group(1), m.group(2))
    m = re.search(r"deezer\.com/(?:[a-z]{2}/)?playlist/(\d{1,20})", url)
    if m:
        return "deezer", m.group(1)
    if re.search(r"deezer\.page\.link|link\.deezer\.com", url):
        raise ImportError_("That's a Deezer share link. Open it, then copy the address from the "
                           "browser (it looks like deezer.com/playlist/123...).")
    raise ImportError_("Paste a link to a public Spotify, Apple Music or Deezer playlist.")


def fetch_playlist(url: str, cfg: dict) -> dict:
    """{name, source, tracks, note}. `note` says when only part of the playlist could be read."""
    kind, ident = parse_link(url)
    if kind == "spotify":
        return _spotify(ident, cfg)
    if kind == "apple":
        return _apple(*ident)
    return _deezer_playlist(ident)


def _get(url, **kw):
    kw.setdefault("timeout", TIMEOUT)
    headers = {"User-Agent": UA, "Accept-Language": "en"}
    headers.update(kw.pop("headers", {}))
    r = requests.get(url, headers=headers, **kw)
    if r.status_code == 404:
        raise ImportError_("That playlist doesn't exist or isn't public.")
    r.raise_for_status()
    if "charset" not in r.headers.get("Content-Type", "").lower():
        r.encoding = "utf-8"  # Apple's pages don't say; requests would guess Latin-1
    return r


# ---- Spotify -----------------------------------------------------------------

_sp_token = {"value": None, "until": 0}


def _spotify_token(cfg):
    cid, secret = cfg.get("spotify_client_id"), cfg.get("spotify_client_secret")
    if not cid or not secret:
        return None
    if _sp_token["value"] and time.time() < _sp_token["until"] - 60:
        return _sp_token["value"]
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    r = requests.post("https://accounts.spotify.com/api/token", data={"grant_type": "client_credentials"},
                      headers={"Authorization": f"Basic {auth}"}, timeout=TIMEOUT)
    if r.status_code in (400, 401):
        raise ImportError_("Spotify didn't accept SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET.")
    r.raise_for_status()
    d = r.json()
    _sp_token.update(value=d["access_token"], until=time.time() + d.get("expires_in", 3600))
    return _sp_token["value"]


def _spotify(pid, cfg):
    token = _spotify_token(cfg)
    if token:
        try:
            return _spotify_api(pid, token)
        except ImportError_:
            raise
        except requests.HTTPError:
            pass  # e.g. Spotify's own editorial playlists, which apps can't read: try the page
    return _spotify_embed(pid, have_keys=bool(token))


def _spotify_api(pid, token):
    h = {"Authorization": f"Bearer {token}"}
    meta = _get(f"https://api.spotify.com/v1/playlists/{pid}?fields=name", headers=h).json()
    tracks = []
    url = (f"https://api.spotify.com/v1/playlists/{pid}/tracks?limit=100&additional_types=track"
           f"&fields=next,items(track(name,duration_ms,type,artists(name),album(name),external_urls))")
    while url and len(tracks) < 5000:
        d = _get(url, headers=h).json()
        for it in d.get("items", []):
            t = it.get("track") or {}
            if t.get("type") != "track" or not t.get("name"):
                continue
            tracks.append({"title": t["name"], "artist": ", ".join(a["name"] for a in t.get("artists", [])),
                           "album": (t.get("album") or {}).get("name", ""), "duration": t.get("duration_ms") or 0,
                           "link": None})
        url = d.get("next")
    return {"name": meta.get("name") or "Spotify playlist", "source": "Spotify", "tracks": tracks, "note": None}


def _spotify_embed(pid, have_keys=False):
    html = _get(f"https://open.spotify.com/embed/playlist/{pid}").text
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    try:
        ent = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except Exception:
        raise ImportError_("Couldn't read that Spotify playlist. Is it public?")
    tracks = [{"title": t.get("title", ""), "artist": (t.get("subtitle") or "").replace(" ", " "),
               "album": "", "duration": t.get("duration") or 0, "link": None}
              for t in ent.get("trackList", []) if t.get("title")]
    note = None
    if len(tracks) >= 100:
        note = ("Spotify only shows the first 100 songs without an app key. "
                + ("Its API refused this playlist (Spotify's own playlists can't be read by apps)."
                   if have_keys else "Add SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET to get all of them."))
    return {"name": ent.get("name") or "Spotify playlist", "source": "Spotify", "tracks": tracks, "note": note}


# ---- Apple Music ---------------------------------------------------------------

def _apple(storefront, pid):
    html = _get(f"https://music.apple.com/{storefront}/playlist/{quote(pid)}").text
    m = re.search(r'<script[^>]*id="serialized-server-data"[^>]*>(.*?)</script>', html, re.S)
    try:
        sections = json.loads(m.group(1))["data"][0]["data"]["sections"]
    except Exception:
        raise ImportError_("Couldn't read that Apple Music playlist. Is it public?")
    name, tracks = "Apple Music playlist", []
    for sec in sections:
        kind = sec.get("itemKind")
        if kind == "containerDetailHeaderLockup" and sec.get("items"):
            name = sec["items"][0].get("title") or name
        elif kind == "trackLockup":
            for it in sec.get("items", []):
                if not it.get("title"):
                    continue
                tracks.append({
                    "title": it["title"],
                    "artist": ", ".join(a.get("title", "") for a in it.get("subtitleLinks") or []),
                    "album": ((it.get("tertiaryLinks") or [{}])[0]).get("title", ""),
                    "duration": it.get("duration") or 0, "link": None})
    if not tracks:
        raise ImportError_("That Apple Music playlist looks empty, or isn't public.")
    note = "Apple Music only shows the first 100 songs of a playlist." if len(tracks) >= 100 else None
    return {"name": name, "source": "Apple Music", "tracks": tracks, "note": note}


# ---- Deezer --------------------------------------------------------------------

def _deezer_track(t):
    return {"title": t.get("title", ""), "artist": (t.get("artist") or {}).get("name", ""),
            "album": (t.get("album") or {}).get("title", ""), "duration": (t.get("duration") or 0) * 1000,
            "link": t.get("link")}


def _deezer_playlist(pid):
    d = _get(f"https://api.deezer.com/playlist/{pid}").json()
    if "error" in d:
        raise ImportError_("That Deezer playlist doesn't exist or isn't public.")
    tracks = [_deezer_track(t) for t in (d.get("tracks") or {}).get("data", [])]
    url = (d.get("tracks") or {}).get("next")
    while url and url.startswith("https://api.deezer.com/") and len(tracks) < 5000:
        page = _get(url).json()
        tracks += [_deezer_track(t) for t in page.get("data", [])]
        url = page.get("next")
    return {"name": d.get("title") or "Deezer playlist", "source": "Deezer", "tracks": tracks, "note": None}


def deezer_search(q: str, limit: int = 15) -> list:
    """Songs from Deezer's catalogue (for finding things that aren't in Plex yet)."""
    d = _get("https://api.deezer.com/search", params={"q": q[:200], "limit": limit}).json()
    return [_deezer_track(t) for t in d.get("data", []) if t.get("type", "track") == "track"]


def deezer_lookup(title: str, artist: str):
    """Fill in album and Deezer link for a song, or None if Deezer doesn't know it."""
    from .matching import best_match, clean_title

    q = f'artist:"{artist}" track:"{clean_title(title)}"' if artist else clean_title(title)
    try:
        found = deezer_search(q, 10) or deezer_search(f"{artist} {clean_title(title)}", 10)
    except requests.RequestException:
        return None
    return best_match({"title": title, "artist": artist}, found)
