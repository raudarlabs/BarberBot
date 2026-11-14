# Barber Style Bot

A Telegram bot for barbershops with two features:
- **AI Hairstyle Recommendations** — customers send a photo and get 6 personalized hairstyle options generated on their actual photo
- **Barber Games** — a Telegram Mini App with 5 games customers can play while waiting, with monthly leaderboards and prizes

---

## Where things run

- **Vercel project `barberbot`** (https://barber-psi-ochre.vercel.app) serves everything:
  the games (`public/`), the arcade API (`api/*.py`) and the **live Telegram bot**
  `@custom_true_bot` — its webhook is `/api/webhook` (`api/webhook.py` + `api/_legacy_*.py`).
  Deploying replaces all three at once, so never deploy a tree without `api/webhook.py`.
- `bot.py` (Altegio booking, voice booking, reminders, new menu) is the next version of the bot;
  it runs in polling mode and is **not deployed yet** — it will move into the webhook.
- Data: Neon Postgres `barber-games-db` (`DATABASE_URL`).

## Architecture

```
Barber/
  bot.py               # Telegram bot (polling, runs on any server)
  config.py            # Environment config and message templates
  openai_service.py    # Grok Vision analysis + OpenAI image generation
  api/                 # Vercel Python functions (stdlib only)
    session.py         # POST /api/session      start a server-timed run
    score.py           # POST /api/score        submit a run (anti-cheat checks)
    leaderboard.py     # GET  /api/leaderboard  monthly / all-time board, ?month= history (top 10)
    summary.py         # GET  /api/summary      hub data: month leaders, my bests, my prize codes
    prize.py           # GET/POST /api/prize    staff: check / hand out a prize code (ARCADE_ADMIN_KEY)
    idea.py            # POST/GET /api/idea     game ideas from players (form hidden: IDEAS_ENABLED)
    _tg.py             # Telegram initData signature check (+ failure reason for logs)
    _games.py          # per-game score limits, Kyiv-month keys, prize codes
    _kv.py             # store: Neon Postgres (DATABASE_URL) > Upstash > in-memory (dev)
    _http.py           # request/response plumbing
  public/
    index.html         # Barber Games hub (Telegram Mini App)
    prize.html         # staff page: verify and hand out monthly prizes
    ideas.html         # staff page: read game ideas
    arcade/            # engine, UI, i18n (uk/en) and games/*.js
  dev_server.py        # local server: public/ + /api with in-memory store
  tests/test_api.py    # API tests
  tests/test_store_pg.py  # Postgres store vs in-memory reference (needs ARCADE_TEST_DATABASE_URL)
  tests/sim.mjs        # headless bot play + fuzz: checks anti-cheat limits and screen sizes
```

## Barber Games (Mini App)

Five games in one hub, styled after the shop's new look (warm black wall, wine-red paint, dusty pink):

| Game | Mechanic | Scoring |
|------|----------|---------|
| Фен-пілот | one-tap flight between barber poles | +1 per pole, +2 golden comb |
| Ідеальний фейд | stop the clipper inside the gold band | perfect 10 (+streak), clean 6, okay 3; 3 lives |
| Борода-ніндзя | swipe to slice beard tufts, avoid lotion | +1 tuft, +5 golden, combo bonus; 3 lives |
| Змійка | classic snake, full-screen, swipe to turn | +10 food, +30 golden comb |
| Укладка | falling-blocks puzzle | 10/30/50/80 per 1–4 rows × level |

Shared engine: crisp canvas at device pixel ratio, fixed-step loop, auto-pause when Telegram
is minimised, synth sound with mute, haptics, Telegram back button, safe areas.

### Leaderboard and anti-cheat
- The player is identified by Telegram `initData`, verified on the server with the bot token.
  No typed-in names.
- Each run gets a one-time session token; the server measures run time and rejects scores above
  what the game allows per second (`api/_games.py`, keep in sync with `public/arcade/games/*.js`).
- Each player's **best** score is kept per game on a monthly board (Kyiv time, resets on the 1st)
  and an all-time board. Month boards are never deleted: History shows each month's top 10.
  Session starts are rate-limited per player. Limits were tuned with `tests/sim.mjs`.
- The #1 of every game each month sees a prize code in the hub; staff check and hand it out on
  `/prize.html` (also shows how long the record run took, to spot fakes).
- Mini Apps opened from a reply-keyboard button get no `initData` (401 `no_init_data` in the
  logs), so open the arcade from an inline button, the chat menu button or a `startapp` link.

## Online booking & reminders (Altegio)

```
Barber/
  booking.py           # in-chat booking conversation (buttons + voice)
  reminders.py         # 24 h / 2 h visit reminders (JobQueue), confirm button
  altegio.py           # Altegio REST client + free-slot maths
  altegio_fixture.py   # fake Altegio server over fixtures/altegio.json (ALTEGIO_MOCK=1, tests)
  linkstore.py         # phone <-> Telegram chat links (Upstash Redis via api/_kv.py)
  voice.py             # voice note -> text -> booking intent (OpenAI)
  menu.py              # bottom menu and its screens (games, prices, contacts, my visits)
  texts.py             # Ukrainian copy for the menu, booking and reminders
  tests/test_altegio.py, test_booking.py, test_reminders.py, test_voice.py, test_menu.py, fakes.py
```

### Main menu (bottom keyboard, Ukrainian)

| Button | What it does |
|--------|--------------|
| 📅 Записатися | booking flow below |
| 👤 Мій запис | next 30 days of visits (found by the shared phone) with ✅ Буду / 🔁 Перенести |
| ✂️ Підібрати стрижку | asks for a selfie → the existing AI hairstyle flow |
| 🎮 Ігри | inline buttons: the whole arcade + one per game (`ARCADE_WEBAPP_URL?game=dryer\|fade\|beard\|snake\|blocks`) |
| 💈 Послуги й ціни | barber → their services and prices from Altegio → "Записатися до …" |
| 📍 Контакти | address, hours, phone, map and Instagram from `config.SHOP` (mirrors the website's `src/data/site.ts`) |

The arcade is only ever opened from inline buttons or the chat menu button (a reply-keyboard
web_app launch gets no initData, which the leaderboard needs). Old keyboards' "Play" and
"Choose Hair Style" buttons still work. Pressing a menu button in the middle of a booking ends
the booking and opens that screen.

| Command | Description |
|---------|-------------|
| `/book`, "📅 Записатися" button, or a voice note | Book a visit in Altegio |
| `/stop_reminders` | Forget my phone, stop visit reminders |

Needs the JobQueue extra: `python-telegram-bot[job-queue]==21.6`.

```bash
ALTEGIO_MOCK=1 python bot.py  # booking + reminders against the fixture (needs TELEGRAM_BOT_TOKEN)
```

### Booking flow
`📅 Записатися`, `/book` or a voice note:

1. **Location**: skipped when only one is configured.
2. **Barber**: the ones Altegio shows for online booking, in Altegio's order.
3. **Service**: that barber's own services and prices (each barber has a personal category).
4. **Day**: the barber's working days, next 14 days.
5. **Time**: start times where the whole service fits, every 15 min, at least 30 min from now.
6. **Phone**: the client taps "📱 Поділитися номером" (only their own contact is accepted).
   The number is remembered, so next time this step is skipped.
7. **Confirm** → appointment created in Altegio (comment "Запис через Telegram-бот").
   If someone took the slot meanwhile, the bot shows the free times again.

**Voice.** A voice note at any step, e.g. *«до Ігоря на завтра о третій, стрижка бороди»*, is
transcribed (`gpt-4o-mini-transcribe`), the barber, service, day and time are extracted
(`gpt-4o-mini`, JSON mode, barber ids only from Altegio's list), and the bot jumps to the first
step still missing ("15:00 зайнято" → free times; "ввечері" → only evening times). A follow-up
voice note keeps what was already chosen. The client still confirms with a button and shares
the phone with the contact button, so a misheard phrase never books anything. Voice is on when
`OPENAI_API_KEY` is set; `VOICE_BOOKING=0` turns it off. Voice notes go to OpenAI, like the
photos already do for hairstyle suggestions.

### Reminders
Altegio has no webhooks, so a JobQueue job polls every `REMINDER_INTERVAL` seconds (default
600). For each location it lists the next 24 h of appointments and sends, once per visit time:

- **24 h before** (window 24 h … 3 h): "Нагадуємо: завтра о 15:00 …" with **✅ Буду** / **🔁 Перенести**
- **2 h before** (window 2 h … 0)

✅ Буду sets the Altegio attendance to *confirmed* (2). 🔁 Перенести opens the day/time picker for
the same barber and service and moves the existing appointment (PUT, full body). Buttons only
work in the chat the appointment is linked to. A moved visit gets reminded again for its new time.

The client's chat is found from the appointment (if booked or reminded in the bot), else from the
client's phone in Altegio matched against numbers shared via the contact button. Clients who
never shared their number in the bot get no Telegram reminder; Altegio's own SMS reminders are
untouched. `/stop_reminders` forgets the chat's number and stops all reminders to it.

Links live in the arcade's store under `bb:*` keys (see `linkstore.py`, via `api/_kv.py`):
Neon Postgres when `DATABASE_URL` is set (table `arcade_kv`), else Upstash. The bot runs outside
Vercel (Vercel only deploys `api/` and `public/`), so put the same Neon `DATABASE_URL` into the
bot's own `.env`. Without it the bot keeps links in memory, and they are lost on restart.

### Configuration

```env
ALTEGIO_PARTNER_TOKEN=...          # developer (partner) token
ALTEGIO_USER_TOKEN=...             # business user token
ALTEGIO_LOCATION_IDS=297851        # comma-separated, shown in this order
# ALTEGIO_LOCATION_NAMES=297851=вул. Володимира Великого, 75;708761=вул. Гагаріна, 29/2
# ALTEGIO_TZ=Europe/Kyiv
# REMINDER_INTERVAL=600
# ALTEGIO_MOCK=1                   # no tokens: run against fixtures/altegio.json
# VOICE_BOOKING=0                  # turn voice booking off
DATABASE_URL=postgres://...        # same Neon DB as the arcade, so phone links survive restarts
```

Without both Altegio tokens booking and reminders are **off** (the button answers "недоступний"),
unless `ALTEGIO_MOCK=1`, which books into an in-memory fake and marks every message
"🧪 Тестовий режим". Never set `ALTEGIO_MOCK` on the production bot.

Location ids: 297851 = вул. Володимира Великого 75, 708761 = вул. Гагаріна 29/2,
1367275 = вул. Смілянська 2. As of Nov 2026 only 297851 has a full team in Altegio
(708761 has one barber, 1367275 a placeholder), so the default is 297851 only.

### Getting the Altegio tokens

Every API call sends `Authorization: Bearer <partner token>, User <user token>`.

1. **Partner token**: sign up for an Altegio developer account at
   [developer.alteg.io](https://developer.alteg.io) and copy the partner token from the account.
2. **User token**: the bot acts as a business user of the shop. Either install your app on the
   location in the Altegio Marketplace and use its system user, or sign in as a dedicated staff
   user (not the owner) to get its user token — Altegio's auth endpoint takes the user's login
   and password with the partner token and returns `user_token`; check the current "Authorization"
   page of the docs for the exact call.
3. Give that user the least access that works: view and create appointments (`record_form_access`,
   `edit_records_access`), read clients (for reminder phone lookup), on each location the bot uses.
4. Put both tokens in the bot's `.env`, restart, then smoke-test reads first: open `/book` and
   walk to the time picker without confirming.

The REST paths are taken from Altegio's API catalog and kept in one class
(`HttpAltegioClient` in `altegio.py`). The fixture mirrors the shapes we expect; the first live
run should check the barber list, services (price and duration), time picker and one test
booking made with the shop's OK. Service duration comes from Altegio; if a service has none,
the bot assumes 60 min, and Altegio rejects any overlap (`save_if_busy: false`).

## Setup

```bash
pip install -r requirements.txt
```

`.env` for the bot:

```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
XAI_API_KEY=your_xai_api_key
OPENAI_API_KEY=your_openai_api_key
ARCADE_WEBAPP_URL=https://your-project.vercel.app/
```

| Key | Where to get it |
|-----|----------------|
| `TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) on Telegram |
| `XAI_API_KEY` | [console.x.ai](https://console.x.ai) |
| `OPENAI_API_KEY` | [platform.openai.com](https://platform.openai.com) |

## Deploy the arcade to Vercel

1. Import the repo in Vercel with **Root Directory = `Barber`**. `vercel.json` serves `public/`
   as the site; `api/*.py` become Python functions.
2. Storage → connect a **Neon** Postgres database (sets `DATABASE_URL`; tables are created on
   first use). Upstash Redis (`UPSTASH_REDIS_REST_URL`/`TOKEN`) also works.
3. Settings → Environment Variables → add **`TELEGRAM_BOT_TOKEN`** (same bot that opens the arcade;
   needed to verify players) and **`ARCADE_ADMIN_KEY`** (staff pages). Never set `ARCADE_DEV` in Vercel.
4. Set `ARCADE_WEBAPP_URL` in the bot's `.env` to the deployment URL and restart the bot.

## Local development

```bash
python dev_server.py          # http://localhost:8787 — in-memory store, plays as "Dev Player"
python -m unittest discover -s tests
```

## Run the Bot

```bash
python bot.py
```

---

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Show welcome message and main menu |
| `/help` | Show usage instructions |
| Send a photo | Trigger AI hairstyle analysis |
| "Choose Hair Style" button | Prompt for photo |
| "Play" button / menu button | Open Barber Games |

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Telegram bot | python-telegram-bot 21.6, polling mode |
| Vision analysis | Grok Vision (`grok-4-1-fast-non-reasoning`) via xAI API |
| Image generation | OpenAI `gpt-image-1` (photo editing) |
| Games | HTML5 Canvas, vanilla JS modules, Telegram WebApp API |
| Leaderboard API | Vercel serverless Python functions (stdlib) |
| Score storage | Neon Postgres via HTTP (stdlib), Upstash Redis as fallback |
| Hosting | Vercel (free tier) |
