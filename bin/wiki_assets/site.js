document.addEventListener("DOMContentLoaded", () => {
  const base = document.querySelector('base[href]')?.getAttribute('href') ?? '';
  fetch(base + 'assets/meta.json')
    .then(r => r.json())
    .then(({generated_at}) => {
      const el = document.getElementById('wiki-generated-at');
      if (el && !el.textContent.trim()) el.textContent = 'last updated ' + generated_at;
    })
    .catch(() => {});
});
