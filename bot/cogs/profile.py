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
from .rolls import animal_embed, animal_header, format_appeal, format_rate, format_reserve_bonus, reserve_pages
from .species_collections import Collections


def _animal_page(row: tuple[Rescue, Animal], number: int, total: int) -> discord.Embed:
    rescue, animal = row
    embed = animal_embed(animal, full=False)
    # Just the photo and the facts: descriptions are too long for a profile.
    embed.description = animal_header(animal)
    embed.add_field(
        name="Aura",
        value=f"{format_appeal(rescue.appeal)} · {reserve_income(animal.rarity, rescue.appeal)} {CURRENCY_NAME}/h comme animal-totem",
        inline=False,
    )
    # Keeps the Wikipedia credit animal_embed sets.
    credit = f" · {embed.footer.text}" if embed.footer.text else ""
    embed.set_footer(text=f"Animal {number}/{total}{credit}")
    return embed


class ProfileView(discord.ui.View):
    """Under the fixed summary, one tab per part of the profile (totems,
    reserve, collections), each with its own pages and ◀️▶️ when it has
    several. Only the invoker clicks, since the message is public."""

    def __init__(self, summary: discord.Embed, tabs: dict[str, list[discord.Embed]], owner_id: int):
        super().__init__(timeout=180)
        self.summary = summary
        self.tabs = tabs
        self.owner_id = owner_id
        self.tab = next(iter(tabs))
        self.index = 0
        self.message: discord.Message | None = None
        self._render()

    @property
    def embeds(self) -> list[discord.Embed]:
        return [self.summary, self.tabs[self.tab][self.index]]

    def _render(self) -> None:
        """Rebuild the buttons: the tabs, then the arrows if this tab needs them."""
        self.clear_items()
        for name in self.tabs:
            button = discord.ui.Button(
                label=name,
                style=discord.ButtonStyle.primary if name == self.tab else discord.ButtonStyle.secondary,
                disabled=name == self.tab,
                row=0,
            )
            button.callback = self._open(name)
            self.add_item(button)
        if len(self.tabs[self.tab]) > 1:
            for emoji, step in (("◀️", -1), ("▶️", 1)):
                button = discord.ui.Button(emoji=emoji, style=discord.ButtonStyle.secondary, row=1)
                button.callback = self._flip(step)
                self.add_item(button)

    def _open(self, name: str):
        async def callback(interaction: discord.Interaction) -> None:
            self.tab, self.index = name, 0
            self._render()
            await interaction.response.edit_message(embeds=self.embeds, view=self)

        return callback

    def _flip(self, step: int):
        async def callback(interaction: discord.Interaction) -> None:
            # Wraps around, like before.
            self.index = (self.index + step) % len(self.tabs[self.tab])
            await interaction.response.edit_message(embeds=self.embeds, view=self)

        return callback

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Lance la commande toi-même pour feuilleter !", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


def _empty(text: str) -> discord.Embed:
    return discord.Embed(description=text, color=discord.Color.gold())


class Profile(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="profile", description="Affiche le profil d'un joueur : badges, totems, réserve et collections.")
    @app_commands.describe(member="Le joueur à afficher (toi par défaut)")
    @app_commands.guild_only()
    async def profile(self, ctx: commands.Context, member: discord.Member | None = None):
        if ctx.guild is None:
            await ctx.send("Utilise cette commande dans un serveur.")
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

        totems = [_animal_page(row, number, len(rows)) for number, row in enumerate(rows, 1)]
        animals = reserve_pages([(animal, rescue) for rescue, animal in owned], target)
        for page in animals:
            # The summary above already shows the avatar.
            page.set_thumbnail(url=None)
        tabs = {
            "Totems": totems or [_empty("Aucun totem pour l'instant.")],
            "Réserve": animals if owned else [_empty("Aucun animal sauvé pour l'instant.")],
            "Collections": [Collections._overview(target, species)],
        }
        view = ProfileView(summary, tabs, ctx.author.id)
        view.message = await ctx.send(embeds=view.embeds, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(Profile(bot))
