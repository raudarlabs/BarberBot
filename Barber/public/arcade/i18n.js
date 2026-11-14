import { store, tg } from './platform.js';

const DICT = {
  uk: {
    tagline: 'Грай поки чекаєш',
    best: 'Рекорд',
    leader: 'Лідер місяця',
    leaderboard: 'Рейтинг',
    month: 'Цей місяць',
    allTime: 'Весь час',
    history: 'Історія',
    tapToStart: 'Грати',
    paused: 'Пауза',
    resume: 'Продовжити',
    toHub: 'До ігор',
    gameOver: 'Гру завершено',
    newBest: 'НОВИЙ РЕКОРД',
    playAgain: 'Ще раз',
    share: 'Поділитися',
    book: 'Записатися на стрижку',
    bookShort: 'Записатися на стрижку →',
    saving: 'Зберігаємо результат…',
    rankMonth: '#{rank} у рейтингу місяця',
    guest: 'Відкрий гру через бота в Telegram, щоб потрапити в рейтинг',
    offline: 'Не вдалося зберегти результат',
    rejected: 'Результат не зараховано',
    loading: 'Завантаження…',
    empty: 'Поки порожньо — стань першим',
    resetsMonthly: 'Рейтинг обнуляється 1-го числа. У рейтинг іде твій найкращий результат, топ-10 кожного місяця зберігаються в історії.',
    prizeTitle: 'Приз місяця',
    prizeBody: 'Перше місце в кожній грі — {prize}',
    endsDays: 'До кінця місяця: {n} дн.',
    endsHours: 'До кінця місяця: {n} год.',
    winTitle: 'Ти переміг!',
    winText: '«{game}», {month}. Покажи цей код барберу:',
    redeemed: 'Приз отримано ✓',
    lastWinner: 'Переможець {month}',
    noHistory: 'Історія з’явиться після першого місяця',
    winnerOf: 'Переможець місяця',
    you: 'ти',
    yourRank: 'Твоє місце: #{rank}',
    shareText: 'Мій рекорд у грі «{game}» — {score}! Спробуй побити 💈',
    soundOn: '🔊 Звук',
    soundOff: '🔈 Без звуку',
    lang: 'EN',
    newGame: 'НОВА',
    perfect: 'Ідеально!',
    great: 'Чисто!',
    ok: 'Норм',
    miss: 'Мимо',
    combo: 'Комбо ×{n}',
    lines: 'Ряди: {n}',
    level: 'Рівень {n}',
  },
  en: {
    tagline: 'Play while you wait',
    best: 'Best',
    leader: 'Leader this month',
    leaderboard: 'Leaderboard',
    month: 'This month',
    allTime: 'All time',
    history: 'History',
    tapToStart: 'Play',
    paused: 'Paused',
    resume: 'Resume',
    toHub: 'All games',
    gameOver: 'Game over',
    newBest: 'NEW BEST',
    playAgain: 'Play again',
    share: 'Share',
    book: 'Book a haircut',
    bookShort: 'Book a haircut →',
    saving: 'Saving your score…',
    rankMonth: '#{rank} on this month’s board',
    guest: 'Open the game from the Telegram bot to join the leaderboard',
    offline: 'Couldn’t save your score',
    rejected: 'Score not counted',
    loading: 'Loading…',
    empty: 'Nothing here yet — be the first',
    resetsMonthly: 'The board resets on the 1st of every month. Your best run counts; each month’s top 10 is kept in History.',
    prizeTitle: 'Prize of the month',
    prizeBody: 'First place in every game wins {prize}',
    endsDays: '{n} days left this month',
    endsHours: '{n} hours left this month',
    winTitle: 'You won!',
    winText: '“{game}”, {month}. Show this code at the shop:',
    redeemed: 'Prize collected ✓',
    lastWinner: '{month} winner',
    noHistory: 'History appears after the first month',
    winnerOf: 'Winner of the month',
    you: 'you',
    yourRank: 'Your place: #{rank}',
    shareText: 'My best in “{game}” is {score}! Can you beat it? 💈',
    soundOn: '🔊 Sound',
    soundOff: '🔈 Muted',
    lang: 'УКР',
    newGame: 'NEW',
    perfect: 'Perfect!',
    great: 'Clean!',
    ok: 'Okay',
    miss: 'Miss',
    combo: 'Combo ×{n}',
    lines: 'Lines: {n}',
    level: 'Level {n}',
  },
};

function detect() {
  const saved = store.get('lang');
  if (saved === 'uk' || saved === 'en') return saved;
  // Ukrainian by default (the shop's audience); English only for Telegram users with an English UI.
  const code = (tg && tg.initDataUnsafe && tg.initDataUnsafe.user && tg.initDataUnsafe.user.language_code) || 'uk';
  return code.toLowerCase().startsWith('en') ? 'en' : 'uk';
}

let lang = detect();

export const getLang = () => lang;
export function setLang(l) { lang = l; store.set('lang', l); }

export function t(key, vars) {
  let s = (DICT[lang] && DICT[lang][key]) || DICT.uk[key] || key;
  if (vars) for (const k in vars) s = s.replace('{' + k + '}', vars[k]);
  return s;
}

/** "2026-11" → "листопад 2026" / "November 2026" */
export function monthLabel(m) {
  const [y, mm] = m.split('-').map(Number);
  try {
    const s = new Intl.DateTimeFormat(lang === 'en' ? 'en-GB' : 'uk-UA', { month: 'long', year: 'numeric', timeZone: 'UTC' })
      .format(new Date(Date.UTC(y, mm - 1, 15)));
    return s.replace(' р.', '');
  } catch (e) {
    return m;
  }
}

/** Pick the current language from a {uk, en} object. */
export const L = (obj) => (obj ? obj[lang] || obj.uk : '');
