from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Animal, Rescue

# Rescue this many different species of a group to complete its achievement.
# The same for every group: difficulty comes from the group's makeup (how many
# common species it has), and the reward follows the difficulty.
ACHIEVEMENT_TARGET = 5

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
class Achievement:
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


ACHIEVEMENTS = [
    Achievement("Lézards et serpents", ("Squamata",), "easy"),
    Achievement("Rongeurs", ("Rodentia",), "easy"),
    Achievement("Félins", ("Felidae",), "easy"),
    Achievement("Bovidés", ("Bovidae",), "easy"),
    Achievement("Rapaces", ("Accipitriformes", "Falconiformes"), "easy"),
    Achievement("Primates", ("Primates",), "easy"),
    Achievement("Perroquets", ("Psittaciformes",), "easy"),
    Achievement("Grenouilles et crapauds", ("Anura",), "easy"),
    Achievement("Canidés", ("Canidae",), "easy"),
    Achievement("Mustélidés", ("Mustelidae",), "medium"),
    Achievement("Insectes", ("Insecta",), "medium"),
    Achievement("Canards, oies et cygnes", ("Anatidae",), "medium"),
    Achievement("Phoques", ("Phocidae",), "medium"),
    Achievement("Chouettes et hiboux", ("Strigiformes",), "medium"),
    Achievement("Manchots", ("Spheniscidae",), "medium"),
    Achievement("Tortues", ("Testudines",), "medium"),
    Achievement("Chauves-souris", ("Chiroptera",), "hard"),
    Achievement("Requins et raies", ("Chondrichthyes",), "medium"),
    Achievement(
        "Marsupiaux",
        ("Diprotodontia", "Dasyuromorphia", "Didelphimorphia", "Peramelemorphia",
         "Microbiotheria", "Paucituberculata", "Notoryctemorphia"),
        "medium",
    ),
    Achievement("Cervidés", ("Cervidae",), "medium"),
    Achievement("Corvidés", ("Corvidae",), "hard"),
    # River dolphins have families of their own.
    Achievement("Dauphins", ("Delphinidae", "Iniidae", "Platanistidae", "Pontoporiidae", "Lipotidae"), "medium"),
    Achievement("Rhinocéros, chevaux et tapirs", ("Perissodactyla",), "very_hard"),
    Achievement("Ours", ("Ursidae",), "very_hard"),
    Achievement("Crocodiliens", ("Crocodilia",), "very_hard"),
    Achievement("Grands singes", ("Hominidae",), "extreme"),
]


def progress(species: list[Animal], achievement: Achievement) -> int:
    """How many of `species` (distinct animals) count toward `achievement`."""
    return sum(achievement.includes(a) for a in species)


def completed(species: list[Animal]) -> list[Achievement]:
    return [a for a in ACHIEVEMENTS if progress(species, a) >= ACHIEVEMENT_TARGET]


def total_bonus(species: list[Animal]) -> int:
    """Reserve income bonus in %, summed over completed achievements."""
    return sum(a.bonus for a in completed(species))


async def rescued_species(session: AsyncSession, guild_id: int, user_id: int) -> list[Animal]:
    """The distinct animals a player has rescued. Rescues are never removed,
    so an achievement, once completed, stays completed."""
    stmt = (
        select(Animal)
        .where(
            Animal.id.in_(
                select(Rescue.animal_id).where(Rescue.guild_id == guild_id, Rescue.user_id == user_id)
            )
        )
    )
    return list((await session.execute(stmt)).scalars().all())
