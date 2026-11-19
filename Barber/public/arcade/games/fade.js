import { COLORS, Particles, Shake, TAU, clamp, lerp, rand, rr, text } from '../engine.js';

// Scoring (keep api/_games.py "fade" limits in sync):
// perfect 10 (+1 per perfect in a row, up to +10), clean 6, okay 3, miss = −1 life.
const LIVES = 3;
const RESULT_TIME = 0.6;
const CLIENTS = [
  { skin: '#efc6a6', shade: '#d39f7c', hair: '#8a6a45', beard: '#b5652e' },
  { skin: '#c48b62', shade: '#a26f4b', hair: '#1a120d', beard: '#1a120d' },
  { skin: '#f0c9a5', shade: '#d6a983', hair: '#5a3a22' },
  { skin: '#9c6a48', shade: '#7d5236', hair: '#0e0a08', beard: '#0e0a08' },
  { skin: '#e8c09a', shade: '#c99e78', hair: '#3b3b3f' },
];

function drawClipper(ctx, x, y, s, glow) {
  ctx.save();
  ctx.translate(x, y);
  if (glow) { ctx.shadowColor = COLORS.gold; ctx.shadowBlur = 18; }
  // body
  const g = ctx.createLinearGradient(0, -s * 0.2, 0, s * 0.2);
  g.addColorStop(0, '#3a3b52'); g.addColorStop(0.5, '#23243a'); g.addColorStop(1, '#16172a');
  ctx.fillStyle = g;
  rr(ctx, 0, -s * 0.17, s, s * 0.34, s * 0.14); ctx.fill();
  ctx.shadowBlur = 0;
  ctx.fillStyle = COLORS.gold;
  rr(ctx, s * 0.32, -s * 0.05, s * 0.5, s * 0.1, s * 0.05); ctx.fill();
  // blade
  ctx.fillStyle = '#d9dbe6';
  rr(ctx, -s * 0.12, -s * 0.2, s * 0.16, s * 0.4, s * 0.03); ctx.fill();
  ctx.fillStyle = '#9fa3b8';
  for (let i = 0; i < 6; i++) ctx.fillRect(-s * 0.17, -s * 0.18 + i * s * 0.07, s * 0.07, s * 0.035);
  ctx.restore();
}

class FadeGame {
  constructor(g) {
    this.g = g;
    this.fx = new Particles();
    this.shake = new Shake();
    this.time = 0;
    this.lives = LIVES;
    this.streak = 0;
    this.cuts = 0;
    this.speed = 0.55;
    this.half = 0.1;
    this.client = 0;
    this.slide = 0;
    this.dead = false;
    this.deadT = 0;
    this.newRound(true);
  }

  resize(w, h) {
    this.w = w; this.h = h;
    this.R = Math.min(w * 0.3, h * 0.3);
    this.cx = w * 0.42;
    this.cy = h * 0.52;
    this.top = this.cy - this.R * 0.86;
    this.bottom = this.cy + this.R * 0.4;
  }

  yOf(p) { return lerp(this.top, this.bottom, p); }

  newRound(first) {
    this.phase = 'aim';
    this.resultT = 0;
    this.target = rand(0.18, 0.82);
    this.pos = Math.random() < 0.5 ? 0 : 1;
    this.dir = this.pos === 0 ? 1 : -1;
    if (first) { this.fadeLine = 1; this.fadeShown = 1; }
  }

  start() { this.g.setLives(this.lives, LIVES); }

  cut() {
    if (this.phase !== 'aim' || this.dead) return;
    this.phase = 'result';
    this.resultT = 0;
    const d = Math.abs(this.pos - this.target) / this.half;
    const y = this.yOf(this.pos);
    const x = this.cx;
    let grade, pts = 0;
    if (d <= 0.25) { grade = 'perfect'; this.streak++; pts = 10 + Math.min(this.streak - 1, 10); }
    else if (d <= 0.6) { grade = 'great'; this.streak = 0; pts = 6; }
    else if (d <= 1) { grade = 'ok'; this.streak = 0; pts = 3; }
    else { grade = 'miss'; this.streak = 0; }

    if (grade === 'miss') {
      this.lives--;
      this.g.setLives(this.lives, LIVES);
      this.g.float(x, y - 26, this.g.t('miss'), COLORS.red, 26);
      this.g.sfx.play('buzz');
      this.g.haptic('error');
      this.shake.kick(8, 0.3);
      if (this.lives <= 0) { this.dead = true; this.phase = 'dead'; }
      return;
    }
    this.cuts++;
    this.fadeLine = this.pos;
    this.speed = Math.min(2.3, this.speed + 0.055);
    this.half = Math.max(0.035, this.half * 0.95);
    const color = grade === 'perfect' ? COLORS.goldHi : grade === 'great' ? COLORS.cream : COLORS.muted;
    this.g.float(x, y - 30, this.g.t(grade) + (this.streak > 1 && grade === 'perfect' ? ` ×${this.streak}` : ''), color, grade === 'perfect' ? 28 : 22);
    this.g.addScore(pts, x + this.R * 0.55, y - 4, '+' + pts);
    this.g.sfx.play(grade === 'perfect' ? 'perfect' : 'good');
    this.g.haptic(grade === 'perfect' ? 'heavy' : 'light');
    // clippings
    for (let i = 0; i < (grade === 'perfect' ? 26 : 14); i++) {
      this.fx.add({
        x: this.cx + rand(-this.R * 0.8, this.R * 0.8), y: y + rand(-4, 4),
        vx: rand(-60, 60), vy: rand(-80, 20), g: 600, drag: 0.6, life: rand(0.6, 1.1),
        size: rand(2, 4), shape: 'line', color: CLIENTS[this.client].hair, vr: rand(-10, 10), rot: rand(0, TAU),
      });
    }
    if (grade === 'perfect') this.fx.burst(this.cx + this.R, y, 14, { colors: [COLORS.gold, COLORS.goldHi], speed: 180, life: 0.5, size: 2.5 });
  }

  control(k, pressed) { if (pressed && (k === 'space' || k === 'up' || k === 'down')) this.cut(); }
  down() { this.cut(); }

  idle(dt) {
    this.time += dt;
    this.pos = 0.5 + Math.sin(this.time * 1.4) * 0.35;
  }

  update(dt) {
    this.time += dt;
    this.fx.update(dt);
    this.shake.update(dt);
    this.fadeShown = lerp(this.fadeShown, this.fadeLine, Math.min(1, dt * 10));
    if (this.slide > 0) this.slide = Math.max(0, this.slide - dt * 3.2);

    if (this.dead) {
      this.deadT += dt;
      if (this.deadT > 0.9) this.g.over();
      return;
    }
    if (this.phase === 'aim') {
      this.pos += this.dir * this.speed * dt;
      if (this.pos > 1) { this.pos = 2 - this.pos; this.dir = -1; }
      if (this.pos < 0) { this.pos = -this.pos; this.dir = 1; }
    } else if (this.phase === 'result') {
      this.resultT += dt;
      if (this.resultT >= RESULT_TIME) {
        if (this.cuts > 0 && this.cuts % 5 === 0 && this.lastClientAt !== this.cuts) {
          this.lastClientAt = this.cuts;
          this.client = (this.client + 1) % CLIENTS.length;
          this.slide = 1;
          this.fadeLine = 1; this.fadeShown = 1;
          this.dots = null;
        }
        this.newRound(false);
      }
    }
  }

  // Stubble dots in head-relative units, generated once: drawn where the hair "density"
  // at their height is above their threshold, which produces a real skin fade.
  ensureDots() {
    if (this.dots) return;
    this.dots = [];
    for (let i = 0; i < 900; i++) {
      this.dots.push({ u: rand(-1.05, 1.05), v: rand(-1.25, 0.75), r: rand(0.006, 0.014), th: Math.random() });
    }
  }

  hairPath(ctx) {
    // Everything behind the front hairline (forehead → temple → sideburn) can carry hair.
    const { cx, cy, R } = this;
    const b = this.bottom + R * 0.25;
    ctx.beginPath();
    ctx.moveTo(cx - R * 1.2, cy - R * 1.6);
    ctx.lineTo(cx + R * 1.2, cy - R * 1.6);
    ctx.lineTo(cx + R * 1.2, b);
    ctx.lineTo(cx + R * 0.56, b);
    ctx.lineTo(cx + R * 0.5, cy + R * 0.18);
    ctx.quadraticCurveTo(cx + R * 0.32, cy - R * 0.12, cx + R * 0.12, cy - R * 0.24);
    ctx.quadraticCurveTo(cx - R * 0.3, cy - R * 0.5, cx - R * 1.2, cy - R * 0.42);
    ctx.closePath();
  }

  density(y) {
    // 1 = full hair, 0 = skin. Solid above the fade line, then an eased fall-off to the bottom.
    const fadeY = this.yOf(this.fadeShown);
    const bottom = this.bottom + this.R * 0.18;
    if (y <= fadeY) return 1;
    if (y >= bottom) return 0;
    const t = (y - fadeY) / (bottom - fadeY);
    return Math.pow(1 - t, 1.6);
  }

  drawHead(ctx) {
    const { cx, cy, R } = this;
    const c = CLIENTS[this.client];
    this.ensureDots();
    ctx.save();

    // neck, cape
    ctx.fillStyle = c.skin;
    rr(ctx, cx - R * 0.36, cy + R * 0.62, R * 0.78, R * 0.8, R * 0.2); ctx.fill();
    ctx.fillStyle = 'rgba(0,0,0,0.18)';
    rr(ctx, cx - R * 0.36, cy + R * 0.62, R * 0.78, R * 0.18, R * 0.08); ctx.fill();
    ctx.fillStyle = '#2a1a1f';
    ctx.beginPath(); ctx.ellipse(cx, cy + R * 1.6, R * 1.3, R * 0.58, 0, Math.PI, 0); ctx.fill();
    ctx.strokeStyle = 'rgba(228,184,178,0.4)'; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.ellipse(cx, cy + R * 1.6, R * 0.5, R * 0.25, 0, Math.PI * 1.05, Math.PI * 1.95); ctx.stroke();

    // skull with soft light from the front-left
    const skin = ctx.createRadialGradient(cx - R * 0.45, cy - R * 0.2, R * 0.1, cx, cy, R * 1.1);
    skin.addColorStop(0, c.skin); skin.addColorStop(1, c.shade);
    ctx.fillStyle = skin;
    ctx.beginPath(); ctx.ellipse(cx, cy, R * 0.9, R, 0, 0, TAU); ctx.fill();
    // nose (3/4 view, facing left)
    ctx.beginPath();
    ctx.moveTo(cx - R * 0.8, cy - R * 0.02);
    ctx.quadraticCurveTo(cx - R * 1.02, cy + R * 0.28, cx - R * 0.86, cy + R * 0.36);
    ctx.quadraticCurveTo(cx - R * 0.8, cy + R * 0.38, cx - R * 0.74, cy + R * 0.34);
    ctx.fill();
    ctx.strokeStyle = 'rgba(0,0,0,0.18)'; ctx.lineWidth = Math.max(1, R * 0.02);
    ctx.stroke();

    // hair: solid block + stubble dots, clipped to skull ∪ crown volume and the hairline
    ctx.save();
    ctx.beginPath();
    ctx.ellipse(cx, cy, R * 0.9 + 1, R + 1, 0, 0, TAU);
    ctx.ellipse(cx + R * 0.05, cy - R * 0.62, R * 0.95, R * 0.5, 0, 0, TAU);
    ctx.clip();
    this.hairPath(ctx);
    ctx.clip();
    const fadeY = this.yOf(this.fadeShown);
    const g = ctx.createLinearGradient(0, fadeY, 0, this.bottom + R * 0.18);
    g.addColorStop(0, c.hair);
    g.addColorStop(0.35, c.hair + 'aa');
    g.addColorStop(0.75, c.hair + '33');
    g.addColorStop(1, c.hair + '00');
    ctx.fillStyle = c.hair;
    ctx.fillRect(cx - R * 1.2, cy - R * 1.3, R * 2.4, fadeY - (cy - R * 1.3));
    ctx.fillStyle = g;
    ctx.fillRect(cx - R * 1.2, fadeY, R * 2.4, this.bottom + R * 0.2 - fadeY);
    ctx.fillStyle = c.hair;
    for (const d of this.dots) {
      const y = cy + d.v * R;
      if (y < fadeY) continue;
      if (d.th < this.density(y)) {
        ctx.beginPath(); ctx.arc(cx + d.u * R, y, d.r * R * 2.2, 0, TAU); ctx.fill();
      }
    }
    // strands on the crown
    ctx.strokeStyle = 'rgba(255,255,255,0.07)'; ctx.lineWidth = Math.max(1, R * 0.018); ctx.lineCap = 'round';
    for (let i = 0; i < 18; i++) {
      const x = cx - R * 0.95 + (i * R * 1.9) / 17;
      const endY = Math.min(fadeY, cy - R * 0.15);
      ctx.beginPath(); ctx.moveTo(x - R * 0.1, cy - R * 1.1);
      ctx.quadraticCurveTo(x + R * 0.12, cy - R * 0.7, x - R * 0.04, endY); ctx.stroke();
    }
    ctx.restore();

    // ear sits on top of the fade
    ctx.fillStyle = c.skin;
    ctx.beginPath(); ctx.ellipse(cx + R * 0.82, cy + R * 0.16, R * 0.13, R * 0.21, -0.1, 0, TAU); ctx.fill();
    ctx.strokeStyle = 'rgba(0,0,0,0.2)'; ctx.lineWidth = Math.max(1, R * 0.025);
    ctx.beginPath(); ctx.ellipse(cx + R * 0.84, cy + R * 0.16, R * 0.06, R * 0.12, -0.1, -1.2, 2.4); ctx.stroke();

    // face: brow, eye, smile, facial hair
    ctx.lineCap = 'round';
    ctx.strokeStyle = c.hair; ctx.lineWidth = R * 0.055;
    ctx.beginPath(); ctx.moveTo(cx - R * 0.66, cy - R * 0.08); ctx.quadraticCurveTo(cx - R * 0.5, cy - R * 0.15, cx - R * 0.32, cy - R * 0.1); ctx.stroke();
    ctx.fillStyle = '#fff';
    ctx.beginPath(); ctx.ellipse(cx - R * 0.5, cy + R * 0.06, R * 0.1, R * 0.055, 0, 0, TAU); ctx.fill();
    ctx.fillStyle = '#3b2a22';
    ctx.beginPath(); ctx.arc(cx - R * 0.54, cy + R * 0.06, R * 0.04, 0, TAU); ctx.fill();
    ctx.strokeStyle = 'rgba(80,30,30,0.6)'; ctx.lineWidth = Math.max(1.2, R * 0.03);
    ctx.beginPath(); ctx.arc(cx - R * 0.6, cy + R * 0.47, R * 0.11, 0.35, Math.PI - 0.7); ctx.stroke();
    if (c.beard) {
      // moustache: two soft lobes under the nose; small chin beard
      ctx.fillStyle = c.beard;
      ctx.beginPath(); ctx.ellipse(cx - R * 0.72, cy + R * 0.43, R * 0.12, R * 0.045, 0.25, 0, TAU); ctx.fill();
      ctx.beginPath(); ctx.ellipse(cx - R * 0.53, cy + R * 0.44, R * 0.1, R * 0.04, -0.2, 0, TAU); ctx.fill();
      ctx.beginPath();
      ctx.moveTo(cx - R * 0.7, cy + R * 0.66);
      ctx.quadraticCurveTo(cx - R * 0.6, cy + R * 0.86, cx - R * 0.5, cy + R * 0.66);
      ctx.closePath(); ctx.fill();
    }
    ctx.restore();
  }

  render(ctx) {
    const { w, h, cx, R } = this;
    const [sx, sy] = this.shake.offset();
    ctx.save();
    ctx.translate(sx + this.slide * w * 0.9, sy);

    // mirror + spotlight backdrop
    const glow = ctx.createRadialGradient(cx, this.cy, R * 0.2, cx, this.cy, R * 2.2);
    glow.addColorStop(0, 'rgba(150,50,82,0.28)'); glow.addColorStop(1, 'rgba(150,50,82,0)');
    ctx.fillStyle = glow; ctx.fillRect(-w, 0, w * 3, h);
    ctx.strokeStyle = 'rgba(228,184,178,0.22)'; ctx.lineWidth = 3;
    rr(ctx, cx - R * 1.55, this.cy - R * 1.5, R * 3.1, R * 3.3, R * 1.4); ctx.stroke();

    this.drawHead(ctx);

    // target band
    const ty = this.yOf(this.target), hz = this.half * (this.bottom - this.top);
    const bx = cx - R * 1.05, bw = R * 2.25;
    const pulse = 0.5 + Math.sin(this.time * 6) * 0.15;
    ctx.fillStyle = `rgba(228,184,178,${0.16 + pulse * 0.1})`;
    ctx.fillRect(bx, ty - hz, bw, hz * 2);
    ctx.fillStyle = 'rgba(245,217,212,0.45)';
    ctx.fillRect(bx, ty - hz * 0.25, bw, hz * 0.5);
    ctx.setLineDash([6, 5]);
    ctx.strokeStyle = 'rgba(245,217,212,0.8)'; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(bx, ty - hz); ctx.lineTo(bx + bw, ty - hz); ctx.moveTo(bx, ty + hz); ctx.lineTo(bx + bw, ty + hz); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = COLORS.accentHi;
    ctx.beginPath(); ctx.moveTo(bx - 10, ty - 7); ctx.lineTo(bx - 1, ty); ctx.lineTo(bx - 10, ty + 7); ctx.fill();

    // clipper + cut line
    const y = this.yOf(this.pos);
    const clipX = cx + R * 1.12;
    ctx.strokeStyle = this.phase === 'result' ? 'rgba(245,217,212,0.9)' : 'rgba(245,240,225,0.35)';
    ctx.lineWidth = this.phase === 'result' ? 2.5 : 1.5;
    ctx.beginPath(); ctx.moveTo(cx - R * 1.05, y); ctx.lineTo(clipX - R * 0.1, y); ctx.stroke();
    if (this.phase === 'aim' && !this.dead) {
      for (let i = 1; i <= 3; i++) {
        ctx.globalAlpha = 0.12 / i;
        drawClipper(ctx, clipX, y - this.dir * i * this.speed * 9, R * 0.5, false);
      }
      ctx.globalAlpha = 1;
    }
    drawClipper(ctx, clipX + (this.phase === 'result' ? Math.sin(this.time * 80) * 1.5 : 0), y, R * 0.5, this.phase === 'result');

    this.fx.render(ctx);
    ctx.restore();

    if (this.streak > 1) text(ctx, `🔥 ${this.streak}`, w - 14, 18, { size: 16, color: COLORS.accentHi, align: 'right' });
    text(ctx, `✂ ${this.cuts}`, 14, 18, { size: 14, color: COLORS.muted, align: 'left', weight: 700 });
  }
}

export default {
  id: 'fade',
  emoji: '🦖',
  tile: ['#f0c24b', '#b2801c'],
  lives: LIVES,
  isNew: true,
  title: { uk: 'Фейдзілла', en: 'Fadezilla' },
  desc: {
    uk: 'Різниця між «норм» і «ідеально» — пів міліметра.',
    en: 'The gap between “fine” and “perfect” is half a millimetre.',
  },
  how: {
    uk: 'Машинка їздить по голові. Тапни, коли вона в підсвіченій смузі. В центр — «Ідеально» і бонус за серію. Три промахи — і клієнт іде.',
    en: 'The clipper slides along the head. Tap when it’s inside the highlighted band. Hit the centre for “Perfect” and a streak bonus. Three misses and the client walks.',
  },
  create: (g) => new FadeGame(g),
};
