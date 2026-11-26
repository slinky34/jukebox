"""Sign in with Plex (the PIN flow plex.tv uses for third-party apps).

The app only uses the token to learn who the person is; it never stores it.
"""
from __future__ import annotations

import time
from urllib.parse import urlencode

import requests

PRODUCT = "Jukebox (shared playlists)"


def _headers(client_id, token=None):
    h = {"Accept": "application/json", "X-Plex-Product": PRODUCT, "X-Plex-Client-Identifier": client_id}
    if token:
        h["X-Plex-Token"] = token
    return h


def create_pin(client_id):
    r = requests.post("https://plex.tv/api/v2/pins", params={"strong": "true"},
                      headers=_headers(client_id), timeout=15)
    r.raise_for_status()
    d = r.json()
    return {"id": d["id"], "code": d["code"]}


def auth_url(client_id, code, forward_url):
    return "https://app.plex.tv/auth#?" + urlencode({
        "clientID": client_id,
        "code": code,
        "context[device][product]": PRODUCT,
        "forwardUrl": forward_url,
    })


def check_pin(client_id, pin_id, code, tries=4):
    for i in range(tries):
        r = requests.get(f"https://plex.tv/api/v2/pins/{pin_id}", params={"code": code},
                         headers=_headers(client_id), timeout=15)
        if r.ok and r.json().get("authToken"):
            return r.json()["authToken"]
        time.sleep(1)
    return None


def user_info(client_id, token):
    r = requests.get("https://plex.tv/api/v2/user", headers=_headers(client_id, token), timeout=15)
    r.raise_for_status()
    return r.json()
