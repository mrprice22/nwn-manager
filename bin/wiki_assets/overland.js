/* nwn-wiki — overland map renderer.
 *
 * Draws the world model that nwn_wiki/render/overland.py embeds in the page
 * (<script type="application/json" class="overland-data">) as a pixel-art
 * overland map: terrain tiles with rounded coastlines, dirt roads, sprites for
 * towns, castles, crypts, mines and caves, and area names.
 *
 * All art is procedural and original: tiles are painted at load time from a
 * seeded PRNG into 20×18-pixel buffers, sprites are the small bitmaps below.
 * The seed comes from the data, so a given build always looks the same.
 *
 * Loaded per page by a <script src> inside the map block, so it runs on first
 * load and again when site.js's in-place navigation swaps a map page in.
 * Self-contained: nothing here reaches the global scope. */
(function () {
"use strict";

const TW = 20, TH = 18;          // tile size in px at zoom 1
const CHUNK = 16;                // cells per cached chunk side
const CHUNK_CACHE = 140;         // chunks kept (each ~370 KB)
const ZOOMS = [0.25, 0.5, 0.75, 1, 1.5, 2, 3, 4];
const ZOOM_DEFAULT = 3;          // index of 1×

/* ── Deterministic randomness ─────────────────────────────────────────── */
function mulberry(seed) {
  let a = seed >>> 0;
  return function () {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
function hash(x, y, s) {
  let h = (Math.imul(x, 374761393) + Math.imul(y, 668265263) + Math.imul(s, 0x85EBCA77)) >>> 0;
  h = Math.imul(h ^ (h >>> 13), 1274126177) >>> 0;
  return (h ^ (h >>> 16)) >>> 0;
}
function rgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

/* ── Terrain tiles ────────────────────────────────────────────────────────
 * Codes match overland.py: water ~ w l, land g f h m d t s r. */
const LAND = new Set(['g', 'f', 'h', 'm', 'd', 't', 's', 'r']);
const MINI = {
  '~': '#1d3f8f', 'w': '#2c62b2', 'l': '#3a74c0', 'g': '#3f8f2f', 'f': '#22611c',
  'h': '#7c9c38', 'm': '#9a968c', 'd': '#e0c070', 't': '#dfe7ef', 's': '#4d6b2f',
  'r': '#5c5450',
};
const SAND = { 't': rgb('#c4d2e0'), 'r': rgb('#3e3834'), 'd': rgb('#f0dc9c') };
const SAND_DEFAULT = rgb('#d8c27e');
const FOAM = rgb('#e8f0f8'), FOAM2 = rgb('#b8d0ec'), SURF = rgb('#5a8cd0');

function Tile() { this.px = new Uint8ClampedArray(TW * TH * 4); }
Tile.prototype.put = function (x, y, c) {
  x |= 0; y |= 0;
  if (x < 0 || y < 0 || x >= TW || y >= TH) return;
  const i = (y * TW + x) * 4;
  this.px[i] = c[0]; this.px[i + 1] = c[1]; this.px[i + 2] = c[2]; this.px[i + 3] = 255;
};
Tile.prototype.fill = function (c) {
  for (let y = 0; y < TH; y++) for (let x = 0; x < TW; x++) this.put(x, y, c);
};

/* Base colour, then short strokes of the speck colours. */
function ground(t, r, base, specks) {
  t.fill(rgb(base));
  for (const [hex, share] of specks) {
    const c = rgb(hex);
    const n = Math.round(TW * TH * share);
    for (let i = 0; i < n; i++) {
      const x = Math.floor(r() * TW), y = Math.floor(r() * TH);
      t.put(x, y, c);
      if (r() < 0.5) t.put(x + 1, y, c);
    }
  }
}

function paintGrass(t, r) {
  ground(t, r, '#3f8f2f', [['#38842a', 0.09], ['#479a35', 0.09], ['#2f7425', 0.015]]);
  const hi = rgb('#6cb84c'), lo = rgb('#32782a');
  for (let i = 0; i < 3; i++) {
    const x = 1 + Math.floor(r() * (TW - 2)), y = 2 + Math.floor(r() * (TH - 3));
    t.put(x, y, hi); t.put(x, y - 1, hi); t.put(x + 1, y, lo);
  }
}

function canopy(t, cx, cy, rx, ry, pal) {
  for (let y = Math.floor(cy - ry); y <= Math.ceil(cy + ry); y++) {
    for (let x = Math.floor(cx - rx); x <= Math.ceil(cx + rx); x++) {
      const dx = (x + 0.5 - cx) / rx, dy = (y + 0.5 - cy) / ry;
      const d = dx * dx + dy * dy;
      if (d > 1) continue;
      const l = -dx * 0.6 - dy * 0.8;
      let c = pal[2];
      if (d > 0.7 && (dx > 0 || dy > 0.2)) c = pal[4];
      else if (l > 0.75) c = pal[0];
      else if (l > 0.05) c = pal[1];
      else if (l < -0.6) c = pal[3];
      t.put(x, y, c);
    }
  }
}

function paintForest(t, r, pine) {
  ground(t, r, '#2f7425', [['#24601c', 0.12], ['#3a8a2c', 0.06]]);
  const pal = (pine
    ? ['#5aa848', '#2f7c34', '#1f5e2a', '#164a22', '#0e3418']
    : ['#74c454', '#3f9a30', '#2a7a22', '#1c5a18', '#123c10']).map(rgb);
  const trunk = rgb('#4a2c14');
  const trees = [];
  for (let row = 0; row < 2; row++) {
    for (let col = 0; col < 3; col++) {
      trees.push([2.5 + col * 7 + (r() - 0.5) * 2.5 + (row ? 2 : 0),
                  4.5 + row * 8 + (r() - 0.5) * 2]);
    }
  }
  trees.sort((a, b) => a[1] - b[1]);
  for (const [x, y] of trees) {
    const rx = 3 + r() * 1.2, ry = 3 + r() * 0.8;
    t.put(x, y + ry + 0.5, trunk);
    t.put(x, y + ry + 1.5, trunk);
    canopy(t, x, y, rx, ry, pal);
  }
}

function mound(t, mx, by, a, b, pal) {
  for (let y = Math.floor(by - b); y <= by; y++) {
    for (let x = Math.floor(mx - a); x <= Math.ceil(mx + a); x++) {
      const dx = (x + 0.5 - mx) / a, dy = (by - y - 0.5) / b;
      const d = dx * dx + dy * dy;
      if (d > 1 || dy < 0) continue;
      let c = dx < -0.25 ? pal[0] : dx < 0.35 ? pal[1] : pal[2];
      if (d > 0.78) c = pal[3];
      else if (dy > 0.75 && dx < 0.2) c = pal[4];
      t.put(x, y, c);
    }
  }
}

function paintHills(t, r) {
  paintGrass(t, r);
  const pal = ['#8cb04c', '#6c9434', '#4e7024', '#33501a', '#b4d070'].map(rgb);
  mound(t, 5 + r() * 3, 10 + r() * 2, 6 + r() * 2, 6 + r() * 2, pal);
  mound(t, 12 + r() * 4, 16 + r(), 7 + r() * 2.5, 7 + r() * 2, pal);
}

function peak(t, r, px, py, w, by, pal) {
  const snowLine = py + (by - py) * (0.28 + r() * 0.1);
  for (let y = Math.ceil(py); y <= by; y++) {
    const half = (y - py) * w / (by - py);
    for (let x = Math.floor(px - half); x <= Math.ceil(px + half); x++) {
      const dx = x + 0.5 - px;
      if (Math.abs(dx) > half) continue;
      const edge = Math.abs(dx) > half - 1.1;
      let c;
      if (y < snowLine + (hash(x, y, 7) % 3) - 1) c = dx < 0.5 ? pal[4] : pal[5];
      else c = dx < 0 ? (hash(x, y, 3) % 5 ? pal[0] : pal[3]) : pal[1];
      if (edge && dx > 0) c = pal[2];
      t.put(x, y, c);
    }
  }
}

function paintMountain(t, r) {
  paintGrass(t, r);
  const pal = ['#b0aca0', '#6c6860', '#3c3a34', '#c8c4b8', '#f8f8f8', '#c8d0dc'].map(rgb);
  peak(t, r, 5 + r() * 3, 3 + r() * 3, 5 + r() * 2, 15, pal);
  peak(t, r, 12 + r() * 3, 1 + r() * 2, 7 + r() * 2, 17, pal);
}

function paintDesert(t, r) {
  ground(t, r, '#e0c070', [['#f0d890', 0.08], ['#c8a050', 0.05]]);
  const lo = rgb('#b8904a'), hi = rgb('#f4e2a8');
  for (let k = 0; k < 3; k++) {
    const y0 = 3 + k * 5 + Math.floor(r() * 2), ph = r() * 6;
    for (let x = 0; x < TW; x++) {
      if (r() < 0.25) continue;
      const y = y0 + Math.round(Math.sin((x + ph) / 2.6));
      t.put(x, y, lo); t.put(x, y - 1, hi);
    }
  }
}

function paintTundra(t, r) {
  ground(t, r, '#dfe7ef', [['#f8fbff', 0.1], ['#c4d0dc', 0.08]]);
  const rock = rgb('#8c949c'), dark = rgb('#5c646c');
  for (let i = 0; i < 3; i++) {
    const x = Math.floor(r() * (TW - 2)), y = 2 + Math.floor(r() * (TH - 3));
    t.put(x, y, rock); t.put(x + 1, y, rock); t.put(x + 1, y + 1, dark);
  }
}

function paintSwamp(t, r) {
  ground(t, r, '#4d6b2f', [['#3c5a24', 0.12], ['#5e7c3a', 0.06]]);
  const water = rgb('#3a6a6a'), shine = rgb('#7aa8a8'), reed = rgb('#8ca040'), top = rgb('#5c3c1c');
  for (let i = 0; i < 3; i++) {
    const cx = 3 + r() * 14, cy = 3 + r() * 12, rx = 2 + r() * 2, ry = 1 + r();
    for (let y = Math.floor(cy - ry); y <= cy + ry; y++)
      for (let x = Math.floor(cx - rx); x <= cx + rx; x++) {
        const dx = (x + 0.5 - cx) / rx, dy = (y + 0.5 - cy) / ry;
        if (dx * dx + dy * dy <= 1) t.put(x, y, water);
      }
    t.put(cx - 1, cy - 0.5, shine);
  }
  for (let i = 0; i < 4; i++) {
    const x = Math.floor(r() * TW), y = 3 + Math.floor(r() * (TH - 4));
    t.put(x, y, reed); t.put(x, y - 1, reed); t.put(x, y - 2, top);
  }
}

function paintRock(t, r) {
  ground(t, r, '#5c5450', [['#6e6660', 0.12], ['#4a4440', 0.1]]);
  const crack = rgb('#2e2a26'), peb = rgb('#8a827a');
  for (let i = 0; i < 2; i++) {
    let x = Math.floor(r() * TW), y = Math.floor(r() * TH);
    for (let s = 0; s < 7; s++) {
      t.put(x, y, crack);
      x += r() < 0.5 ? 1 : 0; y += r() < 0.6 ? 1 : -1;
    }
  }
  for (let i = 0; i < 4; i++) t.put(Math.floor(r() * TW), Math.floor(r() * TH), peb);
}

function paintWater(base, specks, waves) {
  return function (t, r) {
    ground(t, r, base, specks);
    const c = waves.map(rgb);
    for (let i = 0; i < 3; i++) {
      const x = Math.floor(r() * (TW - 4)), y = 1 + Math.floor(r() * (TH - 2));
      t.put(x, y, c[0]); t.put(x + 1, y - 1, c[1]); t.put(x + 2, y - 1, c[1]); t.put(x + 3, y, c[0]);
    }
  };
}

const PAINTERS = {
  '~': paintWater('#1d3f8f', [['#24499c', 0.1], ['#1a3880', 0.06]], ['#3a64b8', '#5a84cc']),
  'w': paintWater('#2c62b2', [['#3470bc', 0.1], ['#2858a4', 0.06]], ['#4a80cc', '#7aa4dc']),
  'l': paintWater('#3a74c0', [['#4282c8', 0.1], ['#3468b0', 0.06]], ['#5a90d4', '#8ab4e4']),
  'g': paintGrass, 'f': (t, r) => paintForest(t, r, false), 'h': paintHills,
  'm': paintMountain, 'd': paintDesert, 's': paintSwamp, 'r': paintRock,
  't': function (t, r) { paintTundra(t, r); },
};
const VARIANTS = 4;

function buildTiles(seed) {
  const tiles = {};
  for (const code of Object.keys(PAINTERS)) {
    tiles[code] = [];
    for (let v = 0; v < VARIANTS; v++) {
      const t = new Tile();
      PAINTERS[code](t, mulberry(seed * 131 + code.charCodeAt(0) * 977 + v * 7919));
      tiles[code].push(t.px);
    }
  }
  return tiles;
}

/* ── Sprites (16×16, original pixel art) ──────────────────────────────── */
const SPRITE_PAL = {
  k: '#1a1410', f: '#e03030', r: '#c0402a', R: '#84261a', w: '#e4dccb',
  W: '#b0a690', y: '#f8d860', g: '#a8a498', G: '#6a6860', d: '#0c0808',
  b: '#7a4c24', B: '#4c2c14', c: '#9a5ad8', C: '#d0a8ff', o: '#f0a030',
  e: '#4a8a30', u: '#9ee4ff', U: '#2a8ad8',
};
const SPRITES = {
  town: [
    '......k.........',
    '.....kfk........',
    '.....kffk.......',
    '.....k..........',
    '....kkkk...kk...',
    '...krrrRk.krRk..',
    '..krrrrrRkrrrRk.',
    '..kwwwwwWkwwwWk.',
    '..kwykwyWkwykWk.',
    'kkkkkkkkkkkkkkkk',
    'kgggGkgggGkgggGk',
    'kgggGkgggGkgggGk',
    'kgGgGkgddGkgGgGk',
    'kGGGGkGddGkGGGGk',
    'kkkkkkkkkkkkkkkk',
    '................',
  ],
  castle: [
    '......ffk.......',
    '.......fk.......',
    'k.k.k...k..k.k.k',
    'kgkgk..kgk.kgkgk',
    'kgggk.kgggkkgggk',
    'kgGgk.kgGgkkgGgk',
    'kgggkkkgggkkgggk',
    'kgGgggggGgggggGk',
    'kgggggggggggggGk',
    'kgdgggggggggdgGk',
    'kgggggkkkggggggk',
    'kgGgggkddkgggGGk',
    'kgggggkddkggggGk',
    'kGGGGGkddkGGGGGk',
    'kkkkkkkkkkkkkkkk',
    '................',
  ],
  tower: [
    '.......kk.......',
    '......kCck......',
    '......kCck......',
    '.....kCccck.....',
    '.....kCccck.....',
    '....kCcccccck...',
    '...kkkkkkkkkkk..',
    '.....kgggGGk....',
    '.....kgyyGGk....',
    '.....kgggGGk....',
    '.....kggggGk....',
    '.....kgGggGk....',
    '.....kgggGGk....',
    '....kggkkgGGk...',
    '....kggkdkGGk...',
    '....kkkkkkkkk...',
  ],
  house: [
    '................',
    '................',
    '.........kk.....',
    '......kkkBk.....',
    '.....krrrrkk....',
    '....krrrrrrRk...',
    '...krrrrrrrRRk..',
    '..krrrrrrrrRRRk.',
    '.kkkkkkkkkkkkkkk',
    '..kwwwwwwwwwWk..',
    '..kwykwwwbbwWk..',
    '..kwkkwwwbbwWk..',
    '..kwwwwwwbbwWk..',
    '..kWWWWWWbbWWk..',
    '..kkkkkkkkkkkk..',
    '................',
  ],
  crypt: [
    '................',
    '.......kk.......',
    '.....kkggkk.....',
    '...kkggggggkk...',
    '.kkggggggggggkk.',
    'kkkkkkkkkkkkkkkk',
    '.kwkwwkddkwwkWk.',
    '.kwkwwkddkwwkWk.',
    '.kwkwwkddkwwkWk.',
    '.kwkwwkddkwwkWk.',
    '.kWkWWkddkWWkWk.',
    'kkkkkkkkkkkkkkkk',
    'kGGGGGGGGGGGGGGk',
    'kkkkkkkkkkkkkkkk',
    '................',
    '................',
  ],
  mine: [
    '................',
    '......kkkk......',
    '....kkggggkk....',
    '...kgggggGGGk...',
    '..kggGgggggGGk..',
    '..kgggbbbbgGGk..',
    '.kggbbBBBBbbGGk.',
    '.kgbbkddddkbbGk.',
    '.kgbkddddddkbGk.',
    'kggbkddddddkbGGk',
    'kgGbkddddddkbGGk',
    'kggbkddddddkbGGk',
    'kgGbkddddddkbGGk',
    'kkkkkkkkkkkkkkkk',
    '...B..B..B..B...',
    'kkkkkkkkkkkkkkkk',
  ],
  cave: [
    '................',
    '................',
    '......kkkk......',
    '....kkggggkk....',
    '...kgggggggGk...',
    '..kgggggggggGk..',
    '.kgggkkkkkggGGk.',
    '.kggkddddkgggGk.',
    'kgggkddddddkgGGk',
    'kggkddddddddkGGk',
    'kegkddddddddkGek',
    'keekddddddddkeek',
    'kkkkkkkkkkkkkkkk',
    '................',
    '................',
    '................',
  ],
  dungeon: [
    '................',
    '....kkkkkkkk....',
    '...kggggggggk...',
    '..kgGkkkkkkGgk..',
    '..kgkddddddkgk..',
    '.okgkddddddkgko.',
    '..kgkdGGGGdkgk..',
    '..kgkddddddkgk..',
    '..kgkdGGGGdkgk..',
    '..kgkddddddkgk..',
    '..kgkdGGGGdkgk..',
    '..kgkddddddkgk..',
    '.kkkkkkkkkkkkkk.',
    '.kGGGGGGGGGGGGk.',
    '.kkkkkkkkkkkkkk.',
    '................',
  ],
  portal: [
    '................',
    '..kkkkkkkkkkkk..',
    '..kggggggggggk..',
    '..kGGGGGGGGGGk..',
    '...kgkkkkkkgk...',
    '...kgkuUUukgk...',
    '...kgkUuuUkgk...',
    '...kgkuCCukgk...',
    '...kgkuCCukgk...',
    '...kgkUuuUkgk...',
    '...kgkuUUukgk...',
    '...kgkUuuUkgk...',
    '...kgkuUUukgk...',
    '..kggGkkkkGggk..',
    '..kkkkkkkkkkkk..',
    '................',
  ],
  sewer: [
    '................',
    '................',
    '................',
    '................',
    '.....kkkkkk.....',
    '...kkGGGGGGkk...',
    '..kGGkGkGkGGGk..',
    '.kGkdkdkdkdkGGk.',
    '.kGkdkdkdkdkGGk.',
    '.kGkdkdkdkdkGGk.',
    '..kGGkGkGkGGGk..',
    '...kkGGGGGGkk...',
    '.....kkkkkk.....',
    '................',
    '................',
    '................',
  ],
};

function buildSprites() {
  const out = {};
  for (const [name, rows] of Object.entries(SPRITES)) {
    const cv = document.createElement('canvas');
    cv.width = 16; cv.height = 16;
    const g = cv.getContext('2d');
    rows.forEach((row, y) => {
      for (let x = 0; x < 16; x++) {
        const c = SPRITE_PAL[row[x]];
        if (!c) continue;
        g.fillStyle = c;
        g.fillRect(x, y, 1, 1);
      }
    });
    out[name] = cv;
  }
  return out;
}

/* ── World ─────────────────────────────────────────────────────────────── */
function World(data) {
  this.W = data.w; this.H = data.h;
  this.seed = data.seed | 0;
  this.code = new Array(this.W * this.H);
  data.terrain.forEach((row, y) => {
    for (let x = 0; x < this.W; x++) this.code[y * this.W + x] = row[x] || '~';
  });
  this.owner = new Int16Array(this.W * this.H);
  let k = 0;
  for (let i = 0; i < data.owner.length; i += 2) {
    const o = data.owner[i], n = data.owner[i + 1];
    for (let j = 0; j < n; j++) this.owner[k++] = o;
  }
  // Corner landness: mean of the four cells that meet at each grid corner.
  const CW = this.W + 1;
  this.corner = new Float32Array(CW * (this.H + 1));
  for (let y = 0; y <= this.H; y++) {
    for (let x = 0; x <= this.W; x++) {
      let s = 0;
      for (const [dx, dy] of [[-1, -1], [0, -1], [-1, 0], [0, 0]]) s += this.isLand(x + dx, y + dy) ? 1 : 0;
      this.corner[y * CW + x] = s / 4;
    }
  }
  // Corner shallowness for water cells (land counts as shallow), so the
  // deep/shallow edge is rounded the same way as the coast.
  this.shallow = new Float32Array(CW * (this.H + 1));
  for (let y = 0; y <= this.H; y++) {
    for (let x = 0; x <= this.W; x++) {
      let s = 0;
      for (const [dx, dy] of [[-1, -1], [0, -1], [-1, 0], [0, 0]]) s += this.at(x + dx, y + dy) === '~' ? 0 : 1;
      this.shallow[y * CW + x] = s / 4;
    }
  }
  this.areas = data.areas;
  this.labels = data.labels;
  this.roads = data.roads;
  this.sites = data.sites.map(s => ({ x: s[0], y: s[1], icon: s[2], area: s[3] }));
  this.siteAt = new Map(this.sites.map(s => [s.y * this.W + s.x, s]));
  // Teleport-hub destinations: area index → hub area index.
  this.callouts = (data.callouts || []).map(c => ({ x: c[0], y: c[1], area: c[2], hub: c[3] }));
  this.calloutOf = new Map(this.callouts.map(c => [c.area, c]));
  this.cellCount = new Int32Array(this.areas.length);
  this.bbox = this.areas.map(() => [1e9, 1e9, -1, -1]);
  for (let y = 0; y < this.H; y++) {
    for (let x = 0; x < this.W; x++) {
      const o = this.owner[y * this.W + x];
      if (o < 0) continue;
      this.cellCount[o]++;
      const b = this.bbox[o];
      if (x < b[0]) b[0] = x; if (y < b[1]) b[1] = y;
      if (x > b[2]) b[2] = x; if (y > b[3]) b[3] = y;
    }
  }
}
World.prototype.at = function (x, y) {
  if (x < 0 || y < 0 || x >= this.W || y >= this.H) return '~';
  return this.code[y * this.W + x];
};
World.prototype.isLand = function (x, y) { return LAND.has(this.at(x, y)); };
World.prototype.ownerAt = function (x, y) {
  if (x < 0 || y < 0 || x >= this.W || y >= this.H) return -1;
  return this.owner[y * this.W + x];
};

/* ── Chunk renderer ───────────────────────────────────────────────────────
 * Composites one CHUNK×CHUNK block of cells pixel by pixel. Coast cells use a
 * marching-squares field (bilinear corner landness) so shores are rounded,
 * with a sand rim on the land side and a foam line on the water side.
 * Borders between different terrains of the same kind are dithered. */
function Renderer(world) {
  this.world = world;
  this.tiles = buildTiles(world.seed);
  this.cache = new Map();
}
Renderer.prototype.tileOf = function (code, x, y) {
  const set = this.tiles[code] || this.tiles.g;
  return set[hash(x, y, this.world.seed) % VARIANTS];
};
Renderer.prototype.chunk = function (i, j) {
  const key = i + ',' + j;
  let cv = this.cache.get(key);
  if (cv) { this.cache.delete(key); this.cache.set(key, cv); return cv; }
  cv = this.renderChunk(i, j);
  this.cache.set(key, cv);
  if (this.cache.size > CHUNK_CACHE) this.cache.delete(this.cache.keys().next().value);
  return cv;
};
Renderer.prototype.renderChunk = function (ci, cj) {
  const w = this.world, CW = w.W + 1, seed = w.seed;
  const x0 = ci * CHUNK, y0 = cj * CHUNK;
  const cols = Math.max(0, Math.min(CHUNK, w.W - x0)), rows = Math.max(0, Math.min(CHUNK, w.H - y0));
  const cv = document.createElement('canvas');
  cv.width = Math.max(1, cols * TW); cv.height = Math.max(1, rows * TH);
  if (!cols || !rows) return cv;
  const g = cv.getContext('2d');
  const img = g.createImageData(cols * TW, rows * TH);
  const out = img.data, stride = cols * TW * 4;
  const DITHER = [0.55, 0.3, 0.12];

  for (let cy = 0; cy < rows; cy++) {
    for (let cx = 0; cx < cols; cx++) {
      const x = x0 + cx, y = y0 + cy;
      const code = w.at(x, y), land = LAND.has(code);
      const own = this.tileOf(code, x, y);
      const c00 = w.corner[y * CW + x], c10 = w.corner[y * CW + x + 1];
      const c01 = w.corner[(y + 1) * CW + x], c11 = w.corner[(y + 1) * CW + x + 1];
      const want = land ? 1 : 0;
      const coast = !(c00 === want && c10 === want && c01 === want && c11 === want);
      // Neighbour codes for dithering: left, right, up, down.
      const nb = [w.at(x - 1, y), w.at(x + 1, y), w.at(x, y - 1), w.at(x, y + 1)];
      const nbTile = nb.map((c, k) => this.tileOf(c, x + [-1, 1, 0, 0][k], y + [0, 0, -1, 1][k]));
      // For coast cells: the tile to borrow for the "other" side.
      let otherLand = null, otherWater = null;
      if (coast) {
        for (const [dx, dy] of [[0, 0], [-1, 0], [1, 0], [0, -1], [0, 1], [-1, -1], [1, -1], [-1, 1], [1, 1]]) {
          const c = w.at(x + dx, y + dy);
          if (LAND.has(c)) { if (!otherLand) otherLand = [c, this.tileOf(c, x + dx, y + dy)]; }
          else if (!otherWater) otherWater = this.tileOf(c, x + dx, y + dy);
        }
        if (!otherLand) otherLand = ['g', this.tiles.g[0]];
        if (!otherWater) otherWater = this.tiles.w[0];
      }
      const sand = otherLand ? (SAND[otherLand[0]] || SAND_DEFAULT) : SAND_DEFAULT;
      const s00 = w.shallow[y * CW + x], s10 = w.shallow[y * CW + x + 1];
      const s01 = w.shallow[(y + 1) * CW + x], s11 = w.shallow[(y + 1) * CW + x + 1];
      const depthEdge = !(s00 === s10 && s10 === s01 && s01 === s11);
      const shallowTile = code === 'w' ? own : this.tileOf('w', x, y);
      const deepTile = code === '~' ? own : this.tileOf('~', x, y);

      for (let py = 0; py < TH; py++) {
        const o = (cy * TH + py) * stride + cx * TW * 4;
        for (let px = 0; px < TW; px++) {
          const ti = (py * TW + px) * 4;
          let src = own, s = ti, color = null;
          if (coast) {
            const u = (px + 0.5) / TW, v = (py + 0.5) / TH;
            const gx = x * TW + px, gy = y * TH + py;
            const f = (c00 * (1 - u) + c10 * u) * (1 - v) + (c01 * (1 - u) + c11 * u) * v
                    + ((hash(gx >> 1, gy >> 1, seed) & 1023) / 1023 - 0.5) * 0.07;
            if (f >= 0.5) {
              src = land ? own : otherLand[1];
              if (f < 0.555) color = sand;
            } else {
              src = land ? otherWater : own;
              if (f > 0.44) color = (hash(gx, gy, seed + 1) & 3) ? FOAM : FOAM2;
              else if (f > 0.36 && f < 0.39) color = SURF;
            }
          } else if (!land && code !== 'l' && depthEdge) {
            const u = (px + 0.5) / TW, v = (py + 0.5) / TH;
            const f = (s00 * (1 - u) + s10 * u) * (1 - v) + (s01 * (1 - u) + s11 * u) * v
                    + ((hash(x * TW + px >> 1, y * TH + py >> 1, seed + 5) & 1023) / 1023 - 0.5) * 0.12;
            src = f >= 0.5 ? shallowTile : deepTile;
          } else {
            // Dither towards a differing neighbour of the same kind.
            const d = [px, TW - 1 - px, py, TH - 1 - py];
            let k = 0;
            for (let q = 1; q < 4; q++) if (d[q] < d[k]) k = q;
            if (d[k] < 3 && nb[k] !== code && LAND.has(nb[k]) === land &&
                (hash(x * TW + px, y * TH + py, seed + 9) & 1023) / 1023 < DITHER[d[k]]) {
              src = nbTile[k];
            }
          }
          if (color) {
            out[o + px * 4] = color[0]; out[o + px * 4 + 1] = color[1];
            out[o + px * 4 + 2] = color[2]; out[o + px * 4 + 3] = 255;
          } else {
            out[o + px * 4] = src[s]; out[o + px * 4 + 1] = src[s + 1];
            out[o + px * 4 + 2] = src[s + 2]; out[o + px * 4 + 3] = 255;
          }
        }
      }
    }
  }
  g.putImageData(img, 0, 0);
  return cv;
};

/* ── The map widget ───────────────────────────────────────────────────── */
function initOverland(root) {
  if (root.dataset.ovInit) return;
  const dataEl = root.querySelector('script.overland-data');
  const wrap = root.querySelector('.overland-wrap');
  const canvas = root.querySelector('.overland-canvas');
  const mini = root.querySelector('.overland-minimap');
  const tip = root.querySelector('.overland-tip');
  if (!dataEl || !wrap || !canvas || !canvas.getContext) return;
  let data;
  try { data = JSON.parse(dataEl.textContent); } catch (e) { console.error(e); return; }
  root.dataset.ovInit = '1';

  const world = new World(data);
  const ren = new Renderer(world);
  const sprites = buildSprites();
  const ctx = canvas.getContext('2d');
  canvas.tabIndex = 0;

  let zi = ZOOM_DEFAULT;
  let vx = (data.start[0] + 0.5) * TW, vy = (data.start[1] + 0.5) * TH;   // view centre, world px
  let cw = 0, ch = 0, dpr = 1;
  let hover = null;          // {area, site}
  let showLabels = true;
  let tick = 0, pending = false;
  let focus = null, fly = null;   // finder: highlighted place, pan animation
  let miniBase = null;
  const outlines = new Map();

  const zoom = () => ZOOMS[zi];
  const worldW = world.W * TW, worldH = world.H * TH;

  function clampView() {
    vx = Math.max(0, Math.min(worldW, vx));
    vy = Math.max(0, Math.min(worldH, vy));
  }
  function resize() {
    dpr = window.devicePixelRatio || 1;
    cw = wrap.clientWidth; ch = wrap.clientHeight;
    canvas.width = Math.max(1, Math.round(cw * dpr));
    canvas.height = Math.max(1, Math.round(ch * dpr));
    canvas.style.width = cw + 'px'; canvas.style.height = ch + 'px';
    miniBase = null;
    draw();
  }
  function request() {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; draw(); });
  }
  const toScreenX = wx => (wx - vx) * zoom() + cw / 2;
  const toScreenY = wy => (wy - vy) * zoom() + ch / 2;

  function outlineOf(a) {
    let segs = outlines.get(a);
    if (segs) return segs;
    segs = [];
    const b = world.bbox[a];
    for (let y = b[1]; y <= b[3]; y++) {
      for (let x = b[0]; x <= b[2]; x++) {
        if (world.ownerAt(x, y) !== a) continue;
        if (world.ownerAt(x - 1, y) !== a) segs.push([x, y, x, y + 1]);
        if (world.ownerAt(x + 1, y) !== a) segs.push([x + 1, y, x + 1, y + 1]);
        if (world.ownerAt(x, y - 1) !== a) segs.push([x, y, x + 1, y]);
        if (world.ownerAt(x, y + 1) !== a) segs.push([x, y + 1, x + 1, y + 1]);
      }
    }
    outlines.set(a, segs);
    return segs;
  }

  function draw() {
    if (!root.isConnected) return;
    const z = zoom();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.imageSmoothingEnabled = false;
    ctx.fillStyle = MINI['~'];
    ctx.fillRect(0, 0, cw, ch);

    // Terrain chunks.
    const wx0 = vx - cw / 2 / z, wy0 = vy - ch / 2 / z;
    const wx1 = vx + cw / 2 / z, wy1 = vy + ch / 2 / z;
    const CPX = CHUNK * TW, CPY = CHUNK * TH;
    const i0 = Math.max(0, Math.floor(wx0 / CPX)), i1 = Math.min(Math.ceil(world.W / CHUNK) - 1, Math.floor(wx1 / CPX));
    const j0 = Math.max(0, Math.floor(wy0 / CPY)), j1 = Math.min(Math.ceil(world.H / CHUNK) - 1, Math.floor(wy1 / CPY));
    for (let j = j0; j <= j1; j++) {
      for (let i = i0; i <= i1; i++) {
        const cv = ren.chunk(i, j);
        const sx = Math.round(toScreenX(i * CPX)), sy = Math.round(toScreenY(j * CPY));
        const ex = Math.round(toScreenX(i * CPX + cv.width)), ey = Math.round(toScreenY(j * CPY + cv.height));
        ctx.drawImage(cv, sx, sy, ex - sx, ey - sy);
      }
    }

    // Wave glints on open water (animated).
    if (z >= 1) {
      const cx0 = Math.max(0, Math.floor(wx0 / TW)), cx1 = Math.min(world.W - 1, Math.floor(wx1 / TW));
      const cy0 = Math.max(0, Math.floor(wy0 / TH)), cy1 = Math.min(world.H - 1, Math.floor(wy1 / TH));
      ctx.fillStyle = 'rgba(200,225,255,0.75)';
      for (let y = cy0; y <= cy1; y++) {
        for (let x = cx0; x <= cx1; x++) {
          const c = world.at(x, y);
          if (c !== '~' && c !== 'w') continue;
          const h = hash(x, y, world.seed + 77);
          if (h % 5) continue;
          const ph = (tick + (h >>> 8)) % 8;
          if (ph > 3) continue;
          const gx = x * TW + 3 + ((h >>> 3) % 12) + ph, gy = y * TH + 3 + ((h >>> 5) % 12);
          ctx.fillRect(toScreenX(gx), toScreenY(gy), (3 - Math.abs(ph - 1.5)) * z, z);
        }
      }
    }

    // Roads: transition point to transition point.
    ctx.lineCap = 'round';
    for (const [ax, ay, bx, by, under] of world.roads) {
      const x1 = toScreenX(ax * TW), y1 = toScreenY(ay * TH);
      const x2 = toScreenX(bx * TW), y2 = toScreenY(by * TH);
      ctx.setLineDash(under ? [2 * z, 4 * z] : []);
      ctx.strokeStyle = 'rgba(60,36,14,0.55)';
      ctx.lineWidth = Math.max(1.2, 2.4 * z);
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
      ctx.strokeStyle = 'rgba(190,150,90,0.8)';
      ctx.lineWidth = Math.max(0.6, 1.1 * z);
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
    }
    ctx.setLineDash([]);

    // Hovered area: tint + outline.
    if (hover && hover.area >= 0 && world.cellCount[hover.area]) {
      const segs = outlineOf(hover.area);
      ctx.strokeStyle = '#ffe680';
      ctx.lineWidth = 2;
      ctx.beginPath();
      for (const [x1, y1, x2, y2] of segs) {
        ctx.moveTo(toScreenX(x1 * TW), toScreenY(y1 * TH));
        ctx.lineTo(toScreenX(x2 * TW), toScreenY(y2 * TH));
      }
      ctx.stroke();
    }

    // Sites, back to front; teleport destinations glow underneath.
    const sz = 16 * Math.max(z, 0.5);
    const pulse = 0.75 + 0.25 * Math.sin(tick / 2);
    for (const c of world.callouts) {
      const px = toScreenX((c.x + 0.5) * TW), py = toScreenY((c.y + 0.5) * TH);
      if (px < -sz * 2 || py < -sz * 2 || px > cw + sz * 2 || py > ch + sz * 2) continue;
      const g = ctx.createRadialGradient(px, py, 0, px, py, sz * 1.1 * pulse);
      g.addColorStop(0, 'rgba(158,228,255,0.85)');
      g.addColorStop(0.5, 'rgba(90,170,255,0.4)');
      g.addColorStop(1, 'rgba(90,170,255,0)');
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(px, py, sz * 1.1 * pulse, 0, Math.PI * 2);
      ctx.fill();
    }
    for (const s of world.sites) {
      const px = toScreenX((s.x + 0.5) * TW), py = toScreenY((s.y + 1) * TH);
      if (px < -sz || py < -sz || px > cw + sz || py > ch + sz * 2) continue;
      ctx.fillStyle = 'rgba(0,0,0,0.3)';
      ctx.beginPath();
      ctx.ellipse(px + sz * 0.05, py - sz * 0.08, sz * 0.5, sz * 0.14, 0, 0, Math.PI * 2);
      ctx.fill();
      const img = sprites[s.icon] || sprites.tower;
      ctx.drawImage(img, Math.round(px - sz / 2), Math.round(py - sz - sz * 0.05), Math.round(sz), Math.round(sz));
      if (hover && hover.site === s) {
        ctx.strokeStyle = '#ffe680'; ctx.lineWidth = 1.5;
        ctx.strokeRect(Math.round(px - sz / 2) - 1.5, Math.round(py - sz) - 1.5, sz + 3, sz + 3);
      }
    }

    // Place picked in the finder: an expanding ring plus a steady one.
    if (focus) {
      const t = (performance.now() % 1100) / 1100, k = Math.max(z, 0.5);
      const px = toScreenX(focus.x * TW), py = toScreenY(focus.y * TH);
      ctx.strokeStyle = 'rgba(255,230,128,' + (1 - t).toFixed(2) + ')';
      ctx.lineWidth = 3;
      ctx.beginPath(); ctx.arc(px, py, (10 + 34 * t) * k, 0, Math.PI * 2); ctx.stroke();
      ctx.strokeStyle = '#ffe680';
      ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(px, py, 13 * k, 0, Math.PI * 2); ctx.stroke();
    }

    // Area names, biggest areas first, skipping any that would overlap.
    if (showLabels && z >= 0.5) {
      const fs = Math.round(Math.max(10, Math.min(18, 12 * Math.sqrt(z))));
      ctx.font = 'bold ' + fs + 'px "Iowan Old Style", Georgia, serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.lineJoin = 'round';
      const placed = [];
      for (const c of world.callouts) {
        const sx = toScreenX((c.x + 0.5) * TW), sy = toScreenY((c.y + 1) * TH) + fs * 0.75;
        if (sx < -200 || sy < -40 || sx > cw + 200 || sy > ch + 40) continue;
        const name = '✦ ' + world.areas[c.area][1].trim();
        const tw = ctx.measureText(name).width;
        const box = [sx - tw / 2 - 6, sy - fs / 2 - 3, sx + tw / 2 + 6, sy + fs / 2 + 3];
        placed.push(box);
        // A banner, so destinations stand out from ordinary area names.
        ctx.fillStyle = 'rgba(10,28,66,0.88)';
        ctx.strokeStyle = '#9ee4ff';
        ctx.lineWidth = 1;
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(box[0], box[1], box[2] - box[0], box[3] - box[1], 4);
        else ctx.rect(box[0], box[1], box[2] - box[0], box[3] - box[1]);
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle = '#d8f2ff';
        ctx.fillText(name, sx, sy);
      }
      const order = world.labels.slice().sort((a, b) => world.cellCount[b[0]] - world.cellCount[a[0]]);
      for (const [a, lx, ly] of order) {
        const sx = toScreenX(lx * TW), sy = toScreenY(ly * TH);
        if (sx < -200 || sy < -40 || sx > cw + 200 || sy > ch + 40) continue;
        const name = world.areas[a][1];
        const tw = ctx.measureText(name).width;
        const box = [sx - tw / 2 - 3, sy - fs / 2 - 2, sx + tw / 2 + 3, sy + fs / 2 + 2];
        if (placed.some(p => box[0] < p[2] && box[2] > p[0] && box[1] < p[3] && box[3] > p[1])) continue;
        placed.push(box);
        ctx.strokeStyle = 'rgba(20,12,4,0.92)';
        ctx.lineWidth = 3.5;
        ctx.strokeText(name, sx, sy);
        ctx.fillStyle = world.areas[a][2] || '#f4d878';
        ctx.fillText(name, sx, sy);
      }
    }
    drawMini();
  }

  // Minimap: one pixel per cell, drawn once per size; the viewport box on top.
  function drawMini() {
    if (!mini) return;
    if (!miniBase) {
      miniBase = document.createElement('canvas');
      miniBase.width = world.W; miniBase.height = world.H;
      const g = miniBase.getContext('2d');
      const img = g.createImageData(world.W, world.H);
      const cache = {};
      for (let i = 0; i < world.code.length; i++) {
        const c = cache[world.code[i]] || (cache[world.code[i]] = rgb(MINI[world.code[i]] || MINI.g));
        img.data[i * 4] = c[0]; img.data[i * 4 + 1] = c[1]; img.data[i * 4 + 2] = c[2]; img.data[i * 4 + 3] = 255;
      }
      g.putImageData(img, 0, 0);
      const s = Math.min(180 / world.W, 180 / world.H);
      mini.width = Math.round(world.W * s * dpr); mini.height = Math.round(world.H * s * dpr);
      mini.style.width = Math.round(world.W * s) + 'px';
      mini.style.height = Math.round(world.H * s) + 'px';
    }
    const m = mini.getContext('2d');
    m.imageSmoothingEnabled = false;
    m.drawImage(miniBase, 0, 0, mini.width, mini.height);
    const sx = mini.width / worldW, sy = mini.height / worldH, z = zoom();
    m.fillStyle = '#bfeaff';
    const d = Math.max(2, Math.round(2 * dpr));
    for (const c of world.callouts) m.fillRect((c.x + 0.5) * TW * sx - d / 2, (c.y + 0.5) * TH * sy - d / 2, d, d);
    m.strokeStyle = '#ffe680';
    m.lineWidth = Math.max(1, dpr);
    m.strokeRect((vx - cw / 2 / z) * sx, (vy - ch / 2 / z) * sy, cw / z * sx, ch / z * sy);
  }

  function setZoom(nzi, px, py) {
    nzi = Math.max(0, Math.min(ZOOMS.length - 1, nzi));
    if (nzi === zi) return;
    px = px === undefined ? cw / 2 : px;
    py = py === undefined ? ch / 2 : py;
    // Keep the world point under (px, py) fixed.
    const wx = vx + (px - cw / 2) / zoom(), wy = vy + (py - ch / 2) / zoom();
    zi = nzi;
    vx = wx - (px - cw / 2) / zoom();
    vy = wy - (py - ch / 2) / zoom();
    clampView();
    request();
  }

  function pick(px, py) {
    const z = zoom();
    const wx = vx + (px - cw / 2) / z, wy = vy + (py - ch / 2) / z;
    const x = Math.floor(wx / TW), y = Math.floor(wy / TH);
    // A sprite stands on its cell and reaches up into the one above.
    const site = world.siteAt.get(y * world.W + x) || world.siteAt.get((y + 1) * world.W + x);
    if (site) return { area: site.area, site: site };
    const a = world.ownerAt(x, y);
    return a >= 0 ? { area: a, site: null } : null;
  }

  function showTip(h, px, py) {
    if (!tip) return;
    if (!h) { tip.hidden = true; return; }
    const a = world.areas[h.area];
    tip.textContent = '';
    const b = document.createElement('b');
    b.textContent = a[1];
    tip.appendChild(b);
    const co = world.calloutOf.get(h.area);
    const meta = [a[4], h.site && h.site.icon !== 'portal' ? h.site.icon : '',
                  co ? 'teleport from ' + world.areas[co.hub][1].trim() : ''].filter(Boolean).join(' · ');
    if (meta) {
      const s = document.createElement('span');
      s.textContent = meta;
      tip.appendChild(s);
    }
    tip.hidden = false;
    const r = tip.getBoundingClientRect();
    tip.style.left = Math.min(px + 14, cw - r.width - 4) + 'px';
    tip.style.top = Math.min(py + 16, ch - r.height - 4) + 'px';
  }

  function open(h, newTab) {
    if (!h) return;
    const href = world.areas[h.area][3];
    if (newTab) { window.open(href, '_blank'); return; }
    // A real anchor click, so site.js's in-place navigation (when on) runs.
    const a = document.createElement('a');
    a.href = href;
    a.hidden = true;
    root.appendChild(a);
    a.click();
    a.remove();
  }

  // Pointer: drag pans, a still click opens.
  let drag = null;
  canvas.addEventListener('pointerdown', e => {
    if (e.button !== 0 && e.pointerType === 'mouse') return;
    drag = { x: e.clientX, y: e.clientY, moved: 0 };
    fly = null;
    canvas.setPointerCapture(e.pointerId);
    canvas.classList.add('dragging');
  });
  canvas.addEventListener('pointermove', e => {
    const r = canvas.getBoundingClientRect();
    const px = e.clientX - r.left, py = e.clientY - r.top;
    if (drag) {
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      drag.moved += Math.abs(dx) + Math.abs(dy);
      drag.x = e.clientX; drag.y = e.clientY;
      vx -= dx / zoom(); vy -= dy / zoom();
      clampView();
      if (drag.moved > 4) showTip(null);
      request();
      return;
    }
    const h = pick(px, py);
    if ((h && h.area) !== (hover && hover.area) || (h && h.site) !== (hover && hover.site)) {
      hover = h;
      request();
    }
    canvas.style.cursor = h ? 'pointer' : '';
    showTip(h, px, py);
  });
  canvas.addEventListener('pointerup', e => {
    if (!drag) return;
    const still = drag.moved < 5;
    drag = null;
    canvas.classList.remove('dragging');
    if (still) {
      const r = canvas.getBoundingClientRect();
      open(pick(e.clientX - r.left, e.clientY - r.top), e.ctrlKey || e.metaKey);
    }
  });
  canvas.addEventListener('pointercancel', () => { drag = null; canvas.classList.remove('dragging'); });
  canvas.addEventListener('pointerleave', () => {
    if (drag) return;
    hover = null; showTip(null); request();
  });
  canvas.addEventListener('auxclick', e => {
    if (e.button !== 1) return;
    const r = canvas.getBoundingClientRect();
    open(pick(e.clientX - r.left, e.clientY - r.top), true);
  });
  canvas.addEventListener('wheel', e => {
    e.preventDefault();
    const r = canvas.getBoundingClientRect();
    setZoom(zi + (e.deltaY < 0 ? 1 : -1), e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });
  canvas.addEventListener('keydown', e => {
    const step = 80 / zoom();
    const keys = {
      ArrowLeft: () => { vx -= step; }, ArrowRight: () => { vx += step; },
      ArrowUp: () => { vy -= step; }, ArrowDown: () => { vy += step; },
      '+': () => setZoom(zi + 1), '=': () => setZoom(zi + 1), '-': () => setZoom(zi - 1),
    };
    const fn = keys[e.key];
    if (!fn) return;
    e.preventDefault();
    fn(); clampView(); request();
  });

  if (mini) {
    let miniDrag = false;
    const jump = e => {
      const r = mini.getBoundingClientRect();
      vx = (e.clientX - r.left) / r.width * worldW;
      vy = (e.clientY - r.top) / r.height * worldH;
      clampView(); request();
    };
    mini.addEventListener('pointerdown', e => { miniDrag = true; mini.setPointerCapture(e.pointerId); jump(e); });
    mini.addEventListener('pointermove', e => { if (miniDrag) jump(e); });
    mini.addEventListener('pointerup', () => { miniDrag = false; });
  }

  // Toolbar.
  root.querySelectorAll('[data-ov]').forEach(btn => {
    btn.addEventListener('click', () => {
      const act = btn.dataset.ov;
      if (act === 'in') setZoom(zi + 1);
      else if (act === 'out') setZoom(zi - 1);
      else if (act === 'reset') setZoom(ZOOM_DEFAULT);
      else if (act === 'labels') {
        showLabels = !showLabels;
        btn.setAttribute('aria-pressed', String(showLabels));
        request();
      } else if (act === 'full') {
        const on = root.classList.toggle('overland-full');
        btn.setAttribute('aria-pressed', String(on));
        resize();
      }
    });
  });
  document.addEventListener('keydown', function onEsc(e) {
    if (!root.isConnected) { document.removeEventListener('keydown', onEsc); return; }
    if (e.key === 'Escape' && root.classList.contains('overland-full')) {
      root.classList.remove('overland-full');
      resize();
    }
  });

  // ── Place finder ─────────────────────────────────────────────────────
  // Every place on the map (areas, and interiors drawn as icons), searchable
  // by name, resref or icon kind (not tileset: the wiki's tileset labels are
  // unreliable, e.g. tin01 houses are labelled "Crypt"). Clicking one flies
  // the map to it.
  const side = root.querySelector('.overland-side');
  const search = root.querySelector('.overland-search');
  const list = root.querySelector('.overland-results');
  const count = root.querySelector('.overland-count');
  const findBtn = root.querySelector('[data-ov="find"]');
  const FIND_KEY = 'overland-find';
  const fold = s => s.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
  const TERRAIN_NAME = { g: 'grassland', f: 'forest', h: 'hills', m: 'mountains',
                         d: 'desert', t: 'tundra', s: 'swamp', r: 'rock' };
  const places = [];
  {
    const at = new Map();
    for (const s of world.sites) if (!at.has(s.area)) at.set(s.area, { x: s.x + 0.5, y: s.y + 0.5, icon: s.icon });
    for (const [a, lx, ly] of world.labels) if (!at.has(a)) at.set(a, { x: lx, y: ly, icon: null });
    for (const [a, p] of at) {
      const row = world.areas[a];
      const name = row[1].trim() || row[0];
      const code = world.at(Math.floor(p.x), Math.floor(p.y));
      const kind = p.icon && p.icon !== 'portal' ? p.icon : (TERRAIN_NAME[code] || 'region');
      const tele = world.calloutOf.has(a);
      places.push({ area: a, name, x: p.x, y: p.y, icon: p.icon, code, kind, tele,
                    fname: fold(name).replace(/^[^a-z0-9]+/, ''),
                    key: fold([name, row[0], kind, tele ? 'teleport' : ''].join(' ')) });
    }
    places.sort((a, b) => a.fname.localeCompare(b.fname));
  }
  let iconURL = null;
  let results = [], sel = -1;

  function sideOpen() { return side && !side.hidden; }
  function setSide(on, remember) {
    if (!side) return;
    side.hidden = !on;
    if (findBtn) findBtn.setAttribute('aria-pressed', String(on));
    if (remember) { try { localStorage.setItem(FIND_KEY, on ? '1' : '0'); } catch (e) { /* private mode */ } }
    if (on && !list.childElementCount) renderResults();
  }

  function renderResults() {
    if (!iconURL) {
      iconURL = {};
      for (const [n, cv] of Object.entries(sprites)) iconURL[n] = cv.toDataURL();
    }
    const words = fold(search.value.trim()).split(/\s+/).filter(Boolean);
    results = places.filter(p => words.every(w => p.key.includes(w)));
    if (words.length) {
      const w0 = words[0];
      const rank = p => p.fname.startsWith(w0) ? 0 : p.fname.includes(w0) ? 1 : 2;
      results.sort((a, b) => rank(a) - rank(b) || a.fname.localeCompare(b.fname));
    }
    list.textContent = '';
    const frag = document.createDocumentFragment();
    results.forEach((p, i) => {
      const li = document.createElement('li');
      li.setAttribute('role', 'option');
      li.dataset.i = String(i);
      li.title = p.name;
      const ic = document.createElement('span');
      ic.className = 'ov-ic';
      if (p.icon && iconURL[p.icon]) {
        const im = document.createElement('img');
        im.src = iconURL[p.icon]; im.alt = ''; im.width = 16; im.height = 16;
        ic.appendChild(im);
      } else {
        ic.classList.add('ov-sw');
        ic.style.background = MINI[p.code] || MINI.g;
      }
      const tx = document.createElement('span');
      tx.className = 'ov-text';
      const nm = document.createElement('span');
      nm.className = 'ov-name';
      nm.textContent = (p.tele ? '✦ ' : '') + p.name;
      if (p.tele) nm.classList.add('ov-tele');
      const kd = document.createElement('span');
      kd.className = 'ov-kind';
      kd.textContent = p.kind + (p.tele ? ' · teleport' : '');
      tx.append(nm, kd);
      const go = document.createElement('a');
      go.className = 'ov-open';
      go.href = world.areas[p.area][3];
      go.textContent = '↗';
      go.title = 'Open the page for ' + p.name;
      go.setAttribute('aria-label', go.title);
      li.append(ic, tx, go);
      frag.appendChild(li);
    });
    list.appendChild(frag);
    count.textContent = words.length
      ? results.length + (results.length === 1 ? ' match' : ' matches')
      : places.length + ' places';
    sel = -1;
  }

  function select(i) {
    const items = list.children;
    if (sel >= 0 && items[sel]) items[sel].classList.remove('selected');
    sel = i;
    if (sel >= 0 && items[sel]) {
      items[sel].classList.add('selected');
      items[sel].scrollIntoView({ block: 'nearest' });
    }
  }

  // Centre of the part of the map the panel doesn't cover, as an x offset.
  function panelShift() { return sideOpen() ? (side.offsetLeft + side.offsetWidth) / 2 : 0; }

  let looping = false;
  function loop(now) {
    if (!root.isConnected) { looping = false; return; }
    if (fly) {
      const k = Math.min(1, (now - fly.t0) / fly.dur);
      const e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
      vx = fly.sx + (fly.tx - fly.sx) * e;
      vy = fly.sy + (fly.ty - fly.sy) * e;
      clampView();
      if (k >= 1) fly = null;
    }
    if (focus && now > focus.until) focus = null;
    draw();
    if (fly || focus) requestAnimationFrame(loop);
    else looping = false;
  }

  function focusPlace(p) {
    if (zoom() < 1.5) zi = ZOOMS.indexOf(2);
    const now = performance.now();
    fly = { sx: vx, sy: vy, tx: p.x * TW - panelShift() / zoom(), ty: p.y * TH, t0: now, dur: 550 };
    focus = { x: p.x, y: p.y, until: now + 3200 };
    hover = { area: p.area, site: world.siteAt.get(Math.floor(p.y) * world.W + Math.floor(p.x)) || null };
    if (!looping) { looping = true; requestAnimationFrame(loop); }
  }

  if (side && search && list) {
    let open0 = wrap.clientWidth >= 760;
    try { const v = localStorage.getItem(FIND_KEY); if (v !== null) open0 = v === '1'; } catch (e) { /* ignore */ }
    setSide(open0, false);
    if (open0) vx -= panelShift() / zoom();   // start view centred in the uncovered part
    search.addEventListener('input', renderResults);
    search.addEventListener('keydown', e => {
      if (e.key === 'ArrowDown') { e.preventDefault(); select(Math.min(results.length - 1, sel + 1)); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); select(Math.max(0, sel - 1)); }
      else if (e.key === 'Enter') {
        e.preventDefault();
        const p = results[sel >= 0 ? sel : 0];
        if (p) { if (sel < 0) select(0); focusPlace(p); }
      } else if (e.key === 'Escape' && search.value) {
        e.stopPropagation();
        search.value = '';
        renderResults();
      }
    });
    list.addEventListener('click', e => {
      if (e.target.closest('a')) return;          // ↗ opens the page normally
      const li = e.target.closest('li');
      if (!li) return;
      const i = +li.dataset.i;
      select(i);
      focusPlace(results[i]);
    });
    side.querySelector('.overland-side-close').addEventListener('click', () => setSide(false, true));
    // Deep link: map/#find=rivendell opens the map flown to the first match.
    const m = /^#find=(.+)$/.exec(location.hash);
    if (m) {
      let q = m[1];
      try { q = decodeURIComponent(q.replace(/\+/g, ' ')); } catch (e) { /* keep raw */ }
      setSide(true, false);
      search.value = q;
      renderResults();
      if (results.length) { select(0); focusPlace(results[0]); }
    }
    if (findBtn) findBtn.addEventListener('click', () => {
      const on = !sideOpen();
      setSide(on, true);
      if (on) search.focus();
    });
  } else if (findBtn) {
    findBtn.hidden = true;
  }

  // Resize, and the water animation while the map is on screen.
  let visible = true;
  if (window.ResizeObserver) {
    const ro = new ResizeObserver(() => { if (!root.isConnected) { ro.disconnect(); return; } resize(); });
    ro.observe(wrap);
  } else {
    window.addEventListener('resize', resize);
  }
  if (window.IntersectionObserver) {
    const io = new IntersectionObserver(es => { visible = es[0].isIntersecting; });
    io.observe(wrap);
  }
  const anim = setInterval(() => {
    if (!root.isConnected) { clearInterval(anim); return; }
    if (!visible || document.hidden || zoom() < 1) return;
    tick++;
    request();
  }, 280);

  resize();
}

function initAll() {
  document.querySelectorAll('.overland').forEach(initOverland);
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initAll);
else initAll();
})();
