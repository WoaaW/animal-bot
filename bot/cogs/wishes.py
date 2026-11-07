import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert

from ..db import SessionLocal
from ..models import Animal, Rescue, Wish
from .rolls import RARITY_LABEL, RARITY_ORDER, _fold, paginate, send_pages

# Wishlist size per player and guild. A long list would ping on most rolls.
MAX_WISHES = 10


async def _wishlist(guild_id: int, user_id: int) -> list[Animal]:
    async with SessionLocal() as session:
        stmt = (
            select(Animal)
            .join(Wish, Wish.animal_id == Animal.id)
            .where(Wish.guild_id == guild_id, Wish.user_id == user_id)
        )
        animals = list((await session.execute(stmt)).scalars())
    animals.sort(key=lambda a: (RARITY_ORDER[a.rarity], _fold(a.name)))
    return animals


def _find(animals: list[Animal], query: str) -> Animal | None:
    # Autocomplete sends the exact name, but typed names are matched case-
    # and accent-insensitively, like /info.
    needle = _fold(query)
    return next((a for a in animals if _fold(a.name) == needle), None)


def _choices(names: list[str], current: str) -> list[app_commands.Choice[str]]:
    needle = _fold(current)
    # Discord caps autocomplete at 25 choices.
    return [app_commands.Choice(name=n, value=n) for n in names if needle in _fold(n)][:25]


class Wishes(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_group(
        name="wish",
        fallback="list",
        description="Ta wishlist : tu es mentionné quand quelqu'un d'autre roll un de ces animaux.",
    )
    async def wish(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send("Utilise cette commande dans un serveur.", ephemeral=True)
            return
        await ctx.defer(ephemeral=True)

        animals = await _wishlist(ctx.guild.id, ctx.author.id)
        async with SessionLocal() as session:
            owned = set(
                (
                    await session.execute(
                        select(Rescue.animal_id).where(
                            Rescue.guild_id == ctx.guild.id, Rescue.user_id == ctx.author.id
                        )
                    )
                ).scalars()
            )
        # 🏠 like /animals: already in the player's reserve.
        lines = [
            f"**{a.name}** — *{a.scientific_name}* ({RARITY_LABEL[a.rarity]}){' · 🏠' if a.id in owned else ''}"
            for a in animals
        ]
        if not lines:
            lines = ["Vide ! Ajoute un animal avec `/wish add`."]
        title = f"Wishlist de {ctx.author.display_name} ({len(animals)}/{MAX_WISHES})"
        await send_pages(ctx, paginate(title, lines, discord.Color.gold()), ephemeral=True)

    @wish.command(name="add", description="Ajoute un animal à ta wishlist.")
    @app_commands.describe(animal="L'animal à souhaiter")
    async def wish_add(self, ctx: commands.Context, *, animal: str):
        if ctx.guild is None:
            await ctx.send("Utilise cette commande dans un serveur.", ephemeral=True)
            return
        await ctx.defer(ephemeral=True)

        async with SessionLocal() as session:
            found = _find(list((await session.execute(select(Animal))).scalars()), animal)
            if found is None:
                await ctx.send(f"Aucun animal ne s'appelle « {animal} ».", ephemeral=True)
                return
            # Count then insert: two simultaneous adds could overshoot the cap
            # by one, which is harmless (nothing is spent).
            count = await session.scalar(
                select(func.count()).where(Wish.guild_id == ctx.guild.id, Wish.user_id == ctx.author.id)
            )
            if count >= MAX_WISHES:
                await ctx.send(
                    f"Ta wishlist est pleine ({MAX_WISHES} animaux max) ! Retires-en un avec `/wish remove`.",
                    ephemeral=True,
                )
                return
            result = await session.execute(
                insert(Wish)
                .values(guild_id=ctx.guild.id, user_id=ctx.author.id, animal_id=found.id)
                .on_conflict_do_nothing()
            )
            await session.commit()

        if result.rowcount == 0:
            await ctx.send(f"**{found.name}** est déjà dans ta wishlist.", ephemeral=True)
            return
        await ctx.send(
            f"Ajouté à ta wishlist : **{found.name}** ({count + 1}/{MAX_WISHES}). "
            "Tu seras mentionné quand quelqu'un d'autre le roll.",
            ephemeral=True,
        )

    @wish.command(name="remove", description="Retire un animal de ta wishlist.")
    @app_commands.describe(animal="L'animal à retirer")
    async def wish_remove(self, ctx: commands.Context, *, animal: str):
        if ctx.guild is None:
            await ctx.send("Utilise cette commande dans un serveur.", ephemeral=True)
            return
        await ctx.defer(ephemeral=True)

        found = _find(await _wishlist(ctx.guild.id, ctx.author.id), animal)
        if found is None:
            await ctx.send(f"Il n'y a aucun « {animal} » dans ta wishlist.", ephemeral=True)
            return
        async with SessionLocal() as session:
            await session.execute(
                delete(Wish).where(
                    Wish.guild_id == ctx.guild.id, Wish.user_id == ctx.author.id, Wish.animal_id == found.id
                )
            )
            await session.commit()
        await ctx.send(f"Retiré de ta wishlist : **{found.name}**.", ephemeral=True)

    @wish_add.autocomplete("animal")
    async def wish_add_autocomplete(self, interaction: discord.Interaction, current: str):
        async with SessionLocal() as session:
            names = (await session.execute(select(Animal.name).order_by(Animal.name))).scalars().all()
        return _choices(list(names), current)

    @wish_remove.autocomplete("animal")
    async def wish_remove_autocomplete(self, interaction: discord.Interaction, current: str):
        if interaction.guild is None:
            return []
        return _choices([a.name for a in await _wishlist(interaction.guild.id, interaction.user.id)], current)


async def setup(bot: commands.Bot):
    await bot.add_cog(Wishes(bot))
