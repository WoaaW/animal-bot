"""Sign in with Discord, so only members of a guild see its data.

OAuth2 with the `identify` scope only: it tells us who the visitor is, and
membership is then checked with the bot's token (`Names.is_member`, cached),
on every API call, so someone who leaves the server loses access within the
cache delay. The session is a signed cookie (no table): the user's id, name
and avatar plus an expiry, HMAC-signed with the client secret.

Enabled when DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET are set; WEB_BASE_URL
(the public address, e.g. https://capybabot.example.ts.net) builds the
redirect URI, which must also be listed in the Developer Portal (OAuth2 →
Redirects) as <WEB_BASE_URL>/auth/callback."""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import urlencode

import aiohttp
from aiohttp import web

from .. import config  # noqa: F401  (loads .env before the reads below)
from .names import API, CDN, _default_avatar

CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "").strip()
BASE_URL = os.environ.get("WEB_BASE_URL", "").strip().rstrip("/")
ENABLED = bool(CLIENT_ID and CLIENT_SECRET)

REDIRECT_PATH = "/auth/callback"
SESSION_COOKIE = "session"
STATE_COOKIE = "oauth_state"
SESSION_SECONDS = 30 * 24 * 3600
STATE_SECONDS = 10 * 60


def redirect_uri(request: web.Request) -> str:
    base = BASE_URL or f"{request.scheme}://{request.host}"
    return base + REDIRECT_PATH


def _secure(request: web.Request) -> bool:
    return (BASE_URL or f"{request.scheme}://").startswith("https://")


def _sign(payload: bytes) -> str:
    key = hashlib.sha256(b"web-session:" + CLIENT_SECRET.encode()).digest()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _encode(data: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode())
    return f"{payload.decode()}.{_sign(payload)}"


def _decode(value: str) -> dict | None:
    payload, _, signature = value.rpartition(".")
    if not payload or not hmac.compare_digest(_sign(payload.encode()), signature):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload))
    except ValueError:
        return None
    return data if data.get("exp", 0) > time.time() else None


def current_user(request: web.Request) -> dict | None:
    """The signed-in visitor ({id, name, avatar}), or None."""
    value = request.cookies.get(SESSION_COOKIE)
    return _decode(value) if value else None


async def login(request: web.Request) -> web.Response:
    if not ENABLED:
        raise web.HTTPFound("/")
    state = secrets.token_urlsafe(24)
    query = urlencode({
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri(request),
        "scope": "identify",
        "state": state,
        "prompt": "none",  # no consent screen again once authorized
    })
    response = web.HTTPFound(f"https://discord.com/oauth2/authorize?{query}")
    response.set_cookie(STATE_COOKIE, state, max_age=STATE_SECONDS, httponly=True,
                        samesite="Lax", secure=_secure(request))
    raise response


async def callback(request: web.Request) -> web.Response:
    if not ENABLED:
        raise web.HTTPFound("/")
    state = request.query.get("state", "")
    expected = request.cookies.get(STATE_COOKIE, "")
    code = request.query.get("code")
    # Refused on Discord's page, or a forged / stale link: back to the start.
    if not code or not expected or not hmac.compare_digest(state, expected):
        raise web.HTTPFound("/?login=failed")

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.post(f"{API}/oauth2/token", data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(request),
        }) as response:
            if response.status != 200:
                raise web.HTTPFound("/?login=failed")
            token = (await response.json())["access_token"]
        async with session.get(f"{API}/users/@me", headers={"Authorization": f"Bearer {token}"}) as response:
            if response.status != 200:
                raise web.HTTPFound("/?login=failed")
            user = await response.json()

    user_id = int(user["id"])
    avatar = (f"{CDN}/avatars/{user_id}/{user['avatar']}.png?size=64"
              if user.get("avatar") else _default_avatar(user_id))
    session_value = _encode({
        "id": str(user_id),
        "name": user.get("global_name") or user["username"],
        "avatar": avatar,
        "exp": int(time.time()) + SESSION_SECONDS,
    })
    response = web.HTTPFound("/")
    response.set_cookie(SESSION_COOKIE, session_value, max_age=SESSION_SECONDS, httponly=True,
                        samesite="Lax", secure=_secure(request))
    response.del_cookie(STATE_COOKIE)
    raise response


async def logout(request: web.Request) -> web.Response:
    response = web.HTTPFound("/")
    response.del_cookie(SESSION_COOKIE)
    raise response
