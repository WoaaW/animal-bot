import math
import random
import time
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from .species_collections import Collection, completed, rescued_species, total_bonus
from . import milestones
from .milestones import Milestone
from .models import Animal, PlayerState, Rescue

# Display name of the currency — the only place to change when it gets renamed.
CURRENCY_NAME = "points"

# Points earned by the roller on every roll, by the rolled animal's rarity. Keys must match RARITY_ODDS.
RARITY_POINTS = {"common": 10, "uncommon": 25, "rare": 50, "epic": 100, "legendary": 250, "mythic": 500}

# Rolls per reset window, before shop upgrades (PlayerState.extra_rolls).
BASE_ROLLS = 10
# Rolls refill at fixed clock times (every hour on the hour), the same moment
# for everyone, rather than one hour after each player's first roll.
ROLL_RESET_SECONDS = 3600

# Rescuing costs a claim. Claims come back one per CLAIM_REGEN_SECONDS, per
# player, and pile up to BASE_CLAIMS + PlayerState.claim_slots.
BASE_CLAIMS = 1
CLAIM_REGEN_SECONDS = 60

# Accepted trades per player per day, gifts included. Days are fixed clock slots
# (midnight UTC), the same for everyone, like roll windows.
MAX_TRADES_PER_DAY = 3
TRADE_RESET_SECONDS = 24 * 3600

# The reserve: rescued animals put on show earn points per hour while the
# player is away: a base by rarity (keys must match RARITY_ODDS) times a
# multiplier for their appeal, a 1-5 star stat rolled with each roll.
RESERVE_BASE_INCOME = {"common": 3, "uncommon": 6, "rare": 9, "epic": 15, "legendary": 24, "mythic": 36}
# Appeal stars → (chance in %, income multiplier).
APPEAL = {1: (45, 1), 2: (30, 1.5), 3: (15, 2), 4: (8, 3), 5: (2, 5)}
# Each shop appeal level adds APPEAL_STEP × (stars - 1) to a star count's
# weight multiplier, like LUCK_STEP does for rarities: at level 10, ★★★★★ is
# ×5 and ★ never moves. The roller's level applies, whoever rescues.
APPEAL_STEP = 0.1
# Animals that fit in the reserve, before shop upgrades (PlayerState.reserve_slots).
BASE_RESERVE_SLOTS = 1
# Income piles up for at most this long between two collections.
RESERVE_CAP_SECONDS = 12 * 3600

# Shop upgrades: (PlayerState column holding the level, price of level 0 → 1,
# price growth per level, max level or None). Every upgrade lasts until the next prestige and
# pricier each time. Caps are written as totals minus the default, so the
# total stays the same if a default changes.
UPGRADES = {
    "rolls": (PlayerState.extra_rolls, 100, 1.5, 25 - BASE_ROLLS),   # +1 roll per window, up to 25
    "luck": (PlayerState.luck_level, 150, 2, 10),                    # see rarity_odds in cogs/rolls.py
    "claims": (PlayerState.claim_slots, 500, 2, 6 - BASE_CLAIMS),    # +1 claim that can be stored, up to 6
    "reserve": (PlayerState.reserve_slots, 1000, 2, 5 - BASE_RESERVE_SLOTS),  # +1 animal, up to 5
    "appeal": (PlayerState.appeal_level, 2000, 2, None),             # see appeal_odds
}


def appeal_odds(level: int) -> dict[int, float]:
    """A player's chance (in %) of rolling each appeal."""
    weights = {stars: chance * (1 + APPEAL_STEP * (stars - 1) * level) for stars, (chance, _) in APPEAL.items()}
    total = sum(weights.values())
    return {stars: w / total * 100 for stars, w in weights.items()}


def roll_appeal(level: int = 0) -> int:
    odds = appeal_odds(level)
    return random.choices(list(odds), weights=list(odds.values()), k=1)[0]


def reserve_income(rarity: str, appeal: int) -> int:
    """Points per hour an animal earns in the reserve; halves round up."""
    return math.floor(RESERVE_BASE_INCOME[rarity] * APPEAL[appeal][1] + 0.5)


def upgrade_price(upgrade: str, level: int) -> int:
    """Price of going from `level` to `level + 1`."""
    _, base, growth, _ = UPGRADES[upgrade]
    return round(base * growth**level)


def upgrade_max_level(upgrade: str) -> int | None:
    return UPGRADES[upgrade][3]


def upgrade_maxed(upgrade: str, level: int) -> bool:
    max_level = UPGRADES[upgrade][3]
    return max_level is not None and level >= max_level


@dataclass
class PlayerStatus:
    points: int
    peak_points: int
    rolls_left: int
    max_rolls: int
    claims: int
    max_claims: int
    bonus_rolls: int
    bonus_claims: int
    luck_level: int
    appeal_level: int
    claim_slots: int
    reserve_slots: int
    max_reserve: int
    # Seconds until the next claim comes back; 0 when the stock is full.
    next_claim_in: int
    trades_left: int
    prestige_level: int


def _current_period() -> int:
    return int(time.time() // ROLL_RESET_SECONDS)


def seconds_until_reset() -> int:
    return int(ROLL_RESET_SECONDS - time.time() % ROLL_RESET_SECONDS)


def _current_trade_period() -> int:
    return int(time.time() // TRADE_RESET_SECONDS)


def seconds_until_trade_reset() -> int:
    return int(TRADE_RESET_SECONDS - time.time() % TRADE_RESET_SECONDS)


def _status(state: PlayerState) -> PlayerStatus:
    now = int(time.time())
    max_rolls = BASE_ROLLS + state.extra_rolls
    used = state.rolls_used if state.rolls_period == _current_period() else 0
    max_claims = BASE_CLAIMS + state.claim_slots
    claims = min(max_claims, (now - state.claims_anchor) // CLAIM_REGEN_SECONDS)
    next_claim_in = 0 if claims >= max_claims else CLAIM_REGEN_SECONDS - (now - state.claims_anchor) % CLAIM_REGEN_SECONDS
    return PlayerStatus(
        points=state.points,
        # Rows from before peak_points existed start at 0: their balance counts.
        peak_points=max(state.peak_points, state.points),
        rolls_left=max_rolls - used,
        max_rolls=max_rolls,
        claims=claims,
        max_claims=max_claims,
        bonus_rolls=state.bonus_rolls,
        bonus_claims=state.bonus_claims,
        luck_level=state.luck_level,
        appeal_level=state.appeal_level,
        claim_slots=state.claim_slots,
        reserve_slots=state.reserve_slots,
        max_reserve=BASE_RESERVE_SLOTS + state.reserve_slots,
        next_claim_in=next_claim_in,
        trades_left=MAX_TRADES_PER_DAY - (state.trades_used if state.trades_period == _current_trade_period() else 0),
        prestige_level=state.prestige_level,
    )


async def _ensure_player(session: AsyncSession, guild_id: int, user_id: int) -> None:
    await session.execute(
        insert(PlayerState).values(guild_id=guild_id, user_id=user_id).on_conflict_do_nothing()
    )


def _player(guild_id: int, user_id: int):
    return (PlayerState.guild_id == guild_id, PlayerState.user_id == user_id)


async def _update_player(session: AsyncSession, guild_id: int, user_id: int, *where, **values) -> PlayerStatus | None:
    """Apply `values` if `where` holds, in one statement; None if it didn't.

    Every spend (roll, claim, purchase) goes through a single conditional
    UPDATE, so concurrent clicks can't overspend. Doesn't commit.
    """
    stmt = (
        update(PlayerState)
        .where(*_player(guild_id, user_id), *where)
        .values(**values)
        .returning(PlayerState)
        .execution_options(synchronize_session=False)
    )
    state = (await session.execute(stmt)).scalar_one_or_none()
    return None if state is None else _status(state)


async def add_points(session: AsyncSession, guild_id: int, user_id: int, amount: int) -> None:
    # Upsert with an in-SQL increment, so two concurrent rolls by the same
    # player can't race on creating the row or lose an update. Doesn't commit.
    stmt = insert(PlayerState).values(guild_id=guild_id, user_id=user_id, points=amount, peak_points=amount)
    stmt = stmt.on_conflict_do_update(
        index_elements=[PlayerState.guild_id, PlayerState.user_id],
        set_={
            "points": PlayerState.points + stmt.excluded.points,
            "peak_points": func.max(PlayerState.peak_points, PlayerState.points + stmt.excluded.points),
        },
    )
    await session.execute(stmt)


async def consume_roll(session: AsyncSession, guild_id: int, user_id: int) -> PlayerStatus | None:
    """Use one roll, regular ones first, then bonus ones; None if none left.
    Doesn't commit."""
    period = _current_period()
    await _ensure_player(session, guild_id, user_id)
    # The WHERE refuses the roll when the window is used up, and the CASE
    # starts the count over when a new window began.
    status = await _update_player(
        session, guild_id, user_id,
        or_(
            PlayerState.rolls_period != period,
            PlayerState.rolls_used < BASE_ROLLS + PlayerState.extra_rolls,
        ),
        rolls_used=case((PlayerState.rolls_period == period, PlayerState.rolls_used + 1), else_=1),
        rolls_period=period,
    )
    return status or await _update_player(
        session, guild_id, user_id,
        PlayerState.bonus_rolls > 0,
        bonus_rolls=PlayerState.bonus_rolls - 1,
    )


async def consume_claim(session: AsyncSession, guild_id: int, user_id: int) -> PlayerStatus | None:
    """Use one claim, regenerated ones first, then bonus ones; None if none
    is ready. Doesn't commit."""
    now = int(time.time())
    await _ensure_player(session, guild_id, user_id)
    # A claim is ready once the anchor is a full regen period behind. Using
    # one first clamps the anchor to the cap (claims beyond it are lost, not
    # banked), then moves it one period forward — so time already counting
    # toward the next claim is kept.
    full_anchor = now - (BASE_CLAIMS + PlayerState.claim_slots) * CLAIM_REGEN_SECONDS
    status = await _update_player(
        session, guild_id, user_id,
        now - PlayerState.claims_anchor >= CLAIM_REGEN_SECONDS,
        claims_anchor=func.max(PlayerState.claims_anchor, full_anchor) + CLAIM_REGEN_SECONDS,
    )
    return status or await _update_player(
        session, guild_id, user_id,
        PlayerState.bonus_claims > 0,
        bonus_claims=PlayerState.bonus_claims - 1,
    )


async def give_bonus(session: AsyncSession, guild_id: int, user_id: int, *, rolls: int = 0, claims: int = 0) -> PlayerStatus:
    """Add bonus rolls/claims. Doesn't commit."""
    await _ensure_player(session, guild_id, user_id)
    return await _update_player(
        session, guild_id, user_id,
        bonus_rolls=PlayerState.bonus_rolls + rolls,
        bonus_claims=PlayerState.bonus_claims + claims,
    )


async def reset_player(session: AsyncSession, guild_id: int, user_id: int) -> None:
    """Wipe a player's points, counters, upgrades and prestige (not their rescues).
    Their reserve is emptied too, since its extra slots are gone. Doesn't commit."""
    await session.execute(delete(PlayerState).where(*_player(guild_id, user_id)))
    await session.execute(
        update(Rescue)
        .where(Rescue.guild_id == guild_id, Rescue.user_id == user_id)
        .values(in_reserve=False)
        .execution_options(synchronize_session=False)
    )


async def buy_upgrade(session: AsyncSession, guild_id: int, user_id: int, upgrade: str, from_level: int) -> PlayerStatus | None:
    """Buy one level of `upgrade`. None if the player can't afford it, the
    upgrade is maxed, or they aren't at `from_level` anymore (e.g. a double
    click), so nobody pays the price of a level other than the one they saw.
    Doesn't commit."""
    if upgrade_maxed(upgrade, from_level):
        return None
    column, _, _, _ = UPGRADES[upgrade]
    price = upgrade_price(upgrade, from_level)
    await _ensure_player(session, guild_id, user_id)
    return await _update_player(
        session, guild_id, user_id,
        PlayerState.points >= price,
        column == from_level,
        points=PlayerState.points - price,
        **{column.key: from_level + 1},
    )


async def get_status(session: AsyncSession, guild_id: int, user_id: int) -> PlayerStatus:
    state = (
        await session.execute(select(PlayerState).where(*_player(guild_id, user_id)))
    ).scalar_one_or_none()
    # Unsaved defaults, so a new player sees full rolls/claims without a write.
    return _status(state or PlayerState(
        guild_id=guild_id, user_id=user_id, points=0, peak_points=0, rolls_used=0, rolls_period=0,
        claims_anchor=0, bonus_rolls=0, bonus_claims=0, extra_rolls=0, luck_level=0, claim_slots=0,
        reserve_slots=0, appeal_level=0, trades_used=0, trades_period=0, prestige_level=0,
    ))


@dataclass
class ReserveStatus:
    rate: float      # points per hour, all animals in the reserve together, bonus included
    bonus: int       # % added by completed collections
    pending: int     # points waiting to be collected
    used: int
    max_slots: int


def _reserve_earnings(base: int, bonus: int, anchor: int, remainder: int, now: int) -> tuple[int, int]:
    """(points earned since anchor, leftover point-seconds). Counting in
    point-seconds keeps the fraction of a point between collections, and
    applying the bonus % there (not to the hourly rate) keeps a small bonus
    on a small reserve from rounding away."""
    earned = base * (100 + bonus) * min(now - anchor, RESERVE_CAP_SECONDS) // 100 + remainder
    return divmod(earned, 3600)


def _in_reserve(guild_id: int, user_id: int, rescue=Rescue):
    return (rescue.guild_id == guild_id, rescue.user_id == user_id, rescue.in_reserve)


async def _reserve_rate(session: AsyncSession, guild_id: int, user_id: int) -> tuple[int, int, int]:
    """(points per hour before bonus, collection bonus in %, number of
    animals) of a player's reserve."""
    income = case(
        *[
            (and_(Animal.rarity == rarity, Rescue.appeal == appeal), reserve_income(rarity, appeal))
            for rarity in RESERVE_BASE_INCOME
            for appeal in APPEAL
        ],
        else_=0,
    )
    stmt = (
        select(func.coalesce(func.sum(income), 0), func.count())
        .select_from(Rescue)
        .join(Animal, Animal.id == Rescue.animal_id)
        .where(*_in_reserve(guild_id, user_id))
    )
    base, used = (await session.execute(stmt)).one()
    bonus = total_bonus(await rescued_species(session, guild_id, user_id))
    return base, bonus, used


async def get_reserve_status(session: AsyncSession, guild_id: int, user_id: int) -> ReserveStatus:
    base, bonus, used = await _reserve_rate(session, guild_id, user_id)
    row = (
        await session.execute(
            select(PlayerState.reserve_anchor, PlayerState.reserve_remainder, PlayerState.reserve_slots)
            .where(*_player(guild_id, user_id))
        )
    ).one_or_none()
    anchor, remainder, slots = row or (0, 0, 0)
    pending, _ = _reserve_earnings(base, bonus, anchor, remainder, int(time.time()))
    return ReserveStatus(rate=base * (100 + bonus) / 100, bonus=bonus, pending=pending, used=used, max_slots=BASE_RESERVE_SLOTS + slots)


async def collect_reserve(session: AsyncSession, guild_id: int, user_id: int) -> int:
    """Pay what the reserve earned since the last collection (at most
    RESERVE_CAP_SECONDS' worth) and start counting again from now. Returns the
    points paid. Runs before every change to the reserve and before every
    rescue or trade (which can complete or break an collection), so neither an
    animal nor a bonus ever earns for time it wasn't there. Doesn't commit."""
    now = int(time.time())
    await _ensure_player(session, guild_id, user_id)
    base, bonus, _ = await _reserve_rate(session, guild_id, user_id)
    anchor, remainder = (
        await session.execute(
            select(PlayerState.reserve_anchor, PlayerState.reserve_remainder).where(*_player(guild_id, user_id))
        )
    ).one()
    paid, remainder_left = _reserve_earnings(base, bonus, anchor, remainder, now)
    # Only applies if nobody collected in between (same idea as buy_upgrade's
    # from_level): a concurrent collection already paid this time.
    status = await _update_player(
        session, guild_id, user_id,
        PlayerState.reserve_anchor == anchor,
        PlayerState.reserve_remainder == remainder,
        points=PlayerState.points + paid,
        peak_points=func.max(PlayerState.peak_points, PlayerState.points + paid),
        reserve_anchor=now,
        reserve_remainder=remainder_left,
    )
    return paid if status else 0


async def place_in_reserve(session: AsyncSession, guild_id: int, user_id: int, rescue_id: int) -> bool:
    """Put one of the player's rescues in their reserve. False if it isn't
    theirs, is already there, or the reserve is full. Doesn't commit."""
    await collect_reserve(session, guild_id, user_id)
    # Aliased: a plain Rescue in the subquery would be correlated to the
    # UPDATE's own table and stop counting.
    placed = aliased(Rescue)
    used = select(func.count()).select_from(placed).where(*_in_reserve(guild_id, user_id, placed))
    slots = select(BASE_RESERVE_SLOTS + PlayerState.reserve_slots).where(*_player(guild_id, user_id))
    result = await session.execute(
        update(Rescue)
        .where(
            Rescue.id == rescue_id,
            Rescue.guild_id == guild_id,
            Rescue.user_id == user_id,
            ~Rescue.in_reserve,
            used.scalar_subquery() < slots.scalar_subquery(),
        )
        .values(in_reserve=True)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


async def remove_from_reserve(session: AsyncSession, guild_id: int, user_id: int, rescue_id: int) -> bool:
    """Take one of the player's rescues out of their reserve. False if it
    wasn't there. Doesn't commit."""
    await collect_reserve(session, guild_id, user_id)
    result = await session.execute(
        update(Rescue)
        .where(Rescue.id == rescue_id, *_in_reserve(guild_id, user_id))
        .values(in_reserve=False)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


@dataclass(frozen=True)
class TradeSide:
    """What one player hands over in a trade."""
    user_id: int
    rescue_id: int | None = None


@dataclass
class TradeResult:
    # None on success, else why it failed ("reserve": the animal is in its
    # owner's reserve, "moved": it isn't theirs anymore, "limit": no trade left
    # today) and whose side failed. The caller must not commit a failed trade.
    failure: str | None = None
    failed_user_id: int | None = None
    # user id → collections gained or lost by this trade.
    unlocked: dict[int, list[Collection]] = field(default_factory=dict)
    lost: dict[int, list[Collection]] = field(default_factory=dict)
    milestones_unlocked: dict[int, list[Milestone]] = field(default_factory=dict)
    milestones_lost: dict[int, list[Milestone]] = field(default_factory=dict)


async def _give_rescue(session: AsyncSession, guild_id: int, rescue_id: int, from_id: int, to_id: int) -> str | None:
    """Hand a rescue over; returns the failure reason, if any. The date becomes
    the transfer's, as /sanctuary shows when the owner got the animal."""
    result = await session.execute(
        update(Rescue)
        .where(Rescue.id == rescue_id, Rescue.guild_id == guild_id, Rescue.user_id == from_id, ~Rescue.in_reserve)
        .values(user_id=to_id, rescued_at=datetime.utcnow())
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 1:
        return None
    in_reserve = await session.scalar(
        select(Rescue.in_reserve).where(Rescue.id == rescue_id, Rescue.guild_id == guild_id, Rescue.user_id == from_id)
    )
    return "reserve" if in_reserve else "moved"


async def _consume_trade(session: AsyncSession, guild_id: int, user_id: int) -> bool:
    """Use one of today's trades; False if none left. Doesn't commit."""
    period = _current_trade_period()
    return await _update_player(
        session, guild_id, user_id,
        or_(PlayerState.trades_period != period, PlayerState.trades_used < MAX_TRADES_PER_DAY),
        trades_used=case((PlayerState.trades_period == period, PlayerState.trades_used + 1), else_=1),
        trades_period=period,
    ) is not None


async def execute_trade(session: AsyncSession, guild_id: int, a: TradeSide, b: TradeSide) -> TradeResult:
    """Swap what each side hands over, all or nothing. Doesn't commit: commit
    only if `failure` is None, closing the session otherwise rolls it all back."""
    # A trade can complete or break collections, which changes reserve income:
    # pay what was earned at the old rate first. These writes also take SQLite's
    # write lock, so nothing can change between here and the commit.
    species_before = {}
    for side in (a, b):
        await collect_reserve(session, guild_id, side.user_id)
        species_before[side.user_id] = await rescued_species(session, guild_id, side.user_id)

    for side in (a, b):
        if not await _consume_trade(session, guild_id, side.user_id):
            return TradeResult(failure="limit", failed_user_id=side.user_id)
    for side, other in ((a, b), (b, a)):
        if side.rescue_id is not None:
            reason = await _give_rescue(session, guild_id, side.rescue_id, side.user_id, other.user_id)
            if reason:
                return TradeResult(failure=reason, failed_user_id=side.user_id)

    result = TradeResult()
    pool = (await session.execute(select(Animal))).scalars().all()
    for side in (a, b):
        species_after = await rescued_species(session, guild_id, side.user_id)
        done_before = completed(species_before[side.user_id])
        done_after = completed(species_after)
        result.unlocked[side.user_id] = [x for x in done_after if x not in done_before]
        result.lost[side.user_id] = [x for x in done_before if x not in done_after]
        reached_before = milestones.completed(species_before[side.user_id], pool)
        reached_after = milestones.completed(species_after, pool)
        result.milestones_unlocked[side.user_id] = [x for x in reached_after if x not in reached_before]
        result.milestones_lost[side.user_id] = [x for x in reached_before if x not in reached_after]
    return result


async def prestige(session: AsyncSession, guild_id: int, user_id: int, from_level: int) -> PlayerStatus | None:
    """Move up one prestige level: wipes points, upgrades, the reserve and every
    animal. Keeps bonus rolls/claims, wishes and the roll, claim and trade
    counters. None if the required milestone isn't complete or the player isn't
    at `from_level` anymore (e.g. a double click). Doesn't commit."""
    milestone = milestones.required_milestone(from_level)
    if milestone is None:
        return None
    await _ensure_player(session, guild_id, user_id)
    species = await rescued_species(session, guild_id, user_id)
    pool = (await session.execute(select(Animal))).scalars().all()
    if milestone not in milestones.completed(species, pool):
        return None
    status = await _update_player(
        session, guild_id, user_id,
        PlayerState.prestige_level == from_level,
        prestige_level=from_level + 1,
        points=0,
        peak_points=0,
        extra_rolls=0,
        luck_level=0,
        claim_slots=0,
        reserve_slots=0,
        appeal_level=0,
        reserve_anchor=0,
        reserve_remainder=0,
    )
    if status is None:
        return None
    await session.execute(delete(Rescue).where(Rescue.guild_id == guild_id, Rescue.user_id == user_id))
    return status
