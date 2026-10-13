// Play log: a day's plays (search, type), what was on air at a moment, CSV.
(function () {
  'use strict';
  const R = window.Radio, $ = R.$;
  let rows = [], title = '';

  function show(plays, summary, withDate, name) {
    rows = plays;
    title = name;
    $('log-summary').textContent = summary;
    $('log').innerHTML = plays.map((p) => R.playRow(p, withDate)).join('') || '<tr><td colspan="6" class="empty-state">Nothing matches.</td></tr>';
  }

  async function showDay(e) {
    if (e) e.preventDefault();
    const day = $('log-day').value;
    if (!day) return;
    const [y, m, dd] = day.split('-').map(Number);
    const from = new Date(y, m - 1, dd), to = new Date(y, m - 1, dd + 1);
    const qs = new URLSearchParams({ from: from.toISOString(), to: to.toISOString(), limit: '5000' });
    if ($('log-q').value.trim()) qs.set('q', $('log-q').value.trim());
    if ($('log-kind').value) qs.set('kind', $('log-kind').value);
    try {
      const d = await R.api(R.withStation('/api/v1/plays?' + qs));
      const music = d.plays.filter((p) => p.kind === 'music').length;
      show(d.plays, d.plays.length + ' plays on ' + from.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' }) +
        ' (' + music + ' music, ' + (d.plays.length - music) + ' imaging)', false, 'playlog-' + day);
    } catch (err) { $('log-summary').textContent = 'Error: ' + err.message; }
  }

  async function showAt(e) {
    e.preventDefault();
    const v = $('at-time').value;
    if (!v) return;
    try {
      const d = await R.api(R.withStation('/api/v1/plays/at?' + new URLSearchParams({ t: new Date(v).toISOString() })));
      const when = new Date(v).toLocaleString();
      show(d.plays, d.plays.length ? 'On air at ' + when + (d.plays.length > 1 ? ' (crossfade: ' + d.plays.length + ' items)' : '')
        : 'Nothing was on air at ' + when, true, 'on-air-' + v.replace(/[:T]/g, '-'));
    } catch (err) { $('log-summary').textContent = 'Error: ' + err.message; }
  }

  function csv() {
    if (!rows.length) { R.toast('Nothing to download: load a day first.', 'bad'); return; }
    const cols = ['started_at', 'ended_at', 'kind', 'artist', 'title', 'directory', 'file', 'deck', 'duration_s', 'played_s', 'end_reason', 'station'];
    const cell = (v) => {
      v = v == null ? '' : String(v);
      if (/^[=+\-@\t\r]/.test(v)) v = "'" + v;                  // no spreadsheet formulas
      return /[",\n\r]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
    };
    const body = [cols.join(',')].concat(rows.map((r) => cols.map((c) => cell(r[c])).join(','))).join('\r\n');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([body], { type: 'text/csv' }));
    a.download = title + '.csv';
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  $('log-day').value = R.isoDay(new Date());
  $('log-form').addEventListener('submit', showDay);
  $('at-form').addEventListener('submit', showAt);
  $('btn-csv').addEventListener('click', csv);
  showDay();
  R.status(5000, 0);
})();
