// Library: search the Core's files, play next / queue / play now / bumper now,
// schedule ads. Reloads itself when the Core uploads a new scan or something plays.
(function () {
  'use strict';
  const R = window.Radio, $ = R.$, esc = R.esc;
  const PAGE = 100;
  let kind = 'music', offset = 0, seq = 0, timer = null, loading = false, stale = false, seen = null;
  const ICON = {
    next: '<svg viewBox="0 0 24 24"><path d="M4 6h10v2H4zm0 5h10v2H4zm0 5h6v2H4zm12-3v-3h2v3h3v2h-3v3h-2v-3h-3v-2z"/></svg>',
    queue: '<svg viewBox="0 0 24 24"><path d="M4 6h16v2H4zm0 5h16v2H4zm0 5h10v2H4z"/></svg>',
    play: '<svg viewBox="0 0 24 24"><path d="M7 5v14l11-7z"/></svg>',
  };
  const NOUN = { music: 'songs', imaging: 'imaging files', ad: 'ads' };
  const HEAD = {
    music: ['Artist – Title', 'Length', 'Without silence', 'Plays', 'Last played', ''],
    imaging: ['Artist – Title', 'Length', 'Plays', 'Last played', ''],
    ad: ['Artist – Title', 'Length', 'Schedule', 'This hour', 'Today', 'Last played', ''],
  };
  const button = (action, icon, text, title, primary) =>
    '<button class="sm' + (primary ? ' primary' : '') + '" data-a="' + action + '" title="' + title + '">' + ICON[icon] + '<span>' + text + '</span></button>';

  function actions(it) {
    if (!R.canOperate || it.unsupported) return '';
    if (it.kind === 'music') {
      return button('next', 'next', 'Play next', 'Play after the current track') + button('queue', 'queue', 'Queue', 'Add to the end of the queue') +
        button('now', 'play', 'Play now', 'Fade to this now', true);
    }
    return it.kind === 'ad' ? button('ad', 'play', 'Play now', 'Fade to an ad break with this ad, now', true)
      : button('bumper', 'play', 'Bumper now', 'Play this at the next transition, now', true);
  }

  // this hour's times for an hourly ad
  const SLOT_TIP = { done: 'played', due: 'owed: plays at the next song change', missed: 'missed its window: skipped', next: 'scheduled' };
  function slots(it) {
    if (!Array.isArray(it.slots)) return '';
    return '<span class="slots">' + it.slots.map((x) => '<span class="' + esc(x.state) + '" title="' + (SLOT_TIP[x.state] || '') + '">:' +
      String(new Date(x.at).getMinutes()).padStart(2, '0') + '</span>').join('') + '</span>';
  }

  function schedule(it) {
    const hourly = it.mode === 'hourly';
    if (!R.canOperate) return esc(hourly ? it.per_hour + ' per hour' : it.mode === 'off' ? 'Off' : 'Mix in');
    return '<span class="rule"><select data-rule="mode" title="Mix in: played in ad breaks in rotation. Per hour: this many plays each clock hour, spread out.">' +
      [['mix', 'Mix in'], ['hourly', 'Per hour'], ['off', 'Off']].map(([v, t]) => '<option value="' + v + '"' + (it.mode === v ? ' selected' : '') + '>' + t + '</option>').join('') +
      '</select><input type="number" data-rule="per_hour" min="1" max="30" step="1" value="' + (R.num(it.per_hour) || 1) + '"' + (hourly ? '' : ' hidden') +
      ' title="Plays per clock hour"></span>';
  }

  // length without the quiet start and end the Core measured on earlier plays
  function audible(it) {
    const lead = R.num(it.lead_in_s), trail = R.num(it.trail_s);
    if (!R.num(it.duration_s) || (lead == null && trail == null)) return '<td class="num">–</td>';
    return '<td class="num" title="quiet start ' + (lead || 0).toFixed(1) + ' s, quiet end ' + (trail || 0).toFixed(1) + ' s">' +
      R.clock(it.audible_s) + '</td>';
  }

  function row(it) {
    const name = esc(R.whoLine(it)) + (it.unsupported ? ' <span class="tag bad" title="The player cannot load this file">unsupported</span>' : '') +
      '<span class="file">' + esc(it.directory + it.file) + '</span>';
    const last = '<td class="num" title="' + esc(R.timeOf(it.last_played, true)) + '">' + esc(R.ago(it.last_played)) + '</td>';
    const len = '<td class="num">' + (R.num(it.duration_s) ? R.clock(it.duration_s) : '–') + '</td>';
    const cells = it.kind === 'ad'
      ? len + '<td>' + schedule(it) + slots(it) + '</td><td class="num">' + (R.num(it.plays_hour) || 0) + (it.mode === 'hourly' ? ' / ' + esc(it.per_hour) : '') +
        '</td><td class="num">' + (R.num(it.plays_today) || 0) + '</td>' + last
      : len + (it.kind === 'music' ? audible(it) : '') + '<td class="num">' + (R.num(it.plays) || 0) + '</td>' + last;
    return '<tr data-key="' + esc(it.key) + '" data-label="' + esc(R.whoLine(it)) + '"><td>' + name + '</td>' + cells + '<td class="acts">' + actions(it) + '</td></tr>';
  }

  // keep: reload the rows already shown (after a rescan or a play), not just the first page
  async function load(append, keep) {
    const my = ++seq;
    const limit = keep ? Math.max(PAGE, offset) : PAGE;
    if (!append) offset = 0;
    loading = true;
    stale = false;
    $('lib-more').disabled = true;
    const qs = new URLSearchParams({ kind, sort: $('lib-sort').value, limit, offset });
    const q = $('lib-q').value.trim();
    if (q) qs.set('q', q);
    try {
      const d = await R.api(R.withStation('/api/v1/library?' + qs));
      if (my !== seq) return;              // a newer search replaced this one
      R.setHTML($('lib-head'), '<tr>' + HEAD[kind].map((h) => '<th>' + h + '</th>').join('') + '</tr>');
      const html = d.items.map(row).join('');
      if (append) {
        $('lib').insertAdjacentHTML('beforeend', html);
        delete $('lib').dataset.html;
      } else {
        R.setHTML($('lib'), html || '<tr><td colspan="' + HEAD[kind].length + '" class="empty-state">' + (q ? 'Nothing matches “' + esc(q) + '”.'
          : kind === 'ad' ? 'No ads yet. Set the Ads folder in Settings; the Core uploads its files after each scan.'
          : 'No files yet. The Core uploads its library after each scan.') + '</td></tr>');
      }
      offset += d.items.length;
      $('lib-more').hidden = offset >= d.total;
      $('lib-info').textContent = d.total + ' ' + NOUN[kind] + (d.station ? ' on ' + d.station : '');
      const t = d.totals || {};
      $('lib-totals').textContent = !t.n ? '' : (q ? 'Matching: ' : 'Total: ') + t.n + ' ' + NOUN[kind] + ' · ' + R.clock(t.duration_s) +
        (kind === 'music' ? ' (' + R.clock(t.audible_s) + ' without leading / trailing silence)' : '') +
        (t.unknown ? ' · ' + t.unknown + ' not measured yet (length known once played)' : '');
    } catch (e) {
      if (my === seq) R.toast('Library: ' + e.message, 'bad');
    } finally {
      if (my === seq) {
        loading = false;
        $('lib-more').disabled = false;
        if (stale && !editing()) setTimeout(() => load(false, true), 0);   // something changed while loading
      }
    }
  }

  // never redraw under an operator who is editing an ad's schedule
  const editing = () => $('lib').contains(document.activeElement) && /^(INPUT|SELECT)$/.test(document.activeElement.tagName);
  function refresh() { if (editing() || loading) stale = true; else load(false, true); }

  async function saveRule(tr) {
    const mode = tr.querySelector('[data-rule="mode"]').value, input = tr.querySelector('[data-rule="per_hour"]');
    input.hidden = mode !== 'hourly';
    const body = { station: R.station || undefined, key: tr.dataset.key, mode };
    if (mode === 'hourly') body.per_hour = Number(input.value);
    try {
      await R.post('/api/v1/ads', body);
      R.toast(tr.dataset.label + ': ' + (mode === 'hourly' ? body.per_hour + ' per hour' : mode === 'off' ? 'off' : 'mixed in'), 'ok');
    } catch (e) { R.toast(tr.dataset.label + ': ' + e.message, 'bad'); }
    refresh();
  }

  $('kind-tabs').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-kind]');
    if (!b) return;
    kind = b.dataset.kind;
    document.querySelectorAll('#kind-tabs .chip').forEach((x) => x.classList.toggle('active', x === b));
    load(false);
  });
  $('lib-q').addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => load(false), 250); });
  $('lib-sort').addEventListener('change', () => load(false));
  $('lib-more').addEventListener('click', () => { if (!loading) load(true); });
  $('btn-rescan').hidden = !R.canOperate;
  $('btn-rescan').addEventListener('click', () => R.command('rescan', {}, 'Rescan library'));
  $('lib').addEventListener('change', (e) => { if (e.target.dataset.rule) saveRule(e.target.closest('tr')); });
  $('lib').addEventListener('focusout', () => setTimeout(() => { if (stale && !editing()) load(false, true); }, 0));
  $('lib').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-a]');
    if (!b) return;
    const tr = b.closest('tr'), key = tr.dataset.key, label = tr.dataset.label, a = b.dataset.a;
    if (a === 'next') R.command('queue_add', { key, position: 'next' }, 'Play next: ' + label);
    else if (a === 'queue') R.command('queue_add', { key, position: 'end' }, 'Queue: ' + label);
    else if (a === 'now' && confirm('Fade to “' + label + '” now?')) R.command('play_now', { key }, 'Play now: ' + label);
    else if (a === 'bumper') R.command('bumper_now', { key }, 'Bumper now: ' + label);
    else if (a === 'ad' && confirm('Fade to an ad break with “' + label + '” now?')) R.command('play_now', { key }, 'Play now: ' + label);
  });

  load(false);
  // a new scan from the Core (library_at) or a new play (counts, last played): reload what is shown
  R.status(3000, 1, (d) => {
    const s = d && d.station;
    if (!s) return;
    const now = s.library_at + '|' + ((d.recent[0] && d.recent[0].id) || '');
    if (seen !== null && now !== seen) refresh();
    seen = now;
  });
})();
