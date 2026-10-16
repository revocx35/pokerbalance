'use strict';
/* Poker Balance - single-page UI. No framework, no inline code (strict CSP). */

const S = { user: null, csrf: '', version: '', defaultCurrency: '₺', needsSetup: false };
let renderSeq = 0;
let pollTimer = null;

// ------------------------------------------------------------------ DOM helpers

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else if (k === 'value') el.value = v;
      else if (k === 'checked') el.checked = !!v;
      else if (k === 'disabled') el.disabled = !!v;
      else el.setAttribute(k, v === true ? '' : v);
    }
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : String(kid));
  }
  return el;
}

const $ = (sel, root = document) => root.querySelector(sel);

function initials(name) {
  const parts = String(name || '?').trim().split(/\s+/);
  return ((parts[0] || '?')[0] + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase();
}

const nf2 = new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const nf0 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });

function money(cents, cur) {
  const neg = cents < 0;
  const a = Math.abs(cents);
  const s = a % 100 === 0 ? nf0.format(a / 100) : nf2.format(a / 100);
  return (neg ? '−' : '') + (cur || '') + s;
}

function signed(cents, cur) {
  return (cents > 0 ? '+' : '') + money(cents, cur);
}

function parseMoney(str) {
  let s = String(str || '').trim().replace(/\s/g, '');
  if (!s) return NaN;
  // Accept 1.234,56 / 1,234.56 / 12,5 / 12.5
  const lastComma = s.lastIndexOf(','), lastDot = s.lastIndexOf('.');
  if (lastComma > lastDot) s = s.replace(/\./g, '').replace(',', '.');
  else s = s.replace(/,/g, '');
  if (!/^\d+(\.\d{1,2})?$/.test(s)) return NaN;
  return Math.round(parseFloat(s) * 100);
}

function pctStr(bp) {
  return (bp / 100).toFixed(2).replace(/\.?0+$/, '');
}

function parsePct(str) {
  const s = String(str || '').trim().replace(',', '.');
  if (!/^\d+(\.\d{1,2})?$/.test(s)) return NaN;
  return Math.round(parseFloat(s) * 100);
}

function ordinal(n) {
  const s = ['th', 'st', 'nd', 'rd'], v = n % 100;
  return n + (s[(v - 20) % 10] || s[v] || s[0]);
}

function dateStr(ts) {
  return new Date(ts * 1000).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
}

/** Same largest-remainder split as the server (app/payouts.py allocate). */
function allocate(total, weights, step, preferLow) {
  const n = weights.length;
  const wsum = weights.reduce((a, b) => a + b, 0);
  if (!n || wsum <= 0) return weights.map(() => 0);
  const num = weights.map(w => total * w); // ideal_i = num_i / wsum
  const shares = num.map(x => Math.floor(x / (wsum * step)) * step);
  const rem = num.map((x, i) => x - shares[i] * wsum);
  let left = total - shares.reduce((a, b) => a + b, 0);
  const order = [...Array(n).keys()].filter(i => weights[i] > 0).sort((a, b) =>
    (rem[b] - rem[a]) || (preferLow ? num[a] - num[b] : 0) || (a - b));
  for (const i of order.slice(0, Math.floor(left / step))) shares[i] += step;
  left = total - shares.reduce((a, b) => a + b, 0);
  if (left) {
    let big = 0;
    for (let i = 1; i < n; i++) if (num[i] > num[big]) big = i;
    shares[big] += left;
  }
  return shares;
}

// ------------------------------------------------------------------ API

class ApiError extends Error {
  constructor(status, msg) { super(msg); this.status = status; }
}

async function api(method, path, body) {
  const opts = { method, headers: {}, credentials: 'same-origin', cache: 'no-store' };
  if (method !== 'GET') {
    opts.headers['Content-Type'] = 'application/json';
    opts.headers['X-CSRF-Token'] = S.csrf || '';
    opts.body = JSON.stringify(body || {});
  }
  let r;
  try {
    r = await fetch('/api' + path, opts);
  } catch (e) {
    throw new ApiError(0, 'Can\'t reach the server. Are you offline?');
  }
  let data = null;
  try { data = await r.json(); } catch (e) { /* empty body */ }
  if (!r.ok) {
    const msg = (data && data.detail) || ('Error ' + r.status);
    if (r.status === 401 && path !== '/login' && path !== '/setup') {
      S.user = null;
      setTimeout(route, 0);
    } else if (r.status === 403 && /temporary password/.test(msg)) {
      setTimeout(boot, 0);
    }
    throw new ApiError(r.status, msg);
  }
  return data;
}

// ------------------------------------------------------------------ feedback

let toastTimer = null;
function toast(msg, isError) {
  const old = $('.toast');
  if (old) old.remove();
  const t = h('div', { class: 'toast' + (isError ? ' error' : ''), role: 'status' }, msg);
  document.body.append(t);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.remove(), isError ? 4200 : 2400);
}

/** Wrap an async action: disables the button while running and shows errors as toasts. */
function act(fn) {
  return async (ev) => {
    const btn = ev && ev.currentTarget && ev.currentTarget.tagName === 'BUTTON' ? ev.currentTarget : null;
    if (btn) btn.disabled = true;
    try { await fn(ev); } catch (e) { toast(e.message || String(e), true); } finally { if (btn) btn.disabled = false; }
  };
}

function modal(title, body, buttons) {
  return new Promise(resolve => {
    const back = h('div', { class: 'modal-back' });
    const close = (v) => { back.remove(); document.removeEventListener('keydown', onKey); resolve(v); };
    const onKey = (e) => { if (e.key === 'Escape') close(null); };
    document.addEventListener('keydown', onKey);
    back.addEventListener('click', (e) => { if (e.target === back) close(null); });
    const row = h('div', { class: 'btnrow' });
    for (const b of buttons || [{ label: 'OK', value: true, cls: 'primary' }]) {
      row.append(h('button', {
        class: 'btn ' + (b.cls || ''), type: 'button',
        onclick: async (e) => {
          if (b.onClick) {
            const btn = e.currentTarget;
            btn.disabled = true;
            try {
              const v = await b.onClick();
              if (v !== undefined) close(v);
            } catch (err) { toast(err.message || String(err), true); } finally { btn.disabled = false; }
          } else close(b.value);
        },
      }, b.label));
    }
    back.append(h('div', { class: 'modal', role: 'dialog', 'aria-modal': 'true' }, h('h3', null, title), body, row));
    document.body.append(back);
    const first = back.querySelector('input, select');
    if (first) setTimeout(() => first.focus(), 50);
  });
}

function confirmBox(title, text, okLabel, danger) {
  return modal(title, h('p', { class: 'muted' }, text), [
    { label: 'Cancel', value: false },
    { label: okLabel || 'OK', value: true, cls: danger ? 'danger solid' : 'primary' },
  ]);
}

function showTempPassword(name, pw) {
  return modal('Temporary password', h('div', null,
    h('p', null, `Give this password to ${name}. It is shown only once; they must change it at their first sign-in.`),
    h('p', { class: 'secret' }, pw)), [
    { label: 'Copy', cls: '', onClick: async () => { await copyText(pw); toast('Copied'); } },
    { label: 'Done', value: true, cls: 'primary' },
  ]);
}

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  const ta = h('textarea', { class: 'hidden' }, text);
  document.body.append(ta);
  ta.select();
  document.execCommand('copy');
  ta.remove();
}

// ------------------------------------------------------------------ shell

function shell() {
  document.body.textContent = '';
  const back = h('button', { class: 'iconbtn back', 'aria-label': 'Back', type: 'button', onclick: () => history.back() }, '‹');
  document.body.append(
    h('div', { id: 'offline', class: 'offline hidden' }, 'You are offline - showing nothing new until the connection is back.'),
    h('header', { class: 'topbar' }, back, h('h1', { id: 'title' }, 'Poker Balance'), h('span', { id: 'topright' })),
    h('main', { id: 'main' }),
    h('nav', { class: 'tabbar', id: 'tabbar' }),
  );
  window.addEventListener('online', () => $('#offline').classList.add('hidden'));
  window.addEventListener('offline', () => $('#offline').classList.remove('hidden'));
  if (!navigator.onLine) $('#offline').classList.remove('hidden');
}

function frame(title, opts = {}) {
  document.title = title ? `${title} · Poker Balance` : 'Poker Balance';
  $('#title').textContent = title || 'Poker Balance';
  $('.topbar .back').classList.toggle('show', !!opts.back);
  const right = $('#topright');
  right.textContent = '';
  if (opts.right) right.append(opts.right);
  const tabs = $('#tabbar');
  tabs.textContent = '';
  tabs.classList.toggle('hidden', !S.user || !!opts.noTabs);
  if (S.user && !opts.noTabs) {
    const cur = location.hash || '#/';
    const tab = (href, ico, label, match) => h('a', { href, class: match ? 'active' : '' }, h('span', { class: 'ico' }, ico), label);
    tabs.append(tab('#/', '♣', 'Groups', !/^#\/(users|account)/.test(cur)));
    if (S.user.is_admin) tabs.append(tab('#/users', '☺', 'Players', cur.startsWith('#/users')));
    tabs.append(tab('#/account', '⚙', 'Account', cur.startsWith('#/account')));
  }
  const main = $('#main');
  main.textContent = '';
  return main;
}

function loading(main) {
  main.append(h('div', { class: 'spinner', 'aria-label': 'Loading' }));
}

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

// ------------------------------------------------------------------ routing

const routes = [
  [/^#?\/?$/, viewHome],
  [/^#\/group\/(\d+)$/, viewGroup],
  [/^#\/group\/(\d+)\/new$/, viewNewGame],
  [/^#\/game\/(\d+)$/, viewGame],
  [/^#\/users$/, viewUsers],
  [/^#\/account$/, viewAccount],
];

async function route() {
  stopPolling();
  const seq = ++renderSeq;
  if (!S.user) return S.needsSetup ? viewSetup() : viewLogin();
  if (S.user.must_change_pw) return viewForcePassword();
  const hash = location.hash || '#/';
  for (const [re, fn] of routes) {
    const m = hash.match(re);
    if (m) {
      try { await fn(seq, ...m.slice(1).map(Number)); } catch (e) {
        if (seq !== renderSeq) return;
        const main = $('#main');
        main.textContent = '';
        main.append(h('div', { class: 'card empty' }, e.message || String(e)));
      }
      return;
    }
  }
  location.hash = '#/';
}

async function boot() {
  try {
    const st = await api('GET', '/state');
    S.user = st.user;
    S.csrf = st.csrf || '';
    S.version = st.version;
    S.defaultCurrency = st.default_currency || '₺';
    S.needsSetup = st.needs_setup;
  } catch (e) {
    shell();
    frame('Poker Balance', { noTabs: true }).append(h('div', { class: 'card empty' }, e.message));
    return;
  }
  if (!$('#main')) shell();
  route();
}

// ------------------------------------------------------------------ auth views

function brand(sub) {
  return h('div', { class: 'brand' }, h('img', { src: '/static/icons/icon-192.png', alt: '' }),
    h('h1', null, 'Poker Balance'), h('div', { class: 'muted' }, sub));
}

function field(label, input, hint) {
  return h('label', { class: 'field' }, h('span', null, label), input, hint ? h('div', { class: 'hint' }, hint) : null);
}

function viewSetup() {
  const main = frame('', { noTabs: true });
  const token = h('input', { class: 'input', autocomplete: 'off', required: true, autocapitalize: 'off', spellcheck: 'false' });
  const user = h('input', { class: 'input', autocomplete: 'username', required: true, autocapitalize: 'off', value: 'admin' });
  const name = h('input', { class: 'input', autocomplete: 'name', required: true });
  const pw = h('input', { class: 'input', type: 'password', autocomplete: 'new-password', required: true, minlength: '10' });
  const pw2 = h('input', { class: 'input', type: 'password', autocomplete: 'new-password', required: true });
  const errEl = h('div', { class: 'err' });
  const form = h('form', {
    class: 'card',
    onsubmit: act(async (e) => {
      e.preventDefault();
      errEl.textContent = '';
      if (pw.value !== pw2.value) { errEl.textContent = 'The passwords don\'t match.'; return; }
      try {
        await api('POST', '/setup', { token: token.value, username: user.value, display_name: name.value, password: pw.value });
      } catch (err) { errEl.textContent = err.message; return; }
      await boot();
      location.hash = '#/';
    }),
  },
  h('h2', null, 'Create the admin account'),
  h('p', { class: 'muted small' }, 'First run. The setup token is printed in the container log (docker compose logs) and stored in the data folder as setup-token.'),
  field('Setup token', token), field('Username', user), field('Your name', name),
  field('Password', pw, 'At least 10 characters.'), field('Repeat password', pw2), errEl,
  h('button', { class: 'btn primary block', type: 'submit' }, 'Create admin'));
  main.append(h('div', { class: 'auth' }, brand('Track buy-ins, payouts and who owes whom.'), form));
}

function viewLogin() {
  const main = frame('', { noTabs: true });
  const user = h('input', { class: 'input', autocomplete: 'username', required: true, autocapitalize: 'off', spellcheck: 'false' });
  const pw = h('input', { class: 'input', type: 'password', autocomplete: 'current-password', required: true });
  const code = h('input', { class: 'input', inputmode: 'numeric', autocomplete: 'one-time-code', maxlength: '6', pattern: '[0-9]*' });
  const codeField = field('2FA code', code, 'From your authenticator app.');
  codeField.classList.add('hidden');
  const errEl = h('div', { class: 'err' });
  const form = h('form', {
    class: 'card',
    onsubmit: act(async (e) => {
      e.preventDefault();
      errEl.textContent = '';
      try {
        await api('POST', '/login', { username: user.value, password: pw.value, totp: code.value });
      } catch (err) {
        if (err.message === 'totp_required') {
          codeField.classList.remove('hidden');
          code.focus();
          return;
        }
        errEl.textContent = err.message;
        return;
      }
      await boot();
    }),
  }, field('Username', user), field('Password', pw), codeField, errEl,
  h('button', { class: 'btn primary block', type: 'submit' }, 'Sign in'));
  main.append(h('div', { class: 'auth' }, brand('Sign in to see your games.'), form));
  setTimeout(() => user.focus(), 50);
}

function passwordForm(onDone, intro) {
  const cur = h('input', { class: 'input', type: 'password', autocomplete: 'current-password', required: true });
  const pw = h('input', { class: 'input', type: 'password', autocomplete: 'new-password', required: true, minlength: '10' });
  const pw2 = h('input', { class: 'input', type: 'password', autocomplete: 'new-password', required: true });
  const errEl = h('div', { class: 'err' });
  return h('form', {
    class: 'card',
    onsubmit: act(async (e) => {
      e.preventDefault();
      errEl.textContent = '';
      if (pw.value !== pw2.value) { errEl.textContent = 'The new passwords don\'t match.'; return; }
      try { await api('POST', '/me/password', { current: cur.value, new: pw.value }); } catch (err) { errEl.textContent = err.message; return; }
      cur.value = pw.value = pw2.value = '';
      toast('Password changed');
      onDone && onDone();
    }),
  }, h('h2', null, 'Change password'), intro ? h('p', { class: 'muted small' }, intro) : null,
  field('Current password', cur), field('New password', pw, 'At least 10 characters.'), field('Repeat new password', pw2), errEl,
  h('button', { class: 'btn primary block', type: 'submit' }, 'Save password'));
}

function viewForcePassword() {
  const main = frame('Choose a password', { noTabs: true });
  main.append(h('div', { class: 'auth' }, brand(`Welcome, ${S.user.display_name}`),
    passwordForm(() => boot(), 'You signed in with a temporary password. Pick your own to continue.'),
    h('button', { class: 'btn block', type: 'button', onclick: act(logout) }, 'Sign out')));
}

async function logout() {
  await api('POST', '/logout', {});
  S.user = null;
  S.csrf = '';
  location.hash = '#/';
  route();
}

// ------------------------------------------------------------------ home (groups)

async function viewHome(seq) {
  const main = frame('Groups');
  loading(main);
  const groups = await api('GET', '/groups');
  if (seq !== renderSeq) return;
  main.textContent = '';
  if (!groups.length) {
    main.append(h('div', { class: 'card empty' }, S.user.is_admin
      ? 'No groups yet. Create one for the people you play with.'
      : 'You are not in any group yet. Ask the admin to add you.'));
  }
  for (const g of groups) {
    main.append(h('a', { class: 'tile', href: `#/group/${g.id}` },
      h('div', { class: 'avatar' }, initials(g.name)),
      h('div', { class: 'grow' },
        h('div', { class: 'title' }, g.name, g.running ? h('span', { class: 'badge live' }, 'LIVE') : null),
        h('div', { class: 'muted small' }, `${g.members} players · ${g.games} games`)),
      h('span', { class: 'chev' }, '›')));
  }
  if (S.user.is_admin) {
    main.append(h('button', { class: 'btn primary fab', type: 'button', onclick: groupDialog }, '+ New group'));
  }
}

async function groupDialog(existing) {
  const isEdit = existing && existing.id;
  const name = h('input', { class: 'input', required: true, maxlength: '40', value: isEdit ? existing.name : '' });
  const cur = h('input', { class: 'input', required: true, maxlength: '8', value: isEdit ? existing.currency : S.defaultCurrency });
  const res = await modal(isEdit ? 'Edit group' : 'New group', h('div', null,
    field('Name', name), field('Currency symbol', cur, 'Shown in front of amounts, e.g. ₺, $, €.')), [
    { label: 'Cancel', value: null },
    {
      label: isEdit ? 'Save' : 'Create', cls: 'primary',
      onClick: async () => {
        if (isEdit) {
          await api('PATCH', `/groups/${existing.id}`, { name: name.value, currency: cur.value });
          return existing.id;
        }
        return (await api('POST', '/groups', { name: name.value, currency: cur.value })).id;
      },
    },
  ]);
  if (res) {
    if (isEdit) route(); else location.hash = `#/group/${res}`;
  }
}

// ------------------------------------------------------------------ group

const groupTab = {};

async function viewGroup(seq, gid) {
  const main = frame('Group', { back: true });
  loading(main);
  const [g, users] = await Promise.all([api('GET', `/groups/${gid}`), S.user.is_admin ? api('GET', '/users') : null]);
  if (seq !== renderSeq) return;
  frame(g.name, {
    back: true,
    right: S.user.is_admin ? h('button', { class: 'iconbtn', 'aria-label': 'Group settings', type: 'button', onclick: () => groupMenu(g) }, '⋯') : null,
  });
  const tab = groupTab[gid] || 'games';
  const seg = h('div', { class: 'seg', role: 'tablist' });
  const body = h('div');
  const tabs = [['games', 'Games'], ['standings', 'Balances'], ['members', 'Players']];
  const show = (t) => {
    groupTab[gid] = t;
    for (const b of seg.children) b.classList.toggle('on', b.dataset.t === t);
    body.textContent = '';
    if (t === 'games') renderGroupGames(body, g);
    else if (t === 'standings') renderStandings(body, g);
    else renderMembers(body, g, users);
  };
  for (const [t, label] of tabs) seg.append(h('button', { type: 'button', 'data-t': t, onclick: () => show(t) }, label));
  main.append(seg, body);
  show(tab);
  if (S.user.is_admin) {
    main.append(h('a', { class: 'btn primary fab', href: `#/group/${gid}/new` }, '+ New game'));
  }
}

function renderGroupGames(body, g) {
  if (!g.games.length) {
    body.append(h('div', { class: 'card empty' }, S.user.is_admin ? 'No games yet. Start one with “New game”.' : 'No games yet.'));
    return;
  }
  const running = g.games.filter(x => x.status === 'running');
  const done = g.games.filter(x => x.status !== 'running');
  const tile = (x) => h('a', { class: 'tile', href: `#/game/${x.id}` },
    h('div', { class: 'avatar' }, x.status === 'running' ? '▶' : '✓'),
    h('div', { class: 'grow' },
      h('div', { class: 'title' }, x.name,
        x.status === 'running' ? h('span', { class: 'badge live' }, 'LIVE') : null,
        x.finish_kind === 'deal' ? h('span', { class: 'badge gold' }, 'DEAL') : null,
        x.unpaid ? h('span', { class: 'badge red' }, `${x.unpaid} unpaid`) : null),
      h('div', { class: 'muted small' }, `${dateStr(x.created_at)} · ${x.players} players · pot ${money(x.pot, g.currency)}`,
        x.winner ? ` · 🏆 ${x.winner}` : '')),
    h('span', { class: 'chev' }, '›'));
  if (running.length) {
    body.append(h('div', { class: 'section-title' }, 'Running'), running.map(tile));
  }
  if (done.length) {
    body.append(h('div', { class: 'section-title' }, 'Finished'), done.map(tile));
  }
}

function renderStandings(body, g) {
  const st = g.standings.filter(s => s.games || s.outstanding);
  if (!st.length) {
    body.append(h('div', { class: 'card empty' }, 'Balances show up after the first finished game.'));
    return;
  }
  const rows = st.filter(s => s.games).map((s, i) => h('tr', null,
    h('td', null, h('span', { class: 'place' + (i < 3 ? ` p${i + 1}` : '') }, String(i + 1))),
    h('td', null, h('b', null, s.name), h('div', { class: 'muted small' }, `${s.games} games · ${s.wins} wins`)),
    h('td', { class: 'r num' }, money(s.invested, g.currency)),
    h('td', { class: 'r num amount ' + (s.net > 0 ? 'pos' : s.net < 0 ? 'neg' : '') }, signed(s.net, g.currency))));
  body.append(h('div', { class: 'card' }, h('h2', null, 'All-time'),
    h('table', { class: 'tbl' }, h('thead', null, h('tr', null, h('th', null, ''), h('th', null, 'Player'), h('th', { class: 'r' }, 'Bought in'), h('th', { class: 'r' }, 'Net'))),
      h('tbody', null, rows))));

  const owing = g.standings.filter(s => s.outstanding);
  const card = h('div', { class: 'card' }, h('h2', null, h('span', { class: 'grow' }, 'Still to pay')));
  if (!g.settle_all.length) {
    card.append(h('div', { class: 'muted' }, 'Everyone is square. 🎉'));
  } else {
    card.append(h('p', { class: 'muted small' }, 'All unpaid transfers from every game, combined into the fewest payments:'));
    card.append(h('div', null, g.settle_all.map(t => h('div', { class: 'transfer' },
      h('div', { class: 'who' }, h('b', null, t.from_name), h('span', { class: 'arrow' }, '→'), h('b', null, t.to_name)),
      h('div', { class: 'amount num' }, money(t.amount, g.currency))))));
    card.append(h('div', { class: 'muted small' }, owing.map(s => `${s.name} ${signed(s.outstanding, g.currency)}`).join(' · ')));
    if (S.user.is_admin) {
      card.append(h('div', { class: 'btnrow' }, h('button', {
        class: 'btn', type: 'button',
        onclick: act(async () => {
          if (!await confirmBox('Mark everything paid?', 'Use this once all the payments above were made. Every unpaid transfer in this group is marked paid.', 'Mark all paid')) return;
          await api('POST', `/groups/${g.id}/settle-all`, {});
          toast('All settled');
          route();
        }),
      }, 'Mark all as paid')));
    }
  }
  body.append(card);
}

function renderMembers(body, g, users) {
  const card = h('div', { class: 'card' });
  const list = h('ul', { class: 'list' });
  for (const m of g.members) {
    list.append(h('li', null, h('div', { class: 'avatar' }, initials(m.display_name)),
      h('div', { class: 'grow' }, h('div', { class: 'title' }, m.display_name, m.disabled ? h('span', { class: 'badge red' }, 'disabled') : null),
        h('div', { class: 'sub' }, '@' + m.username)),
      S.user.is_admin ? h('button', {
        class: 'btn small danger', type: 'button',
        onclick: act(async () => {
          if (!await confirmBox('Remove from group?', `${m.display_name} stays in past games; they just can't join new ones here.`, 'Remove', true)) return;
          await api('DELETE', `/groups/${g.id}/members/${m.id}`, {});
          route();
        }),
      }, 'Remove') : null));
  }
  if (!g.members.length) list.append(h('li', { class: 'muted' }, 'No players yet.'));
  card.append(h('h2', null, `Players (${g.members.length})`), list);
  body.append(card);
  if (!S.user.is_admin) return;

  const inGroup = new Set(g.members.map(m => m.id));
  const others = users.filter(u => !inGroup.has(u.id) && !u.disabled);
  const add = h('div', { class: 'card' }, h('h2', null, 'Add players'));
  if (others.length) {
    const checks = others.map(u => h('label', { class: 'check' }, h('input', { type: 'checkbox', value: String(u.id) }), u.display_name, h('span', { class: 'muted small' }, '@' + u.username)));
    add.append(h('div', null, checks), h('div', { class: 'btnrow' }, h('button', {
      class: 'btn primary', type: 'button',
      onclick: act(async () => {
        const ids = checks.map(c => c.querySelector('input')).filter(i => i.checked).map(i => Number(i.value));
        if (!ids.length) { toast('Tick at least one player', true); return; }
        for (const id of ids) await api('POST', `/groups/${g.id}/members`, { user_id: id });
        toast(ids.length === 1 ? 'Player added' : `${ids.length} players added`);
        route();
      }),
    }, 'Add selected')));
  } else {
    add.append(h('p', { class: 'muted small' }, 'Every existing player is already in this group.'));
  }
  add.append(h('div', { class: 'btnrow' }, h('button', {
    class: 'btn', type: 'button',
    onclick: async () => {
      const u = await userDialog();
      if (u) {
        await api('POST', `/groups/${g.id}/members`, { user_id: u.id }).catch(e => toast(e.message, true));
        route();
      }
    },
  }, '+ Create a new player')));
  body.append(add);
}

async function groupMenu(g) {
  const choice = await modal(g.name, h('p', { class: 'muted' }, 'Group settings'), [
    { label: 'Edit', value: 'edit' },
    { label: 'Delete group', value: 'delete', cls: 'danger' },
    { label: 'Close', value: null },
  ]);
  if (choice === 'edit') groupDialog(g);
  if (choice === 'delete') {
    if (!await confirmBox('Delete group?', `This deletes “${g.name}” with all ${g.games.length} games and their payments. It can't be undone.`, 'Delete', true)) return;
    await api('DELETE', `/groups/${g.id}`, {}).catch(e => toast(e.message, true));
    location.hash = '#/';
  }
}

// ------------------------------------------------------------------ new game

const PRESETS = [
  ['Winner takes all', [10000]],
  ['65 / 35', [6500, 3500]],
  ['60 / 30 / 10', [6000, 3000, 1000]],
  ['50 / 30 / 20', [5000, 3000, 2000]],
  ['50 / 25 / 15 / 10', [5000, 2500, 1500, 1000]],
];

function structureEditor(initial, getPot, cur, onChange) {
  let rows = initial.slice();
  const wrap = h('div');
  const chips = h('div', { class: 'chips' });
  const list = h('div');
  const total = h('div', { class: 'total-bar' });
  const render = () => {
    chips.textContent = '';
    for (const [label, bps] of PRESETS) {
      const on = bps.length === rows.length && bps.every((b, i) => b === rows[i]);
      chips.append(h('button', { type: 'button', class: 'chip' + (on ? ' on' : ''), onclick: () => { rows = bps.slice(); render(); } }, label));
    }
    list.textContent = '';
    const pot = getPot();
    const valid = rows.every(r => r > 0);
    const sum = rows.reduce((a, b) => a + b, 0);
    const amounts = valid && sum === 10000 && pot > 0 ? allocate(pot, rows, 100) : null;
    rows.forEach((bp, i) => {
      const inp = h('input', { class: 'input narrow', inputmode: 'decimal', value: pctStr(bp), 'aria-label': `${ordinal(i + 1)} place percent` });
      inp.addEventListener('input', () => {
        const v = parsePct(inp.value);
        inp.classList.toggle('bad', isNaN(v) || v <= 0);
        rows[i] = isNaN(v) ? 0 : v;
        updateTotal();
      });
      inp.addEventListener('change', render);
      list.append(h('div', { class: 'structrow' }, h('span', { class: 'lbl' }, ordinal(i + 1)), h('span', { class: 'pctwrap' }, inp, '%'),
        h('span', { class: 'amt num' }, amounts ? money(amounts[i], cur()) : ''),
        rows.length > 1 ? h('button', { class: 'iconbtn', type: 'button', 'aria-label': 'Remove place', onclick: () => { rows.splice(i, 1); render(); } }, '×') : null));
    });
    list.append(h('button', { class: 'btn small', type: 'button', onclick: () => { rows.push(0); render(); } }, '+ Add place'));
    updateTotal();
  };
  const updateTotal = () => {
    const sum = rows.reduce((a, b) => a + b, 0);
    total.className = 'total-bar ' + (sum === 10000 ? 'ok' : 'bad');
    total.textContent = '';
    total.append(h('span', null, 'Total'), h('span', null, `${pctStr(sum)}%` + (sum === 10000 ? ' ✓' : sum < 10000 ? ` (${pctStr(10000 - sum)}% left)` : ` (${pctStr(sum - 10000)}% too much)`)));
    onChange && onChange();
  };
  wrap.append(chips, list, total);
  render();
  return { el: wrap, get: () => rows.slice(), refresh: render };
}

async function viewNewGame(seq, gid) {
  const main = frame('New game', { back: true });
  loading(main);
  const g = await api('GET', `/groups/${gid}`);
  if (seq !== renderSeq) return;
  main.textContent = '';
  if (g.members.length < 2) {
    main.append(h('div', { class: 'card empty' }, 'Add at least two players to the group first.'),
      h('a', { class: 'btn block', href: `#/group/${gid}` }, 'Back to group'));
    return;
  }
  const today = new Date().toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
  const n = g.games.length + 1;
  const name = h('input', { class: 'input', maxlength: '40', value: `Game #${n} · ${today}` });
  const buyIn = h('input', { class: 'input', inputmode: 'decimal', placeholder: 'e.g. 100' });
  const checks = g.members.filter(m => !m.disabled).map(m => h('label', { class: 'check' },
    h('input', { type: 'checkbox', value: String(m.id), checked: true }), h('span', { class: 'grow' }, m.display_name)));
  const count = h('span', { class: 'muted small' });
  const selected = () => checks.map(c => c.querySelector('input')).filter(i => i.checked).map(i => Number(i.value));
  const pot = () => { const b = parseMoney(buyIn.value); return isNaN(b) ? 0 : b * selected().length; };
  const potEl = h('div', { class: 'muted small' });
  const editor = structureEditor([6000, 3000, 1000], pot, () => g.currency, () => {});
  const refresh = () => {
    count.textContent = `${selected().length} selected`;
    potEl.textContent = pot() ? `Starting pot: ${money(pot(), g.currency)} (more with rebuys)` : '';
    editor.refresh();
  };
  buyIn.addEventListener('input', refresh);
  checks.forEach(c => c.addEventListener('change', refresh));
  const allBtn = h('button', {
    class: 'btn small', type: 'button',
    onclick: () => { const all = selected().length !== checks.length; checks.forEach(c => { c.querySelector('input').checked = all; }); refresh(); },
  }, 'All / none');
  const errEl = h('div', { class: 'err' });
  main.append(
    h('div', { class: 'card' }, h('h2', null, 'Game'), field('Name', name), field(`Buy-in (${g.currency})`, buyIn), potEl),
    h('div', { class: 'card' }, h('h2', null, h('span', { class: 'grow' }, 'Players'), count, allBtn), h('div', null, checks)),
    h('div', { class: 'card' }, h('h2', null, 'Payouts'), h('p', { class: 'muted small' }, 'Share of the pot for each finishing place.'), editor.el),
    errEl,
    h('button', {
      class: 'btn primary block', type: 'button',
      onclick: act(async () => {
        errEl.textContent = '';
        const b = parseMoney(buyIn.value);
        if (isNaN(b) || b <= 0) { errEl.textContent = 'Enter the buy-in amount.'; return; }
        try {
          const res = await api('POST', `/groups/${gid}/games`, { name: name.value, buy_in: b, payout_bp: editor.get(), players: selected() });
          location.replace(`#/game/${res.id}`);
        } catch (e) { errEl.textContent = e.message; }
      }),
    }, 'Start game'));
  refresh();
}

// ------------------------------------------------------------------ game

const dealState = {};

async function viewGame(seq, id) {
  const main = frame('Game', { back: true });
  loading(main);
  const game = await api('GET', `/games/${id}`);
  if (seq !== renderSeq) return;
  renderGame(game);
  let last = JSON.stringify(game);
  pollTimer = setInterval(async () => {
    if (document.hidden || seq !== renderSeq) return;
    if (document.activeElement && /INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) return;
    if ($('.modal-back') || (dealState[id] && dealState[id].dirty)) return;
    try {
      const fresh = await api('GET', `/games/${id}`);
      const s = JSON.stringify(fresh);
      if (s !== last && seq === renderSeq) { last = s; renderGame(fresh); }
    } catch (e) { /* offline: keep showing the last state */ }
  }, 8000);
}

function rerender(game) {
  // After an admin action: the response is the new game state.
  renderGame(game);
}

function renderGame(game) {
  const cur = game.group.currency;
  const admin = S.user.is_admin;
  const scrollY = window.scrollY;
  const main = frame(game.name, {
    back: true,
    right: admin ? h('button', { class: 'iconbtn', 'aria-label': 'Game settings', type: 'button', onclick: () => gameMenu(game) }, '⋯') : null,
  });
  if (game.status === 'running') renderRunning(main, game, cur, admin);
  else renderFinished(main, game, cur, admin);
  window.scrollTo(0, scrollY);
}

function prizeChips(game, cur) {
  return h('div', { class: 'prizes' }, game.prizes.map((a, i) => h('span', { class: 'prize num' }, h('b', null, ordinal(i + 1)), money(a, cur))));
}

function renderRunning(main, game, cur, admin) {
  const total = game.players.length;
  main.append(h('div', { class: 'card hero' },
    h('div', { class: 'label' }, 'Pot'),
    h('div', { class: 'big num' }, money(game.pot, cur)),
    h('div', { class: 'meta' },
      h('span', null, `Buy-in ${money(game.buy_in, cur)}`),
      h('span', null, `${game.alive} of ${total} left`),
      h('span', null, `${game.players.reduce((a, p) => a + p.buy_ins, 0)} buy-ins`)),
    prizeChips(game, cur)));

  const alive = game.players.filter(p => !p.busted);
  const out = game.players.filter(p => p.busted).sort((a, b) => a.place - b.place);

  const post = (path, body) => act(async () => rerender(await api('POST', path, body || {})));
  const put = (path, body) => act(async () => rerender(await api('PUT', path, body)));

  const inList = h('ul', { class: 'list' });
  for (const p of alive) {
    inList.append(h('li', null,
      h('div', { class: 'avatar' }, initials(p.name)),
      h('div', { class: 'grow' }, h('div', { class: 'title' }, p.name),
        h('div', { class: 'sub num' }, `${p.buy_ins}× · ${money(p.invested, cur)}`)),
      admin ? h('div', { class: 'stepper' },
        h('button', { class: 'btn round', type: 'button', 'aria-label': `Remove a buy-in from ${p.name}`, disabled: p.buy_ins <= 1, onclick: put(`/games/${game.id}/players/${p.user_id}/buy-ins`, { buy_ins: p.buy_ins - 1 }) }, '−'),
        h('span', { class: 'count num' }, String(p.buy_ins)),
        h('button', { class: 'btn round', type: 'button', 'aria-label': `Add a buy-in for ${p.name}`, onclick: post(`/games/${game.id}/players/${p.user_id}/rebuy`) }, '+')) : null,
      admin ? h('button', {
        class: 'btn small danger', type: 'button',
        onclick: act(async () => {
          const place = game.alive;
          const msg = game.alive === 2
            ? `${p.name} finishes ${ordinal(place)}, and the game ends.`
            : `${p.name} finishes ${ordinal(place)}.`;
          if (!await confirmBox(`${p.name} is out?`, msg, 'Out')) return;
          rerender(await api('POST', `/games/${game.id}/players/${p.user_id}/bust`, {}));
        }),
      }, 'Out') : null));
  }
  main.append(h('div', { class: 'card' }, h('h2', null, `Still in (${alive.length})`), inList));

  if (out.length) {
    const outList = h('ul', { class: 'list' });
    for (const p of out) {
      const paid = p.place <= game.prizes.length;
      outList.append(h('li', null,
        h('span', { class: 'place' + (p.place <= 3 ? ` p${p.place}` : '') }, String(p.place)),
        h('div', { class: 'grow' }, h('div', { class: 'title' }, p.name),
          h('div', { class: 'sub num' }, `${p.buy_ins}× · ${money(p.invested, cur)}`, paid ? ` · wins ${money(game.prizes[p.place - 1], cur)}` : '')),
        admin ? h('button', { class: 'btn small', type: 'button', onclick: post(`/games/${game.id}/players/${p.user_id}/unbust`) }, 'Undo') : null,
        admin ? h('button', { class: 'btn small', type: 'button', onclick: post(`/games/${game.id}/players/${p.user_id}/rebuy`) }, 'Rebuy') : null));
    }
    main.append(h('div', { class: 'card' }, h('h2', null, 'Out'), outList));
  }

  if (admin && alive.length >= 2) main.append(dealMaker(game, cur));
  if (!admin) main.append(h('p', { class: 'muted small' }, 'Updates automatically while the game is running.'));
}

function dealMaker(game, cur) {
  const st = dealState[game.id] = dealState[game.id] && dealState[game.id].sig === sigOf(game)
    ? dealState[game.id]
    : { sig: sigOf(game), open: false, dirty: false, shares: { ...game.deal_prefill }, chips: {}, mode: 'pct' };
  const card = h('div', { class: 'card' });
  const head = h('h2', null, h('span', { class: 'grow' }, '🤝 Deal maker'),
    h('button', { class: 'btn small', type: 'button', onclick: () => { st.open = !st.open; draw(); } }, st.open ? 'Hide' : 'Make a deal'));
  const body = h('div');
  card.append(head, body);

  const ids = game.players.map(p => String(p.user_id));
  const amounts = () => {
    const w = ids.map(i => st.shares[i] || 0);
    const sum = w.reduce((a, b) => a + b, 0);
    return sum === 10000 ? allocate(game.pot, w, 100) : w.map(x => Math.floor(game.pot * x / 10000));
  };

  function draw() {
    head.querySelector('button').textContent = st.open ? 'Hide' : 'Make a deal';
    body.textContent = '';
    if (!st.open) {
      body.append(h('p', { class: 'muted small' }, 'Players left agree to split the pot? Set each share and see the amounts before applying.'));
      return;
    }
    const seg = h('div', { class: 'seg' },
      h('button', { type: 'button', class: st.mode === 'pct' ? 'on' : '', onclick: () => { st.mode = 'pct'; draw(); } }, 'By percent'),
      h('button', { type: 'button', class: st.mode === 'chips' ? 'on' : '', onclick: () => { st.mode = 'chips'; draw(); } }, 'By chip count'));
    body.append(seg);

    const rowsEl = h('div');
    const totalEl = h('div', { class: 'total-bar' });
    const amtEls = {};
    const pctLabels = {};
    const refresh = () => {
      const am = amounts();
      ids.forEach((id, i) => { amtEls[id].textContent = money(am[i], cur); });
      const sum = ids.reduce((a, i) => a + (st.shares[i] || 0), 0);
      totalEl.className = 'total-bar ' + (sum === 10000 ? 'ok' : 'bad');
      totalEl.textContent = '';
      totalEl.append(h('span', null, 'Total'), h('span', null, `${pctStr(sum)}%` + (sum === 10000 ? ' ✓' : sum < 10000 ? ` · ${pctStr(10000 - sum)}% left` : ` · ${pctStr(sum - 10000)}% too much`)));
      apply.disabled = sum !== 10000;
    };
    const alive = game.players.filter(p => !p.busted);
    const fixed = ids.filter(i => game.players.find(p => String(p.user_id) === i).busted).reduce((a, i) => a + (st.shares[i] || 0), 0);
    const chipsToShares = () => {
      const chips = alive.map(p => Math.max(0, Number(st.chips[p.user_id]) || 0));
      const pool = 10000 - fixed;
      if (chips.reduce((a, b) => a + b, 0) <= 0) return;
      const split = allocate(pool, chips, 1);
      alive.forEach((p, i) => {
        st.shares[p.user_id] = split[i];
        if (pctLabels[p.user_id]) pctLabels[p.user_id].textContent = pctStr(split[i]) + '%';
      });
    };

    for (const p of game.players) {
      const id = String(p.user_id);
      const amt = h('span', { class: 'amt num' });
      amtEls[id] = amt;
      const pct = h('input', { class: 'input narrow', inputmode: 'decimal', value: pctStr(st.shares[id] || 0), 'aria-label': `${p.name} percent` });
      pct.addEventListener('input', () => {
        const v = parsePct(pct.value);
        pct.classList.toggle('bad', isNaN(v));
        st.shares[id] = isNaN(v) ? 0 : v;
        st.dirty = true;
        refresh();
      });
      let control = h('span', { class: 'pctwrap' }, pct, '%');
      if (st.mode === 'chips' && !p.busted) {
        const chip = h('input', { class: 'input narrow', inputmode: 'numeric', placeholder: 'chips', value: st.chips[id] || '', 'aria-label': `${p.name} chips` });
        chip.addEventListener('input', () => { st.chips[id] = chip.value.replace(/[^\d]/g, ''); st.dirty = true; chipsToShares(); refresh(); });
        pctLabels[id] = h('span', { class: 'muted small' }, pctStr(st.shares[id] || 0) + '%');
        control = h('span', { class: 'pctwrap' }, chip, pctLabels[id]);
      }
      rowsEl.append(h('div', { class: 'dealrow' },
        h('span', { class: 'who' }, p.name, p.busted ? h('span', { class: 'badge' }, ordinal(p.place)) : null),
        control, amt));
    }
    const apply = h('button', {
      class: 'btn gold', type: 'button',
      onclick: act(async () => {
        const am = amounts();
        const summary = h('div', null, h('p', { class: 'muted' }, 'The game ends with these payouts:'),
          h('table', { class: 'tbl' }, h('tbody', null, game.players.map((p, i) => h('tr', null,
            h('td', null, p.name), h('td', { class: 'r' }, pctStr(st.shares[p.user_id] || 0) + '%'), h('td', { class: 'r num amount' }, money(am[i], cur)))))));
        if (!await modal('Apply this deal?', summary, [{ label: 'Cancel', value: false }, { label: 'Apply deal', value: true, cls: 'gold' }])) return;
        const res = await api('POST', `/games/${game.id}/deal`, { shares: st.shares });
        delete dealState[game.id];
        toast('Deal applied');
        rerender(res);
      }),
    }, 'Apply deal');
    body.append(rowsEl, totalEl,
      h('div', { class: 'btnrow' },
        h('button', {
          class: 'btn', type: 'button',
          onclick: () => {
            const pool = 10000 - fixed;
            const even = allocate(pool, alive.map(() => 1), 1);
            alive.forEach((p, i) => { st.shares[p.user_id] = even[i]; });
            st.dirty = true; draw();
          },
        }, 'Even chop'),
        h('button', { class: 'btn', type: 'button', onclick: () => { st.shares = { ...game.deal_prefill }; st.chips = {}; st.dirty = false; draw(); } }, 'Reset'),
        apply),
      h('p', { class: 'hint' }, 'Players already out keep their place\'s share unless you change it. Amounts are rounded to whole units.'));
    refresh();
  }
  draw();
  return card;
}

function sigOf(game) {
  return JSON.stringify([game.pot, game.players.map(p => [p.user_id, p.busted, p.buy_ins])]);
}

function renderFinished(main, game, cur, admin) {
  const winner = game.players.find(p => p.place === 1);
  main.append(h('div', { class: 'card hero' },
    h('div', { class: 'label' }, game.finish_kind === 'deal' ? 'Finished by a deal' : 'Winner'),
    h('div', { class: 'big' }, game.finish_kind === 'deal' ? '🤝 Deal' : `🏆 ${winner ? winner.name : ''}`),
    h('div', { class: 'meta' },
      h('span', null, `Pot ${money(game.pot, cur)}`),
      h('span', null, `Buy-in ${money(game.buy_in, cur)}`),
      h('span', null, `${game.players.length} players`),
      game.finished_at ? h('span', null, dateStr(game.finished_at)) : null)));

  const sorted = game.players.slice().sort((a, b) => (a.place || 99) - (b.place || 99));
  const rounded = game.rounding && game.rounding.step;
  const rows = sorted.map(p => h('tr', null,
    h('td', null, h('span', { class: 'place' + (p.place <= 3 ? ` p${p.place}` : '') }, String(p.place || '-'))),
    h('td', null, h('b', null, p.name), h('div', { class: 'muted small num' }, `${p.buy_ins}× · ${money(p.invested, cur)}`, p.deal_bp != null && game.finish_kind === 'deal' ? ` · ${pctStr(p.deal_bp)}%` : '')),
    h('td', { class: 'r num' }, rounded && p.base_payout !== p.payout ? h('span', { class: 'strike' }, money(p.base_payout, cur)) : null, money(p.payout || 0, cur)),
    h('td', { class: 'r num amount ' + (p.net > 0 ? 'pos' : p.net < 0 ? 'neg' : '') }, signed(p.net, cur))));
  const resultCard = h('div', { class: 'card' }, h('h2', null, 'Results'),
    h('table', { class: 'tbl' }, h('thead', null, h('tr', null, h('th', null, ''), h('th', null, 'Player'), h('th', { class: 'r' }, 'Gets'), h('th', { class: 'r' }, 'Net'))),
      h('tbody', null, rows)));

  if (admin) {
    const r = game.rounding;
    const post = (action) => act(async () => rerender(await api('POST', `/games/${game.id}/round`, { action })));
    const bar = h('div', { class: 'roundbar' });
    if (r.step) bar.append(h('div', { class: 'note' }, `Payouts rounded to ${money(r.step, cur)}s. The pot total is unchanged.`));
    else bar.append(h('div', { class: 'note' }, 'Round the winners\' payouts to friendlier numbers (the pot total stays the same).'));
    if (!r.step) {
      bar.append(h('button', { class: 'btn primary', type: 'button', disabled: !r.next_step, onclick: post('more') },
        r.next_step ? `Round to ${money(r.next_step, cur)}s` : 'Already round'));
    } else {
      bar.append(h('button', { class: 'btn', type: 'button', onclick: post('revert') }, '↶ Revert'));
      bar.append(h('button', { class: 'btn primary', type: 'button', disabled: !r.next_step, onclick: post('more') },
        r.next_step ? `Round more (${money(r.next_step, cur)}s)` : 'Can\'t round more'));
    }
    resultCard.append(bar);
  }
  main.append(resultCard);

  const tcard = h('div', { class: 'card' });
  const paidCount = game.transfers.filter(t => t.paid).length;
  tcard.append(h('h2', null, h('span', { class: 'grow' }, 'Who pays whom'),
    game.transfers.length ? h('span', { class: 'badge' + (paidCount === game.transfers.length ? ' green' : '') }, `${paidCount}/${game.transfers.length} paid`) : null));
  if (!game.transfers.length) {
    tcard.append(h('p', { class: 'muted' }, 'Nobody owes anything.'));
  } else {
    tcard.append(h('p', { class: 'muted small' }, `${game.transfers.length} payment${game.transfers.length > 1 ? 's' : ''} — the fewest possible, preferring round amounts.`));
    for (const t of game.transfers) {
      const box = admin ? h('input', {
        type: 'checkbox', class: 'paidbox', checked: t.paid, 'aria-label': 'Paid',
        onchange: act(async (e) => rerender(await api('PUT', `/games/${game.id}/transfers/${t.id}`, { paid: e.target.checked }))),
      }) : (t.paid ? h('span', { class: 'badge green' }, 'paid') : null);
      tcard.append(h('div', { class: 'transfer' + (t.paid ? ' paid' : '') },
        h('div', { class: 'who' }, h('b', null, t.from_name), h('span', { class: 'arrow' }, '→'), h('b', null, t.to_name)),
        h('div', { class: 'amount num' }, money(t.amount, cur)), box));
    }
  }
  tcard.append(h('div', { class: 'btnrow' }, h('button', { class: 'btn', type: 'button', onclick: act(() => shareResults(game)) }, '↗ Share results')));
  main.append(tcard);
}

async function shareResults(game) {
  const cur = game.group.currency;
  const lines = [`${game.group.name} · ${game.name}`, `Pot ${money(game.pot, cur)}`, ''];
  for (const p of game.players.slice().sort((a, b) => (a.place || 99) - (b.place || 99))) {
    lines.push(`${p.place}. ${p.name}: ${money(p.payout || 0, cur)} (${signed(p.net, cur)})`);
  }
  if (game.transfers.length) {
    lines.push('', 'Payments:');
    for (const t of game.transfers) lines.push(`${t.from_name} → ${t.to_name}: ${money(t.amount, cur)}${t.paid ? ' ✓' : ''}`);
  }
  const text = lines.join('\n');
  if (navigator.share) {
    try { await navigator.share({ text }); return; } catch (e) { if (e.name === 'AbortError') return; }
  }
  await copyText(text);
  toast('Results copied');
}

async function gameMenu(game) {
  const buttons = [];
  if (game.status === 'running') {
    buttons.push({ label: 'Edit game', value: 'edit' }, { label: 'Add player', value: 'add' }, { label: 'Remove player', value: 'remove' });
  } else {
    buttons.push({ label: 'Rename', value: 'rename' }, { label: 'Reopen game', value: 'reopen' });
  }
  buttons.push({ label: 'Delete game', value: 'delete', cls: 'danger' }, { label: 'Close', value: null });
  const choice = await modal(game.name, h('p', { class: 'muted small' }, game.status === 'running' ? 'Running game' : 'Finished game'), buttons);
  try {
    if (choice === 'edit') await editGameDialog(game);
    else if (choice === 'rename') {
      const name = h('input', { class: 'input', maxlength: '40', value: game.name });
      const res = await modal('Rename game', field('Name', name), [{ label: 'Cancel', value: null },
        { label: 'Save', cls: 'primary', onClick: async () => api('PATCH', `/games/${game.id}`, { name: name.value }) }]);
      if (res) rerender(res);
    } else if (choice === 'add') await addPlayerDialog(game);
    else if (choice === 'remove') await removePlayerDialog(game);
    else if (choice === 'reopen') {
      if (!await confirmBox('Reopen game?', 'Payouts, rounding, deal and the payment list are cleared and the game is running again. Use this to fix a mistake.', 'Reopen')) return;
      rerender(await api('POST', `/games/${game.id}/reopen`, {}));
    } else if (choice === 'delete') {
      if (!await confirmBox('Delete game?', `“${game.name}” and its payments will be deleted. This can't be undone.`, 'Delete', true)) return;
      await api('DELETE', `/games/${game.id}`, {});
      location.replace(`#/group/${game.group.id}`);
    }
  } catch (e) { toast(e.message, true); }
}

async function editGameDialog(game) {
  const cur = game.group.currency;
  const name = h('input', { class: 'input', maxlength: '40', value: game.name });
  const buyIn = h('input', { class: 'input', inputmode: 'decimal', value: String(game.buy_in / 100) });
  const pot = () => { const b = parseMoney(buyIn.value); return isNaN(b) ? 0 : b * game.players.reduce((a, p) => a + p.buy_ins, 0); };
  const editor = structureEditor(game.payout_bp, pot, () => cur);
  buyIn.addEventListener('input', () => editor.refresh());
  const res = await modal('Edit game', h('div', null, field('Name', name), field(`Buy-in (${cur})`, buyIn), h('div', { class: 'section-title' }, 'Payouts'), editor.el), [
    { label: 'Cancel', value: null },
    {
      label: 'Save', cls: 'primary',
      onClick: async () => {
        const b = parseMoney(buyIn.value);
        if (isNaN(b) || b <= 0) throw new Error('Enter the buy-in amount.');
        return api('PATCH', `/games/${game.id}`, { name: name.value, buy_in: b, payout_bp: editor.get() });
      },
    },
  ]);
  if (res) rerender(res);
}

async function addPlayerDialog(game) {
  const g = await api('GET', `/groups/${game.group.id}`);
  const inGame = new Set(game.players.map(p => p.user_id));
  const avail = g.members.filter(m => !inGame.has(m.id) && !m.disabled);
  if (!avail.length) { toast('Everyone in the group is already playing', true); return; }
  const sel = h('select', { class: 'input' }, avail.map(m => h('option', { value: String(m.id) }, m.display_name)));
  const res = await modal('Add a late player', field('Player', sel, 'They join with one buy-in.'), [
    { label: 'Cancel', value: null },
    { label: 'Add', cls: 'primary', onClick: async () => api('POST', `/games/${game.id}/players`, { user_id: Number(sel.value) }) },
  ]);
  if (res) rerender(res);
}

async function removePlayerDialog(game) {
  const sel = h('select', { class: 'input' }, game.players.map(p => h('option', { value: String(p.user_id) }, p.name)));
  const res = await modal('Remove a player', field('Player', sel, 'For players added by mistake. Their buy-ins leave the pot.'), [
    { label: 'Cancel', value: null },
    { label: 'Remove', cls: 'danger solid', onClick: async () => api('DELETE', `/games/${game.id}/players/${sel.value}`, {}) },
  ]);
  if (res) rerender(res);
}

// ------------------------------------------------------------------ players (admin users)

async function userDialog() {
  const name = h('input', { class: 'input', required: true, maxlength: '40', autocomplete: 'off' });
  const user = h('input', { class: 'input', required: true, maxlength: '32', autocapitalize: 'off', autocomplete: 'off', spellcheck: 'false' });
  let touched = false;
  user.addEventListener('input', () => { touched = true; });
  name.addEventListener('input', () => {
    if (!touched) user.value = name.value.toLowerCase().normalize('NFKD').replace(/[̀-ͯ]/g, '').replace(/ı/g, 'i').replace(/[^a-z0-9_.-]+/g, '.').replace(/^\.+|\.+$/g, '').slice(0, 32);
  });
  const isAdmin = h('input', { type: 'checkbox' });
  const res = await modal('New player', h('div', null, field('Name', name), field('Username (for signing in)', user),
    h('label', { class: 'check' }, isAdmin, 'Admin (can manage everything)')), [
    { label: 'Cancel', value: null },
    { label: 'Create', cls: 'primary', onClick: async () => api('POST', '/users', { username: user.value, display_name: name.value, is_admin: isAdmin.checked }) },
  ]);
  if (!res) return null;
  await showTempPassword(res.user.display_name, res.temp_password);
  return res.user;
}

async function viewUsers(seq) {
  if (!S.user.is_admin) { location.hash = '#/'; return; }
  const main = frame('Players');
  loading(main);
  const users = await api('GET', '/users');
  if (seq !== renderSeq) return;
  main.textContent = '';
  const list = h('ul', { class: 'list' });
  for (const u of users) {
    list.append(h('li', null, h('div', { class: 'avatar' + (u.disabled ? ' out' : '') }, initials(u.display_name)),
      h('div', { class: 'grow' },
        h('div', { class: 'title' }, u.display_name,
          u.is_admin ? h('span', { class: 'badge gold' }, 'admin') : null,
          u.totp ? h('span', { class: 'badge green' }, '2FA') : null,
          u.disabled ? h('span', { class: 'badge red' }, 'disabled') : null,
          u.must_change_pw ? h('span', { class: 'badge' }, 'temp pw') : null),
        h('div', { class: 'sub' }, '@' + u.username)),
      h('button', { class: 'btn small', type: 'button', onclick: () => userMenu(u) }, 'Manage')));
  }
  main.append(h('div', { class: 'card' }, h('h2', null, `All players (${users.length})`), list),
    h('p', { class: 'muted small' }, 'Every player gets a sign-in to follow their games and balances. Only admins can change things.'),
    h('button', { class: 'btn primary fab', type: 'button', onclick: async () => { if (await userDialog()) route(); } }, '+ New player'));
}

async function userMenu(u) {
  const isMe = u.id === S.user.id;
  const buttons = [
    { label: 'Rename', value: 'rename' },
    { label: 'Reset password', value: 'reset' },
    { label: u.is_admin ? 'Remove admin' : 'Make admin', value: 'admin' },
  ];
  if (u.totp) buttons.push({ label: 'Turn off 2FA', value: 'totp' });
  if (!isMe) buttons.push({ label: u.disabled ? 'Enable' : 'Disable', value: 'disable' }, { label: 'Delete', value: 'delete', cls: 'danger' });
  buttons.push({ label: 'Close', value: null });
  const choice = await modal(u.display_name, h('p', { class: 'muted small' }, '@' + u.username), buttons);
  try {
    if (choice === 'rename') {
      const name = h('input', { class: 'input', maxlength: '40', value: u.display_name });
      const res = await modal('Rename', field('Name', name), [{ label: 'Cancel', value: null },
        { label: 'Save', cls: 'primary', onClick: async () => api('PATCH', `/users/${u.id}`, { display_name: name.value }) }]);
      if (!res) return;
    } else if (choice === 'reset') {
      if (!await confirmBox('Reset password?', `${u.display_name} gets a new temporary password and is signed out everywhere.`, 'Reset')) return;
      const res = await api('POST', `/users/${u.id}/reset-password`, {});
      await showTempPassword(u.display_name, res.temp_password);
    } else if (choice === 'admin') {
      await api('PATCH', `/users/${u.id}`, { is_admin: !u.is_admin });
      if (isMe) { await boot(); return; }
    } else if (choice === 'totp') {
      if (!await confirmBox('Turn off 2FA?', `Use this if ${u.display_name} lost their authenticator.`, 'Turn off', true)) return;
      await api('PATCH', `/users/${u.id}`, { reset_totp: true });
    } else if (choice === 'disable') {
      await api('PATCH', `/users/${u.id}`, { disabled: !u.disabled });
    } else if (choice === 'delete') {
      if (!await confirmBox('Delete player?', `Delete ${u.display_name}? Only possible if they never played a game.`, 'Delete', true)) return;
      await api('DELETE', `/users/${u.id}`, {});
    } else return;
    toast('Saved');
    route();
  } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------------ account

async function viewAccount() {
  const main = frame('Account');
  const u = S.user;
  main.append(h('div', { class: 'card' },
    h('div', { class: 'tile' }, h('div', { class: 'avatar' }, initials(u.display_name)),
      h('div', { class: 'grow' }, h('div', { class: 'title' }, u.display_name, u.is_admin ? h('span', { class: 'badge gold' }, 'admin') : null),
        h('div', { class: 'muted small' }, '@' + u.username)))));
  main.append(passwordForm());

  const tf = h('div', { class: 'card' }, h('h2', null, 'Two-factor sign-in (2FA)'));
  if (u.totp) {
    const pw = h('input', { class: 'input', type: 'password', autocomplete: 'current-password' });
    const code = h('input', { class: 'input', inputmode: 'numeric', maxlength: '6', autocomplete: 'one-time-code' });
    tf.append(h('p', null, h('span', { class: 'badge green' }, 'ON'), ' Signing in needs a code from your authenticator app.'),
      field('Password', pw), field('Current code', code),
      h('button', {
        class: 'btn danger', type: 'button',
        onclick: act(async () => { await api('POST', '/me/totp/disable', { password: pw.value, code: code.value }); toast('2FA turned off'); await boot(); }),
      }, 'Turn off 2FA'));
  } else {
    const area = h('div');
    tf.append(h('p', { class: 'muted small' }, 'Recommended for admins: even with your password, nobody can sign in without your phone.'), area,
      h('button', {
        class: 'btn primary', type: 'button',
        onclick: act(async (e) => {
          const res = await api('POST', '/me/totp/setup', {});
          e.target.classList.add('hidden');
          const code = h('input', { class: 'input', inputmode: 'numeric', maxlength: '6', autocomplete: 'one-time-code' });
          area.append(h('p', null, 'Scan this with Google Authenticator, Aegis, 1Password or similar:'),
            h('img', { class: 'qr', src: res.qr, alt: 'QR code for your authenticator app' }),
            h('p', { class: 'muted small' }, 'Or enter this key by hand:'), h('div', { class: 'secret' }, res.secret),
            h('div', { class: 'section-title' }, ''), field('Code from the app', code),
            h('button', {
              class: 'btn primary', type: 'button',
              onclick: act(async () => { await api('POST', '/me/totp/enable', { code: code.value }); toast('2FA is on'); await boot(); }),
            }, 'Turn on 2FA'));
        }),
      }, 'Set up 2FA'));
  }
  main.append(tf);
  main.append(h('div', { class: 'card' }, h('h2', null, 'Sessions'),
    h('div', { class: 'btnrow' },
      h('button', { class: 'btn', type: 'button', onclick: act(logout) }, 'Sign out'),
      h('button', {
        class: 'btn danger', type: 'button',
        onclick: act(async () => {
          if (!await confirmBox('Sign out everywhere?', 'All your devices, including this one, are signed out.', 'Sign out all', true)) return;
          await api('POST', '/logout-all', {});
          S.user = null;
          route();
        }),
      }, 'Sign out everywhere'))));
  main.append(h('p', { class: 'muted small' }, `Poker Balance ${S.version}`));
}

// ------------------------------------------------------------------ start

// Sign-in needs a Secure cookie, which browsers refuse on http:// pages: always move to https.
if (location.protocol === 'http:' && !/^(localhost|127\.0\.0\.1|\[::1\])$/.test(location.hostname)) {
  location.replace('https://' + location.hostname + location.pathname + location.search + location.hash);
}
window.addEventListener('hashchange', route);
if ('serviceWorker' in navigator && window.isSecureContext) {
  navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {});
}
boot();
