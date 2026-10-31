"""
config.py - settings loaded from environment / .env

If you already have a config.py: keep YOUR values and just add the lines marked NEW.
"""
import os
import re

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:      # python-dotenv is optional
    pass

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SECRET_KEY = os.getenv("SECRET_KEY", "")          # python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
# Supabase: Dashboard -> Connect -> "Session pooler" connection string (IPv4 friendly, works on Railway)
DATABASE_URL = os.getenv("DATABASE_URL", "")
ADMIN_IDS = {int(x) for x in re.split(r"[,\s]+", os.getenv("ADMIN_IDS", "")) if x.strip().isdigit()}

# ---- limits ----------------------------------------------------------------
FREE_LINK_LIMIT = 2
MAX_MESSAGE_LEN = 1000
MAX_REPLIES_PER_MESSAGE = 5
RATE_LIMIT_COUNT = 10            # max messages ...
RATE_LIMIT_WINDOW = 60           # ... per this many seconds

# ---- Pro (monthly subscription, Telegram Stars) ----------------------------  NEW
PRO_PRICE_STARS = int(os.getenv("PRO_PRICE_STARS", "150"))   # price per month
PRO_PERIOD_DAYS = 30                                          # one-time "30-day pass" length
SUBSCRIPTION_PERIOD = 2592000    # seconds. Telegram currently only accepts exactly 30 days.
PRO_EXPIRY_WARNING_DAYS = 3      # warn non-renewing Pro users this many days before the end

# ---- new feature settings ---------------------------------------------------  NEW
INBOX_PAGE_SIZE = 8              # messages per inbox page
MAX_EDITS_PER_MESSAGE = 3        # how often a Pro sender may edit one message
AD_TEXT = os.getenv("AD_TEXT", "")   # optional sponsor line shown to FREE users only (Pro = "No ads")

# ---- gifts: code -> (emoji, name, price in Stars) ---------------------------
GIFTS = {
    "rose":    ("🌹", "Rose", 5),
    "heart":   ("💖", "Heart", 10),
    "bear":    ("🧸", "Teddy bear", 25),
    "cake":    ("🎂", "Cake", 50),
    "diamond": ("💎", "Diamond", 100),
    "rocket":  ("🚀", "Rocket", 250),
}


def validate() -> None:
    missing = [n for n, v in (("BOT_TOKEN", BOT_TOKEN), ("SECRET_KEY", SECRET_KEY), ("DATABASE_URL", DATABASE_URL)) if not v]
    if missing:
        raise SystemExit(f"Missing required settings: {', '.join(missing)} (set them in Railway → Variables, or .env locally)")
    if PRO_PRICE_STARS < 1:
        raise SystemExit("PRO_PRICE_STARS must be at least 1")