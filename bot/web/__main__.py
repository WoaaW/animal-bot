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


async def api_guilds(request: web.Request) -> web.Response:
    async with SessionLocal() as session:
        ids = await guild_ids(session)
    guilds = await asyncio.gather(*(names.guild(g) for g in ids))
    return web.json_response([{"id": str(g), **info} for g, info in zip(ids, guilds)])


async def api_guild(request: web.Request) -> web.Response:
    try:
        guild_id = int(request.match_info["guild_id"])
    except ValueError:
        raise web.HTTPNotFound()
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
    app.router.add_get("/api/guilds", api_guilds)
    app.router.add_get("/api/guilds/{guild_id}", api_guild)
    app.router.add_static("/static", STATIC)
    app.on_cleanup.append(on_cleanup)
    return app


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # Same schema upgrades as the bot, in case the site starts first.
    await init_db()
    runner = web.AppRunner(make_app())
    await runner.setup()
    await web.TCPSite(runner, HOST, PORT).start()
    print(f"Site des stats : http://{'localhost' if HOST == '127.0.0.1' else HOST}:{PORT}")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
