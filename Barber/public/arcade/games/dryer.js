import { COLORS, Particles, Shake, TAU, circleRect, clamp, drawCap, drawPole, rand, rr } from '../engine.js';
import { drawComb } from './snake.js';

// Scoring (keep api/_games.py "dryer" limits in sync): +1 per pole pair passed, +2 per golden comb.
// All sizes are in "units": 1u = stage height / 640, so difficulty is the same on every screen.
const GRAVITY = 1750;
const FLAP = -540;
const MAX_FALL = 760;
const POLE_W = 62;
const SPACING = 250;

function drawDryer(ctx, x, y, s, angle, blowing) {
  ctx.save();
  ctx.translate(x, y);
  ctx.rotate(angle);
  // handle
  ctx.fillStyle = '#2a2b44';
  ctx.save();
  ctx.translate(s * 0.12, s * 0.18);
  ctx.rotate(0.25);
  rr(ctx, -s * 0.11, 0, s * 0.22, s * 0.5, s * 0.08);
  ctx.fill();
  ctx.fillStyle = COLORS.gold;
  ctx.fillRect(-s * 0.11, s * 0.36, s * 0.22, s * 0.05);
  ctx.restore();
  // body
  const g = ctx.createLinearGradient(0, -s * 0.3, 0, s * 0.3);
  g.addColorStop(0, '#f2ead6'); g.addColorStop(0.5, '#d9cfb7'); g.addColorStop(1, '#a89e86');
  ctx.fillStyle = g;
  rr(ctx, -s * 0.38, -s * 0.28, s * 0.86, s * 0.56, s * 0.26); ctx.fill();
  // rear grille
  ctx.fillStyle = '#2a2b44';
  rr(ctx, s * 0.38, -s * 0.22, s * 0.14, s * 0.44, s * 0.06); ctx.fill();
  ctx.strokeStyle = 'rgba(255,255,255,0.25)'; ctx.lineWidth = 1;
  for (let i = -1; i <= 1; i++) { ctx.beginPath(); ctx.moveTo(s * 0.4, i * s * 0.11); ctx.lineTo(s * 0.5, i * s * 0.11); ctx.stroke(); }
  // nozzle
  ctx.fillStyle = '#2a2b44';
  ctx.beginPath();
  ctx.moveTo(-s * 0.36, -s * 0.2); ctx.lineTo(-s * 0.62, -s * 0.15); ctx.lineTo(-s * 0.62, s * 0.15); ctx.lineTo(-s * 0.36, s * 0.2);
  ctx.closePath(); ctx.fill();
  // gold ring + eye
  ctx.fillStyle = COLORS.gold;
  ctx.fillRect(-s * 0.2, -s * 0.28, s * 0.06, s * 0.56);
  ctx.fillStyle = '#fff';
  ctx.beginPath(); ctx.arc(s * 0.12, -s * 0.06, s * 0.1, 0, TAU); ctx.fill();
  ctx.fillStyle = '#111';
  ctx.beginPath(); ctx.arc(s * 0.09, -s * 0.06, s * 0.05, 0, TAU); ctx.fill();
  if (blowing > 0) {
    ctx.strokeStyle = `rgba(245,217,212,${blowing})`; ctx.lineWidth = 2.5; ctx.lineCap = 'round';
    for (let i = -1; i <= 1; i++) {
      ctx.beginPath(); ctx.moveTo(-s * 0.7, i * s * 0.1); ctx.lineTo(-s * (0.9 + blowing * 0.3), i * s * 0.16); ctx.stroke();
    }
  }
  ctx.restore();
}

class DryerGame {
  constructor(g) {
    this.g = g;
    this.fx = new Particles();
    this.shake = new Shake();
    this.time = 0;
    this.poles = [];
    this.combs = [];
    this.vy = 0;
    this.y = 0;
    this.blow = 0;
    this.scroll = 0;
    this.passed = 0;
    this.dead = false;
    this.deadT = 0;
    this.started = false;
  }

  resize(w, h) {
    this.w = w; this.h = h;
    this.u = h / 640;
    this.floor = h - 56 * this.u;
    this.x = w * 0.3;
    if (!this.started) this.y = h * 0.45;
  }

  gap() { return Math.max(150, 205 - this.passed * 1.6) * this.u; }
  speed() { return Math.min(235, 165 + this.passed * 2.2) * this.u; }

  addPole(x) {
    const gap = this.gap();
    const last = this.poles.length ? this.poles[this.poles.length - 1].gy : this.h * 0.45;
    const minY = 70 * this.u + gap / 2, maxY = this.floor - 50 * this.u - gap / 2;
    const gy = clamp(last + rand(-170, 170) * this.u, minY, maxY);
    this.poles.push({ x, gy, gap, passed: false, off: rand(0, 100) });
    if (Math.random() < 0.3) this.combs.push({ x: x + (POLE_W * this.u) / 2, y: gy, got: false });
  }

  flap() {
    if (this.dead) return;
    this.vy = FLAP * this.u;
    this.blow = 1;
    this.g.sfx.play('flap');
    this.g.haptic('light');
    for (let i = 0; i < 5; i++) {
      this.fx.add({ x: this.x - 30 * this.u, y: this.y + rand(-6, 6) * this.u, vx: rand(-260, -160) * this.u, vy: rand(40, 160) * this.u,
        drag: 2.5, life: rand(0.3, 0.5), size: rand(3, 6) * this.u, color: 'rgba(245,240,225,0.5)' });
    }
  }

  start() {
    this.started = true;
    this.addPole(this.w + 40 * this.u);
    this.flap();
  }

  control(k, pressed) { if (pressed && (k === 'space' || k === 'up')) this.flap(); }
  down() { this.flap(); }

  idle(dt) {
    this.time += dt;
    this.scroll += 60 * this.u * dt;
    this.y = this.h * 0.45 + Math.sin(this.time * 3) * 10 * this.u;
    this.blow = Math.max(0, this.blow - dt * 3);
  }

  crash() {
    if (this.dead) return;
    this.dead = true;
    this.g.sfx.play('hit');
    this.g.haptic('heavy');
    this.shake.kick(8, 0.35);
    this.fx.burst(this.x, this.y, 22, { colors: [COLORS.gold, COLORS.cream, COLORS.red], speed: 240, life: 0.7, size: 3.5 * this.u + 1 });
    this.vy = Math.min(this.vy, -200 * this.u);
  }

  update(dt) {
    this.time += dt;
    this.fx.update(dt);
    this.shake.update(dt);
    this.blow = Math.max(0, this.blow - dt * 3);
    const r = 17 * this.u;

    this.vy = Math.min(this.vy + GRAVITY * this.u * dt, MAX_FALL * this.u);
    this.y += this.vy * dt;
    if (this.y < r) { this.y = r; this.vy = Math.max(0, this.vy); }

    if (this.dead) {
      if (this.y >= this.floor - r) { this.y = this.floor - r; this.deadT += dt; }
      if (this.deadT > 0.5) this.g.over();
      return;
    }
    if (this.y >= this.floor - r) { this.y = this.floor - r; this.crash(); return; }

    const v = this.speed() * dt;
    this.scroll += v;
    const pw = POLE_W * this.u;
    for (const p of this.poles) p.x -= v;
    for (const c of this.combs) c.x -= v;
    const last = this.poles[this.poles.length - 1];
    if (last && last.x < this.w - SPACING * this.u) this.addPole(last.x + SPACING * this.u);
    this.poles = this.poles.filter((p) => p.x > -pw - 10);
    this.combs = this.combs.filter((c) => c.x > -40 && !c.got);

    for (const p of this.poles) {
      const top = p.gy - p.gap / 2, bot = p.gy + p.gap / 2;
      if (circleRect(this.x, this.y, r, p.x, -50, pw, top + 50) || circleRect(this.x, this.y, r, p.x, bot, pw, this.floor - bot)) {
        this.crash();
        return;
      }
      if (!p.passed && p.x + pw < this.x) {
        p.passed = true;
        this.passed++;
        this.g.addScore(1, this.x, this.y - 40 * this.u);
        this.g.sfx.play('point');
      }
    }
    for (const c of this.combs) {
      if (Math.hypot(c.x - this.x, c.y - this.y) < r + 16 * this.u) {
        c.got = true;
        this.g.addScore(2, c.x, c.y - 30 * this.u, '+2', COLORS.goldHi);
        this.g.sfx.play('bonus');
        this.g.haptic('medium');
        this.fx.burst(c.x, c.y, 14, { colors: [COLORS.gold, COLORS.goldHi], speed: 180, life: 0.5, size: 3 });
      }
    }
  }

  drawBackground(ctx) {
    const { w, h, u } = this;
    const g = ctx.createLinearGradient(0, 0, 0, h);
    g.addColorStop(0, '#1a0f13'); g.addColorStop(1, '#2c141d');
    ctx.fillStyle = g;
    ctx.fillRect(-10, -10, w + 20, h + 20);
    // wall: mirrors and lamps, slow parallax
    const span = 220 * u;
    const off = -(this.scroll * 0.25) % span;
    for (let x = off - span; x < w + span; x += span) {
      ctx.strokeStyle = 'rgba(228,184,178,0.22)'; ctx.lineWidth = 3 * u;
      rr(ctx, x + 40 * u, h * 0.2, 120 * u, h * 0.42, 60 * u); ctx.stroke();
      ctx.fillStyle = 'rgba(255,255,255,0.03)';
      rr(ctx, x + 46 * u, h * 0.2 + 6 * u, 108 * u, h * 0.42 - 12 * u, 54 * u); ctx.fill();
      ctx.fillStyle = 'rgba(245,217,212,0.5)';
      ctx.beginPath(); ctx.arc(x + 100 * u, h * 0.12, 6 * u, 0, TAU); ctx.fill();
      ctx.fillStyle = 'rgba(245,217,212,0.06)';
      ctx.beginPath(); ctx.arc(x + 100 * u, h * 0.12, 40 * u, 0, TAU); ctx.fill();
    }
  }

  drawFloor(ctx) {
    const { w, h, u, floor } = this;
    const sq = 24 * u;
    const off = -(this.scroll % (sq * 2));
    ctx.fillStyle = '#0e090b';
    ctx.fillRect(0, floor, w, h - floor);
    for (let row = 0; row * sq < h - floor; row++) {
      for (let x = off + (row % 2) * sq; x < w + sq; x += sq * 2) {
        ctx.fillStyle = 'rgba(245,240,225,0.85)';
        ctx.fillRect(x, floor + row * sq, sq, sq);
      }
    }
    ctx.fillStyle = COLORS.gold;
    ctx.fillRect(0, floor - 3 * u, w, 3 * u);
  }

  render(ctx) {
    const [sx, sy] = this.shake.offset();
    ctx.save();
    ctx.translate(sx, sy);
    this.drawBackground(ctx);

    const pw = POLE_W * this.u;
    const capH = 14 * this.u;
    for (const p of this.poles) {
      const top = p.gy - p.gap / 2, bot = p.gy + p.gap / 2;
      const stripe = this.time * 40 * this.u + p.off;
      drawPole(ctx, p.x + 4 * this.u, -10, pw - 8 * this.u, top + 10 - capH, stripe);
      drawCap(ctx, p.x, top - capH, pw, capH);
      drawPole(ctx, p.x + 4 * this.u, bot + capH, pw - 8 * this.u, this.floor - bot - capH, stripe);
      drawCap(ctx, p.x, bot, pw, capH);
    }
    for (const c of this.combs) {
      if (c.got) continue;
      ctx.shadowColor = COLORS.gold; ctx.shadowBlur = 14;
      drawComb(ctx, c.x, c.y + Math.sin(this.time * 4 + c.x) * 4 * this.u, 30 * this.u, COLORS.goldHi);
      ctx.shadowBlur = 0;
    }

    this.drawFloor(ctx);
    this.fx.render(ctx);
    const angle = this.started ? clamp(this.vy / (900 * this.u), -0.45, 1.25) : Math.sin(this.time * 3) * 0.08;
    drawDryer(ctx, this.x, this.y, 62 * this.u, this.dead ? angle + this.deadT * 6 : angle, this.blow);
    ctx.restore();

    if (!this.started) {
      ctx.fillStyle = 'rgba(245,217,212,0.9)';
      ctx.font = `800 ${Math.round(15 * this.u + 6)}px -apple-system, sans-serif`;
      ctx.textAlign = 'center';
      ctx.fillText('👆', this.x, this.y + 70 * this.u);
    }
  }
}

export default {
  id: 'dryer',
  emoji: '💨',
  tile: ['#ff6b5b', '#c2302a'],
  lives: 0,
  isNew: true,
  title: { uk: 'Фен-пілот', en: 'Dryer Pilot' },
  desc: {
    uk: 'Гарячого повітря вистачає. Головне — не врізатися.',
    en: 'Plenty of hot air. Just don’t crash.',
  },
  how: {
    uk: 'Тапай, щоб фен підстрибував. Пролітай між барберськими стовпами (+1) і лови золоті гребінці (+2). Удар об стовп чи підлогу — кінець польоту.',
    en: 'Tap to puff the dryer upward. Fly between the barber poles (+1) and grab golden combs (+2). Hitting a pole or the floor ends the flight.',
  },
  create: (g) => new DryerGame(g),
};
