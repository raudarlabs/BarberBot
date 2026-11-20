// Telegram WebApp wrapper, storage and the arcade API client.

export const tg = (window.Telegram && window.Telegram.WebApp) || null;
const supports = (v) => !!(tg && tg.isVersionAtLeast && tg.isVersionAtLeast(v));

export const store = {
  get(key, fallback = null) {
    try {
      const v = localStorage.getItem('ba:' + key);
      return v === null ? fallback : v;
    } catch (e) {
      return fallback;
    }
  },
  set(key, value) {
    try { localStorage.setItem('ba:' + key, String(value)); } catch (e) { /* private mode */ }
  },
};

export function haptic(kind) {
  try {
    const h = tg && tg.HapticFeedback;
    if (!h || !supports('6.1')) return;
    if (kind === 'success' || kind === 'error' || kind === 'warning') h.notificationOccurred(kind);
    else if (kind === 'select') h.selectionChanged();
    else h.impactOccurred(kind || 'light');
  } catch (e) { /* ignore */ }
}

export function initTelegram(onBack) {
  if (!tg) return;
  try {
    tg.ready();
    tg.expand();
    if (supports('7.7')) tg.disableVerticalSwipes();
    if (supports('6.1')) { tg.setHeaderColor('#120d0f'); tg.setBackgroundColor('#120d0f'); }
    if (supports('7.10')) tg.setBottomBarColor('#120d0f');
    if (supports('8.0') && (tg.platform === 'ios' || tg.platform === 'android')) tg.requestFullscreen();
    if (supports('6.1')) tg.BackButton.onClick(onBack);
  } catch (e) { /* older clients */ }
}

export function setBackButton(visible) {
  if (!tg || !supports('6.1')) return;
  visible ? tg.BackButton.show() : tg.BackButton.hide();
}

export function onAppHidden(cb) {
  document.addEventListener('visibilitychange', () => { if (document.hidden) cb(); });
  if (tg && supports('8.0')) tg.onEvent('deactivated', cb);
}

export function openLink(url) {
  if (tg && tg.openLink) tg.openLink(url);
  else window.open(url, '_blank', 'noopener');
}

export function openTelegramLink(url) {
  if (tg && tg.openTelegramLink && supports('6.1')) tg.openTelegramLink(url);
  else window.open(url, '_blank', 'noopener');
}

async function call(path, { method = 'GET', body } = {}) {
  const res = await fetch(path, {
    method,
    headers: {
      'Content-Type': 'application/json',
      'X-Telegram-Init-Data': (tg && tg.initData) || '',
      // diagnostics only: which Telegram client and launch data shape (no user data)
      'X-Tg-Client': tg ? `${tg.platform || '?'}/${tg.version || '?'}/${tg.initData ? 'init' : 'noinit'}/${tg.initDataUnsafe && tg.initDataUnsafe.user ? 'user' : 'nouser'}` : 'browser',
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (e) { /* empty body */ }
  if (!res.ok) {
    const err = new Error((data && data.error) || 'http_' + res.status);
    err.status = res.status;
    throw err;
  }
  return data;
}

export const api = {
  startSession: (game) => call('/api/session', { method: 'POST', body: { game } }),
  submitScore: (session, score) => call('/api/score', { method: 'POST', body: { session, score } }),
  leaderboard: (game, period, limit = 30) =>
    call(`/api/leaderboard?game=${encodeURIComponent(game)}&period=${period}&limit=${limit}`),
  history: (game, month) =>
    call(`/api/leaderboard?game=${encodeURIComponent(game)}&month=${encodeURIComponent(month)}`),
  summary: () => call('/api/summary'),
  sendIdea: (title, text) => call('/api/idea', { method: 'POST', body: { title, text } }),
};
