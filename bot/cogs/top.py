from collections import defaultdict

import discord
from discord.ext import commands
from sqlalchemy import select

from .. import milestones
from ..db import SessionLocal
from ..economy import CURRENCY_NAME, animal_score, milestone_bonus, reserve_income, reserve_rate
from ..models import Animal, Rescue
from ..species_collections import COLLECTIONS, completed, total_bonus
from .rolls import format_rate

# Players shown per ranking.
TOP_SIZE = 10
MEDALS = ["🥇", "🥈", "🥉"]


def _ranking(values: dict[int, float], fmt, author_id: int) -> str:
    """Best first, players at 0 left out. The invoker gets their own rank
    below if they're not in the top."""
    ranked = sorted(((v, u) for u, v in values.items() if v > 0), reverse=True)
    if not ranked:
        return "Personne pour l'instant."
    lines = []
    for i, (value, user_id) in enumerate(ranked[:TOP_SIZE]):
        place = MEDALS[i] if i < len(MEDALS) else f"`#{i + 1}`"
        lines.append(f"{place} <@{user_id}> · **{fmt(value)}**")
    rank = next((i for i, (_, u) in enumerate(ranked) if u == author_id), None)
    if rank is not None and rank >= TOP_SIZE:
        lines.append(f"…\n`#{rank + 1}` <@{author_id}> · **{fmt(ranked[rank][0])}**")
    return "\n".join(lines)


class Top(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="top", description="Les classements du serveur : score, revenu des totems, collections.")
    async def top(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send("Utilise cette commande dans un serveur.")
            return
        await ctx.defer()

        # One query for every rescue of the guild; everything else is computed
        # here, the same way /profile and /totem compute it for one player.
        async with SessionLocal() as session:
            pool = list((await session.execute(select(Animal))).scalars())
            rows = (
                await session.execute(
                    select(Rescue.user_id, Rescue.animal_id, Rescue.appeal, Rescue.in_reserve).where(
                        Rescue.guild_id == ctx.guild.id
                    )
                )
            ).all()
        animals = {a.id: a for a in pool}

        score: dict[int, int] = defaultdict(int)
        base: dict[int, int] = defaultdict(int)
        species: dict[int, set[int]] = defaultdict(set)
        for user_id, animal_id, appeal, in_reserve in rows:
            animal = animals[animal_id]
            score[user_id] += animal_score(animal.rarity, appeal)
            species[user_id].add(animal_id)
            if in_reserve:
                base[user_id] += reserve_income(animal.rarity, appeal)

        income: dict[int, float] = {}
        collections: dict[int, int] = {}
        for user_id, ids in species.items():
            owned = [animals[i] for i in ids]
            collections[user_id] = len(completed(owned))
            if base[user_id]:
                reached = milestones.completed(owned, pool)
                income[user_id] = reserve_rate(base[user_id], total_bonus(owned), milestone_bonus(len(reached)))

        embed = discord.Embed(title=f"Classements de {ctx.guild.name}", color=discord.Color.gold())
        # Mentions inside embeds show names without pinging.
        embed.add_field(name="Score", value=_ranking(score, str, ctx.author.id), inline=False)
        embed.add_field(
            name=f"Revenu des totems ({CURRENCY_NAME}/h)",
            value=_ranking(income, format_rate, ctx.author.id),
            inline=False,
        )
        embed.add_field(
            name=f"Collections complétées (sur {len(COLLECTIONS)})",
            value=_ranking(collections, str, ctx.author.id),
            inline=False,
        )
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Top(bot))
