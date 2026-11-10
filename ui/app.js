'use strict';
/* openhop.openbot operator console. Vanilla JS, no build step, no CDN.
 * Every mesh-sourced string is rendered via textContent (see h()); never innerHTML. */

const PLUGIN_ID = 'openhop.openbot';
const MAX_BYTES = 160;
const POLL_MS = 3000;
const STALE_S = 20;
const API_SETTINGS = '/api/plugins/settings';
const API_RUNTIME = '/api/plugins/runtime';
const API_UPDATES = '/api/plugins/updates';
const API_UPDATE = '/api/plugins/update';
const REPO = 'zfouts/openhop-openbot-plugin';
const RECHECK_S = 6 * 3600;

const DEFAULTS = {
  schema_version: 1, log_level: 'INFO',
  companion: { host: '127.0.0.1', port: 5001 },
  node: { name: '', latitude: null, longitude: null, timezone: 'America/Chicago', units: 'imperial', path_hash_bytes: 2 },
  channels: [], regions: [],
  scope: { channels: [], regions: [], allow_dm: true, protected_channels: ['Public'], max_hops: null },
  limits: { tx_gap_s: 2.5, tx_max_per_min: 8, user_cooldown_s: 5, reply_in_request_region: true },
  commands: [], announcements: [],
  weather: { units: 'auto', default_country: 'us', contact: '' },
  corescope: { enabled: false, base_url: '', shortener: 'v.gd' },
};

const TABS = [
  ['status', 'Status'], ['commands', 'Commands'], ['scope', 'Scope & Regions'], ['announce', 'Announcements'],
  ['weather', 'Weather'], ['send', 'Send'], ['activity', 'Activity'], ['usage', 'Usage'], ['nodes', 'Nodes'],
  ['corescope', 'CoreScope'], ['advanced', 'Advanced'],
];

let cfg = null;          // working copy
let savedStr = '';       // JSON of last loaded/saved config
let rt = null;           // runtime object ({} when empty)
let rtState = 'loading'; // loading | ok | empty | error
let rtError = '';
let curTab = 'status';
let view = null;         // {el, refresh?}
let authRequired = false;
let lastReplyTA = null;
let uid = 0;

// ---------------------------------------------------------------- DOM helpers
function h(tag, props, ...kids) {
  const e = document.createElement(tag);
  const late = {};
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k === 'text') e.textContent = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (k === 'value' || k === 'checked' || k === 'disabled') late[k] = v;
    else e.setAttribute(k, v === true ? '' : v);
  }
  const add = (c) => {
    if (c == null || c === false) return;
    if (Array.isArray(c)) c.forEach(add);
    else e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  };
  kids.forEach(add);
  Object.assign(e, late);
  return e;
}
const $ = (s) => document.querySelector(s);
const clone = (o) => JSON.parse(JSON.stringify(o));
const isObj = (o) => o && typeof o === 'object' && !Array.isArray(o);
const bytes = (s) => new TextEncoder().encode(String(s ?? '')).length;
function deepMerge(base, over) {
  const out = clone(base);
  for (const [k, v] of Object.entries(over || {})) {
    out[k] = isObj(v) && isObj(out[k]) ? deepMerge(out[k], v) : clone(v);
  }
  return out;
}
function ago(ts) {
  if (!ts) return 'never';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm ago';
  return Math.floor(s / 86400) + 'd ago';
}
function dur(s) {
  s = Math.max(0, Math.floor(s));
  const d = Math.floor(s / 86400), hh = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return (d ? d + 'd ' : '') + (d || hh ? hh + 'h ' : '') + m + 'm ' + (s % 60) + 's';
}
function clock(ts) { return new Date(ts * 1000).toLocaleTimeString([], { hour12: false }); }

let toastTimer = null;
function toast(msg, bad) {
  const t = $('#toast');
  t.textContent = msg; t.className = 'toast' + (bad ? ' error' : ''); t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, bad ? 7000 : 3500);
}

// ---------------------------------------------------------------- API / auth
// Token discovery and Bearer header copied from the Dhovin plugin UI approach:
// ?token= query param, then common storage keys, same-origin cookies as fallback.
const TOKEN_KEYS = ['pymc_jwt_token', 'openhop_token', 'auth_token', 'access_token', 'jwt', 'token'];
function getToken() {
  try {
    const q = new URLSearchParams(location.search).get('token');
    if (q && q.trim()) { saveToken(q.trim()); return q.trim(); }
  } catch (_) { /* ignore */ }
  const stores = [];
  for (const get of [() => localStorage, () => sessionStorage, () => parent.localStorage, () => parent.sessionStorage]) {
    try { stores.push(get()); } catch (_) { /* cross-origin or blocked */ }
  }
  for (const s of stores) for (const k of TOKEN_KEYS) {
    try { const v = s.getItem(k); if (v && v.trim()) return v.trim().replace(/^["']|["']$/g, ''); } catch (_) { /* ignore */ }
  }
  return '';
}
function saveToken(t) {
  try { sessionStorage.setItem('pymc_jwt_token', t); } catch (_) { /* ignore */ }
  try { localStorage.setItem('pymc_jwt_token', t); } catch (_) { /* ignore */ }
}
function headers() {
  const hd = { Accept: 'application/json', 'Content-Type': 'application/json' };
  const t = getToken();
  if (t) hd.Authorization = 'Bearer ' + t;
  return hd;
}
async function api(method, url, body) {
  let res;
  try {
    res = await fetch(url, { method, headers: headers(), credentials: 'same-origin', cache: 'no-store',
      body: body === undefined ? undefined : JSON.stringify(body) });
  } catch (e) {
    const err = new Error('Cannot reach the repeater (' + e.message + ')');
    err.network = true;
    throw err;
  }
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) { showAuth(true); const e = new Error('Sign-in required'); e.auth = true; throw e; }
  if (!res.ok || data.success === false) {
    const err = new Error(data.error || data.message || 'HTTP ' + res.status);
    err.status = res.status; err.outcome = data.outcome;
    throw err;
  }
  if (authRequired) showAuth(false);
  return data;
}

function showAuth(on, err) {
  authRequired = on;
  const box = $('#auth');
  box.hidden = !on;
  $('#view').hidden = on;
  if (!on) { box.replaceChildren(); return; }
  const u = h('input', { id: 'au', autocomplete: 'username', required: true });
  const p = h('input', { id: 'ap', type: 'password', autocomplete: 'current-password', required: true });
  const msg = h('p', { class: 'hint', role: 'alert', text: err || '' });
  const form = h('form', { class: 'card stack', onsubmit: async (ev) => {
    ev.preventDefault();
    try {
      const r = await fetch('/auth/login', { method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ username: u.value, password: p.value }) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || !d.token) throw new Error(d.error || d.message || 'Login failed (HTTP ' + r.status + ')');
      saveToken(d.token); showAuth(false); await boot();
    } catch (e) { msg.textContent = e.message; }
  } },
  h('h2', { text: 'Sign in to the repeater' }),
  h('p', { class: 'muted', text: 'The repeater API requires authentication.' }),
  field('Username', u), field('Password', p), msg,
  h('button', { type: 'submit', class: 'primary', text: 'Sign in' }));
  box.replaceChildren(form);
}

async function fetchConfig() {
  const r = await api('GET', API_SETTINGS + '?id=' + encodeURIComponent(PLUGIN_ID));
  const c = r.config || r.saved || r.defaults || {};
  const out = deepMerge(DEFAULTS, c);
  delete out._cmd; delete out._runtime;
  return out;
}
async function fetchRuntime() {
  try {
    const r = await api('GET', API_RUNTIME + '?id=' + encodeURIComponent(PLUGIN_ID));
    const run = isObj(r.runtime) ? r.runtime : {};
    if (!Object.keys(run).length) { rt = {}; rtState = 'empty'; }
    else { rt = run; rtState = 'ok'; }
    rtError = '';
  } catch (e) {
    if (e.auth) return;
    rtState = 'error'; rtError = e.message;
    if (!rt) rt = {};
  }
}
async function postConfig(config) {
  const body = clone(config); delete body._runtime;
  return api('POST', API_SETTINGS, { id: PLUGIN_ID, config: body, restart: false });
}

// ---------------------------------------------------------------- dirty / validation
const isDirty = () => cfg && JSON.stringify(cfg) !== savedStr;

function validate() {
  const e = [];
  const c = cfg;
  const ids = new Set(), trigs = new Set();
  c.commands.forEach((m, i) => {
    const n = 'Command ' + (i + 1) + (m.id ? ' (' + m.id + ')' : '');
    if (!m.id || !String(m.id).trim()) e.push(n + ': id is required');
    else if (ids.has(m.id)) e.push(n + ': duplicate id "' + m.id + '"');
    ids.add(m.id);
    const t = String(m.trigger || '').trim().toLowerCase();
    if (!t) e.push(n + ': trigger is required');
    else if (trigs.has(t)) e.push(n + ': duplicate trigger "' + m.trigger + '"');
    trigs.add(t);
    if (typeof m.cooldown_s !== 'number' || !isFinite(m.cooldown_s) || m.cooldown_s < 0) e.push(n + ': cooldown must be a number of 0 or more');
  });
  const idx = new Set();
  c.channels.forEach((ch, i) => {
    const n = 'Channel slot ' + (ch.idx === '' ? '?' : ch.idx);
    if (!Number.isInteger(ch.idx) || ch.idx < 1 || ch.idx > 39) e.push('Channel row ' + (i + 1) + ': slot must be an integer from 1 to 39');
    else if (idx.has(ch.idx)) e.push(n + ': duplicate slot');
    idx.add(ch.idx);
    const sec = String(ch.secret || '').trim();
    if (sec && !/^[0-9a-f]{32}$/i.test(sec)) e.push(n + ': secret must be 32 hex characters');
    if (!sec && !String(ch.name || '').startsWith('#')) e.push(n + ': name must start with # (or give a 32-hex secret)');
  });
  const rn = new Set();
  c.regions.forEach((r) => {
    if (!String(r).startsWith('#')) e.push('Region "' + r + '" must start with #');
    if (rn.has(r)) e.push('Duplicate region "' + r + '"');
    rn.add(r);
  });
  const aid = new Set();
  c.announcements.forEach((a, i) => {
    const n = 'Announcement ' + (i + 1) + (a.id ? ' (' + a.id + ')' : '');
    if (!a.id || !String(a.id).trim()) e.push(n + ': id is required');
    else if (aid.has(a.id)) e.push(n + ': duplicate id');
    aid.add(a.id);
    if (!String(a.channel || '').trim()) e.push(n + ': channel is required');
    if (!validSchedule(a.schedule)) e.push(n + ': schedule must be a 5-field cron expression or "every <seconds>"');
  });
  const num = (v, lbl, min, max) => {
    if (typeof v !== 'number' || !isFinite(v) || v < min || (max != null && v > max)) e.push(lbl + ' must be a number' + (max != null ? ' from ' + min + ' to ' + max : ' of ' + min + ' or more'));
  };
  num(c.companion.port, 'Companion port', 1, 65535);
  num(c.node.path_hash_bytes, 'path_hash_bytes', 1, 3);
  num(c.limits.tx_gap_s, 'tx_gap_s', 0); num(c.limits.tx_max_per_min, 'tx_max_per_min', 0); num(c.limits.user_cooldown_s, 'user_cooldown_s', 0);
  if ((c.corescope.enabled || c.corescope.base_url) && !/^https?:\/\/[^\/\s]+/.test(c.corescope.base_url || '')) e.push('CoreScope base URL must start with http:// or https://');
  if (c.weather.default_country && !/^[A-Za-z]{2}$/.test(c.weather.default_country)) e.push('Default country must be a 2-letter code such as us, gb or de');
  if (c.node.latitude != null) num(c.node.latitude, 'Latitude', -90, 90);
  if (c.node.longitude != null) num(c.node.longitude, 'Longitude', -180, 180);
  if (c.scope.max_hops != null) num(c.scope.max_hops, 'max_hops', 0);
  return e;
}
function validSchedule(s) {
  s = String(s || '').trim();
  if (/^every\s+\d+(\.\d+)?$/i.test(s)) return parseFloat(s.split(/\s+/)[1]) > 0;
  const f = s.split(/\s+/);
  return f.length === 5 && f.every((x) => /^[0-9a-zA-Z*,/\-?]+$/.test(x));
}

function changed(cb) { if (cb) cb(); chrome(); }
function chrome() {
  const d = isDirty();
  const errs = cfg ? validate() : [];
  $('#dirty').hidden = !d;
  $('#btn-save').disabled = !d;
  $('#btn-revert').disabled = !d;
  const p = $('#problems');
  if (errs.length && d) {
    p.hidden = false;
    p.replaceChildren(h('b', { text: errs.length + (errs.length === 1 ? ' problem' : ' problems') + ' to fix before saving' }), h('ul', {}, errs.map((x) => h('li', { text: x }))));
  } else p.hidden = true;
  // connection pill
  const conn = $('#conn');
  const st = rt && rt.status ? rt.status.companion : null;
  let cls = 'pill', txt = 'Loading';
  if (rtState === 'error') { cls += ' bad'; txt = 'API error'; }
  else if (rtState === 'empty') { cls += ' warn'; txt = 'Bot not running'; }
  else if (rtState === 'ok') {
    if (stale()) { cls += ' warn'; txt = 'Bot not updating'; }
    else if (st === 'connected') { cls += ' ok'; txt = 'Companion connected'; }
    else { cls += ' warn'; txt = 'Companion ' + (st || 'unknown'); }
  }
  conn.className = cls; conn.textContent = txt;
  $('#ver').textContent = rt && rt.version ? 'v' + rt.version : '';
  banner();
  updNotice();
}
const stale = () => rt && rt.timestamp && Date.now() / 1000 - rt.timestamp > STALE_S;
function banner() {
  const b = $('#banner');
  // While an update runs the bot is down on purpose; the update notice says so.
  if (updRunning()) { b.hidden = true; return; }
  let msg = '', cls = 'banner';
  if (rtState === 'error') { msg = 'Cannot read runtime: ' + rtError + '. Retrying every 3 s.'; cls += ' bad'; }
  else if (rtState === 'empty') { msg = 'No runtime data yet. The plugin is probably not running, or has not written runtime.json. You can still edit and save the configuration.'; cls += ' warn'; }
  else if (rtState === 'ok' && stale()) { msg = 'The bot last wrote runtime data ' + ago(rt.timestamp) + '. It may have stopped; values below are stale.'; cls += ' warn'; }
  b.hidden = !msg; b.className = cls; b.textContent = msg;
}

// ---------------------------------------------------------------- form builders
function field(label, control, hint, cls) {
  return h('label', { class: 'field ' + (cls || '') }, h('span', { class: 'lbl', text: label }), control, hint ? h('small', { text: hint }) : null);
}
function textIn(obj, key, o = {}) {
  const el = h('input', { type: o.type || 'text', class: o.mono ? 'mono' : '', placeholder: o.ph, autocomplete: 'off', spellcheck: 'false',
    list: o.list, min: o.min, max: o.max, step: o.step, 'aria-label': o.aria });
  el.value = obj[key] == null ? '' : obj[key];
  el.addEventListener('input', () => {
    let v = el.value;
    if (o.num) v = v === '' ? (o.nullable ? null : '') : Number(v);
    else if (o.nullable && v === '') v = null;
    obj[key] = v;
    const bad = o.num && v !== null && (typeof v !== 'number' || !isFinite(v));
    el.setAttribute('aria-invalid', bad ? 'true' : 'false');
    changed(o.onchange);
  });
  return el;
}
function numIn(obj, key, o = {}) { return textIn(obj, key, { ...o, type: 'number', num: true, step: o.step || 'any' }); }
function check(label, obj, key, o = {}) {
  const el = h('input', { type: 'checkbox', checked: !!obj[key] });
  el.addEventListener('change', () => { obj[key] = el.checked; changed(o.onchange); });
  return h('label', { class: 'chk' }, el, h('span', { text: label }));
}
function sel(obj, key, opts, o = {}) {
  const el = h('select', {}, opts.map(([v, l]) => h('option', { value: v, text: l })));
  el.value = obj[key] == null ? '' : String(obj[key]);
  el.addEventListener('change', () => { obj[key] = o.map ? o.map(el.value) : el.value; changed(o.onchange); });
  return el;
}
function datalist(id, vals) { return h('datalist', { id }, [...new Set(vals)].map((v) => h('option', { value: v }))); }
function byteCounter(getText) {
  const s = h('span', { class: 'counter-bytes' });
  const upd = () => {
    const n = bytes(getText());
    s.textContent = n + ' / ' + MAX_BYTES + ' bytes' + (n > MAX_BYTES ? ' (over: will be truncated on air)' : '');
    s.classList.toggle('over', n > MAX_BYTES);
  };
  upd();
  return [s, upd];
}
function textarea(obj, key, o = {}) {
  const el = h('textarea', { class: 'mono', rows: o.rows || 3, spellcheck: 'false', 'aria-label': o.aria });
  el.value = obj[key] ?? '';
  el.addEventListener('input', () => { obj[key] = el.value; changed(o.onchange); });
  return el;
}

/* chips: edits array `arr` in place. `inherit` is shown as a muted chip when the array is empty. */
function chips(arr, o = {}) {
  const wrap = h('div', { class: 'chips' });
  const id = 'dl' + (++uid);
  const inp = h('input', { placeholder: o.ph || 'Add and press Enter', list: id, 'aria-label': o.aria || 'Add value', autocomplete: 'off' });
  const render = () => {
    wrap.replaceChildren();
    if (!arr.length && o.inherit) wrap.append(h('span', { class: 'chip inherit', text: 'inherits: ' + (typeof o.inherit === 'function' ? o.inherit() : o.inherit) }));
    arr.forEach((v, i) => wrap.append(h('span', { class: 'chip' }, v,
      h('button', { type: 'button', 'aria-label': 'Remove ' + v, text: '×', onclick: () => { arr.splice(i, 1); render(); changed(o.onchange); } }))));
    wrap.append(inp, datalist(id, (o.suggest ? o.suggest() : []).filter((s) => !arr.includes(s))));
  };
  const add = () => {
    const v = inp.value.trim().replace(/,$/, '');
    inp.value = '';
    if (v && !arr.includes(v)) { arr.push(v); render(); changed(o.onchange); inp.focus(); }
  };
  inp.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ',') { ev.preventDefault(); add(); } else if (ev.key === 'Backspace' && !inp.value && arr.length) { arr.pop(); render(); changed(o.onchange); inp.focus(); } });
  inp.addEventListener('change', add);
  render();
  return wrap;
}

const channelNames = () => [...(cfg.channels || []).map((c) => c.name).filter(Boolean)];
const regionNames = () => [...(cfg.regions || [])];

// ---------------------------------------------------------------- actions (mailbox)
async function waitResult(id, ms) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    await new Promise((r) => setTimeout(r, 700));
    await fetchRuntime();
    const res = rt && rt.mailbox && rt.mailbox.results && rt.mailbox.results[id];
    if (res !== undefined) { chrome(); return res; }
  }
  return null;
}
/* Returns {id, result|null} or null when aborted/failed. */
async function runAction(action, args, o = {}) {
  try {
    if (isDirty()) {
      const errs = validate();
      if (errs.length) { toast('Fix the listed problems first; this action also saves your edits.', true); return null; }
      if (!confirm('You have unsaved edits. This action saves the whole configuration together with it.\n\nOK = save edits and run "' + action + '"\nCancel = do nothing')) return null;
    }
    const wasDirty = isDirty();
    const base = wasDirty ? clone(cfg) : await fetchConfig();
    const id = 'c-' + Math.floor(Date.now() / 1000) + '-' + Math.random().toString(36).slice(2, 6);
    const payload = clone(base);
    payload._cmd = { id, action, args: args || {} };
    await postConfig(payload);
    delete base._cmd;
    // Keep the live cfg object: rendered controls hold references into it.
    const external = !wasDirty && JSON.stringify(base) !== JSON.stringify(cfg);
    if (external) cfg = base;
    savedStr = JSON.stringify(cfg);
    if (external) draw(); else chrome();
    const result = await waitResult(id, o.timeout || 10000);
    return { id, result };
  } catch (e) {
    toast('Action failed: ' + e.message, true);
    return null;
  }
}
function resultNode(r) {
  if (!r) return h('div', { class: 'result' }, h('b', { text: 'No result after 10 s. ' }), 'The bot may not be running; the command stays queued in config.');
  return h('div', { class: 'result' }, h('pre', { text: JSON.stringify(r.result, null, 2) }));
}

// ---------------------------------------------------------------- updates
// openHop's plugin manager does the work. GET /api/plugins/updates asks GitHub for
// the latest full release (never a pre-release) of the repository recorded in the
// plugin's state.json and compares versions the way pip does. POST
// /api/plugins/update downloads that release's wheel, stops the bot, installs it
// and starts it again; settings and data are kept.
let upd = { state: 'idle', info: null, error: '', at: 0 }; // idle | checking | ok | error
let run = null; // {phase: installing | restarting | done | failed, target, started, error}

const updRunning = () => !!run && run.phase !== 'failed';
const updAvailable = () => upd.state === 'ok' && !!upd.info && !!upd.info.updateAvailable && !!upd.info.latestVersion;
const safeUrl = (u) => (typeof u === 'string' && /^https:\/\/github\.com\//.test(u) ? u : null);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const runningVersion = () => (rt && rt.version) || (upd.info && upd.info.installedVersion) || null;

/* -1 / 0 / 1 for X.Y.Z[+local] strings; a +local build sorts above its base, as in pip. */
function cmpVer(a, b) {
  const parse = (v) => { const [base, local] = String(v || '').replace(/^v/, '').split('+'); return [base.split(/[.-]/).map((x) => parseInt(x, 10) || 0), !!local]; };
  const [x, xl] = parse(a), [y, yl] = parse(b);
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) > (y[i] || 0) ? 1 : -1;
  }
  return xl === yl ? 0 : xl ? 1 : -1;
}

/* Rebuild el only when sig changes, so buttons are not swapped out under the pointer. */
function paint(el, sig, build) {
  if (el._sig === sig) return;
  el._sig = sig;
  build();
}

function updRedraw() {
  chrome();
  if (curTab === 'status' && view && view.refresh) view.refresh();
}

async function checkUpdates(force) {
  if (updRunning()) return;
  upd = { ...upd, state: 'checking' };
  updRedraw();
  try {
    const r = await api('GET', API_UPDATES + '?id=' + encodeURIComponent(PLUGIN_ID) + (force ? '&refresh=1' : ''));
    upd = { state: 'ok', info: r, error: '', at: Date.now() / 1000 };
  } catch (e) {
    upd = { state: e.auth ? 'idle' : 'error', info: null, error: e.message, status: e.status || 0, at: Date.now() / 1000 };
  }
  updRedraw();
}

async function startUpdate() {
  if (!updAvailable() || updRunning()) return;
  if (isDirty()) { toast('Save or revert your edits first: the page reloads after the update.', true); return; }
  const target = upd.info.latestVersion;
  if (!confirm('Update OpenBot from ' + (runningVersion() || '?') + ' to ' + target + '?\n\n'
    + 'openHop downloads the release from GitHub, stops the bot, installs it and starts it again. '
    + 'The bot does not answer commands for about a minute. Settings and data are kept.')) return;
  run = { phase: 'installing', target, started: Date.now() / 1000 };
  updRedraw();
  let lost = false;
  try {
    await api('POST', API_UPDATE, { id: PLUGIN_ID, version: target });
  } catch (e) {
    if (e.auth) { run = null; updRedraw(); return; }
    // A gateway timeout or dropped connection does not stop the install: keep watching the bot.
    lost = !!e.network || e.status === 502 || e.status === 504 || e.outcome === 'unknown';
    if (!lost) { run = { phase: 'failed', target, error: e.message }; updRedraw(); return; }
  }
  run.phase = 'restarting';
  updRedraw();
  const deadline = Date.now() + (lost ? 15 * 60 : 3 * 60) * 1000;
  while (Date.now() < deadline) {
    await sleep(2000);
    await fetchRuntime();
    if (rtState === 'ok' && rt.version === target && !stale()) {
      run.phase = 'done';
      updRedraw();
      await sleep(1000);
      location.reload(); // the page itself ships in the new release
      return;
    }
    chrome();
  }
  run = { phase: 'failed', target, error: 'the bot has not reported version ' + target + ' yet. Check the plugin log on the Plugins page.' };
  updRedraw();
}

/* One-line notice above every tab: an update is available, running, or failed. */
function updNotice() {
  const n = $('#update');
  const secs = run && run.started ? Math.round(Date.now() / 1000 - run.started) : 0;
  const i = upd.info || {};
  const sig = JSON.stringify([run && run.phase, run && run.error, updAvailable() && i.latestVersion, runningVersion(), run && run.phase !== 'failed' ? secs : 0]);
  paint(n, sig, () => {
    const show = (cls, ...kids) => { n.className = 'banner update ' + cls; n.replaceChildren(...kids); n.hidden = false; };
    const elapsed = h('span', { class: 'muted', 'aria-hidden': 'true', text: ' (' + secs + ' s)' });
    if (run && run.phase === 'installing') return show('busy', h('b', { text: 'Updating to ' + run.target + '.' }), ' openHop is downloading and installing the release; the bot is offline meanwhile.', elapsed);
    if (run && run.phase === 'restarting') return show('busy', h('b', { text: 'Updating to ' + run.target + '.' }), ' Waiting for the bot to start the new version.', elapsed);
    if (run && run.phase === 'done') return show('ok', h('b', { text: 'Updated to ' + run.target + '.' }), ' Reloading the page.');
    if (run && run.phase === 'failed') {
      return show('bad', h('span', {}, h('b', { text: 'Update to ' + run.target + ' failed: ' }), run.error),
        h('button', { type: 'button', class: 'sm', text: 'Try again', onclick: () => { run = null; startUpdate(); } }),
        h('button', { type: 'button', class: 'sm', text: 'Dismiss', onclick: () => { run = null; updRedraw(); } }));
    }
    if (updAvailable()) {
      const url = safeUrl(i.releaseUrl);
      return show('', h('span', {}, h('b', { text: 'OpenBot ' + i.latestVersion + ' is available' }), ' (running ' + (runningVersion() || '?') + ').'),
        url ? h('a', { href: url, target: '_blank', rel: 'noopener', text: 'Release notes' }) : null,
        h('button', { type: 'button', class: 'sm primary', text: 'Update now', onclick: startUpdate }));
    }
    n.hidden = true;
    n.replaceChildren();
  });
}

/* Status tab card. */
function updCard(box) {
  const i = upd.info || {};
  const running = runningVersion();
  const sig = JSON.stringify([upd, run && run.phase, running]);
  paint(box, sig, () => {
    const pill = (t, c) => h('span', { class: 'pill ' + (c || ''), text: t });
    const latest = i.latestVersion ? (i.releaseTag || 'v' + i.latestVersion) : null;
    const url = safeUrl(i.releaseUrl);
    let state = pill('Checking'), hint = null, notes = null;
    if (updRunning()) state = pill('Updating', 'warn');
    else if (upd.state === 'error') {
      state = pill('Check failed', 'bad');
      hint = upd.status === 404
        ? 'GitHub did not find the repository or its releases. openHop asks GitHub without signing in, so a private '
          + 'repository looks the same as a missing one: update checks and one-click updates need a public repository.'
        : 'Could not check for updates: ' + upd.error + '.';
    }
    else if (upd.state === 'ok' && !i.repository) {
      state = pill('Unavailable');
      hint = 'openHop does not know which GitHub repository this plugin comes from, because it was installed from an uploaded wheel '
        + 'rather than the openHop catalogue. Add "repository": "' + REPO + '" to the plugin\'s state.json once '
        + '(see "Updating" in docs/SETUP.md); later installs keep it.';
    } else if (updAvailable()) {
      state = pill('Update available', 'warn');
      if (i.releaseNotes) notes = h('div', {}, h('p', { class: 'small muted', style: 'margin:8px 0 4px', text: 'What\'s new in ' + latest }), h('pre', { class: 'notes', text: i.releaseNotes }));
    } else if (upd.state === 'ok' && i.latestVersion) {
      state = pill('Up to date', 'ok');
      if (running && cmpVer(running, i.latestVersion) > 0) hint = 'Running ' + running + ', newer than the latest release ' + latest + '.';
    } else if (upd.state === 'ok') { state = pill('No releases'); hint = 'The repository has no published release yet.'; }
    const kv = (pairs) => h('dl', { class: 'kv' }, pairs.map(([k, v]) => [h('dt', { text: k }), h('dd', { text: v == null || v === '' ? '-' : String(v) })]));
    box.replaceChildren(h('div', { class: 'card' }, h('h3', { text: 'Updates' }),
      h('p', {}, state),
      kv([['Running', running ? 'v' + running : null], ['Latest release', latest], ['Checked', upd.at ? clock(upd.at) : null]]),
      hint ? h('p', { class: 'hint', style: 'margin-top:8px', text: hint }) : null,
      notes,
      h('div', { class: 'row', style: 'margin-top:8px' },
        updAvailable() && !updRunning() ? h('button', { type: 'button', class: 'primary', text: 'Update to v' + i.latestVersion, onclick: startUpdate }) : null,
        url ? h('a', { href: url, target: '_blank', rel: 'noopener', text: 'Release notes' }) : null,
        h('span', { class: 'grow' }),
        h('button', { type: 'button', text: 'Check again', disabled: upd.state === 'checking' || updRunning(), onclick: () => checkUpdates(true) }))));
  });
}

// ---------------------------------------------------------------- tab: status
function tabStatus() {
  const body = h('div');
  const clear = h('button', { type: 'button', text: 'Clear cooldowns', onclick: async () => {
    clear.disabled = true;
    const r = await runAction('clear_cooldowns', {});
    clear.disabled = false;
    if (r) toast(r.result ? 'Cleared ' + (r.result.cleared ?? 0) + ' cooldowns' : 'Queued; no result yet', !r.result);
  } });
  const advert = (flood) => async (ev) => {
    if (flood && !confirm('Send a flood advert? It is repeated across the whole mesh, so use it sparingly.')) return;
    const b = ev.currentTarget;
    b.disabled = true;
    const r = await runAction('advert', { flood });
    b.disabled = false;
    if (!r) return;
    const res = r.result;
    if (!res) toast('Queued; the bot did not answer yet. Is it running?', true);
    else if (!res.ok) toast('Advert failed: ' + (res.error || 'unknown error'), true);
    else if (!res.sent) toast('The radio did not send the advert.', true);
    else toast(res.flood ? 'Advert sent to the whole mesh' : 'Advert sent to neighbours');
  };
  const advertBox = h('div', { class: 'row advert' }, h('span', { class: 'muted', text: 'Send advert:' }),
    h('button', { type: 'button', text: 'Advert to neighbours', onclick: advert(false) }),
    h('button', { type: 'button', text: 'Advert to whole mesh', onclick: advert(true) }));
  const updBox = h('div'); // kept across refreshes; updCard repaints it only when its data changes
  const refresh = () => {
    updCard(updBox);
    const s = (rt && rt.status) || {};
    const empty = !rt || !Object.keys(rt).length;
    const c = (rt && rt.counters) || {};
    const kv = (pairs) => h('dl', { class: 'kv' }, pairs.map(([k, v]) => [h('dt', { text: k }), h('dd', { text: v == null || v === '' ? '-' : String(v) })]));
    const pill = (txt, good) => h('span', { class: 'pill ' + (good === true ? 'ok' : good === false ? 'warn' : ''), text: txt });
    const seen = (rt && rt.regions_seen) || {};
    const max = Math.max(1, ...Object.values(seen).map(Number));
    const names = { '*': '* unscoped', '?': '? unknown', direct: 'direct' };
    body.replaceChildren(
      h('div', { class: 'grid' },
        h('div', { class: 'card' }, h('h3', { text: 'Companion' }),
          h('p', {}, pill(empty ? 'no data' : s.companion || 'unknown', empty ? undefined : s.companion === 'connected')),
          kv([['Host', cfg.companion.host + ':' + cfg.companion.port], ['Uptime', rt && rt.started_at ? dur(Date.now() / 1000 - rt.started_at) : '-'], ['Last update', rt && rt.timestamp ? ago(rt.timestamp) : '-']])),
        h('div', { class: 'card' }, h('h3', { text: 'Node' }),
          kv([['Name', s.node_name], ['Pubkey', s.pubkey], ['Position', s.latitude != null && s.longitude != null ? s.latitude + ', ' + s.longitude : null]])),
        h('div', { class: 'card' }, h('h3', { text: 'CoreScope links' }),
          h('p', {}, pill(cfg.corescope.enabled ? 'on' : 'off', cfg.corescope.enabled ? true : undefined)),
          h('p', { class: 'hint', text: cfg.corescope.enabled ? (cfg.corescope.base_url || 'No base URL set.') + ' · links via ' + (cfg.corescope.shortener || 'v.gd') : '!path replies with hop hashes only.' }))),
      updBox,
      h('div', { class: 'card' }, h('h3', { text: 'Counters' }),
        h('div', { class: 'counters' }, Object.entries(c).map(([k, v]) => h('div', { class: 'counter' }, h('b', { text: String(v) }), h('span', { text: k }))))),
      h('div', { class: 'grid wide' },
        h('div', { class: 'card' }, h('h3', { text: 'Channel slots' }),
          (rt && rt.channels && rt.channels.length)
            ? h('div', { class: 'row' }, rt.channels.map((ch) => h('span', { class: 'pill ' + (ch.ok ? 'ok' : 'bad'), title: ch.ok ? 'Provisioned on the radio' : 'Not provisioned or key mismatch' }, '#' + ch.idx + ' ' + ch.name + (ch.ok ? ' ok' : ' not ok'))))
            : h('p', { class: 'muted', text: 'No channel slot data.' })),
        h('div', { class: 'card' }, h('h3', { text: 'Requests by region' }),
          Object.keys(seen).length
            ? Object.entries(seen).sort((a, b) => b[1] - a[1]).map(([k, v]) => h('div', { class: 'bar' }, h('span', { class: 'mono', text: names[k] || k }), h('i', { style: 'width:' + Math.round((v / max) * 100) + '%' }), h('span', { text: String(v) })))
            : h('p', { class: 'muted', text: 'No requests seen yet.' }))));
  };
  refresh();
  return { el: h('div', {}, h('div', { class: 'row status-actions', style: 'margin-bottom:8px' }, advertBox, h('span', { class: 'grow' }), clear), body), refresh };
}

// ---------------------------------------------------------------- tab: commands
const openCmds = new WeakSet();
function inheritChannels() {
  const s = cfg.scope.channels;
  return s.length ? s.join(', ') : 'all channels except protected (' + (cfg.scope.protected_channels.join(', ') || 'none') + ')';
}
const inheritRegions = () => (cfg.scope.regions.length ? cfg.scope.regions.join(', ') : 'any region');

function tabCommands() {
  const list = h('div');
  const fires = new Map();
  const builtins = h('div', { class: 'builtins' });
  const rtFor = (id) => (rt && rt.commands || []).find((x) => x.id === id) || {};

  function insertToken(name) {
    const tok = '{' + name + '}';
    const ta = lastReplyTA && document.body.contains(lastReplyTA) ? lastReplyTA : null;
    if (!ta) { toast('Click into a reply template first, then pick a token.', true); return; }
    const a = ta.selectionStart ?? ta.value.length, b = ta.selectionEnd ?? a;
    ta.setRangeText(tok, a, b, 'end');
    ta.dispatchEvent(new Event('input'));
    ta.focus();
  }
  function renderBuiltins() {
    const bi = (rt && rt.builtins) || [];
    builtins.replaceChildren(...(bi.length ? bi.map((b) => h('button', { type: 'button', class: 'sm', title: b.help || '', onclick: () => insertToken(b.name), text: '{' + b.name + '}' }))
      : [h('span', { class: 'muted', text: 'Builtin list appears once the bot is running. See docs/TEMPLATES.md.' })]));
    return bi;
  }

  function card(m, i) {
    const sum = h('summary');
    const fireEl = h('span', { class: 'muted small' });
    const upd = () => {
      d.classList.toggle('off', !m.enabled);
      const r = rtFor(m.id);
      sum.replaceChildren(h('span', { class: 'trig', text: m.trigger || '(no trigger)' }), h('span', { class: 'muted', text: m.id }),
        h('span', { class: 'pill ' + (m.enabled ? 'ok' : ''), text: m.enabled ? 'on' : 'off' }),
        h('span', { class: 'muted small grow', text: m.description || '' }), fireEl);
      fireEl.textContent = r.fires != null ? r.fires + ' fires, last ' + ago(r.last_fired) : '';
      fires.set(m, upd);
    };
    const d = h('details', { class: 'cmd' }, sum);
    if (openCmds.has(m)) d.open = true;
    d.addEventListener('toggle', () => { d.open ? openCmds.add(m) : openCmds.delete(m); });
    const [cnt, updCnt] = byteCounter(() => m.reply);
    const ta = textarea(m, 'reply', { rows: 3, aria: 'Reply template for ' + m.id, onchange: updCnt });
    ta.addEventListener('focus', () => { lastReplyTA = ta; });
    const dm = sel(m, 'dm', [['', 'Inherit (' + (cfg.scope.allow_dm ? 'yes' : 'no') + ')'], ['true', 'Yes, answer DMs'], ['false', 'No, ignore DMs']],
      { map: (v) => (v === '' ? null : v === 'true') });
    dm.value = m.dm == null ? '' : String(m.dm);
    const mv = (delta) => () => { const j = i + delta; if (j < 0 || j >= cfg.commands.length) return; [cfg.commands[i], cfg.commands[j]] = [cfg.commands[j], cfg.commands[i]]; openCmds.add(m); changed(); renderList(); };
    d.append(h('div', { class: 'body stack' },
      h('div', { class: 'grid' },
        field('Trigger', textIn(m, 'trigger', { mono: true, ph: '!name', onchange: upd })),
        field('ID', textIn(m, 'id', { mono: true, onchange: upd }), 'Unique; used for stats and announce/test.'),
        field('Cooldown (seconds)', numIn(m, 'cooldown_s', { min: 0 }), 'Per requester, per channel and region.'),
        h('div', {}, check('Enabled', m, 'enabled', { onchange: upd }))),
      field('Description', textIn(m, 'description', { onchange: upd }), 'Shown by {help}.'),
      h('label', { class: 'field' }, h('span', { class: 'lbl', text: 'Reply template' }), ta,
        h('small', {}, cnt, ' \u00b7 Counts the template text; the rendered reply is capped at ' + MAX_BYTES + ' bytes on air.')),
      h('div', { class: 'grid' },
        h('div', { class: 'field' }, h('span', { class: 'lbl', text: 'Channels' }), chips(m.channels, { suggest: channelNames, inherit: inheritChannels, aria: 'Add channel', ph: 'Channel name' })),
        h('div', { class: 'field' }, h('span', { class: 'lbl', text: 'Regions (* = unscoped)' }), chips(m.regions, { suggest: () => ['*', ...regionNames()], inherit: inheritRegions, aria: 'Add region', ph: '#region or *' })),
        field('Direct messages', dm)),
      h('div', { class: 'row' },
        h('button', { type: 'button', class: 'sm', text: 'Move up', disabled: i === 0, onclick: mv(-1) }),
        h('button', { type: 'button', class: 'sm', text: 'Move down', disabled: i === cfg.commands.length - 1, onclick: mv(1) }),
        h('button', { type: 'button', class: 'sm', text: 'Duplicate', onclick: () => {
          const c = clone(m); let n = 2;
          while (cfg.commands.some((x) => x.id === m.id + '-' + n)) n++;
          c.id = m.id + '-' + n; c.trigger = (m.trigger || '!cmd') + n;
          cfg.commands.splice(i + 1, 0, c); openCmds.add(c); changed(); renderList();
        } }),
        h('button', { type: 'button', class: 'sm danger', text: 'Delete', onclick: () => {
          if (!confirm('Delete command "' + m.id + '"?')) return;
          cfg.commands.splice(i, 1); changed(); renderList();
        } }))));
    upd();
    return d;
  }
  function renderList() {
    fires.clear();
    list.replaceChildren(...(cfg.commands.length ? cfg.commands.map(card) : [h('p', { class: 'muted', text: 'No commands defined. Add one to get started.' })]));
  }
  function refresh() { fires.forEach((f) => f()); renderBuiltins(); }

  const add = h('button', { type: 'button', class: 'primary', text: 'Add command', onclick: () => {
    let n = 1; while (cfg.commands.some((x) => x.id === 'cmd' + n)) n++;
    const m = { id: 'cmd' + n, enabled: true, trigger: '!cmd' + n, description: '', reply: '', channels: [], regions: [], dm: null, cooldown_s: 10 };
    cfg.commands.push(m); openCmds.add(m); changed(); renderList();
    list.lastElementChild.querySelector('input').focus();
  } });

  // test box
  const T = { text: '!ping', channel: channelNames()[0] || 'dm', sender: 'alice', region: '*', hops: '' };
  const tres = h('div', { 'aria-live': 'polite' });
  const chSel = h('select', { 'aria-label': 'Test channel' }, [...channelNames(), 'dm'].map((v) => h('option', { value: v, text: v === 'dm' ? 'Direct message' : v })));
  chSel.value = T.channel; chSel.addEventListener('change', () => { T.channel = chSel.value; });
  const rgSel = h('select', { 'aria-label': 'Test region' }, [...['*', ...regionNames(), '?', 'direct']].map((v) => h('option', { value: v, text: v })));
  rgSel.value = T.region; rgSel.addEventListener('change', () => { T.region = rgSel.value; });
  const tb = h('button', { type: 'button', text: 'Run test', onclick: async () => {
    tb.disabled = true; tres.replaceChildren(h('p', { class: 'muted', text: 'Waiting for the bot...' }));
    const args = { text: T.text, channel: T.channel, sender: T.sender, region: T.region };
    if (T.hops.trim()) args.hops = T.hops.trim();
    const r = await runAction('test', args);
    tb.disabled = false;
    if (!r) { tres.replaceChildren(); return; }
    const res = r.result;
    if (res && 'matched' in res) {
      tres.replaceChildren(h('div', { class: 'result' },
        h('p', {}, h('b', { text: res.matched ? 'Matched: ' + res.matched : 'No command matched' }), res.reason ? ' (' + res.reason + ')' : ''),
        res.reply ? h('p', { class: 'mono', text: res.reply }) : null,
        res.reply ? h('span', { class: 'counter-bytes', text: bytes(res.reply) + ' bytes' }) : null));
    } else tres.replaceChildren(resultNode(r));
  } });
  const tin = (k, lbl, o = {}) => { const el = h('input', { class: o.mono ? 'mono' : '', placeholder: o.ph, autocomplete: 'off' }); el.value = T[k]; el.addEventListener('input', () => { T[k] = el.value; }); return field(lbl, el, o.hint); };

  const el = h('div', {},
    h('div', { class: 'row', style: 'margin-bottom:8px' }, h('h2', { class: 'grow', text: 'Commands' }), add),
    h('details', { class: 'card' }, h('summary', { text: 'Template reference (click a token to insert at the cursor in the reply you last edited)' }), builtins,
      h('p', { class: 'hint', style: 'margin-top:8px', text: 'Other tokens: {sender} {hops} {snr} {gateway} {region_tag} {args}. Full list in docs/TEMPLATES.md.' })),
    list,
    h('div', { class: 'card' }, h('h2', { text: 'Test a message' }),
      h('p', { class: 'hint', text: 'Dry run: shows which command would match and what it would reply. Nothing is transmitted.' }),
      h('div', { class: 'grid' }, tin('text', 'Message text', { mono: true }), field('Channel', chSel), tin('sender', 'Sender'), field('Region', rgSel),
        tin('hops', 'Hop chain (optional hex)', { mono: true, ph: 'a1,b2c3', hint: 'Comma-separated path hashes.' })),
      h('div', { class: 'row', style: 'margin-top:8px' }, tb), tres));
  renderList(); renderBuiltins();
  return { el, refresh };
}

// ---------------------------------------------------------------- tab: scope
function tabScope() {
  const s = cfg.scope;
  const chRows = h('div');
  const renderCh = () => {
    chRows.replaceChildren(h('div', { class: 'tablewrap' }, h('table', {},
      h('thead', {}, h('tr', {}, ['Slot', 'Name', 'Secret (optional, 32 hex)', 'On radio', ''].map((t) => h('th', { text: t })))),
      h('tbody', {},
        h('tr', {}, h('td', { class: 'mono', text: '0' }), h('td', { text: 'Public (fixed)' }), h('td', { class: 'muted', text: 'well-known key' }), h('td', {}), h('td', {})),
        cfg.channels.map((ch, i) => {
          const live = (rt && rt.channels || []).find((x) => x.idx === ch.idx);
          return h('tr', {},
            h('td', { style: 'width:80px' }, numIn(ch, 'idx', { min: 1, max: 39, step: 1, aria: 'Slot' })),
            h('td', {}, textIn(ch, 'name', { mono: true, ph: '#name', aria: 'Channel name' })),
            h('td', {}, textIn(ch, 'secret', { mono: true, ph: 'only for non-hashtag channels', aria: 'Secret', nullable: true })),
            h('td', {}, live ? h('span', { class: 'pill ' + (live.ok ? 'ok' : 'bad'), text: live.ok ? 'ok' : 'not ok' }) : h('span', { class: 'muted', text: '-' })),
            h('td', {}, h('button', { type: 'button', class: 'sm danger', text: 'Remove', 'aria-label': 'Remove channel slot ' + ch.idx, onclick: () => { cfg.channels.splice(i, 1); changed(); renderCh(); } })));
        })))));
  };
  renderCh();
  const prov = h('button', { type: 'button', text: 'Provision channels on radio', onclick: async () => {
    prov.disabled = true; pres.replaceChildren(h('p', { class: 'muted', text: 'Waiting for the bot...' }));
    const r = await runAction('provision_channels', {});
    prov.disabled = false; pres.replaceChildren(r ? resultNode(r) : '');
  } });
  const pres = h('div', { 'aria-live': 'polite' });
  const regIn = chips(cfg.regions, { aria: 'Add region', ph: '#tx-dfw', onchange: () => {} });
  return { el: h('div', {},
    h('div', { class: 'card' }, h('h2', { text: 'What region scoping does' }),
      h('p', { text: 'MeshCore regions are transport-code scopes. A region-scoped flood packet carries a 16-bit code derived from the region name, so repeaters can filter traffic by region. The bot checks each request against every region listed below to learn which one it arrived under.' }),
      h('p', { class: 'hint', text: 'A request region is #name (a configured region), * (plain unscoped flood), ? (a region not in your list: fails closed whenever a region list is set) or direct (always allowed). With "reply in request region" on, answers go out under the region the question arrived in.' })),
    h('div', { class: 'card stack' }, h('h2', { text: 'Regions' }),
      h('div', { class: 'field' }, h('span', { class: 'lbl', text: 'Known regions (names start with #)' }), regIn)),
    h('div', { class: 'card stack' }, h('h2', { text: 'Global scope' }),
      h('p', { class: 'hint', text: 'A command fires only if every check passes. Per-command settings override these; empty means inherit.' }),
      h('div', { class: 'grid wide' },
        h('div', { class: 'field' }, h('span', { class: 'lbl', text: 'Answer on channels' }), chips(s.channels, { suggest: channelNames, aria: 'Add channel', ph: 'Channel name', inherit: 'all channels except protected' })),
        h('div', { class: 'field' }, h('span', { class: 'lbl', text: 'Answer in regions (* = unscoped)' }), chips(s.regions, { suggest: () => ['*', ...regionNames()], aria: 'Add region', ph: '#region or *', inherit: 'any region' })),
        h('div', { class: 'field' }, h('span', { class: 'lbl', text: 'Protected channels (never answered)' }), chips(s.protected_channels, { suggest: () => ['Public'], aria: 'Add protected channel', ph: 'Public' })),
        field('Max hops', numIn(s, 'max_hops', { nullable: true, min: 0, step: 1, ph: 'no limit' }), 'Ignore requests that travelled further than this. Empty = no limit.')),
      check('Answer direct messages by default', s, 'allow_dm', { onchange: () => {} }),
      check('Reply in the region the request arrived under', cfg.limits, 'reply_in_request_region')),
    h('div', { class: 'card stack' }, h('div', { class: 'row' }, h('h2', { class: 'grow', text: 'Channel slots' }),
      h('button', { type: 'button', text: 'Add channel', onclick: () => { let n = 1; const used = new Set(cfg.channels.map((c) => c.idx)); while (used.has(n) && n < 40) n++; cfg.channels.push({ idx: n, name: '#' }); changed(); renderCh(); } }), prov),
      h('p', { class: 'hint', text: 'Slot 1 to 39 on the companion radio. Hashtag channels (#name) derive their key from the name; anything else needs a 32-hex secret. Save, then provision to write them to the radio.' }),
      chRows, pres)) };
}

// ---------------------------------------------------------------- tab: announcements
function tabAnnounce() {
  const list = h('div');
  const render = () => {
    list.replaceChildren(...(cfg.announcements.length ? cfg.announcements.map((a, i) => {
      const [cnt, upd] = byteCounter(() => a.text);
      const res = h('div', { 'aria-live': 'polite' });
      const now = h('button', { type: 'button', class: 'sm', text: 'Announce now', onclick: async () => {
        now.disabled = true;
        const r = await runAction('announce_now', { id: a.id });
        now.disabled = false; if (r) res.replaceChildren(resultNode(r));
      } });
      return h('div', { class: 'card stack' },
        h('div', { class: 'grid' },
          h('div', {}, check('Enabled', a, 'enabled')),
          field('ID', textIn(a, 'id', { mono: true })),
          field('Channel', textIn(a, 'channel', { mono: true, list: 'dl-ch', ph: '#bot' })),
          field('Region', textIn(a, 'region', { mono: true, list: 'dl-rg', ph: 'default scope' }), 'Empty = companion default, * = unscoped.'),
          field('Schedule', textIn(a, 'schedule', { mono: true, ph: '0 8 * * *  or  every 3600' }), 'Cron (5 fields, node timezone) or every <seconds>.')),
        h('label', { class: 'field' }, h('span', { class: 'lbl', text: 'Text' }), textarea(a, 'text', { rows: 2, aria: 'Announcement text', onchange: upd }), h('small', {}, cnt, ' Template tokens such as {date} and {forecast 76131} work here.')),
        h('div', { class: 'row' }, now,
          h('button', { type: 'button', class: 'sm', text: 'Duplicate', onclick: () => { const c = clone(a); c.id += '-copy'; cfg.announcements.splice(i + 1, 0, c); changed(); render(); } }),
          h('button', { type: 'button', class: 'sm danger', text: 'Delete', onclick: () => { if (confirm('Delete announcement "' + a.id + '"?')) { cfg.announcements.splice(i, 1); changed(); render(); } } })), res);
    }) : [h('p', { class: 'muted', text: 'No announcements. Add one to post on a schedule.' })]));
  };
  render();
  return { el: h('div', {}, h('div', { class: 'row', style: 'margin-bottom:8px' }, h('h2', { class: 'grow', text: 'Announcements' }),
    h('button', { type: 'button', class: 'primary', text: 'Add announcement', onclick: () => { let n = 1; while (cfg.announcements.some((x) => x.id === 'ann' + n)) n++; cfg.announcements.push({ id: 'ann' + n, enabled: true, channel: channelNames()[0] || '#bot', region: '', schedule: 'every 3600', text: '' }); changed(); render(); } })),
    list, datalist('dl-ch', channelNames()), datalist('dl-rg', ['*', ...regionNames()])) };
}

// ---------------------------------------------------------------- tab: weather
function tabWeather() {
  const w = cfg.weather, node = cfg.node;
  return { el: h('div', {},
    h('div', { class: 'card stack' }, h('h2', { text: 'Weather (!wx)' }),
      h('p', { class: 'hint', text: 'Users ask for a place: !wx 76131, !wx Karlsruhe, !wx 32.9,-97.3. Places are looked up with OpenStreetMap Nominatim and forecasts come from Open-Meteo. Both are free, need no key, and work worldwide.' }),
      h('div', { class: 'grid' },
        field('Units', sel(w, 'units', [['auto', 'Auto (by the place asked about)'], ['imperial', 'Imperial (F, mph)'], ['metric', 'Metric (C, km/h)']])),
        field('Default country for bare postal codes', textIn(w, 'default_country', { mono: true, ph: 'us' }), 'A bare 76131 is Fort Worth in the US and Karlsruhe in Germany. This picks which one; users can still type "76131 DE". Empty = worldwide.'),
        field('Contact (optional)', textIn(w, 'contact', { mono: true, ph: 'you@example.com' }), 'Added to the User-Agent sent to Nominatim and Open-Meteo, as their usage policies ask.'))),
    h('div', { class: 'card stack' }, h('h2', { text: 'This node' }),
      h('p', { class: 'hint', text: 'Position is used for !path distances. Timezone drives {time}, {date} and announcement schedules.' }),
      h('div', { class: 'grid' },
        field('Node name', textIn(node, 'name')), field('Latitude', numIn(node, 'latitude', { nullable: true, min: -90, max: 90, ph: 'e.g. 32.9' })),
        field('Longitude', numIn(node, 'longitude', { nullable: true, min: -180, max: 180, ph: 'e.g. -97.3' })),
        field('Timezone (IANA)', textIn(node, 'timezone', { mono: true, ph: 'America/Chicago' })),
        field('Units for distances', sel(node, 'units', [['imperial', 'Imperial'], ['metric', 'Metric']]))))) };
}

// ---------------------------------------------------------------- tab: send
const sendState = { channel: '', region: '', text: '' };
function tabSend() {
  const S = sendState;
  if (!S.channel) S.channel = channelNames()[0] || '';
  const [cnt, upd] = byteCounter(() => S.text);
  const ta = h('textarea', { class: 'mono', rows: 3, 'aria-label': 'Message text' }); ta.value = S.text;
  ta.addEventListener('input', () => { S.text = ta.value; upd(); });
  const ch = h('input', { class: 'mono', list: 'dl-ch', autocomplete: 'off' }); ch.value = S.channel; ch.addEventListener('input', () => { S.channel = ch.value; });
  const rg = h('input', { class: 'mono', list: 'dl-rg', autocomplete: 'off', placeholder: 'default scope' }); rg.value = S.region; rg.addEventListener('input', () => { S.region = rg.value; });
  const res = h('div', { 'aria-live': 'polite' });
  const btn = h('button', { type: 'button', class: 'primary', text: 'Send message', onclick: async () => {
    if (!S.channel.trim() || !S.text.trim()) { toast('Pick a channel and enter text.', true); return; }
    if (bytes(S.text) > MAX_BYTES && !confirm('Text is over ' + MAX_BYTES + ' bytes and will be truncated. Send anyway?')) return;
    btn.disabled = true; res.replaceChildren(h('p', { class: 'muted', text: 'Queuing...' }));
    const r = await runAction('send', { channel: S.channel.trim(), text: S.text, region: S.region.trim() });
    btn.disabled = false; res.replaceChildren(r ? resultNode(r) : '');
  } });
  return { el: h('div', { class: 'card stack' }, h('h2', { text: 'Send a message' }),
    h('p', { class: 'hint', text: 'Transmits from this node, subject to the TX rate limits in Advanced.' }),
    h('div', { class: 'grid' }, field('Channel (name or slot number)', ch), field('Region', rg, 'Empty = default, * = unscoped, or #name.')),
    h('label', { class: 'field' }, h('span', { class: 'lbl', text: 'Text' }), ta, h('small', {}, cnt)),
    h('div', { class: 'row' }, btn), res, datalist('dl-ch', channelNames()), datalist('dl-rg', ['*', ...regionNames()])) };
}

// ---------------------------------------------------------------- tab: usage
const USAGE_PRESETS = [['24h', '24 h'], ['7d', '7 d'], ['30d', '30 d'], ['90d', '90 d'], ['1y', '1 y']];
const USAGE = { range: '24h', from: '', to: '', key: '', data: null, err: '', loading: false, formErr: '', render: null };
const SVGNS = 'http://www.w3.org/2000/svg';
function svg(tag, props, ...kids) {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(props || {})) if (v != null && v !== false) e.setAttribute(k, v);
  kids.forEach((c) => { if (c != null) e.append(c instanceof Node ? c : document.createTextNode(String(c))); });
  return e;
}
const usageArgs = () => (USAGE.range ? { range: USAGE.range } : { from: USAGE.from, to: USAGE.to });
async function usageLoad(force) {
  const args = usageArgs();
  const key = JSON.stringify(args);
  if (!force && key === USAGE.key && USAGE.data) return;
  if (USAGE.loading && key === USAGE.key) return;
  USAGE.key = key; USAGE.loading = true; USAGE.err = '';
  if (USAGE.render) USAGE.render();
  const r = await runAction('usage', args, { timeout: 20000 });
  if (USAGE.key !== key) return; // superseded by a newer request
  USAGE.loading = false;
  if (!r || !r.result) { USAGE.err = r ? "The bot didn't answer; is it running?" : 'The request was not sent.'; if (!r) USAGE.key = ''; }
  else if (!r.result.ok) { USAGE.err = r.result.error || 'The bot rejected this request.'; USAGE.key = ''; }
  else { USAGE.data = r.result; }
  if (USAGE.render) USAGE.render();
}
function niceStep(max) { // tick step giving ~4 whole-number ticks
  const raw = Math.max(1, max) / 4, p = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / p;
  return Math.max(1, (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p);
}
function usageChart(d, host) {
  const series = d.series || [];
  const totalBy = {};
  series.forEach((b) => Object.entries(b.counts || {}).forEach(([k, n]) => { totalBy[k] = (totalBy[k] || 0) + n; }));
  const trig = {}; (d.commands || []).forEach((c) => { trig[c.id] = c.trigger || c.id; });
  const ranked = Object.keys(totalBy).filter((k) => totalBy[k] > 0).sort((a, b) => totalBy[b] - totalBy[a]);
  const top = ranked.slice(0, 6);
  const hasOther = ranked.length > 6;
  const keys = hasOther ? [...top, 'other'] : top;
  const colorOf = (k) => 'var(' + (k === 'other' ? '--c-other' : '--c' + (top.indexOf(k) + 1)) + ')';
  const nameOf = (k) => (k === 'other' ? 'other (' + (ranked.length - 6) + (ranked.length === 7 ? ' command)' : ' commands)') : trig[k] || k);
  const bucketCounts = (b) => {
    const out = {}; let other = 0;
    Object.entries(b.counts || {}).forEach(([k, n]) => { if (!n) return; if (top.includes(k)) out[k] = n; else other += n; });
    if (other) out.other = other;
    return out;
  };
  const sums = series.map((b) => Object.values(b.counts || {}).reduce((a, n) => a + n, 0));
  const peak = Math.max(1, ...sums), tstep = niceStep(peak), ymax = Math.ceil(peak / tstep) * tstep;
  const tip = h('div', { class: 'chart-tip', hidden: true, role: 'presentation' });
  const legend = h('ul', { class: 'legend', 'aria-label': 'Commands' }, keys.map((k) => h('li', {}, h('i', { style: 'background:' + colorOf(k) }), h('span', { class: 'mono', text: nameOf(k) }), h('span', { class: 'muted', text: String(k === 'other' ? ranked.slice(6).reduce((a, x) => a + totalBy[x], 0) : totalBy[k]) }))));
  const box = h('div', { class: 'chart-box' });
  let lastW = 0;
  const draw = () => {
    const W = Math.max(280, Math.floor(box.clientWidth));
    if (W === lastW) return;
    lastW = W;
    const H = W < 480 ? 200 : 240, L = 34, R = 6, T = 8, B = 24;
    const pw = W - L - R, ph = H - T - B, n = series.length || 1;
    const step = pw / n, bw = Math.max(1, Math.min(28, step * 0.72));
    const y = (v) => T + ph - (v / ymax) * ph;
    const el = svg('svg', { width: W, height: H, viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': 'Stacked bars of command requests per ' + d.bucket + ', ' + d.label + '. Exact numbers are in the tables below.' });
    for (let v = 0; v <= ymax; v += tstep) {
      const i = v;
      el.append(svg('line', { x1: L, x2: W - R, y1: y(v), y2: y(v), class: i ? 'grid-line' : 'axis-line' }),
        svg('text', { x: L - 6, y: y(v) + 4, 'text-anchor': 'end', class: 'ax' }, v));
    }
    const maxLabels = Math.max(2, Math.floor(pw / (d.bucket === 'hour' ? 44 : 64)));
    const every = Math.ceil(n / maxLabels);
    series.forEach((b, i) => {
      const cx = L + step * i + step / 2, x0 = cx - bw / 2;
      if (i % every === 0) el.append(svg('text', { x: cx, y: H - 6, 'text-anchor': 'middle', class: 'ax' }, b.label));
      let acc = 0;
      const cnt = bucketCounts(b);
      keys.forEach((k) => {
        const v = cnt[k]; if (!v) return;
        const y1 = y(acc + v), y0 = y(acc); acc += v;
        el.append(svg('rect', { x: x0, y: y1, width: bw, height: Math.max(0.5, y0 - y1), fill: colorOf(k), class: 'seg' }));
      });
      const desc = b.label + ': ' + (sums[i] ? Object.entries(cnt).map(([k, v]) => nameOf(k) + ' ' + v).join(', ') + ' (total ' + sums[i] + ')' : 'no requests');
      const hit = svg('rect', { x: L + step * i, y: T, width: step, height: ph, class: 'hit' }, svg('title', {}, desc));
      const show = () => {
        tip.replaceChildren(h('b', { text: b.label }), ...(sums[i] ? Object.entries(cnt).map(([k, v]) => h('div', {}, h('i', { style: 'background:' + colorOf(k) }), h('span', { class: 'mono', text: nameOf(k) }), h('span', { text: String(v) }))) : [h('div', { class: 'muted', text: 'No requests' })]));
        tip.hidden = false;
        const tw = tip.offsetWidth;
        tip.style.left = Math.max(0, Math.min(W - tw, cx - tw / 2)) + 'px';
        tip.style.top = Math.max(0, y(sums[i]) - tip.offsetHeight - 6) + 'px';
      };
      hit.addEventListener('mouseenter', show); hit.addEventListener('mouseleave', () => { tip.hidden = true; });
      hit.addEventListener('touchstart', show, { passive: true });
      el.append(hit);
    });
    box.replaceChildren(el, tip);
  };
  if (window.ResizeObserver) new ResizeObserver(draw).observe(box);
  requestAnimationFrame(draw);
  return h('div', { class: 'card' }, h('h3', { text: 'Requests per ' + d.bucket }), legend, box);
}
function usageTable(title, cols, rows, empty) {
  return h('div', { class: 'card' }, h('h3', { text: title }),
    rows.length ? h('div', { class: 'tablewrap' }, h('table', {}, h('thead', {}, h('tr', {}, cols.map(([t, num]) => h('th', { class: num ? 'num' : '', text: t })))),
      h('tbody', {}, rows.map((r) => h('tr', {}, r.map((v, i) => h('td', { class: (cols[i][1] ? 'num ' : '') + (cols[i][2] || ''), text: String(v) })))))))
      : h('p', { class: 'muted', text: empty || 'Nothing to show.' }));
}
function tabUsage() {
  const out = h('div', { class: 'usage-out' });
  const presets = h('div', { class: 'row seg-btns', role: 'group', 'aria-label': 'Time window' });
  const fromIn = h('input', { type: 'date', 'aria-label': 'From date', value: USAGE.from });
  const toIn = h('input', { type: 'date', 'aria-label': 'To date', value: USAGE.to });
  const formErr = h('p', { class: 'form-err', role: 'alert', hidden: true });
  const apply = h('button', { type: 'button', text: 'Apply', onclick: () => {
    if (!fromIn.value || !toIn.value) { formErr.textContent = 'Pick both a From and a To date.'; formErr.hidden = false; return; }
    if (fromIn.value > toIn.value) { formErr.textContent = 'From must be on or before To.'; formErr.hidden = false; return; }
    formErr.hidden = true;
    USAGE.range = ''; USAGE.from = fromIn.value; USAGE.to = toIn.value;
    drawPresets(); usageLoad();
  } });
  const drawPresets = () => presets.replaceChildren(...USAGE_PRESETS.map(([id, label]) => h('button', { type: 'button', text: label, class: USAGE.range === id ? 'sel' : '', 'aria-pressed': String(USAGE.range === id),
    onclick: () => { USAGE.range = id; formErr.hidden = true; drawPresets(); usageLoad(); } })));
  const render = () => {
    const d = USAGE.data, kids = [];
    if (USAGE.err) kids.push(h('div', { class: 'card usage-msg bad', role: 'alert' }, h('b', { text: 'Could not load usage. ' }), USAGE.err, ' ', h('button', { type: 'button', class: 'sm', text: 'Retry', onclick: () => usageLoad(true) })));
    if (d) {
      const t = d.totals || {};
      const stat = (v, l) => h('div', { class: 'counter' }, h('b', { text: String(v ?? 0) }), h('span', { text: l }));
      const body = [h('div', { class: 'card' }, h('div', { class: 'row' }, h('h2', { class: 'grow', text: d.label || 'Usage' }), h('span', { class: 'muted small', text: d.tz || '' })),
        h('div', { class: 'counters' }, stat(t.requests, 'requests'), stat(t.answered, 'answered'), stat(t.skipped, 'skipped')))];
      if (!t.requests) body.push(h('div', { class: 'card usage-msg' }, h('b', { text: 'No commands in this window.' }), ' Try a longer range.'));
      else {
        body.push(usageChart(d, out));
        body.push(h('div', { class: 'grid wide' },
          usageTable('Commands', [['Command'], ['Answered', true], ['Skipped', true]], (d.commands || []).map((c) => [c.trigger || c.id, c.answered, c.skipped]).map((r) => r), 'No commands.'),
          usageTable('Skipped by reason', [['Reason'], ['Count', true]], (d.skipped || []).map((s) => [s.reason, s.count]), 'Nothing was skipped.'),
          usageTable('Top requesters', [['Name'], ['Requests', true]], (d.requesters || []).map((x) => [x.name, x.requests]), 'No requesters.'),
          usageTable('Channels', [['Channel'], ['Requests', true]], (d.channels || []).map((x) => [x.name, x.requests]), 'No channels.'),
          usageTable('Regions', [['Region'], ['Requests', true]], (d.regions || []).map((x) => [x.name, x.requests]), 'No regions.')));
      }
      kids.push(h('div', { class: 'usage-data' + (USAGE.loading ? ' dim' : ''), 'aria-busy': String(USAGE.loading) }, body));
    }
    if (USAGE.loading) kids.unshift(h('p', { class: 'muted usage-loading', role: 'status', text: d ? 'Updating...' : 'Loading usage...' }));
    out.replaceChildren(...kids);
  };
  USAGE.render = render;
  drawPresets(); render(); usageLoad();
  return { el: h('div', {},
    h('div', { class: 'card usage-bar' }, h('div', { class: 'row' }, presets,
      h('span', { class: 'grow' }),
      h('label', { class: 'row date' }, h('span', { class: 'muted small', text: 'From' }), fromIn),
      h('label', { class: 'row date' }, h('span', { class: 'muted small', text: 'To' }), toIn), apply), formErr),
    out) };
}

// ---------------------------------------------------------------- tab: activity / nodes
const actFilter = new Set();
function tabActivity() {
  const KINDS = ['rx', 'reply', 'skip', 'announce', 'alert', 'tx', 'error'];
  const log = h('div', { class: 'log', role: 'log', 'aria-label': 'Activity log' });
  const filters = h('div', { class: 'row' }, h('span', { class: 'muted', text: 'Show:' }), KINDS.map((k) => {
    const cb = h('input', { type: 'checkbox', checked: !actFilter.size || actFilter.has(k) });
    cb.addEventListener('change', () => {
      const cur = new Set(actFilter.size ? actFilter : KINDS);
      cb.checked ? cur.add(k) : cur.delete(k);
      actFilter.clear(); if (cur.size !== KINDS.length) cur.forEach((x) => actFilter.add(x));
      refresh();
    });
    return h('label', { class: 'chk k-' + k }, cb, h('span', { text: k }));
  }));
  const refresh = () => {
    const ev = ((rt && rt.activity) || []).filter((e) => !actFilter.size || actFilter.has(e.kind)).slice().reverse();
    log.replaceChildren(...(ev.length ? ev.map((e) => h('div', { class: 'ev' },
      h('span', { class: 'muted', text: clock(e.ts) }), h('span', { class: 'kind k-' + e.kind, text: e.kind }),
      h('span', { class: 'msg' },
        [e.channel, e.region && e.region !== '*' ? e.region : null, e.sender ? e.sender + ':' : null].filter(Boolean).map((x, i, a) => h('span', { class: 'muted', text: x + (i < a.length - 1 && !x.endsWith(':') ? ' ' : ' ') })),
        e.text || '', e.detail ? h('span', { class: 'muted', text: '  [' + e.detail + ']' }) : null)))
      : [h('p', { class: 'muted', text: rtState === 'ok' ? 'No matching activity yet.' : 'No activity data.' })]));
  };
  refresh();
  return { el: h('div', {}, h('div', { class: 'row', style: 'margin-bottom:8px' }, h('h2', { class: 'grow', text: 'Activity' }), filters), h('div', { class: 'card' }, log)), refresh };
}
let nodeQ = '';
function tabNodes() {
  const body = h('div');
  const q = h('input', { type: 'search', placeholder: 'Filter by name, key or type', 'aria-label': 'Filter nodes', autocomplete: 'off' });
  q.value = nodeQ; q.addEventListener('input', () => { nodeQ = q.value; refresh(); });
  const refresh = () => {
    const r = (rt && rt.roster) || {};
    const all = r.recent || [];
    const f = nodeQ.trim().toLowerCase();
    const rows = all.filter((n) => !f || [n.name, n.pubkey, n.type].some((x) => String(x || '').toLowerCase().includes(f)));
    body.replaceChildren(
      h('div', { class: 'row', style: 'margin-bottom:8px' }, h('span', { class: 'pill', text: (r.nodes ?? 0) + ' nodes known' }), h('span', { class: 'pill', text: (r.repeaters ?? 0) + ' repeaters' }), h('span', { class: 'muted small', text: 'Showing ' + rows.length + ' of the ' + all.length + ' most recent' })),
      rows.length ? h('div', { class: 'tablewrap card' }, h('table', {}, h('thead', {}, h('tr', {}, ['Name', 'Type', 'Key', 'Last heard', 'Hops', 'SNR', 'Position'].map((t, i) => h('th', { class: i >= 4 && i < 6 ? 'num' : '', text: t })))),
        h('tbody', {}, rows.map((n) => h('tr', {}, h('td', { text: n.name || '(unnamed)' }), h('td', { text: n.type || '' }), h('td', { class: 'mono', text: n.pubkey || '' }), h('td', { text: ago(n.last_heard) }),
          h('td', { class: 'num', text: n.hops ?? '' }), h('td', { class: 'num', text: n.snr ?? '' }), h('td', { class: 'mono', text: n.latitude != null && n.longitude != null ? n.latitude + ', ' + n.longitude : '' }))))))
        : h('p', { class: 'muted', text: all.length ? 'No nodes match the filter.' : 'No nodes heard yet.' }));
  };
  refresh();
  return { el: h('div', {}, h('div', { class: 'row', style: 'margin-bottom:8px' }, h('h2', { class: 'grow', text: 'Nodes' }), q), body), refresh };
}

// ---------------------------------------------------------------- tab: corescope
const SHORTENERS = [['v.gd', 'v.gd'], ['is.gd', 'is.gd'], ['tinyurl.com', 'TinyURL'], ['da.gd', 'da.gd'], ['none', 'None (full link)']];
function tabCoreScope() {
  const cs = cfg.corescope;
  return { el: h('div', {},
    h('div', { class: 'card stack' }, h('h2', { text: 'CoreScope map links' }),
      h('p', { text: '!path answers with the hop count and the hop hashes in travel order. With CoreScope on, it adds a short link to that route on CoreScope\'s map: "@[bob] [3h] 03b9,4216,d924 https://v.gd/AbC123". The map opens the packet by its hash, so it draws the route when CoreScope\'s observers heard the same packet.' }),
      h('p', { class: 'hint', text: 'The shortener gets the full map link (the packet hash and hop hashes); nothing else leaves the node. If shortening fails, !path uses the full link when it fits in one message, otherwise just the hashes. The {pathnames} template token still resolves hops to names and distance, using CoreScope\'s node list when this is on (that sends only the hop hashes to CoreScope).' }),
      check('Add CoreScope map links to !path', cs, 'enabled'),
      h('div', { class: 'grid' },
        field('CoreScope base URL', textIn(cs, 'base_url', { mono: true, ph: 'https://analyzer.example.net' }), 'The address you open CoreScope at, without /api.'),
        field('Link shortener', sel(cs, 'shortener', SHORTENERS), 'Keyless services. "None" sends the full link, which only fits short paths.')))) };
}

// ---------------------------------------------------------------- tab: advanced
function tabAdvanced() {
  const raw = h('textarea', { class: 'mono', rows: 18, spellcheck: 'false', 'aria-label': 'Raw configuration JSON' });
  const status = h('span', { class: 'hint', role: 'status' });
  const load = () => { raw.value = JSON.stringify(cfg, null, 2); raw.setAttribute('aria-invalid', 'false'); status.textContent = 'Mirrors the form. Apply to use your edits.'; };
  const parse = () => { const o = JSON.parse(raw.value); if (!isObj(o)) throw new Error('top level must be an object'); return o; };
  raw.addEventListener('input', () => {
    try { parse(); raw.setAttribute('aria-invalid', 'false'); status.textContent = 'Valid JSON.'; }
    catch (e) { raw.setAttribute('aria-invalid', 'true'); status.textContent = 'Invalid JSON: ' + e.message; }
  });
  load();
  return { el: h('div', {},
    h('div', { class: 'card stack' }, h('h2', { text: 'Companion & node' }),
      h('div', { class: 'grid' },
        field('Companion host', textIn(cfg.companion, 'host', { mono: true })), field('Companion port', numIn(cfg.companion, 'port', { min: 1, max: 65535, step: 1 })),
        field('path_hash_bytes', numIn(cfg.node, 'path_hash_bytes', { min: 1, max: 3, step: 1 }), 'Bytes per hop hash (1 to 3). Must match the mesh.'),
        field('Log level', sel(cfg, 'log_level', ['DEBUG', 'INFO', 'WARNING', 'ERROR'].map((x) => [x, x]))))),
    h('div', { class: 'card stack' }, h('h2', { text: 'Limits' }),
      h('div', { class: 'grid' },
        field('Gap between transmissions (s)', numIn(cfg.limits, 'tx_gap_s', { min: 0 })), field('Max transmissions per minute', numIn(cfg.limits, 'tx_max_per_min', { min: 0, step: 1 })),
        field('Per-user cooldown floor (s)', numIn(cfg.limits, 'user_cooldown_s', { min: 0 }), 'Applies across all commands.'))),
    h('div', { class: 'card stack' }, h('h2', { text: 'Raw JSON' }),
      h('p', { class: 'hint', text: 'The whole configuration, including keys this console does not know about. Applying replaces the form state; nothing is saved until you press Save.' }),
      raw, h('div', { class: 'row' },
        h('button', { type: 'button', text: 'Apply to form', onclick: () => {
          try { cfg = deepMerge(DEFAULTS, parse()); chrome(); toast('Applied. Review the other tabs, then Save.'); load(); }
          catch (e) { toast('Not applied: ' + e.message, true); }
        } }),
        h('button', { type: 'button', text: 'Reset from form', onclick: load }), status))) };
}

// ---------------------------------------------------------------- shell
const BUILDERS = { status: tabStatus, commands: tabCommands, scope: tabScope, announce: tabAnnounce, weather: tabWeather, send: tabSend, activity: tabActivity, usage: tabUsage, nodes: tabNodes, corescope: tabCoreScope, advanced: tabAdvanced };

function renderTabs() {
  const nav = $('#tabs');
  nav.replaceChildren(...TABS.map(([id, label]) => h('button', { type: 'button', role: 'tab', id: 'tab-' + id, 'aria-selected': String(id === curTab), 'aria-controls': 'view', tabindex: id === curTab ? '0' : '-1',
    text: label, onclick: () => go(id) })));
}
nav_keys();
function nav_keys() {
  document.addEventListener('keydown', (ev) => {
    if (!ev.target.closest || !ev.target.closest('#tabs') || !['ArrowLeft', 'ArrowRight'].includes(ev.key)) return;
    const i = TABS.findIndex(([id]) => id === curTab);
    const j = (i + (ev.key === 'ArrowRight' ? 1 : TABS.length - 1)) % TABS.length;
    go(TABS[j][0]); $('#tab-' + TABS[j][0]).focus();
  });
}
function go(id) {
  curTab = BUILDERS[id] ? id : 'status';
  if (location.hash !== '#' + curTab) history.replaceState(null, '', '#' + curTab);
  renderTabs(); draw();
}
function draw() {
  if (!cfg) return;
  view = BUILDERS[curTab]();
  $('#view').setAttribute('aria-labelledby', 'tab-' + curTab);
  $('#view').setAttribute('role', 'tabpanel');
  $('#view').replaceChildren(view.el);
  chrome();
}

async function save() {
  const errs = validate();
  if (errs.length) { toast('Fix ' + errs.length + ' problem(s) before saving.', true); return; }
  const b = $('#btn-save'); b.disabled = true;
  try {
    const out = clone(cfg); delete out._cmd;
    await postConfig(out);
    savedStr = JSON.stringify(cfg);
    toast('Saved. The bot reloads the config within 2 seconds.');
  } catch (e) { toast('Save failed: ' + e.message, true); }
  chrome();
}
function revert() {
  if (!confirm('Discard all unsaved edits?')) return;
  cfg = JSON.parse(savedStr); draw();
}

async function boot() {
  try {
    cfg = await fetchConfig();
    savedStr = JSON.stringify(cfg);
    await fetchRuntime();
    draw();
    checkUpdates(false);
  } catch (e) {
    if (e.auth) return;
    $('#view').replaceChildren(h('div', { class: 'card' }, h('h2', { text: 'Could not load configuration' }), h('p', { text: e.message }),
      h('button', { type: 'button', text: 'Retry', onclick: boot })));
    rtState = 'error'; rtError = e.message; chrome();
  }
}

let polling = false;
async function poll() {
  if (polling || authRequired) return;
  polling = true;
  try {
    await fetchRuntime();
    chrome();
    if (view && view.refresh) view.refresh();
    if (upd.at && upd.state !== 'checking' && Date.now() / 1000 - upd.at > RECHECK_S) checkUpdates(false);
  } finally { polling = false; }
}

window.addEventListener('beforeunload', (ev) => { if (isDirty()) { ev.preventDefault(); ev.returnValue = ''; } });
$('#btn-save').addEventListener('click', save);
$('#btn-revert').addEventListener('click', revert);
window.addEventListener('hashchange', () => { const t = location.hash.slice(1); if (t !== curTab && BUILDERS[t]) go(t); });
{ const t = location.hash.slice(1); if (BUILDERS[t]) curTab = t; }
renderTabs();
boot();
setInterval(poll, POLL_MS);
