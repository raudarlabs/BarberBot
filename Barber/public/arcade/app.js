import { BOOKING_URL, BOT_URL, IDEAS_ENABLED, PRIZE } from './config.js';
import { Floaters, Loop, Stage, bindPointer, sfx } from './engine.js';
import { L, getLang, monthLabel, setLang, t } from './i18n.js';
import {
  api, haptic, initTelegram, onAppHidden, openLink, openTelegramLink, setBackButton, store, tg,
} from './platform.js';

import dryer from './games/dryer.js';
import fade from './games/fade.js';
import beard from './games/beard.js';
import snake from './games/snake.js';
import blocks from './games/blocks.js';

const GAMES = [snake, blocks, dryer, fade, beard];
const byId = Object.fromEntries(GAMES.map((g) => [g.id, g]));

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

let summary = null;
let stage = null;
let loop = null;
const floaters = new Floaters();

const run = {
  mod: null, inst: null, state: 'idle', score: 0,
  session: null, sessionError: null, boardFrom: 'hub',
};

sfx.enabled = store.get('sound', 'on') === 'on';

// ------------------------------------------------------------------ screens

let current = 'hub';
function show(name) {
  for (const s of ['hub', 'play', 'board', 'idea']) $(s).classList.toggle('hidden', s !== name);
  current = name;
  setBackButton(name !== 'hub');
}

function goBack() {
  if (current === 'idea') {
    openHub();
  } else if (current === 'board') {
    if (run.boardFrom === 'play') show('play');
    else openHub();
  } else if (current === 'play') {
    leaveGame();
  }
}

function toast(msg) {
  const el = $('toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.add('hidden'), 2600);
}

function applyStaticText() {
  document.documentElement.lang = getLang();
  document.querySelectorAll('[data-i18n]').forEach((el) => { el.textContent = t(el.dataset.i18n); });
  $('langBtn').textContent = t('lang');
  $('soundBtn').textContent = sfx.enabled ? t('soundOn') : t('soundOff');
}

// ------------------------------------------------------------------ hub

function localBest(id) { return parseInt(store.get('best:' + id, '0'), 10) || 0; }

function tileStyle(mod) {
  const [a, b] = mod.tile;
  return `background:linear-gradient(145deg, ${a}, ${b});box-shadow:0 6px 20px ${a}55`;
}

function renderCards() {
  $('cards').innerHTML = GAMES.map((g) => {
    const s = summary && summary.games && summary.games[g.id];
    const serverBest = s && s.best && s.best.all;
    const best = Math.max(localBest(g.id), serverBest || 0);
    const meta = [];
    if (best) meta.push(`🏆 ${t('best')}: ${best}`);
    if (s && s.leader) meta.push(`👑 ${esc(s.leader.name)} · ${s.leader.score}`);
    return `
      <button class="card" type="button" data-game="${g.id}">
        <span class="tile" style="${tileStyle(g)}">${g.emoji}</span>
        <span class="card-body">
          <span class="card-title">${esc(L(g.title))}${g.isNew ? `<span class="new-pill">${t('newGame')}</span>` : ''}</span>
          <span class="card-desc">${esc(L(g.desc))}</span>
          <span class="card-meta">${meta.join('<span>·</span>')}</span>
        </span>
        <span class="chev">›</span>
      </button>`;
  }).join('');
}

function renderPrize() {
  const el = $('prizeBanner');
  let ends = '';
  if (summary && summary.ends_at) {
    const left = summary.ends_at * 1000 - Date.now();
    const days = Math.ceil(left / 86400000);
    ends = days > 1 ? t('endsDays', { n: days }) : t('endsHours', { n: Math.max(1, Math.ceil(left / 3600000)) });
  }
  el.innerHTML = `
    <span class="prize-icon">🏆</span>
    <span class="prize-body">
      <span class="prize-title">${t('prizeTitle')}</span>
      <span class="prize-text">${esc(t('prizeBody', { prize: L(PRIZE) }))}</span>
      ${ends ? `<span class="prize-ends">${ends}</span>` : ''}
    </span>
    <span class="chev">›</span>`;
}

function renderWins() {
  const wins = (summary && summary.wins) || [];
  $('wins').innerHTML = wins.map((w) => {
    const g = byId[w.game];
    return `
      <div class="win${w.redeemed ? ' done' : ''}">
        <div class="win-title">🎉 ${t('winTitle')}</div>
        <div class="win-text">${esc(t('winText', { game: g ? L(g.title) : w.game, month: monthLabel(w.month) }))}</div>
        <div class="win-code">${esc(w.code)}</div>
        ${w.redeemed ? `<div class="win-done">${t('redeemed')}</div>` : ''}
      </div>`;
  }).join('');
}

async function refreshSummary() {
  try {
    summary = await api.summary();
    if (current === 'hub') { renderCards(); renderPrize(); renderWins(); }
  } catch (e) { /* offline: local bests only */ }
}

function openHub() {
  leaveLoop();
  show('hub');
  renderCards();
  renderPrize();
  renderWins();
  refreshSummary();
}

// ------------------------------------------------------------------ play

function setScore(n, bump) {
  run.score = n;
  const el = $('hudScore');
  el.textContent = n;
  if (bump) {
    el.classList.add('bump');
    clearTimeout(setScore.timer);
    setScore.timer = setTimeout(() => el.classList.remove('bump'), 120);
  }
}

function setLives(n, max) {
  const el = $('hudLives');
  if (!max) { el.innerHTML = ''; return; }
  let html = '';
  for (let i = 0; i < max; i++) html += `<span class="${i < n ? '' : 'off'}">❤️</span>`;
  el.innerHTML = html;
}

const gameApi = {
  get w() { return stage.w; },
  get h() { return stage.h; },
  addScore(n, x, y, label, color) {
    if (run.state !== 'playing' || !n) return;
    setScore(run.score + n, true);
    if (x != null) floaters.add(x, y, label || '+' + n, color);
  },
  float(x, y, label, color, size) { floaters.add(x, y, label, color, size); },
  setLives,
  over() { if (run.state === 'playing') finishRun(); },
  haptic,
  sfx,
  t,
  L,
};

function renderControls(mod) {
  const c = $('controls');
  if (mod.controls === 'dpad') {
    c.innerHTML = `<div class="pad">
      <button data-k="up" class="up">▲</button><button data-k="left" class="left">◀</button>
      <button data-k="down" class="down">▼</button><button data-k="right" class="right">▶</button></div>`;
  } else if (mod.controls === 'blocks') {
    c.innerHTML = `<div class="keys">
      <button data-k="left">◀</button><button data-k="up">⟳</button><button data-k="right">▶</button>
      <button data-k="down">▼</button><button data-k="space">⤓</button></div>`;
  } else {
    c.innerHTML = '';
  }
  c.querySelectorAll('button[data-k]').forEach((b) => {
    const k = b.dataset.k;
    const press = (e) => {
      e.preventDefault();
      b.classList.add('on');
      if (run.state === 'playing' && run.inst.control) { run.inst.control(k, true); haptic('select'); }
    };
    const release = (e) => {
      b.classList.remove('on');
      if (run.inst && run.inst.control) run.inst.control(k, false);
    };
    b.addEventListener('pointerdown', press);
    b.addEventListener('pointerup', release);
    b.addEventListener('pointercancel', release);
    b.addEventListener('pointerleave', release);
  });
}

function ensureStage() {
  if (stage) { stage.fit(); return; }
  stage = new Stage($('canvas'));
  stage.onResize((w, h) => { if (run.inst && run.inst.resize) run.inst.resize(w, h); });
  loop = new Loop(update, render);
  bindPointer($('canvas'), {
    down: (p) => { if (run.state === 'playing' && run.inst.down) run.inst.down(p); },
    move: (p) => { if (run.state === 'playing' && run.inst.move) run.inst.move(p); },
    up: (p) => { if (run.state === 'playing' && run.inst.up) run.inst.up(p); },
  });
}

function update(dt) {
  if (run.state === 'paused') return;
  if (run.state === 'playing') run.inst.update(dt);
  else if (run.state === 'ready' && run.inst.idle) run.inst.idle(dt);
  floaters.update(dt);
}

function render() {
  const ctx = stage.ctx;
  stage.clear();
  run.inst.render(ctx);
  floaters.render(ctx);
}

function openGame(id) {
  const mod = byId[id];
  if (!mod) return;
  run.mod = mod;
  show('play');
  ensureStage();
  $('hudTitle').textContent = L(mod.title);
  renderControls(mod);
  newRun();
}

function newRun() {
  const mod = run.mod;
  floaters.clear();
  setScore(0);
  setLives(mod.lives || 0, mod.lives || 0);
  run.inst = mod.create(gameApi);
  if (run.inst.resize) run.inst.resize(stage.w, stage.h);
  run.state = 'ready';
  run.session = null; run.sessionError = null;

  $('startIcon').setAttribute('style', tileStyle(mod));
  $('startIcon').textContent = mod.emoji;
  $('startTitle').textContent = L(mod.title);
  $('startHow').textContent = L(mod.how);
  const best = localBest(mod.id);
  $('startBest').textContent = best ? `🏆 ${t('best')}: ${best}` : '';
  $('ovStart').classList.remove('hidden');
  $('ovPause').classList.add('hidden');
  $('ovOver').classList.add('hidden');
  $('hudPause').disabled = false;
  loop.start();
}

function startRun() {
  if (run.state !== 'ready') return;
  sfx.unlock();
  sfx.play('start');
  haptic('medium');
  $('ovStart').classList.add('hidden');
  run.state = 'playing';
  run.session = api.startSession(run.mod.id)
    .then((r) => r.session)
    .catch((e) => { run.sessionError = e; return null; });
  run.inst.start && run.inst.start();
}

function pause() {
  if (run.state !== 'playing') return;
  run.state = 'paused';
  run.inst.control && ['left', 'right', 'down', 'up', 'space'].forEach((k) => run.inst.control(k, false));
  $('ovPause').classList.remove('hidden');
}

function resume() {
  if (run.state !== 'paused') return;
  $('ovPause').classList.add('hidden');
  run.state = 'playing';
  loop.last = performance.now();
}

function leaveLoop() {
  if (loop) loop.stop();
  run.state = 'idle';
}

function leaveGame() {
  leaveLoop();
  openHub();
}

async function finishRun() {
  const mod = run.mod;
  const score = run.score;
  run.state = 'over';
  $('hudPause').disabled = true;
  sfx.play('over');
  haptic('error');

  const prev = localBest(mod.id);
  const isNew = score > prev && score > 0;
  if (isNew) store.set('best:' + mod.id, score);

  $('overScore').textContent = score;
  $('overBadge').classList.toggle('hidden', !isNew);
  $('overBoard').innerHTML = '';
  const status = $('overStatus');
  status.className = 'status';
  status.textContent = t('saving');
  $('ovOver').classList.remove('hidden');
  if (isNew) setTimeout(() => haptic('success'), 350);

  const thisRun = run.inst;
  const session = await run.session;
  if (run.inst !== thisRun) return; // player already moved on
  if (!session) {
    const err = run.sessionError;
    status.textContent = err && err.status === 401 ? t('guest') : t('offline');
    return;
  }
  try {
    const res = await api.submitScore(session, score);
    if (run.inst !== thisRun) return;
    status.textContent = t('rankMonth', { rank: res.month.rank });
    status.classList.add('good');
    if (res.all && res.all.best > prev) store.set('best:' + mod.id, res.all.best);
  } catch (e) {
    if (run.inst !== thisRun) return;
    status.textContent = e.status === 422 || e.status === 409 ? t('rejected') : t('offline');
  }
  try {
    const board = await api.leaderboard(mod.id, 'month', 5);
    if (run.inst !== thisRun) return;
    $('overBoard').innerHTML = boardRows(board, true);
  } catch (e) { /* ignore */ }
}

function share() {
  const mod = run.mod;
  const best = Math.max(localBest(mod.id), run.score);
  const msg = t('shareText', { game: L(mod.title), score: best });
  openTelegramLink(`https://t.me/share/url?url=${encodeURIComponent(BOT_URL)}&text=${encodeURIComponent(msg)}`);
}

// ------------------------------------------------------------------ leaderboard

const MEDALS = ['🥇', '🥈', '🥉'];

function rowHTML(e) {
  const rank = e.rank <= 3 ? MEDALS[e.rank - 1] : e.rank;
  const name = esc(e.name) + (e.me ? ` <span style="color:var(--muted)">(${t('you')})</span>` : '');
  return `<li class="lb-row${e.me ? ' me' : ''}"><span class="lb-rank">${rank}</span><span class="lb-name">${name}</span><span class="lb-score">${e.score}</span></li>`;
}

function boardRows(board, compact) {
  if (!board.entries.length) return compact ? '' : `<li class="lb-empty">${t(board.period === 'history' ? 'noHistory' : 'empty')}</li>`;
  let html = board.entries.map(rowHTML).join('');
  if (board.me && !board.entries.some((e) => e.me)) {
    html += `<li class="lb-gap">···</li>` + rowHTML({ rank: board.me.rank, name: t('you'), score: board.me.score, me: true });
  }
  return html;
}

let boardGame = null;
let boardPeriod = 'month';
let boardMonth = null;   // selected finished month in the History tab
let boardMonths = null;  // finished months, newest first

function renderChips() {
  const el = $('monthChips');
  el.classList.toggle('hidden', boardPeriod !== 'history' || !boardMonths || !boardMonths.length);
  if (!boardMonths) return;
  el.innerHTML = boardMonths.map((m) =>
    `<button class="chip${m === boardMonth ? ' active' : ''}" type="button" data-month="${m}">${esc(monthLabel(m))}</button>`).join('');
}

async function loadBoard() {
  const list = $('boardList');
  list.innerHTML = `<li class="lb-empty">${t('loading')}</li>`;
  document.querySelectorAll('.tab').forEach((b) => b.classList.toggle('active', b.dataset.period === boardPeriod));
  const want = boardGame + boardPeriod + boardMonth;
  try {
    let board;
    if (boardPeriod === 'history') {
      if (!boardMonths) boardMonths = (await api.leaderboard(boardGame, 'month', 1)).months;
      if (!boardMonth) boardMonth = boardMonths[0] || null;
      renderChips();
      board = boardMonth ? await api.history(boardGame, boardMonth) : { entries: [], period: 'history', me: null };
    } else {
      renderChips();
      board = await api.leaderboard(boardGame, boardPeriod, 50);
      boardMonths = board.months;
    }
    if (want !== boardGame + boardPeriod + boardMonth) return;
    list.innerHTML = boardRows(board, false);
    if (boardPeriod === 'history') $('boardNote').textContent = board.entries.length ? `👑 ${t('winnerOf')}: ${board.entries[0].name}` : '';
    else $('boardNote').textContent = board.me ? t('yourRank', { rank: board.me.rank }) : t('resetsMonthly');
  } catch (e) {
    list.innerHTML = `<li class="lb-empty">${t('offline')}</li>`;
  }
}

function renderGameSwitch() {
  $('gameSwitch').innerHTML = GAMES.map((g) =>
    `<button type="button" data-game="${g.id}" class="${g.id === boardGame ? 'active' : ''}" aria-label="${esc(L(g.title))}">${g.emoji}</button>`).join('');
  $('boardTitle').textContent = L(byId[boardGame].title);
}

function openBoard(id, from) {
  boardGame = id;
  boardPeriod = 'month';
  boardMonth = null;
  boardMonths = null;
  run.boardFrom = from;
  renderGameSwitch();
  show('board');
  loadBoard();
}

// ------------------------------------------------------------------ game ideas

function openIdea() {
  leaveLoop();
  $('ideaForm').classList.remove('hidden');
  $('ideaDone').classList.add('hidden');
  $('ideaStatus').textContent = '';
  $('ideaStatus').className = 'status';
  $('ideaSend').disabled = false;
  $('ideaSend').textContent = t('ideaSend');
  show('idea');
}

async function sendIdea(e) {
  e.preventDefault();
  const text = $('ideaText').value.trim();
  const title = $('ideaName').value.trim();
  const status = $('ideaStatus');
  status.className = 'status';
  if (text.length < 10) { status.textContent = t('ideaShort'); haptic('warning'); return; }
  $('ideaSend').disabled = true;
  $('ideaSend').textContent = t('ideaSending');
  try {
    await api.sendIdea(title, text);
    haptic('success');
    $('ideaText').value = ''; $('ideaName').value = ''; $('ideaCount').textContent = '0 / 1500';
    $('ideaForm').classList.add('hidden');
    $('ideaDone').classList.remove('hidden');
  } catch (err) {
    haptic('error');
    status.textContent = err.status === 401 ? t('ideaGuest') : err.status === 429 ? t('ideaLimit')
      : err.message === 'too_short' ? t('ideaShort') : t('ideaFail');
    $('ideaSend').disabled = false;
    $('ideaSend').textContent = t('ideaSend');
  }
}

// ------------------------------------------------------------------ wiring

function bind() {
  $('cards').addEventListener('click', (e) => {
    const card = e.target.closest('.card');
    if (!card) return;
    haptic('light');
    sfx.unlock();
    openGame(card.dataset.game);
  });
  $('bookBtn').addEventListener('click', () => openLink(BOOKING_URL));
  $('ideaBtn').addEventListener('click', () => { haptic('light'); openIdea(); });
  $('ideaBack').addEventListener('click', openHub);
  $('ideaDoneBtn').addEventListener('click', openHub);
  $('ideaForm').addEventListener('submit', sendIdea);
  $('ideaText').addEventListener('input', () => { $('ideaCount').textContent = `${$('ideaText').value.length} / 1500`; });
  $('overBook').addEventListener('click', () => openLink(BOOKING_URL));
  $('langBtn').addEventListener('click', () => {
    setLang(getLang() === 'uk' ? 'en' : 'uk');
    applyStaticText();
    renderCards();
    renderPrize();
    renderWins();
  });
  $('soundBtn').addEventListener('click', () => {
    sfx.enabled = !sfx.enabled;
    store.set('sound', sfx.enabled ? 'on' : 'off');
    sfx.unlock();
    sfx.play('tap');
    applyStaticText();
  });

  $('startBtn').addEventListener('click', startRun);
  $('ovStart').addEventListener('pointerdown', (e) => { if (e.target === $('ovStart')) startRun(); });
  $('hudPause').addEventListener('click', () => (run.state === 'paused' ? resume() : pause()));
  $('resumeBtn').addEventListener('click', resume);
  $('quitBtn').addEventListener('click', leaveGame);
  $('hudBack').addEventListener('click', leaveGame);
  $('againBtn').addEventListener('click', () => { haptic('light'); newRun(); startRun(); });
  $('shareBtn').addEventListener('click', share);
  $('boardBtn').addEventListener('click', () => openBoard(run.mod.id, 'play'));
  $('boardBack').addEventListener('click', goBack);
  $('gameSwitch').addEventListener('click', (e) => {
    const b = e.target.closest('[data-game]');
    if (!b || b.dataset.game === boardGame) return;
    boardGame = b.dataset.game;
    haptic('select');
    renderGameSwitch();
    loadBoard();
  });
  $('monthChips').addEventListener('click', (e) => {
    const chip = e.target.closest('[data-month]');
    if (!chip) return;
    boardMonth = chip.dataset.month;
    haptic('select');
    loadBoard();
  });
  $('prizeBanner').addEventListener('click', () => {
    haptic('light');
    openBoard(GAMES[0].id, 'hub');
  });
  document.querySelectorAll('.tab').forEach((b) => b.addEventListener('click', () => {
    boardPeriod = b.dataset.period;
    haptic('select');
    loadBoard();
  }));

  const KEYS = { ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up', ArrowDown: 'down', ' ': 'space', w: 'up', a: 'left', s: 'down', d: 'right' };
  document.addEventListener('keydown', (e) => {
    if (current !== 'play') return;
    if (e.key === 'Escape' || e.key === 'p') { run.state === 'paused' ? resume() : pause(); return; }
    if (run.state === 'ready' && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); startRun(); return; }
    const k = KEYS[e.key];
    if (k && run.state === 'playing') {
      e.preventDefault();
      if (!e.repeat && run.inst.control) run.inst.control(k, true);
    }
  });
  document.addEventListener('keyup', (e) => {
    const k = KEYS[e.key];
    if (k && run.inst && run.inst.control) run.inst.control(k, false);
  });

  onAppHidden(pause);
}

initTelegram(goBack);
applyStaticText();
bind();
$('ideaBtn').classList.toggle('hidden', !IDEAS_ENABLED);
// Opened inside Telegram but without signed launch data (old reply-keyboard button): explain how to get on the board.
if (tg && tg.platform && tg.platform !== 'unknown' && !tg.initData) $('launchHint').classList.remove('hidden');
$('launchHintBtn').addEventListener('click', () => openTelegramLink(BOT_URL));
openHub();
// Deep link to a game: t.me/<bot>?startapp=snake, or /?game=snake (old /snake and /tetris URLs redirect here)
const wanted = (tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param) || new URLSearchParams(location.search).get('game');
if (wanted && byId[wanted]) openGame(wanted);
