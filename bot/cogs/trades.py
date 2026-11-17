import discord
from discord import app_commands
from discord.ext import commands

from ..db import SessionLocal
from ..economy import (
    MAX_TRADE_ANIMALS,
    MAX_TRADES_PER_DAY,
    TradeResult,
    TradeSide,
    execute_trade,
    get_status,
    seconds_until_trade_reset,
)
from ..models import Animal, Rescue
from .totems import _choices, _income, _label, _rescues
from .rolls import _fold, format_wait

TRADE_TIMEOUT_SECONDS = 300


# Up to MAX_TRADE_ANIMALS (3) animals per side: one option each, since a slash
# option holds a single value.
class TradeOffer(commands.FlagConverter):
    give_animal: str | None = commands.flag(default=None, description="Un animal que tu donnes")
    give_animal_2: str | None = commands.flag(default=None, description="Un 2e animal que tu donnes")
    give_animal_3: str | None = commands.flag(default=None, description="Un 3e animal que tu donnes")
    want_animal: str | None = commands.flag(default=None, description="Un animal que tu demandes")
    want_animal_2: str | None = commands.flag(default=None, description="Un 2e animal que tu demandes")
    want_animal_3: str | None = commands.flag(default=None, description="Un 3e animal que tu demandes")

    def gives(self) -> list[str]:
        return [q for q in (self.give_animal, self.give_animal_2, self.give_animal_3) if q]

    def wants(self) -> list[str]:
        return [q for q in (self.want_animal, self.want_animal_2, self.want_animal_3) if q]


GIVE_OPTIONS = ("give_animal", "give_animal_2", "give_animal_3")
WANT_OPTIONS = ("want_animal", "want_animal_2", "want_animal_3")
assert len(GIVE_OPTIONS) == len(WANT_OPTIONS) == MAX_TRADE_ANIMALS


def _pick_copy(rows: list[tuple[Rescue, Animal]], query: str, taken: set[int]) -> tuple[Rescue, Animal] | None:
    """The copy `query` names, other than those already `taken` in this offer.
    Autocomplete sends "#<rescue id>"; a typed name picks the weakest copy,
    preferring one outside the reserve."""
    if query.startswith("#") and query[1:].isdigit():
        return next((row for row in rows if row[0].id == int(query[1:]) and row[0].id not in taken), None)
    needle = _fold(query)
    matches = [row for row in rows if _fold(row[1].name) == needle and row[0].id not in taken]
    return min(matches, key=lambda row: (row[0].in_reserve, _income(row))) if matches else None


def _describe(rows: list[tuple[Rescue, Animal]]) -> str:
    return "\n".join(f"**{_label(row)}**" for row in rows) or "Rien"


def _taken_in_options(interaction: discord.Interaction, options: tuple[str, ...]) -> set[int]:
    """Copies already picked by autocomplete in the side's other options."""
    taken = set()
    for name in options:
        value = getattr(interaction.namespace, name, None)
        if isinstance(value, str) and value.startswith("#") and value[1:].isdigit():
            taken.add(int(value[1:]))
    return taken


class TradeView(discord.ui.View):
    """Accept/refuse buttons under an offer. The offer is fixed once posted, so
    the target always accepts exactly what they read."""

    def __init__(self, guild_id: int, author: discord.abc.User, target: discord.abc.User, give: TradeSide, want: TradeSide):
        super().__init__(timeout=TRADE_TIMEOUT_SECONDS)
        self.guild_id = guild_id
        self.author = author
        self.target = target
        self.give = give
        self.want = want
        self.message: discord.Message | None = None
        self.settled = False

    def _close(self) -> None:
        for item in self.children:
            item.disabled = True
        self.stop()

    def _announcement(self, result: TradeResult) -> str:
        message = f"Échange conclu entre {self.author.mention} et {self.target.mention} !"
        for member in (self.author, self.target):
            for collection in result.unlocked[member.id]:
                message += f"\n{member.mention} complète la collection **{collection.label}** (+{collection.bonus} % de revenu des totems)"
            for collection in result.lost[member.id]:
                message += f"\n{member.mention} perd la collection **{collection.label}** (-{collection.bonus} % de revenu des totems)"
            for milestone in result.milestones_unlocked[member.id]:
                message += f"\n{member.mention} complète le milestone {milestone.badge} **{milestone.label}** !"
            for milestone in result.milestones_lost[member.id]:
                message += f"\n{member.mention} perd le milestone **{milestone.label}**"
        return message

    def _failure_text(self, result: TradeResult) -> str:
        who = f"<@{result.failed_user_id}>"
        if result.failure == "reserve":
            return f"Échange impossible : un animal de {who} est un de ses totems (`/totem remove`). L'offre reste ouverte."
        if result.failure == "limit":
            return (
                f"Échange impossible : {who} a déjà fait ses {MAX_TRADES_PER_DAY} échanges du jour "
                f"(recharge dans {format_wait(seconds_until_trade_reset())}). L'offre reste ouverte."
            )
        return f"Échange annulé : un animal de {who} ne lui appartient plus."

    @discord.ui.button(label="Accepter", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target.id:
            await interaction.response.send_message(f"Seul·e {self.target.mention} peut accepter cette offre.", ephemeral=True)
            return
        # Flag set before any await, like RescueView: only the first click passes.
        if self.settled:
            await interaction.response.send_message("Cet échange est déjà en cours.", ephemeral=True)
            return
        self.settled = True
        async with SessionLocal() as session:
            result = await execute_trade(session, self.guild_id, self.give, self.want)
            if result.failure is None:
                await session.commit()

        if result.failure is not None:
            # A moved animal can't come back into this offer; the others can be fixed.
            self.settled = result.failure == "moved"
            if self.settled:
                self._close()
                await interaction.response.edit_message(
                    content=self._failure_text(result), view=self, allowed_mentions=discord.AllowedMentions.none()
                )
            else:
                await interaction.response.send_message(
                    self._failure_text(result), ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
                )
            return

        self._close()
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(self._announcement(result), allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="Refuser / Annuler", style=discord.ButtonStyle.secondary)
    async def refuse(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in (self.author.id, self.target.id):
            await interaction.response.send_message("Cette offre ne te concerne pas.", ephemeral=True)
            return
        if self.settled:
            await interaction.response.send_message("Cet échange est déjà en cours.", ephemeral=True)
            return
        self.settled = True
        self._close()
        await interaction.response.edit_message(
            content=f"{interaction.user.mention} a annulé l'offre d'échange.",
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(content="Cette offre d'échange a expiré.", view=self)
            except discord.HTTPException:
                pass


class Trades(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(
        name="trade",
        description="Propose un échange d'animaux, ou un cadeau, à un autre joueur.",
    )
    @app_commands.describe(member="Le joueur avec qui échanger")
    @app_commands.guild_only()
    async def trade(self, ctx: commands.Context, member: discord.Member, *, offer: TradeOffer):
        if ctx.guild is None:
            await ctx.send("Utilise cette commande dans un serveur.")
            return
        if member.bot or member.id == ctx.author.id:
            await ctx.send("Choisis un autre joueur.")
            return
        if not (offer.gives() or offer.wants()):
            await ctx.send(
                f"Indique ce que tu donnes ou demandes (jusqu'à {MAX_TRADE_ANIMALS} animaux de chaque côté) : "
                "`give_animal`, `want_animal`.",
            )
            return
        await ctx.defer()

        async with SessionLocal() as session:
            author_status = await get_status(session, ctx.guild.id, ctx.author.id)
            member_status = await get_status(session, ctx.guild.id, member.id)
        if author_status.trades_left <= 0 or member_status.trades_left <= 0:
            who = "Tu as" if author_status.trades_left <= 0 else f"{member.mention} a"
            await ctx.send(
                f"{who} déjà fait {MAX_TRADES_PER_DAY} échanges aujourd'hui "
                f"(recharge dans {format_wait(seconds_until_trade_reset())}).",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        given: list[tuple[Rescue, Animal]] = []
        if offer.gives():
            rows = await _rescues(ctx.guild.id, ctx.author.id)
            for query in offer.gives():
                row = _pick_copy(rows, query, {r.id for r, _ in given})
                if row is None:
                    other = " autre" if any(_fold(a.name) == _fold(query) for _, a in given) else ""
                    await ctx.send(f"Tu n'as aucun{other} « {query} ».")
                    return
                if row[0].in_reserve:
                    await ctx.send(f"**{row[1].name}** est un de tes totems : retire-le d'abord avec `/totem remove`.")
                    return
                given.append(row)
        wanted: list[tuple[Rescue, Animal]] = []
        if offer.wants():
            rows = await _rescues(ctx.guild.id, member.id)
            for query in offer.wants():
                row = _pick_copy(rows, query, {r.id for r, _ in wanted})
                if row is None:
                    other = " autre" if any(_fold(a.name) == _fold(query) for _, a in wanted) else ""
                    await ctx.send(
                        f"{member.mention} n'a aucun{other} « {query} ».",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                if row[0].in_reserve:
                    await ctx.send(
                        f"**{row[1].name}** est un totem de {member.mention}, qui doit d'abord le retirer.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                wanted.append(row)

        embed = discord.Embed(title="Offre d'échange", color=discord.Color.teal())
        embed.add_field(name=f"{ctx.author.display_name} donne", value=_describe(given))
        embed.add_field(name=f"{member.display_name} donne", value=_describe(wanted))
        embed.set_footer(text="Céder le dernier exemplaire d'une espèce peut faire perdre une collection ou un milestone.")
        view = TradeView(
            ctx.guild.id,
            ctx.author,
            member,
            TradeSide(ctx.author.id, tuple(r.id for r, _ in given)),
            TradeSide(member.id, tuple(r.id for r, _ in wanted)),
        )
        view.message = await ctx.send(
            member.mention,
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions(users=[member]),
        )

    async def _give_choices(self, interaction: discord.Interaction, current: str, option: str):
        if interaction.guild is None:
            return []
        taken = _taken_in_options(interaction, tuple(o for o in GIVE_OPTIONS if o != option))
        rows = [row for row in await _rescues(interaction.guild.id, interaction.user.id) if row[0].id not in taken]
        return _choices(rows, current, in_reserve=False)

    async def _want_choices(self, interaction: discord.Interaction, current: str, option: str):
        member = interaction.namespace.member
        if interaction.guild is None or member is None:
            return []
        taken = _taken_in_options(interaction, tuple(o for o in WANT_OPTIONS if o != option))
        rows = [row for row in await _rescues(interaction.guild.id, member.id) if row[0].id not in taken]
        return _choices(rows, current, in_reserve=False)

    # One callback per option (autocomplete needs one per parameter); each
    # leaves out the copies already picked in the side's other options.
    @trade.autocomplete("give_animal")
    async def give_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._give_choices(interaction, current, "give_animal")

    @trade.autocomplete("give_animal_2")
    async def give_autocomplete_2(self, interaction: discord.Interaction, current: str):
        return await self._give_choices(interaction, current, "give_animal_2")

    @trade.autocomplete("give_animal_3")
    async def give_autocomplete_3(self, interaction: discord.Interaction, current: str):
        return await self._give_choices(interaction, current, "give_animal_3")

    @trade.autocomplete("want_animal")
    async def want_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._want_choices(interaction, current, "want_animal")

    @trade.autocomplete("want_animal_2")
    async def want_autocomplete_2(self, interaction: discord.Interaction, current: str):
        return await self._want_choices(interaction, current, "want_animal_2")

    @trade.autocomplete("want_animal_3")
    async def want_autocomplete_3(self, interaction: discord.Interaction, current: str):
        return await self._want_choices(interaction, current, "want_animal_3")

async def setup(bot: commands.Bot):
    await bot.add_cog(Trades(bot))
