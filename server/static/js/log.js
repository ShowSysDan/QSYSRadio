// Play log: a day's plays (search, type), what was on air at a moment, CSV.
(function () {
  'use strict';
  const R = window.Radio, $ = R.$;
  let rows = [], title = '', shown = null, seq = 0;   // shown: the day query on screen (reloaded while it is today)

  function show(plays, summary, withDate, name) {
    rows = plays;
    title = name;
    $('log-summary').textContent = summary;
    R.setHTML($('log'), plays.map((p) => R.playRow(p, withDate)).join('') || '<tr><td colspan="6" class="empty-state">Nothing matches.</td></tr>');
  }

  async function showDay(e, again) {
    if (e) e.preventDefault();
    const day = again ? again.day : $('log-day').value;
    if (!day) return;
    const from = R.fromServer(day), to = R.fromServer(day, 1);          // the server's day
    const qs = again ? again.qs : new URLSearchParams({ from: from.toISOString(), to: to.toISOString(), limit: '5000' });
    if (!again && $('log-q').value.trim()) qs.set('q', $('log-q').value.trim());
    if (!again && $('log-kind').value) qs.set('kind', $('log-kind').value);
    const my = ++seq;
    try {
      const d = await R.api(R.withStation('/api/v1/plays?' + qs));
      if (my !== seq) return;              // a newer request replaced this one
      const n = (k) => d.plays.filter((p) => p.kind === k).length;
      shown = { day, qs };
      show(d.plays, d.plays.length + ' plays on ' + new Date(day + 'T12:00:00Z').toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric', timeZone: 'UTC' }) +
        ' (' + n('music') + ' music, ' + n('imaging') + ' imaging, ' + n('ad') + ' advertisements)', false, 'playlog-' + day);
    } catch (err) { $('log-summary').textContent = 'Error: ' + err.message; }
  }

  async function showAt(e) {
    e.preventDefault();
    const v = $('at-time').value;
    if (!v) return;
    shown = null;
    const my = ++seq;
    try {
      const at = R.fromServer(v);                                           // typed as server time
      const d = await R.api(R.withStation('/api/v1/plays/at?' + new URLSearchParams({ t: at.toISOString() })));
      if (my !== seq) return;
      const when = R.timeOf(at.toISOString(), true);
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

  $('log-form').addEventListener('submit', showDay);
  $('at-form').addEventListener('submit', showAt);
  $('btn-csv').addEventListener('click', csv);
  // the first poll brings the server's clock: today is the server's day. While today is shown, a play
  // starting or ending reloads it (the same search and type)
  let started = false, seen = null;
  R.status(5000, 1, (d) => {
    if (!d) return;
    const p = d.recent[0] || {}, now = p.id + '|' + p.ended_at;
    if (!started) {
      started = true;
      $('log-day').value = R.serverDay();
      showDay();
    } else if (shown && shown.day === R.serverDay() && now !== seen) {
      showDay(null, shown);
    }
    seen = now;
  });
})();
