document.addEventListener("DOMContentLoaded", () => {
  const el = document.getElementById('wiki-generated-at');
  // Pages that bake a server-side timestamp (e.g. activity, server firsts) leave
  // the span pre-filled; only the JS-driven pages have it empty. Skip the rest.
  if (!el || el.textContent.trim()) return;
  fetch(el.dataset.metaUrl || 'assets/meta.json')
    .then(r => r.json())
    .then(({generated_at}) => { el.textContent = 'last updated ' + generated_at; })
    .catch(() => {});
});
