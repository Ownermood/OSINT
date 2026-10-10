# OSINT Telegram Bot

## Setup
```bash
pip install -r requirements.txt
cp .env.example .env   # fill in BOT_TOKEN, API_BASE_URL, API_KEY, MONGO_URI
python bot.py
```

Secrets live only in `.env` (git-ignored) — never hardcode them in `bot.py`.
