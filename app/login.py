"""Interactive one-time Telegram login. Run: docker exec -it tgarr python -m app.login"""
from telethon.sync import TelegramClient

from . import config

if not (config.TG_API_ID and config.TG_API_HASH):
    raise SystemExit("TG_API_ID / TG_API_HASH missing in .env")

with TelegramClient(config.SESSION, config.TG_API_ID, config.TG_API_HASH) as c:  # prompts phone, code, 2FA
    me = c.get_me()
    print(f"\nLogged in as {me.first_name} (@{me.username or me.id}).")
    print("Now restart the app:  docker restart tgarr")
