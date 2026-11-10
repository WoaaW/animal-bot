import random
import re
import time
import unicodedata
from datetime import timezone

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from .. import milestones
from ..species_collections import COLLECTION_TARGET, COLLECTIONS, completed, progress, rescued_species
from ..db import SessionLocal
from ..economy import (
    APPEAL,
    CURRENCY_NAME,
    MILESTONE_POINTS_BONUS,
    RARITY_POINTS,
    RESERVE_BASE_INCOME,
    ReserveStatus,
    add_points,
    bank_reserve,
    consume_claim,
    consume_roll,
    get_status,
    milestone_points,
    reserve_income,
    roll_appeal,
    seconds_until_reset,
)
from ..models import Animal, Rescue, Wish

# Base weight of each tier, in % of the full table. A roll picks the tier
# first, then an animal uniformly within it, so the odds don't depend on how
# many animals a tier holds. rarity_odds() turns these into a player's odds.
# "mythic" is the hidden tier above legendary, reserved for extinct species.
RARITY_ODDS = {"common": 60, "uncommon": 25, "rare": 10, "epic": 4, "legendary": 0.7, "mythic": 0.1}
RARITY_COLOR = {
    "common": discord.Color.light_grey(),
    "uncommon": discord.Color.green(),
    "rare": discord.Color.blue(),
    "epic": discord.Color.purple(),
    "legendary": discord.Color.gold(),
    "mythic": discord.Color.dark_red(),
}
# What players see; the English keys are only identifiers.
RARITY_LABEL = {
    "common": "commun",
    "uncommon": "peu commun",
    "rare": "rare",
    "epic": "épique",
    "legendary": "légendaire",
    "mythic": "mythique",
}
assert RARITY_LABEL.keys() == RARITY_ODDS.keys(), "every rarity needs a label"
assert RARITY_POINTS.keys() == RARITY_ODDS.keys(), "every rarity needs a point value"
assert RESERVE_BASE_INCOME.keys() == RARITY_ODDS.keys(), "every rarity needs a reserve income"
# Shop luck level needed to roll each tier: everything but the hidden mythic
# tier is rollable from the start.
RARITY_UNLOCK_LEVEL = {"common": 0, "uncommon": 0, "rare": 0, "epic": 0, "legendary": 0, "mythic": 5}
# Each luck level adds LUCK_STEP × rank to a tier's weight multiplier, so the
# rarer the tier, the faster it grows: at level 10, legendary is ×3 and
# mythic ×3.5, while common never moves (its share shrinks as others grow).
LUCK_STEP = 0.05
assert RARITY_UNLOCK_LEVEL.keys() == RARITY_ODDS.keys()
# Rarity follows the IUCN Red List: the more threatened in the wild, the
# rarer the roll; extinct species get the hidden "mythic" tier. DD (data
# deficient) has no rarity, so those animals need an explicit "rarity" in
# animals.json.
IUCN_RARITY = {
    "LC": "common",
    "NT": "uncommon",
    "VU": "rare",
    "EN": "epic",
    "CR": "legendary",
    "EW": "legendary",
    "EX": "mythic",
}
IUCN_LABEL = {
    "LC": "Préoccupation mineure",
    "NT": "Quasi menacée",
    "VU": "Vulnérable",
    "EN": "En danger",
    "CR": "En danger critique",
    "EW": "Éteinte à l'état sauvage",
    "EX": "Éteinte",
    "DD": "Données insuffisantes",
}


# A roll's buttons stay up ROLL_TIMEOUT_SECONDS. For the first
# ROLL_PROTECTION_SECONDS, only the roller can rescue; then anyone can.
ROLL_TIMEOUT_SECONDS = 120
ROLL_PROTECTION_SECONDS = 60
assert ROLL_PROTECTION_SECONDS < ROLL_TIMEOUT_SECONDS, "others must get a chance to rescue"

EMBED_DESCRIPTION_LIMIT = 4096
# Split after . ! ? when the next sentence starts with a capital or a quote,
# so "env. 250" or "J.-C. en" don't count as sentence ends.
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-ZÀ-ÖØ-Ý«])")


def rarity_odds(luck_level: int) -> dict[str, float]:
    """A player's chance (in %) of rolling each tier they've unlocked."""
    weights = {
        tier: base * (1 + LUCK_STEP * rank * luck_level)
        for rank, (tier, base) in enumerate(RARITY_ODDS.items())
        if luck_level >= RARITY_UNLOCK_LEVEL[tier]
    }
    total = sum(weights.values())
    return {tier: w / total * 100 for tier, w in weights.items()}


def _weighted_pick(animals: list[Animal], odds: dict[str, float]) -> Animal:
    # Tiers that are locked (absent from odds) or hold no animal are left
    # out, so their share spreads over the others.
    by_rarity: dict[str, list[Animal]] = {}
    for animal in animals:
        if animal.rarity in odds:
            by_rarity.setdefault(animal.rarity, []).append(animal)
    tiers = list(by_rarity)
    weights = [odds[r] for r in tiers]
    tier = random.choices(tiers, weights=weights, k=1)[0]
    return random.choice(by_rarity[tier])


def _first_sentences(text: str, count: int = 2) -> str:
    first_paragraph = text.split("\n", 1)[0]
    return " ".join(SENTENCE_END.split(first_paragraph)[:count])


def format_wait(seconds: int) -> str:
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d}" if minutes else f"{hours} h"


def rarity_initials(rarity: str) -> str:
    # "peu commun" → "PC", "épique" → "E" (accents dropped).
    return "".join(word[0] for word in _fold(RARITY_LABEL[rarity]).split()).upper()


def format_rate(rate: float) -> str:
    # Points per hour: "45", or "6,2" when an collection bonus adds a fraction.
    return f"{rate:.0f}" if rate == int(rate) else f"{rate:.1f}".replace(".", ",")


def format_reserve_bonus(status: ReserveStatus) -> str:
    # " (dont +9 % de collections, +25 % de milestones)", or nothing without bonus.
    parts = []
    if status.bonus:
        parts.append(f"+{status.bonus} % de collections")
    if status.milestones:
        parts.append(f"+{status.milestones} % de milestones")
    return f" (dont {', '.join(parts)})" if parts else ""


def format_appeal(appeal: int) -> str:
    return "★" * appeal + "☆" * (len(APPEAL) - appeal)


def _fold(text: str) -> str:
    # Case- and accent-insensitive key, so "herisson" matches "Hérisson".
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def animal_header(animal: Animal) -> str:
    header = (
        f"*{animal.scientific_name}* — **{RARITY_LABEL[animal.rarity]}**"
        f" · {RARITY_POINTS[animal.rarity]} {CURRENCY_NAME}"
    )
    if animal.iucn_status:
        header += f"\nStatut UICN : {IUCN_LABEL[animal.iucn_status]} ({animal.iucn_status})"
    return header


def animal_embed(animal: Animal, *, full: bool) -> discord.Embed:
    header = animal_header(animal)
    text = animal.description if full else _first_sentences(animal.description)
    description = f"{header}\n\n{text}" if text else header
    if len(description) > EMBED_DESCRIPTION_LIMIT:
        description = description[: EMBED_DESCRIPTION_LIMIT - 1] + "…"

    embed = discord.Embed(
        title=animal.name,
        url=animal.wiki_url,
        description=description,
        color=RARITY_COLOR[animal.rarity],
    )
    if animal.image_url:
        embed.set_image(url=animal.image_url)
    if animal.wiki_url:
        # Wikipedia text is CC BY-SA: the title link plus this line credit it.
        embed.set_footer(text="Source : Wikipédia")
    return embed


def add_collections_field(embed: discord.Embed, animal: Animal, species: list[Animal] | None) -> None:
    """The collection groups `animal` counts for, with the progress of the
    player owning `species` in each (✓ once complete). No progress shown
    when `species` is None (no player to measure, e.g. outside a guild)."""
    lines = []
    for collection in COLLECTIONS:
        if not collection.includes(animal):
            continue
        if species is None:
            lines.append(collection.label)
            continue
        count = progress(species, collection)
        done = " ✓" if count >= COLLECTION_TARGET else ""
        lines.append(f"{collection.label} · {min(count, COLLECTION_TARGET)}/{COLLECTION_TARGET}{done}")
    if lines:
        embed.add_field(name="Collections", value="\n".join(lines), inline=False)


class RescueView(discord.ui.View):
    """Buttons under a roll. Any animal can be rescued, even one already in
    someone's reserve (or the clicker's own), but only once per roll, and
    only by the roller during the first ROLL_PROTECTION_SECONDS."""

    def __init__(self, animal: Animal, guild_id: int, appeal: int, roller_id: int):
        super().__init__(timeout=ROLL_TIMEOUT_SECONDS)
        self.animal = animal
        self.guild_id = guild_id
        self.appeal = appeal
        self.roller_id = roller_id
        # Until then, only the roller can rescue.
        self.protected_until = int(time.time()) + ROLL_PROTECTION_SECONDS
        self.message: discord.Message | None = None
        self.rescued = False

    @discord.ui.button(label="Sauver", style=discord.ButtonStyle.primary)
    async def rescue(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.roller_id and time.time() < self.protected_until:
            await interaction.response.send_message(
                f"Cet animal est réservé à <@{self.roller_id}> pour l'instant. "
                f"Tu pourras le sauver <t:{self.protected_until}:R>.",
                ephemeral=True,
            )
            return
        # Flag set before any await: the event loop runs one callback at a
        # time, so only the first click gets past this check.
        if self.rescued:
            await interaction.response.send_message(
                f"Quelqu'un est déjà en train de sauver cet animal ({self.animal.name}) !", ephemeral=True
            )
            return
        self.rescued = True
        async with SessionLocal() as session:
            claim = await consume_claim(session, self.guild_id, interaction.user.id)
            if claim is None:
                # Nothing was rescued: let someone else (with a claim) try.
                self.rescued = False
                status = await get_status(session, self.guild_id, interaction.user.id)
                await interaction.response.send_message(
                    f"Tu n'as pas de claim disponible ! Le prochain arrive dans {format_wait(status.next_claim_in)}.",
                    ephemeral=True,
                )
                return
            # This rescue may complete a collection, which raises reserve
            # income: bank what was earned at the old rate first.
            await bank_reserve(session, self.guild_id, interaction.user.id)
            before = await rescued_species(session, self.guild_id, interaction.user.id)
            pool = (await session.execute(select(Animal))).scalars().all()
            session.add(
                Rescue(
                    guild_id=self.guild_id,
                    user_id=interaction.user.id,
                    animal_id=self.animal.id,
                    appeal=self.appeal,
                )
            )
            await session.commit()
        # A duplicate adds no species, so it can't complete anything.
        if any(a.id == self.animal.id for a in before):
            unlocked = []
            reached = []
        else:
            done_before = completed(before)
            unlocked = [a for a in completed([*before, self.animal]) if a not in done_before]
            reached_before = milestones.completed(before, pool)
            reached = [m for m in milestones.completed([*before, self.animal], pool) if m not in reached_before]

        button.disabled = True
        button.label = f"Sauvetage par {interaction.user.display_name}"
        # Don't stop the view: "En savoir plus" stays usable until the timeout.
        await interaction.response.edit_message(view=self)
        # Public announcement. The mention shows the name without pinging.
        message = (
            f"{interaction.user.mention} a sauvé **{self.animal.name}** · "
            f"{rarity_initials(self.animal.rarity)} ({format_appeal(self.appeal)}) !"
        )
        for collection in unlocked:
            message += f"\nCollection complétée : **{collection.label}** (+{collection.bonus} % de revenu des totems)"
        for milestone in reached:
            message += (
                f"\nMilestone complété : {milestone.badge} **{milestone.label}** "
                f"(+{MILESTONE_POINTS_BONUS} % de {CURRENCY_NAME}) !"
            )
        await interaction.followup.send(message, allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="En savoir plus", style=discord.ButtonStyle.secondary)
    async def more(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=animal_embed(self.animal, full=True), ephemeral=True
        )

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        # Without this edit the buttons still look clickable after the view
        # dies, and clicks fail with "This interaction failed".
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


PAGE_SIZE = 20
# Rescuers named per animal in /animals; the rest are just counted.
MAX_RESCUERS_SHOWN = 3
# Copies of one animal whose appeal /reserve lists; the rest are just counted.
MAX_COPIES_SHOWN = 5
# Rarest first in listings.
RARITY_ORDER = {tier: i for i, tier in enumerate(reversed(RARITY_ODDS))}


def paginate(title: str, lines: list[str], color: discord.Color) -> list[discord.Embed]:
    # Embed descriptions cap at 4096 chars, so long lists need pages.
    chunks = [lines[i : i + PAGE_SIZE] for i in range(0, len(lines), PAGE_SIZE)] or [[]]
    return _pages(title, chunks, color)


def paginate_sections(title: str, sections: list[tuple[str, list[str]]], color: discord.Color) -> list[discord.Embed]:
    """Like paginate, with a header line before each section. A section
    cut by a page break gets its header again, marked "(suite)", at the top
    of the next page. Headers count toward PAGE_SIZE."""
    chunks: list[list[str]] = [[]]
    for header, lines in sections:
        for i, line in enumerate(lines):
            if len(chunks[-1]) >= PAGE_SIZE - (1 if i == 0 else 0):
                chunks.append([])
            if i == 0:
                chunks[-1].append(header)
            elif not chunks[-1]:
                chunks[-1].append(f"{header} (suite)")
            chunks[-1].append(line)
    return _pages(title, chunks, color)


def _pages(title: str, chunks: list[list[str]], color: discord.Color) -> list[discord.Embed]:
    pages = []
    for number, chunk in enumerate(chunks, 1):
        embed = discord.Embed(title=title, description="\n".join(chunk), color=color)
        if len(chunks) > 1:
            embed.set_footer(text=f"Page {number}/{len(chunks)}")
        pages.append(embed)
    return pages


class PaginatorView(discord.ui.View):
    def __init__(self, pages: list[discord.Embed], owner_id: int):
        super().__init__(timeout=180)
        self.pages = pages
        self.owner_id = owner_id
        self.index = 0
        self.message: discord.Message | None = None
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.first.disabled = self.previous.disabled = self.index == 0
        self.last.disabled = self.next.disabled = self.index == len(self.pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # With a prefix command the message is public: don't let others flip it.
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Lance la commande toi-même pour feuilleter !", ephemeral=True)
            return False
        return True

    async def _show(self, interaction: discord.Interaction, index: int) -> None:
        self.index = index % len(self.pages)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary)
    async def first(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show(interaction, 0)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.primary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show(interaction, self.index - 1)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show(interaction, self.index + 1)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def last(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show(interaction, len(self.pages) - 1)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def send_pages(
    ctx: commands.Context, pages: list[discord.Embed], *, content: str | None = None, ephemeral: bool = False
) -> None:
    if len(pages) == 1:
        await ctx.send(content, embed=pages[0], ephemeral=ephemeral)
        return
    view = PaginatorView(pages, ctx.author.id)
    view.message = await ctx.send(content, embed=pages[0], view=view, ephemeral=ephemeral)


class AnimalsView(PaginatorView):
    """/animals paginator with a button switching between the compact list
    and one card per animal (photo, collections, rescuers). Switching keeps
    the place: the first animal of the current list page, or the list page
    holding the current card."""

    def __init__(self, list_pages: list[discord.Embed], cards: list[discord.Embed], owner_id: int):
        self.list_pages = list_pages
        self.cards = cards
        super().__init__(list_pages, owner_id)

    @discord.ui.button(label="Fiches", emoji="🖼️", style=discord.ButtonStyle.success)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.pages is self.list_pages:
            self.pages, index = self.cards, self.index * PAGE_SIZE
            button.label, button.emoji = "Liste", "📋"
        else:
            self.pages, index = self.list_pages, self.index // PAGE_SIZE
            button.label, button.emoji = "Fiches", "🖼️"
        await self._show(interaction, index)


def _parse_animals_filter(text: str) -> tuple[str | None, str | None]:
    """(rarity key, group text) from "!animals" arguments: an optional rarity
    (label or key, "peu commun" is two words) first, then a group. Anything
    that doesn't start with a rarity is taken as a group name."""
    words = text.split()
    for size in (2, 1):
        head = _fold(" ".join(words[:size]))
        tier = next((t for t in RARITY_ODDS if head in (t, _fold(RARITY_LABEL[t]))), None)
        if tier is not None and len(words) >= size:
            return tier, " ".join(words[size:]) or None
    return None, text or None


class Rolls(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # Hybrid commands answer both /rescue and !rescue. With a prefix command,
    # ctx.defer() just shows "typing…" and ephemeral=True is ignored.
    # Hybrid aliases only cover the prefix side: /rc is registered below.
    @commands.hybrid_command(name="rescue", aliases=["rc"], description="Tire un animal au hasard à sauver !")
    async def rescue(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send("Utilise cette commande dans un serveur.", ephemeral=True)
            return
        await ctx.defer()

        async with SessionLocal() as session:
            # Rescued animals stay rollable, and rescuable again.
            animals = list((await session.execute(select(Animal))).scalars().all())
            # The roll doesn't change them; read before so the bonus is known.
            species = await rescued_species(session, ctx.guild.id, ctx.author.id)
            # An empty table (seed never run) mustn't cost a roll.
            status = await consume_roll(session, ctx.guild.id, ctx.author.id) if animals else None
            if status is not None:
                animal = _weighted_pick(animals, rarity_odds(status.luck_level))
                # Every roll pays its rarity's points, in the same transaction
                # as the roll it was spent on.
                reached = milestones.completed(species, animals)
                points = milestone_points(RARITY_POINTS[animal.rarity], len(reached))
                await add_points(session, ctx.guild.id, ctx.author.id, points)
            await session.commit()
            if status is not None:
                # The roller's own copies of this animal, best appeal first.
                owned = list(
                    (
                        await session.execute(
                            select(Rescue.appeal)
                            .where(
                                Rescue.guild_id == ctx.guild.id,
                                Rescue.user_id == ctx.author.id,
                                Rescue.animal_id == animal.id,
                            )
                            .order_by(Rescue.appeal.desc())
                        )
                    ).scalars()
                )
                wishers = list(
                    (
                        await session.execute(
                            select(Wish.user_id).where(Wish.guild_id == ctx.guild.id, Wish.animal_id == animal.id)
                        )
                    ).scalars()
                )

        if not animals:
            await ctx.send("Il n'y a encore aucun animal ! Lance `python -m bot.seed`.", ephemeral=True)
            return
        if status is None:
            await ctx.send(
                f"Tu n'as plus de rolls ! Ils reviennent dans {format_wait(seconds_until_reset())}.",
                ephemeral=True,
            )
            return

        embed = animal_embed(animal, full=False)
        milestone = milestones.milestone_of(animal)
        if milestone is not None:
            embed.title = f"{milestone.badge} {embed.title}"
        # Rolled per roll, not per animal: the same species can come back
        # with a better appeal, which makes rescuing it again worthwhile.
        appeal = roll_appeal(status.appeal_level)
        embed.add_field(
            name="Aura",
            value=f"{format_appeal(appeal)} · {reserve_income(animal.rarity, appeal)} {CURRENCY_NAME}/h comme animal-totem",
            inline=False,
        )
        # The collection groups this animal counts for, with the roller's
        # progress in each (✓ once complete).
        add_collections_field(embed, animal, species)
        if owned:
            copies = "\n".join(format_appeal(a) for a in owned[:MAX_COPIES_SHOWN])
            if len(owned) > MAX_COPIES_SHOWN:
                copies += f"\n+{len(owned) - MAX_COPIES_SHOWN} autres"
            embed.add_field(
                name=f"Déjà dans ta réserve (×{len(owned)})",
                value=copies,
                inline=False,
            )
        view = RescueView(animal, ctx.guild.id, appeal, ctx.author.id)
        counters = f"-# +{points} {CURRENCY_NAME} · {status.rolls_left}/{status.max_rolls} rolls restants"
        if status.bonus_rolls:
            counters += f" (+{status.bonus_rolls} bonus)"
        counters += f" · {status.claims}/{status.max_claims} claims"
        if status.bonus_claims:
            counters += f" (+{status.bonus_claims} bonus)"
        # Players who wished for this animal (/wish) get pinged, except the
        # roller, who's already looking at it.
        others = [u for u in wishers if u != ctx.author.id]
        content = counters
        if ctx.author.id in wishers:
            content = f"⭐ Dans ta wishlist !\n{content}"
        if others:
            content = (
                f"⭐ Souhaité par {' '.join(f'<@{u}>' for u in others)}"
                f" · sauvetage possible <t:{view.protected_until}:R>\n{content}"
            )
        view.message = await ctx.send(
            content,
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(u) for u in others]),
        )

    @app_commands.command(name="rc", description="Raccourci pour /rescue.")
    async def rc(self, interaction: discord.Interaction):
        ctx = await commands.Context.from_interaction(interaction)
        await self.rescue.callback(self, ctx)

    @commands.hybrid_command(name="reserve", description="Affiche ta réserve : les animaux que tu as sauvés.")
    async def reserve(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send("Utilise cette commande dans un serveur.", ephemeral=True)
            return
        await ctx.defer()

        async with SessionLocal() as session:
            stmt = (
                select(Animal, Rescue)
                .join(Rescue, Rescue.animal_id == Animal.id)
                .where(
                    Rescue.guild_id == ctx.guild.id,
                    Rescue.user_id == ctx.author.id,
                )
                .order_by(Rescue.rescued_at)
            )
            rows = (await session.execute(stmt)).all()

        if not rows:
            await ctx.send("Tu n'as encore sauvé aucun animal ! Essaie `/rescue`.", ephemeral=True)
            return

        # One section per rarity, rarest first; in each, one line per species,
        # best appeal first (then by first rescue: rows come sorted by date
        # and the sorts are stable). Each line has its first rescue date, each
        # copy's appeal (best first), and 🔺 (the shop's totem icon) before
        # the name if a copy is a totem.
        copies: dict[int, tuple[Animal, list[Rescue]]] = {}
        for a, rescue in rows:
            copies.setdefault(a.id, (a, []))[1].append(rescue)
        sections: dict[str, list[str]] = {}
        species = sorted(copies.values(), key=lambda c: -max(r.appeal for r in c[1]))
        for a, rescues in sorted(species, key=lambda c: RARITY_ORDER[c[0].rarity]):
            # Stored as naive UTC. A Discord timestamp shows in each viewer's
            # own timezone and date format.
            first = int(rescues[0].rescued_at.replace(tzinfo=timezone.utc).timestamp())
            rescues.sort(key=lambda r: -r.appeal)
            shown = " ".join(f"{r.appeal}★" for r in rescues[:MAX_COPIES_SHOWN])
            if len(rescues) > MAX_COPIES_SHOWN:
                shown += f" +{len(rescues) - MAX_COPIES_SHOWN}"
            count = f" ×{len(rescues)}" if len(rescues) > 1 else ""
            marker = "🔺 " if any(r.in_reserve for r in rescues) else ""
            sections.setdefault(a.rarity, []).append(
                f"{marker}**{a.name}**{count} — *{a.scientific_name}* · {shown} · <t:{first}:d>"
            )
        total = len(rows)
        headers = [
            (f"__**{RARITY_LABEL[tier].capitalize()}**__ ({len(lines)})", lines)
            for tier, lines in sections.items()
        ]
        pages = paginate_sections(f"Réserve de {ctx.author.display_name} ({total})", headers, discord.Color.green())
        await send_pages(ctx, pages)

    @commands.hybrid_command(name="animals", description="Liste tous les animaux, des plus communs aux plus rares.")
    @app_commands.describe(rarity="N'afficher qu'une rareté", group="N'afficher qu'une collection")
    @app_commands.choices(rarity=[app_commands.Choice(name=RARITY_LABEL[t].capitalize(), value=t) for t in RARITY_ODDS])
    async def animals(self, ctx: commands.Context, rarity: str | None = None, *, group: str | None = None):
        if ctx.interaction is None:
            # Typed by hand ("!animals peu commun félidés"): the words don't
            # split cleanly into the two parameters, so parse the whole text.
            rarity, group = _parse_animals_filter(" ".join(filter(None, [rarity, group])))
        elif rarity is not None and rarity not in RARITY_ODDS:
            rarity = ""  # can't happen with the choices, but don't trust it
        if rarity == "":
            labels = ", ".join(RARITY_LABEL.values())
            await ctx.send(f"Rareté inconnue. Choisis parmi : {labels}.", ephemeral=True)
            return
        collection = None
        if group:
            needle = _fold(group)
            collection = next((a for a in COLLECTIONS if _fold(a.label) == needle), None)
            if collection is None:
                await ctx.send(f"Aucun groupe ne s'appelle « {group} ».", ephemeral=True)
                return
        await ctx.defer(ephemeral=True)
        async with SessionLocal() as session:
            stmt = select(Animal)
            if rarity is not None:
                stmt = stmt.where(Animal.rarity == rarity)
            animals = list((await session.execute(stmt)).scalars().all())
            if collection is not None:
                animals = [a for a in animals if collection.includes(a)]
            owners: dict[int, list[int]] = {}
            # The invoker's species, for collection progress on the cards.
            species = None
            if ctx.guild is not None:
                species = await rescued_species(session, ctx.guild.id, ctx.author.id)
                rescues = await session.execute(
                    select(Rescue.animal_id, Rescue.user_id)
                    .where(Rescue.guild_id == ctx.guild.id)
                    .distinct()
                )
                for animal_id, user_id in rescues.all():
                    owners.setdefault(animal_id, []).append(user_id)

        # Most common first, names A→Z within a rarity.
        animals.sort(key=lambda a: (-RARITY_ORDER[a.rarity], _fold(a.name)))
        lines = []
        for a in animals:
            line = f"**{a.name}** — *{a.scientific_name}* ({RARITY_LABEL[a.rarity]})"
            if a.id in owners:
                # Mentions inside embeds render as names but never ping.
                # Capped so a popular animal can't blow up the page length.
                rescuers = owners[a.id]
                line += " · 🏠 " + ", ".join(f"<@{u}>" for u in rescuers[:MAX_RESCUERS_SHOWN])
                if len(rescuers) > MAX_RESCUERS_SHOWN:
                    line += f" +{len(rescuers) - MAX_RESCUERS_SHOWN}"
            lines.append(line)

        title = collection.label if collection else "Animaux"
        if rarity is not None:
            title += f" {RARITY_LABEL[rarity]}s" if collection is None else f" · {RARITY_LABEL[rarity]}s"
        title += f" ({len(animals)})"
        if ctx.guild is not None:
            title += f" · {sum(a.id in owners for a in animals)} sauvés ici"
        color = RARITY_COLOR[rarity] if rarity else discord.Color.green()
        if not animals:
            await send_pages(ctx, paginate(title, lines, color), ephemeral=True)
            return

        # One card per animal, in the list's order, for the "Fiches" button.
        cards = []
        for number, a in enumerate(animals, 1):
            card = animal_embed(a, full=False)
            milestone = milestones.milestone_of(a)
            if milestone is not None:
                card.title = f"{milestone.badge} {card.title}"
            add_collections_field(card, a, species)
            rescuers = owners.get(a.id)
            if rescuers:
                names = ", ".join(f"<@{u}>" for u in rescuers[:MAX_RESCUERS_SHOWN])
                if len(rescuers) > MAX_RESCUERS_SHOWN:
                    names += f" +{len(rescuers) - MAX_RESCUERS_SHOWN}"
                card.add_field(name="🏠 Sauvé par", value=names, inline=False)
            footer = f"Fiche {number}/{len(animals)}"
            if a.wiki_url:
                footer += " · Source : Wikipédia"
            card.set_footer(text=footer)
            cards.append(card)
        list_pages = paginate(title, lines, color)
        view = AnimalsView(list_pages, cards, ctx.author.id)
        view.message = await ctx.send(embed=list_pages[0], view=view, ephemeral=True)

    @animals.autocomplete("group")
    async def animals_group_autocomplete(self, interaction: discord.Interaction, current: str):
        needle = _fold(current)
        return [app_commands.Choice(name=a.label, value=a.label) for a in COLLECTIONS if needle in _fold(a.label)][:25]

    @commands.hybrid_command(name="info", description="Affiche la description Wikipédia complète d'un animal.")
    @app_commands.describe(animal="L'animal à rechercher")
    async def info(self, ctx: commands.Context, *, animal: str):
        # The "*" makes "!info Panda roux" take the whole rest of the message.
        await ctx.defer()
        async with SessionLocal() as session:
            animals = (await session.execute(select(Animal))).scalars().all()
        # Autocomplete sends the exact name, but "!info panda roux" is typed
        # by hand, so match case- and accent-insensitively.
        needle = _fold(animal)
        found = next((a for a in animals if _fold(a.name) == needle), None)

        if found is None:
            await ctx.send(f"Aucun animal ne s'appelle « {animal} ».", ephemeral=True)
            return
        await ctx.send(embed=animal_embed(found, full=True))

    @info.autocomplete("animal")
    async def info_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        async with SessionLocal() as session:
            names = (await session.execute(select(Animal.name).order_by(Animal.name))).scalars().all()
        needle = _fold(current)
        # Discord caps autocomplete at 25 choices.
        return [app_commands.Choice(name=n, value=n) for n in names if needle in _fold(n)][:25]


async def setup(bot: commands.Bot):
    await bot.add_cog(Rolls(bot))
