from dataclasses import dataclass

from .models import Animal


@dataclass(frozen=True)
class Milestone:
    """A big group of animals to rescue entirely. Unlike collections, it gives
    no reserve income."""
    label: str
    # Scientific names of orders/classes; an animal belongs to the group if its
    # Animal.taxa contains any of them.
    taxa: tuple[str, ...]

    def includes(self, animal: Animal) -> bool:
        return bool(animal.taxa) and any(f"|{taxon}|" in animal.taxa for taxon in self.taxa)


# Every animal of the pool must fall in exactly one group. Fish and reptiles
# have no usable class: Wikidata splits fish into several classes (Gadiformes
# has none at all) and nests birds inside Reptilia, so reptiles go by order.
MILESTONES = [
    Milestone("Mammifères", ("Mammalia",)),
    Milestone("Oiseaux", ("Aves",)),
    Milestone("Reptiles", ("Squamata", "Testudines", "Crocodilia", "Rhynchocephalia")),
    Milestone("Amphibiens", ("Amphibia",)),
    Milestone(
        "Poissons",
        ("Actinopteri", "Actinopterygii", "Chondrichthyes", "Coelacanthi", "Dipneusti",
         "Petromyzonti", "Petromyzontida", "Cephalaspidomorphi", "Myxini", "Gadiformes", "Teleostei"),
    ),
    Milestone(
        "Invertébrés",
        ("Insecta", "Arachnida", "Malacostraca", "Merostomata", "Bivalvia", "Cephalopoda", "Gastropoda", "Hydrozoa"),
    ),
]


def progress(species: list[Animal], milestone: Milestone) -> int:
    """How many of `species` (distinct animals) count toward `milestone`."""
    return sum(milestone.includes(a) for a in species)


def completed(species: list[Animal], pool: list[Animal]) -> list[Milestone]:
    """Milestones whose every animal of `pool` is in `species`. Computed like
    collections, so losing a species or a new animal in the pool undoes one."""
    owned = {a.id for a in species}
    return [
        m for m in MILESTONES
        if all(a.id in owned for a in pool if m.includes(a)) and any(m.includes(a) for a in pool)
    ]
