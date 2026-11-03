import asyncio
import logging

import discord
from discord.ext import commands

from .config import DISCORD_TOKEN, PREFIX
from .db import init_db

intents = discord.Intents.default()
# Prefix commands (!rescue) need to read message text. This is a privileged
# intent: it must also be enabled in the Developer Portal (Bot tab).
intents.message_content = True
# help_command=None: our hybrid /help replaces discord.py's built-in !help.
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    # "!" is common in chat, so unknown "!something" messages are ignored silently.
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.NotOwner):
        await ctx.send("Cette commande est réservée au propriétaire du bot.", ephemeral=True)
        return
    if isinstance(error, (commands.BadArgument, commands.BadLiteralArgument)):
        await ctx.send(f"Argument invalide : {error}", ephemeral=True)
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Argument manquant : `{error.param.name}`. Voir `{PREFIX}help`.")
        return
    await commands.Bot.on_command_error(bot, ctx, error)  # default: log it


@bot.event
async def on_ready():
    print(f"Connecté en tant que {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"{len(synced)} commandes slash synchronisées")
    except Exception as exc:
        print(f"Échec de la synchronisation des commandes slash : {exc}")


async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    async with bot:
        await bot.load_extension("bot.cogs.rolls")
        await bot.load_extension("bot.cogs.economy")
        await bot.load_extension("bot.cogs.reserve")
        await bot.load_extension("bot.cogs.species_collections")
        await bot.load_extension("bot.cogs.milestones")
        await bot.load_extension("bot.cogs.prestige")
        await bot.load_extension("bot.cogs.profile")
        await bot.load_extension("bot.cogs.wishes")
        await bot.load_extension("bot.cogs.trades")
        await bot.load_extension("bot.cogs.help")
        await bot.load_extension("bot.cogs.admin")
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
