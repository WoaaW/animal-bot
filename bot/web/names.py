"""Player and server names for the web page. The database only stores Discord
IDs, so names and avatars come from Discord's REST API, with the bot's token,
and are cached for a while. Single-member lookups don't need the privileged
members intent (only listing a guild's members does)."""

import asyncio
import time

import aiohttp

API = "https://discord.com/api/v10"
CDN = "https://cdn.discordapp.com"
CACHE_SECONDS = 15 * 60


class Names:
    def __init__(self, token: str | None):
        self.token = token
        self._cache: dict[str, tuple[float, dict]] = {}
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def _get(self, path: str) -> dict | None:
        if not self.token:
            return None
        if self._session is None:
            self._session = aiohttp.ClientSession(headers={"Authorization": f"Bot {self.token}"})
        try:
            async with self._session.get(API + path, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 429:
                    # Rate-limited: wait the time Discord asks for, once.
                    retry = (await response.json()).get("retry_after", 1)
                    await asyncio.sleep(min(float(retry), 5))
                    async with self._session.get(API + path) as again:
                        return await again.json() if again.status == 200 else None
                return await response.json() if response.status == 200 else None
        except (aiohttp.ClientError, TimeoutError):
            return None

    async def _cached(self, key: str, fetch) -> dict:
        hit = self._cache.get(key)
        if hit is not None and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
        value = await fetch()
        self._cache[key] = (time.time(), value)
        return value

    async def guild(self, guild_id: int) -> dict:
        async def fetch():
            data = await self._get(f"/guilds/{guild_id}")
            if data is None:
                return {"name": f"Serveur {guild_id}", "icon": None}
            icon = f"{CDN}/icons/{guild_id}/{data['icon']}.png?size=128" if data.get("icon") else None
            return {"name": data["name"], "icon": icon}

        return await self._cached(f"g{guild_id}", fetch)

    async def member(self, guild_id: int, user_id: int) -> dict:
        """Server nickname and avatar if they're still a member, else the
        account's, else a placeholder."""

        async def fetch():
            member = await self._get(f"/guilds/{guild_id}/members/{user_id}")
            user = member["user"] if member else await self._get(f"/users/{user_id}")
            if user is None:
                return {"name": f"Joueur …{str(user_id)[-4:]}", "avatar": _default_avatar(user_id)}
            name = (member or {}).get("nick") or user.get("global_name") or user["username"]
            if member and member.get("avatar"):
                avatar = f"{CDN}/guilds/{guild_id}/users/{user_id}/avatars/{member['avatar']}.png?size=64"
            elif user.get("avatar"):
                avatar = f"{CDN}/avatars/{user_id}/{user['avatar']}.png?size=64"
            else:
                avatar = _default_avatar(user_id)
            return {"name": name, "avatar": avatar}

        return await self._cached(f"m{guild_id}:{user_id}", fetch)


def _default_avatar(user_id: int) -> str:
    return f"{CDN}/embed/avatars/{(user_id >> 22) % 6}.png"

