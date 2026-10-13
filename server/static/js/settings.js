// Station settings (admins): edit overrides; show what the Core is running.
(function () {
  'use strict';
  const R = window.Radio, $ = R.$, esc = R.esc;
  let data = null;
  const reset = new Set();
  const fmt = (v) => (v === null || v === undefined) ? '–' : (v === '' ? '(none)' : String(v));
  const effective = (s) => (s.override != null ? s.override : s.default);

  function render() {
    const groups = {};
    data.settings.forEach((s) => { (groups[s.group] = groups[s.group] || []).push(s); });
    $('groups').innerHTML = Object.keys(groups).map((g) => '<div class="set-group"><div class="subhead" style="margin-top:0">' + esc(g) + '</div>' +
      groups[g].map((s) => {
        const eff = effective(s), num = s.type !== 'dir';
        // differs after the Core applied the latest settings = radio.lua's own fallback was edited
        const core = s.running == null ? '–' : (String(s.running) === String(eff) ? esc(fmt(s.running))
          : '<span class="diff">' + esc(fmt(s.running)) + (data.in_sync && s.override == null ? ' (radio.lua default; set it here to change)' : ' (updating)') + '</span>');
        return '<div class="set-row"><div><div class="nm">' + esc(s.label) + '</div><div class="state">' +
          (s.override != null ? '<span class="ovr">set here</span>' : 'default') + ' · Core: ' + core + '</div></div>' +
          '<div><input name="' + esc(s.name) + '" ' + (num ? 'type="number" required step="' + s.step + '" min="' + s.min + '" max="' + s.max + '"' : 'type="text"') +
          ' value="' + esc(eff) + '" data-orig="' + esc(eff) + '">' +
          (s.override != null ? '<button type="button" class="linkbtn" data-reset="' + esc(s.name) + '">reset to default (' + esc(fmt(s.default)) + ')</button>' : '') + '</div>' +
          '<div class="help">' + esc(s.help) + (num ? ' Range ' + s.min + '–' + s.max + '.' : '') + '</div></div>';
      }).join('') + '</div>').join('');
    syncBanner();
  }

  function syncBanner() {
    const b = $('sync');
    if (!data.core_rev) { b.className = 'banner wait'; b.textContent = 'No Core has reported yet. Settings are sent when it connects.'; }
    else if (data.in_sync) { b.className = 'banner ok'; b.textContent = '✓ The Core is running these settings.'; }
    else { b.className = 'banner wait'; b.textContent = '… Waiting for the Core to apply the latest settings (it syncs every second).'; }
  }

  async function load() {
    try {
      data = await R.api(R.withStation('/api/v1/settings'));
      reset.clear();
      render();
    } catch (e) { R.toast('Settings: ' + e.message, 'bad'); }
  }

  // refresh the "Core: ..." column without clobbering edits in progress
  async function watch() {
    try {
      if (data && !document.querySelector('#groups input.changed')) {
        const d = await R.api(R.withStation('/api/v1/settings'));
        if (d.rev !== data.rev || d.core_rev !== data.core_rev || JSON.stringify(d.settings) !== JSON.stringify(data.settings)) { data = d; render(); }
      }
    } catch (e) { /* the header pill shows connection problems */ }
    setTimeout(watch, 3000);
  }

  $('groups').addEventListener('input', (e) => {
    if (e.target.matches('input')) { e.target.classList.toggle('changed', e.target.value !== e.target.dataset.orig); reset.delete(e.target.name); }
  });
  $('groups').addEventListener('click', (e) => {
    const b = e.target.closest('[data-reset]');
    if (!b) return;
    const s = data.settings.find((x) => x.name === b.dataset.reset);
    const inp = document.querySelector('#groups input[name="' + s.name + '"]');
    inp.value = s.default;
    inp.classList.add('changed');
    reset.add(s.name);
  });
  $('btn-revert').addEventListener('click', load);
  $('set-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const out = {};
    document.querySelectorAll('#groups input').forEach((inp) => {
      if (reset.has(inp.name)) out[inp.name] = null;               // back to the radio.lua default
      else if (inp.value !== inp.dataset.orig) out[inp.name] = inp.type === 'number' ? Number(inp.value) : inp.value;
    });
    if (!Object.keys(out).length) { R.toast('Nothing changed.'); return; }
    try {
      const r = await R.post('/api/v1/settings', { settings: out });
      R.toast(r.changed.length ? 'Saved: ' + r.changed.join(', ') : 'Nothing changed.', 'ok');
      load();
    } catch (err) { R.toast('Not saved: ' + err.message, 'bad'); }
  });

  load();
  watch();
  R.status(5000, 0);
})();
