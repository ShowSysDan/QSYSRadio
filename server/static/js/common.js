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
  // text wider than its box scrolls by like a ticker, ~35 px/s, round and round: a second copy (after a dot)
  // follows the first, so the loop has no seam (el gets class "marq"; called again only re-measures)
  R.marquee = (el, text) => {
    if (!el) return;
    if (el.dataset.text !== text || !(el.firstElementChild && el.firstElementChild.firstElementChild)) {   // new text, or written over
      el.dataset.text = text;
      el.classList.add('marq');
      el.innerHTML = '<span><span>' + R.esc(text) + '</span></span>';
    }
    const run = el.firstChild, one = run.firstChild, over = one.offsetWidth > el.clientWidth + 2;
    if (over !== (run.childNodes.length > 1)) {
      if (over) run.insertAdjacentHTML('beforeend', '<i aria-hidden="true">•</i><span aria-hidden="true">' + R.esc(text) + '</span>');
      else while (run.childNodes.length > 1) run.lastChild.remove();
    }
    el.classList.toggle('scroll', over);
    if (over) {
      // one lap: from the text to its copy (sub-pixel, so the loop does not jump); rewritten only when the width
      // really changed (a new duration shifts the animation, and measuring mid-scroll wobbles by a hundredth)
      const dist = run.lastChild.getBoundingClientRect().left - one.getBoundingClientRect().left;
      if (Math.abs(dist - (parseFloat(el.style.getPropertyValue('--dist')) || 0)) > 0.5) {
        el.style.setProperty('--dist', dist.toFixed(2) + 'px');
        el.style.setProperty('--dur', (dist / 35).toFixed(2) + 's');
      }
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
  // the header drop-down, by name; cur: the station shown (the chosen one, else the server's: the most recent)
  function fillStations(list, cur) {
    const sel = R.$('station-select');
    if (!sel) return;
    sel.hidden = list.length < 2;
    cur = R.station || cur || '';
    R.setHTML(sel, list.slice().sort((a, b) => (a.station < b.station ? -1 : a.station > b.station ? 1 : 0)).map((s) => '<option value="' + R.esc(s.station) + '"' + (s.station === cur ? ' selected' : '') + '>' +
      R.esc(s.station) + (s.online ? (s.fault ? ' (fault)' : '') : ' (offline)') + '</option>').join(''));
  }
  // make a station the header's (every page shows it until another is chosen)
  R.pick = (station) => {
    R.station = station;
    try { localStorage.setItem('qsysradio.station', station); } catch (err) { /* private mode */ }
  };
  R.$('station-select')?.addEventListener('change', (e) => { R.pick(e.target.value); location.reload(); });

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
    if (st.fault) return setPill('stopped', 'fault · ' + s.station);
    if (st.automation && st.stop_after) return setPill('wait', 'stopping · ' + s.station);
    if (st.automation) return setPill('', 'on air · ' + s.station);
    return setPill('stopped', 'stopped · ' + s.station);
  }

  // ── commands ───────────────────────────────────────────────────────────
  // btn (optional): the button that sent it; it shows "busy" until the Core answers, then flashes ok / bad
  const watching = new Map();
  const answered = (btn, cls) => {
    if (!btn) return;
    btn.classList.remove('busy');
    btn.classList.add(cls);
    setTimeout(() => btn.classList.remove(cls), 900);
  };
  // station: another than the header's (the Stations page)
  R.command = async (cmd, args, label, btn, station) => {
    if (!R.canOperate) { R.toast('Your account is read-only.', 'bad'); return null; }
    btn?.classList.add('busy');
    try {
      const r = await R.post('/api/v1/control', { station: station || R.station || undefined, cmd, args: args || {} });
      watching.set(r.id, { label: label || cmd, t: Date.now(), btn });
      R.toast((label || cmd) + ': sent');
      R.refresh();
      return r.id;
    } catch (e) { answered(btn, 'sent-bad'); R.toast((label || cmd) + ': ' + e.message, 'bad'); return null; }
  };
  function noteCommands(cmds) {
    for (const c of cmds || []) {
      const w = watching.get(c.id);
      if (!w || c.state === 'pending' || c.state === 'sent') continue;
      watching.delete(c.id);
      answered(w.btn, c.state === 'done' ? 'sent-ok' : 'sent-bad');
      R.toast(w.label + (c.state === 'done' ? ': done' : ': failed') + (c.note ? ' (' + c.note + ')' : ''), c.state === 'done' ? 'ok' : 'bad');
    }
    for (const [id, w] of watching) if (Date.now() - w.t > 70000) { w.btn?.classList.remove('busy'); watching.delete(id); }
  }

  // ── what is on air (the dashboard and the nav rail) ──────────────────────
  R.playing = (d) => d && (d.state === 'playing' || d.state === 'fading');
  // a one-player station's only deck stays on air while it fades out (before the next song loads on it)
  R.liveDeck = (st) => {
    if (!st || !st.decks) return null;
    const ds = ['A', 'B'].map((id) => st.decks[id]).filter(R.playing);
    return ds.find((d) => d.live && d.state === 'playing') || ds.find((d) => d.state === 'playing') || ds.find((d) => d.live) || null;
  };
  R.core = () => (R.last && R.last.station && R.last.station.online ? R.last.station.state : null);
  // a one-player station (a background-music zone): songs only; body.one-player hides everything marked data-breaks
  R.onePlayer = () => !!(R.last && R.last.station && R.last.station.players === 1);
  // seconds since the Core measured elapsed/remaining (server-side age + time since we fetched)
  R.age = () => (R.num(R.last && R.last.station && R.last.station.state_age_s) || 0) + (Date.now() - (R.last ? R.last.receivedAt : Date.now())) / 1000;

  // the nav rail: the song on air with its countdown, its progress as a line under the rail; library counts on
  // the tabs; file analysis in the header
  const nowEl = R.$('nav-now'), prog = R.$('nav-progress');
  function navNow() {
    const st = R.core(), lv = st && st.automation ? R.liveDeck(st) : null, dt = R.age();
    const rem = lv && R.num(lv.remaining), dur = lv && R.num(lv.duration);
    if (prog) prog.style.width = rem != null && dur ? Math.min(100, Math.max(0, (lv.elapsed + dt) / dur * 100)).toFixed(2) + '%' : '0';
    if (!nowEl) return;
    nowEl.hidden = !R.last;
    const lamp = nowEl.firstElementChild, on = !!lv;
    lamp.className = on ? 'onair-lamp' : 'lamp-off';
    lamp.textContent = on ? 'ON AIR' : (R.last && R.last.station && !R.last.station.online ? 'NO CORE' : 'OFF');
    R.marquee(nowEl.querySelector('.now-t'), on ? R.whoLine(lv) : (st && !st.automation ? 'Automation stopped' : st ? 'Between tracks' : 'Core offline'));
    nowEl.querySelector('.now-rem').textContent = rem != null ? '-' + R.clock(rem - dt) : '';
  }
  function header(d) {
    const s = d && d.station, st = R.core(), lib = (st && st.library) || {};
    document.body.classList.toggle('one-player', R.onePlayer());
    document.querySelectorAll('.tab-n[data-count]').forEach((el) => {
      const n = R.num(lib[el.dataset.count]);
      if (el.textContent !== (n == null ? '' : String(n))) el.textContent = n == null ? '' : n;
    });
    const a = s && s.analysis, chip = R.$('hdr-analysis');
    if (chip) {
      chip.hidden = !(a && a.active && a.left);
      if (!chip.hidden) chip.querySelector('span').textContent = 'Analyzing · ' + a.left + ' left';
    }
    navNow();
  }
  const um = R.$('usermenu');
  if (um) document.addEventListener('click', (e) => { if (um.open && !um.contains(e.target)) um.open = false; });

  // ── deck meters (the Dashboard's decks, the Stations page's rows) ──────
  // dB on the player meter -> percent of the scale, widest around 0 dB (radio.css colours match it)
  const STEPS = [[-60, 0], [-40, 12], [-20, 35], [-10, 52], [0, 70], [10, 86], [20, 100]];
  R.METER_FLOOR = -60;
  R.meterPct = (v) => {
    v = Math.max(-60, Math.min(20, R.num(v) == null ? -120 : v));
    const i = Math.max(1, STEPS.findIndex(([db]) => v <= db)), [a, pa] = STEPS[i - 1], [b, pb] = STEPS[i];
    return pa + (v - a) / (b - a) * (pb - pa);
  };
  const pct = (v) => v.toFixed(1) + '%';
  const meterDb = (v) => Math.max(R.METER_FLOOR, Math.min(20, R.num(v) == null ? R.METER_FLOOR : v));
  // R.meter(key, bar, pk, m, across) after each poll: bar = the dark cover over the lit scale, pk = the peak line,
  // m = the Core's {rms, peak} (null: gone); across = a horizontal meter (else upright)
  const bars = {};
  let frameAt = 0, animating = false;
  R.meter = (key, bar, pk, m, across) => {
    if (!m || !bar || !pk) { delete bars[key]; return; }
    const a = bars[key] = bars[key] || { base: R.METER_FLOOR, vel: 0, pkv: R.METER_FLOOR, pkAt: 0, ph: [0, 1, 2].map(() => Math.random() * 6.3) };
    Object.assign(a, { bar, pk, rms: meterDb(m.rms), peak: meterDb(m.peak), size: across ? 'width' : 'height', pos: across ? 'left' : 'bottom' });
    if (!animating) { animating = true; requestAnimationFrame(animate); }
  };
  // ~30 frames a second: the bar follows the reported rms like a damped spring (no corners; quicker up than down)
  // and sways gently around it while there is sound (wider for music with more dynamics: peak well over rms);
  // the peak line holds 1 s, then falls
  const put = (obj, prop, v) => { if (obj[prop] !== v) obj[prop] = v; };
  function animate(t) {
    requestAnimationFrame(animate);
    const dt = Math.min(0.1, (t - frameAt) / 1000), s = t / 1000;
    if (dt < 0.03) return;
    frameAt = t;
    for (const key in bars) {
      const a = bars[key];
      const w = a.rms > a.base ? 4.5 : 3.2;
      a.vel += (w * w * (a.rms - a.base) - 2 * w * a.vel) * dt;
      a.base += a.vel * dt;
      const sound = Math.min(1, Math.max(0, (a.base - R.METER_FLOOR - 5) / 10));
      const swing = sound * Math.min(2.5, Math.max(1, (a.peak - a.rms) / 5));
      const v = a.base + swing * (0.55 * Math.sin(1.3 * s + a.ph[0]) + 0.3 * Math.sin(3.1 * s + a.ph[1]) + 0.15 * Math.sin(6.3 * s + a.ph[2]));
      if (a.peak >= a.pkv) { a.pkv = a.peak; a.pkAt = s; } else if (s - a.pkAt > 1) a.pkv = Math.max(a.peak, a.pkv - 10 * dt);
      put(a.bar.style, a.size, pct(100 - R.meterPct(v)));
      put(a.pk.style, a.pos, pct(R.meterPct(Math.max(a.pkv, v))));
    }
  }

  // ── the status loop (one per page) ─────────────────────────────────────
  // path: the page's own poll instead of /api/v1/now (the Stations page), answering the same header fields and the
  // commands it is waiting on (cmds=)
  let loop = { ms: 5000, plays: 0, onData: null, timer: null, busy: false };
  async function tick() {
    clearTimeout(loop.timer);
    if (loop.busy) return;
    loop.busy = true;
    try {
      const sent = Date.now();
      const d = await R.api(R.withStation(loop.path ? loop.path + '?cmds=' + [...watching.keys()].join(',')
        : '/api/v1/now?plays=' + loop.plays + (loop.ahead ? '&ahead=' + loop.ahead : '')));
      d.receivedAt = Date.now();
      R.last = d;
      setClock(d.clock, sent, d.receivedAt);
      fillStations(d.stations || [], d.station && d.station.station);
      pillFor(d.station);
      noteCommands(((d.station && d.station.commands) || []).concat(d.commands || []));
      header(d);
      if (loop.onData) loop.onData(d);
    } catch (e) {
      setPill('stopped', 'server offline');
      if (loop.onData) loop.onData(null, e);
    } finally {
      loop.busy = false;
      loop.timer = setTimeout(tick, document.hidden ? Math.max(loop.ms, 5000) : loop.ms);
    }
  }
  R.status = (ms, plays, onData, ahead, path) => { loop = Object.assign(loop, { ms, plays, onData, ahead, path }); tick(); };
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
    navNow();
    if (!clk || !clock) return;
    clk.textContent = new Date(Date.now() + clock.offset + clock.zoneMin * 60000).toISOString().slice(11, 19);
  }
  setInterval(tickClock, 1000);
})();
