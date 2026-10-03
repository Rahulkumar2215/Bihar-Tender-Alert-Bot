"""All settings come from environment variables (or a .env file next to the project)."""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # dotenv is optional
    pass


def _int(name, default):
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


DB_PATH = os.getenv("DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "tenders.db"))

PORTAL_URL = "https://eproc2.bihar.gov.in/EPSV2Web/openarea/tenderListingPage.action#latestTenders"
PORTAL_LINK = "https://eproc2.bihar.gov.in/EPSV2Web/openarea/tenderListingPage.action"

# Scraper politeness
DETAIL_DELAY_MS = _int("DETAIL_DELAY_MS", 1500)      # pause between detail lookups
MAX_DETAILS_PER_RUN = _int("MAX_DETAILS_PER_RUN", 400)
ELIG_BACKFILL_PER_RUN = _int("ELIG_BACKFILL_PER_RUN", 250)   # older tenders re-read for eligibility each run
HEADLESS = os.getenv("HEADLESS", "1") != "0"

# Alert behaviour
CLOSING_SOON_DAYS = _int("CLOSING_SOON_DAYS", 3)      # remind when a tender closes within N days
OPEN_LIST_LIMIT = _int("OPEN_LIST_LIMIT", 15)         # rows returned by the OPEN command

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# Channels that get the all-departments feed (bot must be a channel admin), comma separated
TELEGRAM_CHANNELS = [c.strip() for c in os.getenv("TELEGRAM_CHANNELS", "").split(",") if c.strip()]
# Channels get new + extended tenders every run; closing-soon reminders only if this is 1
TELEGRAM_CHANNEL_REMINDERS = os.getenv("TELEGRAM_CHANNEL_REMINDERS", "0") == "1"

# WhatsApp Cloud API (Meta)
WA_TOKEN = os.getenv("WA_TOKEN", "")                  # permanent system-user token
WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "")
WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "")    # any random string you choose
WA_APP_SECRET = os.getenv("WA_APP_SECRET", "")        # to verify webhook signatures
WA_TEMPLATE_NAME = os.getenv("WA_TEMPLATE_NAME", "tender_digest")
WA_TEMPLATE_LANG = os.getenv("WA_TEMPLATE_LANG", "en")
WA_API_VERSION = os.getenv("WA_API_VERSION", "v21.0")

# Reading NIT files with AI (only when a subscriber taps ✅ on a tender whose rules are only in the NIT).
# Free option: a Google Gemini API key from aistudio.google.com (no card needed). Used first if set.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# tried in order: the next one is used when one is busy, out of free quota or retired
GEMINI_MODELS = [m.strip() for m in os.getenv(
    "GEMINI_MODELS", "gemini-2.5-flash,gemini-3.5-flash-lite,gemini-3.5-flash").split(",") if m.strip()]
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
NIT_MODEL = os.getenv("NIT_MODEL", "claude-sonnet-5-5")
NIT_MAX_PAGES = _int("NIT_MAX_PAGES", 0)              # pages sent to the AI (0 = 15 on Gemini, 6 on Claude)
NIT_DAILY_LIMIT = _int("NIT_DAILY_LIMIT", 40)         # NITs read per day, all users together
NIT_PER_USER_DAILY = _int("NIT_PER_USER_DAILY", 5)    # NITs one person can trigger per day

FILES_PER_USER_DAILY = _int("FILES_PER_USER_DAILY", 20)  # BOQ / NIT downloads one person can ask for per day

# Owner's Telegram chat id(s): can send STATS to the bot and get a morning report
ADMIN_CHAT_IDS = [c.strip() for c in os.getenv("ADMIN_CHAT_IDS", "").split(",") if c.strip()]

DRY_RUN = os.getenv("DRY_RUN", "0") == "1"            # print messages instead of sending
