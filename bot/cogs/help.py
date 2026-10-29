import discord
from discord.ext import commands

from ..config import PREFIX


class Help(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="help", description="Liste toutes les commandes du bot.")
    async def help(self, ctx: commands.Context):
        # Built from the registered hybrid commands, so new ones show up
        # without editing this. Shortcuts (like /rc) are shown as aliases.
        lines = []
        for command in sorted(self.bot.commands, key=lambda c: c.name):
            if command.hidden:
                continue
            if isinstance(command, commands.HybridGroup):
                # A slash group runs its own callback as its fallback
                # subcommand (/reserve view), then come the others.
                if command.fallback:
                    lines.append(f"**/{command.name} {command.fallback}** — {command.description}")
                for sub in sorted(command.commands, key=lambda c: c.name):
                    params = "".join(f" <{name}>" for name in sub.clean_params)
                    lines.append(f"**/{command.name} {sub.name}{params}** — {sub.description}")
                continue
            params = "".join(f" <{name}>" for name in command.clean_params)
            aliases = "".join(f" (ou /{alias})" for alias in command.aliases)
            lines.append(f"**/{command.name}{params}**{aliases} — {command.description}")
        lines.append(f"\nChaque commande marche aussi avec `{PREFIX}` (ex. `{PREFIX}rescue`).")

        embed = discord.Embed(
            title="Commandes",
            description="\n".join(lines),
            color=discord.Color.green(),
        )
        await ctx.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Help(bot))
