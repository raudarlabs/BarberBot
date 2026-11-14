import { COLORS, Particles, Shake, Swipe, TAU, clamp, lerp, rr } from '../engine.js';

// Scoring (keep api/_games.py "snake" limits in sync): food +10, golden comb +30.
const COLS = 17;
const FOOD_POINTS = 10;
const BONUS_POINTS = 30;
const BONUS_EVERY = 5;
const BONUS_TTL = 5.5;
// Seconds per step: starts calm, speeds up a little with every food, never faster than MIN_INTERVAL.
const START_INTERVAL = 0.2;
const MIN_INTERVAL = 0.1;
const SPEEDUP = 0.0022;
const DIRS = { up: { x: 0, y: -1 }, down: { x: 0, y: 1 }, left: { x: -1, y: 0 }, right: { x: 1, y: 0 } };

export function drawComb(ctx, x, y, s, color = COLORS.gold) {
  ctx.save();
  ctx.translate(x, y);
  ctx.rotate(-0.35);
  ctx.fillStyle = color;
  rr(ctx, -s * 0.5, -s * 0.32, s, s * 0.24, s * 0.08);
  ctx.fill();
  const teeth = 7;
  for (let i = 0; i < teeth; i++) {
    const tx = -s * 0.46 + (i * s * 0.92) / (teeth - 1);
    ctx.fillRect(tx - s * 0.035, -s * 0.1, s * 0.07, s * 0.4);
  }
  ctx.restore();
}

class SnakeGame {
  constructor(g) {
    this.g = g;
    this.fx = new Particles();
    this.shake = new Shake();
    this.swipe = new Swipe((d) => this.turn(d), 18);
    this.snake = null;
    this.time = 0;
  }

  resize(w, h) {
    if (!this.rows) {
      const guess = Math.floor(h / (w / COLS));
      this.rows = clamp(guess, 12, 42); // tall phones get a tall board, no empty bands
    }
    this.cell = Math.floor(Math.min((w - 12) / COLS, (h - 12) / this.rows));
    this.ox = Math.floor((w - this.cell * COLS) / 2);
    this.oy = Math.floor((h - this.cell * this.rows) / 2);
    if (!this.snake) this.init();
  }

  init() {
    const cx = Math.floor(COLS / 2) - 2, cy = Math.floor(this.rows / 2);
    this.snake = [{ x: cx, y: cy }, { x: cx - 1, y: cy }, { x: cx - 2, y: cy }];
    this.prev = this.snake.map((s) => ({ ...s }));
    this.dir = DIRS.right;
    this.queue = [];
    this.tick = 0;
    this.interval = START_INTERVAL;
    this.eaten = 0;
    this.bonus = null;
    this.dead = false;
    this.deadT = 0;
    this.food = this.freeCell();
  }

  freeCell() {
    const taken = new Set(this.snake.map((s) => s.x + ',' + s.y));
    if (this.food) taken.add(this.food.x + ',' + this.food.y);
    if (this.bonus) taken.add(this.bonus.x + ',' + this.bonus.y);
    const free = [];
    for (let y = 0; y < this.rows; y++) for (let x = 0; x < COLS; x++) if (!taken.has(x + ',' + y)) free.push({ x, y });
    return free.length ? free[Math.floor(Math.random() * free.length)] : null;
  }

  // Validate against the last *queued* direction, so two quick taps can't reverse into the body.
  turn(name) {
    const d = DIRS[name];
    if (!d || this.dead) return;
    const last = this.queue.length ? this.queue[this.queue.length - 1] : this.dir;
    if (d === last || (d.x === -last.x && d.y === -last.y)) return;
    if (this.queue.length < 3) this.queue.push(d);
  }

  control(k, pressed) { if (pressed) this.turn(k); }
  down(p) { this.swipe.down(p); }
  move(p) { this.swipe.move(p); }
  up() { this.swipe.up(); }

  idle(dt) { this.time += dt; }

  update(dt) {
    this.time += dt;
    this.fx.update(dt);
    this.shake.update(dt);
    if (this.dead) {
      this.deadT += dt;
      if (this.deadT > 0.8) this.g.over();
      return;
    }
    if (this.bonus) {
      this.bonus.ttl -= dt;
      if (this.bonus.ttl <= 0) this.bonus = null;
    }
    this.tick += dt;
    while (this.tick >= this.interval && !this.dead) {
      this.tick -= this.interval;
      this.step();
    }
  }

  center(c) { return { x: this.ox + (c.x + 0.5) * this.cell, y: this.oy + (c.y + 0.5) * this.cell }; }

  step() {
    if (this.queue.length) this.dir = this.queue.shift();
    const head = this.snake[0];
    const next = { x: head.x + this.dir.x, y: head.y + this.dir.y };
    const eatsFood = this.food && next.x === this.food.x && next.y === this.food.y;
    const eatsBonus = this.bonus && next.x === this.bonus.x && next.y === this.bonus.y;
    const grows = eatsFood || eatsBonus;

    const hitsWall = next.x < 0 || next.y < 0 || next.x >= COLS || next.y >= this.rows;
    // The tail moves away this tick unless we grow, so it's not an obstacle.
    const body = grows ? this.snake : this.snake.slice(0, -1);
    const hitsSelf = body.some((s) => s.x === next.x && s.y === next.y);
    if (hitsWall || hitsSelf) return this.die();

    this.prev = this.snake.map((s) => ({ ...s }));
    this.snake.unshift(next);
    const at = this.center(next);
    if (eatsFood) {
      this.eaten++;
      this.g.addScore(FOOD_POINTS, at.x, at.y - this.cell * 0.6);
      this.g.sfx.play('point');
      this.g.haptic('light');
      this.fx.burst(at.x, at.y, 10, { colors: [COLORS.red, '#ff8a80', COLORS.cream], speed: 160, life: 0.5, size: 3 });
      this.food = this.freeCell();
      this.interval = Math.max(MIN_INTERVAL, START_INTERVAL - this.eaten * SPEEDUP);
      if (this.eaten % BONUS_EVERY === 0 && !this.bonus) {
        const c = this.freeCell();
        if (c) this.bonus = { ...c, ttl: BONUS_TTL };
      }
    }
    if (eatsBonus) {
      this.bonus = null;
      this.g.addScore(BONUS_POINTS, at.x, at.y - this.cell * 0.6, '+' + BONUS_POINTS, COLORS.goldHi);
      this.g.sfx.play('bonus');
      this.g.haptic('medium');
      this.fx.burst(at.x, at.y, 18, { colors: [COLORS.gold, COLORS.goldHi, '#fff'], speed: 220, life: 0.7, size: 3.5 });
    }
    if (!grows) this.snake.pop();
    if (!this.food) this.die(); // board full
  }

  die() {
    this.dead = true;
    this.g.sfx.play('hit');
    this.g.haptic('heavy');
    this.shake.kick(7, 0.3);
    const h = this.center(this.snake[0]);
    this.fx.burst(h.x, h.y, 26, { colors: [COLORS.accent, COLORS.red, COLORS.cream], speed: 260, life: 0.8, size: 4 });
  }

  render(ctx) {
    const { cell, ox, oy } = this;
    const [sx, sy] = this.shake.offset();
    ctx.save();
    ctx.translate(sx, sy);

    // board
    ctx.fillStyle = '#160f12';
    rr(ctx, ox - 4, oy - 4, cell * COLS + 8, cell * this.rows + 8, 12);
    ctx.fill();
    ctx.fillStyle = 'rgba(228,184,178,0.035)';
    for (let y = 0; y < this.rows; y++) for (let x = (y % 2); x < COLS; x += 2) ctx.fillRect(ox + x * cell, oy + y * cell, cell, cell);
    ctx.strokeStyle = 'rgba(228,184,178,0.25)';
    ctx.lineWidth = 1;
    rr(ctx, ox - 4.5, oy - 4.5, cell * COLS + 9, cell * this.rows + 9, 12);
    ctx.stroke();

    // food
    if (this.food) {
      const c = this.center(this.food);
      const pulse = 1 + Math.sin(this.time * 6) * 0.1;
      const r = cell * 0.36 * pulse;
      ctx.shadowColor = COLORS.red; ctx.shadowBlur = 12;
      ctx.fillStyle = COLORS.red;
      ctx.beginPath(); ctx.arc(c.x, c.y, r, 0, TAU); ctx.fill();
      ctx.shadowBlur = 0;
      ctx.fillStyle = 'rgba(255,255,255,0.55)';
      ctx.beginPath(); ctx.arc(c.x - r * 0.35, c.y - r * 0.35, r * 0.28, 0, TAU); ctx.fill();
    }
    if (this.bonus) {
      const c = this.center(this.bonus);
      const blink = this.bonus.ttl < 1.5 && Math.floor(this.time * 8) % 2 === 0;
      if (!blink) {
        ctx.shadowColor = COLORS.gold; ctx.shadowBlur = 16;
        drawComb(ctx, c.x, c.y + cell * 0.05, cell * 0.95, COLORS.goldHi);
        ctx.shadowBlur = 0;
      }
    }

    // snake, interpolated between ticks for smooth motion
    const k = this.dead ? 1 : clamp(this.tick / this.interval, 0, 1);
    const pts = this.snake.map((s, i) => {
      const p = this.prev[i] || s;
      return this.center({ x: lerp(p.x, s.x, k), y: lerp(p.y, s.y, k) });
    });
    ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    const n = pts.length;
    for (let i = n - 1; i > 0; i--) {
      const t = i / Math.max(1, n - 1);
      ctx.strokeStyle = `rgb(${Math.round(lerp(235, 122, t))},${Math.round(lerp(190, 43, t))},${Math.round(lerp(184, 67, t))})`;
      ctx.lineWidth = cell * lerp(0.78, 0.55, t);
      ctx.beginPath(); ctx.moveTo(pts[i].x, pts[i].y); ctx.lineTo(pts[i - 1].x, pts[i - 1].y); ctx.stroke();
    }
    const h = pts[0];
    ctx.fillStyle = this.dead && Math.floor(this.deadT * 10) % 2 ? COLORS.red : COLORS.accentHi;
    ctx.beginPath(); ctx.arc(h.x, h.y, cell * 0.46, 0, TAU); ctx.fill();
    // eyes look where we're heading
    const d = this.queue[0] || this.dir;
    const ex = d.x * cell * 0.16, ey = d.y * cell * 0.16;
    const px = -d.y * cell * 0.19, py = d.x * cell * 0.19;
    for (const s of [1, -1]) {
      ctx.fillStyle = '#fff';
      ctx.beginPath(); ctx.arc(h.x + ex + px * s, h.y + ey + py * s, cell * 0.12, 0, TAU); ctx.fill();
      ctx.fillStyle = '#111';
      ctx.beginPath(); ctx.arc(h.x + ex * 1.4 + px * s, h.y + ey * 1.4 + py * s, cell * 0.06, 0, TAU); ctx.fill();
    }

    this.fx.render(ctx);
    ctx.restore();
  }
}

export default {
  id: 'snake',
  emoji: '🐍',
  tile: ['#34d27a', '#14864a'],
  controls: null, // full-screen board, swipe only (arrow keys still work on desktop)
  title: { uk: 'Змійка', en: 'Snake' },
  desc: {
    uk: 'Ніхто не народжується з великим пітоном. Його вирощують.',
    en: 'Nobody is born with a big python. You grow it.',
  },
  how: {
    uk: 'Свайпай по екрану, щоб повернути. Червоне — +10. Золотий гребінець — +30, але він швидко зникає.',
    en: 'Swipe anywhere to turn. Red dots are +10. The golden comb is +30 — grab it before it fades.',
  },
  create: (g) => new SnakeGame(g),
};
