// Stations: every station at a glance (what plays, its countdown and meter, Start / Stop / Skip), from one poll
// for all of them (/api/v1/stations-overview). A card per station, by name; each part written only when it changed.
(function () {
  'use strict';
  const R = window.Radio, $ = R.$, esc = R.esc, num = R.num;
  const grid = $('st-grid'), sum = $('st-sum');
  let last = null, names = '';
  const cards = new Map();      // station -> its card's elements
  const put = (obj, prop, v) => { if (obj[prop] !== v) obj[prop] = v; };

  const BUTTONS = R.canOperate ? '<span class="st-btns">' +
    '<button class="sm primary" data-cmd="start" title="Start automation"><svg viewBox="0 0 24 24"><path d="M7 5v14l11-7z"/></svg>Start</button>' +
    '<button class="sm" data-cmd="skip" title="Fade to the next track"><svg viewBox="0 0 24 24"><path d="M5 5v14l9-7zM15 5h3v14h-3z"/></svg>Skip</button>' +
    '<button class="sm danger" data-cmd="stop_now" title="Fade out now and stop"><svg viewBox="0 0 24 24"><path d="M6 6h12v12H6z"/></svg>Stop</button>' +
    '</span>' : '';
  const skeleton = (name) => '<article class="tile st-card">' +
    '<div class="st-head"><a class="st-name" href="/" data-pick="' + esc(name) + '" title="Make it the header\'s station and open its Dashboard">' +
    esc(name) + '</a><span class="st-badge"></span></div>' +
    '<div class="st-fault" hidden></div>' +
    '<div class="st-now"><span class="st-title"></span><span class="st-rem"></span></div>' +
    '<div class="st-prog"><i></i></div>' +
    '<div class="st-next"></div>' +
    '<div class="st-mtr"><i></i><b></b></div>' +
    '<div class="st-foot"><span class="st-core"></span>' + BUTTONS + '</div></article>';

  // what a station is doing: [card class, lamp]
  function mode(s) {
    const st = s.state;
    if (!s.online) return ['offline', '<span class="lamp-off">OFFLINE</span>'];
    if (st.fault) return ['fault', '<span class="lamp-off bad">FAULT</span>'];
    if (st.automation && st.stop_after) return ['stopping', '<span class="lamp-off warn">STOPPING</span>'];
    if (st.automation) return ['onair', '<span class="onair-lamp">ON AIR</span>'];
    return ['stopped', '<span class="lamp-off">STOPPED</span>'];
  }
  const deckOf = (st, lv) => ['A', 'B'].find((id) => st.decks[id] === lv);
  // seconds since the Core measured this station's elapsed / remaining
  const ageOf = (s) => (num(s.state_age_s) || 0) + (Date.now() - last.receivedAt) / 1000;

  function nowLine(s, lv) {
    const st = s.state;
    if (!s.online) return 'Core offline · last seen ' + R.ago(s.last_seen) + (s.playing ? ' · last logged: ' + R.whoLine(s.playing) : '');
    if (st.fault) return 'Station fault: its decks are stopped';
    if (lv) return R.whoLine(lv);
    if (!st.automation) return 'Automation stopped';
    return s.playing ? R.whoLine(s.playing) : 'Between tracks';
  }

  function nextHtml(s) {
    const st = s.state, n = st.next_up;
    if (!s.online || st.fault || !st.automation) return '';
    if (st.stop_after) return '<span class="src stop">STOP</span> after this song';
    if (!n) return '';
    return '<span class="dim">Next</span> ' + (n.queued ? '<span class="src queue">QUEUE</span> ' : '') + esc(R.whoLine(n));
  }

  // the Core it is on; a warning while its memory keeps growing or it runs another radio.lua than the server
  function coreHtml(s) {
    const c = (last.cores || {})[s.core] || {}, v = c.version || s.version, warn = [];
    if (c.mem_warning) {
      warn.push('<span class="st-warn" title="The Core\'s Lua memory keeps growing (possible leak: see the journal). ' +
        'A restart of the script clears it.">memory ' + (num(c.mem_kb) ? (c.mem_kb / 1024).toFixed(1) + ' MB' : 'growing') + '</span>');
    }
    if (v && last.version && v !== last.version) {
      warn.push('<span class="st-warn" title="The Core runs radio.lua ' + esc(v) + ', this server is ' + esc(last.version) +
        ': paste radio.lua from this server\'s version into the Core">radio.lua ' + esc(v) + '</span>');
    }
    return (s.core ? 'on <b>' + esc(s.core) + '</b>' : '<span class="dim">Core not named</span>') + warn.join('');
  }

  function renderCard(s, c) {
    const st = s.state, [cls, lamp] = mode(s), lv = s.online && st.automation ? R.liveDeck(st) : null;
    put(c.el, 'className', 'tile st-card ' + cls + (last.station && last.station.station === s.station ? ' current' : ''));
    R.setHTML(c.badge, lamp + '<span class="tag" title="' + (s.players === 1 ? 'A background-music zone: songs only' : 'Two players: songs, bumpers and ads') +
      '">' + (s.players === 1 ? '1 player' : '2 players') + '</span>');
    put(c.fault, 'hidden', !(s.online && st.fault));
    put(c.fault, 'textContent', s.online && st.fault ? st.fault + (num(st.retry_s) != null ? ' · next try in about ' + R.clock(Math.max(0, st.retry_s - ageOf(s))) : '') : '');
    R.marquee(c.title, nowLine(s, lv));
    R.setHTML(c.next, nextHtml(s));
    const id = lv && deckOf(st, lv), m = id && s.meters && s.meters[id];
    put(c.mtr, 'hidden', !m);
    if (m) put(c.mtr, 'title', 'Deck ' + id + ' after its gain: rms ' + R.db(m.rms, -120) + ' dB, peak ' + R.db(m.peak, -120) + ' dB');
    R.meter('station:' + s.station, c.mtr.querySelector('i'), c.mtr.querySelector('b'), m, true);
    R.setHTML(c.core, coreHtml(s));
    c.lv = lv;
    if (!c.skip) return;
    const op = R.canOperate && s.online && !st.fault, auto = st.automation;   // faulted: the Core rebuilds it
    c.skip.disabled = !(op && auto && st.state === 'playing');
    c.stop.hidden = s.online && !auto;
    c.stop.disabled = !(op && auto);
    c.start.hidden = !(s.online && !auto);
    c.start.disabled = !op;
  }

  // countdowns and progress between polls: extrapolated from each Core's last report
  function tick() {
    if (!last) return;
    for (const s of last.stations) {
      const c = cards.get(s.station), lv = c && c.lv;
      if (!c) continue;
      const rem = lv && num(lv.remaining), dur = lv && num(lv.duration), dt = lv ? ageOf(s) : 0;
      put(c.rem, 'textContent', rem != null ? '-' + R.clock(rem - dt) : '');
      put(c.prog.style, 'width', rem != null && dur ? Math.min(100, Math.max(0, (num(lv.elapsed) + dt) / dur * 100)).toFixed(2) + '%' : '0');
    }
  }

  function summary(list, archived) {
    const n = { on: 0, stopped: 0, offline: 0, fault: 0 };
    for (const s of list) {
      if (!s.online) n.offline++;
      else if (s.state.fault) n.fault++;
      else if (s.state.automation) n.on++;
      else n.stopped++;
    }
    const part = (v, word, cls) => '<span class="' + (v && cls ? cls : '') + '"><b>' + v + '</b> ' + word + '</span>';
    return part(list.length, list.length === 1 ? 'station' : 'stations') + part(n.on, 'on air', 'ok') + part(n.stopped, 'stopped') +
      part(n.offline, 'offline', 'warn') + part(n.fault, 'faulted', 'bad') +
      (archived.length ? '<span title="No longer in their Core\'s CFG.STATIONS: hidden until listed again (' +
        esc(archived.map((a) => a.station + (a.core ? ' on ' + a.core : '')).join(', ')) + ')"><b>' + archived.length + '</b> archived</span>' : '');
  }

  function render(d, err) {
    grid.classList.toggle('stale', !d);
    if (!d) { R.setHTML(sum, '<span class="bad">Server offline: ' + esc(err ? err.message : '') + '</span>'); return; }
    last = d;
    const list = d.stations || [], key = list.map((s) => s.station).join('\n');
    if (key !== names || !list.length) {
      names = key;
      R.setHTML(grid, list.length ? list.map((s) => skeleton(s.station)).join('')
        : '<div class="empty-state">No station has connected yet (set CFG.SERVER_URL in radio.lua).</div>');
      cards.clear();
      list.forEach((s, i) => {
        const el = grid.children[i], q = (sel) => el.querySelector(sel);
        cards.set(s.station, { el, badge: q('.st-badge'), fault: q('.st-fault'), title: q('.st-title'), rem: q('.st-rem'), prog: q('.st-prog i'),
          next: q('.st-next'), mtr: q('.st-mtr'), core: q('.st-core'), start: q('[data-cmd="start"]'), skip: q('[data-cmd="skip"]'),
          stop: q('[data-cmd="stop_now"]') });
      });
    }
    R.setHTML(sum, summary(list, d.archived || []));
    list.forEach((s) => renderCard(s, cards.get(s.station)));
    tick();
  }

  grid.addEventListener('click', (e) => {
    const pick = e.target.closest('a[data-pick]');
    if (pick) { R.pick(pick.dataset.pick); return; }            // then on to its Dashboard
    const btn = e.target.closest('button[data-cmd]');
    if (!btn || btn.disabled) return;
    const name = btn.closest('.st-card').querySelector('a[data-pick]').dataset.pick, cmd = btn.dataset.cmd;
    if (cmd === 'stop_now' && !confirm('Fade out and stop ' + name + ' now?')) return;
    R.command(cmd, {}, btn.textContent.trim() + ' · ' + name, btn, name);
  });

  setInterval(tick, 250);
  R.status(1000, 0, render, 0, '/api/v1/stations-overview');
})();
