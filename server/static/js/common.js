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

  R.clock = (sec) => {
    sec = R.num(sec);
    if (sec == null) return '-:--';
    sec = Math.max(0, Math.round(sec));
    const h = Math.floor(sec / 3600), m = Math.floor(sec / 60) % 60, s = sec % 60;
    return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(s).padStart(2, '0');
  };
  R.timeOf = (iso, withDate) => {
    if (!iso) return '';
    const d = new Date(iso);
    const t = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' });
    return withDate ? d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + t : t;
  };
  R.ago = (iso) => {
    if (!iso) return 'never';
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 60) return 'just now';
    if (s < 3600) return Math.round(s / 60) + ' min ago';
    if (s < 86400) return Math.round(s / 3600) + ' h ago';
    return Math.round(s / 86400) + ' d ago';
  };
  R.isoDay = (d) => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  R.who = (p) => ({ title: String(p.title || p.file || '?'), artist: p.artist ? String(p.artist) : '' });
  R.whoLine = (p) => { const w = R.who(p); return (w.artist ? w.artist + ' – ' : '') + w.title; };
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
      const d = await R.api(R.withStation('/api/v1/now?plays=' + loop.plays));
      d.receivedAt = Date.now();
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
  R.status = (ms, plays, onData) => { loop = Object.assign(loop, { ms, plays, onData }); tick(); };
  R.refresh = () => { if (!loop.busy) tick(); };

  R.playRow = (p, withDate) => {
    const open = !p.ended_at;
    const ended = open ? '<span class="tag playing">On air</span>'
      : '<span class="tag ' + R.esc(p.end_reason) + '">' + R.esc(R.REASONS[p.end_reason] || p.end_reason || '-') + '</span>';
    const played = R.num(p.played_s), dur = R.num(p.duration_s);
    return '<tr class="' + (open ? 'open' : '') + '">' +
      '<td class="t">' + R.esc(R.timeOf(p.started_at, withDate)) + '</td>' +
      '<td><span class="tag ' + (p.kind === 'imaging' ? 'imaging' : 'music') + '">' + (p.kind === 'imaging' ? 'Imaging' : 'Music') + '</span></td>' +
      '<td>' + R.esc(R.whoLine(p)) + '<span class="file">' + R.esc((p.directory || '') + p.file) + '</span></td>' +
      '<td class="num">' + R.esc(p.deck || '') + '</td>' +
      '<td class="num">' + (played != null ? R.clock(played) : '') + (dur ? ' / ' + R.clock(dur) : '') + '</td>' +
      '<td>' + ended + '</td></tr>';
  };

  const clk = R.$('server-clock');
  const tickClock = () => { if (clk) clk.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }); };
  tickClock();
  setInterval(tickClock, 1000);
})();
