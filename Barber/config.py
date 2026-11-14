import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
XAI_API_KEY = os.getenv("XAI_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

def arcade_webapp_url(env=None) -> str:
    """The games hub URL. ARCADE_WEBAPP_URL, else the older GAMES_WEBAPP_URL / SNAKE_WEBAPP_URL names.
    An old single-game page (…/snake_app.html, …/tetris_app.html) is mapped to the hub at the site root:
    those pages redirect to one game and would drop the ?game= of every other button."""
    from urllib.parse import urlsplit, urlunsplit
    env = os.environ if env is None else env
    url = env.get("ARCADE_WEBAPP_URL") or env.get("GAMES_WEBAPP_URL") or env.get("SNAKE_WEBAPP_URL") \
        or "https://yourdomain.com/"
    parts = urlsplit(url)
    if parts.path.rstrip("/").split("/")[-1] in ("snake_app.html", "tetris_app.html", "snake", "tetris"):
        url = urlunsplit((parts.scheme, parts.netloc, "/", "", ""))
    return url


# Arcade Mini App (public/index.html on Vercel)
ARCADE_WEBAPP_URL = arcade_webapp_url()

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

# --- Chat menu button (left of the message field).
# "booking" (default): the shop's Altegio online-booking page, as the live bot has today.
# "arcade": Barber Games. "none": leave whatever is set in BotFather untouched.
CHAT_MENU_BUTTON = os.getenv("CHAT_MENU_BUTTON", "booking").strip().lower()
CHAT_MENU_BOOKING_TEXT = os.getenv("CHAT_MENU_BOOKING_TEXT", "Записатись")
BOOKING_WIDGET_URL = os.getenv("BOOKING_WIDGET_URL", "https://n308628.alteg.io/company/297851/personal/menu?o=")

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
