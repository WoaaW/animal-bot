import os

from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///bot.db")
PREFIX = os.environ.get("COMMAND_PREFIX", "!")
# Local model answering when the bot is mentioned (bot/cogs/chat.py).
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:0.5b")
