// Library: search the Core's files, play next / queue / play now / bumper now.
(function () {
  'use strict';
  const R = window.Radio, $ = R.$, esc = R.esc;
  const PAGE = 100;
  let kind = 'music', offset = 0, seq = 0, timer = null, loading = false;
  const ICON = {
    next: '<svg viewBox="0 0 24 24"><path d="M4 6h10v2H4zm0 5h10v2H4zm0 5h6v2H4zm12-3v-3h2v3h3v2h-3v3h-2v-3h-3v-2z"/></svg>',
    queue: '<svg viewBox="0 0 24 24"><path d="M4 6h16v2H4zm0 5h16v2H4zm0 5h10v2H4z"/></svg>',
    play: '<svg viewBox="0 0 24 24"><path d="M7 5v14l11-7z"/></svg>',
  };
  const button = (action, icon, text, title, primary) =>
    '<button class="sm' + (primary ? ' primary' : '') + '" data-a="' + action + '" title="' + title + '">' + ICON[icon] + '<span>' + text + '</span></button>';

  function row(it) {
    const acts = !R.canOperate ? '' : it.kind === 'music'
      ? button('next', 'next', 'Play next', 'Play after the current track') + button('queue', 'queue', 'Queue', 'Add to the end of the queue') +
        button('now', 'play', 'Play now', 'Fade to this now', true)
      : button('bumper', 'play', 'Bumper now', 'Play this at the next transition, now', true);
    return '<tr data-key="' + esc(it.key) + '" data-label="' + esc(R.whoLine(it)) + '">' +
      '<td>' + esc(R.whoLine(it)) + '<span class="file">' + esc(it.directory + it.file) + '</span></td>' +
      '<td class="num">' + (R.num(it.duration_s) ? R.clock(it.duration_s) : '–') + '</td>' +
      '<td class="num">' + (R.num(it.plays) || 0) + '</td>' +
      '<td class="num" title="' + esc(it.last_played ? new Date(it.last_played).toLocaleString() : '') + '">' + esc(R.ago(it.last_played)) + '</td>' +
      '<td class="acts">' + acts + '</td></tr>';
  }

  async function load(append) {
    const my = ++seq;
    if (!append) offset = 0;
    loading = true;
    $('lib-more').disabled = true;
    const qs = new URLSearchParams({ kind, sort: $('lib-sort').value, limit: PAGE, offset });
    const q = $('lib-q').value.trim();
    if (q) qs.set('q', q);
    try {
      const d = await R.api(R.withStation('/api/v1/library?' + qs));
      if (my !== seq) return;              // a newer search replaced this one
      const html = d.items.map(row).join('');
      if (append) $('lib').insertAdjacentHTML('beforeend', html);
      else $('lib').innerHTML = html || '<tr><td colspan="5" class="empty-state">' +
        (q ? 'Nothing matches “' + esc(q) + '”.' : 'No files yet. The Core uploads its library after each scan.') + '</td></tr>';
      offset += d.items.length;
      $('lib-more').hidden = offset >= d.total;
      $('lib-info').textContent = d.total + (kind === 'music' ? ' songs' : ' imaging files') + (d.station ? ' on ' + d.station : '');
    } catch (e) {
      if (my === seq) R.toast('Library: ' + e.message, 'bad');
    } finally {
      if (my === seq) { loading = false; $('lib-more').disabled = false; }
    }
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
  $('lib').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-a]');
    if (!b) return;
    const tr = b.closest('tr'), key = tr.dataset.key, label = tr.dataset.label, a = b.dataset.a;
    if (a === 'next') R.command('queue_add', { key, position: 'next' }, 'Play next: ' + label);
    else if (a === 'queue') R.command('queue_add', { key, position: 'end' }, 'Queue: ' + label);
    else if (a === 'now' && confirm('Fade to “' + label + '” now?')) R.command('play_now', { key }, 'Play now: ' + label);
    else if (a === 'bumper') R.command('bumper_now', { key }, 'Bumper now: ' + label);
  });

  load(false);
  R.status(3000, 0);
})();
