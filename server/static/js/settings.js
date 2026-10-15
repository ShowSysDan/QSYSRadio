// Settings (admins): station settings, for all stations or for one station on top of them (sent to the Core;
// shows what it runs), and server settings (syslog, clock; shows what this server uses).
(function () {
  'use strict';
  const R = window.Radio, $ = R.$, esc = R.esc;
  const fmt = (v) => (v === null || v === undefined) ? '–' : (v === '' ? '(none)' : String(v));
  const SOURCE = { station: 'this station', all: 'all stations', default: 'default' };
  const unsaved = (el) => !!el.querySelector('.changed');

  // one settings form: el = the groups container, src() = its API url, where = what the "running" column is,
  // body(out) = what to post, after(d) = after each render
  function section(el, form, src, where, body, after) {
    const sec = { data: null, reset: new Set(), seq: 0 };
    const url = () => src().split('?')[0];
    // what a field holds when nothing is set in the scope shown: for a station its All-stations value
    const below = (s) => (sec.data.scope === 'station' && s.all != null ? s.all : s.default);
    const effective = (s) => (s.override != null ? s.override : below(s));

    function field(s, eff) {
      const attrs = ' id="' + el.id + '-' + esc(s.name) + '" name="' + esc(s.name) + '" data-orig="' + esc(eff) + '"';
      if (s.type === 'choice') {
        return '<select' + attrs + '>' + s.choices.map((c) => '<option' + (String(c) === String(eff) ? ' selected' : '') + '>' + esc(c) + '</option>').join('') + '</select>';
      }
      const num = s.type === 'int' || s.type === 'float';
      return '<input' + attrs + (num ? ' type="number" required step="' + esc(s.step) + '" min="' + esc(s.min) + '" max="' + esc(s.max) + '"' : ' type="text"') + ' value="' + esc(eff) + '">';
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
      const d = sec.data, groups = {}, scope = d.scope;
      d.settings.forEach((s) => { (groups[s.group] = groups[s.group] || []).push(s); });
      const here = scope === 'station' ? 'for this station' : scope === 'all' ? 'for all stations' : 'not the default';
      // one box per group, in columns; each setting: name and field, what it does, where it comes from
      el.innerHTML = Object.keys(groups).map((g, i) => {
        const set = groups[g].filter((s) => s.override != null).length;
        return '<section class="tile set-card ' + TINTS[i % TINTS.length] + '"><div class="t-head"><div class="t-label">' + esc(g) + '</div>' +
          (set ? '<span class="tag ok" title="Settings in this group set here (' + here + ')">' + set + ' set here</span>' : '') + '</div>' +
          groups[g].map((s) => {
            const eff = effective(s), num = s.type === 'int' || s.type === 'float', u = unit(s.name);
            const from = s.source ? SOURCE[s.source] || 'default' : (s.override != null ? 'set here' : 'default');
            // differs after the Core applied the latest settings = radio.lua's own fallback was edited
            const run = s.running == null ? '–' : (String(s.running) === String(eff) ? esc(fmt(s.running))
              : '<span class="diff">' + esc(fmt(s.running)) + (d.in_sync && s.source === 'default' ? ' (radio.lua default; set it here to change)' : ' (updating)') + '</span>');
            const back = scope === 'station' && !s.station_only ? 'use all stations (' + esc(fmt(below(s))) + ')' : 'reset to default (' + esc(fmt(s.default)) + ')';
            return '<div class="set-row' + (s.override != null ? ' is-set' : '') + '"><label class="nm" for="' + el.id + '-' + esc(s.name) + '">' + esc(s.label) + '</label>' +
              '<div class="field">' + field(s, eff) +
              '<span class="unit">' + esc(u) + '</span></div>' +
              '<div class="help">' + esc(s.help) + (num ? ' Range ' + esc(s.min) + '–' + esc(s.max) + (u ? ' ' + u : '') + '.' : '') + '</div>' +
              '<div class="state">' + (s.override != null ? '<span class="ovr">' + from + '</span>' : from) +
              (scope === 'all' ? '' : ' · ' + (s.server_only ? 'server' : where) + ': ' + run) +
              (s.override != null ? ' <button type="button" class="linkbtn" data-reset="' + esc(s.name) + '">' + back + '</button>' : '') +
              '</div></div>';
          }).join('') + '</section>';
      }).join('');
      dirty();
      if (after) after(d);
    };

    // each load bumps seq: an answer that arrives after a later load (another scope) began is dropped
    sec.load = async () => {
      const n = ++sec.seq;
      try {
        const d = await R.api(src());
        if (n !== sec.seq) return;
        sec.data = d;
        sec.reset.clear();
        sec.render();
      } catch (e) { if (n === sec.seq) R.toast('Settings: ' + e.message, 'bad'); }
    };

    // refresh the "running" column without clobbering edits in progress (or a load that began meanwhile)
    sec.watch = async () => {
      try {
        if (sec.data && !unsaved(el)) {
          const n = sec.seq, d = await R.api(src());
          if (n === sec.seq && !unsaved(el) && JSON.stringify(d) !== JSON.stringify(sec.data)) { sec.data = d; sec.render(); }
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
      inp.value = below(s);
      inp.classList.add('changed');
      sec.reset.add(s.name);
      dirty();
    });
    form.querySelector('[data-revert]').addEventListener('click', () => sec.load());
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const out = {};
      el.querySelectorAll('input, select').forEach((inp) => {
        if (sec.reset.has(inp.name)) out[inp.name] = null;           // back to the layer below
        else if (inp.value !== inp.dataset.orig) out[inp.name] = inp.type === 'number' ? Number(inp.value) : inp.value;
      });
      if (!Object.keys(out).length) { R.toast('Nothing changed.'); return; }
      try {
        const r = await R.post(url(), body(out, sec.data));
        const who = r.scope === 'station' ? ' for ' + r.station : r.scope === 'all' ? ' for all stations' : '';
        R.toast(r.changed.length ? 'Saved' + who + ': ' + r.changed.join(', ') : 'Nothing changed.', 'ok');
        sec.load();
      } catch (err) { R.toast('Not saved: ' + err.message, 'bad'); }
    });
    return sec;
  }

  // station settings: All stations, or the station chosen in the header (the default, once one has connected)
  let want = 'station', name = '';
  const station = section($('groups'), $('set-form'),
    () => (want === 'all' ? '/api/v1/settings?scope=all' : R.withStation('/api/v1/settings')), 'Core',
    (out, d) => (d.scope === 'station' ? { settings: out, scope: 'station', station: d.station } : { settings: out, scope: 'all' }),
    (d) => {
      if (d.station) name = d.station;
      R.setHTML($('scope'), '<button type="button" class="chip' + (d.scope === 'all' ? ' active' : '') + '" data-scope="all">All stations</button>' +
        (name ? '<button type="button" class="chip' + (d.scope === 'station' ? ' active' : '') + '" data-scope="station" title="This station\'s own values, over the All-stations ones">' + esc(name) + '</button>' : ''));
      const b = $('sync'), note = $('scope-note');
      b.hidden = d.scope !== 'station';
      if (!d.core_rev) { b.className = 'banner wait'; b.textContent = 'No Core has reported yet. Settings are sent when it connects.'; }
      else if (d.in_sync) { b.className = 'banner ok'; b.textContent = '✓ The Core is running these settings.'; }
      else { b.className = 'banner wait'; b.textContent = '… Waiting for the Core to apply the latest settings (it syncs every second).'; }
      const music = d.settings.find((s) => s.name === 'music_dir');
      note.hidden = d.scope === 'station' && !(music && music.own == null);
      if (d.scope === 'all') {
        note.className = 'banner';
        note.textContent = 'Every station uses these unless it sets its own. The folders (music, imaging, advertisements) are set per station.';
      } else if (music) {
        // the folder the Core reports once it runs these settings (its radio.lua may have been edited)
        const dir = d.in_sync && music.running != null ? music.running : music.default;
        note.className = 'banner wait';
        note.textContent = d.station + ' has no music folder of its own: it plays radio.lua\'s fallback folder (' + fmt(dir) +
          '). Set Music folder under Library.';
      }
    });
  $('scope').addEventListener('click', (e) => {
    const b = e.target.closest('[data-scope]');
    // against the scope asked for last, not the one shown (its load may still be on the way); no station yet = All
    if (!b || b.dataset.scope === (name ? want : 'all')) return;
    if (unsaved($('groups')) && !confirm('Discard your unsaved changes to these settings?')) return;
    want = b.dataset.scope;
    station.load();
  });
  // the header's station drop-down reloads the page: ask first when there are unsaved changes
  const sel = $('station-select');
  let shown = '';
  sel?.addEventListener('focus', () => { shown = sel.value; });
  document.addEventListener('change', (e) => {
    if (e.target !== sel || !document.querySelector('.set-grid .changed')) return;
    if (!confirm('Discard your unsaved settings changes?')) { e.stopPropagation(); sel.value = shown; }
  }, true);

  const server = section($('srv-groups'), $('srv-form'), () => '/api/v1/server-settings', 'in use', (out) => ({ settings: out }));
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
