"""Deciding whether a track in Plex is the same song as one from Spotify, Apple Music, etc.

Titles differ in small ways between services ("Song (feat. X)", "Song - Remastered 2011",
"Song [From the Movie]"), so both sides are normalised before comparing. A match needs a
close title, a matching artist, and (when both sides know it) a similar length.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

# Bits of a title that services add or leave out.
_EXTRA = re.compile(
    r"""\s*[\(\[]\s*(feat\.?|ft\.?|featuring|with|from|prod\.?)\b[^\)\]]*[\)\]]   # (feat. X), [From Y]
      | \s+-\s+.*\b(remaster(ed)?|version|edit|mix|mono|stereo|live|single|radio)\b.*$  # - Remastered 2011
      | \s*[\(\[][^\)\]]*\b(remaster(ed)?|version|edit|mono|stereo|deluxe|bonus)\b[^\)\]]*[\)\]]
      | \s+(feat\.?|ft\.?|featuring)\s.*$                                  # Song feat. X
    """, re.I | re.X)
_ARTIST_SPLIT = re.compile(r"\s*(?:,|&|\band\b|\bx\b|\bfeat\.?|\bft\.?|\bfeaturing\b|\bwith\b|;|/|\+)\s*", re.I)


def fold(text: str) -> str:
    """Lowercase, no accents, no punctuation, single spaces."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("&", " and ").replace("’", "'")
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_title(title: str) -> str:
    """The title without feat./remaster/version decorations (for searching and comparing)."""
    prev, t = None, title or ""
    while prev != t:  # strip repeatedly: "Song (feat. X) - Remastered"
        prev, t = t, _EXTRA.sub("", t).strip()
    return t or (title or "")


def artists(text: str) -> set:
    return {fold(a) for a in _ARTIST_SPLIT.split(text or "") if fold(a)}


def similar(a: str, b: str) -> float:
    a, b = fold(a), fold(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def score(want: dict, have: dict) -> float:
    """0..1: how sure we are that `have` (a Plex track) is `want` (an outside track)."""
    t = max(similar(clean_title(want["title"]), clean_title(have["title"])),
            similar(want["title"], have["title"]))
    if t < 0.8:
        return 0.0
    wa, ha = artists(want.get("artist", "")), artists(have.get("artist", ""))
    if wa and ha:
        if wa & ha:
            a = 1.0
        else:  # "The Weeknd" vs "Weeknd", typos
            a = max(similar(x, y) for x in wa for y in ha)
            if a < 0.75:
                return 0.0
    else:
        a = 0.6  # one side has no artist: rely on the title
    d = 0.5
    wd, hd = want.get("duration") or 0, have.get("duration") or 0
    if wd and hd:
        gap = abs(wd - hd) / 1000
        if gap > 45:  # a live, extended or different recording
            return 0.0
        d = 1.0 if gap <= 3 else 0.7 if gap <= 10 else 0.3
    return 0.55 * t + 0.3 * a + 0.15 * d


MATCH = 0.82  # anything below this counts as "not in Plex"


def best_match(want: dict, candidates: list):
    """The best Plex track for `want` among `candidates`, or None."""
    best, best_score = None, 0.0
    for c in candidates:
        s = score(want, c)
        if s > best_score:
            best, best_score = c, s
    return best if best_score >= MATCH else None


def loose_score(want: dict, have: dict) -> float:
    """Like score(), but never rules anything out: for ranking suggestions a person picks from."""
    t = max(similar(clean_title(want["title"]), clean_title(have["title"])),
            similar(want["title"], have["title"]))
    wa, ha = artists(want.get("artist", "")), artists(have.get("artist", ""))
    if wa and ha:
        a = 1.0 if wa & ha else max(similar(x, y) for x in wa for y in ha)
    else:
        a = 0.5
    d = 0.5
    wd, hd = want.get("duration") or 0, have.get("duration") or 0
    if wd and hd:
        gap = abs(wd - hd) / 1000
        d = 1.0 if gap <= 3 else 0.7 if gap <= 10 else 0.4 if gap <= 45 else 0.0
    return 0.6 * t + 0.3 * a + 0.1 * d
