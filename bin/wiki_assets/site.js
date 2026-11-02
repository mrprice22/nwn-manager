/* nwn-wiki — site-wide behaviour: nav menus, header-load gate, footer timestamp.
 *
 * The header banner is chosen at parse time by a tiny inline script in the page
 * (see page() in bin/nwn-wiki) so the browser only ever fetches one image and
 * lays out with the right box. This file handles everything after that.
 *
 * Wrapped in an IIFE: some pages inject their own script (e.g. the item search
 * on items/search.html), so nothing here should reach the global scope. */
(function () {
"use strict";

/* ── Nav gate ──────────────────────────────────────────────────────────────
 * page() stamps html.nav-pending, which hard-blocks every dropdown. Menus stay
 * locked until the header banner has loaded, so they can't expand and then get
 * shoved around when the header settles. Released defensively: a missing or
 * broken banner must never leave the nav permanently dead. */
function releaseNav() {
  document.documentElement.classList.remove('nav-pending');
}

(function gateNav() {
  const img = document.querySelector('img.site-header-img');
  if (!img) { releaseNav(); return; }
  if (img.complete && img.naturalWidth) { releaseNav(); return; }
  img.addEventListener('load', releaseNav);
  img.addEventListener('error', releaseNav);
  window.addEventListener('load', releaseNav);
  setTimeout(releaseNav, 2000);   // safety net: dead URL, stalled connection
})();

/* ── Nav menus ─────────────────────────────────────────────────────────────
 * Open state is the .nav-open class; each node carries its own close timer.
 * Entering a menu closes its siblings immediately, so hovering a different
 * top-level menu collapses whatever was left sticky from the previous one. */
document.addEventListener("DOMContentLoaded", () => {
  const nav = document.querySelector('header.site-header nav');
  if (nav) initNav(nav);
  initPage();
  initRadio();
  if (RADIO.src) initSoftNav();
});

/* Everything that belongs to one page's <main> / <footer>. Runs on the first
 * load and again after every in-place page swap (see initSoftNav), so each of
 * these must find its own elements and tolerate being called more than once. */
function initPage() {
  initMap();
  initFooterTimestamp();
  initOnlineStatus();
  syncRadioOptOut();
}

/* ── Radio ─────────────────────────────────────────────────────────────────
 * An optional web-radio player, as an iframe docked along the bottom of every
 * page or sitting in the header nav. The player remembers each visitor's
 * station by itself; the site only places it, and lets the visitor minimise
 * it to a single music-note button (remembered in localStorage).
 *
 * RADIO is written into the copied assets/site.js by nwn-wiki from
 * --radio-url / --radio-default-station / --radio-placement / --radio-start:
 *   {src: player URL, placement: 'bottom' | 'header',
 *    start: 'open' | 'minimised'  (how a visitor who never chose sees it)}
 * Empty, as here in the source, means no radio and no in-place navigation:
 * every build without the flag ships this line untouched. The marker comment
 * is what the generator matches on. */
const RADIO = {};  // @nwn-wiki:radio

const RADIO_COLLAPSED_KEY = 'radio-collapsed';
let radioDock = null;

function initRadio() {
  if (!RADIO.src) return;
  const placement = RADIO.placement === 'header' ? 'header' : 'bottom';
  const nav = document.querySelector('header.site-header nav');
  const host = placement === 'header' ? nav : document.body;
  if (!host) return;

  const dock = document.createElement(placement === 'header' ? 'span' : 'div');
  dock.className = 'radio-dock';
  dock.dataset.placement = placement;

  const f = document.createElement('iframe');
  f.className = 'radio-frame';
  f.title = 'Radio';
  // Required: lets the player start sound without a click of its own when it
  // carries playback over a full page load (the fallback when the in-place
  // swap below can't be used). No loading="lazy": it would only delay it.
  f.allow = 'autoplay; encrypted-media';
  f.src = RADIO.src;

  const collapse = document.createElement('button');
  collapse.type = 'button';
  collapse.className = 'radio-collapse';
  collapse.title = 'Minimise radio';
  collapse.setAttribute('aria-label', 'Minimise radio');
  collapse.innerHTML = '<span aria-hidden="true">&#x2013;</span>';

  const expand = document.createElement('button');
  expand.type = 'button';
  expand.className = 'radio-expand';
  expand.title = 'Show radio';
  expand.setAttribute('aria-label', 'Show radio');
  expand.innerHTML = '<span aria-hidden="true">&#x266B;</span>';

  dock.append(f, collapse, expand);
  host.appendChild(dock);
  radioDock = dock;

  const root = document.documentElement;
  root.classList.add('has-radio', 'radio-' + placement);
  // A visitor's own choice ('1' / '0') wins; until they make one, the site's
  // RADIO.start decides. That first state is deliberately not stored, so a
  // later change of the site default still reaches everyone who never chose.
  let stored = null;
  try { stored = localStorage.getItem(RADIO_COLLAPSED_KEY); } catch (e) {}
  const apply = (c) => root.classList.toggle('radio-collapsed', c);
  apply(stored === null ? RADIO.start === 'minimised' : stored === '1');
  const choose = (c) => {
    apply(c);
    try { localStorage.setItem(RADIO_COLLAPSED_KEY, c ? '1' : '0'); } catch (e) {}
  };
  collapse.addEventListener('click', () => { choose(true); expand.focus(); });
  expand.addEventListener('click', () => { choose(false); collapse.focus(); });

  // The player posts its playback state, so the minimised note can glow while
  // music is on. Only messages from our own iframe count.
  window.addEventListener('message', (e) => {
    if (e.source !== f.contentWindow || !e.data || e.data.type !== 'slightly-epic-radio:state') return;
    dock.dataset.state = String(e.data.state || '');
  });
  syncRadioOptOut();
}

/* Per-page opt-out: <main data-no-radio>. Hidden, not removed, so music that
 * is already playing carries on underneath an opted-out page. */
function syncRadioOptOut() {
  document.documentElement.classList.toggle(
    'radio-off', !!document.querySelector('main[data-no-radio]'));
}

/* ── In-place navigation ───────────────────────────────────────────────────
 * Any full page load reloads the radio iframe, and Firefox / Safari will not
 * let the new copy start sound without a click. So while a radio is
 * configured, same-site links swap the page in place instead: fetch the page,
 * replace <main>, <footer> and the title, push the URL, re-run initPage().
 * The header (and the radio in it, or docked below) is never touched, so
 * playback never stops.
 *
 * Anything unusual falls back to an ordinary navigation: other origins, new
 * tabs, downloads, non-HTML files, a fetch or parse failure, or a page that is
 * not one of ours. The fallback is always safe, just not gap-free. */
let softNavSeq = 0;
let softNavPath = '';

function initSoftNav() {
  if (!window.fetch || !window.DOMParser || !history.pushState) return;
  // Header links are relative to the page they were written for; once the URL
  // starts changing underneath them they would point at the wrong depth.
  document.querySelectorAll('header.site-header a[href]').forEach(a => {
    a.setAttribute('href', a.href);
  });
  if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
  softNavPath = pathKey(location.href);
  history.replaceState({ softnav: 1, y: window.scrollY }, '', location.href);

  // Remember where each entry was scrolled to, for back / forward.
  let saveTimer = 0;
  window.addEventListener('scroll', () => {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      const st = history.state;
      if (st && st.softnav) history.replaceState({ softnav: 1, y: window.scrollY }, '', location.href);
    }, 150);
  }, { passive: true });

  document.addEventListener('click', (e) => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const a = e.target.closest && e.target.closest('a[href]');
    if (!a || a.hasAttribute('download')) return;
    if (a.target && a.target !== '_self') return;
    const url = new URL(a.href, location.href);
    if (!softNavTarget(url)) return;
    // Same page, different #anchor: the browser's own jump is right.
    if (pathKey(url.href) === softNavPath && url.hash) return;
    e.preventDefault();
    softNavigate(url.href, { push: true });
  });

  window.addEventListener('popstate', (e) => {
    // A hash-only history entry on the same page: nothing to load.
    if (pathKey(location.href) === softNavPath) return;
    softNavigate(location.href, { push: false, y: e.state && e.state.y });
  });
}

function pathKey(href) {
  const u = new URL(href, location.href);
  return u.origin + u.pathname + u.search;
}

function softNavTarget(url) {
  if (url.origin !== location.origin) return false;
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return false;
  // Directory URLs and .html pages only; .json, .png, .zip ... load normally.
  const last = url.pathname.split('/').pop();
  return !last.includes('.') || /\.html?$/i.test(last);
}

async function softNavigate(href, opts) {
  const seq = ++softNavSeq;
  const full = () => { if (seq === softNavSeq) location.assign(href); };
  let doc, finalUrl;
  try {
    const res = await fetch(href, { headers: { Accept: 'text/html' } });
    const type = res.headers.get('content-type') || '';
    if (!res.ok || !/text\/html/i.test(type)) return full();
    const text = await res.text();
    if (seq !== softNavSeq) return;            // a newer click won
    doc = new DOMParser().parseFromString(text, 'text/html');
    // Only our own pages: same shell, same site.js.
    if (!doc.querySelector('main') || !doc.querySelector('script[src$="assets/site.js"]')) return full();
    const hash = new URL(href, location.href).hash;
    finalUrl = res.url ? res.url.replace(/#.*$/, '') + hash : href;
  } catch (err) {
    return full();
  }

  try {
    // URL first: relative src/href inside the new content resolve against it.
    if (opts.push) history.pushState({ softnav: 1, y: 0 }, '', finalUrl);
    softNavPath = pathKey(location.href);

    document.title = doc.title;
    const main = document.querySelector('main');
    const newMain = document.adoptNode(doc.querySelector('main'));
    main.replaceWith(newMain);
    const footer = document.querySelector('body > footer');
    const newFooter = doc.querySelector('body > footer');
    if (footer && newFooter) footer.replaceWith(document.adoptNode(newFooter));

    runPageScripts(newMain);
    initPage();

    const id = decodeURIComponent(location.hash.slice(1));
    const target = id && document.getElementById(id);
    if (typeof opts.y === 'number') window.scrollTo(0, opts.y);
    else if (target) target.scrollIntoView();
    else window.scrollTo(0, 0);
  } catch (err) {
    // Half-swapped is worse than a reload: start this page over properly.
    console.error('[nwn-wiki] in-place navigation failed; reloading', err);
    location.reload();
  }
}

/* Scripts inserted by innerHTML / adoptNode never run, so each page script in
 * the new <main> is re-created to execute it. Page scripts are written for a
 * fresh load and wait for DOMContentLoaded, which has long since fired, so
 * those listeners are collected while the scripts run and called right after.
 * Data blocks (type="application/json") are left alone. */
function runPageScripts(root) {
  const ready = [];
  const add = document.addEventListener;
  document.addEventListener = function (type, fn, o) {
    if (type === 'DOMContentLoaded') { ready.push(fn); return; }
    return add.call(this, type, fn, o);
  };
  try {
    root.querySelectorAll('script').forEach(old => {
      const type = (old.getAttribute('type') || '').trim().toLowerCase();
      if (type && !/^(text|application)\/(java|ecma)script$|^module$/.test(type)) return;
      const s = document.createElement('script');
      for (const at of old.attributes) s.setAttribute(at.name, at.value);
      s.textContent = old.textContent;
      old.replaceWith(s);
    });
  } finally {
    document.addEventListener = add;
  }
  ready.forEach(fn => {
    try { fn.call(document, new Event('DOMContentLoaded')); }
    catch (err) { console.error(err); }
  });
}

function graceMs() {
  const raw = getComputedStyle(document.documentElement)
    .getPropertyValue('--nav-grace').trim();
  const n = parseFloat(raw);
  if (!isFinite(n)) return 300;
  return raw.endsWith('ms') ? n : n * 1000;
}

function initNav(nav) {
  const timers = new WeakMap();

  const cancel = (node) => {
    const t = timers.get(node);
    if (t) { clearTimeout(t); timers.delete(node); }
  };

  // Every menu node at the same level as `node`, within the same parent menu.
  const siblings = (node) => {
    const scope = node.parentElement;
    const sel = node.classList.contains('nav-dropdown') ? '.nav-dropdown' : '.nav-submenu';
    return Array.from(scope.children).filter(el => el !== node && el.matches(sel));
  };

  const close = (node) => {
    cancel(node);
    node.classList.remove('nav-open');
    node.querySelectorAll('.nav-submenu').forEach(sub => {
      cancel(sub);
      sub.classList.remove('nav-open');
    });
  };

  const open = (node) => {
    cancel(node);
    siblings(node).forEach(close);
    // Keep the whole ancestor chain open and un-timed while we're inside it.
    for (let p = node.parentElement; p && p !== nav; p = p.parentElement) {
      if (p.matches('.nav-dropdown, .nav-submenu')) {
        cancel(p);
        p.classList.add('nav-open');
      }
    }
    node.classList.add('nav-open');
  };

  const closeAll = () => nav.querySelectorAll('.nav-dropdown').forEach(close);

  nav.querySelectorAll('.nav-dropdown, .nav-submenu').forEach(node => {
    node.addEventListener('mouseenter', () => open(node));
    node.addEventListener('mouseleave', () => {
      cancel(node);
      timers.set(node, setTimeout(() => close(node), graceMs()));
    });
  });

  // Collapse before navigating, so the next page doesn't paint an open menu.
  nav.addEventListener('click', (e) => {
    if (e.target.closest('a')) closeAll();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeAll();
  });
  document.addEventListener('pointerdown', (e) => {
    if (!e.target.closest('header.site-header nav')) closeAll();
  });
}

/* ── Search engine ─────────────────────────────────────────────────────────
 * items/search.html and creatures/search.html run the same machine over two
 * different JSON indexes: fetch the index, fill the property dropdowns, cascade
 * property → subtype, read N condition rows on submit, filter, sort, render.
 * Only the page-specific parts are supplied by the caller, which is why this
 * lives here instead of being pasted into both pages.
 *
 * cfg:
 *   formId, resultsId  element ids of the <form> and the results container
 *   ids                {prop, sub, min, mode?} id prefixes of the condition
 *                      rows (row n uses prop+n etc.); mode is the optional
 *                      has/lacks select (creatures only)
 *   rows               number of condition rows (default 4)
 *   indexUrl           JSON index to fetch (default 'search-index.json')
 *   keys               where the property data lives: {list, prop, sub, val}
 *                      i.e. record[keys.list] is an array of property objects
 *                      whose fields are p[keys.prop] / p[keys.sub] / p[keys.val]
 *   fillFirstMatch     when a record matched no condition, show its first
 *                      property anyway (items do this, creatures don't)
 *   populate(data,u)   fill the page's own dropdowns (called after the index
 *                      lands; the property selects are already filled)
 *   onReady(data,u)    optional, called after populate()
 *   readForm(u)        -> f, the page's own form state; must include .asc
 *   filter(rec,f)      -> keep this record? (conditions are applied after)
 *   compare(a,b,f)     sort comparator over {rec, matched} pairs, ascending
 *   render(rows,f,conds,u)  paint the results
 *
 * u is a small toolbox: $, num, esc, fillSel, plus .results and .data. */
function searchUtils(results) {
  const u = {
    results: results,
    data: [],
    $: (id) => document.getElementById(id),
    num: (id) => { const v = parseFloat(u.$(id).value); return isNaN(v) ? null : v; },
    esc: (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                         .replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
    fillSel: (sel, vals) => { vals.forEach(v => sel.appendChild(new Option(v, v))); },
  };
  return u;
}

function initSearch(cfg) {
  const N = cfg.rows || 4;
  const form = document.getElementById(cfg.formId);
  const results = document.getElementById(cfg.resultsId);
  if (!form || !results) return;

  const ids = cfg.ids, keys = cfg.keys, u = searchUtils(results);
  const modeSels = [], propSels = [], subSels = [], minVals = [];
  for (let i = 1; i <= N; i++) {
    if (ids.mode) modeSels.push(u.$(ids.mode + i));
    propSels.push(u.$(ids.prop + i));
    subSels.push(u.$(ids.sub + i));
    minVals.push(u.$(ids.min + i));
  }
  const propsOf = (rec) => rec[keys.list] || [];
  const subs = {};

  fetch(cfg.indexUrl || 'search-index.json').then(r => r.json()).then(d => {
    u.data = d;
    const props = {};
    d.forEach(rec => propsOf(rec).forEach(p => {
      props[p[keys.prop]] = 1;
      if (p[keys.sub]) {
        if (!subs[p[keys.prop]]) subs[p[keys.prop]] = {};
        subs[p[keys.prop]][p[keys.sub]] = 1;
      }
    }));
    const propOpts = Object.keys(props).sort();
    propSels.forEach(sel => u.fillSel(sel, propOpts));
    if (cfg.populate) cfg.populate(d, u);
    if (cfg.onReady) cfg.onReady(d, u);
  });

  propSels.forEach((sel, idx) => {
    sel.addEventListener('change', () => {
      const sub = subs[sel.value] || {};
      subSels[idx].innerHTML = '<option value="">— any —</option>';
      Object.keys(sub).sort().forEach(s => subSels[idx].appendChild(new Option(s, s)));
      subSels[idx].disabled = !sel.value || !Object.keys(sub).length;
    });
  });

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const f = cfg.readForm ? cfg.readForm(u) : {};

    const conds = [];
    for (let i = 0; i < N; i++) {
      const prop = propSels[i].value, sub = subSels[i].value;
      const minv = parseInt(minVals[i].value, 10) || 0;
      if (prop || sub || minv > 0) {
        conds.push({prop: prop, sub: sub, minv: minv,
                    neg: !!(modeSels[i] && modeSels[i].value === 'lacks')});
      }
    }

    const out = [];
    u.data.forEach(rec => {
      if (cfg.filter && !cfg.filter(rec, f)) return;
      const matched = [];
      const ok = conds.every(c => {
        const mp = propsOf(rec).filter(p => {
          if (c.prop && p[keys.prop] !== c.prop) return false;
          if (c.sub && p[keys.sub] !== c.sub) return false;
          if (c.minv > 0 && (p[keys.val] || 0) < c.minv) return false;
          return true;
        });
        if (c.neg) return mp.length === 0;
        if (!mp.length) return false;
        matched.push(mp[0]);
        return true;
      });
      if (!ok) return;
      let m = matched;
      if (cfg.fillFirstMatch && !m.length && propsOf(rec).length) m = [propsOf(rec)[0]];
      out.push({rec: rec, matched: m});
    });

    out.sort((a, b) => { const v = cfg.compare(a, b, f); return f.asc ? v : -v; });
    cfg.render(out, f, conds, u);
  });
}
window.initSearch = initSearch;

/* ── Area map: pan/zoom + download ─────────────────────────────────────────
 * The home index and /map embed the same SVG from render_map_svg(), always with
 * the same two ids, at most one map per page — so this finds its own elements
 * and is a no-op on every other page.
 *
 * Panning is the wrap's own overflow:auto scrolling; this only resizes the SVG
 * (the viewBox is never touched) and keeps the point under the cursor fixed. */
function initMap() {
  const wrap = document.getElementById('area-map-wrap');
  const svg = document.getElementById('area-map-svg');
  if (!wrap || !svg) return;

  // Capture the 1:1 baseline size before any resize, then default the view to
  // 20% out (zoom=0.8); the "1:1" reset returns to baseline.
  const baseW = parseFloat(svg.getAttribute('width'));
  const baseH = parseFloat(svg.getAttribute('height'));
  let zoom = 0.8;
  svg.setAttribute('width', Math.round(baseW * zoom));
  svg.setAttribute('height', Math.round(baseH * zoom));
  // Center now that the SVG is at its initial (20%-out) size.
  wrap.scrollLeft = (wrap.scrollWidth - wrap.clientWidth) / 2;
  wrap.scrollTop = (wrap.scrollHeight - wrap.clientHeight) / 2;

  function applyZoom(z, px, py) {
    z = Math.max(0.2, Math.min(5, z));
    if (z === zoom) return;
    px = px !== undefined ? px : wrap.clientWidth / 2;
    py = py !== undefined ? py : wrap.clientHeight / 2;
    const fx = (wrap.scrollLeft + px) / (baseW * zoom);
    const fy = (wrap.scrollTop + py) / (baseH * zoom);
    zoom = z;
    svg.setAttribute('width', Math.round(baseW * zoom));
    svg.setAttribute('height', Math.round(baseH * zoom));
    wrap.scrollLeft = fx * baseW * zoom - px;
    wrap.scrollTop = fy * baseH * zoom - py;
  }
  document.getElementById('area-map-zoom-in').onclick = function () { applyZoom(zoom * 1.25); };
  document.getElementById('area-map-zoom-out').onclick = function () { applyZoom(zoom / 1.25); };
  document.getElementById('area-map-zoom-reset').onclick = function () { applyZoom(1); };
  wrap.addEventListener('wheel', function (e) {
    e.preventDefault();
    const r = wrap.getBoundingClientRect();
    applyZoom(e.deltaY < 0 ? zoom * 1.12 : zoom / 1.12, e.clientX - r.left, e.clientY - r.top);
  }, {passive: false});

  // Download. Called by the toolbar button's onclick attribute, so it has to be
  // global; serialises the live SVG, zoom-resized width/height and all.
  window.downloadAreaMap = function () {
    let src = new XMLSerializer().serializeToString(svg);
    if (!src.match(/^<\?xml/)) src = '<?xml version="1.0" encoding="UTF-8"?>\n' + src;
    const blob = new Blob([src], {type: 'image/svg+xml;charset=utf-8'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'area-map.svg';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 100);
  };
}

/* ── Who's Online ──────────────────────────────────────────────────────────
 * The only live data on the site. Everything else is static HTML built by
 * nwn-wiki and shipped by a git push; this polls the wiki worker's /api/online,
 * which serves a roster the game server's host pushes every few minutes. The
 * game itself is never contacted, by this page or by anything else.
 *
 * Renders into [data-online-roster] (the Who's Online page body) and updates
 * every [data-online-count] badge. Both are optional: on a realm with no status
 * endpoint the elements are absent and this does nothing, and the fetch failing
 * is a normal offline state, not an error -- the server-rendered fallback text
 * already in the element is left exactly as it is. */
const ONLINE_POLL_MS = 60000;

/* One poll for the whole visit: initPage() re-runs this after an in-place page
 * swap, which must replace the previous page's timer, not add a second one. */
let onlineTimer = 0;

function initOnlineStatus() {
  clearInterval(onlineTimer);
  const roster = document.querySelector('[data-online-roster]');
  const badges = document.querySelectorAll('[data-online-count]');
  if (!roster && !badges.length) return;

  const url = (roster && roster.dataset.onlineUrl) || '/api/online';

  function paint(data) {
    const count = (data && !data.stale && data.count) || 0;
    badges.forEach(b => {
      b.textContent = count ? String(count) : '';
      b.hidden = !count;
    });
    if (!roster) return;

    if (!data || data.stale) {
      /* Deliberately not "0 players online": a stale blob means the pusher or
       * the network is down, and we do not know who is on. */
      roster.innerHTML = '<p class="muted">Live status is unavailable right now.</p>';
      return;
    }
    if (!data.count) {
      roster.innerHTML = '<p class="muted">Nobody is online right now.</p>';
      return;
    }
    const rows = data.players.map(p => {
      const who = p.character || p.player || '';
      const by = p.character && p.player ? p.player : '';
      const lvl = p.level ? 'Level ' + p.level : '';
      return '<tr><td>' + linkTo('characters', who) + '</td><td>' +
             linkTo('players', by) + '</td><td>' + esc(lvl) + '</td></tr>';
    }).join('');
    roster.innerHTML =
      '<p>' + data.count + (data.count === 1 ? ' player' : ' players') +
      ' online' + (data.updated_at ? ' <span class="muted">(as of ' +
        esc(new Date(data.updated_at).toLocaleTimeString()) + ')</span>' : '') +
      '</p><table class="data"><thead><tr><th>Character</th><th>Player</th>' +
      '<th>Level</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }

  function esc(s) {
    const d = document.createElement('span');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  /* The roster can describe a different realm than the wiki showing it -- the
   * TEST wiki deliberately shows who is on the live server. The page ships an
   * index of the names THIS wiki has pages for, so a name links only when the
   * target actually exists here and renders as plain text otherwise. That is
   * why names are not clickable on the TEST wiki and are on the live one. */
  const linkIndex = (() => {
    const el = document.getElementById('online-link-index');
    if (!el) return {characters: {}, players: {}};
    try {
      const d = JSON.parse(el.textContent);
      return {characters: d.characters || {}, players: d.players || {}};
    } catch { return {characters: {}, players: {}}; }
  })();

  function linkTo(kind, name) {
    if (!name) return '';
    const href = linkIndex[kind][name];
    return href ? '<a href="' + esc(href) + '">' + esc(name) + '</a>' : esc(name);
  }

  function tick() {
    fetch(url, { cache: 'no-store' })
      .then(r => (r.ok ? r.json() : null))
      .then(paint)
      .catch(() => paint(null));
  }

  tick();
  onlineTimer = setInterval(tick, ONLINE_POLL_MS);
}

function initFooterTimestamp() {
  const el = document.getElementById('wiki-generated-at');
  // Pages that bake a server-side timestamp (e.g. activity, server firsts) leave
  // the span pre-filled; only the JS-driven pages have it empty. Skip the rest.
  if (!el || el.textContent.trim()) return;
  fetch(el.dataset.metaUrl || 'assets/meta.json')
    .then(r => r.json())
    .then(({generated_at}) => { el.textContent = 'last updated ' + generated_at; })
    .catch(() => {});
}

})();
