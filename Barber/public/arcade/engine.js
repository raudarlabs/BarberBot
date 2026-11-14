// Shared game engine: crisp canvas, fixed-step loop, pointer input, synth sounds,
// particles, floating texts and a few drawing helpers.

export const TAU = Math.PI * 2;
export const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
export const lerp = (a, b, t) => a + (b - a) * t;
export const rand = (a, b) => a + Math.random() * (b - a);
export const randInt = (a, b) => Math.floor(rand(a, b + 1));
export const pick = (arr) => arr[Math.floor(Math.random() * arr.length)];
export const FONT = '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif';

export const COLORS = {
  bg: '#120d0f', bg2: '#1a1214', card: '#2b1b20', gold: '#d4af37', goldHi: '#f3d77a', goldLo: '#a8862a',
  cream: '#f3ebe8', muted: '#a8939a', red: '#e0433a', blue: '#2f6fd6', white: '#f4f1ea', green: '#2ecc71',
  accent: '#e4b8b2', accentHi: '#f5d9d4', accentLo: '#b58580', wine: '#7a2b43', wineHi: '#a8435f',
};

/** Rounded-rect path (works on browsers without ctx.roundRect). */
export function rr(ctx, x, y, w, h, r) {
  r = Math.max(0, Math.min(r, w / 2, h / 2));
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

/** Canvas that fills its parent and renders at device pixel ratio. */
export class Stage {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.w = 0; this.h = 0; this.dpr = 1;
    this.listeners = [];
    const fit = () => this.fit();
    if (window.ResizeObserver) {
      this.ro = new ResizeObserver(fit);
      this.ro.observe(canvas.parentElement);
    }
    window.addEventListener('resize', fit);
    this.fit();
  }
  fit() {
    const r = this.canvas.parentElement.getBoundingClientRect();
    const w = Math.max(1, Math.floor(r.width));
    const h = Math.max(1, Math.floor(r.height));
    const dpr = Math.min(window.devicePixelRatio || 1, 3);
    if (w === this.w && h === this.h && dpr === this.dpr) return;
    this.w = w; this.h = h; this.dpr = dpr;
    this.canvas.width = Math.round(w * dpr);
    this.canvas.height = Math.round(h * dpr);
    this.canvas.style.width = w + 'px';
    this.canvas.style.height = h + 'px';
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.listeners.forEach((f) => f(w, h));
  }
  onResize(f) { this.listeners.push(f); }
  clear(color) {
    this.ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    this.ctx.fillStyle = color || COLORS.bg2;
    this.ctx.fillRect(0, 0, this.w, this.h);
  }
}

/** requestAnimationFrame loop with a fixed simulation step. */
export class Loop {
  constructor(update, render, step = 1 / 120) {
    this.update = update; this.render = render; this.step = step;
    this.running = false; this.raf = 0; this.acc = 0; this.last = 0;
    this.frame = this.frame.bind(this);
  }
  start() {
    if (this.running) return;
    this.running = true; this.acc = 0; this.last = performance.now();
    this.raf = requestAnimationFrame(this.frame);
  }
  stop() { this.running = false; cancelAnimationFrame(this.raf); }
  frame(now) {
    if (!this.running) return;
    let dt = (now - this.last) / 1000;
    this.last = now;
    if (dt > 0.1) dt = 0.1; // tab switch / hiccup: don't fast-forward the game
    this.acc += dt;
    while (this.acc >= this.step) { this.update(this.step); this.acc -= this.step; }
    this.render();
    this.raf = requestAnimationFrame(this.frame);
  }
}

/** Unified pointer input on an element; positions are in CSS pixels. */
export function bindPointer(el, handlers) {
  const pos = (e) => {
    const r = el.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top, id: e.pointerId, t: performance.now() };
  };
  const down = (e) => {
    e.preventDefault();
    try { el.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
    handlers.down && handlers.down(pos(e), e);
  };
  const move = (e) => {
    if (!handlers.move) return;
    const list = e.getCoalescedEvents ? e.getCoalescedEvents() : null;
    if (list && list.length > 1) list.forEach((ce) => handlers.move(pos(ce), e));
    else handlers.move(pos(e), e);
  };
  const up = (e) => handlers.up && handlers.up(pos(e), e);
  el.addEventListener('pointerdown', down);
  el.addEventListener('pointermove', move);
  el.addEventListener('pointerup', up);
  el.addEventListener('pointercancel', up);
  return () => {
    el.removeEventListener('pointerdown', down);
    el.removeEventListener('pointermove', move);
    el.removeEventListener('pointerup', up);
    el.removeEventListener('pointercancel', up);
  };
}

/** Tracks a swipe gesture and reports each direction change once it travels `min` px. */
export class Swipe {
  constructor(onDir, min = 22) { this.onDir = onDir; this.min = min; this.start = null; this.moved = false; }
  down(p) { this.start = p; this.moved = false; }
  move(p) {
    if (!this.start) return;
    const dx = p.x - this.start.x, dy = p.y - this.start.y;
    if (Math.max(Math.abs(dx), Math.abs(dy)) < this.min) return;
    const dir = Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? 'right' : 'left') : (dy > 0 ? 'down' : 'up');
    this.onDir(dir, p);
    this.start = p; this.moved = true;
  }
  up() { const tapped = this.start && !this.moved; this.start = null; return tapped; }
}

// ------------------------------------------------------------------ sound

class Sfx {
  constructor() { this.ctx = null; this.enabled = true; }
  unlock() {
    try {
      if (!this.ctx) {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return;
        this.ctx = new AC();
      }
      if (this.ctx.state === 'suspended') this.ctx.resume();
    } catch (e) { /* no audio */ }
  }
  tone(freq, dur, { type = 'square', vol = 0.06, to = null, delay = 0 } = {}) {
    const c = this.ctx, t = c.currentTime + delay;
    const o = c.createOscillator(), g = c.createGain();
    o.type = type;
    o.frequency.setValueAtTime(freq, t);
    if (to) o.frequency.exponentialRampToValueAtTime(to, t + dur);
    g.gain.setValueAtTime(vol, t);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    o.connect(g).connect(c.destination);
    o.start(t); o.stop(t + dur + 0.02);
  }
  noise(dur, { vol = 0.12, freq = 1200, q = 0.8, delay = 0, to = null } = {}) {
    const c = this.ctx, t = c.currentTime + delay;
    const len = Math.max(1, Math.floor(c.sampleRate * dur));
    const buf = c.createBuffer(1, len, c.sampleRate);
    const d = buf.getChannelData(0);
    for (let i = 0; i < len; i++) d[i] = Math.random() * 2 - 1;
    const src = c.createBufferSource(); src.buffer = buf;
    const f = c.createBiquadFilter(); f.type = 'bandpass'; f.frequency.setValueAtTime(freq, t); f.Q.value = q;
    if (to) f.frequency.exponentialRampToValueAtTime(to, t + dur);
    const g = c.createGain(); g.gain.setValueAtTime(vol, t); g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    src.connect(f).connect(g).connect(c.destination);
    src.start(t);
  }
  play(name) {
    if (!this.enabled || !this.ctx) return;
    try {
      switch (name) {
        case 'tap': this.tone(520, 0.06, { type: 'triangle', vol: 0.05 }); break;
        case 'flap': this.noise(0.12, { vol: 0.08, freq: 900, to: 2400 }); break;
        case 'point': this.tone(880, 0.07, { vol: 0.04 }); this.tone(1320, 0.09, { vol: 0.04, delay: 0.06 }); break;
        case 'bonus': [660, 880, 1100, 1320].forEach((f, i) => this.tone(f, 0.08, { vol: 0.04, delay: i * 0.05 })); break;
        case 'perfect': [784, 988, 1175, 1568].forEach((f, i) => this.tone(f, 0.1, { type: 'triangle', vol: 0.06, delay: i * 0.045 })); break;
        case 'good': this.tone(660, 0.1, { type: 'triangle', vol: 0.06 }); break;
        case 'slice': this.noise(0.14, { vol: 0.1, freq: 2600, to: 900, q: 2 }); break;
        case 'buzz': this.tone(110, 0.25, { type: 'sawtooth', vol: 0.04, to: 90 }); this.noise(0.25, { vol: 0.05, freq: 300 }); break;
        case 'hit': this.tone(180, 0.25, { type: 'sawtooth', vol: 0.07, to: 60 }); this.noise(0.2, { vol: 0.12, freq: 400 }); break;
        case 'drop': this.tone(140, 0.08, { type: 'triangle', vol: 0.08, to: 90 }); break;
        case 'move': this.tone(300, 0.03, { type: 'triangle', vol: 0.03 }); break;
        case 'rotate': this.tone(420, 0.05, { type: 'triangle', vol: 0.04, to: 620 }); break;
        case 'clear': [523, 659, 784, 1047].forEach((f, i) => this.tone(f, 0.12, { vol: 0.05, delay: i * 0.06 })); break;
        case 'over': [440, 349, 262, 196].forEach((f, i) => this.tone(f, 0.18, { type: 'triangle', vol: 0.07, delay: i * 0.12 })); break;
        case 'start': [392, 523, 659].forEach((f, i) => this.tone(f, 0.09, { type: 'triangle', vol: 0.05, delay: i * 0.06 })); break;
        default: break;
      }
    } catch (e) { /* ignore */ }
  }
}
export const sfx = new Sfx();

// ------------------------------------------------------------------ effects

export class Particles {
  constructor() { this.items = []; }
  add(p) {
    this.items.push(Object.assign({ vx: 0, vy: 0, g: 0, drag: 0, life: 0.6, size: 3, color: '#fff', shape: 'circle', rot: 0, vr: 0 }, p, { max: p.life || 0.6 }));
  }
  burst(x, y, n, opts = {}) {
    for (let i = 0; i < n; i++) {
      const a = rand(0, TAU), s = rand(opts.speedMin || 60, opts.speed || 220);
      this.add(Object.assign({}, opts, {
        x, y, vx: Math.cos(a) * s, vy: Math.sin(a) * s - (opts.lift || 0),
        life: rand(0.4, opts.life || 0.9), size: rand(opts.sizeMin || 1.5, opts.size || 4),
        color: Array.isArray(opts.colors) ? pick(opts.colors) : (opts.color || '#fff'),
        rot: rand(0, TAU), vr: rand(-8, 8),
      }));
    }
  }
  update(dt) {
    for (const p of this.items) {
      p.life -= dt;
      p.vy += p.g * dt;
      if (p.drag) { const k = Math.max(0, 1 - p.drag * dt); p.vx *= k; p.vy *= k; }
      p.x += p.vx * dt; p.y += p.vy * dt; p.rot += p.vr * dt;
    }
    this.items = this.items.filter((p) => p.life > 0);
  }
  render(ctx) {
    for (const p of this.items) {
      ctx.globalAlpha = clamp(p.life / p.max, 0, 1);
      ctx.fillStyle = p.color;
      if (p.shape === 'line') {
        ctx.save(); ctx.translate(p.x, p.y); ctx.rotate(p.rot);
        ctx.fillRect(-p.size * 1.6, -0.75, p.size * 3.2, 1.5);
        ctx.restore();
      } else if (p.shape === 'square') {
        ctx.save(); ctx.translate(p.x, p.y); ctx.rotate(p.rot);
        ctx.fillRect(-p.size / 2, -p.size / 2, p.size, p.size);
        ctx.restore();
      } else {
        ctx.beginPath(); ctx.arc(p.x, p.y, p.size, 0, TAU); ctx.fill();
      }
    }
    ctx.globalAlpha = 1;
  }
  clear() { this.items = []; }
}

export class Floaters {
  constructor() { this.items = []; }
  add(x, y, text, color = COLORS.accentHi, size = 22) {
    this.items.push({ x, y, text, color, size, life: 0.9, max: 0.9 });
  }
  update(dt) {
    for (const f of this.items) { f.life -= dt; f.y -= 46 * dt; }
    this.items = this.items.filter((f) => f.life > 0);
  }
  render(ctx) {
    ctx.save();
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.lineJoin = 'round';
    for (const f of this.items) {
      const age = f.max - f.life;
      const s = age < 0.12 ? 0.6 + (age / 0.12) * 0.4 : 1;
      ctx.globalAlpha = clamp(f.life / 0.35, 0, 1);
      ctx.font = `900 ${Math.round(f.size * s)}px ${FONT}`;
      ctx.lineWidth = 5; ctx.strokeStyle = 'rgba(14,9,11,0.85)';
      ctx.strokeText(f.text, f.x, f.y);
      ctx.fillStyle = f.color;
      ctx.fillText(f.text, f.x, f.y);
    }
    ctx.restore();
  }
  clear() { this.items = []; }
}

/** Screen shake helper: call kick(), then apply() before drawing, restore after. */
export class Shake {
  constructor() { this.t = 0; this.mag = 0; }
  kick(mag = 6, t = 0.25) { this.mag = mag; this.t = t; }
  update(dt) { this.t = Math.max(0, this.t - dt); }
  offset() {
    if (this.t <= 0) return [0, 0];
    const m = this.mag * (this.t / 0.25);
    return [rand(-m, m), rand(-m, m)];
  }
}

// ------------------------------------------------------------------ drawing

/** Barber pole with moving diagonal stripes and a cylinder shading. */
export function drawPole(ctx, x, y, w, h, offset = 0) {
  ctx.save();
  rr(ctx, x, y, w, h, Math.min(6, w / 5));
  ctx.clip();
  ctx.fillStyle = COLORS.white;
  ctx.fillRect(x, y, w, h);
  const band = Math.max(8, w * 0.42);
  const slant = w * 0.75;
  const colors = [COLORS.red, COLORS.white, COLORS.blue, COLORS.white];
  const k0 = Math.floor((y - slant - offset) / band) - 1;
  for (let k = k0; k * band + offset < y + h + slant; k++) {
    const yy = k * band + offset;
    ctx.fillStyle = colors[((k % 4) + 4) % 4];
    ctx.beginPath();
    ctx.moveTo(x, yy + slant);
    ctx.lineTo(x + w, yy);
    ctx.lineTo(x + w, yy + band);
    ctx.lineTo(x, yy + slant + band);
    ctx.closePath();
    ctx.fill();
  }
  const g = ctx.createLinearGradient(x, 0, x + w, 0);
  g.addColorStop(0, 'rgba(0,0,0,0.38)');
  g.addColorStop(0.3, 'rgba(255,255,255,0.22)');
  g.addColorStop(0.55, 'rgba(0,0,0,0)');
  g.addColorStop(1, 'rgba(0,0,0,0.5)');
  ctx.fillStyle = g;
  ctx.fillRect(x, y, w, h);
  ctx.restore();
}

/** Chrome / gold cap for pole ends. */
export function drawCap(ctx, x, y, w, h) {
  const g = ctx.createLinearGradient(x, 0, x + w, 0);
  g.addColorStop(0, COLORS.goldLo);
  g.addColorStop(0.35, COLORS.goldHi);
  g.addColorStop(0.7, COLORS.gold);
  g.addColorStop(1, '#6d5419');
  ctx.fillStyle = g;
  rr(ctx, x, y, w, h, Math.min(5, h / 2));
  ctx.fill();
}

export function text(ctx, str, x, y, { size = 16, weight = 800, color = COLORS.cream, align = 'center', base = 'middle', alpha = 1 } = {}) {
  ctx.save();
  ctx.globalAlpha = alpha;
  ctx.font = `${weight} ${size}px ${FONT}`;
  ctx.textAlign = align; ctx.textBaseline = base;
  ctx.fillStyle = color;
  ctx.fillText(str, x, y);
  ctx.restore();
}

/** Distance from point (px,py) to segment (ax,ay)-(bx,by). */
export function segDist(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay;
  const len2 = dx * dx + dy * dy;
  let t = len2 ? ((px - ax) * dx + (py - ay) * dy) / len2 : 0;
  t = clamp(t, 0, 1);
  const cx = ax + dx * t, cy = ay + dy * t;
  return Math.hypot(px - cx, py - cy);
}

export function circleRect(cx, cy, r, x, y, w, h) {
  const nx = clamp(cx, x, x + w), ny = clamp(cy, y, y + h);
  return (cx - nx) ** 2 + (cy - ny) ** 2 < r * r;
}
