import os

from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///bot.db")
PREFIX = os.environ.get("COMMAND_PREFIX", "!")
