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
  initFooterTimestamp();
});

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
