"""
The live Telegram bot (restored from the April 2026 deployment) — handlers for api/webhook.py.
"""

import sys
import os
import io
import logging

# Allow imports from project root (config, openai_service)
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ChatAction

from _legacy_config import (
    TELEGRAM_BOT_TOKEN,
    WELCOME_MESSAGE,
    PROCESSING_MESSAGE,
    ERROR_MESSAGE,
    NO_FACE_MESSAGE,
    GAMES_WEBAPP_URL,
)
from _legacy_openai import analyze_photo_and_get_hairstyles, generate_all_hairstyle_images

logger = logging.getLogger(__name__)

MAIN_MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Підібрати зачіску")],
        [KeyboardButton("Ігровуха")],
    ],
    resize_keyboard=True,
)

# Games open from an INLINE web_app button: unlike a reply-keyboard web_app button, that launch
# passes signed initData, which the leaderboard API needs to know who is playing.
GAMES_BUTTON = InlineKeyboardMarkup(
    [[InlineKeyboardButton("🎮 Грати", web_app=WebAppInfo(url=GAMES_WEBAPP_URL))]]
)


async def handle_games(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "5 ігор і рейтинг місяця з призами. Тисни, щоб грати:",
        reply_markup=GAMES_BUTTON,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME_MESSAGE, reply_markup=MAIN_MENU_KEYBOARD)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Як користуватись ботом:\n\n"
        "1. Зроби або вибери чітке фото свого обличчя\n"
        "2. Надішли його сюди в чат\n"
        "3. Зачекай поки AI-барбер аналізує твоє фото (~30-60 секунд)\n"
        "4. Отримай 6 персональних варіантів зачісок!\n\n"
        "Або натисни 'Грати' щоб пограти поки чекаєш!\n\n"
        "Для найкращого результату — фото анфас з гарним освітленням.",
        reply_markup=MAIN_MENU_KEYBOARD,
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    logger.info(f"Received photo from user {user.id} (@{user.username})")

    processing_msg = await update.message.reply_text(PROCESSING_MESSAGE)

    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.UPLOAD_PHOTO,
        )

        # Handle both compressed photos and photos sent as documents
        if update.message.photo:
            photo_file = await update.message.photo[-1].get_file()
        elif update.message.document:
            photo_file = await update.message.document.get_file()
        else:
            await processing_msg.edit_text(NO_FACE_MESSAGE)
            return
        photo_bytes = await photo_file.download_as_bytearray()

        logger.info(f"Downloaded photo ({len(photo_bytes)} bytes) from user {user.id}")

        await processing_msg.edit_text(
            "Крок 1/3: Вивчаю твоє волосся як справжній детектив..."
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

        await processing_msg.edit_text(
            "Крок 2/3: Малюю 6 зачісок прямо на твоєму фото...\n"
            "Це займе 30-60 секунд. Можеш поки пограти!"
        )

        generated = await generate_all_hairstyle_images(analysis, bytes(photo_bytes))

        if not generated:
            await processing_msg.edit_text(ERROR_MESSAGE)
            return

        await processing_msg.edit_text(
            f"Крок 3/3: Надсилаю {len(generated)} варіантів зачісок... Тримай!"
        )

        summary_lines = [
            f"Тадам! Ось твої {len(generated)} персональних варіантів зачісок!\n",
            f"Тип волосся: {hair_type}\n",
        ]
        for i, style in enumerate(generated, 1):
            summary_lines.append(f"{i}. {style['name']}")

        await update.message.reply_text("\n".join(summary_lines))

        for i, style in enumerate(generated, 1):
            caption = (
                f"Варіант {i}: {style['name']}\n\n"
                f"{style['reason']}"
            )
            if len(caption) > 1024:
                caption = caption[:1021] + "..."

            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=io.BytesIO(style["image_bytes"]),
                caption=caption,
            )

        await processing_msg.delete()
        await update.message.reply_text(
            "Готово! Покажи ці варіанти своєму барберу і обирай улюблений стиль!\n"
            "P.S. Якщо барбер скаже 'вау' — значить AI знає свою справу"
        )

        logger.info(f"Successfully sent {len(generated)} hairstyles to user {user.id}")

    except Exception as e:
        logger.error(f"Error processing photo for user {user.id}: {e}", exc_info=True)
        await processing_msg.edit_text(ERROR_MESSAGE)


async def handle_choose_hair_style(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Надішли мені фото свого обличчя — і я підберу тобі зачіску мрії!\n"
        "Тільки не надсилай фото паспорта, я ж барбер, а не прикордонник",
        reply_markup=MAIN_MENU_KEYBOARD,
    )


async def handle_non_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Я розумію тільки фото! Надішли фото свого обличчя — і я підберу зачіску.\n"
        "Або натисни 'Грати' щоб пограти!\n"
        "Напиши /start щоб побачити інструкції.",
        reply_markup=MAIN_MENU_KEYBOARD,
    )


def build_application() -> Application:
    """Build and configure the bot Application with all handlers."""
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(filters.Regex(r"^(Play|Ігровуха|🎮 Ігровуха)$"), handle_games))
    app.add_handler(MessageHandler(
        filters.Regex(r"^(Choose Hair Style|Підібрати зачіску)$"),
        handle_choose_hair_style,
    ))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_non_photo)
    )

    return app
