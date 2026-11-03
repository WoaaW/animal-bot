import discord
from discord.ext import commands
from sqlalchemy import select

from ..db import SessionLocal
from ..economy import CURRENCY_NAME, get_status, prestige
from ..milestones import MILESTONES, completed, progress, required_milestone
from ..models import Animal
from ..species_collections import rescued_species
from .milestones import _bar

CONFIRM_TIMEOUT_SECONDS = 60


class PrestigeView(discord.ui.View):
    """Confirmation under the prestige message: it wipes the account."""

    def __init__(self, guild_id: int, user_id: int, level: int):
        super().__init__(timeout=CONFIRM_TIMEOUT_SECONDS)
        self.guild_id = guild_id
        self.user_id = user_id
        self.level = level
        self.message: discord.Message | None = None
        self.used = False

    def _close(self) -> None:
        for item in self.children:
            item.disabled = True
        self.stop()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("Ce prestige n'est pas le tien.", ephemeral=True)
        return False

    @discord.ui.button(label="Tout remettre à zéro et prestige", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Flag set before any await, like RescueView: only the first click passes.
        if self.used:
            await interaction.response.send_message("Ce prestige est déjà en cours.", ephemeral=True)
            return
        self.used = True
        async with SessionLocal() as session:
            status = await prestige(session, self.guild_id, self.user_id, self.level)
            await session.commit()
        self._close()
        if status is None:
            await interaction.response.edit_message(
                content="Prestige impossible : le milestone demandé n'est plus complet.", view=self
            )
            return
        await interaction.response.edit_message(content="Prestige effectué.", view=self)
        await interaction.followup.send(
            f"{interaction.user.mention} passe au prestige **{status.prestige_level}** !",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._close()
        await interaction.response.edit_message(content="Prestige annulé.", view=self)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class Prestige(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(
        name="prestige",
        description="Complète un milestone pour tout remettre à zéro et monter en prestige.",
    )
    async def prestige(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send("Utilise cette commande dans un serveur.", ephemeral=True)
            return
        await ctx.defer(ephemeral=True)

        async with SessionLocal() as session:
            status = await get_status(session, ctx.guild.id, ctx.author.id)
            species = await rescued_species(session, ctx.guild.id, ctx.author.id)
            animals = (await session.execute(select(Animal))).scalars().all()

        level = status.prestige_level
        milestone = required_milestone(level)
        if milestone is None:
            await ctx.send(f"Tu as atteint le prestige maximum (**{len(MILESTONES)}**) !", ephemeral=True)
            return
        total = sum(milestone.includes(a) for a in animals)
        ready = milestone in completed(species, animals)
        text = (
            f"Prestige actuel : **{level}**\n"
            f"Milestone demandé : **{milestone.label}**\n"
            f"{_bar(progress(species, milestone), total)}\n\n"
        )
        if not ready:
            await ctx.send(
                text + f"Sauve toutes ses espèces pour pouvoir prestige (`/milestones {milestone.label}`).",
                ephemeral=True,
            )
            return
        view = PrestigeView(ctx.guild.id, ctx.author.id, level)
        view.message = await ctx.send(
            text + f"**Attention :** tu perdras tes {CURRENCY_NAME}, tes améliorations, ta réserve et **tous tes animaux**. "
            "Cette action est définitive.",
            view=view,
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Prestige(bot))
