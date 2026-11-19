import { COLORS, Particles, Shake, clamp, rr, text } from '../engine.js';

// Scoring (keep api/_games.py "blocks" limits in sync):
// cleared rows 1/2/3/4 → 10/30/50/80 points × level. Level = 1 + lines/10 (max 15).
const W = 10, H = 20;
const LINE_POINTS = [0, 10, 30, 50, 80];
const MAX_LEVEL = 15;
const LOCK_DELAY = 0.5;
const DAS = 0.16, ARR = 0.05;

const SHAPES = {
  I: { c: '#3fc7e8', m: [[0, 0, 0, 0], [1, 1, 1, 1], [0, 0, 0, 0], [0, 0, 0, 0]] },
  O: { c: COLORS.gold, m: [[1, 1], [1, 1]] },
  T: { c: '#a66cf0', m: [[0, 1, 0], [1, 1, 1], [0, 0, 0]] },
  S: { c: '#3dcc78', m: [[0, 1, 1], [1, 1, 0], [0, 0, 0]] },
  Z: { c: COLORS.red, m: [[1, 1, 0], [0, 1, 1], [0, 0, 0]] },
  J: { c: COLORS.blue, m: [[1, 0, 0], [1, 1, 1], [0, 0, 0]] },
  L: { c: '#f08c3a', m: [[0, 0, 1], [1, 1, 1], [0, 0, 0]] },
};
const KINDS = Object.keys(SHAPES);
const KICKS = [[0, 0], [-1, 0], [1, 0], [0, -1], [-2, 0], [2, 0], [-1, -1], [1, -1]];

const rotate = (m) => m[0].map((_, i) => m.map((row) => row[i]).reverse());

function shade(hex, amt) {
  const n = parseInt(hex.slice(1), 16);
  const r = clamp((n >> 16) + amt, 0, 255), g = clamp(((n >> 8) & 255) + amt, 0, 255), b = clamp((n & 255) + amt, 0, 255);
  return `rgb(${r},${g},${b})`;
}

function drawBlock(ctx, x, y, s, color, alpha = 1) {
  ctx.globalAlpha = alpha;
  const p = Math.max(1, s * 0.06);
  ctx.fillStyle = shade(color, -45);
  rr(ctx, x + p, y + p, s - p * 2, s - p * 2, s * 0.18); ctx.fill();
  ctx.fillStyle = color;
  rr(ctx, x + p, y + p, s - p * 2, s - p * 2 - s * 0.1, s * 0.18); ctx.fill();
  ctx.fillStyle = 'rgba(255,255,255,0.28)';
  rr(ctx, x + s * 0.2, y + s * 0.16, s * 0.6, s * 0.14, s * 0.07); ctx.fill();
  ctx.globalAlpha = 1;
}

class BlocksGame {
  constructor(g) {
    this.g = g;
    this.fx = new Particles();
    this.shake = new Shake();
    this.grid = Array.from({ length: H }, () => Array(W).fill(null));
    this.bag = [];
    this.next = [this.draw(), this.draw(), this.draw()];
    this.lines = 0;
    this.level = 1;
    this.fall = 0;
    this.lock = 0;
    this.clearing = null;
    this.dead = false;
    this.deadT = 0;
    this.keys = { left: false, right: false, down: false };
    this.das = 0;
    this.spawn();
  }

  draw() {
    if (!this.bag.length) {
      this.bag = KINDS.slice();
      for (let i = this.bag.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [this.bag[i], this.bag[j]] = [this.bag[j], this.bag[i]];
      }
    }
    return this.bag.pop();
  }

  spawn() {
    const kind = this.next.shift();
    this.next.push(this.draw());
    const m = SHAPES[kind].m.map((r) => r.slice());
    this.piece = { kind, m, x: Math.floor((W - m[0].length) / 2), y: kind === 'I' ? -1 : 0 };
    this.lock = 0;
    this.fall = 0;
    if (this.collides(this.piece.m, this.piece.x, this.piece.y)) this.gameOver();
  }

  collides(m, px, py) {
    for (let y = 0; y < m.length; y++) for (let x = 0; x < m[y].length; x++) {
      if (!m[y][x]) continue;
      const gx = px + x, gy = py + y;
      if (gx < 0 || gx >= W || gy >= H) return true;
      if (gy >= 0 && this.grid[gy][gx]) return true;
    }
    return false;
  }

  shift(dx) {
    if (this.busy()) return false;
    const p = this.piece;
    if (this.collides(p.m, p.x + dx, p.y)) return false;
    p.x += dx;
    this.lock = 0;
    this.g.sfx.play('move');
    return true;
  }

  rotatePiece() {
    if (this.busy() || this.piece.kind === 'O') return;
    const p = this.piece;
    const m = rotate(p.m);
    for (const [kx, ky] of KICKS) {
      if (!this.collides(m, p.x + kx, p.y + ky)) {
        p.m = m; p.x += kx; p.y += ky; this.lock = 0;
        this.g.sfx.play('rotate');
        return;
      }
    }
  }

  ghostY() {
    const p = this.piece;
    let y = p.y;
    while (!this.collides(p.m, p.x, y + 1)) y++;
    return y;
  }

  hardDrop() {
    if (this.busy()) return;
    const p = this.piece;
    const y = this.ghostY();
    const dist = y - p.y;
    p.y = y;
    if (dist > 0) this.shake.kick(Math.min(6, 2 + dist * 0.2), 0.15);
    this.g.sfx.play('drop');
    this.g.haptic('medium');
    this.lockPiece();
  }

  busy() { return this.dead || this.clearing || !this.piece; }

  lockPiece() {
    const p = this.piece;
    const color = SHAPES[p.kind].c;
    for (let y = 0; y < p.m.length; y++) for (let x = 0; x < p.m[y].length; x++) {
      if (!p.m[y][x]) continue;
      if (p.y + y < 0) { this.gameOver(); return; }
      this.grid[p.y + y][p.x + x] = color;
    }
    this.piece = null;
    const full = [];
    for (let y = 0; y < H; y++) if (this.grid[y].every(Boolean)) full.push(y);
    if (full.length) {
      this.clearing = { rows: full, t: 0 };
      const n = full.length;
      const pts = LINE_POINTS[n] * this.level;
      const midY = this.oy + (full.reduce((a, b) => a + b, 0) / n + 0.5) * this.cell;
      this.g.addScore(pts, this.ox + (W / 2) * this.cell, midY, '+' + pts, n === 4 ? COLORS.goldHi : undefined);
      if (n >= 2) this.g.float(this.ox + (W / 2) * this.cell, midY - 30, ['', '', 'Double!', 'Triple!', 'Прораб! ×4'][n], COLORS.gold, 18);
      this.g.sfx.play(n === 4 ? 'bonus' : 'clear');
      this.g.haptic(n === 4 ? 'heavy' : 'medium');
      for (const row of full) for (let x = 0; x < W; x++) {
        this.fx.burst(this.ox + (x + 0.5) * this.cell, this.oy + (row + 0.5) * this.cell, 3,
          { color: this.grid[row][x], speed: 180, life: 0.6, size: 3, shape: 'square', g: 500 });
      }
    } else {
      this.spawn();
    }
  }

  finishClear() {
    const rows = this.clearing.rows;
    this.grid = this.grid.filter((_, y) => !rows.includes(y));
    while (this.grid.length < H) this.grid.unshift(Array(W).fill(null));
    this.lines += rows.length;
    const lvl = Math.min(MAX_LEVEL, 1 + Math.floor(this.lines / 10));
    if (lvl > this.level) {
      this.level = lvl;
      this.g.float(this.ox + (W / 2) * this.cell, this.oy + H * this.cell * 0.4, this.g.t('level', { n: lvl }), COLORS.goldHi, 24);
    }
    this.clearing = null;
    this.spawn();
  }

  gameOver() {
    if (this.dead) return;
    this.dead = true;
    this.piece = null;
    this.g.sfx.play('hit');
    this.g.haptic('heavy');
    this.shake.kick(8, 0.35);
  }

  gravity() { return Math.max(0.045, 0.8 - (this.level - 1) * 0.055); }

  control(k, pressed) {
    if (k === 'left' || k === 'right' || k === 'down') {
      this.keys[k] = pressed;
      if (pressed && k !== 'down') { this.shift(k === 'left' ? -1 : 1); this.das = -DAS; }
      return;
    }
    if (!pressed) return;
    if (k === 'up') this.rotatePiece();
    if (k === 'space') this.hardDrop();
  }

  // touch: tap = rotate, drag sideways = move, flick down = hard drop, drag down = soft drop
  down(p) { this.touch = { x: p.x, y: p.y, t: p.t, shifted: 0, moved: false, soft: false, dropped: false }; }
  move(p) {
    const tc = this.touch;
    if (!tc || tc.dropped) return;
    const dx = p.x - tc.x, dy = p.y - tc.y;
    const want = Math.trunc(dx / (this.cell * 0.9));
    while (tc.shifted < want && this.shift(1)) { tc.shifted++; tc.moved = true; }
    while (tc.shifted > want && this.shift(-1)) { tc.shifted--; tc.moved = true; }
    if (dy > this.cell * 1.2 && Math.abs(dy) > Math.abs(dx)) {
      tc.moved = true;
      const speed = dy / Math.max(1, p.t - tc.t);
      if (speed > 1.1 && p.t - tc.t < 220) { tc.dropped = true; this.keys.down = false; this.hardDrop(); }
      else { tc.soft = true; this.keys.down = true; }
    }
  }
  up(p) {
    const tc = this.touch;
    this.touch = null;
    if (!tc) return;
    if (tc.soft) this.keys.down = false;
    if (!tc.moved && Math.hypot(p.x - tc.x, p.y - tc.y) < 12 && p.t - tc.t < 350) this.rotatePiece();
  }

  update(dt) {
    this.fx.update(dt);
    this.shake.update(dt);
    if (this.dead) {
      this.deadT += dt;
      if (this.deadT > 1.0) this.g.over();
      return;
    }
    if (this.clearing) {
      this.clearing.t += dt;
      if (this.clearing.t > 0.28) this.finishClear();
      return;
    }
    if (!this.piece) return;

    // auto-repeat for held left/right
    const dir = this.keys.left && !this.keys.right ? -1 : this.keys.right && !this.keys.left ? 1 : 0;
    if (dir) {
      this.das += dt;
      while (this.das >= ARR) { this.das -= ARR; if (!this.shift(dir)) break; }
    }

    const p = this.piece;
    const speed = this.keys.down ? Math.min(this.gravity(), 0.035) : this.gravity();
    const grounded = this.collides(p.m, p.x, p.y + 1);
    if (grounded) {
      this.lock += dt;
      if (this.lock >= LOCK_DELAY) { this.g.sfx.play('drop'); this.lockPiece(); }
    } else {
      this.fall += dt;
      while (this.fall >= speed) {
        this.fall -= speed;
        if (this.collides(p.m, p.x, p.y + 1)) break;
        p.y++;
      }
    }
  }

  // Fill the stage: the well takes all the height it can; the next-piece panel gets what's left
  // on the right (at least MIN_PANEL px), so phones of every size get the biggest possible cells.
  resize(w, h) {
    const MIN_PANEL = 58, GAP = 8, PAD = 6;
    this.cell = Math.max(8, Math.floor(Math.min((h - PAD * 2) / H, (w - PAD * 2 - GAP - MIN_PANEL) / W)));
    const boardW = this.cell * W;
    const panel = Math.min(130, Math.max(MIN_PANEL, w - PAD * 2 - GAP - boardW));
    this.ox = Math.floor((w - boardW - GAP - panel) / 2);
    this.oy = Math.floor((h - this.cell * H) / 2);
    this.px = this.ox + boardW + GAP;
    this.panel = panel;
  }

  render(ctx) {
    const { cell: s, ox, oy } = this;
    const [sx, sy] = this.shake.offset();
    ctx.save();
    ctx.translate(sx, sy);

    // well
    ctx.fillStyle = '#150e11';
    rr(ctx, ox - 3, oy - 3, s * W + 6, s * H + 6, 10); ctx.fill();
    ctx.strokeStyle = 'rgba(228,184,178,0.25)'; ctx.lineWidth = 1;
    rr(ctx, ox - 3.5, oy - 3.5, s * W + 7, s * H + 7, 10); ctx.stroke();
    ctx.fillStyle = 'rgba(255,255,255,0.025)';
    for (let x = 1; x < W; x++) ctx.fillRect(ox + x * s, oy, 1, s * H);

    const flash = this.clearing && Math.floor(this.clearing.t * 24) % 2 === 0;
    for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
      const c = this.grid[y][x];
      if (!c) continue;
      const clearingRow = this.clearing && this.clearing.rows.includes(y);
      if (clearingRow) {
        if (flash) { ctx.fillStyle = '#fff'; rr(ctx, ox + x * s + 1, oy + y * s + 1, s - 2, s - 2, s * 0.18); ctx.fill(); }
        continue;
      }
      drawBlock(ctx, ox + x * s, oy + y * s, s, this.dead ? '#4a4b66' : c);
    }

    if (this.piece) {
      const p = this.piece, color = SHAPES[p.kind].c, gy = this.ghostY();
      for (let y = 0; y < p.m.length; y++) for (let x = 0; x < p.m[y].length; x++) {
        if (!p.m[y][x]) continue;
        if (gy + y >= 0) {
          ctx.strokeStyle = color; ctx.globalAlpha = 0.45; ctx.lineWidth = 1.5;
          rr(ctx, ox + (p.x + x) * s + 2, oy + (gy + y) * s + 2, s - 4, s - 4, s * 0.18); ctx.stroke();
          ctx.globalAlpha = 1;
        }
        if (p.y + y >= 0) drawBlock(ctx, ox + (p.x + x) * s, oy + (p.y + y) * s, s, color);
      }
    }

    // side panel: next pieces, level, lines
    const px = this.px, pw = this.panel;
    const narrow = pw < 80;
    text(ctx, narrow ? 'Lv ' + this.level : this.g.t('level', { n: this.level }), px + pw / 2, oy + 10, { size: narrow ? 12 : 13, color: COLORS.accent, weight: 800 });
    text(ctx, narrow ? '≡ ' + this.lines : this.g.t('lines', { n: this.lines }), px + pw / 2, oy + 30, { size: 12, color: COLORS.muted, weight: 700 });
    const ns = Math.min(s * 0.75, (pw - 12) / 4);
    let ny = oy + 56;
    this.next.forEach((kind, i) => {
      const m = SHAPES[kind].m;
      const rows = m.filter((r) => r.some(Boolean));
      const top = m.indexOf(rows[0]);
      const cols = m[0].length;
      const scale = i === 0 ? 1 : 0.75;
      const bs = ns * scale;
      ctx.fillStyle = i === 0 ? 'rgba(228,184,178,0.08)' : 'rgba(255,255,255,0.03)';
      rr(ctx, px, ny - 6, pw, bs * 2 + 16, 10); ctx.fill();
      const bx = px + (pw - cols * bs) / 2;
      for (let y = 0; y < rows.length; y++) for (let x = 0; x < cols; x++) {
        if (m[top + y][x]) drawBlock(ctx, bx + x * bs, ny + y * bs + (2 - rows.length) * bs / 2, bs, SHAPES[kind].c, i === 0 ? 1 : 0.6);
      }
      ny += bs * 2 + 26;
    });

    this.fx.render(ctx);
    ctx.restore();
  }
}

export default {
  id: 'blocks',
  emoji: '👷',
  tile: ['#4a86ff', '#1c44c6'],
  controls: 'blocks',
  title: { uk: 'Прораб', en: 'Foreman' },
  desc: {
    uk: 'Всі хочуть довгу паличку. Не всім вона дістається.',
    en: 'Everyone wants the long bar. Not everyone gets it.',
  },
  how: {
    uk: 'Тап — повернути, тягни вбік — посунути, різкий свайп вниз — скинути. Збирай повні ряди: чотири разом дають найбільше.',
    en: 'Tap to rotate, drag sideways to move, flick down to drop. Fill whole rows — four at once scores the most.',
  },
  create: (g) => new BlocksGame(g),
};

