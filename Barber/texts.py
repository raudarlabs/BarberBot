"""Ukrainian copy for booking and reminders, plus date formatting."""
import re
from datetime import date, datetime

BOOK_BUTTON = "📅 Записатися"

WEEKDAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
WEEKDAYS = ["понеділок", "вівторок", "середа", "четвер", "пʼятниця", "субота", "неділя"]
MONTHS_GEN = ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня",
              "серпня", "вересня", "жовтня", "листопада", "грудня"]


def day_button(d: date) -> str:
    return "%s %02d.%02d" % (WEEKDAYS_SHORT[d.weekday()], d.day, d.month)


def when(dt: datetime) -> str:
    """'вівторок, 17 листопада о 12:00'"""
    return "%s, %d %s о %s" % (WEEKDAYS[dt.weekday()], dt.day, MONTHS_GEN[dt.month - 1], dt.strftime("%H:%M"))


def relative_day(dt: datetime, now: datetime) -> str:
    delta = (dt.date() - now.date()).days
    if delta == 0:
        return "сьогодні о %s" % dt.strftime("%H:%M")
    if delta == 1:
        return "завтра о %s" % dt.strftime("%H:%M")
    return when(dt)


UNAVAILABLE = "Онлайн-запис зараз недоступний. Спробуйте пізніше або зателефонуйте нам."
MOCK_NOTE = "\n\n🧪 Тестовий режим: запис не потрапить у журнал."
API_ERROR = "Не вдалося звʼязатися з системою запису. Спробуйте ще раз за хвилину."
SESSION_EXPIRED = "Ця кнопка вже неактуальна. Почніть запис заново: /book"

PICK_LOCATION = "Оберіть барбершоп:"
PICK_STAFF = "Оберіть майстра:"
NO_STAFF = "У цьому барбершопі зараз немає майстрів для онлайн-запису."
PICK_SERVICE = "Послуги майстра {staff}:"
NO_SERVICES = "У майстра {staff} немає послуг для онлайн-запису."
PICK_DAY = "Оберіть день:"
NO_DAYS = "У найближчі дні вільних днів немає. Оберіть іншого майстра."
PICK_TIME = "Вільний час, {day}:"
NO_TIME = "На цей день вільного часу немає. Оберіть інший день."
SLOT_TAKEN = "Цей час щойно зайняли. Оберіть інший:"

ASK_PHONE = (
    "Щоб підтвердити запис і надсилати нагадування, поділіться номером телефону — "
    "натисніть кнопку нижче 👇"
)
SHARE_PHONE_BUTTON = "📱 Поділитися номером"
NOT_YOUR_CONTACT = "Будь ласка, надішліть саме свій номер кнопкою «Поділитися номером»."
PHONE_SAVED = "Дякуємо, номер збережено."

SUMMARY = (
    "Перевірте запис:\n\n"
    "📍 {location}\n"
    "💈 Майстер: {staff}\n"
    "✂️ {service}{price}\n"
    "🕐 {when}\n"
    "📱 +{phone}"
)
RESCHEDULE_SUMMARY = "Перенести візит до майстра {staff} на {when}?"
CONFIRM_BUTTON = "✅ Підтвердити"
OTHER_PHONE_BUTTON = "📱 Інший номер"
BACK_BUTTON = "← Назад"
CANCEL_BUTTON = "✖️ Скасувати"
CANCELLED = "Добре, запис не створено. Повертайтеся, коли зручно 🙂"
RESCHEDULE_KEPT = "Добре, нічого не змінюємо: візит залишається на {when}."
CANCEL_VISIT_INFO = (
    "Скасувати візит у боті поки не можна — зателефонуйте нам: {phone}. "
    "Перенести візит можна в «👤 Мій запис»."
)
PHONE_BY_BUTTON = "Номер потрібно надіслати кнопкою «📱 Поділитися номером» нижче 👇"

BOOKED = (
    "Готово! Ви записані ✅\n\n"
    "💈 {staff}\n✂️ {service}\n🕐 {when}\n📍 {location}\n\n"
    "Нагадаємо за добу і за 2 години до візиту."
)
RESCHEDULED = "Готово! Візит перенесено на {when} ✅"

REMINDER_24H = "Нагадуємо: {rel} у вас візит до майстра {staff}{service}.\n📍 {location}"
REMINDER_2H = "Через 2 години ({time}) чекаємо вас у майстра {staff}.\n📍 {location}"
REMINDER_CONFIRM = "✅ Буду"
REMINDER_MOVE = "🔁 Перенести"
CONFIRMED = "Дякуємо, візит підтверджено ✅"
CANT_CHANGE = "Цей запис не вдалося змінити. Будь ласка, зателефонуйте нам."
NOT_YOURS = "Цей запис привʼязаний до іншого чату."

UNLINKED = "Номер відвʼязано, нагадування в Telegram більше не надходитимуть."
NOT_LINKED = "До цього чату не привʼязано номер."

VOICE_HINT = "🎙 Можна й голосом, напр.: «до Ігоря на завтра о 15:00, чоловіча стрижка»."
VOICE_HEARD = "🎙 Я почув: «{text}»"
VOICE_OFF = "Голосові поки не підтримуються — оберіть, будь ласка, кнопками: /book"
VOICE_TOO_LONG = "Голосове задовге. Скажіть коротко, напр.: «до Ігоря на завтра о 15:00»."
VOICE_NOT_UNDERSTOOD = "Не вдалося розібрати голосове. Спробуйте ще раз або оберіть кнопками."
VOICE_TIME_BUSY = "{time} вже зайнято."
VOICE_WINDOW_EMPTY = "У цей проміжок вільного часу немає, ось увесь вільний час:"


# ------------------------------------------------------------------ main menu
MENU_VISITS = "👤 Мій запис"
MENU_STYLE = "✂️ Підібрати стрижку"
MENU_GAMES = "🎮 Ігри"
MENU_PRICES = "💈 Послуги й ціни"
MENU_CONTACTS = "📍 Контакти"
LEGACY_STYLE = "Choose Hair Style"  # old keyboards still on clients' phones
LEGACY_GAMES = "Play"
LEGACY_STYLE_UK = "Підібрати зачіску"  # the April 2026 live bot's keyboard
LEGACY_GAMES_UK = "Ігровуха"
MENU_PLACEHOLDER = "Оберіть дію або надішліть голосове 🎙"
# Menu buttons other than "📅 Записатися" (that one starts the booking itself).
MENU_REGEX = "^(%s)$" % "|".join(re.escape(x) for x in (
    MENU_VISITS, MENU_STYLE, MENU_GAMES, MENU_PRICES, MENU_CONTACTS, LEGACY_STYLE, LEGACY_GAMES,
    LEGACY_STYLE_UK, LEGACY_GAMES_UK))

WELCOME = (
    "Вітаємо в Custom True Barbershop! 💈\n\n"
    "📅 Записатися — майстер, послуга і час за хвилину (можна й голосом 🎙)\n"
    "👤 Мій запис — ваші візити, перенесення\n"
    "✂️ Підібрати стрижку — надішліть селфі, і ШІ покаже 6 стрижок на вашому фото\n"
    "🎮 Ігри — 5 міні-ігор і щомісячний рейтинг\n"
    "💈 Послуги й ціни · 📍 Контакти"
)
HELP = (
    "Що вміє бот:\n\n"
    "📅 Записатися (або /book, або просто голосове: «до Ігоря на завтра о 15:00»)\n"
    "👤 Мій запис — найближчі візити, кнопки «Буду» і «Перенести»\n"
    "✂️ Підібрати стрижку — надішліть чітке фронтальне фото при гарному світлі\n"
    "🎮 Ігри — грайте, поки чекаєте на свою чергу\n\n"
    "Нагадування про візит приходять за добу і за 2 години. /stop_reminders — вимкнути."
)
FALLBACK = "Оберіть дію в меню нижче 👇 або надішліть фото для підбору стрижки."

STYLE_PROMPT = (
    "Надішліть селфі — ШІ підбере 6 стрижок і покаже їх на вашому фото ✂️\n\n"
    "Щоб вийшло найкраще:\n• гарне світло на обличчі\n• фото анфас\n• волосся добре видно"
)

GAMES_TEXT = "🎮 5 ігор і щомісячний рейтинг з призами. Оберіть гру або відкрийте весь зал:"
GAMES_HUB_BUTTON = "🎮 Усі ігри"

PRICES_PICK_LOCATION = "Ціни залежать від майстра. Оберіть барбершоп:"
PRICES_PICK_STAFF = "Ціни залежать від майстра. Оберіть, чиї послуги показати:"
PRICES_OF = "💈 Послуги майстра {staff}:\n\n{lines}"
PRICES_LINE = "• {title}{price}"
PRICES_BOOK = "📅 Записатися до {staff}"
PRICES_BACK = "← Усі майстри"
PRICES_UNAVAILABLE = "Ціни зараз недоступні. Спробуйте пізніше або зателефонуйте нам: {phone}"

CONTACTS = "📍 {address}\n🕘 {hours}\n📞 {phone}"
CONTACTS_MAP = "🗺 Прокласти маршрут"
CONTACTS_CALL = "📞 Подзвонити"
CONTACTS_INSTAGRAM = "📸 Instagram"

VISITS_ASK_PHONE = "Щоб знайти ваші записи, поділіться номером телефону кнопкою нижче 👇"
VISITS_NONE = "Найближчих записів не знайшли. Записатися?"
VISITS_HEADER = "Ваші найближчі візити:"
VISIT = "💈 {staff}{service}\n🕐 {when}\n📍 {location}"
VISITS_BOOK = "📅 Записатися"
