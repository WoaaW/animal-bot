import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from ..db import SessionLocal
from ..economy import get_status
from ..milestones import MILESTONES, progress, required_milestone
from ..models import Animal
from ..species_collections import rescued_species
from .rolls import RARITY_LABEL, RARITY_ORDER, _fold, paginate, send_pages

BAR_WIDTH = 20


def _bar(count: int, total: int) -> str:
    # Same look as the shop's bars, on a fixed width since groups are huge.
    done = count * BAR_WIDTH // total if total else 0
    percent = count * 100 // total if total else 0
    return f"`|{'*' * done}{'-' * (BAR_WIDTH - done)}|` {count}/{total} ({percent} %)"


class Milestones(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(
        name="milestones",
        description="Tes milestones : sauve toutes les espèces d'un grand groupe d'animaux.",
    )
    @app_commands.describe(group="Voir les espèces d'un groupe")
    async def milestones(self, ctx: commands.Context, *, group: str | None = None):
        if ctx.guild is None:
            await ctx.send("Utilise cette commande dans un serveur.", ephemeral=True)
            return
        milestone = None
        if group is not None:
            needle = _fold(group)
            milestone = next((m for m in MILESTONES if _fold(m.label) == needle), None)
            if milestone is None:
                await ctx.send(f"Aucun milestone ne s'appelle « {group} ».", ephemeral=True)
                return
        await ctx.defer(ephemeral=True)

        async with SessionLocal() as session:
            species = await rescued_species(session, ctx.guild.id, ctx.author.id)
            animals = (await session.execute(select(Animal))).scalars().all()
            status = await get_status(session, ctx.guild.id, ctx.author.id)

        if milestone is None:
            await ctx.send(embed=self._overview(ctx.author, species, animals, status.prestige_level), ephemeral=True)
            return

        # One group: every species in it, rarest first, ✓ for those rescued.
        owned = {a.id for a in species}
        members = [a for a in animals if milestone.includes(a)]
        members.sort(key=lambda a: (RARITY_ORDER[a.rarity], _fold(a.name)))
        lines = [
            f"{'✓' if a.id in owned else '·'} **{a.name}** ({RARITY_LABEL[a.rarity]})"
            for a in members
        ]
        title = f"{milestone.badge} {milestone.label} · {progress(species, milestone)}/{len(members)}"
        await send_pages(ctx, paginate(title, lines, discord.Color.gold()), ephemeral=True)

    @staticmethod
    def _overview(member: discord.abc.User, species: list[Animal], animals: list[Animal], prestige_level: int) -> discord.Embed:
        required = required_milestone(prestige_level)
        next_prestige = f"Pour le prochain prestige : {required.badge} **{required.label}**.\n\n" if required else "\n"
        lines = []
        for milestone in MILESTONES:
            count = progress(species, milestone)
            total = sum(milestone.includes(a) for a in animals)
            mark = "✓" if total and count >= total else "·"
            lines.append(f"{mark} {milestone.badge} **{milestone.label}**\n{_bar(count, total)}")
        return discord.Embed(
            title=f"Milestones de {member.display_name}",
            description=(
                "Sauve toutes les espèces d'un groupe pour compléter son milestone. "
                "Les espèces d'un groupe : `/milestones <groupe>`.\n"
                + next_prestige + "\n".join(lines)
            ),
            color=discord.Color.gold(),
        )

    @milestones.autocomplete("group")
    async def group_autocomplete(self, interaction: discord.Interaction, current: str):
        needle = _fold(current)
        return [
            app_commands.Choice(name=m.label, value=m.label)
            for m in MILESTONES
            if needle in _fold(m.label)
        ][:25]


async def setup(bot: commands.Bot):
    await bot.add_cog(Milestones(bot))
