import discord
from discord import app_commands
from discord.ext import commands

from ..economy import (
    APPEAL,
    BASE_CLAIMS,
    BASE_RESERVE_SLOTS,
    BASE_ROLLS,
    CLAIM_REGEN_SECONDS,
    CURRENCY_NAME,
    MAX_TRADES_PER_DAY,
    MILESTONE_POINTS_BONUS,
    RARITY_POINTS,
    RESERVE_CAP_SECONDS,
    ROLL_RESET_SECONDS,
)
from ..milestones import MILESTONES
from ..species_collections import COLLECTION_TARGET, DIFFICULTY
from .rolls import RARITY_LABEL, ROLL_PROTECTION_SECONDS, ROLL_TIMEOUT_SECONDS, format_wait, send_pages
from .wishes import MAX_WISHES


def _every(seconds: int) -> str:
    # "chaque heure", not "toutes les 1 h".
    return "chaque heure" if seconds == 3600 else f"toutes les {format_wait(seconds)}"


def _pages() -> list[discord.Embed]:
    """The tutorial, one embed per topic. Numbers come from the game's
    constants so the text follows any rebalancing."""
    # The mythic tier stays a surprise, as in the shop.
    rarities = "\n".join(
        f"· **{RARITY_LABEL[tier].capitalize()}** : {points} {CURRENCY_NAME}"
        for tier, points in RARITY_POINTS.items()
        if tier != "mythic"
    )
    bonuses = [bonus for _, bonus in DIFFICULTY.values()]
    badges = " ".join(m.badge for m in MILESTONES)
    best_aura = max(APPEAL)

    topics = [
        (
            "1) Bienvenue !\n \n",

            "La boucle du jeu :\n"
            "1. **Roll** : tire un animal au hasard avec `/rescue`.\n"
            "2. **Sauve-le** pour l'accueillir dans ta **réserve**.\n"
            f"3. Choisis tes meilleurs animaux comme **totems** : ils te rapportent des {CURRENCY_NAME}.\n"
            f"4. Dépense tes {CURRENCY_NAME} dans le `/shop` pour améliorer tes chances.\n"
            "5. Complète des **collections** et des **milestones** pour des bonus permanents.\n\n"
            "Chaque animal est une vraie espèce, avec sa fiche Wikipédia. Utilise ◀️ ▶️ pour lire la suite !",
        ),
        (
            "2) Les rolls",
            f"`/rescue` (ou `/rc`) tire un animal au hasard. Tu as **{BASE_ROLLS} rolls**, rechargés "
            f"{_every(ROLL_RESET_SECONDS)}, au même moment pour tout le monde.\n\n"
            "La rareté suit le **statut de conservation UICN** de l'espèce : plus elle est menacée dans la nature, "
            f"plus elle est rare. Chaque roll te rapporte des {CURRENCY_NAME}, même si tu ne sauves pas l'animal :\n"
            f"{rarities}\n \n"
            "Chaque roll tire aussi une **aura** (de ★ à " + "★" * best_aura + ") : "
            "plus elle est haute, plus l'animal rapportera s'il devient totem.",
        ),
        (
            "3) Sauver un animal",
            "Clique sur **Rescue** sous un roll pour sauver l'animal. Ça coûte un **claim**.\n\n"
            f"· Il t'en revient 1 {_every(CLAIM_REGEN_SECONDS)}, et tu peux en garder "
            f"{BASE_CLAIMS} d'avance (plus avec le `/shop`). "
            "Garde-le pour un animal qui en vaut la peine !\n"
            f"· Pendant {format_wait(ROLL_PROTECTION_SECONDS)}, seul celui qui a lancé le roll peut sauver l'animal. "
            f"Ensuite, n'importe qui peut le faire, jusqu'à {format_wait(ROLL_TIMEOUT_SECONDS)}.\n"
            "· Un même animal peut être sauvé plusieurs fois, par toi ou par d'autres : "
            "une deuxième copie avec une meilleure aura peut valoir le coup.\n\n"
            "`/reserve` montre tous les animaux que tu as sauvés (`/reserve @membre` pour ceux d'un autre joueur).",
        ),
        (
            "4) Les totems",
            f"Tes totems te rapportent des {CURRENCY_NAME} chaque heure, selon leur rareté et leur aura.\n\n"
            "· `/totem add <animal>` : fais d'un animal de ta réserve un totem.\n"
            "· `/totem remove <animal>` : retire-le.\n"
            "· `/totem` : vois tes totems et **récolte** ce qu'ils ont produit.\n\n"
            f"Tu commences avec {BASE_RESERVE_SLOTS} place de totem. Tes totems produisent pendant "
            f"{format_wait(RESERVE_CAP_SECONDS)} au maximum, puis s'arrêtent jusqu'à ta prochaine récolte."
            "Ouvrir le `/shop` récolte aussi.",
        ),
        (
            "5) La boutique",
            f"`/shop` dépense tes {CURRENCY_NAME} en améliorations permanentes :\n"
            "· 🟢 **Chance** : plus de chances de tirer des animaux rares.\n"
            "· 🔷 **Rolls** : plus de rolls par heure.\n"
            "· 🔺 **Place de totem** : un totem de plus.\n"
            "· 🟨 **Stock de claims** : garde plus de claims d'avance.\n"
            "· 🟣 **Aura** : de meilleures auras sur tes rolls.\n\n"
            "Certaines améliorations n'apparaissent qu'une fois que tu as assez de "
            f"{CURRENCY_NAME} pour les acheter. `/balance` résume tes {CURRENCY_NAME}, rolls et claims.",
        ),
        (
            "6) Collections et milestones",
            f"**Collections** : sauve **{COLLECTION_TARGET} espèces différentes** d'un groupe (Félidés, Ursidés…) "
            f"pour augmenter le revenu de tes totems, de +{min(bonuses)} % à +{max(bonuses)} % selon la difficulté. "
            "Les bonus s'additionnent. Voir `/collections`.\n\n"
            f"**Milestones** : sauve **toutes** les espèces d'un grand groupe (mammifères, oiseaux…) "
            f"pour gagner son badge ({badges}) et +{MILESTONE_POINTS_BONUS} % de {CURRENCY_NAME} sur tes rolls "
            "et tes totems. Voir `/milestones`.\n\n"
            "Les deux comptent les espèces que tu possèdes **en ce moment** : "
            "donner la dernière copie d'une espèce peut te faire perdre un bonus.",
        ),
        (
            "7) Trades et wishes",
            f"· `/wish add <animal>` : ajoute jusqu'à {MAX_WISHES} animaux à ta wishlist. "
            "Tu es mentionné quand quelqu'un d'autre les tire, pour tenter de les sauver après "
            f"les {format_wait(ROLL_PROTECTION_SECONDS)} de protection.\n"
            "· `/wish add Collection : <collection>` : souhaite une collection (une seule à la fois). "
            "Tu es mentionné quand quelqu'un d'autre tire une de ses espèces que tu n'as pas encore.\n"
            "· `/trade <membre>` : propose un échange d'animaux, ou fais un cadeau. "
            f"{MAX_TRADES_PER_DAY} échanges par jour. Un totem doit être retiré avant d'être échangé.\n"
            "· `/profile` : ton profil, tes badges, tes totems et ton score.\n"
            "· `/top` : les classements du serveur (score, revenu des totems, collections).",
        ),
        (
            "8) Pour aller plus loin",
            "· `/animals` : tous les animaux du jeu, filtrables par rareté ou collection. "
            "Le bouton **Fiches** les montre un par un, avec photo.\n"
            "· `/info <animal>` : la fiche complète d'un animal.\n"
            "· `/help` : la liste de toutes les commandes.\n\n"
            "Bon sauvetage ! 🐾",
        ),
    ]
    pages = []
    for number, (title, text) in enumerate(topics, 1):
        embed = discord.Embed(title=title, description=text, color=discord.Color.green())
        embed.set_footer(text=f"Tutoriel · {number}/{len(topics)}")
        pages.append(embed)
    return pages


class Tutorial(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="tutorial", aliases=["tu"], description="Explique comment jouer, étape par étape.")
    async def tutorial(self, ctx: commands.Context):
        await send_pages(ctx, _pages())

    # Hybrid aliases only apply to the prefix side, so /tu is its own command.
    @app_commands.command(name="tu", description="Raccourci pour /tutorial.")
    async def tu(self, interaction: discord.Interaction):
        ctx = await commands.Context.from_interaction(interaction)
        await self.tutorial.callback(self, ctx)


async def setup(bot: commands.Bot):
    await bot.add_cog(Tutorial(bot))
