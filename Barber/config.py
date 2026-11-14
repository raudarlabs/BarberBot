import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
XAI_API_KEY = os.getenv("XAI_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Arcade Mini App (public/index.html on Vercel). SNAKE_WEBAPP_URL is the old name.
ARCADE_WEBAPP_URL = os.getenv("ARCADE_WEBAPP_URL") or os.getenv(
    "SNAKE_WEBAPP_URL", "https://yourdomain.com/"
)

# xAI / Grok API settings
XAI_BASE_URL = "https://api.x.ai/v1"
VISION_MODEL = "grok-4-1-fast-non-reasoning"

# OpenAI image generation settings
IMAGE_MODEL = "gpt-image-1"
NUM_HAIRSTYLES = 6

# Messages
# Messages (Ukrainian; the menu, booking and reminder copy lives in texts.py)
PROCESSING_MESSAGE = (
    "Аналізую фото і готую 6 варіантів стрижки… ✂️\n"
    "Це може зайняти до хвилини."
)

ERROR_MESSAGE = (
    "Вибачте, не вдалося обробити фото. "
    "Спробуйте ще раз з чітким фото при гарному світлі."
)

NO_FACE_MESSAGE = (
    "Не вдалося чітко розгледіти обличчя на фото. "
    "Надішліть, будь ласка, фронтальне фото при гарному освітленні."
)

# --- Altegio booking + Telegram reminders (see README "Online booking & reminders")
# Tokens: ALTEGIO_PARTNER_TOKEN, ALTEGIO_USER_TOKEN (read by altegio.make_client).
# ALTEGIO_MOCK=1 runs booking against fixtures/altegio.json when tokens are missing.
DEFAULT_LOCATION_NAMES = {
    297851: "вул. Володимира Великого, 75",
    708761: "вул. Гагаріна, 29/2",
    1367275: "вул. Смілянська, 2",
}


def _location_names():
    """ALTEGIO_LOCATION_NAMES="297851=вул. ...;708761=..." overrides the defaults."""
    names = dict(DEFAULT_LOCATION_NAMES)
    for part in (os.getenv("ALTEGIO_LOCATION_NAMES") or "").split(";"):
        loc, _, name = part.partition("=")
        if loc.strip().isdigit() and name.strip():
            names[int(loc)] = name.strip()
    return names


def booking_locations():
    """{location_id: address} offered in the bot, in ALTEGIO_LOCATION_IDS order."""
    names = _location_names()
    ids = [int(x) for x in (os.getenv("ALTEGIO_LOCATION_IDS") or "297851").split(",") if x.strip().isdigit()]
    return {loc: names.get(loc, "Custom True #%d" % loc) for loc in ids}


REMINDER_INTERVAL = int(os.getenv("REMINDER_INTERVAL", "600"))  # seconds between Altegio polls

# --- Shop facts for the "📍 Контакти" screen. Mirrors the website's single source of truth
# (Barber site repo: src/data/site.ts) - change them there and here together.
SHOP = {
    "address": os.getenv("SHOP_ADDRESS", "Черкаси, вул. Володимира Великого, 75"),
    "hours": os.getenv("SHOP_HOURS", "Щодня 9:00 — 20:00"),
    "phone": os.getenv("SHOP_PHONE", "+380980337733"),
    "phone_display": os.getenv("SHOP_PHONE_DISPLAY", "+380 98 033 77 33"),
    "maps": os.getenv("SHOP_MAPS_URL", "https://maps.app.goo.gl/BNCbQUyHSZDvVZv17"),
    "instagram": os.getenv("SHOP_INSTAGRAM_URL", "https://www.instagram.com/custom_true_barbershop"),
}
