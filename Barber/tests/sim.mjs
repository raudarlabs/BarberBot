// Headless game simulation.
//
//   node tests/sim.mjs
//
// 1. Plays every game with a strong bot (perfect timing, or elite-human speed for the puzzle)
//    and checks the score rate stays under the server's anti-cheat limit in api/_games.py —
//    otherwise the best real players would get "Score not counted".
// 2. Fuzzes every game with random input on several screen sizes and checks nothing throws,
//    no NaN sneaks into the score, and rendering works.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

import dryer from '../public/arcade/games/dryer.js';
import fade from '../public/arcade/games/fade.js';
import beard from '../public/arcade/games/beard.js';
import snake from '../public/arcade/games/snake.js';
import blocks from '../public/arcade/games/blocks.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const gamesPy = readFileSync(path.join(here, '../api/_games.py'), 'utf8');
const LIMITS = {};
for (const m of gamesPy.matchAll(/"(\w+)": \{"max_per_sec": (\d+), "max_score": (\d+)\}/g)) {
  LIMITS[m[1]] = { perSec: +m[2], max: +m[3] };
}
const GRACE = +(gamesPy.match(/GRACE_SECONDS = (\d+)/) || [0, 3])[1];

// ---------------------------------------------------------------- stubs
const noop = () => {};
const gradient = { addColorStop: noop };
const ctx = new Proxy({}, {
  get(t, k) {
    if (k === 'createLinearGradient' || k === 'createRadialGradient') return () => gradient;
    if (k === 'measureText') return () => ({ width: 10 });
    if (k in t) return t[k];
    return noop;
  },
  set(t, k, v) { t[k] = v; return true; },
});

let clock = 0; // simulated ms, fed to pointer events
globalThis.performance = { now: () => clock };

function makeApi(w, h) {
  const st = { score: 0, over: false, lives: null, playing: true };
  const api = {
    w, h,
    addScore(n) {
      if (!st.playing || !n) return;
      if (!Number.isFinite(n)) throw new Error('non-finite score ' + n);
      st.score += n;
    },
    float: noop,
    setLives(n) { st.lives = n; },
    over() { st.over = true; st.playing = false; },
    haptic: noop,
    sfx: { play: noop },
    t: (k, v) => k + (v ? JSON.stringify(v) : ''),
    L: (o) => o.uk,
  };
  return { st, api };
}

const DT = 1 / 120;

function run(mod, { w = 360, h = 600, seconds = 300, bot, renderEvery = 6 }) {
  const { st, api } = makeApi(w, h);
  const inst = mod.create(api);
  inst.resize && inst.resize(w, h);
  for (let i = 0; i < 120; i++) inst.idle && inst.idle(DT); // start screen
  inst.start && inst.start();
  let t = 0, steps = 0;
  while (t < seconds && !st.over) {
    if (bot) bot(inst, t, api);
    inst.update(DT);
    clock += DT * 1000;
    t += DT;
    if (++steps % renderEvery === 0) inst.render(ctx);
  }
  return { score: st.score, seconds: t, over: st.over };
}

// ---------------------------------------------------------------- bots

const dryerBot = () => (g) => {
  const pw = 62 * g.u;
  const next = g.poles.find((p) => p.x + pw > g.x - 18 * g.u);
  // aim for the lower part of the gap: a flap peaks ~80u higher
  let target = next ? next.gy + next.gap * 0.22 : g.h * 0.5;
  const comb = g.combs.find((c) => !c.got && c.x > g.x && c.x - g.x < 90 * g.u && Math.abs(c.y - target) < 40 * g.u);
  if (comb) target = comb.y + 20 * g.u;
  if (g.y > target && g.vy >= 0) g.control('space', true);
};

const fadeBot = () => (g) => {
  if (g.phase !== 'aim') return;
  const step = g.speed * DT;
  if (Math.abs(g.pos - g.target) <= step * 0.6) g.control('space', true);
};

const beardBot = () => function (g) {
  if (g.pointer) return;
  const live = g.items.filter((it) => !it.cut && it.delay <= 0 && it.y < g.h - it.r && it.y > it.r);
  const tufts = live.filter((it) => it.kind !== 'bottle');
  if (!tufts.length) return;
  // wait until 2+ tufts are up (combo) unless one is about to fall
  const falling = tufts.some((it) => it.vy > 0 && it.y > g.h * 0.75);
  if (tufts.length < 2 && !falling) return;
  const bottles = live.filter((it) => it.kind === 'bottle');
  const path = tufts.sort((a, b) => a.x - b.x).filter((tf) => !bottles.some((b) => Math.hypot(b.x - tf.x, b.y - tf.y) < b.r + tf.r + 10));
  if (!path.length) return;
  let p = { x: path[0].x - path[0].r * 1.2, y: path[0].y, id: 1, t: clock };
  g.down(p);
  for (const tf of path) {
    for (const dx of [-0.5, 0.5, 1.2]) {
      clock += 4;
      p = { x: tf.x + tf.r * dx, y: tf.y, id: 1, t: clock };
      g.move(p);
    }
  }
  g.up(p);
};

const SNAKE_DIRS = { up: [0, -1], down: [0, 1], left: [-1, 0], right: [1, 0] };
const snakeBot = () => function (g) {
  if (g.dead || g.queue.length) return;
  const head = g.snake[0];
  const target = g.bonus || g.food;
  if (!target) return;
  const blocked = new Set(g.snake.slice(0, -1).map((s) => s.x + ',' + s.y));
  const rows = g.rows, cols = 17;
  // BFS from head to target
  const prev = new Map([[head.x + ',' + head.y, null]]);
  const q = [head];
  while (q.length) {
    const c = q.shift();
    if (c.x === target.x && c.y === target.y) break;
    for (const [name, [dx, dy]] of Object.entries(SNAKE_DIRS)) {
      const n = { x: c.x + dx, y: c.y + dy };
      const k = n.x + ',' + n.y;
      if (n.x < 0 || n.y < 0 || n.x >= cols || n.y >= rows || blocked.has(k) || prev.has(k)) continue;
      prev.set(k, { c, name });
      q.push(n);
    }
  }
  let k = target.x + ',' + target.y, first = null;
  while (prev.get(k)) { first = prev.get(k).name; const c = prev.get(k).c; k = c.x + ',' + c.y; }
  if (!first) {
    first = Object.keys(SNAKE_DIRS).find((name) => {
      const [dx, dy] = SNAKE_DIRS[name];
      const n = { x: head.x + dx, y: head.y + dy };
      return n.x >= 0 && n.y >= 0 && n.x < cols && n.y < rows && !blocked.has(n.x + ',' + n.y);
    });
  }
  if (first) g.turn(first);
};

// Puzzle bot: picks the best placement with a classic heuristic, at an elite human pace.
const rotateM = (m) => m[0].map((_, i) => m.map((row) => row[i]).reverse());
function evalGrid(grid) {
  const H = grid.length, W = grid[0].length;
  let lines = 0;
  const rows = grid.filter((r) => { if (r.every(Boolean)) { lines++; return false; } return true; });
  while (rows.length < H) rows.unshift(Array(W).fill(null));
  const heights = [];
  let holes = 0;
  for (let x = 0; x < W; x++) {
    let y = 0;
    while (y < H && !rows[y][x]) y++;
    heights.push(H - y);
    for (let yy = y + 1; yy < H; yy++) if (!rows[yy][x]) holes++;
  }
  let bump = 0;
  for (let x = 1; x < W; x++) bump += Math.abs(heights[x] - heights[x - 1]);
  const agg = heights.reduce((a, b) => a + b, 0);
  return -0.51 * agg + 0.76 * lines - 0.36 * holes - 0.18 * bump;
}
const blocksBot = (pps) => () => {
  let nextAt = 0;
  return (g, t) => {
    if (!g.piece || g.clearing || g.dead || t < nextAt) return;
    nextAt = t + 1 / pps;
    let best = null;
    let m = g.piece.m;
    for (let r = 0; r < 4; r++, m = rotateM(m)) {
      for (let x = -2; x < 10; x++) {
        if (g.collides(m, x, g.piece.y)) continue;
        let y = g.piece.y;
        while (!g.collides(m, x, y + 1)) y++;
        const grid = g.grid.map((row) => row.slice());
        let ok = true;
        for (let yy = 0; yy < m.length; yy++) for (let xx = 0; xx < m[yy].length; xx++) {
          if (!m[yy][xx]) continue;
          if (y + yy < 0) ok = false; else grid[y + yy][x + xx] = '#';
        }
        if (!ok) continue;
        const score = evalGrid(grid);
        if (!best || score > best.score) best = { score, m, x };
      }
    }
    if (best) { g.piece.m = best.m; g.piece.x = best.x; }
    g.control('space', true);
  };
};

// ---------------------------------------------------------------- run

let failed = 0;
const report = (name, ok, msg) => { console.log(`${ok ? 'PASS' : 'FAIL'}  ${name.padEnd(34)} ${msg}`); if (!ok) failed++; };

const BOTS = [
  ['dryer', dryer, dryerBot, 600],
  ['fade', fade, fadeBot, 600],
  ['beard', beard, beardBot, 600],
  ['snake', snake, snakeBot, 900],
  ['blocks', blocks, blocksBot(3), 900],
  ['blocks-5pps', blocks, blocksBot(5), 900],
];

console.log('— Perfect-bot runs vs anti-cheat limits (api/_games.py)');
for (const [id, mod, bot, seconds] of BOTS) {
  const lim = LIMITS[id.split('-')[0]];
  for (const [w, h] of [[360, 600], [430, 760]]) {
    const r = run(mod, { w, h, seconds, bot: bot() });
    const allowed = Math.min(lim.max, Math.floor((r.seconds + GRACE) * lim.perSec));
    const rate = r.score / r.seconds;
    report(`${id} bot @${w}x${h}`, r.score <= allowed && r.score > 0,
      `score ${r.score} in ${r.seconds.toFixed(0)}s (${rate.toFixed(1)}/s, limit ${lim.perSec}/s)${r.over ? ', died' : ''}`);
  }
}

console.log('\n— Random-input fuzz on several screen sizes (no exceptions, finite scores)');
const SIZES = [[296, 430], [343, 520], [375, 640], [430, 760], [744, 900], [1100, 640], [280, 300]];
const KEYS = ['left', 'right', 'up', 'down', 'space'];
let seed = 7;
const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
for (const [id, mod] of BOTS.filter(([id]) => !id.includes('-'))) {
  for (const [w, h] of SIZES) {
    try {
      let held = null;
      const r = run(mod, {
        w, h, seconds: 120, renderEvery: 2,
        bot: (g) => {
          const x = rnd();
          if (x < 0.02 && g.control) { const k = KEYS[Math.floor(rnd() * 5)]; g.control(k, true); g.control(k, false); }
          else if (x < 0.035 && g.down) { held = { x: rnd() * w, y: rnd() * h, id: 1, t: clock }; g.down(held); }
          else if (held && x < 0.3 && g.move) { held = { x: held.x + (rnd() - 0.5) * 120, y: held.y + (rnd() - 0.5) * 120, id: 1, t: clock }; g.move(held); }
          else if (held && x < 0.33 && g.up) { g.up(held); held = null; }
          if (x > 0.995 && g.resize) g.resize(w, h); // mid-game resize (rotation, keyboard)
        },
      });
      report(`${id} fuzz @${w}x${h}`, Number.isFinite(r.score), `score ${r.score}, ${r.seconds.toFixed(0)}s${r.over ? ', game over' : ''}`);
    } catch (e) {
      report(`${id} fuzz @${w}x${h}`, false, e.stack.split('\n').slice(0, 3).join(' | '));
    }
  }
}

console.log('\n— Пітон: no walls, edges wrap around');
try {
  const { api } = makeApi(360, 600);
  const g = snake.create(api);
  g.resize && g.resize(360, 600);
  g.start && g.start();
  const cols = 17, y = Math.floor(g.rows / 2);
  const place = (cells, dir) => {
    g.snake = cells; g.prev = cells.map((c) => ({ ...c })); g.dir = dir; g.queue = []; g.bonus = null;
    g.food = { x: 8, y: (y + 4) % g.rows };
  };
  place([{ x: cols - 1, y }, { x: cols - 2, y }, { x: cols - 3, y }], { x: 1, y: 0 });
  g.step();
  const right = !g.dead && g.snake[0].x === 0 && g.snake[0].y === y;
  g.tick = g.interval / 2; g.render(ctx);                       // half-way across the edge draws fine
  place([{ x: 5, y: 0 }, { x: 5, y: 1 }, { x: 5, y: 2 }], { x: 0, y: -1 });
  g.step();
  const up = !g.dead && g.snake[0].x === 5 && g.snake[0].y === g.rows - 1;
  place([{ x: 0, y }, { x: cols - 1, y }, { x: cols - 2, y }], { x: -1, y: 0 });
  g.queue = [{ x: 0, y: 1 }]; g.step(); g.queue = [{ x: 1, y: 0 }]; g.step();
  g.queue = [{ x: 0, y: -1 }]; g.step();                         // still alive: a long way from its tail
  const selfStill = !g.dead;
  place([{ x: 2, y }, { x: 1, y }, { x: 1, y: y + 1 }, { x: 2, y: y + 1 }, { x: 3, y: y + 1 }], { x: 0, y: 1 });
  g.step();                                                      // turning into its own body still ends the run
  report('python wraps right edge → left', right, 'head at x=0, alive');
  report('python wraps top edge → bottom', up, 'head at y=rows-1, alive');
  report('python wrap keeps it alive', selfStill, 'no wall death');
  report('python still dies biting itself', g.dead, 'self-collision ends the run');
} catch (e) {
  report('python wrap', false, e.stack.split('\n').slice(0, 3).join(' | '));
}

console.log(failed ? `\n${failed} FAILED` : '\nAll simulations passed');
process.exit(failed ? 1 : 0);
