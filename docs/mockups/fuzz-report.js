// UI mixed-click regression test for report.html (paste into the page console, or run via browser automation).
// Rule (docs/11-ui.md 1.15, docs/10-verification.md 3-1): run on every UI change, fixed seeds + random seeds.
(function (seed = 1, steps = 500) {
  let x = seed; const rnd = () => ((x = (x * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff);
  const q = s => document.querySelector(s), errs = [], bad = [];
  window.addEventListener('error', e => errs.push(e.message));
  const dev = () => `Tablet-${1 + Math.floor(rnd() * 3)}`;
  const acts = [
    ['preset all', () => q('[data-p="all"]').click()], ['preset first', () => q('[data-p="first"]').click()],
    ['preset last', () => q('[data-p="last"]').click()], ['preset red', () => q('[data-p="red"]').click()],
    ['jump', () => q('[data-jump]').click()], ['red only', () => q('#redOnly').click()],
    ['toggle device', () => { const c = q('#sh-' + dev()); c.checked = !c.checked; c.dispatchEvent(new Event('change', { bubbles: true })); }],
    ['click cell', () => { const c = q(`.cells span[data-dev="${dev()}"][data-i="${Math.floor(rnd() * 288)}"]`); c.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true })); document.dispatchEvent(new PointerEvent('pointerup', { bubbles: true })); }],
    ['from', () => { const f = q('#rFrom'); f.value = String(Math.floor(rnd() * 288)); f.dispatchEvent(new Event('change', { bubbles: true })); }],
    ['to', () => { const t = q('#rTo'); t.value = String(Math.floor(rnd() * 288)); t.dispatchEvent(new Event('change', { bubbles: true })); }],
    ['close chart', () => { const b = q('#closeChart'); if (b) b.click(); }],
  ];
  for (let n = 0; n < steps; n++) {
    const [name, fn] = acts[Math.floor(rnd() * acts.length)]; fn();
    const m = q('#banner').innerText.match(/Showing (\d+)h (\d+)m – (\d+)h (\d+)m/);
    const from = (+m[1] * 60 + +m[2]) / 5, to = (+m[3] * 60 + +m[4]) / 5 - 1;
    // Invariants
    if (from !== +q('#rFrom').value || to !== +q('#rTo').value) bad.push([n, name, 'banner range != selects']);
    const sel = q('.cells span.sel'), open = !q('#chart').hidden;
    if (!!sel !== open) bad.push([n, name, 'selected box and 1-second detail disagree']);
    if (sel && (getComputedStyle(sel).opacity !== '1')) bad.push([n, name, 'selected box is not fully visible']);
    const shown = [...document.querySelectorAll('.lab input')].filter(c => c.checked).length;
    const cards = document.querySelectorAll('#keys .dev').length;
    if (shown !== cards) bad.push([n, name, 'device cards != ticked devices']);
    const g = q('.gridscroll'); if (innerWidth >= 900 && g.scrollWidth > g.clientWidth + 1) bad.push([n, name, 'grid does not fit width']);
  }
  const result = { seed, steps, failures: bad.length, first: bad.slice(0, 10), errors: errs };
  console.log(result); return result;
})
