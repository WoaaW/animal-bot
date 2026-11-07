import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func, select

from ..db import SessionLocal
from ..economy import CURRENCY_NAME, animal_score, completed_milestones, get_reserve_status, milestone_bonus, reserve_income
from ..milestones import MILESTONES
from ..models import Animal, Rescue
from ..species_collections import COLLECTIONS, completed, rescued_species
from .totems import _income, _rescues
from .rolls import animal_embed, animal_header, format_appeal, format_rate, format_reserve_bonus


def _animal_page(row: tuple[Rescue, Animal], number: int, total: int) -> discord.Embed:
    rescue, animal = row
    embed = animal_embed(animal, full=False)
    # Just the photo and the facts: descriptions are too long for a profile.
    embed.description = animal_header(animal)
    embed.add_field(
        name="Aura",
        value=f"{format_appeal(rescue.appeal)} · {reserve_income(animal.rarity, rescue.appeal)} {CURRENCY_NAME}/h comme totem",
        inline=False,
    )
    # Keeps the Wikipedia credit animal_embed sets.
    credit = f" · {embed.footer.text}" if embed.footer.text else ""
    embed.set_footer(text=f"Animal {number}/{total}{credit}")
    return embed


class ProfileView(discord.ui.View):
    """Flips through the reserve's animals under the fixed summary."""

    def __init__(self, summary: discord.Embed, pages: list[discord.Embed], owner_id: int):
        super().__init__(timeout=180)
        self.summary = summary
        self.pages = pages
        self.owner_id = owner_id
        self.index = 0
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Lance la commande toi-même pour feuilleter !", ephemeral=True)
            return False
        return True

    async def _show(self, interaction: discord.Interaction, index: int) -> None:
        self.index = index % len(self.pages)
        await interaction.response.edit_message(embeds=[self.summary, self.pages[self.index]], view=self)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.primary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show(interaction, self.index - 1)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show(interaction, self.index + 1)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class Profile(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="profile", description="Affiche le profil d'un joueur : badges de milestones et totems.")
    @app_commands.describe(member="Le joueur à afficher (toi par défaut)")
    @app_commands.guild_only()
    async def profile(self, ctx: commands.Context, member: discord.Member | None = None):
        if ctx.guild is None:
            await ctx.send("Utilise cette commande dans un serveur.", ephemeral=True)
            return
        await ctx.defer()
        target = member or ctx.author

        async with SessionLocal() as session:
            reached = await completed_milestones(session, ctx.guild.id, target.id)
            reserve = await get_reserve_status(session, ctx.guild.id, target.id)
            species = await rescued_species(session, ctx.guild.id, target.id)
            pool_size = await session.scalar(select(func.count()).select_from(Animal))
        owned = await _rescues(ctx.guild.id, target.id)
        score = sum(animal_score(animal.rarity, rescue.appeal) for rescue, animal in owned)
        rows = [row for row in owned if row[0].in_reserve]
        rows.sort(key=_income, reverse=True)

        badges = " ".join(m.badge for m in reached) or "Aucun pour l'instant"
        summary = discord.Embed(
            title=f"Profil de {target.display_name}",
            description=(
                f"Score : **{score}**\nMilestones : **{len(reached)}/{len(MILESTONES)}**"
                + (f" (+{milestone_bonus(len(reached))} % de {CURRENCY_NAME})" if reached else "")
                + f"\nBadges : {badges}\n"
                f"Espèces : **{len(species)}/{pool_size}** · collections : **{len(completed(species))}/{len(COLLECTIONS)}**\n"
                f"Totems : **{reserve.used}/{reserve.max_slots}**, {format_rate(reserve.rate)} {CURRENCY_NAME}/h"
                + format_reserve_bonus(reserve)
            ),
            color=discord.Color.gold(),
        )
        summary.set_thumbnail(url=target.display_avatar.url)

        if not rows:
            await ctx.send(embed=summary)
            return
        pages = [_animal_page(row, number, len(rows)) for number, row in enumerate(rows, 1)]
        if len(pages) == 1:
            await ctx.send(embeds=[summary, pages[0]])
            return
        view = ProfileView(summary, pages, ctx.author.id)
        view.message = await ctx.send(embeds=[summary, pages[0]], view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(Profile(bot))
