import asyncio
import io
import json
import logging
import os
import sys
import time
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ChatAction

from config import (
    TELEGRAM_BOT_TOKEN,
    PROCESSING_MESSAGE,
    ERROR_MESSAGE,
    NO_FACE_MESSAGE,
    ARCADE_WEBAPP_URL,
)
from config import (
    BOOKING_WIDGET_URL,
    CHAT_MENU_BOOKING_TEXT,
    CHAT_MENU_BUTTON,
    REMINDER_INTERVAL,
    SHOP,
    booking_locations,
)
from openai_service import analyze_photo_and_get_hairstyles, generate_all_hairstyle_images
import admin
import booking
import menu
import reminders
import reviews
import team
import waitlist
import texts
from altegio import make_client
from linkstore import LinkStore
from voice import make_assistant

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "api"))
import _kv  # noqa: E402

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
# httpx logs every request URL at INFO, and Telegram's URLs contain the bot token: keep it out of the logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

# Bottom menu: Записатися · Мій запис / Підібрати стрижку · Ігри / Послуги й ціни · Контакти.
# "🎮 Ігри" answers with INLINE web_app buttons (menu.games_markup), never a reply-keyboard
# web_app button: only inline / chat-menu launches hand the Mini App the signed initData
# the leaderboard API needs to know who is playing.
MAIN_MENU_KEYBOARD = menu.main_menu_keyboard()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles /start command."""
    await update.message.reply_text(texts.WELCOME, reply_markup=MAIN_MENU_KEYBOARD)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles /help command."""
    await update.message.reply_text(
        texts.HELP,
        reply_markup=MAIN_MENU_KEYBOARD,
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Main handler: receives user photo, runs AI analysis,
    generates 6 hairstyle options, sends them back.
    """
    user = update.effective_user
    logger.info(f"Received photo from user {user.id} (@{user.username})")

    # Send processing message
    processing_msg = await update.message.reply_text(PROCESSING_MESSAGE)

    try:
        # Download the highest-resolution version of the photo
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.UPLOAD_PHOTO,
        )

        # A compressed photo, or a photo sent as a file (document)
        if update.message.photo:
            photo_file = await update.message.photo[-1].get_file()
        else:
            photo_file = await update.message.document.get_file()
        photo_bytes = await photo_file.download_as_bytearray()

        logger.info(f"Downloaded photo ({len(photo_bytes)} bytes) from user {user.id}")

        # Step 1: Analyze photo with GPT-4 Vision
        await processing_msg.edit_text(
            "Крок 1/3: аналізую форму обличчя і тип волосся…"
        )

        analysis = await analyze_photo_and_get_hairstyles(bytes(photo_bytes))

        hairstyles = analysis.get("hairstyles", [])
        if not hairstyles:
            await processing_msg.edit_text(NO_FACE_MESSAGE)
            return

        hair_type = analysis.get("hair_type", "unknown")
        logger.info(
            f"Analysis for user {user.id}: hair={hair_type}, "
            f"styles={len(hairstyles)}"
        )

        # Step 2: Edit original photo with gpt-image-1
        await processing_msg.edit_text(
            "Крок 2/3: генерую 6 стрижок на вашому фото…\n"
            "Це займає 30–60 секунд."
        )

        generated = await generate_all_hairstyle_images(analysis, bytes(photo_bytes))

        if not generated:
            await processing_msg.edit_text(ERROR_MESSAGE)
            return

        # Step 3: Send results
        await processing_msg.edit_text(
            f"Крок 3/3: надсилаю {len(generated)} варіантів…"
        )

        # Build summary caption
        summary_lines = [
            f"Ваші {len(generated)} варіантів стрижки!\n",
            f"Тип волосся: {hair_type}\n",
        ]
        for i, style in enumerate(generated, 1):
            summary_lines.append(f"{i}. {style['name']}")

        await update.message.reply_text("\n".join(summary_lines))

        # Send each hairstyle as a photo with caption
        for i, style in enumerate(generated, 1):
            caption = (
                f"Варіант {i}: {style['name']}\n\n"
                f"{style['reason']}"
            )
            # Truncate caption to Telegram's 1024-char limit
            if len(caption) > 1024:
                caption = caption[:1021] + "..."

            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=io.BytesIO(style["image_bytes"]),
                caption=caption,
            )

        await processing_msg.delete()
        await update.message.reply_text(
            "Готово! Покажіть варіанти майстру й оберіть улюблений ✂️\n"
            "Записатися — кнопка «📅 Записатися» нижче.",
            reply_markup=MAIN_MENU_KEYBOARD,
        )

        logger.info(f"Successfully sent {len(generated)} hairstyles to user {user.id}")

    except Exception as e:
        logger.error(f"Error processing photo for user {user.id}: {e}", exc_info=True)
        await processing_msg.edit_text(ERROR_MESSAGE)


def prepare(app: Application) -> None:
    """Shared services in bot_data (Altegio client, phone links, menu...). Used by polling and webhook."""
    client = make_client()  # None when Altegio tokens are missing (and ALTEGIO_MOCK != 1)
    app.bot_data.update(
        altegio=client,
        links=LinkStore(),
        locations=booking_locations(),
        main_menu=MAIN_MENU_KEYBOARD,
        menu_dispatch=menu.dispatch,
        arcade_url=ARCADE_WEBAPP_URL,
        shop=SHOP,
        voice=make_assistant(),  # None without OPENAI_API_KEY or with VOICE_BOOKING=0
    )


async def post_init(app: Application) -> None:
    """Polling mode: chat menu button, shared services, reminder job."""
    # Default keeps today's "Записатись" -> Altegio booking page; CHAT_MENU_BUTTON=arcade opens the games.
    button = menu.chat_menu_button(CHAT_MENU_BUTTON, ARCADE_WEBAPP_URL, BOOKING_WIDGET_URL, CHAT_MENU_BOOKING_TEXT)
    if button is not None:
        await app.bot.set_chat_menu_button(menu_button=button)
    prepare(app)
    client = app.bot_data["altegio"]
    if client is not None:
        if app.job_queue is None:
            logger.error("No JobQueue: install python-telegram-bot[job-queue]; visit reminders are OFF")
        else:
            app.job_queue.run_repeating(reminders.reminder_job, interval=REMINDER_INTERVAL, first=30,
                                        name="altegio-reminders")


async def post_shutdown(app: Application) -> None:
    client = app.bot_data.get("altegio")
    if client is not None:
        await client.aclose()


async def handle_non_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Any other text: point at the menu."""
    if await reviews.take_feedback(update, context):  # the comment right after a low rating
        return
    await update.message.reply_text(texts.FALLBACK, reply_markup=MAIN_MENU_KEYBOARD)


CHATS = "bb:chats"  # every private chat that wrote to the bot: id -> {name, username, last}


async def record_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remember who uses the bot (Telegram gives bots no subscriber list), for future announcements."""
    chat, user = update.effective_chat, update.effective_user
    if chat is None or chat.type != "private" or user is None:
        return
    entry = {"name": user.full_name, "username": user.username or "", "last": int(time.time())}
    try:
        await asyncio.to_thread(_kv.pipeline, ["HSET", CHATS, str(chat.id), json.dumps(entry, ensure_ascii=False)])
    except Exception as e:  # noqa: BLE001 - never block the update because of bookkeeping
        logger.warning("record_chat failed: %s", e)


def add_handlers(app: Application, persistent: bool = False) -> Application:
    app.add_handler(TypeHandler(Update, record_chat), group=-1)  # runs before, never stops, the handlers below
    for h in team.handlers():  # /start team_<token> must win over the plain /start below; /me
        app.add_handler(h)
    # Booking first: its contact/text steps must win over the catch-all text handler below.
    app.add_handler(booking.build_handler(persistent=persistent))
    for h in reminders.handlers():
        app.add_handler(h)
    for h in menu.handlers():  # menu buttons, prices, contact sharing outside booking
        app.add_handler(h)
    for h in admin.handlers():  # /admin panel, only for ADMIN_CHAT_IDS
        app.add_handler(h)
    for h in reviews.handlers() + waitlist.handlers():  # post-visit rating, "tell me if a time frees up"
        app.add_handler(h)
    app.add_handler(CallbackQueryHandler(booking.stale_button, pattern=r"^bk:"))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_non_photo)
    )
    return app


def build_webhook_application(persistence) -> Application:
    """One Application per webhook call (Vercel): state comes from `persistence`, no JobQueue
    (reminders run from the Vercel cron, api/cron_reminders.py)."""
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).persistence(persistence).job_queue(None).build()
    return add_handlers(app, persistent=True)


def main() -> None:
    """Local / server polling mode. Production runs as a webhook: api/webhook.py."""
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set in .env file")

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    add_handlers(app)
    logger.info("Barber Style Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
