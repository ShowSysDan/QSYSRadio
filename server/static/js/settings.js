// Settings (admins): station settings (sent to the Core; shows what it runs)
// and server settings (syslog; shows what this server uses).
(function () {
  'use strict';
  const R = window.Radio, $ = R.$, esc = R.esc;
  const fmt = (v) => (v === null || v === undefined) ? '–' : (v === '' ? '(none)' : String(v));
  const effective = (s) => (s.override != null ? s.override : s.default);

  // one settings form: el = the groups container, url = its API, where = what the "running" column is
  function section(el, form, url, where, after) {
    const sec = { data: null, reset: new Set() };

    function field(s, eff) {
      const attrs = ' id="' + el.id + '-' + esc(s.name) + '" name="' + esc(s.name) + '" data-orig="' + esc(eff) + '"';
      if (s.type === 'choice') {
        return '<select' + attrs + '>' + s.choices.map((c) => '<option' + (String(c) === String(eff) ? ' selected' : '') + '>' + esc(c) + '</option>').join('') + '</select>';
      }
      const num = s.type === 'int' || s.type === 'float';
      return '<input' + attrs + (num ? ' type="number" required step="' + s.step + '" min="' + s.min + '" max="' + s.max + '"' : ' type="text"') + ' value="' + esc(eff) + '">';
    }
    // the unit, from the setting's name (…_s, …_db, …_dbfs, …_min)
    const unit = (name) => ({ s: 's', db: 'dB', dbfs: 'dBFS', min: 'min' }[(name.match(/_(s|db|dbfs|min)$/) || [])[1]] || '');
    const TINTS = ['blue', 'amber', 'cyan', '', 'purple', 'pink', 'red'];

    // unsaved changes: counted on the save bar, which stays in view while there are any
    function dirty() {
      const n = el.querySelectorAll('.changed').length, bar = form.querySelector('.form-actions');
      bar.classList.toggle('dirty', n > 0);
      bar.querySelector('.unsaved').textContent = n ? n + ' unsaved change' + (n === 1 ? '' : 's') : '';
    }

    sec.render = () => {
      const d = sec.data, groups = {};
      d.settings.forEach((s) => { (groups[s.group] = groups[s.group] || []).push(s); });
      // one box per group, in columns; each setting: name and field, what it does, where it comes from
      el.innerHTML = Object.keys(groups).map((g, i) => {
        const set = groups[g].filter((s) => s.override != null).length;
        return '<section class="tile set-card ' + TINTS[i % TINTS.length] + '"><div class="t-head"><div class="t-label">' + esc(g) + '</div>' +
          (set ? '<span class="tag ok" title="Settings in this group set here (not the default)">' + set + ' set here</span>' : '') + '</div>' +
          groups[g].map((s) => {
            const eff = effective(s), num = s.type === 'int' || s.type === 'float', u = unit(s.name);
            // differs after the Core applied the latest settings = radio.lua's own fallback was edited
            const run = s.running == null ? '–' : (String(s.running) === String(eff) ? esc(fmt(s.running))
              : '<span class="diff">' + esc(fmt(s.running)) + (d.in_sync && s.override == null ? ' (radio.lua default; set it here to change)' : ' (updating)') + '</span>');
            return '<div class="set-row' + (s.override != null ? ' is-set' : '') + '"><label class="nm" for="' + el.id + '-' + esc(s.name) + '">' + esc(s.label) + '</label>' +
              '<div class="field">' + field(s, eff) +
              '<span class="unit">' + esc(u) + '</span></div>' +
              '<div class="help">' + esc(s.help) + (num ? ' Range ' + s.min + '–' + s.max + (u ? ' ' + u : '') + '.' : '') + '</div>' +
              '<div class="state">' + (s.override != null ? '<span class="ovr">set here</span>' : 'default') + ' · ' + (s.server_only ? 'server' : where) + ': ' + run +
              (s.override != null ? ' <button type="button" class="linkbtn" data-reset="' + esc(s.name) + '">reset to default (' + esc(fmt(s.default)) + ')</button>' : '') +
              '</div></div>';
          }).join('') + '</section>';
      }).join('');
      dirty();
      if (after) after(d);
    };

    sec.load = async () => {
      try {
        sec.data = await R.api(R.withStation(url));
        sec.reset.clear();
        sec.render();
      } catch (e) { R.toast('Settings: ' + e.message, 'bad'); }
    };

    // refresh the "running" column without clobbering edits in progress
    sec.watch = async () => {
      try {
        if (sec.data && !el.querySelector('.changed')) {
          const d = await R.api(R.withStation(url));
          if (JSON.stringify(d) !== JSON.stringify(sec.data)) { sec.data = d; sec.render(); }
        }
      } catch (e) { /* the header pill shows connection problems */ }
    };

    const mark = (e) => {
      const t = e.target;
      if (t.matches('input, select')) { t.classList.toggle('changed', t.value !== t.dataset.orig); sec.reset.delete(t.name); dirty(); }
    };
    el.addEventListener('input', mark);
    el.addEventListener('change', mark);
    el.addEventListener('click', (e) => {
      const b = e.target.closest('[data-reset]');
      if (!b) return;
      const s = sec.data.settings.find((x) => x.name === b.dataset.reset);
      const inp = el.querySelector('[name="' + s.name + '"]');
      inp.value = s.default;
      inp.classList.add('changed');
      sec.reset.add(s.name);
      dirty();
    });
    form.querySelector('[data-revert]').addEventListener('click', sec.load);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const out = {};
      el.querySelectorAll('input, select').forEach((inp) => {
        if (sec.reset.has(inp.name)) out[inp.name] = null;           // back to the default
        else if (inp.value !== inp.dataset.orig) out[inp.name] = inp.type === 'number' ? Number(inp.value) : inp.value;
      });
      if (!Object.keys(out).length) { R.toast('Nothing changed.'); return; }
      try {
        const r = await R.post(url, { settings: out });
        R.toast(r.changed.length ? 'Saved: ' + r.changed.join(', ') : 'Nothing changed.', 'ok');
        sec.load();
      } catch (err) { R.toast('Not saved: ' + err.message, 'bad'); }
    });
    return sec;
  }

  const station = section($('groups'), $('set-form'), '/api/v1/settings', 'Core', (d) => {
    const b = $('sync');
    if (!d.core_rev) { b.className = 'banner wait'; b.textContent = 'No Core has reported yet. Settings are sent when it connects.'; }
    else if (d.in_sync) { b.className = 'banner ok'; b.textContent = '✓ The Core is running these settings.'; }
    else { b.className = 'banner wait'; b.textContent = '… Waiting for the Core to apply the latest settings (it syncs every second).'; }
  });
  const server = section($('srv-groups'), $('srv-form'), '/api/v1/server-settings', 'in use');
  $('btn-syslog-test').addEventListener('click', async () => {
    try {
      const r = await R.post('/api/v1/server-settings/test', {});
      R.toast('Test message sent to ' + r.sent_to + '. Check your syslog server.', 'ok');
    } catch (e) { R.toast('Test: ' + e.message, 'bad'); }
  });

  const sections = [station, server];
  sections.forEach((s) => s.load());
  (function watch() { Promise.all(sections.map((s) => s.watch())).finally(() => setTimeout(watch, 3000)); })();
  R.status(5000, 0);
})();
