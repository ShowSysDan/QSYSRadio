// Dashboard: live decks from the Core, transport, up next + queue, activity.
(function () {
  'use strict';
  const R = window.Radio, $ = R.$, esc = R.esc, num = R.num;
  let last = null;
  const ICON = {
    up: '<svg viewBox="0 0 24 24"><path d="M12 5l-7 7h4v7h6v-7h4z"/></svg>',
    down: '<svg viewBox="0 0 24 24"><path d="M12 19l7-7h-4V5H9v7H5z"/></svg>',
    x: '<svg viewBox="0 0 24 24"><path d="M6.4 5L5 6.4 10.6 12 5 17.6 6.4 19 12 13.4 17.6 19 19 17.6 13.4 12 19 6.4 17.6 5 12 10.6z"/></svg>',
  };

  const core = () => (last && last.station && last.station.online ? last.station.state : null);
  // seconds since the Core measured elapsed/remaining (server-side age + time since we fetched)
  const age = () => (num(last && last.station && last.station.state_age_s) || 0) + (Date.now() - (last ? last.receivedAt : Date.now())) / 1000;
  const playing = (d) => d && (d.state === 'playing' || d.state === 'fading');

  function liveDeck(st) {
    if (!st || !st.decks) return null;
    const ds = ['A', 'B'].map((id) => st.decks[id]).filter(playing);
    return ds.find((d) => d.live && d.state === 'playing') || ds.find((d) => d.state === 'playing') || null;
  }

  function deckHtml(id, d) {
    if (!d || !d.file || !(playing(d) || d.state === 'loading')) {
      return '<div class="dk"><div class="dk-head"><span class="dk-id">DECK ' + esc(id) + '</span><span class="dk-state">idle</span></div><div class="dk-idle">NO SIGNAL</div></div>';
    }
    let cls = 'dk', label = d.state;
    if (d.kind === 'imaging') { cls += ' imaging'; label = 'imaging'; }
    else if (d.live && d.state === 'playing') { cls += ' live'; label = 'on air'; }
    else if (d.state === 'playing') { cls += ' incoming'; label = 'incoming'; }
    if (d.state === 'fading') { cls += ' fading'; label = 'fading out'; }
    const w = R.who(d);
    const lamp = cls.includes(' live') ? '<span class="onair-lamp">ON AIR</span>' : '<span class="dk-state">' + esc(label) + '</span>';
    return '<div class="' + cls + '" data-deck="' + esc(id) + '"><div class="dk-head"><span class="dk-id">DECK ' + esc(id) + '</span>' + lamp + '</div>' +
      '<div class="dk-title">' + esc(w.title) + '</div>' + (w.artist ? '<div class="dk-artist">' + esc(w.artist) + '</div>' : '') +
      '<div class="dk-file">' + esc((d.dir || '') + d.file) + '</div>' +
      '<div class="dk-times"><span class="el"></span><span class="rem"></span></div><div class="bar"><i></i></div></div>';
  }

  // clocks between polls: extrapolate from the Core's last report
  function tick() {
    const st = core();
    if (!st) return;
    const dt = age();
    document.querySelectorAll('.dk[data-deck]').forEach((el) => {
      const d = st.decks[el.dataset.deck], el0 = num(d && d.elapsed), dur = num(d && d.duration), rem = num(d && d.remaining);
      if (el0 == null) return;
      el.querySelector('.el').textContent = R.clock(el0 + dt) + ' / ' + R.clock(dur);
      el.querySelector('.rem').textContent = '-' + R.clock(rem != null ? rem - dt : null);
      if (dur) el.querySelector('.bar i').style.width = Math.min(100, (el0 + dt) / dur * 100).toFixed(1) + '%';
    });
    const lv = liveDeck(st);
    if (lv && num(lv.remaining) != null) {
      $('air-rem').textContent = '-' + R.clock(lv.remaining - dt);
      if (num(lv.duration)) $('air-meter').style.width = Math.min(100, (lv.elapsed + dt) / lv.duration * 100).toFixed(1) + '%';
    }
  }

  function renderDecks() {
    const st = core();
    if (st) {
      R.setHTML($('decks'), deckHtml('A', st.decks.A) + deckHtml('B', st.decks.B));
      $('decks-note').textContent = '';
      return;
    }
    // Core offline: show what the play log last saw on air
    const on = (last && last.on_air) || [];
    $('decks-note').textContent = on.length ? 'Core offline: from the play log' : '';
    R.setHTML($('decks'), on.length ? on.slice(0, 2).map((p) => deckHtml(p.deck || '?', { file: p.file, dir: p.directory,
      artist: p.artist, title: p.title, kind: p.kind, state: 'playing', live: true })).join('') : deckHtml('A', null) + deckHtml('B', null));
  }

  function renderKpis() {
    const s = last && last.station, st = core(), lv = liveDeck(st);
    const lamp = $('air-lamp');
    lamp.className = st && st.automation && lv ? 'onair-lamp' : 'lamp-off';
    lamp.textContent = st && st.automation && lv ? 'LIVE' : (s && !s.online ? 'NO CORE' : 'OFF');
    $('air-rem').className = lv ? 't-big red' : 't-big dimmed';
    if (lv) {
      $('air-sub').textContent = R.whoLine(lv);
    } else {
      $('air-rem').textContent = '-:--';
      $('air-meter').style.width = '0';
      $('air-sub').textContent = !s ? 'No Core has connected yet' : (!s.online ? 'Core offline · last seen ' + R.ago(s.last_seen)
        : (st && !st.automation ? 'Automation stopped' : 'Between tracks'));
    }
    const n = st && st.next_up, stopping = st && st.stop_after;
    $('next-title').textContent = stopping ? 'Nothing (stopping)' : (n ? R.who(n).title : '–');
    $('next-sub').textContent = stopping ? 'Stop after current is armed' : (n ? (n.artist || '') + (num(n.duration) ? '  ·  ' + R.clock(n.duration) : '') : ' ');
    R.setHTML($('next-src'), !st ? '' : (stopping ? '<span class="src stop">STOP</span>' : (n && n.queued ? '<span class="src queue">QUEUE</span>' : '<span class="src rotation">ROTATION</span>')));
    $('next-meter').style.width = st ? Math.min(100, st.queue.length * 10) + '%' : '0';
    if (st) {
      const every = num(st.bumper_every) || 0, since = num(st.tracks_since_bumper) || 0, left = every > 0 ? Math.max(0, every - since) : null;
      R.setHTML($('bump-big'), st.bumper_requested || st.next_bumper ? 'NEXT' : (left == null ? 'OFF' : left + ' <small>song' + (left === 1 ? '' : 's') + '</small>'));
      $('bump-sub').textContent = st.next_bumper ? R.who(st.next_bumper).title : (left == null ? 'Bumpers are off' : 'every ' + every + ' songs · ' + (num(st.library.imaging) || 0) + ' in pool');
      $('bump-meter').style.width = every > 0 ? Math.min(100, since / every * 100) + '%' : '0';
      R.setHTML($('lib-big'), (num(st.library.music) || 0) + ' <small>songs</small>');
      $('lib-sub').textContent = (num(st.library.imaging) || 0) + ' imaging · Core up ' + R.clock(st.uptime) + (num(st.outbox) > 20 ? ' · ' + st.outbox + ' events queued' : '');
      $('core-meter').style.width = '100%';
    } else {
      $('bump-big').textContent = '–'; $('bump-sub').textContent = ' '; $('lib-big').textContent = '–'; $('lib-sub').textContent = ' ';
      $('core-meter').style.width = '0';
    }
    $('core-info').textContent = s ? '· ' + s.station + ' · radio.lua ' + (s.version || '?') + (s.online ? '' : ' · offline')
      : '· no Core connected (set CFG.SERVER_URL in radio.lua)';
  }

  function renderTransport() {
    const st = core(), op = R.canOperate, auto = !!(st && st.automation);
    const b = (c) => document.querySelector('#transport [data-cmd="' + c + '"]');
    b('skip').disabled = !(op && auto && st.state === 'playing');
    b('bumper_now').disabled = !(op && auto && st.state !== 'bumper' && !st.bumper_requested && num(st.library.imaging) > 0);
    $('btn-stop-after').disabled = !(op && auto);
    $('btn-stop-after').classList.toggle('on', !!(st && st.stop_after));
    $('btn-stop-after').querySelector('span').textContent = st && st.stop_after ? 'Cancel stop after' : 'Stop after current';
    $('btn-stop').hidden = !!(st && !auto);
    $('btn-stop').disabled = !(op && auto);
    $('btn-start').hidden = !(st && !auto);
    $('btn-start').disabled = !(op && st);
  }

  function renderNext() {
    const st = core(), n = st && st.next_up;
    R.setHTML($('next'), !st ? '<div class="a">Core offline</div>' :
      '<div class="lbl">' + (st.stop_after ? '<span class="src stop">STOP</span> after the current track' :
        (n && n.queued ? '<span class="src queue">QUEUE</span>' : '<span class="src rotation">ROTATION</span>')) + '</div>' +
      (n && !st.stop_after ? '<div class="t">' + esc(R.who(n).title) + '</div>' + (n.artist ? '<div class="a">' + esc(n.artist) + '</div>' : '') : ''));
    const q = st ? st.queue : [], op = R.canOperate;
    $('qcount').textContent = q.length ? '(' + q.length + ')' : '';
    $('btn-clear').hidden = !(q.length && op);
    const btn = (kind, i, t, title, extra) => '<button class="icon' + (kind === 'rm' ? ' ghost-danger' : '') + '" title="' + title + '" data-q="' + kind +
      '" data-i="' + (i + 1) + '" data-key="' + esc(t.key) + '"' + (extra ? ' disabled' : '') + '>' + ICON[kind === 'rm' ? 'x' : kind] + '</button>';
    R.setHTML($('queue'), q.length ? q.map((t, i) => '<li><span class="n">' + (i + 1) + '</span><span class="nm" title="' + esc((t.dir || '') + t.file) + '">' +
      esc(R.whoLine(t)) + '</span>' + (op ? '<span class="acts">' + btn('up', i, t, 'Move up', i === 0) + btn('down', i, t, 'Move down', i === q.length - 1) +
      btn('rm', i, t, 'Remove') + '</span>' : '<span></span>') + '</li>').join('')
      : '<li class="empty-q">Queue empty: rotation picks the next song.<br>Add songs from the <a href="/library">Library</a>.</li>');
  }

  function renderActivity() {
    const c = (last && last.station && last.station.commands) || [];
    const ICONS = { done: '✓', failed: '✕', pending: '…', sent: '→' };
    R.setHTML($('activity'), c.length ? c.map((x) => '<li><span class="st ' + esc(x.state) + '">' + (ICONS[x.state] || '?') + '</span><span>' + esc(x.label || x.cmd) +
      (x.note ? '<span class="note">' + esc(x.note) + '</span>' : '') + '</span><span class="who">' + esc(x.by || '') + '<br>' +
      esc(R.timeOf(x.created_at)) + '</span></li>').join('') : '<li class="dim" style="grid-template-columns:1fr">No commands yet.</li>');
  }

  function render(d) {
    if (!d) return;
    last = d;
    renderKpis(); renderTransport(); renderDecks(); renderNext(); renderActivity(); tick();
    R.setHTML($('recent'), d.recent.map((p) => R.playRow(p, false)).join('') || '<tr><td colspan="6" class="empty-state">No plays logged yet.</td></tr>');
  }

  $('transport').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-cmd]');
    if (!btn || btn.disabled) return;
    const cmd = btn.dataset.cmd, st = core();
    if (cmd === 'stop_now' && !confirm('Fade out and stop the station now?')) return;
    if (cmd === 'stop_after') {
      const on = !(st && st.stop_after);
      R.command('stop_after', { on }, on ? 'Stop after current' : 'Cancel stop after current');
    } else {
      R.command(cmd, {}, btn.textContent.trim());
    }
  });
  $('btn-clear').addEventListener('click', () => { if (confirm('Clear the whole queue?')) R.command('queue_clear', {}, 'Clear queue'); });
  $('queue').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-q]');
    if (!b) return;
    const i = +b.dataset.i, key = b.dataset.key;
    if (b.dataset.q === 'rm') R.command('queue_remove', { index: i, key }, 'Remove from queue');
    else R.command('queue_move', { index: i, key, to: b.dataset.q === 'up' ? i - 1 : i + 1 }, 'Move in queue');
  });

  setInterval(tick, 250);
  R.status(1000, 12, render);
})();
