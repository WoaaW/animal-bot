from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from ..db import SessionLocal
from ..economy import CURRENCY_NAME, add_points, get_status, give_bonus, reset_player


class Admin(commands.Cog):
    """Testing/moderation tools, restricted to the bot's owner (the owner of
    the application in the Developer Portal, or its team members)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        # Applies to every command here, slash and prefix alike.
        if not await self.bot.is_owner(ctx.author):
            raise commands.NotOwner()
        return True

    # hidden: left out of /help. default_permissions: Discord only shows the
    # slash command to server admins; the owner check above is what enforces.
    @commands.hybrid_group(name="admin", description="Outils d'administration du bot.", hidden=True)
    @app_commands.default_permissions(administrator=True)
    async def admin(self, ctx: commands.Context):
        await ctx.send(f"Sous-commandes : `give`, `reset`. Voir `{ctx.prefix}admin give`.", ephemeral=True)

    @admin.command(name="give", description="Donne des points, rolls ou claims bonus à un joueur.")
    @app_commands.describe(
        what="Ce qu'il faut donner",
        amount="La quantité",
        member="Le joueur (toi par défaut)",
    )
    async def give(
        self,
        ctx: commands.Context,
        what: Literal["points", "rolls", "claims"],
        amount: commands.Range[int, 1, 1_000_000],
        member: discord.Member | None = None,
    ):
        if ctx.guild is None:
            await ctx.send("Utilise cette commande dans un serveur.", ephemeral=True)
            return
        target = member or ctx.author
        async with SessionLocal() as session:
            if what == "points":
                await add_points(session, ctx.guild.id, target.id, amount)
            else:
                await give_bonus(session, ctx.guild.id, target.id, **{what: amount})
            await session.commit()
            status = await get_status(session, ctx.guild.id, target.id)

        await ctx.send(
            f"**{amount}** {CURRENCY_NAME if what == 'points' else what} donnés à {target.mention}. "
            f"Ce joueur a maintenant {status.points} {CURRENCY_NAME}, "
            f"{status.rolls_left} rolls (+{status.bonus_rolls} bonus), "
            f"{status.claims} claims (+{status.bonus_claims} bonus).",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @admin.command(name="reset", description="Remet à zéro les points, rolls, claims et améliorations d'un joueur.")
    @app_commands.describe(member="Le joueur (toi par défaut)")
    async def reset(self, ctx: commands.Context, member: discord.Member | None = None):
        if ctx.guild is None:
            await ctx.send("Utilise cette commande dans un serveur.", ephemeral=True)
            return
        target = member or ctx.author
        async with SessionLocal() as session:
            await reset_player(session, ctx.guild.id, target.id)
            await session.commit()
        await ctx.send(
            f"Points, rolls, claims et améliorations de {target.mention} remis à zéro, et réserve vidée "
            "(les animaux sauvés sont gardés).",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
