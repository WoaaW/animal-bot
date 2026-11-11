from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from sqlalchemy import select

from ..db import SessionLocal
from ..economy import (
    APPEAL,
    CURRENCY_NAME,
    CURRENCY_WORD,
    add_points,
    bank_reserve,
    get_status,
    give_bonus,
    reset_player,
    roll_appeal,
)
from ..models import Animal, Rescue
from .rolls import _fold, format_appeal, rarity_initials


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
        await ctx.send(f"Sous-commandes : `give`, `animal`, `reset`. Voir `{ctx.prefix}admin give`.", ephemeral=True)

    @admin.command(name="give", description=f"Donne des {CURRENCY_WORD}, rolls ou claims bonus à un joueur.")
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
            f"{target.mention} reçoit **{amount}** {CURRENCY_NAME if what == 'points' else what}. "
            f"Ce joueur a maintenant {status.points} {CURRENCY_NAME}, "
            f"{status.rolls_left} rolls (+{status.bonus_rolls} bonus), "
            f"{status.claims} claims (+{status.bonus_claims} bonus).",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # animal isn't keyword-only: a slash command can't put required options
    # after optional ones, so on the prefix side a multi-word name needs
    # quotes: !admin animal "Panda roux" @membre 5
    @admin.command(name="animal", description="Ajoute un animal à la réserve d'un joueur (debug).")
    @app_commands.describe(
        animal="L'animal à donner",
        member="Le joueur (toi par défaut)",
        aura="Son aura en étoiles (tirée au hasard par défaut)",
    )
    async def give_animal(
        self,
        ctx: commands.Context,
        animal: str,
        member: discord.Member | None = None,
        aura: commands.Range[int, 1, len(APPEAL)] = None,
    ):
        if ctx.guild is None:
            await ctx.send("Utilise cette commande dans un serveur.", ephemeral=True)
            return
        target = member or ctx.author
        await ctx.defer(ephemeral=True)
        async with SessionLocal() as session:
            animals = (await session.execute(select(Animal))).scalars().all()
            needle = _fold(animal)
            found = next((a for a in animals if _fold(a.name) == needle), None)
            if found is None:
                await ctx.send(f"Aucun animal ne s'appelle « {animal} ».", ephemeral=True)
                return
            appeal = aura or roll_appeal()
            # Like a rescue: a new species may complete a collection, which
            # raises totem income, so bank what was earned at the old rate.
            await bank_reserve(session, ctx.guild.id, target.id)
            session.add(Rescue(guild_id=ctx.guild.id, user_id=target.id, animal_id=found.id, appeal=appeal))
            await session.commit()
        await ctx.send(
            f"{target.mention} reçoit **{found.name}** · {rarity_initials(found.rarity)} ({format_appeal(appeal)}).",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @give_animal.autocomplete("animal")
    async def give_animal_autocomplete(self, interaction: discord.Interaction, current: str):
        async with SessionLocal() as session:
            names = (await session.execute(select(Animal.name).order_by(Animal.name))).scalars().all()
        needle = _fold(current)
        # Discord caps autocomplete at 25 choices.
        return [app_commands.Choice(name=n, value=n) for n in names if needle in _fold(n)][:25]

    @admin.command(name="reset", description=f"Remet à zéro les {CURRENCY_WORD}, rolls, claims et améliorations d'un joueur.")
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
            f"{CURRENCY_WORD.capitalize()}, rolls, claims, échanges, prestige et améliorations de {target.mention} "
            "remis à zéro, et totems retirés (les animaux sauvés restent dans la réserve).",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
