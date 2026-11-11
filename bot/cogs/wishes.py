import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert

from ..db import SessionLocal
from ..models import Animal, CollectionWish, Rescue, Wish
from ..species_collections import COLLECTION_TARGET, COLLECTIONS, Collection, progress, rescued_species
from .rolls import RARITY_LABEL, RARITY_ORDER, _fold, paginate, send_pages

# Wishlist size per player and guild. A long list would ping on most rolls.
MAX_WISHES = 10
# Wished collections, counted apart: one can cover dozens of species.
MAX_COLLECTION_WISHES = 3


async def _wished_collections(guild_id: int, user_id: int) -> list[Collection]:
    async with SessionLocal() as session:
        labels = set(
            (
                await session.execute(
                    select(CollectionWish.collection).where(
                        CollectionWish.guild_id == guild_id, CollectionWish.user_id == user_id
                    )
                )
            ).scalars()
        )
    # COLLECTIONS order; a label no longer in COLLECTIONS is dropped.
    return [c for c in COLLECTIONS if c.label in labels]


def _find_collection(collections: list[Collection], query: str) -> Collection | None:
    needle = _fold(query)
    return next((c for c in collections if _fold(c.label) == needle), None)


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
            await ctx.send("Utilise cette commande dans un serveur.")
            return
        await ctx.defer()

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
        content = None
        collections = await _wished_collections(ctx.guild.id, ctx.author.id)
        if collections:
            async with SessionLocal() as session:
                species = await rescued_species(session, ctx.guild.id, ctx.author.id)
            shown = ", ".join(
                f"**{c.label}** ({min(progress(species, c), COLLECTION_TARGET)}/{COLLECTION_TARGET})"
                for c in collections
            )
            content = f"📚 Collections souhaitées ({len(collections)}/{MAX_COLLECTION_WISHES}) : {shown}"
        await send_pages(ctx, paginate(title, lines, discord.Color.gold()), content=content)

    @wish.command(name="add", description="Ajoute un animal à ta wishlist.")
    @app_commands.describe(animal="L'animal à souhaiter")
    async def wish_add(self, ctx: commands.Context, *, animal: str):
        if ctx.guild is None:
            await ctx.send("Utilise cette commande dans un serveur.")
            return
        await ctx.defer()

        async with SessionLocal() as session:
            found = _find(list((await session.execute(select(Animal))).scalars()), animal)
            if found is None:
                await ctx.send(f"Aucun animal ne s'appelle « {animal} ».")
                return
            # Count then insert: two simultaneous adds could overshoot the cap
            # by one, which is harmless (nothing is spent).
            count = await session.scalar(
                select(func.count()).where(Wish.guild_id == ctx.guild.id, Wish.user_id == ctx.author.id)
            )
            if count >= MAX_WISHES:
                await ctx.send(
                    f"Ta wishlist est pleine ({MAX_WISHES} animaux max) ! Retires-en un avec `/wish remove`.",
                )
                return
            result = await session.execute(
                insert(Wish)
                .values(guild_id=ctx.guild.id, user_id=ctx.author.id, animal_id=found.id)
                .on_conflict_do_nothing()
            )
            await session.commit()

        if result.rowcount == 0:
            await ctx.send(f"**{found.name}** est déjà dans ta wishlist.")
            return
        await ctx.send(
            f"Ajouté à ta wishlist : **{found.name}** ({count + 1}/{MAX_WISHES}). "
            "Tu seras mentionné quand quelqu'un d'autre le roll.",
        )

    @wish.command(name="remove", description="Retire un animal de ta wishlist.")
    @app_commands.describe(animal="L'animal à retirer")
    async def wish_remove(self, ctx: commands.Context, *, animal: str):
        if ctx.guild is None:
            await ctx.send("Utilise cette commande dans un serveur.")
            return
        await ctx.defer()

        found = _find(await _wishlist(ctx.guild.id, ctx.author.id), animal)
        if found is None:
            await ctx.send(f"Il n'y a aucun « {animal} » dans ta wishlist.")
            return
        async with SessionLocal() as session:
            await session.execute(
                delete(Wish).where(
                    Wish.guild_id == ctx.guild.id, Wish.user_id == ctx.author.id, Wish.animal_id == found.id
                )
            )
            await session.commit()
        await ctx.send(f"Retiré de ta wishlist : **{found.name}**.")

    @wish.command(
        name="addcollection",
        description="Souhaite une collection : tu es mentionné quand on roll une de ses espèces qui te manque.",
    )
    @app_commands.describe(collection="La collection à souhaiter")
    async def wish_add_collection(self, ctx: commands.Context, *, collection: str):
        if ctx.guild is None:
            await ctx.send("Utilise cette commande dans un serveur.")
            return
        found = _find_collection(COLLECTIONS, collection)
        if found is None:
            await ctx.send(f"Aucune collection ne s'appelle « {collection} ».")
            return
        await ctx.defer()

        async with SessionLocal() as session:
            # Count then insert, like /wish add: overshooting by one is harmless.
            count = await session.scalar(
                select(func.count()).where(
                    CollectionWish.guild_id == ctx.guild.id, CollectionWish.user_id == ctx.author.id
                )
            )
            if count >= MAX_COLLECTION_WISHES:
                await ctx.send(
                    f"Tu souhaites déjà {MAX_COLLECTION_WISHES} collections, le maximum ! "
                    "Retires-en une avec `/wish removecollection`."
                )
                return
            result = await session.execute(
                insert(CollectionWish)
                .values(guild_id=ctx.guild.id, user_id=ctx.author.id, collection=found.label)
                .on_conflict_do_nothing()
            )
            await session.commit()

        if result.rowcount == 0:
            await ctx.send(f"Tu souhaites déjà la collection **{found.label}**.")
            return
        await ctx.send(
            f"Collection souhaitée : **{found.label}** ({count + 1}/{MAX_COLLECTION_WISHES}). "
            "Tu seras mentionné quand quelqu'un d'autre roll une de ses espèces que tu n'as pas encore.",
        )

    @wish.command(name="removecollection", description="Ne souhaite plus une collection.")
    @app_commands.describe(collection="La collection à retirer")
    async def wish_remove_collection(self, ctx: commands.Context, *, collection: str):
        if ctx.guild is None:
            await ctx.send("Utilise cette commande dans un serveur.")
            return
        await ctx.defer()

        found = _find_collection(await _wished_collections(ctx.guild.id, ctx.author.id), collection)
        if found is None:
            await ctx.send(f"Tu ne souhaites aucune collection « {collection} ».")
            return
        async with SessionLocal() as session:
            await session.execute(
                delete(CollectionWish).where(
                    CollectionWish.guild_id == ctx.guild.id,
                    CollectionWish.user_id == ctx.author.id,
                    CollectionWish.collection == found.label,
                )
            )
            await session.commit()
        await ctx.send(f"Collection retirée de ta wishlist : **{found.label}**.")

    @wish_add_collection.autocomplete("collection")
    async def wish_add_collection_autocomplete(self, interaction: discord.Interaction, current: str):
        return _choices([c.label for c in COLLECTIONS], current)

    @wish_remove_collection.autocomplete("collection")
    async def wish_remove_collection_autocomplete(self, interaction: discord.Interaction, current: str):
        if interaction.guild is None:
            return []
        return _choices([c.label for c in await _wished_collections(interaction.guild.id, interaction.user.id)], current)

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
