"""Mention the bot and it answers, through a small local model (Ollama).

The model is tiny (it runs on the Pi next to the bot), fluent but unreliable
on facts, so when the message names animals of the pool, their Wikipedia
summary is passed along for it to lean on. One answer at a time, a short
cooldown per player, and a capped queue keep the Pi responsive for the game.
"""

import asyncio
import logging
import re

import aiohttp
import discord
from discord.ext import commands
from sqlalchemy import select

from ..config import OLLAMA_MODEL, OLLAMA_URL, PREFIX
from ..db import SessionLocal
from ..models import Animal
from .rolls import RARITY_LABEL, _first_sentences, _fold

log = logging.getLogger(__name__)

# Seconds between two questions of the same player.
CHAT_COOLDOWN_SECONDS = 15
# Questions waiting for the model at most; beyond, the bot says it's busy.
MAX_WAITING = 2
# Animals of the pool whose summary goes into the prompt at most.
MAX_ANIMALS_IN_PROMPT = 2
# Generation limits: short answers, small context (less memory on the Pi).
MAX_ANSWER_TOKENS = 200
CONTEXT_TOKENS = 2048
REQUEST_TIMEOUT_SECONDS = 120

SYSTEM_PROMPT = (
    "Tu es Capybabot, le bot d'un serveur Discord francophone. Dans le jeu, les joueurs "
    "sauvent des animaux du monde entier pour les mettre dans leur réserve. "
    "Réponds toujours en français, en une à trois phrases, sur un ton amical et un peu drôle. "
    "N'invente pas de faits : si tu ne sais pas, dis-le simplement."
)


class Chat(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.lock = asyncio.Lock()
        self.waiting = 0
        self.cooldown = commands.CooldownMapping.from_cooldown(1, CHAT_COOLDOWN_SECONDS, commands.BucketType.user)
        # (pattern on the folded name, animal), longest names first, loaded once.
        self.animals: list[tuple[re.Pattern, Animal]] | None = None

    async def _load_animals(self) -> list[tuple[re.Pattern, Animal]]:
        if self.animals is None:
            async with SessionLocal() as session:
                pool = (await session.execute(select(Animal))).scalars().all()
            pool = sorted(pool, key=lambda a: len(a.name), reverse=True)
            self.animals = [(re.compile(rf"\b{re.escape(_fold(a.name))}\b"), a) for a in pool]
        return self.animals

    async def _facts(self, text: str) -> str:
        """Summaries of the pool's animals named in the text, as reliable facts."""
        folded = _fold(text)
        found: list[Animal] = []
        for pattern, animal in await self._load_animals():
            if pattern.search(folded):
                found.append(animal)
                # "Panda roux" also contains "panda": skip the shorter name.
                folded = pattern.sub(" ", folded)
                if len(found) == MAX_ANIMALS_IN_PROMPT:
                    break
        lines = []
        for animal in found:
            summary = _first_sentences(animal.description or "", 3)
            lines.append(
                f"- {animal.name} ({animal.scientific_name}), rareté dans le jeu : "
                f"{RARITY_LABEL[animal.rarity]}. {summary}"
            )
        return "\n".join(lines)

    async def _ask(self, messages: list[dict]) -> str:
        payload = {
            "model": OLLAMA_MODEL,
            "messages": messages,
            "stream": False,
            "options": {"num_predict": MAX_ANSWER_TOKENS, "num_ctx": CONTEXT_TOKENS, "temperature": 0.7},
        }
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{OLLAMA_URL}/api/chat", json=payload) as response:
                response.raise_for_status()
                data = await response.json()
        return data["message"]["content"].strip()

    def _question(self, message: discord.Message) -> str:
        # clean_content shows other mentions as @name; drop the bot's own.
        me = message.guild.me
        text = message.clean_content
        for name in {me.display_name, me.name}:
            text = text.replace(f"@{name}", "")
        return text.strip()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None or self.bot.user not in message.mentions:
            return
        if message.content.startswith(PREFIX):
            return  # a command, handled elsewhere
        if self.cooldown.update_rate_limit(message):
            await message.add_reaction("⏳")
            return
        if self.waiting >= MAX_WAITING:
            await message.reply("Je réfléchis déjà à plein de choses, repose ta question dans un instant !",
                                mention_author=False)
            return

        question = self._question(message) or "Coucou !"
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        # Answering a message: give it as context (the bot's own as its earlier turn).
        reference = message.reference.resolved if message.reference else None
        if isinstance(reference, discord.Message) and reference.content:
            if reference.author == self.bot.user:
                messages.append({"role": "assistant", "content": reference.content})
            else:
                question = f"(En réponse à {reference.author.display_name} : « {reference.clean_content} »)\n{question}"
        facts = await self._facts(question)
        if facts:
            messages[0]["content"] += f"\n\nInformations fiables à utiliser si c'est utile :\n{facts}"
        messages.append({"role": "user", "content": f"{message.author.display_name} : {question}"})

        self.waiting += 1
        try:
            async with self.lock, message.channel.typing():
                answer = await self._ask(messages)
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError) as exc:
            log.warning("Réponse du modèle impossible : %r", exc)
            answer = "Je n'arrive pas à réfléchir pour l'instant 😴 Réessaie plus tard !"
        finally:
            self.waiting -= 1

        await message.reply(answer[:2000] or "…", mention_author=False,
                            allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    await bot.add_cog(Chat(bot))
