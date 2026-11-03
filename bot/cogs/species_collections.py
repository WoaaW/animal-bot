import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from ..species_collections import COLLECTION_TARGET, COLLECTIONS, DIFFICULTY, progress, rescued_species, total_bonus
from ..db import SessionLocal
from ..models import Animal
from .rolls import RARITY_LABEL, RARITY_ORDER, _fold, paginate, send_pages


def _bar(count: int) -> str:
    # Same look as the shop's bars; in a code span so the stars stay stars.
    done = min(count, COLLECTION_TARGET)
    return f"`|{'*' * done}{'-' * (COLLECTION_TARGET - done)}|` {done}/{COLLECTION_TARGET}"


class Collections(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(
        name="collections",
        description="Tes collections : réunis des familles d'animaux pour augmenter le revenu de ta réserve.",
    )
    @app_commands.describe(group="Voir les espèces d'un groupe")
    async def collections(self, ctx: commands.Context, *, group: str | None = None):
        if ctx.guild is None:
            await ctx.send("Utilise cette commande dans un serveur.", ephemeral=True)
            return
        collection = None
        if group is not None:
            needle = _fold(group)
            collection = next((a for a in COLLECTIONS if _fold(a.label) == needle), None)
            if collection is None:
                await ctx.send(f"Aucune collection ne s'appelle « {group} ».", ephemeral=True)
                return
        await ctx.defer(ephemeral=True)

        async with SessionLocal() as session:
            species = await rescued_species(session, ctx.guild.id, ctx.author.id)
            members = []
            if collection is not None:
                animals = (await session.execute(select(Animal))).scalars().all()
                members = [a for a in animals if collection.includes(a)]

        if collection is None:
            await ctx.send(embed=self._overview(ctx.author, species), ephemeral=True)
            return

        # One group: every species in it, rarest first, ✓ for those rescued.
        owned = {a.id for a in species}
        members.sort(key=lambda a: (RARITY_ORDER[a.rarity], _fold(a.name)))
        lines = [
            f"{'✓' if a.id in owned else '·'} **{a.name}** ({RARITY_LABEL[a.rarity]})"
            for a in members
        ]
        count = progress(species, collection)
        difficulty, bonus = DIFFICULTY[collection.difficulty]
        title = f"{collection.label} · {min(count, COLLECTION_TARGET)}/{COLLECTION_TARGET} · {difficulty}, +{bonus} %"
        await send_pages(ctx, paginate(title, lines, discord.Color.gold()), ephemeral=True)

    @staticmethod
    def _overview(member: discord.abc.User, species: list[Animal]) -> discord.Embed:
        embed = discord.Embed(
            title=f"Collections de {member.display_name} · +{total_bonus(species)} % de revenu de réserve",
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
