// QSYSRadio shared helpers + the one status loop every page uses.
// Pages poll short GETs (no long-held connections); every request times out.
(function () {
  'use strict';
  const R = (window.Radio = {});
  R.canOperate = document.body.dataset.canOperate === '1';
  R.isAdmin = document.body.dataset.isAdmin === '1';
  R.$ = (id) => document.getElementById(id);
  R.esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  R.num = (v) => (typeof v === 'number' && isFinite(v) ? v : null);
  R.REASONS = { end: 'End', silence: 'Silence', cut: 'Skipped', eof: 'End of file', core_restart: 'Core restart' };
  R.KINDS = { music: 'Music', imaging: 'Imaging', ad: 'Ad' };
  // a level in dB with its sign ("+4.2", "−6.0"); at or under floor (the meter's / gain's bottom) "−∞"
  R.db = (v, floor) => {
    v = R.num(v);
    if (v == null) return '–';
    if (floor != null && v <= floor + 0.5) return '−∞';
    return (v > 0.04 ? '+' : v < -0.04 ? '−' : '') + Math.abs(v).toFixed(1);
  };

  R.clock = (sec) => {
    sec = R.num(sec);
    if (sec == null) return '-:--';
    sec = Math.max(0, Math.round(sec));
    const h = Math.floor(sec / 3600), m = Math.floor(sec / 60) % 60, s = sec % 60;
    return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(s).padStart(2, '0');
  };
  // every time on the panel is the server's (its zone, 24 h); the browser's zone only until the first poll
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const zoneMs = () => (clock ? clock.zoneMin : -new Date().getTimezoneOffset()) * 60000;
  R.timeOf = (iso, withDate) => {
    if (!iso) return '';
    const d = new Date(new Date(iso).getTime() + zoneMs());       // read with UTC getters = server wall time
    return (withDate ? MONTHS[d.getUTCMonth()] + ' ' + d.getUTCDate() + ' ' : '') + d.toISOString().slice(11, 19);
  };
  R.serverHM = (iso) => R.timeOf(iso).slice(0, 5);
  R.serverDay = () => new Date(Date.now() + (clock ? clock.offset : 0) + zoneMs()).toISOString().slice(0, 10);
  // 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM[:SS]' read as server wall time -> a Date (plus `days`)
  R.fromServer = (s, days) => {
    const [y, m, d, hh, mm, ss] = s.split(/[-T:]/).map(Number);
    return new Date(Date.UTC(y, m - 1, d + (days || 0), hh || 0, mm || 0, ss || 0) - zoneMs());
  };
  R.ago = (iso) => {
    if (!iso) return 'never';
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 60) return 'just now';
    if (s < 3600) return Math.round(s / 60) + ' min ago';
    if (s < 86400) return Math.round(s / 3600) + ' h ago';
    return Math.round(s / 86400) + ' d ago';
  };
  R.who = (p) => ({ title: String(p.title || p.file || '?'), artist: p.artist ? String(p.artist) : '' });
  R.whoLine = (p) => { const w = R.who(p); return (w.artist ? w.artist + ' – ' : '') + w.title; };
  // text that slides back and forth when it is wider than its box (el gets class "marq")
  R.marquee = (el, text) => {
    if (!el) return;
    if (el.dataset.text !== text) {
      el.dataset.text = text;
      el.classList.add('marq');
      el.innerHTML = '<span>' + R.esc(text) + '</span>';
    }
    const over = el.firstChild.scrollWidth - el.clientWidth;
    el.classList.toggle('scroll', over > 2);
    if (over > 2) {
      el.style.setProperty('--dist', over + 'px');
      el.style.setProperty('--dur', Math.max(6, over / 25 + 4) + 's');   // ~25 px/s, with pauses at each end
    }
  };
  // write innerHTML only when it changed (keeps buttons alive under the cursor)
  R.setHTML = (el, html) => { if (el && el.dataset.html !== html) { el.innerHTML = html; el.dataset.html = html; } };

  R.api = async (url, opts) => {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 8000);
    try {
      const r = await fetch(url, Object.assign({ cache: 'no-store', credentials: 'same-origin', signal: ctl.signal }, opts || {}));
      if (r.status === 401) { location.href = '/login?next=' + encodeURIComponent(location.pathname); throw new Error('signed out'); }
      const body = await r.json().catch(() => ({}));
      if (!r.ok || body.ok === false) throw new Error(body.error || ('HTTP ' + r.status));
      return body;
    } catch (e) {
      throw e.name === 'AbortError' ? new Error('no answer in 8 s') : e;
    } finally { clearTimeout(timer); }
  };
  R.post = (url, data) => R.api(url, {
    method: 'POST', body: JSON.stringify(data),
    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'QSYSRadio' },
  });

  R.toast = (msg, kind) => {
    const el = document.createElement('div');
    el.className = 'toast ' + (kind || '');
    el.textContent = msg;
    R.$('toasts').appendChild(el);
    setTimeout(() => el.remove(), kind === 'bad' ? 7000 : 4000);
  };

  // ── station selection ──────────────────────────────────────────────────
  R.station = (() => { try { return localStorage.getItem('qsysradio.station') || ''; } catch (e) { return ''; } })();
  R.withStation = (url) => R.station ? url + (url.includes('?') ? '&' : '?') + 'station=' + encodeURIComponent(R.station) : url;
  function fillStations(list) {
    const sel = R.$('station-select');
    if (!sel) return;
    sel.hidden = list.length < 2;
    const cur = R.station || (list[0] && list[0].station) || '';
    R.setHTML(sel, list.map((s) => '<option value="' + R.esc(s.station) + '"' + (s.station === cur ? ' selected' : '') + '>' +
      R.esc(s.station) + (s.online ? '' : ' (offline)') + '</option>').join(''));
  }
  R.$('station-select')?.addEventListener('change', (e) => {
    R.station = e.target.value;
    try { localStorage.setItem('qsysradio.station', R.station); } catch (err) { /* private mode */ }
    location.reload();
  });

  // ── header pill ────────────────────────────────────────────────────────
  function setPill(cls, text) {
    const p = R.$('station-pill');
    if (!p) return;
    p.className = 'status-pill ' + cls;
    p.querySelector('span').textContent = text;
  }
  function pillFor(s) {
    if (!s) return setPill('wait', 'no core yet');
    const st = s.state || {};
    if (!s.online) return setPill('stopped', 'core offline');
    if (st.automation && st.stop_after) return setPill('wait', 'stopping · ' + s.station);
    if (st.automation) return setPill('', 'on air · ' + s.station);
    return setPill('stopped', 'stopped · ' + s.station);
  }

  // ── commands ───────────────────────────────────────────────────────────
  const watching = new Map();
  R.command = async (cmd, args, label) => {
    if (!R.canOperate) { R.toast('Your account is read-only.', 'bad'); return null; }
    try {
      const r = await R.post('/api/v1/control', { station: R.station || undefined, cmd, args: args || {} });
      watching.set(r.id, { label: label || cmd, t: Date.now() });
      R.toast((label || cmd) + ': sent');
      R.refresh();
      return r.id;
    } catch (e) { R.toast((label || cmd) + ': ' + e.message, 'bad'); return null; }
  };
  function noteCommands(cmds) {
    for (const c of cmds || []) {
      const w = watching.get(c.id);
      if (!w || c.state === 'pending' || c.state === 'sent') continue;
      watching.delete(c.id);
      R.toast(w.label + (c.state === 'done' ? ': done' : ': failed') + (c.note ? ' (' + c.note + ')' : ''), c.state === 'done' ? 'ok' : 'bad');
    }
    for (const [id, w] of watching) if (Date.now() - w.t > 70000) watching.delete(id);
  }

  // ── the status loop (one per page) ─────────────────────────────────────
  let loop = { ms: 5000, plays: 0, onData: null, timer: null, busy: false };
  async function tick() {
    clearTimeout(loop.timer);
    if (loop.busy) return;
    loop.busy = true;
    try {
      const sent = Date.now();
      const d = await R.api(R.withStation('/api/v1/now?plays=' + loop.plays + (loop.ahead ? '&ahead=' + loop.ahead : '')));
      d.receivedAt = Date.now();
      setClock(d.clock, sent, d.receivedAt);
      fillStations(d.stations || []);
      pillFor(d.station);
      noteCommands(d.station && d.station.commands);
      if (loop.onData) loop.onData(d);
    } catch (e) {
      setPill('stopped', 'server offline');
      if (loop.onData) loop.onData(null, e);
    } finally {
      loop.busy = false;
      loop.timer = setTimeout(tick, document.hidden ? Math.max(loop.ms, 5000) : loop.ms);
    }
  }
  R.status = (ms, plays, onData, ahead) => { loop = Object.assign(loop, { ms, plays, onData, ahead }); tick(); };
  R.refresh = () => { if (!loop.busy) tick(); };

  R.playRow = (p, withDate) => {
    const open = !p.ended_at;
    const ended = open ? '<span class="tag playing">On air</span>'
      : '<span class="tag ' + R.esc(p.end_reason) + '">' + R.esc(R.REASONS[p.end_reason] || p.end_reason || '-') + '</span>';
    const played = R.num(p.played_s), dur = R.num(p.duration_s);
    return '<tr class="' + (open ? 'open' : '') + '">' +
      '<td class="t">' + R.esc(R.timeOf(p.started_at, withDate)) + '</td>' +
      '<td><span class="tag ' + (R.KINDS[p.kind] ? p.kind : 'music') + '">' + (R.KINDS[p.kind] || 'Music') + '</span></td>' +
      '<td>' + R.esc(R.whoLine(p)) + '<span class="file">' + R.esc((p.directory || '') + p.file) + '</span></td>' +
      '<td class="num">' + R.esc(p.deck || '') + '</td>' +
      '<td class="num">' + (played != null ? R.clock(played) : '') + (dur ? ' / ' + R.clock(dur) : '') + '</td>' +
      '<td>' + ended + '</td></tr>';
  };

  // ── header clock: the server's time in its zone, 24 h; ticks locally between polls ──
  const clk = R.$('server-clock');
  let clock = null;                       // { offset: server ms - browser ms, zoneMin, zone, ntp }
  function setClock(c, sent, received) {
    if (!c || !R.num(c.server_ms)) return;
    clock = { offset: c.server_ms - (sent + received) / 2, zoneMin: c.utc_offset_min || 0, zone: c.zone || '', ntp: c.ntp || {} };
    const n = clock.ntp, off = R.num(n.offset_s);
    clk.classList.toggle('bad', n.ok === false);
    clk.classList.toggle('ok', n.ok === true);
    clk.title = 'Server time (' + clock.zone + '). ' + (!n.server ? 'No NTP check: set an NTP server in Settings → Server settings.'
      : n.error ? 'NTP ' + n.server + ' unreachable: ' + n.error
      : off == null ? 'Checking NTP ' + n.server + '…'
      : 'NTP ' + n.server + ': server is ' + (off >= 0 ? '+' : '') + off.toFixed(3) + ' s off (allowed ' + n.max_s + ' s), checked ' +
        Math.round((n.checked_ago_s || 0) / 60) + ' min ago.');
    tickClock();
  }
  function tickClock() {
    if (!clk || !clock) return;
    clk.textContent = new Date(Date.now() + clock.offset + clock.zoneMin * 60000).toISOString().slice(11, 19);
  }
  setInterval(tickClock, 1000);
})();
