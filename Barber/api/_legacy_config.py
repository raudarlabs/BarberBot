"""Settings for the Telegram bot restored from the April 2026 live deployment (api/webhook.py)."""
import os

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
XAI_API_KEY = os.getenv("XAI_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Games Mini App settings (landing page with game selection)
GAMES_WEBAPP_URL = os.getenv("GAMES_WEBAPP_URL", os.getenv("SNAKE_WEBAPP_URL", "https://yourdomain.com/"))
SNAKE_API_PORT = int(os.getenv("SNAKE_API_PORT", "8080"))

# xAI / Grok API settings
XAI_BASE_URL = "https://api.x.ai/v1"
VISION_MODEL = "grok-4-1-fast-non-reasoning"

# OpenAI image generation settings
IMAGE_MODEL = "gpt-image-1"
NUM_HAIRSTYLES = 6

# Messages
WELCOME_MESSAGE = (
    "Привіт! Ласкаво просимо до Custom True Style Bot! \n\n"
    "Надішли мені чітке фото свого обличчя — і я підберу 6 персональних варіантів зачіски спеціально для тебе!\n\n"
    "Поради для найкращого результату:\n"
    "- Гарне освітлення обличчя\n"
    "- Фото анфас (дивись прямо в камеру)\n"
    "- Волосся добре видно\n\n"
    "Давай спробуємо!"
)

PROCESSING_MESSAGE = (
    "Хмммм, цікаве волосся... Зараз мої AI-ножиці подумають!\n\n"
    "Генерую 6 варіантів зачісок спеціально для тебе...\n"
    "Почекай хвилинку — навіть найкращий барбер потребує часу!"
)

ERROR_MESSAGE = (
    "Ой, щось пішло не так... Мої AI-ножиці заїли!\n\n"
    "Спробуй ще раз з чітким, добре освітленим фото обличчя. "
    "Обіцяю, наступного разу все вийде!"
)

NO_FACE_MESSAGE = (
    "Хм, я не можу знайти обличчя на фото...\n"
    "Може, ти надіслав фото свого кота? Він гарний, але зачіски підбираю тільки людям!\n\n"
    "Надішли, будь ласка, чітке фото анфас з гарним освітленням."
)
