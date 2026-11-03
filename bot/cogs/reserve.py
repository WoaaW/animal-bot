import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from ..db import SessionLocal
from ..economy import (
    CURRENCY_NAME,
    RESERVE_CAP_SECONDS,
    collect_reserve,
    get_reserve_status,
    place_in_reserve,
    remove_from_reserve,
    reserve_income,
)
from ..models import Animal, Rescue
from .rolls import _fold, format_appeal, format_rate, format_reserve_bonus, format_wait, paginate, send_pages


async def _rescues(guild_id: int, user_id: int) -> list[tuple[Rescue, Animal]]:
    async with SessionLocal() as session:
        stmt = (
            select(Rescue, Animal)
            .join(Animal, Animal.id == Rescue.animal_id)
            .where(Rescue.guild_id == guild_id, Rescue.user_id == user_id)
            .order_by(Rescue.rescued_at)
        )
        return [tuple(row) for row in (await session.execute(stmt)).all()]


def _income(row: tuple[Rescue, Animal]) -> int:
    rescue, animal = row
    return reserve_income(animal.rarity, rescue.appeal)


def _label(row: tuple[Rescue, Animal]) -> str:
    rescue, animal = row
    return f"{animal.name} {format_appeal(rescue.appeal)} · {_income(row)} {CURRENCY_NAME}/h"


def _pick(rows: list[tuple[Rescue, Animal]], query: str, *, in_reserve: bool) -> tuple[Rescue, Animal] | None:
    """The rescue `query` names among those in (or out of) the reserve.
    Autocomplete sends "#<rescue id>"; a typed name picks the best copy to
    put in, or the weakest one to take out."""
    rows = [row for row in rows if row[0].in_reserve == in_reserve]
    if query.startswith("#") and query[1:].isdigit():
        return next((row for row in rows if row[0].id == int(query[1:])), None)
    needle = _fold(query)
    matches = [row for row in rows if _fold(row[1].name) == needle]
    if not matches:
        return None
    return max(matches, key=_income) if not in_reserve else min(matches, key=_income)


def _choices(rows: list[tuple[Rescue, Animal]], current: str, *, in_reserve: bool) -> list[app_commands.Choice[str]]:
    needle = _fold(current)
    rows = [row for row in rows if row[0].in_reserve == in_reserve and needle in _fold(row[1].name)]
    rows.sort(key=_income, reverse=True)
    # Discord caps autocomplete at 25 choices and their names at 100 chars.
    return [app_commands.Choice(name=_label(row)[:100], value=f"#{row[0].id}") for row in rows[:25]]


class Reserve(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # A slash group can't be run by itself, so fallback="view" makes it
    # /reserve view (and plain !reserve on the prefix side).
    @commands.hybrid_group(
        name="reserve",
        fallback="view",
        description=f"Ta réserve : les animaux exposés y rapportent des {CURRENCY_NAME} chaque heure.",
    )
    async def reserve(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send("Utilise cette commande dans un serveur.", ephemeral=True)
            return
        await ctx.defer()

        # Opening the reserve collects what it earned.
        async with SessionLocal() as session:
            paid = await collect_reserve(session, ctx.guild.id, ctx.author.id)
            await session.commit()
            status = await get_reserve_status(session, ctx.guild.id, ctx.author.id)
        rows = [row for row in await _rescues(ctx.guild.id, ctx.author.id) if row[0].in_reserve]

        rows.sort(key=_income, reverse=True)
        lines = [f"**{animal.name}** {format_appeal(rescue.appeal)} · {_income((rescue, animal))} {CURRENCY_NAME}/h"
                 for rescue, animal in rows]
        if not lines:
            lines = ["Vide ! Expose tes animaux sauvés avec `/reserve add`."]
        title = f"Réserve de {ctx.author.display_name} ({status.used}/{status.max_slots}) · {format_rate(status.rate)} {CURRENCY_NAME}/h"
        title += format_reserve_bonus(status)
        pages = paginate(title, lines, discord.Color.dark_green())
        for page in pages:
            page.add_field(
                name="​",
                value=f"-# Les visiteurs paient pendant {format_wait(RESERVE_CAP_SECONDS)} au maximum, puis attendent que tu encaisses.",
                inline=False,
            )
        content = f"Tu as encaissé **{paid}** {CURRENCY_NAME} grâce à ta réserve !" if paid else None
        await send_pages(ctx, pages, content=content)

    @reserve.command(name="add", description="Expose un de tes animaux sauvés dans ta réserve.")
    @app_commands.describe(animal="L'animal à exposer")
    async def reserve_add(self, ctx: commands.Context, *, animal: str):
        if ctx.guild is None:
            await ctx.send("Utilise cette commande dans un serveur.", ephemeral=True)
            return
        await ctx.defer(ephemeral=True)

        row = _pick(await _rescues(ctx.guild.id, ctx.author.id), animal, in_reserve=False)
        if row is None:
            await ctx.send(f"Tu n'as aucun « {animal} » hors de ta réserve.", ephemeral=True)
            return
        async with SessionLocal() as session:
            placed = await place_in_reserve(session, ctx.guild.id, ctx.author.id, row[0].id)
            await session.commit()
            status = await get_reserve_status(session, ctx.guild.id, ctx.author.id)

        if not placed:
            await ctx.send(
                f"Ta réserve est pleine ({status.max_slots} animaux max) ! "
                "Retires-en un avec `/reserve remove`, ou achète plus de place dans le `/shop`.",
                ephemeral=True,
            )
            return
        await ctx.send(
            f"Dans ta réserve : **{_label(row)}**. Elle rapporte {format_rate(status.rate)} {CURRENCY_NAME}/h "
            f"({status.used}/{status.max_slots}).",
            ephemeral=True,
        )

    @reserve.command(name="remove", description="Retire un animal de ta réserve.")
    @app_commands.describe(animal="L'animal à retirer")
    async def reserve_remove(self, ctx: commands.Context, *, animal: str):
        if ctx.guild is None:
            await ctx.send("Utilise cette commande dans un serveur.", ephemeral=True)
            return
        await ctx.defer(ephemeral=True)

        row = _pick(await _rescues(ctx.guild.id, ctx.author.id), animal, in_reserve=True)
        if row is None:
            await ctx.send(f"Il n'y a aucun « {animal} » dans ta réserve.", ephemeral=True)
            return
        async with SessionLocal() as session:
            removed = await remove_from_reserve(session, ctx.guild.id, ctx.author.id, row[0].id)
            await session.commit()
            status = await get_reserve_status(session, ctx.guild.id, ctx.author.id)

        if not removed:
            await ctx.send(f"Il n'y a aucun « {animal} » dans ta réserve.", ephemeral=True)
            return
        await ctx.send(
            f"Retour au sanctuaire : **{row[1].name}**. Ta réserve rapporte {format_rate(status.rate)} {CURRENCY_NAME}/h "
            f"({status.used}/{status.max_slots}).",
            ephemeral=True,
        )

    @reserve_add.autocomplete("animal")
    async def reserve_add_autocomplete(self, interaction: discord.Interaction, current: str):
        if interaction.guild is None:
            return []
        return _choices(await _rescues(interaction.guild.id, interaction.user.id), current, in_reserve=False)

    @reserve_remove.autocomplete("animal")
    async def reserve_remove_autocomplete(self, interaction: discord.Interaction, current: str):
        if interaction.guild is None:
            return []
        return _choices(await _rescues(interaction.guild.id, interaction.user.id), current, in_reserve=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Reserve(bot))
