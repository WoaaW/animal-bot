"""Everything the web page shows about one guild, computed from the database
the same way the bot computes it (score, totem income, collections...).
Read-only: it never writes, so it never collects anyone's totem income."""

import time
from collections import defaultdict
from datetime import timezone

from sqlalchemy import select

from .. import milestones
from ..economy import (
    BASE_RESERVE_SLOTS,
    BASE_ROLLS,
    BASE_CLAIMS,
    MAX_TRADES_PER_DAY,
    UPGRADES,
    _status,
    animal_score,
    milestone_bonus,
    reserve_income,
    reserve_rate,
    upgrade_max_level,
)
from ..models import Animal, CollectionWish, PlayerState, Rescue, Wish
from ..species_collections import COLLECTION_TARGET, COLLECTIONS, DIFFICULTY, completed, total_bonus

# Copied rather than imported from bot.cogs.rolls, which pulls in the whole
# Discord cog: the web server doesn't need it.
RARITY_ORDER = ["common", "uncommon", "rare", "epic", "legendary", "mythic"]
RARITY_LABEL = {
    "common": "commun",
    "uncommon": "peu commun",
    "rare": "rare",
    "epic": "épique",
    "legendary": "légendaire",
    "mythic": "mythique",
}
UPGRADE_LABEL = {
    "luck": "Chance",
    "rolls": "Rolls",
    "reserve": "Places de totem",
    "claims": "Stock de claims",
    "appeal": "Aura",
    "special": "Rolls spéciaux",
    "multi": "Roll multiple",
}


async def guild_ids(session) -> list[int]:
    """Guilds with at least one rescue or player."""
    rescued = set((await session.execute(select(Rescue.guild_id).distinct())).scalars())
    players = set((await session.execute(select(PlayerState.guild_id).distinct())).scalars())
    return sorted(rescued | players)


async def guild_data(session, guild_id: int) -> dict:
    pool = list((await session.execute(select(Animal))).scalars())
    animals = {a.id: a for a in pool}
    rescues = list(
        (await session.execute(select(Rescue).where(Rescue.guild_id == guild_id).order_by(Rescue.rescued_at))).scalars()
    )
    states = {
        s.user_id: s
        for s in (await session.execute(select(PlayerState).where(PlayerState.guild_id == guild_id))).scalars()
    }
    wishes: dict[int, list[int]] = defaultdict(list)
    for user_id, animal_id in (
        await session.execute(select(Wish.user_id, Wish.animal_id).where(Wish.guild_id == guild_id))
    ).all():
        wishes[user_id].append(animal_id)
    collection_wish: dict[int, str] = {}
    for user_id, label in (
        await session.execute(
            select(CollectionWish.user_id, CollectionWish.collection).where(CollectionWish.guild_id == guild_id)
        )
    ).all():
        collection_wish[user_id] = label

    by_player: dict[int, list[Rescue]] = defaultdict(list)
    for rescue in rescues:
        by_player[rescue.user_id].append(rescue)

    collection_index = {c.label: i for i, c in enumerate(COLLECTIONS)}
    milestone_index = {m.label: i for i, m in enumerate(milestones.MILESTONES)}

    players = []
    for user_id in sorted(set(by_player) | set(states) | set(wishes)):
        owned = by_player.get(user_id, [])
        species = [animals[i] for i in {r.animal_id for r in owned}]
        done = completed(species)
        reached = milestones.completed(species, pool)
        base = sum(reserve_income(animals[r.animal_id].rarity, r.appeal) for r in owned if r.in_reserve)
        bonus = total_bonus(species)
        milestone_pct = milestone_bonus(len(reached))
        state = states.get(user_id)
        status = _status(state) if state is not None else None
        players.append(
            {
                "id": str(user_id),  # 64-bit: too big for a JS number
                "points": state.points if state else 0,
                "peak": status.peak_points if status else 0,
                "score": sum(animal_score(animals[r.animal_id].rarity, r.appeal) for r in owned),
                "species": len(species),
                "rescues": len(owned),
                "income": reserve_rate(base, bonus, milestone_pct),
                "incomeBase": base,
                "collectionBonus": bonus,
                "milestoneBonus": milestone_pct,
                "collectionsDone": [collection_index[c.label] for c in done],
                "collectionProgress": [
                    sum(c.includes(a) for a in species) for c in COLLECTIONS
                ],
                "milestonesDone": [milestone_index[m.label] for m in reached],
                "milestoneProgress": [
                    sum(m.includes(a) for a in species) for m in milestones.MILESTONES
                ],
                "copies": [
                    {
                        "id": r.id,
                        "animal": r.animal_id,
                        "appeal": r.appeal,
                        "totem": r.in_reserve,
                        # rescued_at is naive UTC.
                        "at": int(r.rescued_at.replace(tzinfo=timezone.utc).timestamp()) if r.rescued_at else None,
                    }
                    for r in owned
                ],
                "wishes": wishes.get(user_id, []),
                "collectionWish": collection_index.get(collection_wish.get(user_id)),
                "upgrades": {
                    key: getattr(state, column.key) if state else 0 for key, (column, *_rest) in UPGRADES.items()
                },
                "totemSlots": BASE_RESERVE_SLOTS + (state.reserve_slots if state else 0),
                "maxRolls": BASE_ROLLS + (state.extra_rolls if state else 0),
                "maxClaims": BASE_CLAIMS + (state.claim_slots if state else 0),
                "tradesLeft": status.trades_left if status else None,
            }
        )

    # Only the animals someone owns or wishes for, plus every member of a
    # collection or milestone (needed for "what's missing"): in practice the
    # whole pool, so send it all, compactly.
    return {
        "guildId": str(guild_id),
        "generatedAt": int(time.time()),
        "collectionTarget": COLLECTION_TARGET,
        "maxTrades": MAX_TRADES_PER_DAY,
        "rarities": [{"key": k, "label": RARITY_LABEL[k]} for k in RARITY_ORDER],
        "collections": [
            {
                "label": c.label,
                "difficulty": DIFFICULTY[c.difficulty][0],
                "bonus": c.bonus,
                "size": sum(c.includes(a) for a in pool),
            }
            for c in COLLECTIONS
        ],
        "milestones": [
            {"label": m.label, "badge": m.badge, "size": sum(m.includes(a) for a in pool)}
            for m in milestones.MILESTONES
        ],
        "upgrades": [
            {"key": key, "label": UPGRADE_LABEL[key], "max": upgrade_max_level(key)} for key in UPGRADES
        ],
        "animals": {
            str(a.id): {
                "name": a.name,
                "sci": a.scientific_name,
                "rarity": a.rarity,
                "image": a.image_url,
                "wiki": a.wiki_url,
                "iucn": a.iucn_status,
                "collections": [i for i, c in enumerate(COLLECTIONS) if c.includes(a)],
                "milestone": next((i for i, m in enumerate(milestones.MILESTONES) if m.includes(a)), None),
                "income": [reserve_income(a.rarity, stars) for stars in range(1, 6)],
            }
            for a in pool
        },
        "players": players,
    }
