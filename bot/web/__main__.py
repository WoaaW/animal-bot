"""The stats website: python -m bot.web, then open http://localhost:8080.

A small aiohttp server (aiohttp already comes with discord.py) next to the
bot, reading the same database. It serves one static page (static/) that
fetches a guild's data as JSON and renders it in the browser."""

import asyncio
import logging
import os
from pathlib import Path

from aiohttp import web

from ..config import DISCORD_TOKEN
from ..db import SessionLocal, init_db
from . import auth
from .data import guild_data, guild_ids
from .names import Names

STATIC = Path(__file__).parent / "static"
# 127.0.0.1: only this machine. Set WEB_HOST=0.0.0.0 to open it to the network.
HOST = os.environ.get("WEB_HOST", "127.0.0.1")
PORT = int(os.environ.get("WEB_PORT", "8080"))
# Parallel name lookups on Discord's API, to stay clear of its rate limit.
NAME_LOOKUPS = 5

names = Names(DISCORD_TOKEN)


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html")


def visitor(request: web.Request) -> dict | None:
    """The signed-in user; 401 when sign-in is on and nobody is."""
    user = auth.current_user(request)
    if auth.ENABLED and user is None:
        raise web.HTTPUnauthorized()
    return user


async def can_see(user: dict | None, guild_id: int) -> bool:
    return user is None or await names.is_member(guild_id, int(user["id"]))


async def api_me(request: web.Request) -> web.Response:
    return web.json_response({"auth": auth.ENABLED, "user": auth.current_user(request)})


async def api_guilds(request: web.Request) -> web.Response:
    user = visitor(request)
    async with SessionLocal() as session:
        ids = await guild_ids(session)
    # Only the servers the visitor is in.
    allowed = await asyncio.gather(*(can_see(user, g) for g in ids))
    ids = [g for g, ok in zip(ids, allowed) if ok]
    guilds = await asyncio.gather(*(names.guild(g) for g in ids))
    return web.json_response([{"id": str(g), **info} for g, info in zip(ids, guilds)])


async def api_guild(request: web.Request) -> web.Response:
    try:
        guild_id = int(request.match_info["guild_id"])
    except ValueError:
        raise web.HTTPNotFound()
    if not await can_see(visitor(request), guild_id):
        raise web.HTTPForbidden()
    async with SessionLocal() as session:
        if guild_id not in await guild_ids(session):
            raise web.HTTPNotFound()
        data = await guild_data(session, guild_id)

    limit = asyncio.Semaphore(NAME_LOOKUPS)

    async def member(player: dict) -> None:
        async with limit:
            player.update(await names.member(guild_id, int(player["id"])))

    await asyncio.gather(*(member(p) for p in data["players"]))
    data["guild"] = await names.guild(guild_id)
    response = web.json_response(data)
    # A few hundred KB of JSON (the whole animal pool): gzip it.
    response.enable_compression()
    return response


async def on_cleanup(app: web.Application) -> None:
    await names.close()


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/login", auth.login)
    app.router.add_get(auth.REDIRECT_PATH, auth.callback)
    app.router.add_get("/logout", auth.logout)
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/guilds", api_guilds)
    app.router.add_get("/api/guilds/{guild_id}", api_guild)
    app.router.add_static("/static", STATIC)
    app.on_cleanup.append(on_cleanup)
    return app


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # Same schema upgrades as the bot, in case the site starts first.
    await init_db()
    if not auth.ENABLED:
        print("Connexion Discord désactivée (DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET absents) : "
              "tout visiteur voit tous les serveurs.", flush=True)
    runner = web.AppRunner(make_app())
    await runner.setup()
    await web.TCPSite(runner, HOST, PORT).start()
    print(f"Site des stats : http://{'localhost' if HOST == '127.0.0.1' else HOST}:{PORT}", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
