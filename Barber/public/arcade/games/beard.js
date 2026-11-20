import { COLORS, Particles, Shake, TAU, clamp, pick, rand, randInt, rr, segDist } from '../engine.js';

// Scoring (keep api/_games.py "beard" limits in sync):
// tuft +1, golden tuft +5, 3+ tufts in one swipe → +combo size bonus.
// Losing a life: a tuft falls uncut, or you cut a lotion bottle.
const LIVES = 3;
const TRAIL_MS = 140;
const MIN_CUT_SPEED = 0.45; // px per ms
const HAIR = ['#6b4429', '#8a5a36', '#4e3323', '#a0764d', '#5d5d66'];

// Deterministic blob layout for a tuft (unit circle coordinates)
const BLOBS = [[0, 0, 0.55], [-0.42, -0.18, 0.4], [0.42, -0.2, 0.42], [-0.3, 0.32, 0.38], [0.32, 0.3, 0.4], [0, -0.45, 0.36], [0, 0.48, 0.32]];

function drawTuft(ctx, r, color, gold) {
  // light rim so dark hair reads on the dark wall
  ctx.fillStyle = gold ? 'rgba(245,217,212,0.5)' : 'rgba(240,200,205,0.28)';
  for (const [x, y, s] of BLOBS) { ctx.beginPath(); ctx.arc(x * r, y * r, s * r + Math.max(1.5, r * 0.06), 0, TAU); ctx.fill(); }
  ctx.fillStyle = color;
  for (const [x, y, s] of BLOBS) { ctx.beginPath(); ctx.arc(x * r, y * r, s * r, 0, TAU); ctx.fill(); }
  ctx.strokeStyle = gold ? 'rgba(255,255,255,0.55)' : 'rgba(255,255,255,0.13)';
  ctx.lineWidth = Math.max(1, r * 0.07);
  ctx.lineCap = 'round';
  for (let i = 0; i < 5; i++) {
    const a = -0.9 + i * 0.45;
    ctx.beginPath();
    ctx.arc(Math.cos(a) * r * 0.25, Math.sin(a) * r * 0.25, r * 0.28, a, a + 1.6);
    ctx.stroke();
  }
}

function drawBottle(ctx, r) {
  ctx.fillStyle = '#2b4c9b';
  rr(ctx, -r * 0.42, -r * 0.55, r * 0.84, r * 1.25, r * 0.2); ctx.fill();
  ctx.fillStyle = '#d9dbe6';
  rr(ctx, -r * 0.18, -r * 0.85, r * 0.36, r * 0.34, r * 0.06); ctx.fill();
  ctx.fillStyle = COLORS.white;
  rr(ctx, -r * 0.32, -r * 0.2, r * 0.64, r * 0.5, r * 0.08); ctx.fill();
  ctx.strokeStyle = COLORS.red; ctx.lineWidth = r * 0.1; ctx.lineCap = 'round';
  ctx.beginPath(); ctx.moveTo(-r * 0.14, -r * 0.08); ctx.lineTo(r * 0.14, r * 0.18); ctx.moveTo(r * 0.14, -r * 0.08); ctx.lineTo(-r * 0.14, r * 0.18); ctx.stroke();
  ctx.fillStyle = 'rgba(255,255,255,0.18)';
  rr(ctx, -r * 0.36, -r * 0.5, r * 0.12, r * 1.1, r * 0.06); ctx.fill();
}

class BeardGame {
  constructor(g) {
    this.g = g;
    this.fx = new Particles();
    this.shake = new Shake();
    this.items = [];
    this.halves = [];
    this.trail = [];
    this.lives = LIVES;
    this.elapsed = 0;
    this.spawnT = 0.6;
    this.swipeCuts = 0;
    this.swipeAt = { x: 0, y: 0 };
    this.lastCutT = 0;
    this.pointer = null;
    this.dead = false;
    this.deadT = 0;
    this.time = 0;
    this.flash = 0;
  }

  resize(w, h) {
    this.w = w; this.h = h;
    this.unit = Math.min(w, h * 0.7) / 400;
    this.grav = h * 0.95;
  }

  start() { this.g.setLives(this.lives, LIVES); }

  spawnWave() {
    const diff = clamp(this.elapsed / 90, 0, 1);
    const count = randInt(1, 1 + Math.round(diff * 3));
    const bottleChance = 0.06 + diff * 0.16;
    for (let i = 0; i < count; i++) {
      const kind = i > 0 && Math.random() < bottleChance ? 'bottle' : Math.random() < 0.07 ? 'gold' : 'tuft';
      const r = (kind === 'bottle' ? 30 : rand(30, 40)) * this.unit;
      const x = rand(this.w * 0.15, this.w * 0.85);
      const peak = rand(this.h * 0.18, this.h * 0.5);
      const vy = -Math.sqrt(2 * this.grav * (this.h + r - peak));
      const flight = (-2 * vy) / this.grav;
      const tx = rand(this.w * 0.2, this.w * 0.8);
      this.items.push({
        kind, r, x, y: this.h + r, vx: (tx - x) / flight, vy: vy * rand(0.97, 1.0),
        rot: rand(0, TAU), vr: rand(-2.5, 2.5), color: kind === 'gold' ? COLORS.gold : pick(HAIR), delay: i * rand(0.12, 0.3),
      });
    }
    this.spawnT = Math.max(0.55, 1.35 - diff * 0.75) + rand(0, 0.35);
  }

  loseLife(x, y) {
    if (this.dead) return;
    this.lives--;
    this.g.setLives(this.lives, LIVES);
    this.g.haptic('error');
    this.shake.kick(7, 0.3);
    this.flash = 0.25;
    if (x != null) this.g.float(x, y, '−❤', COLORS.red, 24);
    if (this.lives <= 0) { this.dead = true; this.trail = []; }
  }

  slice(it, ax, ay, bx, by) {
    it.cut = true;
    const ang = Math.atan2(by - ay, bx - ax);
    if (it.kind === 'bottle') {
      this.g.sfx.play('hit');
      this.fx.burst(it.x, it.y, 22, { colors: ['#7fb2ff', '#ffffff', '#2b4c9b'], speed: 260, life: 0.7, size: 4, g: 500 });
      this.loseLife(it.x, it.y - it.r);
      return;
    }
    const pts = it.kind === 'gold' ? 5 : 1;
    this.swipeCuts++;
    this.swipeAt = { x: it.x, y: it.y };
    this.lastCutT = this.time;
    this.g.addScore(pts, it.x, it.y - it.r, '+' + pts, it.kind === 'gold' ? COLORS.goldHi : undefined);
    this.g.sfx.play(it.kind === 'gold' ? 'bonus' : 'slice');
    this.g.haptic(it.kind === 'gold' ? 'medium' : 'light');
    const nx = -Math.sin(ang), ny = Math.cos(ang);
    for (const s of [1, -1]) {
      this.halves.push({
        x: it.x, y: it.y, vx: it.vx * 0.5 + nx * s * 90, vy: it.vy * 0.3 + ny * s * 90 - 60,
        rot: it.rot, vr: s * rand(3, 6), r: it.r, color: it.color, side: s, ang, life: 1.4, gold: it.kind === 'gold',
      });
    }
    for (let i = 0; i < 12; i++) {
      this.fx.add({ x: it.x, y: it.y, vx: rand(-160, 160), vy: rand(-200, 40), g: 700, drag: 0.4, life: rand(0.5, 0.9),
        size: rand(2, 4.5), shape: 'line', color: it.kind === 'gold' ? COLORS.goldHi : it.color, rot: rand(0, TAU), vr: rand(-12, 12) });
    }
  }

  endSwipe() {
    if (this.swipeCuts >= 3) {
      const n = this.swipeCuts;
      this.g.addScore(n, this.swipeAt.x, this.swipeAt.y - 50, this.g.t('combo', { n }), COLORS.goldHi);
      this.g.sfx.play('perfect');
      this.g.haptic('heavy');
    }
    this.swipeCuts = 0;
  }

  down(p) { if (this.pointer) return; this.pointer = p; this.trail = [p]; }
  move(p) {
    // follow only the finger that started the swipe
    if (!this.pointer || this.dead || p.id !== this.pointer.id) return;
    const a = this.pointer;
    const dt = Math.max(1, p.t - a.t);
    const speed = Math.hypot(p.x - a.x, p.y - a.y) / dt;
    this.trail.push(p);
    if (speed >= MIN_CUT_SPEED) {
      for (const it of this.items) {
        if (it.cut || it.delay > 0) continue;
        if (segDist(it.x, it.y, a.x, a.y, p.x, p.y) <= it.r * 0.9) this.slice(it, a.x, a.y, p.x, p.y);
      }
    }
    this.pointer = p;
  }
  up(p) {
    if (!this.pointer || p.id !== this.pointer.id) return;
    this.pointer = null;
    this.endSwipe();
  }

  idle(dt) { this.time += dt; }

  update(dt) {
    this.time += dt;
    this.fx.update(dt);
    this.shake.update(dt);
    this.flash = Math.max(0, this.flash - dt);
    const now = performance.now();
    this.trail = this.trail.filter((p) => now - p.t < TRAIL_MS);
    if (this.swipeCuts && this.time - this.lastCutT > 0.35 && !this.pointer) this.endSwipe();

    for (const hv of this.halves) {
      hv.vy += this.grav * dt; hv.x += hv.vx * dt; hv.y += hv.vy * dt; hv.rot += hv.vr * dt; hv.life -= dt;
    }
    this.halves = this.halves.filter((hv) => hv.life > 0 && hv.y < this.h + hv.r * 2);

    if (this.dead) {
      this.deadT += dt;
      if (this.deadT > 1.0) this.g.over();
      return;
    }

    this.elapsed += dt;
    this.spawnT -= dt;
    if (this.spawnT <= 0) this.spawnWave();

    for (const it of this.items) {
      if (it.delay > 0) { it.delay -= dt; continue; }
      it.vy += this.grav * dt; it.x += it.vx * dt; it.y += it.vy * dt; it.rot += it.vr * dt;
    }
    const gone = this.items.filter((it) => it.cut || (it.vy > 0 && it.y > this.h + it.r));
    for (const it of gone) {
      if (!it.cut && it.kind !== 'bottle') {
        this.g.sfx.play('buzz');
        this.loseLife(clamp(it.x, 30, this.w - 30), this.h - 30);
      }
    }
    this.items = this.items.filter((it) => !gone.includes(it));
  }

  render(ctx) {
    const { w, h } = this;
    const [sx, sy] = this.shake.offset();
    ctx.save();
    ctx.translate(sx, sy);

    // backdrop: barbershop tiles
    ctx.fillStyle = '#170f12';
    ctx.fillRect(-10, -10, w + 20, h + 20);
    const tile = 34 * this.unit + 10;
    ctx.fillStyle = 'rgba(228,184,178,0.03)';
    for (let y = 0; y < h; y += tile) for (let x = ((y / tile) % 2) * tile; x < w; x += tile * 2) ctx.fillRect(x, y, tile, tile);

    for (const it of this.items) {
      if (it.delay > 0) continue;
      ctx.save();
      ctx.translate(it.x, it.y);
      ctx.rotate(it.rot);
      if (it.kind === 'bottle') {
        ctx.shadowColor = 'rgba(224,67,58,0.7)'; ctx.shadowBlur = 14;
        drawBottle(ctx, it.r);
      } else {
        if (it.kind === 'gold') { ctx.shadowColor = COLORS.gold; ctx.shadowBlur = 20; }
        drawTuft(ctx, it.r, it.color, it.kind === 'gold');
      }
      ctx.restore();
    }

    for (const hv of this.halves) {
      ctx.save();
      ctx.globalAlpha = clamp(hv.life / 0.4, 0, 1);
      ctx.translate(hv.x, hv.y);
      ctx.rotate(hv.rot);
      ctx.beginPath();
      ctx.rect(-hv.r * 2, hv.side > 0 ? 0 : -hv.r * 2, hv.r * 4, hv.r * 2);
      ctx.clip();
      drawTuft(ctx, hv.r, hv.color, hv.gold);
      ctx.restore();
    }

    this.fx.render(ctx);

    // blade trail
    if (this.trail.length > 1) {
      const now = performance.now();
      ctx.lineCap = 'round'; ctx.lineJoin = 'round';
      for (let i = 1; i < this.trail.length; i++) {
        const a = this.trail[i - 1], b = this.trail[i];
        const k = 1 - (now - b.t) / TRAIL_MS;
        ctx.strokeStyle = `rgba(245,217,212,${0.25 * k})`;
        ctx.lineWidth = 12 * k + 2;
        ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
        ctx.strokeStyle = `rgba(255,255,255,${0.9 * k})`;
        ctx.lineWidth = 4 * k + 1;
        ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
      }
    }
    ctx.restore();

    if (this.flash > 0) {
      ctx.fillStyle = `rgba(224,67,58,${this.flash * 0.6})`;
      ctx.fillRect(0, 0, w, h);
    }
  }
}

export default {
  id: 'beard',
  emoji: '🪒',
  tile: ['#9a5cf0', '#5a2bb5'],
  lives: LIVES,
  isNew: true,
  title: { uk: 'Бородоріз', en: 'Beardslayer' },
  desc: {
    uk: 'Борода росте швидше, ніж ти думаєш.',
    en: 'Beards grow faster than you think.',
  },
  how: {
    uk: 'Різко веди пальцем, щоб зрізати пучки бороди. Не пропускай жодного і не чіпай флакони з лосьйоном. Три пучки за один свайп — комбо.',
    en: 'Swipe fast to slice beard tufts. Don’t let any drop and don’t hit the lotion bottles. Three tufts in one swipe is a combo.',
  },
  create: (g) => new BeardGame(g),
};
