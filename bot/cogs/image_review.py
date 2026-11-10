"""Temporary: /newimages shows the animals whose image was just replaced, to check them in Discord.
Delete this file and its load_extension line in __main__.py once reviewed."""
import discord
from discord.ext import commands
from sqlalchemy import select

from ..db import SessionLocal
from ..models import Animal
from .rolls import animal_embed, send_pages

REVIEWED = [
    "Marsouin de Burmeister",
    "Matamata",
    "Morpho bleu",
    "Potto de Bosman",
    "Chlamyphore tronqué",
    "Morue de l'Atlantique",
    "Phoque moine de Méditerranée",
    "Requin-taupe commun",
    "Chinchilla à longue queue",
    "Dauphin de l'Irrawaddy",
    "Grenouille du lac Titicaca",
    "Grenouille Goliath",
    "Napoléon",
    "Pangolin géant",
    "Phoque à capuchon",
    "Baiji",
    "Courlis esquimau",
    "Cœlacanthe",
    "Esturgeon européen",
    "Grand esturgeon",
    "Gibbon de Hainan",
    "Orang-outan de Tapanuli",
    "Paresseux nain",
    "Pic à bec ivoire",
    "Pithécophage des Philippines",
    "Saola",
    "Tortue géante à carapace molle du Yangtsé",
]


class ImageReview(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(
        name="newimages", description="(Temporaire) Montre les animaux dont l'image vient de changer.", hidden=True
    )
    async def newimages(self, ctx: commands.Context):
        await ctx.defer(ephemeral=True)
        async with SessionLocal() as session:
            animals = {a.name: a for a in (await session.scalars(select(Animal).where(Animal.name.in_(REVIEWED)))).all()}
        cards = []
        for number, name in enumerate(REVIEWED, 1):
            animal = animals.get(name)
            if animal is None:
                cards.append(discord.Embed(title=name, description="Introuvable : relance `python -m bot.seed`."))
                continue
            card = animal_embed(animal, full=False)
            card.set_footer(text=f"Image {number}/{len(REVIEWED)} · {animal.image_url or 'aucune image'}")
            cards.append(card)
        await send_pages(ctx, cards, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ImageReview(bot))
