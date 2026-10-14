// Library pages (Music, Imaging & IDs, Advertisements): search, filter, play next / queue /
// play now / bumper now, mark station IDs, schedule ads. Reloads itself when the
// Core uploads a new scan or something plays. Search, sort and filter live in the URL.
(function () {
  'use strict';
  const R = window.Radio, $ = R.$, esc = R.esc;
  const PAGE = 100;
  const kind = $('lib-tile').dataset.kind;
  let offset = 0, seq = 0, timer = null, loading = false, stale = false, seen = null, only = '';
  const byKey = {};                    // the rows shown, for the quiet start / end editor
  const ICON = {
    next: '<svg viewBox="0 0 24 24"><path d="M4 6h10v2H4zm0 5h10v2H4zm0 5h6v2H4zm12-3v-3h2v3h3v2h-3v3h-2v-3h-3v-2z"/></svg>',
    queue: '<svg viewBox="0 0 24 24"><path d="M4 6h16v2H4zm0 5h16v2H4zm0 5h10v2H4z"/></svg>',
    play: '<svg viewBox="0 0 24 24"><path d="M7 5v14l11-7z"/></svg>',
    analyze: '<svg viewBox="0 0 24 24"><path d="M3 11h2v2H3zm4-4h2v10H7zm4-4h2v18h-2zm4 6h2v6h-2zm4-2h2v10h-2z"/></svg>',
  };
  const NOUN = { music: 'songs', imaging: 'imaging files', ad: 'advertisements' };
  const FILTERS = {
    music: [['', 'All'], ['never', 'Never played'], ['not_analyzed', 'Not analyzed'], ['adjusted', 'Adjusted'], ['unsupported', 'Unsupported']],
    imaging: [['', 'All'], ['ident', 'Station IDs'], ['never', 'Never played'], ['not_analyzed', 'Not analyzed'], ['adjusted', 'Adjusted'],
      ['unsupported', 'Unsupported']],
    ad: [['', 'All'], ['hourly', 'Per hour'], ['mix', 'Mix in'], ['off', 'Off'], ['never', 'Never played'], ['not_analyzed', 'Not analyzed'],
      ['adjusted', 'Adjusted']],
  };
  const HEAD = {
    music: ['Artist – Title', 'Length', 'Without silence', 'Plays', 'Last played', ''],
    imaging: ['Artist – Title', 'Length', 'Without silence', 'Plays', 'Last played', ''],     // the ID toggle sits with the actions
    ad: ['Artist – Title', 'Length', 'Without silence', 'Schedule', 'This hour', 'Today', 'Last played', ''],
  };
  const button = (action, icon, text, title, primary) =>
    '<button class="sm' + (primary ? ' primary' : '') + '" data-a="' + action + '" title="' + title + '">' + ICON[icon] + '<span>' + text + '</span></button>';

  function actions(it) {
    if (!R.canOperate || it.unsupported) return '';
    return button('analyze', 'analyze', '', it.analyzed_at ? 'Analyze again on the off-air deck (analyzed ' + esc(R.timeOf(it.analyzed_at, true)) + ')'
      : 'Analyze on the off-air deck (measures its quiet start and end)') + playActions(it);
  }

  function playActions(it) {
    if (it.kind === 'music') {
      return button('next', 'next', 'Play next', 'Play after the current track') + button('queue', 'queue', 'Queue', 'Add to the end of the queue') +
        button('now', 'play', 'Play now', 'Fade to this now', true);
    }
    return it.kind === 'ad' ? button('ad', 'play', 'Play now', 'Fade to an ad break with this ad, now', true)
      : '<button class="sm id-btn' + (it.ident ? ' on' : '') + '" data-a="ident" title="' +
          (it.ident ? 'A station ID: click to make it an ordinary bumper' : 'Mark as a station ID (played on the ID schedule)') + '">ID</button>' +
        button('bumper', 'play', 'Bumper now', 'Play this at the next transition, now', true);
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

  // length without the quiet start and end (measured on earlier plays, or set by an operator); click to view / adjust
  const pick = (set, measured) => (R.num(set) != null ? R.num(set) : R.num(measured));
  const adjusted = (it) => R.num(it.lead_set_s) != null || R.num(it.trail_set_s) != null;
  function audible(it) {
    const lead = pick(it.lead_set_s, it.lead_in_s), trail = pick(it.trail_set_s, it.trail_s);
    const known = R.num(it.duration_s) && (lead != null || trail != null);
    const inner = (known ? R.clock(it.audible_s) : '–') + (adjusted(it) ? ' <span class="tag" title="Set by an operator">adj</span>' : '') +
      analysisTag(it);
    const tip = known ? 'quiet start ' + (lead || 0).toFixed(1) + ' s, quiet end ' + (trail || 0).toFixed(1) + ' s' +
      (it.analyzed_at ? ' (analyzed)' : ' (from plays)') : 'not measured yet';
    return '<td class="num" title="' + tip + '">' + (R.canOperate && !it.unsupported
      ? '<button type="button" class="linkbtn" data-a="trim" title="' + tip + ': view or adjust">' + inner + '</button>' : inner) + '</td>';
  }

  // analysis state: queued in the run, failed (tried again by the next run), or analyzed
  function analysisTag(it) {
    if (it.analyze_queued) return ' <span class="tag" title="Waiting for the off-air deck">queued</span>';
    if (!it.analyzed_at && it.analyze_failed_at) {
      return ' <span class="tag bad" title="Analysis failed ' + esc(R.timeOf(it.analyze_failed_at, true)) + ': ' + esc(it.analyze_error || '') +
        '. Tried again by the next run.">failed</span>';
    }
    return it.analyzed_at ? ' <span class="tag ok" title="Analyzed ' + esc(R.timeOf(it.analyzed_at, true)) + '">✓</span>' : '';
  }

  // the editor under a row: measured figures, the operator's (blank = use the measured one)
  function trimRow(tr) {
    const open = tr.nextElementSibling && tr.nextElementSibling.classList.contains('trim-row') ? tr.nextElementSibling : null;
    $('lib').querySelectorAll('.trim-row').forEach((x) => x.remove());
    if (open) { refresh(); return; }
    const it = byKey[tr.dataset.key];
    if (!it) return;
    const measured = (v) => (R.num(v) != null ? 'measured ' + R.num(v).toFixed(1) + ' s'
      : it.plays ? 'none heard' : 'not measured yet');
    const input = (name, set, m) => '<input type="number" min="0" max="600" step="0.1" data-trim="' + name + '" value="' +
      (R.num(set) == null ? '' : R.num(set)) + '" placeholder="' + (R.num(m) == null ? 'auto' : R.num(m).toFixed(1)) + '">';
    tr.insertAdjacentHTML('afterend', '<tr class="trim-row" data-key="' + esc(it.key) + '" data-label="' + esc(tr.dataset.label) + '"><td colspan="' +
      HEAD[kind].length + '"><div class="trim">' +
      '<label>Quiet start ' + input('lead', it.lead_set_s, it.lead_in_s) + ' s <span class="dim">' + measured(it.lead_in_s) + '</span></label>' +
      '<label>Quiet end ' + input('trail', it.trail_set_s, it.trail_s) + ' s <span class="dim">' + measured(it.trail_s) + '</span></label>' +
      '<button type="button" class="sm primary" data-t="save">Save</button>' +
      '<button type="button" class="sm" data-t="clear" title="Remove the figures set here">Use measured</button>' +
      '<button type="button" class="sm" data-t="forget" title="Measure again on the next play">Forget measured</button>' +
      '<button type="button" class="sm" data-t="analyze" title="Measure it again on the off-air deck">Analyze again</button>' +
      '<span class="dim small">' + (it.analyzed_at ? 'analyzed ' + esc(R.timeOf(it.analyzed_at, true))
        : it.analyze_failed_at ? 'analysis failed: ' + esc(it.analyze_error || '') : 'measured from plays') + '</span>' +
      '<button type="button" class="linkbtn" data-t="close">close</button>' +
      '<div class="dim small">Quiet start: where the sound begins, in seconds from the start of the file (the Core starts just before it, ' +
      'within Settings → Silence detection). Quiet end: seconds of silence at the end (the next item starts that much early). ' +
      'Blank = what the Core measures. A figure set here is not measured over.</div></div></td></tr>');
    tr.nextElementSibling.querySelector('input').focus();
  }

  async function trimAction(row, t) {
    const key = row.dataset.key, label = row.dataset.label, body = { station: R.station || undefined, key };
    try {
      if (t === 'save' || t === 'clear') {
        const val = (name) => { const v = row.querySelector('[data-trim="' + name + '"]').value; return t === 'clear' || v === '' ? null : Number(v); };
        body.lead = val('lead');
        body.trail = val('trail');
        await R.post('/api/v1/library/trim', body);
        R.toast(label + (t === 'clear' ? ': using the measured figures' : ': saved; the Core picks it up after its rescan'), 'ok');
      } else if (t === 'analyze') {
        await R.post('/api/v1/analyze', body);
        R.toast(label + ': queued for analysis on the off-air deck', 'ok');
      } else if (t === 'forget') {
        if (!confirm('Forget the measured quiet start and end of “' + label + '”? They are measured again on its next play.')) return;
        await R.post('/api/v1/library/forget', body);
        R.toast(label + ': measured figures forgotten', 'ok');
      }
    } catch (e) { R.toast(label + ': ' + e.message, 'bad'); return; }
    row.remove();
    refresh();
  }

  function row(it) {
    const name = esc(R.whoLine(it)) + (it.ident ? ' <span class="tag ident" title="Station ID">ID</span>' : '') +
      (it.unsupported ? ' <span class="tag bad" title="The player cannot load this file">unsupported</span>' : '') +
      '<span class="file">' + esc(it.directory + it.file) + '</span>';
    const last = '<td class="num" title="' + esc(R.timeOf(it.last_played, true)) + '">' + esc(R.ago(it.last_played)) + '</td>';
    const len = '<td class="num">' + (R.num(it.duration_s) ? R.clock(it.duration_s) : '–') + '</td>';
    const cells = it.kind === 'ad'
      ? len + audible(it) + '<td>' + schedule(it) + slots(it) + '</td><td class="num">' + (R.num(it.plays_hour) || 0) + (it.mode === 'hourly' ? ' / ' + esc(it.per_hour) : '') +
        '</td><td class="num">' + (R.num(it.plays_today) || 0) + '</td>' + last
      : len + audible(it) + '<td class="num">' + (R.num(it.plays) || 0) + '</td>' + last;
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
    if (only) qs.set('only', only);
    const here = new URLSearchParams();               // remembered in the URL (back button, links, reload)
    if (q) here.set('q', q);
    if (only) here.set('only', only);
    if ($('lib-sort').value !== 'artist') here.set('sort', $('lib-sort').value);
    history.replaceState(null, '', location.pathname + (here.toString() ? '?' + here : ''));
    try {
      const d = await R.api(R.withStation('/api/v1/library?' + qs));
      if (my !== seq) return;              // a newer search replaced this one
      R.setHTML($('lib-head'), '<tr>' + HEAD[kind].map((h) => '<th>' + h + '</th>').join('') + '</tr>');
      d.items.forEach((it) => { byKey[it.key] = it; });
      const html = d.items.map(row).join('');
      if (append) {
        $('lib').insertAdjacentHTML('beforeend', html);
        delete $('lib').dataset.html;
      } else {
        if (kind === 'ad' && !q && !only) hourLine(d.items);
        R.setHTML($('lib'), html || '<tr><td colspan="' + HEAD[kind].length + '" class="empty-state">' + (q || only ? 'Nothing matches.'
          : kind === 'ad' ? 'No advertisements yet. Set the Advertisements folder in Settings; the Core uploads its files after each scan.'
          : 'No files yet. The Core uploads its library after each scan.') + '</td></tr>');
      }
      offset += d.items.length;
      $('lib-more').hidden = offset >= d.total;
      $('lib-info').textContent = d.total + ' ' + NOUN[kind] + (d.station ? ' on ' + d.station : '');
      const t = d.totals || {};
      $('lib-totals').textContent = !t.n ? '' : (q || only ? 'Matching: ' : 'Total: ') + t.n + ' ' + NOUN[kind] + ' · ' + R.clock(t.duration_s) +
        ' (' + R.clock(t.audible_s) + ' without leading / trailing silence)' +
        (t.unknown ? ' · ' + t.unknown + ' not measured yet (length known once played)' : '') +
        (kind === 'music' && t.n ? ' · rotation: ' + (t.never_played ? t.never_played + ' never played' : 'every song played') +
          (t.oldest_played ? ', longest wait ' + R.ago(t.oldest_played).replace(' ago', '') : '') : '');
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

  // never redraw under an operator who is editing an ad's schedule or a file's quiet start / end
  const editing = () => !!$('lib').querySelector('.trim-row') ||
    ($('lib').contains(document.activeElement) && /^(INPUT|SELECT)$/.test(document.activeElement.tagName));
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

  // every per-hour ad's slots this hour, in time order (Advertisements page)
  function hourLine(items) {
    const all = [];
    items.forEach((it) => (it.slots || []).forEach((x) => all.push({ at: x.at, state: x.state, name: R.whoLine(it) })));
    all.sort((a, b) => a.at.localeCompare(b.at));
    $('hour-tile').hidden = !all.length;
    R.setHTML($('hour'), all.map((x) => '<span class="hl ' + esc(x.state) + '" title="' + esc(SLOT_TIP[x.state] || '') + '"><b>' +
      esc(R.serverHM(x.at)) + '</b> ' + esc(x.name) + '</span>').join(''));
  }

  async function setIdent(tr, on) {
    try {
      await R.post('/api/v1/library/ident', { station: R.station || undefined, key: tr.dataset.key, ident: on });
      R.toast(tr.dataset.label + (on ? ': station ID' : ': ordinary bumper'), 'ok');
    } catch (e) { R.toast(tr.dataset.label + ': ' + e.message, 'bad'); }
    refresh();
  }

  // filters, then whatever the URL remembered
  const url = new URLSearchParams(location.search);
  only = FILTERS[kind].some(([v]) => v === url.get('only')) ? url.get('only') : '';
  $('lib-q').value = url.get('q') || '';
  if (url.get('sort')) $('lib-sort').value = url.get('sort');
  $('lib-filters').innerHTML = FILTERS[kind].map(([v, t]) => '<button type="button" class="chip' + (v === only ? ' active' : '') +
    '" data-only="' + v + '">' + t + '</button>').join('');
  $('lib-filters').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-only]');
    if (!b) return;
    only = b.dataset.only;
    document.querySelectorAll('#lib-filters .chip').forEach((x) => x.classList.toggle('active', x === b));
    load(false);
  });
  document.addEventListener('keydown', (e) => {         // "/" jumps to the search box
    if (e.key === '/' && !/^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName)) { e.preventDefault(); $('lib-q').focus(); }
    if (e.key === 'Escape' && document.activeElement === $('lib-q') && $('lib-q').value) { $('lib-q').value = ''; load(false); }
  });
  $('lib-q').addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => load(false), 250); });
  $('lib-sort').addEventListener('change', () => load(false));
  $('lib-more').addEventListener('click', () => { if (!loading) load(true); });
  $('btn-rescan').hidden = !R.canOperate;
  $('btn-rescan').addEventListener('click', () => R.command('rescan', {}, 'Rescan library'));
  $('btn-forget').hidden = !R.canOperate;
  $('btn-forget').addEventListener('click', async () => {
    if (!confirm('Forget the measured quiet start and end of every one of the ' + NOUN[kind] + '? Each is measured again on its next play. Figures set by an operator stay.')) return;
    try {
      const r = await R.post('/api/v1/library/forget', { station: R.station || undefined, kind });
      R.toast('Forgotten for ' + r.files + ' ' + NOUN[kind], 'ok');
    } catch (e) { R.toast('Forget: ' + e.message, 'bad'); }
    refresh();
  });
  $('lib').addEventListener('change', (e) => { if (e.target.dataset.rule) saveRule(e.target.closest('tr')); });
  $('lib').addEventListener('keydown', (e) => {
    if (e.target.dataset.trim && e.key === 'Enter') trimAction(e.target.closest('tr'), 'save');
    if (e.target.dataset.trim && e.key === 'Escape') { e.target.closest('tr').remove(); refresh(); }
  });
  $('lib').addEventListener('focusout', () => setTimeout(() => { if (stale && !editing()) load(false, true); }, 0));
  $('lib').addEventListener('click', (e) => {
    const t = e.target.closest('button[data-t]');
    if (t) {
      if (t.dataset.t === 'close') { t.closest('tr').remove(); refresh(); } else trimAction(t.closest('tr'), t.dataset.t);
      return;
    }
    const b = e.target.closest('button[data-a]');
    if (!b) return;
    const tr = b.closest('tr'), key = tr.dataset.key, label = tr.dataset.label, a = b.dataset.a;
    if (a === 'next') R.command('queue_add', { key, position: 'next' }, 'Play next: ' + label);
    else if (a === 'queue') R.command('queue_add', { key, position: 'end' }, 'Queue: ' + label);
    else if (a === 'now' && confirm('Fade to “' + label + '” now?')) R.command('play_now', { key }, 'Play now: ' + label);
    else if (a === 'bumper') R.command('bumper_now', { key }, 'Bumper now: ' + label);
    else if (a === 'ident') setIdent(tr, !b.classList.contains('on'));
    else if (a === 'trim') trimRow(tr);
    else if (a === 'analyze') {
      R.post('/api/v1/analyze', { station: R.station || undefined, key })
        .then(() => { R.toast(label + ': queued for analysis on the off-air deck', 'ok'); refresh(); })
        .catch((err) => R.toast(label + ': ' + err.message, 'bad'));
    }
    else if (a === 'ad' && confirm('Fade to an ad break with “' + label + '” now?')) R.command('play_now', { key }, 'Play now: ' + label);
  });

  // the first poll brings the server's clock (times are shown in its zone): load then; after that, a new
  // scan from the Core (library_at) or a new play (counts, last played) reloads what is shown
  let started = false;
  // file analysis: progress and the buttons to start / stop it
  function analysisLine(a) {
    const el = $('lib-analysis');
    el.hidden = !a;
    if (!a) return;
    const btn = (scope, text, title) => R.canOperate ? '<button type="button" class="sm" data-scope="' + scope + '" title="' + title + '">' + text + '</button>' : '';
    const daily = a.at === 'off' ? 'no daily run' : 'daily at ' + esc(a.at);
    R.setHTML(el, !a.can
      ? '<span>File analysis needs the players\' mixer: set <b>CFG.MIXER</b> in radio.lua (the Core\'s Debug Output says why it is off).</span>'
      : a.active
        ? '<span><b>Analyzing</b> on the off-air deck, between songs: ' + R.num(a.left) + ' file' + (a.left === 1 ? '' : 's') + ' to go.</span>' +
          btn('stop', 'Stop', 'Stop analyzing (what is done stays)')
        : '<span><b>File analysis</b>: ' + (a.never ? R.num(a.never) + ' file' + (a.never === 1 ? '' : 's') + ' not analyzed yet' +
          (a.failed ? ' (' + R.num(a.failed) + ' failed, tried again by the next run)' : '') : 'every file analyzed') +
          ' · ' + daily + ' (Settings).</span>' + (a.never ? btn('new', 'Analyze now', 'Measure the files not analyzed yet, on the off-air deck') : '') +
          btn('all', 'Re-analyze all', 'Measure every file again'));
  }
  $('lib-analysis').addEventListener('click', async (e) => {
    const b = e.target.closest('button[data-scope]');
    if (!b) return;
    const scope = b.dataset.scope;
    if (scope === 'all' && !confirm('Measure every file again? It runs on the off-air deck between songs and can take hours for a large library.')) return;
    try {
      await R.post('/api/v1/analyze', { station: R.station || undefined, scope });
      R.toast(scope === 'stop' ? 'Analysis stopped' : 'Analysis started on the off-air deck', 'ok');
    } catch (err) { R.toast('Analysis: ' + err.message, 'bad'); }
  });

  R.status(3000, 1, (d) => {
    if (!started) { started = true; load(false); }
    const s = d && d.station;
    analysisLine(s && s.analysis);
    if (!s) return;
    const now = s.library_at + '|' + ((d.recent[0] && d.recent[0].id) || '');
    if (seen !== null && now !== seen) refresh();
    seen = now;
  });
})();
