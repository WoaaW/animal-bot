import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from ..achievements import ACHIEVEMENT_TARGET, ACHIEVEMENTS, DIFFICULTY, progress, rescued_species, total_bonus
from ..db import SessionLocal
from ..models import Animal
from .rolls import RARITY_LABEL, RARITY_ORDER, _fold, paginate, send_pages


def _bar(count: int) -> str:
    # Same look as the shop's bars; in a code span so the stars stay stars.
    done = min(count, ACHIEVEMENT_TARGET)
    return f"`|{'*' * done}{'-' * (ACHIEVEMENT_TARGET - done)}|` {done}/{ACHIEVEMENT_TARGET}"


class Achievements(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(
        name="achievements",
        description="Tes succès : collectionne des familles d'animaux pour augmenter le revenu de ta réserve.",
    )
    @app_commands.describe(group="Voir les espèces d'un groupe")
    async def achievements(self, ctx: commands.Context, *, group: str | None = None):
        if ctx.guild is None:
            await ctx.send("Utilise cette commande dans un serveur.", ephemeral=True)
            return
        achievement = None
        if group is not None:
            needle = _fold(group)
            achievement = next((a for a in ACHIEVEMENTS if _fold(a.label) == needle), None)
            if achievement is None:
                await ctx.send(f"Aucun succès ne s'appelle « {group} ».", ephemeral=True)
                return
        await ctx.defer(ephemeral=True)

        async with SessionLocal() as session:
            species = await rescued_species(session, ctx.guild.id, ctx.author.id)
            members = []
            if achievement is not None:
                animals = (await session.execute(select(Animal))).scalars().all()
                members = [a for a in animals if achievement.includes(a)]

        if achievement is None:
            await ctx.send(embed=self._overview(ctx.author, species), ephemeral=True)
            return

        # One group: every species in it, rarest first, ✓ for those rescued.
        owned = {a.id for a in species}
        members.sort(key=lambda a: (RARITY_ORDER[a.rarity], _fold(a.name)))
        lines = [
            f"{'✓' if a.id in owned else '·'} **{a.name}** ({RARITY_LABEL[a.rarity]})"
            for a in members
        ]
        count = progress(species, achievement)
        difficulty, bonus = DIFFICULTY[achievement.difficulty]
        title = f"{achievement.label} · {min(count, ACHIEVEMENT_TARGET)}/{ACHIEVEMENT_TARGET} · {difficulty}, +{bonus} %"
        await send_pages(ctx, paginate(title, lines, discord.Color.gold()), ephemeral=True)

    @staticmethod
    def _overview(member: discord.abc.User, species: list[Animal]) -> discord.Embed:
        embed = discord.Embed(
            title=f"Succès de {member.display_name} · +{total_bonus(species)} % de revenu de réserve",
            description=(
                f"Sauve {ACHIEVEMENT_TARGET} espèces différentes d'un groupe pour compléter son succès. "
                "Les espèces d'un groupe : `/achievements <groupe>`."
            ),
            color=discord.Color.gold(),
        )
        # One field per difficulty, easiest first (at most a handful of lines
        # each, well under the 1024-char field limit).
        for key, (label, bonus) in DIFFICULTY.items():
            lines = []
            for achievement in ACHIEVEMENTS:
                if achievement.difficulty != key:
                    continue
                count = progress(species, achievement)
                mark = "✓" if count >= ACHIEVEMENT_TARGET else "·"
                lines.append(f"{mark} **{achievement.label}** {_bar(count)}")
            if lines:
                embed.add_field(name=f"{label} · +{bonus} % chacun", value="\n".join(lines), inline=False)
        return embed

    @achievements.autocomplete("group")
    async def group_autocomplete(self, interaction: discord.Interaction, current: str):
        needle = _fold(current)
        return [
            app_commands.Choice(name=a.label, value=a.label)
            for a in ACHIEVEMENTS
            if needle in _fold(a.label)
        ][:25]


async def setup(bot: commands.Bot):
    await bot.add_cog(Achievements(bot))
