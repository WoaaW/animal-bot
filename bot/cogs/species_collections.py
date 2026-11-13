import discord
from discord import app_commands
from discord.ext import commands

from ..species_collections import COLLECTION_TARGET, COLLECTIONS, DIFFICULTY, progress, rescued_species, total_bonus
from ..db import SessionLocal
from ..models import Animal
from .rolls import _fold


def _bar(count: int) -> str:
    # Same look as the shop's bars; in a code span so the stars stay stars.
    # The bar stops at the target, but the count goes past it (7/5): extra
    # species still matter, they keep the collection if one is traded away.
    done = min(count, COLLECTION_TARGET)
    return f"`|{'*' * done}{'-' * (COLLECTION_TARGET - done)}|` {count}/{COLLECTION_TARGET}"


class Collections(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(
        name="collections",
        description="Tes collections : réunis des familles d'animaux pour augmenter le revenu de tes totems.",
    )
    @app_commands.describe(group="Voir les espèces d'un groupe")
    async def collections(self, ctx: commands.Context, *, group: str | None = None):
        if ctx.guild is None:
            await ctx.send("Utilise cette commande dans un serveur.")
            return
        collection = None
        if group is not None:
            needle = _fold(group)
            collection = next((a for a in COLLECTIONS if _fold(a.label) == needle), None)
            if collection is None:
                await ctx.send(f"Aucune collection ne s'appelle « {group} ».")
                return
        await ctx.defer()
        async with SessionLocal() as session:
            species = await rescued_species(session, ctx.guild.id, ctx.author.id)
        if collection is None:
            await ctx.send(embed=self._overview(ctx.author, species))
            return
        # Same list and cards as /animals <groupe>, with the player's progress
        # and the collection's reward above.
        difficulty, bonus = DIFFICULTY[collection.difficulty]
        content = (
            f"**{collection.label}** {_bar(progress(species, collection))} · "
            f"{difficulty}, +{bonus} % de revenu des totems"
        )
        await self.bot.get_cog("Rolls").send_animal_list(ctx, None, collection, content=content)

    @staticmethod
    def _overview(member: discord.abc.User, species: list[Animal]) -> discord.Embed:
        embed = discord.Embed(
            title=f"Collections de {member.display_name} · +{total_bonus(species)} % de revenu des totems",
            description=(
                f"Sauve {COLLECTION_TARGET} espèces différentes d'un groupe pour compléter sa collection. "
                "Les espèces d'un groupe : `/collections <groupe>`."
            ),
            color=discord.Color.gold(),
        )
        # One field per difficulty, easiest first (at most a handful of lines
        # each, well under the 1024-char field limit).
        for key, (label, bonus) in DIFFICULTY.items():
            lines = []
            for collection in COLLECTIONS:
                if collection.difficulty != key:
                    continue
                count = progress(species, collection)
                mark = "✓" if count >= COLLECTION_TARGET else "·"
                lines.append(f"{mark} **{collection.label}** {_bar(count)}")
            if lines:
                embed.add_field(name=f"{label} · +{bonus} % chacun", value="\n".join(lines), inline=False)
        return embed

    @collections.autocomplete("group")
    async def group_autocomplete(self, interaction: discord.Interaction, current: str):
        needle = _fold(current)
        return [
            app_commands.Choice(name=a.label, value=a.label)
            for a in COLLECTIONS
            if needle in _fold(a.label)
        ][:25]


async def setup(bot: commands.Bot):
    await bot.add_cog(Collections(bot))
