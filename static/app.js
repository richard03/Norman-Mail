'use strict';

const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const COLORS = ['#2563eb', '#db2777', '#059669', '#d97706', '#7c3aed', '#0891b2', '#dc2626', '#65a30d'];

const PRESETS = {
  gmail: { imap: ['imap.gmail.com', 993, 'ssl'], smtp: ['smtp.gmail.com', 465, 'ssl'],
    domains: ['gmail.com', 'googlemail.com'],
    note: 'Doporučujeme přihlášení přes Google. Pokud použijete heslo, musí to být „heslo aplikace“ (myaccount.google.com → Zabezpečení → Hesla aplikací).' },
  seznam: { imap: ['imap.seznam.cz', 993, 'ssl'], smtp: ['smtp.seznam.cz', 465, 'ssl'],
    domains: ['seznam.cz', 'email.cz', 'post.cz', 'spoluzaci.cz', 'stream.cz'],
    note: 'V nastavení schránky Seznamu musí být povolen přístup přes IMAP/externí aplikace.' },
  centrum: { imap: ['imap.centrum.cz', 993, 'ssl'], smtp: ['smtp.centrum.cz', 465, 'ssl'],
    domains: ['centrum.cz'], note: '' },
  icloud: { imap: ['imap.mail.me.com', 993, 'ssl'], smtp: ['smtp.mail.me.com', 587, 'starttls'],
    domains: ['icloud.com', 'me.com', 'mac.com'],
    note: 'iCloud vyžaduje heslo pro aplikaci (appleid.apple.com). Uživatelské jméno je adresa bez @icloud.com.' },
  yahoo: { imap: ['imap.mail.yahoo.com', 993, 'ssl'], smtp: ['smtp.mail.yahoo.com', 465, 'ssl'],
    domains: ['yahoo.com'], note: 'Yahoo vyžaduje heslo aplikace.' },
  outlook: { imap: ['outlook.office365.com', 993, 'ssl'], smtp: ['smtp.office365.com', 587, 'starttls'],
    domains: ['outlook.com', 'hotmail.com', 'live.com', 'outlook.cz'],
    note: 'Pozor: Microsoft u většiny účtů zakázal přihlášení heslem (vyžaduje OAuth), takže připojení nemusí fungovat.' },
};

const state = {
  accounts: [], folders: {}, folderErrors: {},
  view: { type: 'unified', filter: 'important' },  // výchozí pohled po startu: Důležité
  viewUnread: null,
  settings: null,         // nastavení aplikace (z /api/settings)       // nepřečtené v pohledech { important, notifications, all }
  messages: [], page: 0, hasMore: false, query: '', listErrors: [], listLoading: false,
  current: null,          // { accountId, folder, uid, data }
  context: [],            // související zprávy aktuální zprávy
  showImages: false,
  editingId: null,
  compose: null,          // { inReplyTo, references, files: [], forward }
};
let listToken = 0;

const UNIFIED_VIEWS = [['important', 'Důležité'], ['notifications', 'Notifikace'], ['all', 'Všechny doručené']];
const UNIFIED_TITLE = Object.fromEntries(UNIFIED_VIEWS);

// ------------------------------------------------------------------ utility

async function api(path, { method = 'GET', body } = {}) {
  const opts = { method, headers: {} };
  if (method !== 'GET') opts.headers['X-Mailer'] = '1';
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch { /* prázdná odpověď */ }
  if (!r.ok) throw new Error(data?.error || `Chyba serveru (${r.status})`);
  return data;
}

let toastTimer;
function toast(msg, isError = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'show' + (isError ? ' error' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = ''), isError ? 6000 : 3000);
}

const accById = id => state.accounts.find(a => a.id === id);
const accColor = id => COLORS[Math.max(0, state.accounts.findIndex(a => a.id === id)) % COLORS.length];
const enc = encodeURIComponent;

function fmtDate(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000), now = new Date();
  if (d.toDateString() === now.toDateString())
    return d.toLocaleTimeString('cs-CZ', { hour: '2-digit', minute: '2-digit' });
  if (d.getFullYear() === now.getFullYear())
    return d.toLocaleDateString('cs-CZ', { day: 'numeric', month: 'numeric' });
  return d.toLocaleDateString('cs-CZ');
}
const fmtFull = ts => (ts ? new Date(ts * 1000).toLocaleString('cs-CZ') : '');
function fmtSize(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' kB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}
function fmtAddr(a) {
  if (!a.name) return a.addr;
  return `"${a.name.replace(/["\\]/g, '')}" <${a.addr}>`;
}
const shortAddr = list => (list || []).map(a => a.name || a.addr).join(', ') || '(neznámý)';
const badge = n => (n ? `<span class="badge">${n}</span>` : '');

function folderRole(accountId, folder) {
  return (state.folders[accountId] || []).find(f => f.name === folder)?.role;
}

// ------------------------------------------------------------------ sidebar

async function loadAccounts() {
  state.accounts = await api('/api/accounts');
  renderSidebar();
  // Složky se načítají na pozadí – na ně se nečeká (seznam zpráv se zobrazí hned z databáze).
  state.accounts.forEach(a => loadFolders(a.id));
}

async function loadFolders(id) {
  try {
    state.folders[id] = await api(`/api/accounts/${id}/folders`);
    delete state.folderErrors[id];
  } catch (e) {
    state.folderErrors[id] = e.message;
  }
  renderSidebar();
}

function renderSidebar() {
  const unread = state.accounts.reduce(
    (s, a) => s + ((state.folders[a.id] || []).find(f => f.role === 'inbox')?.unseen || 0), 0);
  let h = '';
  if (state.accounts.length) {
    const vu = state.viewUnread || { all: unread };
    for (const [filter, label] of UNIFIED_VIEWS) {
      const active = state.view.type === 'unified' && state.view.filter === filter;
      h += `<div class="nav-item unified ${active ? 'active' : ''}" data-unified="${filter}">
              <span class="label">${label}</span>${badge(vu[filter])}</div>`;
    }
  } else {
    h += `<p class="muted pad">Zatím nemáte žádný účet. Začněte tlačítkem „Přidat účet“.</p>`;
  }
  if (state.accounts.length) h += '<div class="nav-section">Účty</div>';
  for (const a of state.accounts) {
    // Každý účet = jedna položka vedoucí do jeho doručené pošty.
    const inbox = (state.folders[a.id] || []).find(f => f.role === 'inbox');
    const active = state.view.type === 'folder' && state.view.accountId === a.id;
    const error = state.folderErrors[a.id];
    h += `<div class="nav-item account-item ${active ? 'active' : ''}" data-acc="${a.id}"
            title="${esc(error ? error : a.email)}">
        <span class="dot" style="background:${accColor(a.id)}"></span>
        <span class="label">${esc(a.name || a.email)}</span>
        ${error ? `<button class="icon-btn small warn" data-retry="${a.id}" title="Chyba: ${esc(error)} – kliknutím zkusit znovu">!</button>`
                : badge(inbox?.unseen)}
        <button class="icon-btn small edit" data-edit="${a.id}" title="Nastavení účtu">⚙</button>
      </div>`;
  }
  $('#accounts').innerHTML = h;
}

$('#accounts').addEventListener('click', e => {
  const el = e.target.closest('[data-edit],[data-retry],[data-unified],[data-acc]');
  if (!el) return;
  if (el.dataset.edit) return openAccountDialog(el.dataset.edit);
  if (el.dataset.retry) {
    api('/api/sync', { method: 'POST' }).catch(() => {});
    return loadFolders(el.dataset.retry);
  }
  if (el.dataset.unified) return setView({ type: 'unified', filter: el.dataset.unified });
  setView({ type: 'folder', accountId: el.dataset.acc, folder: 'INBOX', title: 'Doručená pošta' });
});

/** Po opuštění Notifikací: vše označit jako přečtené (počty hned, server na pozadí). */
function markNotificationsRead() {
  const vu = state.viewUnread;
  if (vu && vu.notifications) {
    state.viewUnread = { ...vu, all: Math.max(0, vu.all - vu.notifications), notifications: 0 };
    renderSidebar();
  }
  api('/api/notifications/mark-read', { method: 'POST' })
    .then(() => pollSync())
    .catch(err => toast('Označení notifikací jako přečtených selhalo: ' + err.message, true));
}

const isNotifView = v => v.type === 'unified' && v.filter === 'notifications';

function setView(view) {
  if (isNotifView(state.view) && !isNotifView(view) && state.settings?.notify_mark_read_on_leave)
    markNotificationsRead();
  state.view = view;
  state.query = '';
  $('#searchInput').value = '';
  renderSidebar();
  loadList();
}

// ------------------------------------------------------------------ seznam zpráv

async function loadList({ append = false, quiet = false } = {}) {
  const token = ++listToken;
  const v = state.view;
  if (!append) state.page = 0;
  state.listLoading = true;
  if (!append && !quiet) { state.messages = []; state.listErrors = []; }
  renderList();
  try {
    const qs = new URLSearchParams({ page: state.page });
    if (state.query) qs.set('q', state.query);
    let res;
    if (v.type === 'unified') { qs.set('filter', v.filter); res = await api('/api/unified?' + qs); }
    else { qs.set('folder', v.folder); res = await api(`/api/accounts/${v.accountId}/messages?${qs}`); }
    if (token !== listToken) return;
    state.messages = append ? state.messages.concat(res.messages) : res.messages;
    state.hasMore = res.has_more;
    if (!append) state.listVersion = res.version ?? null;
    state.listErrors = res.errors || [];
  } catch (e) {
    if (token !== listToken) return;
    state.listErrors = [e.message];
  }
  state.listLoading = false;
  renderList();
}

function renderList() {
  const v = state.view;
  const acc = v.type === 'folder' ? accById(v.accountId) : null;
  $('#listTitle').textContent = v.type === 'unified' ? UNIFIED_TITLE[v.filter]
    : `${v.title || v.folder} · ${acc?.name || ''}`;
  $('#searchInput').placeholder = v.type === 'unified' ? 'Hledat v doručené poště…' : 'Hledat ve složce…';
  $('#listErrors').innerHTML = state.listErrors.map(e => `<div class="list-error">${esc(e)}</div>`).join('');

  const showTo = v.type === 'folder' && ['sent', 'drafts'].includes(folderRole(v.accountId, v.folder));
  const cur = state.current;
  $('#messages').innerHTML = state.messages.map((m, i) => {
    const active = cur && cur.accountId === m.account_id && cur.folder === m.folder && cur.uid === m.uid;
    return `<li class="msg ${m.seen ? '' : 'unread'} ${active ? 'active' : ''}" data-i="${i}">
      ${v.type === 'unified' ? `<span class="strip" style="background:${accColor(m.account_id)}"></span>` : ''}
      <div class="row1">
        <span class="from">${esc(showTo ? 'Komu: ' + shortAddr(m.to) : shortAddr(m.from))}</span>
        <span class="date">${m.snoozed_until ? `<span title="Odloženo do ${esc(fmtSnooze(m.snoozed_until))}">⏰</span> ` : ''}${m.flagged ? '★ ' : ''}${m.has_attachments ? '📎 ' : ''}${fmtDate(m.date)}</span>
      </div>
      <div class="subject">${esc(m.subject)}</div>
      ${v.type === 'unified' ? `<div class="acc-label">${esc(accById(m.account_id)?.name || '')}</div>` : ''}
    </li>`;
  }).join('');

  let footer = '';
  if (state.listLoading) footer = '<div class="muted pad center">Načítám…</div>';
  else if (state.hasMore) footer = '<button class="btn wide" id="moreBtn">Načíst další</button>';
  else if (!state.messages.length && state.accounts.length && !state.listErrors.length)
    footer = `<div class="muted pad center">${state.query ? 'Nic nenalezeno.' : 'Žádné zprávy.'}</div>`;
  $('#listFooter').innerHTML = footer;
}

$('#messages').addEventListener('click', e => {
  const li = e.target.closest('li[data-i]');
  if (li) openMessage(state.messages[+li.dataset.i]);
});
$('#listFooter').addEventListener('click', e => {
  if (e.target.id === 'moreBtn') { state.page++; loadList({ append: true }); }
});
$('#refreshBtn').addEventListener('click', () => refreshAll());
$('#searchForm').addEventListener('submit', e => {
  e.preventDefault();
  state.query = $('#searchInput').value.trim();
  loadList();
});
$('#searchInput').addEventListener('search', () => {  // křížek v poli hledání
  if (!$('#searchInput').value && state.query) { state.query = ''; loadList(); }
});

function adjustUnseen(accountId, folder, delta) {
  const f = (state.folders[accountId] || []).find(x => x.name === folder);
  if (f) f.unseen = Math.max(0, (f.unseen || 0) + delta);
  renderSidebar();
}

// ------------------------------------------------------------------ čtení zprávy

async function openMessage(m) {
  // item = položka ze seznamu: stačí pro většinu akcí, takže jsou dostupné hned (obsah se načítá na pozadí)
  state.current = { accountId: m.account_id, folder: m.folder, uid: m.uid, data: null, item: m, loading: null };
  state.showImages = false;
  renderList();
  updateActions();
  clearContext('Načítám…');
  showReaderPlaceholder('<div class="empty">Načítám zprávu…</div>');
  const cur = state.current;
  cur.loading = api(`/api/accounts/${m.account_id}/messages/${m.uid}?folder=${enc(m.folder)}`);
  try {
    const data = await cur.loading;
    if (cur.snoozedUntil !== undefined) data.snoozed_until = cur.snoozedUntil;   // odloženo během načítání
    if (cur.notification !== undefined) data.notification = cur.notification;   // přepnuto během načítání
    cur.data = data;
    if (state.current !== cur) return;
    if (!m.seen) { m.seen = true; adjustUnseen(m.account_id, m.folder, -1); renderList(); }
    renderReader();
    updateActions();
    loadContext();
  } catch (e) {
    if (state.current !== cur) return;
    showReaderPlaceholder(`<div class="empty error">${esc(e.message)}</div>`);
    clearContext('');
  }
}

/** Čtecí část bez zprávy (načítání, chyba, nic nevybráno) – společné záhlaví se skryje. */
function showReaderPlaceholder(html) {
  $('#messageHead').hidden = true;
  $('#messageHead').innerHTML = '';
  $('#reader').innerHTML = html;
}

function clearCurrent(text = 'Vyberte zprávu') {
  state.current = null;
  showReaderPlaceholder(`<div class="empty">${esc(text)}</div>`);
  clearContext();
  updateActions();
  renderList();
}

function hasRemoteContent(html) {
  return /(<img[^>]+src\s*=\s*["']?\s*(https?:)?\/\/)|url\(\s*["']?\s*(https?:)?\/\//i.test(html);
}

function buildFrameDoc(html, allowRemote) {
  const remote = allowRemote ? ' https: http:' : '';
  const csp = `default-src 'none'; img-src data:${remote}; style-src 'unsafe-inline'${remote}; font-src data:${remote}`;
  return `<!doctype html><html><head><meta charset="utf-8">
    <meta http-equiv="Content-Security-Policy" content="${csp}">
    <base target="_blank">
    <style>body{font-family:system-ui,sans-serif;font-size:14px;line-height:1.5;margin:16px;color:#111;background:#fff;word-wrap:break-word}
    img{max-width:100%;height:auto}</style></head><body>${html}</body></html>`;
}

function renderReader() {
  const { data, accountId } = state.current;
  const acc = accById(accountId);
  const line = (label, list) => (list && list.length
    ? `<div><span class="muted">${label}:</span> ${list.map(a => esc(a.name ? `${a.name} <${a.addr}>` : a.addr)).join(', ')}</div>` : '');
  const atts = data.attachments.map(a =>
    `<a class="chip" data-index="${a.index}" href="/api/accounts/${accountId}/messages/${data.uid}/attachments/${a.index}?folder=${enc(data.folder)}"
        download="${esc(a.filename)}">📎 ${esc(a.filename)} <span class="muted">${fmtSize(a.size)}</span></a>`).join('');
  const remote = data.html && hasRemoteContent(data.html);
  const role = folderRole(accountId, data.folder);
  const folderNote = role && role !== 'inbox'
    ? `<span class="tag folder ${role === 'junk' ? 'spam' : ''}">${esc((state.folders[accountId] || []).find(f => f.name === data.folder)?.display || data.folder)}</span>` : '';

  // záhlaví je společné pro obsah, akce i kontext
  $('#messageHead').hidden = false;
  $('#messageHead').innerHTML = `
      <h2>${esc(data.subject)}</h2>
      <div class="meta">
        ${line('Od', data.from)}${line('Komu', data.to)}${line('Kopie', data.cc)}
        <div><span class="muted">${fmtFull(data.date)}</span>
          <span class="acc-tag" style="--c:${accColor(accountId)}">${esc(acc?.name || '')}</span> ${folderNote}</div>
      </div>
      ${atts ? `<div class="chips">${atts}</div>` : ''}
      ${data.snoozed_until ? `<div class="banner snooze">⏰ Odloženo – ${esc(fmtSnooze(data.snoozed_until))} se zpráva znovu
          označí jako nepřečtená. <button class="link" data-act="unsnooze">Zrušit odložení</button></div>` : ''}
      ${remote && !state.showImages ? `<div class="banner">Vzdálené obrázky byly zablokovány kvůli soukromí.
          <button class="link" data-act="images">Zobrazit obrázky</button></div>` : ''}`;
  $('#reader').innerHTML = `
    ${data.html ? '<iframe class="body-frame" sandbox="allow-popups allow-popups-to-escape-sandbox"></iframe>'
                : '<div class="body-text"></div>'}`;
  if (data.html) $('#reader iframe').srcdoc = buildFrameDoc(data.html, state.showImages);
  else $('#reader .body-text').textContent = data.text || '(prázdná zpráva)';
}

// V desktopové verzi ukládáme přílohy přes nativní dialog „Uložit jako“.
const desktop = () => window.pywebview?.api;
$('#messageHead').addEventListener('click', async e => {
  if (e.target.closest('[data-act="images"]') && state.current?.data) {
    state.showImages = true;
    return renderReader();
  }
  if (e.target.closest('[data-act="unsnooze"]') && state.current?.data) return unsnooze(state.current);
  const chip = e.target.closest('a.chip[data-index]');
  if (!chip || !desktop() || !state.current?.data) return;
  e.preventDefault();
  const { accountId, data } = state.current;
  const res = await desktop().save_attachment(accountId, data.folder, data.uid, +chip.dataset.index);
  if (res?.error) toast(res.error, true);
  else if (res?.ok) toast('Uloženo: ' + res.path);
});

// ------------------------------------------------------------------ sloupec Akce

function updateActions() {
  const ready = !!state.current;
  document.querySelectorAll('#actions [data-act]').forEach(b => { b.disabled = !ready; });
  const nb = $('#actions [data-act="notification"]');
  const isNotif = !!state.current && isNotification(state.current);
  nb.classList.toggle('active', isNotif);
  nb.querySelector('.label').textContent = isNotif ? 'Není notifikace' : 'Notifikace';
  nb.title = isNotif ? 'Odesílatel přestane být zdrojem notifikací (jeho zprávy se vrátí do Důležité)'
                     : 'Označí tuto i všechny ostatní zprávy od stejného odesílatele jako notifikace';
}

const MOVE_DONE = { archive: 'Zpráva archivována', spam: 'Zpráva přesunuta do spamu', delete: 'Zpráva přesunuta do koše' };

/** Je odesílatel zprávy zdroj notifikací? (z načtené zprávy, jinak z položky seznamu) */
function isNotification(cur) {
  if (cur.notification !== undefined) return cur.notification;
  return !!(cur.data ? cur.data.notification : cur.item?.notification);
}

/** Odpověď/přeposlání potřebují obsah zprávy – počkáme na jeho načtení. */
async function withLoadedMessage(cur, fn) {
  if (!cur.data) {
    try { await cur.loading; } catch { return; }
    if (state.current !== cur || !cur.data) return;
  }
  fn();
}

async function doAction(act, btn) {
  const cur = state.current;
  if (!cur) return;
  if (act === 'replyAll') return withLoadedMessage(cur, () => reply(true));
  if (act === 'forward') return withLoadedMessage(cur, forward);
  if (act === 'notification') return toggleNotification(cur, btn);
  if (act === 'snoozeDay' || act === 'snoozeWeek') return snooze(cur, act === 'snoozeDay' ? 1 : 7, btn);
  if (!MOVE_DONE[act]) return;

  // Změna se v UI projeví okamžitě; server ji dostane na pozadí (při selhání přijde hlášení
  // a synchronizace zprávu vrátí do seznamu).
  removeAndAdvance(cur);
  toast(MOVE_DONE[act]);
  try {
    const res = await api(`/api/accounts/${cur.accountId}/messages/${cur.uid}/${act}`,
      { method: 'POST', body: { folder: cur.folder } });
    if (act === 'spam' && res.job_id) watchSpamJob(res.job_id, res.sender);
  } catch (err) {
    toast(err.message, true);
    loadList({ quiet: true });  // vrátit skutečný stav
  }
}

// ------------------------------------------------------------------ odložení

/** Čas probuzení: za `days` dní v 8:00 místního času. */
function snoozeTime(days) {
  const d = new Date();
  d.setDate(d.getDate() + days);
  d.setHours(8, 0, 0, 0);
  return d.getTime() / 1000;
}
const fmtSnooze = ts => new Date(ts * 1000).toLocaleString('cs-CZ',
  { weekday: 'short', day: 'numeric', month: 'numeric', hour: '2-digit', minute: '2-digit' });

const findListItem = cur => state.messages.find(m =>
  m.account_id === cur.accountId && m.folder === cur.folder && m.uid === cur.uid);

async function snooze(cur, days, btn) {
  const until = snoozeTime(days);
  // okamžitě v UI, server na pozadí
  cur.snoozedUntil = until;
  if (cur.data) cur.data.snoozed_until = until;
  const item = findListItem(cur);
  if (item) item.snoozed_until = until;
  toast('Odloženo do ' + fmtSnooze(until));
  // zpráva zůstává v seznamu (přečtená), pokračujeme na další
  const idx = item ? state.messages.indexOf(item) : -1;
  const next = idx >= 0 ? state.messages[idx + 1] : null;
  if (state.current !== cur) renderList();
  else if (next) openMessage(next);
  else { if (cur.data) renderReader(); renderList(); }
  try {
    await api(`/api/accounts/${cur.accountId}/messages/${cur.uid}/snooze`,
      { method: 'POST', body: { folder: cur.folder, until } });
  } catch (err) {
    toast(err.message, true);
    cur.snoozedUntil = null;
    if (cur.data) cur.data.snoozed_until = null;
    if (item) item.snoozed_until = null;
    if (state.current === cur && cur.data) renderReader();
    renderList();
  }
}

async function unsnooze(cur) {
  try {
    await api(`/api/accounts/${cur.accountId}/messages/${cur.uid}/unsnooze`,
      { method: 'POST', body: { folder: cur.folder } });
    cur.data.snoozed_until = null;
    const item = findListItem(cur);
    if (item) item.snoozed_until = null;
    if (state.current === cur) renderReader();
    renderList();
    toast('Odložení zrušeno');
  } catch (err) { toast(err.message, true); }
}

/** Odebere zprávu (a případně další zprávy splňující `alsoRemove`) ze seznamu a otevře následující. */
function removeAndAdvance(cur, alsoRemove = null) {
  const same = m => m.account_id === cur.accountId && m.folder === cur.folder && m.uid === cur.uid;
  const remove = m => same(m) || (alsoRemove && alsoRemove(m));
  const idx = state.messages.findIndex(same);
  const after = idx >= 0 ? state.messages.slice(idx + 1).find(m => !remove(m)) : null;
  const before = idx >= 0 ? state.messages.slice(0, idx).reverse().find(m => !remove(m)) : null;
  state.messages = state.messages.filter(m => !remove(m));
  if (state.current !== cur) return renderList();  // uživatel mezitím otevřel jinou zprávu
  const next = after || before;
  if (next) openMessage(next);
  else clearCurrent('Žádná další zpráva');
}

/** Odesílatel zprávy = zdroj notifikací (nebo už ne). Platí pro všechny jeho zprávy. */
async function toggleNotification(cur, btn) {
  const value = !isNotification(cur);
  btn.classList.add('busy');
  try {
    const res = await api(`/api/accounts/${cur.accountId}/messages/${cur.uid}/notification`,
      { method: 'POST', body: { folder: cur.folder, value } });
    cur.notification = value;
    if (cur.data) cur.data.notification = value;
    toast(value ? `Zprávy od ${res.sender} se teď řadí do Notifikací` : `Zprávy od ${res.sender} se vrátily do Důležité`);
    const v = state.view;
    const leavesView = v.type === 'unified'
      && ((value && v.filter === 'important') || (!value && v.filter === 'notifications'));
    if (leavesView) removeAndAdvance(cur, m => m.from.some(a => a.addr.toLowerCase() === res.sender));
    else updateActions();
    pollSync();  // počty v levém sloupci
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.classList.remove('busy');
  }
}

/** Sleduje hromadný přesun starších zpráv odesílatele do spamu (běží na serveru na pozadí). */
function watchSpamJob(jobId, sender) {
  const timer = setInterval(async () => {
    let job;
    try { job = await api('/api/jobs/' + jobId); } catch { clearInterval(timer); return; }
    if (job.status !== 'done') return;
    clearInterval(timer);
    if (job.errors.length) toast('Hromadný přesun do spamu: ' + job.errors.join('; '), true);
    else if (job.moved) toast(`Do spamu přesunuto dalších ${job.moved} zpráv od ${sender}`);
    if (job.moved) {
      loadList({ quiet: true });
      state.accounts.forEach(a => loadFolders(a.id));
    }
  }, 1500);
}

$('#actions').addEventListener('click', e => {
  const btn = e.target.closest('[data-act]');
  if (btn && !btn.disabled) doAction(btn.dataset.act, btn);
});

// Klávesa Delete smaže zobrazenou zprávu – ale ne při psaní (dialogy, textová pole).
document.addEventListener('keydown', e => {
  if (e.key !== 'Delete' || e.repeat || e.ctrlKey || e.altKey || e.metaKey || e.shiftKey) return;
  if (document.querySelector('dialog[open]')) return;
  const el = document.activeElement;
  if (el && (el.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName))) return;
  const btn = $('#actions [data-act="delete"]');
  if (!state.current || btn.disabled || btn.classList.contains('busy')) return;
  e.preventDefault();
  doAction('delete', btn);
});

// ------------------------------------------------------------------ sloupec Kontext

const REASONS = { sender: 'odesílatel', mine: 'ode mě', cc: 'v kopii', subject: 'předmět' };
let ctxToken = 0;

function clearContext(text = 'Vyberte zprávu') {
  ctxToken++;
  state.context = [];
  $('#contextCount').textContent = '';
  $('#contextBody').innerHTML = text ? `<div class="empty small">${esc(text)}</div>` : '';
}

async function loadContext() {
  const cur = state.current;
  const token = ++ctxToken;
  $('#contextBody').innerHTML = '<div class="empty small">Hledám související zprávy…</div>';
  try {
    const res = await api(`/api/accounts/${cur.accountId}/messages/${cur.uid}/context?folder=${enc(cur.folder)}`);
    if (token !== ctxToken || res.stale) return;
    state.context = res.messages;
    renderContext();
  } catch (e) {
    if (token === ctxToken) $('#contextBody').innerHTML = `<div class="empty small error">${esc(e.message)}</div>`;
  }
}

function renderContext() {
  const items = state.context;
  $('#contextCount').textContent = items.length ? `(${items.length})` : '';
  if (!items.length) {
    $('#contextBody').innerHTML = '<div class="empty small">Žádné související zprávy.</div>';
    return;
  }
  $('#contextBody').innerHTML = items.map((m, i) => {
    const me = (accById(m.account_id)?.email || '').toLowerCase();
    const fromMe = m.from[0]?.addr.toLowerCase() === me;
    const who = fromMe ? `Já → ${shortAddr(m.to)}` : shortAddr(m.from);
    const tags = m.reasons.map(r => `<span class="tag">${REASONS[r] || r}</span>`);
    if (m.folder_role !== 'inbox')
      tags.unshift(`<span class="tag folder ${m.folder_role === 'junk' ? 'spam' : ''}">${esc(m.folder_display)}</span>`);
    return `<div class="ctx ${m.seen ? '' : 'unread'}" data-i="${i}" title="${esc(m.subject)}">
      <div class="ctx-row1"><span class="ctx-who">${esc(who)}</span><span class="ctx-date">${fmtDate(m.date)}</span></div>
      <div class="ctx-subject">${esc(m.subject)}</div>
      <div class="ctx-tags">${tags.join('')}</div>
    </div>`;
  }).join('');
}

$('#contextBody').addEventListener('click', e => {
  const el = e.target.closest('.ctx[data-i]');
  if (el) openMessage(state.context[+el.dataset.i]);
});

// ------------------------------------------------------------------ psaní zpráv

function quote(d) {
  const who = d.from[0] ? (d.from[0].name || d.from[0].addr) : '';
  return `\n\nDne ${fmtFull(d.date)} ${who} napsal(a):\n` + (d.text || '').split('\n').map(l => '> ' + l).join('\n');
}

function reply(all) {
  const { data: d, accountId } = state.current;
  const me = (accById(accountId)?.email || '').toLowerCase();
  const fromMe = d.from[0]?.addr.toLowerCase() === me;
  const primary = d.reply_to.length ? d.reply_to : (fromMe ? d.to : d.from);
  const seen = new Set(primary.map(a => a.addr.toLowerCase()).concat(me));
  const cc = [];
  if (all) {
    for (const a of [...d.to, ...d.cc]) {
      const k = a.addr.toLowerCase();
      if (k && !seen.has(k)) { seen.add(k); cc.push(fmtAddr(a)); }
    }
  }
  openCompose({
    title: all ? 'Odpovědět všem' : 'Odpovědět',
    accountId, to: primary.map(fmtAddr).join(', '), cc: cc.join(', '),
    subject: /^(re|odp):/i.test(d.subject) ? d.subject : 'Re: ' + d.subject,
    body: quote(d),
    inReplyTo: d.message_id,
    references: [d.references, d.message_id].filter(Boolean).join(' '),
  });
}

function forward() {
  const { data: d, accountId, folder, uid } = state.current;
  const list = l => l.map(a => (a.name ? `${a.name} <${a.addr}>` : a.addr)).join(', ');
  openCompose({
    title: 'Přeposlat',
    accountId,
    subject: /^(fwd?|př):/i.test(d.subject) ? d.subject : 'Fwd: ' + d.subject,
    body: `\n\n---------- Přeposlaná zpráva ----------\nOd: ${list(d.from)}\nDatum: ${fmtFull(d.date)}\n` +
          `Předmět: ${d.subject}\nKomu: ${list(d.to)}\n${d.cc.length ? 'Kopie: ' + list(d.cc) + '\n' : ''}\n${d.text}`,
    forward: d.attachments.length ? { account_id: accountId, folder, uid, attachments: d.attachments.slice() } : null,
  });
}

function openCompose(opts = {}) {
  if (!state.accounts.length) { toast('Nejdřív přidejte emailový účet.', true); return openAccountDialog(); }
  const f = $('#composeForm');
  f.reset();
  const defaultAcc = opts.accountId || (state.view.type === 'folder' ? state.view.accountId : state.accounts[0].id);
  $('#fromSelect').innerHTML = state.accounts.map(a =>
    `<option value="${a.id}" ${a.id === defaultAcc ? 'selected' : ''}>${esc(a.display_name ? `${a.display_name} <${a.email}>` : a.email)}</option>`).join('');
  f.to.value = opts.to || '';
  f.cc.value = opts.cc || '';
  f.subject.value = opts.subject || '';
  f.body.value = opts.body || '';
  $('#composeTitle').textContent = opts.title || 'Nová zpráva';
  $('#composeError').textContent = '';
  state.compose = { inReplyTo: opts.inReplyTo || '', references: opts.references || '', files: [], forward: opts.forward || null };
  renderComposeAttachments();
  $('#composeDialog').showModal();
  (opts.to ? f.body : f.to).focus();
  if (opts.to) f.body.setSelectionRange(0, 0);
}

function renderComposeAttachments() {
  const c = state.compose;
  const fwd = (c.forward?.attachments || []).map((a, i) =>
    `<span class="chip">📎 ${esc(a.filename)} <button type="button" class="x" data-fwd="${i}">×</button></span>`);
  const files = c.files.map((file, i) =>
    `<span class="chip">📎 ${esc(file.name)} <span class="muted">${fmtSize(file.size)}</span>
       <button type="button" class="x" data-file="${i}">×</button></span>`);
  $('#composeAttachments').innerHTML = fwd.concat(files).join('');
}

$('#composeAttachments').addEventListener('click', e => {
  const b = e.target.closest('button.x');
  if (!b) return;
  if (b.dataset.fwd !== undefined) state.compose.forward.attachments.splice(+b.dataset.fwd, 1);
  else state.compose.files.splice(+b.dataset.file, 1);
  renderComposeAttachments();
});

$('#fileInput').addEventListener('change', e => {
  state.compose.files.push(...e.target.files);
  e.target.value = '';
  renderComposeAttachments();
});

const readBase64 = file => new Promise((resolve, reject) => {
  const r = new FileReader();
  r.onload = () => resolve(String(r.result).split(',', 2)[1] || '');
  r.onerror = () => reject(new Error('Soubor nelze načíst: ' + file.name));
  r.readAsDataURL(file);
});

$('#composeForm').addEventListener('submit', async e => {
  e.preventDefault();
  const f = e.target, c = state.compose, btn = $('#sendBtn');
  if (!f.to.value.trim() && !f.cc.value.trim() && !f.bcc.value.trim()) {
    $('#composeError').textContent = 'Zadejte alespoň jednoho adresáta.';
    return;
  }
  btn.disabled = true;
  btn.textContent = 'Odesílám…';
  $('#composeError').textContent = '';
  try {
    const attachments = await Promise.all(c.files.map(async file => ({
      filename: file.name, content_type: file.type || 'application/octet-stream', data: await readBase64(file),
    })));
    const fwd = c.forward && c.forward.attachments.length
      ? { account_id: c.forward.account_id, folder: c.forward.folder, uid: c.forward.uid,
          indexes: c.forward.attachments.map(a => a.index) } : null;
    const res = await api('/api/send', { method: 'POST', body: {
      account_id: f.account_id.value, to: f.to.value, cc: f.cc.value, bcc: f.bcc.value,
      subject: f.subject.value, body: f.body.value,
      in_reply_to: c.inReplyTo, references: c.references, attachments, forward: fwd,
    } });
    $('#composeDialog').close();
    toast(res.warning || 'Zpráva odeslána', !!res.warning);
    const v = state.view;
    if (v.type === 'folder' && v.accountId === f.account_id.value && folderRole(v.accountId, v.folder) === 'sent')
      loadList({ quiet: true });
  } catch (err) {
    $('#composeError').textContent = err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Odeslat';
  }
});

$('#composeBtn').addEventListener('click', () => openCompose());

// ------------------------------------------------------------------ nastavení účtů

function applyPreset(key) {
  const p = PRESETS[key], f = $('#accountForm');
  const editingOauth = accById(state.editingId)?.auth === 'oauth2';
  $('#presetNote').textContent = editingOauth ? '' : (p?.note || '');
  $('#googleBox').hidden = !(key === 'gmail' || editingOauth);
  if (!editingOauth) {
    const base = state.editingId ? '(ponechte prázdné pro zachování stávajícího)' : '*';
    $('#pwHint').textContent = key === 'gmail' ? base + ' – jen heslo aplikace, běžné heslo Google nefunguje' : base;
  }
  if (!p) return;
  [f.imap_host.value, f.imap_port.value, f.imap_security.value] = p.imap;
  [f.smtp_host.value, f.smtp_port.value, f.smtp_security.value] = p.smtp;
}

function openAccountDialog(id = null) {
  const f = $('#accountForm');
  f.reset();
  state.editingId = id;
  const acc = id ? accById(id) : null;
  $('#accountTitle').textContent = acc ? 'Upravit účet' : 'Přidat účet';
  $('#deleteAccountBtn').hidden = !acc;
  $('#deleteAccountBtn').textContent = 'Smazat účet';
  delete $('#deleteAccountBtn').dataset.confirm;
  $('#accountError').textContent = '';
  $('#presetNote').textContent = '';
  $('#pwHint').textContent = acc ? '(ponechte prázdné pro zachování stávajícího)' : '*';
  if (acc) for (const k of Object.keys(acc)) {
    const input = f.elements.namedItem(k);
    if (input && k !== 'preset' && k !== 'password') input.value = acc[k] ?? '';
  }
  // Přihlášení přes Google
  const oauth = acc?.auth === 'oauth2';
  stopGooglePolling();
  $('#passwordLabel').hidden = oauth;
  $('#googleConfig').hidden = true;
  $('#googleOrPassword').hidden = oauth;
  $('#googleBtnText').textContent = oauth ? 'Přihlásit znovu' : 'Přihlásit přes Google';
  $('#googleHint').textContent = oauth ? '' : 'bez hesla – adresa a servery se vyplní samy';
  setGoogleStatus(oauth ? 'Účet je připojen přes Google (bez hesla).' : '', oauth ? 'ok' : '');
  const isGmail = acc && (acc.imap_host === 'imap.gmail.com');
  if (isGmail) f.elements.namedItem('preset').value = 'gmail';
  $('#googleBox').hidden = !(oauth || isGmail);
  $('#accountDialog').showModal();
}

// ------------------------------------------------------------------ Google OAuth

let googlePoll = null;
function stopGooglePolling() { clearInterval(googlePoll); googlePoll = null; }
function setGoogleStatus(text, kind = '') {
  const el = $('#googleStatus');
  el.textContent = text;
  el.className = 'note' + (kind ? ' ' + kind : '');
}

async function googleLogin() {
  try {
    const cfg = await api('/api/google/config');
    if (!cfg.configured) {
      $('#googleConfig').hidden = false;
      $('#gClientId').focus();
      setGoogleStatus('');
      return;
    }
    const f = $('#accountForm');
    const val = n => f.elements.namedItem(n).value.trim();
    const { url, state: st } = await api('/api/oauth/start', { method: 'POST', body: {
      account_id: state.editingId, name: val('name'), display_name: val('display_name'), email: val('email'),
    } });
    let opened;
    if (desktop()) opened = await desktop().open_external(url);  // systémový prohlížeč
    else opened = !!window.open(url, '_blank');
    $('#googleStatus').innerHTML = 'Dokončete přihlášení v prohlížeči, který se právě otevřel…' +
      (opened ? '' : ` <a href="${esc(url)}" target="_blank" rel="noopener">Otevřít přihlášení</a>`);
    $('#googleStatus').className = 'note';
    stopGooglePolling();
    const started = Date.now();
    googlePoll = setInterval(async () => {
      if (Date.now() - started > 10 * 60 * 1000) { stopGooglePolling(); setGoogleStatus('Přihlášení vypršelo.', 'bad'); return; }
      let res;
      try { res = await api('/api/oauth/status?state=' + enc(st)); } catch { return; }
      if (res.status === 'pending') return;
      stopGooglePolling();
      if (res.status === 'error') { setGoogleStatus(res.error, 'bad'); return; }
      $('#accountDialog').close();
      toast('Účet Google připojen');
      delete state.folders[res.account_id];
      await loadAccounts();
      loadList({ quiet: true });
    }, 1500);
  } catch (err) {
    setGoogleStatus(err.message, 'bad');
  }
}

$('#googleLoginBtn').addEventListener('click', googleLogin);
$('#gSaveConfig').addEventListener('click', async () => {
  try {
    await api('/api/google/config', { method: 'POST', body: {
      client_id: $('#gClientId').value, client_secret: $('#gClientSecret').value } });
    $('#googleConfig').hidden = true;
    $('#gClientSecret').value = '';
    googleLogin();
  } catch (err) { setGoogleStatus(err.message, 'bad'); }
});
$('#accountDialog').addEventListener('close', stopGooglePolling);

$('#presetSelect').addEventListener('change', e => applyPreset(e.target.value));
$('#accountForm').email.addEventListener('change', e => {
  const f = $('#accountForm');
  const domain = e.target.value.split('@')[1]?.toLowerCase();
  const key = Object.keys(PRESETS).find(k => PRESETS[k].domains.includes(domain));
  if (key && !f.imap_host.value) { f.preset.value = key; applyPreset(key); }
});

$('#accountForm').addEventListener('submit', async e => {
  e.preventDefault();
  const f = e.target, btn = $('#saveAccountBtn');
  const body = Object.fromEntries(new FormData(f));
  delete body.preset;
  btn.disabled = true;
  btn.textContent = 'Ověřuji připojení…';
  $('#accountError').textContent = '';
  try {
    const id = state.editingId;
    const res = id ? await api(`/api/accounts/${id}`, { method: 'PUT', body })
                   : await api('/api/accounts', { method: 'POST', body });
    $('#accountDialog').close();
    toast(id ? 'Účet uložen' : 'Účet přidán');
    delete state.folders[res.id];
    await loadAccounts();
    if (!id && state.accounts.length === 1) setView({ type: 'unified', filter: 'important' }); else loadList({ quiet: true });
  } catch (err) {
    $('#accountError').textContent = err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Uložit';
  }
});

$('#deleteAccountBtn').addEventListener('click', async e => {
  const btn = e.currentTarget;
  if (!btn.dataset.confirm) {  // dvoukrokové potvrzení
    btn.dataset.confirm = '1';
    btn.textContent = 'Opravdu smazat?';
    return;
  }
  const id = state.editingId;
  try {
    await api(`/api/accounts/${id}`, { method: 'DELETE' });
    $('#accountDialog').close();
    delete state.folders[id];
    if (state.current?.accountId === id) clearCurrent();
    await loadAccounts();
    setView({ type: 'unified', filter: 'important' });
    toast('Účet odstraněn (zprávy na serveru zůstávají)');
  } catch (err) { $('#accountError').textContent = err.message; }
});

$('#addAccountBtn').addEventListener('click', () => openAccountDialog());

// ------------------------------------------------------------------ nastavení

const RULE_KINDS = { sender: 'Odesílatel' };

let settingsSection = 'spam';

/** Přepne téma v levém sloupci nastavení (prostřední a pravý sloupec ukážou jeho obsah). */
function showSettingsSection(section) {
  settingsSection = section;
  const dlg = $('#settingsDialog');
  dlg.querySelectorAll('[data-section]').forEach(b => b.classList.toggle('active', b.dataset.section === section));
  dlg.querySelectorAll('[data-panel]').forEach(el => { el.hidden = el.dataset.panel !== section; });
  // pravý sloupec jen pokud téma má pravidla
  const hasRules = !!dlg.querySelector(`.settings-rules [data-panel="${section}"]`);
  dlg.querySelector('.settings-body').classList.toggle('no-rules', !hasRules);
}

async function openSettings() {
  $('#spamRules').innerHTML = $('#notifyRules').innerHTML = '<li class="empty-rules">Načítám…</li>';
  showSettingsSection(settingsSection);
  renderSettingOptions();
  $('#settingsDialog').showModal();
  await Promise.all([renderSpamRules(), renderNotifyRules(), loadSettings().then(renderSettingOptions)]);
}

$('#settingsDialog .settings-nav').addEventListener('click', e => {
  const b = e.target.closest('[data-section]');
  if (b) showSettingsSection(b.dataset.section);
});
// klik mimo dialog (na ztmavené pozadí) nastavení zavře
$('#settingsDialog').addEventListener('click', e => {
  if (e.target === e.currentTarget) e.currentTarget.close();
});

async function loadSettings() {
  try { state.settings = await api('/api/settings'); } catch { /* zůstanou poslední známé */ }
}

function renderSettingOptions() {
  document.querySelectorAll('[data-setting]').forEach(cb => {
    cb.checked = !!state.settings?.[cb.dataset.setting];
    cb.disabled = !state.settings;
  });
}

document.querySelectorAll('[data-setting]').forEach(cb => cb.addEventListener('change', async () => {
  try {
    state.settings = await api('/api/settings', { method: 'PUT', body: { [cb.dataset.setting]: cb.checked } });
  } catch (err) {
    toast(err.message, true);
  }
  renderSettingOptions();
}));

async function renderNotifyRules() {
  let rules;
  try { rules = await api('/api/notification-rules'); } catch (e) {
    $('#notifyRules').innerHTML = `<li class="empty-rules error">${esc(e.message)}</li>`;
    return;
  }
  $('#notifyRulesCount').textContent = rules.length ? `(${rules.length})` : '';
  $('#notifyRules').innerHTML = rules.length ? rules.map(r => `
    <li class="rule">
      <div class="rule-main">
        <div class="rule-value" title="${esc(r.value)}">${esc(r.value)}</div>
        <div class="rule-meta">${RULE_KINDS[r.kind] || r.kind} · přidáno ${new Date(r.created * 1000).toLocaleDateString('cs-CZ')}
          · ${r.messages} ${r.messages === 1 ? 'zpráva' : (r.messages >= 2 && r.messages <= 4 ? 'zprávy' : 'zpráv')} v doručené poště</div>
      </div>
      <button type="button" class="btn danger" data-notify-rule="${r.id}" data-value="${esc(r.value)}" title="Smazat pravidlo">Smazat</button>
    </li>`).join('')
    : '<li class="empty-rules">Zatím žádná pravidla. Vzniknou tlačítkem Notifikace u zprávy.</li>';
}

$('#notifyRules').addEventListener('click', async e => {
  const btn = e.target.closest('[data-notify-rule]');
  if (!btn) return;
  btn.disabled = true;
  try {
    await api('/api/notification-rules/' + btn.dataset.notifyRule, { method: 'DELETE' });
    toast('Pravidlo smazáno – zprávy se vrátily do Důležité');
    const cur = state.current;
    const from = cur ? (cur.data || cur.item)?.from || [] : [];
    if (from.some(a => a.addr.toLowerCase() === btn.dataset.value)) {
      cur.notification = false;
      if (cur.data) cur.data.notification = false;
      updateActions();
    }
    await renderNotifyRules();
    if (state.view.type === 'unified') loadList({ quiet: true });
    pollSync();
  } catch (err) { toast(err.message, true); btn.disabled = false; }
});

async function renderSpamRules() {
  let rules;
  try { rules = await api('/api/spam-rules'); } catch (e) {
    $('#spamRules').innerHTML = `<li class="empty-rules error">${esc(e.message)}</li>`;
    return;
  }
  $('#spamRulesCount').textContent = rules.length ? `(${rules.length})` : '';
  $('#spamRules').innerHTML = rules.length ? rules.map(r => `
    <li class="rule">
      <div class="rule-main">
        <div class="rule-value" title="${esc(r.value)}">${esc(r.value)}</div>
        <div class="rule-meta">${RULE_KINDS[r.kind] || r.kind} · přidáno ${new Date(r.created * 1000).toLocaleDateString('cs-CZ')}
          · zachyceno ${r.hits}×</div>
      </div>
      <button type="button" class="btn danger" data-rule="${r.id}" title="Smazat pravidlo">Smazat</button>
    </li>`).join('')
    : '<li class="empty-rules">Zatím žádná pravidla. Vzniknou, když zprávu označíte jako Spam.</li>';
}

$('#spamRules').addEventListener('click', async e => {
  const btn = e.target.closest('[data-rule]');
  if (!btn) return;
  btn.disabled = true;
  try {
    await api('/api/spam-rules/' + btn.dataset.rule, { method: 'DELETE' });
    toast('Pravidlo smazáno');
    await renderSpamRules();
  } catch (err) { toast(err.message, true); btn.disabled = false; }
});

$('#settingsBtn').addEventListener('click', openSettings);
document.querySelectorAll('dialog [data-close]').forEach(b =>
  b.addEventListener('click', () => b.closest('dialog').close()));

// ------------------------------------------------------------------ start

async function refreshAll() {
  api('/api/sync', { method: 'POST' }).catch(() => {});  // synchronizace se serverem na pozadí
  await Promise.all([loadList({ quiet: true }), ...state.accounts.map(a => loadFolders(a.id))]);
}

/** Data se zobrazují z lokální databáze; synchronizace běží na pozadí. Každé 3 s se zeptáme,
 *  zda se databáze změnila (levný lokální dotaz), a případně překreslíme seznam a počty. */
let lastNoticeId = null;  // poslední zobrazené hlášení ze serveru
async function pollSync() {
  if (document.hidden) return;
  let st;
  try { st = await api('/api/sync/status'); } catch { return; }
  let countsChanged = false;
  for (const a of state.accounts) {
    const inbox = (state.folders[a.id] || []).find(f => f.role === 'inbox');
    const n = st.unread[a.id] || 0;
    if (inbox && inbox.unseen !== n) { inbox.unseen = n; countsChanged = true; }
  }
  if (st.views && JSON.stringify(st.views) !== JSON.stringify(state.viewUnread)) {
    state.viewUnread = st.views;
    countsChanged = true;
  }
  // chyby připojení hlásí synchronizace na pozadí (vykřičník u účtu)
  for (const a of state.accounts) {
    const err = st.errors?.[a.id] || null;
    if ((state.folderErrors[a.id] || null) !== err) {
      if (err) state.folderErrors[a.id] = err; else delete state.folderErrors[a.id];
      countsChanged = true;
    }
  }
  if (countsChanged) renderSidebar();
  const notices = st.notices || [];
  const newest = notices.length ? notices[notices.length - 1][0] : 0;
  if (lastNoticeId !== null) notices.filter(([id]) => id > lastNoticeId).forEach(([, text]) => toast(text, true));
  lastNoticeId = Math.max(lastNoticeId ?? 0, newest);
  // Seznam vznikl z určité verze databáze; pokud se od té doby změnila, načteme ho znovu.
  if (!state.listLoading && state.listVersion != null && st.version !== state.listVersion
      && state.page === 0 && !state.query) loadList({ quiet: true });
}

(async () => {
  try {
    await loadAccounts();  // jen seznam účtů z lokálního úložiště – okamžité
    if (!state.accounts.length) openAccountDialog();
    loadList();            // zprávy z lokální databáze
    pollSync();            // počty nepřečtených hned, ne až za 3 s
    loadSettings();
  } catch (e) { toast(e.message, true); }
  setInterval(pollSync, 3000);
  // seznam složek (a případné chyby připojení) občas obnovíme ze serveru
  setInterval(() => { if (!document.hidden) state.accounts.forEach(a => loadFolders(a.id)); }, 300000);
})();
