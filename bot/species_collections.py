from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Animal, Rescue

# Rescue this many different species of a group to complete its collection.
# The same for every group: difficulty comes from the group's makeup (how many
# common species it has), and the reward follows the difficulty.
COLLECTION_TARGET = 5

# Difficulty → (label, bonus in % on reserve income). The tiers follow the
# expected hours of rolls to find 5 species of each group (luck 0, 10 rolls/h):
# up to 25 h easy, 50 h medium, 75 h hard, 100 h very hard, beyond extreme.
DIFFICULTY = {
    "easy": ("Facile", 3),
    "medium": ("Moyen", 6),
    "hard": ("Difficile", 12),
    "very_hard": ("Très difficile", 18),
    "extreme": ("Extrême", 30),
}


@dataclass(frozen=True)
class Collection:
    label: str
    # Scientific names of families/orders/classes; an animal belongs to the
    # group if its Animal.taxa contains any of them.
    taxa: tuple[str, ...]
    difficulty: str

    @property
    def bonus(self) -> int:
        return DIFFICULTY[self.difficulty][1]

    def includes(self, animal: Animal) -> bool:
        return bool(animal.taxa) and any(f"|{taxon}|" in animal.taxa for taxon in self.taxa)


COLLECTIONS = [
    Collection("Squamates", ("Squamata",), "easy"),
    Collection("Rongeurs", ("Rodentia",), "easy"),
    Collection("Félidés", ("Felidae",), "easy"),
    Collection("Bovidés", ("Bovidae",), "easy"),
    # Diurnal raptors minus the falcons (Falconiformes, closer to parrots):
    # hawks and eagles, New World vultures, osprey and secretary bird.
    Collection("Accipitriformes", ("Accipitriformes",), "easy"),
    Collection("Primates", ("Primates",), "easy"),
    Collection("Psittaciformes", ("Psittaciformes",), "easy"),
    Collection("Anoures", ("Anura",), "easy"),
    Collection("Canidés", ("Canidae",), "easy"),
    Collection("Mustélidés", ("Mustelidae",), "medium"),
    Collection("Insectes", ("Insecta",), "medium"),
    Collection("Anatidés", ("Anatidae",), "medium"),
    Collection("Phocidés", ("Phocidae",), "medium"),
    Collection("Strigiformes", ("Strigiformes",), "medium"),
    Collection("Sphénisciformes", ("Spheniscidae",), "medium"),
    Collection("Chéloniens", ("Testudines",), "medium"),
    Collection("Chiroptères", ("Chiroptera",), "hard"),
    Collection("Chondrichtyens", ("Chondrichthyes",), "medium"),
    Collection(
        "Marsupiaux",
        ("Diprotodontia", "Dasyuromorphia", "Didelphimorphia", "Peramelemorphia",
         "Microbiotheria", "Paucituberculata", "Notoryctemorphia"),
        "medium",
    ),
    Collection("Cervidés", ("Cervidae",), "medium"),
    Collection("Corvidés", ("Corvidae",), "hard"),
    # Toothed whales. Odontoceti is a suborder, a rank the seed doesn't fetch
    # (TAXON_RANKS), so list its families.
    Collection(
        "Odontocètes",
        ("Delphinidae", "Iniidae", "Platanistidae", "Pontoporiidae", "Lipotidae",
         "Phocoenidae", "Physeteridae", "Kogiidae", "Ziphiidae", "Monodontidae"),
        "medium",
    ),
    Collection("Périssodactyles", ("Perissodactyla",), "very_hard"),
    Collection("Ursidés", ("Ursidae",), "very_hard"),
    Collection("Crocodiliens", ("Crocodilia",), "very_hard"),
    Collection("Grands singes", ("Hominidae",), "extreme"),
]


def progress(species: list[Animal], collection: Collection) -> int:
    """How many of `species` (distinct animals) count toward `collection`."""
    return sum(collection.includes(a) for a in species)


def completed(species: list[Animal]) -> list[Collection]:
    return [a for a in COLLECTIONS if progress(species, a) >= COLLECTION_TARGET]


def total_bonus(species: list[Animal]) -> int:
    """Reserve income bonus in %, summed over completed collections."""
    return sum(a.bonus for a in completed(species))


async def rescued_species(session: AsyncSession, guild_id: int, user_id: int) -> list[Animal]:
    """The distinct animals a player currently owns. Completion is computed
    from this, so trading away the last copy of a species can undo an collection."""
    stmt = (
        select(Animal)
        .where(
            Animal.id.in_(
                select(Rescue.animal_id).where(Rescue.guild_id == guild_id, Rescue.user_id == user_id)
            )
        )
    )
    return list((await session.execute(stmt)).scalars().all())
