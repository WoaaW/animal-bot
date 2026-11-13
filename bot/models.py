from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Animal(Base):
    __tablename__ = "animals"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    scientific_name: Mapped[str] = mapped_column(String(100))
    rarity: Mapped[str] = mapped_column(String(20))
    # Lead section of the French Wikipedia article (up to a few thousand chars).
    description: Mapped[str] = mapped_column(Text, default="")
    image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    wiki_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # IUCN Red List category code (LC, NT, VU, EN, CR, EW, EX, DD), from Wikidata.
    iucn_status: Mapped[str | None] = mapped_column(String(2), nullable=True)
    # Scientific names of the animal's families, orders and classes (several
    # when Wikidata lists competing classifications), as "|Felidae|Carnivora|
    # Mammalia|". Collections (species_collections.py) match groups against it.
    taxa: Mapped[str | None] = mapped_column(Text, nullable=True)


class Rescue(Base):
    """One row per rescue. The same animal can be rescued any number of
    times, by different players or the same one, so there's no unique key."""

    __tablename__ = "rescues"

    id: Mapped[int] = mapped_column(primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    animal_id: Mapped[int] = mapped_column(ForeignKey("animals.id"))
    rescued_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Appeal (1-5 stars), rolled with each roll, so two rescues of the same
    # animal can differ. With the rarity, it sets what the animal earns in the
    # reserve (see economy.py). 0 means not rolled yet: init_db rolls one for
    # rescues made before the reserve existed.
    appeal: Mapped[int] = mapped_column(server_default="0")
    in_reserve: Mapped[bool] = mapped_column(default=False, server_default="0")

    animal: Mapped[Animal] = relationship()


class Wish(Base):
    """An animal a player wants: they're pinged when someone else rolls it.
    Kept after a rescue, since a copy with a better appeal is still worth it."""

    __tablename__ = "wishes"
    __table_args__ = (UniqueConstraint("guild_id", "user_id", "animal_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    animal_id: Mapped[int] = mapped_column(ForeignKey("animals.id"))

    animal: Mapped[Animal] = relationship()


class CollectionWish(Base):
    """A collection a player wants to fill: they're pinged when someone else
    rolls one of its species that they don't own yet."""

    __tablename__ = "collection_wishes"
    __table_args__ = (UniqueConstraint("guild_id", "user_id", "collection"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    # Collection.label: collections live in code, not in a table, so renaming
    # one in COLLECTIONS orphans its wishes (they're then just ignored).
    collection: Mapped[str] = mapped_column(String(100))


class PlayerState(Base):
    """Per-player, per-guild state: currency, roll/claim counters, shop upgrades."""

    __tablename__ = "player_states"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    points: Mapped[int] = mapped_column(default=0)
    # Highest balance ever reached: shop upgrades unlock (become visible) once
    # it covers their first level, and stay unlocked after spending.
    peak_points: Mapped[int] = mapped_column(default=0, server_default="0")
    # Rolls used during rolls_period (a reset-window index, see economy.py).
    # When the period moves on, the count is treated as 0.
    rolls_used: Mapped[int] = mapped_column(default=0, server_default="0")
    rolls_period: Mapped[int] = mapped_column(default=0, server_default="0")
    # Claims regenerate one per CLAIM_REGEN_SECONDS, on the clock tick, up to a
    # cap. Stored as a single anchor time:
    # available = (last tick - claims_anchor) // regen, capped.
    # 0 (the default) means a full stock. See consume_claim in economy.py.
    claims_anchor: Mapped[int] = mapped_column(default=0, server_default="0")
    # Extra rolls/claims handed out (e.g. /admin give): spent only once the
    # regular ones run out, never expire, no cap.
    bonus_rolls: Mapped[int] = mapped_column(default=0, server_default="0")
    bonus_claims: Mapped[int] = mapped_column(default=0, server_default="0")
    # Shop upgrades, stored as levels/bonuses over the defaults, so changing a
    # default (BASE_ROLLS, RARITY_ODDS...) still applies to every player.
    extra_rolls: Mapped[int] = mapped_column(default=0, server_default="0")
    luck_level: Mapped[int] = mapped_column(default=0, server_default="0")
    claim_slots: Mapped[int] = mapped_column(default=0, server_default="0")
    reserve_slots: Mapped[int] = mapped_column(default=0, server_default="0")
    appeal_level: Mapped[int] = mapped_column(default=0, server_default="0")
    # Reserve income is counted up to reserve_anchor (a timestamp) and banked
    # in reserve_remainder, in point-seconds (/3600), until /totem view or
    # /shop pays it out (the fraction of a point stays banked).
    # reserve_elapsed: seconds of production counted since that last payout,
    # capped at RESERVE_CAP_SECONDS.
    reserve_anchor: Mapped[int] = mapped_column(default=0, server_default="0")
    reserve_remainder: Mapped[int] = mapped_column(default=0, server_default="0")
    reserve_elapsed: Mapped[int] = mapped_column(default=0, server_default="0")
    # Trades used during trades_period (a day index, see economy.py), counted
    # like rolls_used.
    trades_used: Mapped[int] = mapped_column(default=0, server_default="0")
    trades_period: Mapped[int] = mapped_column(default=0, server_default="0")
    # How many times the player prestiged; also picks the next required milestone.
    prestige_level: Mapped[int] = mapped_column(default=0, server_default="0")
    # The special roll (/rescue <groupe>): 1 once bought in the shop. Usable once
    # per day: special_roll_period is the day index (see economy.py) of the
    # last use, 0 for never.
    special_roll: Mapped[int] = mapped_column(default=0, server_default="0")
    special_roll_period: Mapped[int] = mapped_column(default=0, server_default="0")
