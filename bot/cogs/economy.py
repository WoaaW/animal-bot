import discord
from discord.ext import commands

from ..db import SessionLocal
from ..economy import (
    BASE_ROLLS,
    CLAIM_REGEN_SECONDS,
    CURRENCY_NAME,
    PlayerStatus,
    appeal_odds,
    buy_upgrade,
    get_reserve_status,
    get_status,
    seconds_until_reset,
    upgrade_max_level,
    upgrade_maxed,
    upgrade_price,
)
from .rolls import RARITY_LABEL, RARITY_UNLOCK_LEVEL, format_rate, format_wait, rarity_odds

# The top tier stays a surprise until a player unlocks it.
HIDDEN_TIER = "mythic"


def _percent(pct: float) -> str:
    # French decimal comma: 53,7 %.
    return f"{pct:.{1 if pct >= 1 else 2}f} %".replace(".", ",")


def _format_odds(odds: dict[str, float], luck_level: int) -> str:
    # The hidden tier keeps its "???" until the player has unlocked it, even
    # in the next level's preview.
    def label(tier: str) -> str:
        hidden = tier == HIDDEN_TIER and luck_level < RARITY_UNLOCK_LEVEL[tier]
        return "???" if hidden else RARITY_LABEL[tier]

    return " · ".join(f"{label(tier)} {_percent(pct)}" for tier, pct in odds.items())


def _format_appeal_odds(odds: dict[int, float]) -> str:
    return " · ".join(f"{stars}★ {_percent(pct)}" for stars, pct in odds.items())


def _level(status: PlayerStatus, upgrade: str) -> int:
    return {
        "rolls": status.max_rolls - BASE_ROLLS,
        "luck": status.luck_level,
        "claims": status.claim_slots,
        "reserve": status.reserve_slots,
        "appeal": status.appeal_level,
    }[upgrade]


def _cost(status: PlayerStatus, upgrade: str) -> str:
    level = _level(status, upgrade)
    return "maximum atteint" if upgrade_maxed(upgrade, level) else f"{upgrade_price(upgrade, level)} {CURRENCY_NAME}"


def _progress(status: PlayerStatus, upgrade: str) -> str:
    """`|***-------|` 3/10: one * per level bought. In a code span, or
    Discord would read the stars as italics/bold."""
    level = _level(status, upgrade)
    max_level = upgrade_max_level(upgrade)
    if max_level is None:
        return f"Niveau **{level}**"
    return f"`|{'*' * level}{'-' * (max_level - level)}|` {level}/{max_level}"


# Shop order. The ones after ALWAYS_SHOWN stay hidden until the player has
# once had enough points for their first level (peak_points), so the shop
# unfolds bit by bit and never folds back.
SHOP_ORDER = ("rolls", "luck", "claims", "reserve", "appeal")
ALWAYS_SHOWN = {"rolls", "luck"}


def visible_upgrades(status: PlayerStatus) -> list[str]:
    return [
        upgrade for upgrade in SHOP_ORDER
        if upgrade in ALWAYS_SHOWN
        or _level(status, upgrade) > 0
        or status.peak_points >= upgrade_price(upgrade, 0)
    ]


def shop_embed(status: PlayerStatus) -> discord.Embed:
    next_luck = status.luck_level + 1
    unlocks = [
        "???" if tier == HIDDEN_TIER else f"**{RARITY_LABEL[tier]}**"
        for tier, level in RARITY_UNLOCK_LEVEL.items()
        if level == next_luck
    ]
    luck_effect = f"Débloque : {', '.join(unlocks)}." if unlocks else "Un peu plus de chances pour les animaux rares."
    regen = format_wait(CLAIM_REGEN_SECONDS)

    embed = discord.Embed(
        title="Boutique",
        description=f"Tu as **{status.points}** {CURRENCY_NAME}. Tes améliorations durent jusqu'à ton prochain `/prestige`.",
        color=discord.Color.gold(),
    )
    # upgrade -> (field name, field value). A maxed upgrade shows where the
    # player stands instead of "a → b".
    fields: dict[str, tuple[str, str]] = {}
    rolls_maxed = upgrade_maxed("rolls", _level(status, "rolls"))
    fields["rolls"] = (
        f"🔷 Rolls — {_cost(status, 'rolls')}",
        f"**{status.max_rolls}** rolls par heure." if rolls_maxed
        else f"{status.max_rolls} → **{status.max_rolls + 1}** rolls par heure.",
    )
    luck_maxed = upgrade_maxed("luck", status.luck_level)
    fields["luck"] = (
        f"🟢 Chance {'' if luck_maxed else f'niveau {next_luck} '}— {_cost(status, 'luck')}",
        ("" if luck_maxed else f"{luck_effect}\n")
        + f"Tes chances : {_format_odds(rarity_odds(status.luck_level), status.luck_level)}"
        + ("" if luck_maxed else
           f"\nAu niveau suivant : {_format_odds(rarity_odds(next_luck), status.luck_level)}"),
    )
    claims_maxed = upgrade_maxed("claims", status.claim_slots)
    fields["claims"] = (
        f"🟨 Stock de claims — {_cost(status, 'claims')}",
        (f"Garde jusqu'à **{status.max_claims}** claims" if claims_maxed
         else f"Garde jusqu'à {status.max_claims} → **{status.max_claims + 1}** claims")
        + f" (recharge : 1 claim par {regen}).",
    )
    reserve_maxed = upgrade_maxed("reserve", status.reserve_slots)
    fields["reserve"] = (
        f"🔺 Place en réserve — {_cost(status, 'reserve')}",
        f"Expose **{status.max_reserve}** animaux dans ta `/reserve`." if reserve_maxed
        else f"Expose {status.max_reserve} → **{status.max_reserve + 1}** animaux dans ta `/reserve`.",
    )
    fields["appeal"] = (
        f"🟣 Attrait niveau {status.appeal_level + 1} — {upgrade_price('appeal', status.appeal_level)} {CURRENCY_NAME}",
        f"Plus de chances de tirer des animaux avec un bon attrait.\n"
        f"Tes chances : {_format_appeal_odds(appeal_odds(status.appeal_level))}\n"
        f"Au niveau suivant : {_format_appeal_odds(appeal_odds(status.appeal_level + 1))}",
    )
    for upgrade in visible_upgrades(status):
        name, value = fields[upgrade]
        embed.add_field(name=name, value=f"{_progress(status, upgrade)}\n{value}", inline=False)
    return embed


class ShopView(discord.ui.View):
    def __init__(self, owner_id: int, guild_id: int, status: PlayerStatus):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.status = status
        self.message: discord.Message | None = None
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        # Only the upgrades shown in the embed get a button, in the same order.
        buttons = {
            "rolls": self.rolls,
            "luck": self.luck,
            "claims": self.claims,
            "reserve": self.reserve,
            "appeal": self.appeal,
        }
        self.clear_items()
        for upgrade in visible_upgrades(self.status):
            button = buttons[upgrade]
            button.disabled = upgrade_maxed(upgrade, _level(self.status, upgrade))
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # With !shop the message is public, so others could click it.
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Ce n'est pas ta boutique ! Ouvre la tienne avec `/shop`.", ephemeral=True)
            return False
        return True

    async def _buy(self, interaction: discord.Interaction, upgrade: str) -> None:
        # Pay for the level shown on screen, not whatever it is by now.
        from_level = _level(self.status, upgrade)
        async with SessionLocal() as session:
            bought = await buy_upgrade(session, self.guild_id, self.owner_id, upgrade, from_level)
            await session.commit()
            self.status = bought or await get_status(session, self.guild_id, self.owner_id)

        self._sync_buttons()
        await interaction.response.edit_message(embed=shop_embed(self.status), view=self)
        if bought is None:
            if upgrade_maxed(upgrade, from_level):
                message = "Cette amélioration est déjà au maximum."
            elif _level(self.status, upgrade) != from_level:
                message = "Le prix a changé entre-temps, regarde à nouveau la boutique."
            else:
                price = upgrade_price(upgrade, from_level)
                message = f"Pas assez de {CURRENCY_NAME} : ça coûte {price}, tu en as {self.status.points}."
            await interaction.followup.send(message, ephemeral=True)

    @discord.ui.button(emoji="🔷", label="Rolls", style=discord.ButtonStyle.primary)
    async def rolls(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._buy(interaction, "rolls")

    @discord.ui.button(emoji="🟢", label="Chance", style=discord.ButtonStyle.primary)
    async def luck(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._buy(interaction, "luck")

    @discord.ui.button(emoji="🟨", label="Stock de claims", style=discord.ButtonStyle.primary)
    async def claims(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._buy(interaction, "claims")

    @discord.ui.button(emoji="🔺", label="Place en réserve", style=discord.ButtonStyle.primary)
    async def reserve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._buy(interaction, "reserve")

    @discord.ui.button(emoji="🟣", label="Attrait", style=discord.ButtonStyle.primary)
    async def appeal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._buy(interaction, "appeal")

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class Economy(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="balance", description=f"Affiche tes {CURRENCY_NAME}, tes rolls et tes claims.")
    async def balance(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send("Utilise cette commande dans un serveur.", ephemeral=True)
            return
        await ctx.defer(ephemeral=True)

        async with SessionLocal() as session:
            status = await get_status(session, ctx.guild.id, ctx.author.id)
            reserve = await get_reserve_status(session, ctx.guild.id, ctx.author.id)

        claims = f"Claims : **{status.claims}/{status.max_claims}**"
        if status.bonus_claims:
            claims += f" (+{status.bonus_claims} bonus)"
        if status.next_claim_in:
            claims += f" (prochain dans {format_wait(status.next_claim_in)})"
        await ctx.send(
            f"Tu as **{status.points}** {CURRENCY_NAME}.\n"
            f"Rolls : **{status.rolls_left}/{status.max_rolls}**"
            + (f" (+{status.bonus_rolls} bonus)" if status.bonus_rolls else "")
            + f" (recharge dans {format_wait(seconds_until_reset())}).\n"
            f"{claims}\n"
            f"Réserve : **{reserve.used}/{reserve.max_slots}** animaux, {format_rate(reserve.rate)} {CURRENCY_NAME}/h"
            + (f" (dont +{reserve.bonus} % de collections)" if reserve.bonus else "")
            + ", "
            f"**{reserve.pending}** en attente dans `/reserve`.\n"
            f"Niveau de chance : **{status.luck_level}** · niveau d'attrait : **{status.appeal_level}**\n"
            f"Échanges restants aujourd'hui : **{status.trades_left}** · prestige : **{status.prestige_level}**",
            ephemeral=True,
        )

    @commands.hybrid_command(name="shop", description=f"Dépense tes {CURRENCY_NAME} en améliorations.")
    async def shop(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send("Utilise cette commande dans un serveur.", ephemeral=True)
            return
        await ctx.defer(ephemeral=True)

        async with SessionLocal() as session:
            status = await get_status(session, ctx.guild.id, ctx.author.id)

        view = ShopView(ctx.author.id, ctx.guild.id, status)
        view.message = await ctx.send(embed=shop_embed(status), view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Economy(bot))
