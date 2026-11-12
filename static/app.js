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
  settings: null,         // nastavení aplikace (z /api/settings)
  userFolders: [],        // uživatelské složky (z /api/user-folders)
  editingFolder: null,    // id složky v dialogu konfigurace
  messages: [], page: 0, hasMore: false, query: '', listErrors: [], listLoading: false,
  unreadOnly: false,      // přepínač „Jen nepřečtené“ – platí jen do opuštění složky
  selected: new Set(),    // klíče vybraných zpráv (Ctrl/Shift + klik) – pro drag & drop a hromadné mazání
  anchor: null,           // klíč zprávy, od které se počítá výběr se Shiftem
  dragItems: null,        // právě přetahované zprávy
  trashTotal: 0,          // počet zpráv ve Smazaných (všechny, ne jen nepřečtené)
  spamUnread: 0,          // nepřečtené zprávy ve spamu (všechny účty)
  current: null,          // { accountId, folder, uid, data }
  context: [],            // související zprávy aktuální zprávy
  showImages: false,
  originalColors: false,  // otevřená HTML zpráva v tmavém režimu bez inverze barev („Zobrazit v původních barvách“)
  editingId: null,
  compose: null,          // { inReplyTo, references, files: [], forward }
};
let listToken = 0;

// Každá zpráva doručené pošty patří právě do jednoho z těchto pohledů (žádný souhrnný seznam navíc).
const UNIFIED_VIEWS = [['important', 'Důležité'], ['fyi', 'Vzít na vědomí'], ['unimportant', 'Nedůležité'],
                       ['notifications', 'Notifikace']];
const UNIFIED_TITLE = Object.fromEntries(UNIFIED_VIEWS);
// Ikona přílohy (sponka) v seznamu zpráv a v Kontextu
const CLIP_ICON = '<svg class="clip" viewBox="0 0 24 24" role="img" aria-label="Příloha"><title>Má přílohu</title>'
  + '<path d="M21.4 11.1l-9.2 9.2a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 0 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5"/></svg>';
// Kategorie, které určuje uživatel (tlačítka v Třídění, pravidla v Nastavení)
const CATEGORIES = {
  unimportant: { act: 'unimportant', title: 'Nedůležité', flag: 'unimportant', setting: 'unimp',
                 off: 'Není nedůležité', in: 'do Nedůležitých', from: 'z Nedůležitých' },
  notifications: { act: 'notification', title: 'Notifikace', flag: 'notification', setting: 'notify',
                   off: 'Není notifikace', in: 'do Notifikací', from: 'z Notifikací' },
};

// ------------------------------------------------------------------ utility

// Desktop: stránka běží na virtuální doméně WebView2 a Python volá přes most pywebview (žádný server).
// Vývoj (tools/dev_server.py): běžné HTTP požadavky na lokální server.
const DESKTOP = location.hostname === 'mailer-app.example';
const bridgeReady = new Promise(resolve => {
  if (window.pywebview?.api?.request) resolve();
  else window.addEventListener('pywebviewready', () => resolve(), { once: true });
});

async function api(path, { method = 'GET', body } = {}) {
  let r, data = null;
  if (DESKTOP) {
    await bridgeReady;
    const res = await window.pywebview.api.request(method, path, body === undefined ? null : body);
    r = { ok: res.status >= 200 && res.status < 300, status: res.status };
    data = res.data;
  } else {
    const opts = { method, headers: {} };
    if (method !== 'GET') opts.headers['X-Mailer'] = '1';
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    r = await fetch(path, opts);
    try { data = await r.json(); } catch { /* prázdná odpověď */ }
  }
  if (!r.ok) {
    const err = new Error(data?.error || `Chyba serveru (${r.status})`);
    err.status = r.status;
    err.data = data;
    throw err;
  }
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
const zprav = n => `${n} ${n === 1 ? 'zpráva' : n >= 2 && n <= 4 ? 'zprávy' : 'zpráv'}`;
const keyOf = m => `${m.account_id}|${m.folder}|${m.uid}`;
const curKey = cur => (cur ? `${cur.accountId}|${cur.folder}|${cur.uid}` : null);
const selectedItems = () => state.messages.filter(m => state.selected.has(keyOf(m)));
const SPAM_ICON = '<svg class="folder-ico" viewBox="0 0 24 24"><path d="M12 2l9 4v6c0 5-4 9-9 10-5-1-9-5-9-10V6zM12 8v5M12 16v.5"/></svg>';
const SENT_ICON = '<svg class="folder-ico" viewBox="0 0 24 24"><path d="M22 2L11 13M22 2l-7 20-4-9-9-4z"/></svg>';
const TRASH_ICON = '<svg class="folder-ico" viewBox="0 0 24 24"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14M10 10v7M14 10v7"/></svg>';

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
  let h = '';
  if (state.accounts.length) {
    const vu = state.viewUnread || {};
    for (const [filter, label] of UNIFIED_VIEWS) {
      const active = state.view.type === 'unified' && state.view.filter === filter;
      h += `<div class="nav-item unified ${active ? 'active' : ''}" data-unified="${filter}">
              <span class="label">${label}</span>${badge(vu[filter])}</div>`;
    }
    // Odeslané: odeslaná pošta všech účtů
    h += `<div class="nav-item unified sent-item ${state.view.type === 'sent' ? 'active' : ''}" data-sent
            title="Odeslané zprávy ze všech účtů">${SENT_ICON}<span class="label">Odeslané</span></div>`;
    // Spam: spam všech účtů (kdyby tam něco spadlo omylem); číslo = nepřečtené
    h += `<div class="nav-item unified spam-item ${state.view.type === 'spam' ? 'active' : ''}" data-spam
            title="Spam ze všech účtů">${SPAM_ICON}<span class="label">Spam</span>
            ${state.spamUnread ? `<span class="badge total">${state.spamUnread}</span>` : ''}</div>`;
    // Smazané: koše všech účtů; číslo = celkový počet zpráv (všechny jsou přečtené)
    h += `<div class="nav-item unified trash-item ${state.view.type === 'trash' ? 'active' : ''}" data-trash
            title="Smazané zprávy ze všech účtů">${TRASH_ICON}<span class="label">Smazané</span>
            ${state.trashTotal ? `<span class="badge total">${state.trashTotal}</span>` : ''}</div>`;
  } else {
    h += `<p class="muted pad">Zatím nemáte žádný účet. Začněte tlačítkem „Přidat účet“.</p>`;
  }
  if (state.accounts.length) {
    h += '<div class="nav-section">Složky</div>';
    for (const f of state.userFolders) {
      const active = state.view.type === 'ufolder' && state.view.id === f.id;
      const facc = accById(f.account_id);
      const tip = facc ? `${f.name} – ${facc.name || facc.email}: ${decodeFolderName(f.imap_name)}` : `${f.name} – nemá nastavený účet`;
      h += `<div class="nav-item ufolder-item ${active ? 'active' : ''}" data-ufolder="${f.id}" title="${esc(tip)}">
          <svg class="folder-ico" viewBox="0 0 24 24" style="color:${facc ? accColor(facc.id) : 'var(--danger)'}"><path d="M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>
          <span class="label">${esc(f.name)}</span>${facc ? badge(f.unread) : '<span class="warn-mark" title="Nastavte cílový účet">!</span>'}
          <button class="icon-btn small edit" data-edit-folder="${f.id}" title="Nastavení složky">⚙</button>
        </div>`;
    }
    h += '<div class="nav-item add-folder" data-add-folder>+ Přidat složku</div>';
  }
  if (state.dragItems) setTimeout(markDropCandidates, 0);  // překreslení během přetahování
  if (state.accounts.length) h += '<div class="nav-section">Účty</div>';
  for (const a of state.accounts) {
    // Účet není seznam zpráv (ty jsou v pohledech a složkách nahoře) – kliknutí otevře jeho nastavení.
    const error = state.folderErrors[a.id];
    h += `<div class="nav-item account-item" data-acc="${a.id}"
            title="${esc(error ? error : `${a.email} – nastavení účtu`)}">
        <span class="dot" style="background:${accColor(a.id)}"></span>
        <span class="label">${esc(a.name || a.email)}</span>
        ${error ? `<button class="icon-btn small warn" data-retry="${a.id}" title="Chyba: ${esc(error)} – kliknutím zkusit znovu">!</button>` : ''}
      </div>`;
  }
  $('#accounts').innerHTML = h;
}

$('#accounts').addEventListener('click', e => {
  const el = e.target.closest('[data-edit-folder],[data-add-folder],[data-ufolder],[data-retry],[data-trash],[data-spam],[data-sent],[data-unified],[data-acc]');
  if (!el) return;
  if (el.hasAttribute('data-trash')) return setView({ type: 'trash' });
  if (el.hasAttribute('data-sent')) return setView({ type: 'sent' });
  if (el.hasAttribute('data-spam')) return setView({ type: 'spam' });
  if (el.dataset.editFolder) return openFolderDialog(+el.dataset.editFolder);
  if (el.hasAttribute('data-add-folder')) return openFolderDialog(null);
  if (el.dataset.ufolder) {
    const f = state.userFolders.find(x => x.id === +el.dataset.ufolder);
    return setView({ type: 'ufolder', id: +el.dataset.ufolder, title: f?.name || 'Složka' });
  }
  if (el.dataset.retry) {
    api('/api/sync', { method: 'POST' }).catch(() => {});
    return loadFolders(el.dataset.retry);
  }
  if (el.dataset.unified) return setView({ type: 'unified', filter: el.dataset.unified });
  openAccountDialog(el.dataset.acc);
});

// --- kontextové menu položek levého panelu (pravé tlačítko myši)

/** Co položka panelu představuje: cíl pro „Označit vše jako přečtené“, počet nepřečtených a akce Nastavení. */
function navItemInfo(el) {
  if (el.hasAttribute('data-trash'))
    return { label: 'Smazané', target: null, unread: 0, readNote: 'Zprávy ve Smazaných jsou vždy přečtené',
             settings: () => openSettings('trash') };
  if (el.hasAttribute('data-sent'))
    return { label: 'Odeslané', target: null, unread: 0, readNote: 'Odeslané zprávy jsou vždy přečtené',
             settings: null, settingsNote: 'Tato složka nemá žádné nastavení' };
  if (el.hasAttribute('data-spam'))
    return { label: 'Spam', target: { type: 'spam' }, unread: state.spamUnread, settings: () => openSettings('spam') };
  if (el.dataset.unified) {
    const view = el.dataset.unified;
    return { label: UNIFIED_TITLE[view], target: { type: 'unified', view }, unread: state.viewUnread?.[view] || 0,
             settings: CATEGORIES[view] ? () => openSettings(view) : null,
             settingsNote: CATEGORIES[view] ? '' : 'Tento pohled nemá žádné nastavení' };
  }
  if (el.dataset.ufolder) {
    const f = state.userFolders.find(x => x.id === +el.dataset.ufolder);
    if (!f) return null;
    return { label: f.name, target: accById(f.account_id) ? { type: 'ufolder', id: f.id } : null, unread: f.unread || 0,
             settings: () => openFolderDialog(f.id) };
  }
  if (el.dataset.acc) {
    const a = accById(el.dataset.acc);
    const inbox = (state.folders[el.dataset.acc] || []).find(x => x.role === 'inbox');
    return { label: a?.name || a?.email || '', target: { type: 'account', account_id: el.dataset.acc },
             unread: inbox?.unseen || 0, settings: () => openAccountDialog(el.dataset.acc) };
  }
  return null;
}

let ctxActions = [];
function openContextMenu(x, y, items) {
  const menu = $('#ctxMenu');
  ctxActions = items.map(i => i.run);
  menu.innerHTML = items.map((i, n) =>
    `<button type="button" role="menuitem" data-ctx="${n}" ${i.disabled ? 'disabled' : ''} title="${esc(i.title || '')}">${esc(i.text)}</button>`).join('');
  menu.hidden = false;
  // v okně: menu nesmí přetéct přes pravý / dolní okraj
  const r = menu.getBoundingClientRect();
  menu.style.left = Math.min(x, innerWidth - r.width - 4) + 'px';
  menu.style.top = Math.min(y, innerHeight - r.height - 4) + 'px';
  menu.querySelector('button:not([disabled])')?.focus();
}
function closeContextMenu() { $('#ctxMenu').hidden = true; }

$('#accounts').addEventListener('contextmenu', e => {
  const el = e.target.closest('.nav-item');
  const info = el && navItemInfo(el);
  if (!info) return;
  e.preventDefault();
  openContextMenu(e.clientX, e.clientY, [
    { text: 'Označit vše jako přečtené', disabled: !info.target || !info.unread,
      title: !info.target ? (info.readNote || 'Složka nemá nastavený účet') : (!info.unread ? 'Žádné nepřečtené zprávy' : ''),
      run: () => markAllRead(info) },
    { text: 'Nastavení', disabled: !info.settings, title: info.settingsNote || '', run: info.settings },
  ]);
});
$('#ctxMenu').addEventListener('click', e => {
  const b = e.target.closest('[data-ctx]');
  if (!b || b.disabled) return;
  closeContextMenu();
  ctxActions[+b.dataset.ctx]?.();
});
document.addEventListener('mousedown', e => { if (!e.target.closest('#ctxMenu')) closeContextMenu(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeContextMenu(); });
window.addEventListener('blur', closeContextMenu);
window.addEventListener('resize', closeContextMenu);
$('#accounts').addEventListener('scroll', closeContextMenu, { passive: true });

/** Označit vše jako přečtené – v UI hned (seznam i počty), server na pozadí. */
async function markAllRead(info) {
  const t = info.target, v = state.view;
  const isCurrent = (t.type === 'unified' && v.type === 'unified' && v.filter === t.view)
    || (t.type === 'ufolder' && v.type === 'ufolder' && v.id === t.id)
    || (t.type === 'spam' && v.type === 'spam')
    || (t.type === 'account' && v.type === 'folder' && v.accountId === t.account_id && v.folder === 'INBOX');
  if (isCurrent) {  // zobrazený seznam hned
    state.messages.forEach(m => { m.seen = true; });
    renderList();
  }
  try {
    const res = await api('/api/mark-all-read', { method: 'POST', body: t });
    toast(res.count ? `${info.label}: označeno jako přečtené – ${zprav(res.count)}` : `${info.label}: nic nepřečteného`);
  } catch (err) { toast(err.message, true); }
  await pollSync();                                    // počty v levém panelu
  loadList({ quiet: true, keepLoaded: true });         // seznam z databáze (může se překrývat s cílem)
}

/** Po opuštění Notifikací / Nedůležitých: vše označit jako přečtené (počty hned, server na pozadí). */
function markCategoryRead(cat) {
  const vu = state.viewUnread;
  if (vu && vu[cat]) {
    state.viewUnread = { ...vu, all: Math.max(0, vu.all - vu[cat]), [cat]: 0 };
    renderSidebar();
  }
  api(`/api/categories/${cat}/mark-read`, { method: 'POST' })
    .then(() => pollSync())
    .catch(err => toast(`Označení složky ${CATEGORIES[cat].title} jako přečtené selhalo: ` + err.message, true));
}

const viewCategory = v => (v.type === 'unified' && CATEGORIES[v.filter]) ? v.filter : null;

/** reveal = otevřená zpráva, kterou má nový seznam obsahovat a nalistovat (pořadí od serveru). */
function setView(view, reveal = null) {
  const left = viewCategory(state.view);
  if (left && left !== viewCategory(view) && state.settings?.[`${CATEGORIES[left].setting}_mark_read_on_leave`])
    markCategoryRead(left);
  state.view = view;
  setUnreadOnly(false);
  state.selected = reveal ? new Set([reveal.key]) : new Set();
  state.anchor = reveal ? reveal.key : null;
  state.query = '';
  $('#searchInput').value = '';
  if (view.type !== 'search') { $('#globalSearchInput').value = ''; state.beforeSearch = null; }
  renderSidebar();
  return loadList({ reveal });
}

function setUnreadOnly(on) {
  state.unreadOnly = on;
  $('#unreadOnly').checked = on;
}
$('#unreadOnly').addEventListener('change', e => {
  state.unreadOnly = e.target.checked;
  loadList();
});

const sameView = (a, b) => a.type === b.type && a.filter === b.filter && a.id === b.id
  && a.accountId === b.accountId && a.folder === b.folder;

/** Zprávu otevřenou mimo seznam (z pravého sloupce) najde v levém sloupci: přepne na pohled, kde je,
 *  načte seznam aspoň po ni a odroluje na ni. */
async function revealInList(cur) {
  let res;
  try {
    res = await api(`/api/accounts/${cur.accountId}/messages/${cur.uid}/locate?folder=${enc(cur.folder)}`);
  } catch { return; }  // nenalezeno – seznam zůstane, jak je
  if (state.current !== cur || !res.view) return;
  const key = curKey(cur);
  if (sameView(res.view, state.view) && !state.query && state.messages.some(m => keyOf(m) === key))
    return scrollToActive();
  const reveal = { key, position: res.position ?? 0 };
  if (sameView(res.view, state.view)) {  // stejný pohled, jen zpráva ještě není načtená (nebo je zapnuté hledání)
    state.query = '';
    $('#searchInput').value = '';
    setUnreadOnly(false);
    await loadList({ reveal, quiet: true });
  } else await setView(res.view, reveal);
  if (state.current === cur && cur.data) markListedSeen(cur);
}

function markListedSeen(cur) {
  const listed = state.messages.find(m => keyOf(m) === curKey(cur));
  if (listed && !listed.seen) { listed.seen = true; renderList(); }
}

function scrollToActive() {
  $('#messages .msg.active')?.scrollIntoView({ block: 'center' });
}

// ------------------------------------------------------------------ seznam zpráv

async function loadList({ append = false, quiet = false, keepLoaded = false, reveal = null } = {}) {
  const token = ++listToken;
  const v = state.view;
  const loaded = state.messages.length;
  if (!append && !keepLoaded) state.page = 0;
  state.listLoading = true;
  if (!append && !quiet) { state.messages = []; state.listErrors = []; $('#messages').scrollTop = 0; }
  renderList();
  try {
    const qs = new URLSearchParams({ page: state.page });
    if (state.unreadOnly) {  // jen nepřečtené; právě otevřená zpráva v seznamu zůstane, i když je přečtená
      qs.set('unread', '1');
      const cur = state.current;
      if (cur) { qs.set('keep_acc', cur.accountId); qs.set('keep_folder', cur.folder); qs.set('keep_uid', cur.uid); }
    }
    if (state.query) qs.set('q', state.query);                    // filtr (z uložených zpráv)
    const local = state.messages.filter(m => !m.remote).length;   // dohledané ze serveru jsou až na konci
    if (append) qs.set('offset', local);                           // pokračovat za zobrazenými
    else if (reveal) { qs.set('offset', 0); qs.set('limit', reveal.position + 20); }  // rovnou po hledanou zprávu
    else if (keepLoaded && local) { qs.set('offset', 0); qs.set('limit', local); }  // obnovit vše zobrazené
    let res;
    if (v.type === 'search') {
      if (v.adv) for (const [k, val] of Object.entries(v.adv)) { if (val) qs.set(k, val); }
      else qs.set('q', v.q);
      res = await api('/api/search?' + qs);
    }
    else if (v.type === 'unified') { qs.set('filter', v.filter); res = await api('/api/unified?' + qs); }
    else if (v.type === 'ufolder') res = await api(`/api/user-folders/${v.id}/messages?${qs}`);
    else if (v.type === 'trash') res = await api('/api/trash?' + qs);
    else if (v.type === 'spam') res = await api('/api/spam?' + qs);
    else if (v.type === 'sent') res = await api('/api/sent?' + qs);
    else { qs.set('folder', v.folder); res = await api(`/api/accounts/${v.accountId}/messages?${qs}`); }
    if (token !== listToken) return;
    if (append) {  // další stránka patří před dohledané ze serveru (a bez těch, které už tam jsou)
      const remote = state.messages.filter(m => m.remote);
      const have = new Set(state.messages.map(keyOf));
      state.messages = state.messages.filter(m => !m.remote)
        .concat(res.messages.filter(m => !have.has(keyOf(m))), remote);
    } else state.messages = res.messages;
    if (v.type === 'trash' && !state.query && !state.unreadOnly && res.total != null && res.total !== state.trashTotal) {
      state.trashTotal = res.total;
      renderSidebar();
    }
    if (!append) pruneSelection();
    if (keepLoaded && !state.query && !state.messages.length && loaded) state.page = 0;
    state.hasMore = v.type === 'search' ? false : res.has_more;  // hledání: jedna dávka (nejnovější výsledky)
    state.searchTruncated = v.type === 'search' && res.has_more;
    state.loadMoreFailed = false;
    if (!append || keepLoaded) state.listVersion = res.version ?? null;
    state.listErrors = res.errors || [];
  } catch (e) {
    if (token !== listToken) return;
    if (append) { state.page--; state.loadMoreFailed = true; }  // tlačítko „Zkusit znovu“, ne smyčka
    else state.listErrors = [e.message];
  }
  state.listLoading = false;
  renderList();
  if (reveal) scrollToActive();
  setTimeout(maybeLoadMore, 0);  // seznam je pořád kratší než okno → načíst další hned
  if (!append && token === listToken) searchRemote();  // filtr / hledání: dohledat na serveru
}

// --- dohledání na serveru: zprávy, které lokální filtr nenajde (shoda v textu zprávy, nestažené zprávy)

let remoteToken = 0, remoteTimer = null;

/** Text, který se v aktuálním seznamu hledá (hledání ve veškeré poště, nebo filtr složky). */
const activeQuery = () => (state.view.type === 'search' ? state.view.q : state.query) || '';
/** Pokročilé hledání: kritéria jsou jen v hlavičkách, takže stačí uložené zprávy (bez dohledání na serveru). */
const isAdvSearch = () => state.view.type === 'search' && !!state.view.adv;

function remoteScope() {
  const v = state.view, qs = new URLSearchParams({ q: activeQuery() });
  if (v.type === 'search') qs.set('scope', 'all');
  else if (v.type === 'unified') { qs.set('scope', 'unified'); qs.set('filter', v.filter); }
  else if (v.type === 'ufolder') { qs.set('scope', 'ufolder'); qs.set('id', v.id); }
  else if (['trash', 'spam', 'sent'].includes(v.type)) qs.set('scope', v.type);
  else { qs.set('scope', 'folder'); qs.set('account', v.accountId); qs.set('folder', v.folder); }
  if (state.unreadOnly) qs.set('unread', '1');
  return qs;
}

function searchRemote() {
  const token = ++remoteToken;
  clearTimeout(remoteTimer);
  state.remoteStatus = null;
  if (activeQuery().length < 2) return;
  state.remoteStatus = 'searching';
  renderList();
  remoteTimer = setTimeout(async () => {  // až uživatel dopíše
    let res;
    try { res = await api('/api/search/remote?' + remoteScope()); } catch (e) {
      if (token !== remoteToken) return;
      state.remoteStatus = { error: e.message };
      return renderList();
    }
    if (token !== remoteToken || res.stale) return;
    const have = new Set(state.messages.map(keyOf));
    const ids = new Set(state.messages.map(m => m.message_id && `${m.account_id}|${m.message_id}`).filter(Boolean));
    const extra = res.messages.filter(m => !have.has(keyOf(m))
      && !(m.message_id && ids.has(`${m.account_id}|${m.message_id}`))).map(m => ({ ...m, remote: true }));
    state.messages = state.messages.concat(extra);
    state.remoteStatus = { found: extra.length, errors: res.errors || [] };
    renderList();
  }, 450);
}

/** Automatické načítání dalších zpráv, když se seznam posune blízko konce. */
function maybeLoadMore() {
  const el = $('#messages');
  if (!state.hasMore || state.listLoading || state.loadMoreFailed || !state.messages.length) return;
  if (el.scrollTop + el.clientHeight >= el.scrollHeight - 400) {
    state.page++;
    loadList({ append: true });
  }
}

function renderList() {
  const v = state.view;
  const acc = v.type === 'folder' ? accById(v.accountId) : null;
  const isSearch = v.type === 'search';
  const multi = ['unified', 'ufolder', 'trash', 'spam', 'sent', 'search'].includes(v.type);  // zprávy z více účtů
  $('#listTitle').textContent = isSearch ? (v.adv ? `Hledání: ${advSummary(v.adv)}` : `Hledání: „${v.q}“`) : v.type === 'unified' ? UNIFIED_TITLE[v.filter]
    : v.type === 'trash' ? 'Smazané' : v.type === 'spam' ? 'Spam' : v.type === 'sent' ? 'Odeslané' : v.type === 'ufolder' ? v.title
    : `${v.title || v.folder} · ${acc?.name || ''}`;
  $('#listTitle').title = $('#listTitle').textContent;
  $('#searchForm').hidden = isSearch;              // výsledky hledání se dál nefiltrují
  $('.unread-toggle').hidden = isSearch;
  $('#advSearchBtn').classList.toggle('on', isSearch && !!v.adv);
  renderTrashFooter();
  $('#listErrors').innerHTML = state.listErrors.map(e => `<div class="list-error">${esc(e)}</div>`).join('');

  const showTo = v.type === 'sent' || (v.type === 'folder' && ['sent', 'drafts'].includes(folderRole(v.accountId, v.folder)));
  const cur = state.current;
  const firstRemote = state.messages.findIndex(m => m.remote);
  $('#messages').innerHTML = state.messages.map((m, i) => {
    const sep = i === firstRemote
      ? '<li class="list-sep">Další nalezené na serveru (shoda v textu zprávy nebo zatím nestažené)</li>' : '';
    const active = cur && cur.accountId === m.account_id && cur.folder === m.folder && cur.uid === m.uid;
    const sel = state.selected.has(keyOf(m));
    return `${sep}<li class="msg ${m.seen ? '' : 'unread'} ${active ? 'active' : ''} ${sel ? 'selected' : ''}" data-i="${i}" draggable="true">
      ${multi ? `<span class="strip" style="background:${accColor(m.account_id)}"></span>` : ''}
      <div class="row1">
        <span class="from">${esc(showTo ? 'Komu: ' + shortAddr(m.to) : shortAddr(m.from))}</span>
        ${m.has_attachments ? CLIP_ICON : ''}
        <span class="date">${m.snoozed_until ? `<span title="Odloženo do ${esc(fmtSnooze(m.snoozed_until))}">⏰</span> ` : ''}${m.flagged ? '★ ' : ''}${fmtDate(m.date)}</span>
      </div>
      <div class="subject">${esc(m.subject)}</div>
      ${multi ? `<div class="acc-label">${esc(accById(m.account_id)?.name || '')}${isSearch && m.folder_label
        ? ` · <span class="folder-label">${esc(m.folder_label)}</span>` : ''}</div>` : ''}
    </li>`;
  }).join('') + listTail();
}

/** Smazané: dole počet zpráv a tlačítko Vyprázdnit. */
function renderTrashFooter() {
  const isTrash = state.view.type === 'trash';
  $('#listFooter').hidden = !isTrash;
  if (!isTrash) return;
  $('#trashInfo').textContent = state.trashTotal ? `Celkem ${zprav(state.trashTotal)}` : 'Složka je prázdná';
  $('#emptyTrashBtn').disabled = !state.trashTotal;
}

/** Poslední řádek seznamu: spinner při načítání, jinak stav (prázdno / chyba dalších zpráv / dohledávání). */
function listTail() {
  if (state.listLoading) return '<li class="list-tail"><span class="spinner" aria-label="Načítám"></span></li>';
  if (state.loadMoreFailed)
    return '<li class="list-tail">Další zprávy se nepodařilo načíst. <button class="link" id="retryMore">Zkusit znovu</button></li>';
  if (state.hasMore) return '';  // další stránky se načtou při rolování
  const rs = activeQuery() ? state.remoteStatus : null;
  if (rs === 'searching')
    return `<li class="list-tail"><span class="spinner small" aria-hidden="true"></span> ${state.messages.length
      ? 'Dohledávám další na serveru…' : 'V uložených zprávách nic – hledám na serveru…'}</li>`;
  const errs = rs?.errors?.length ? `<div class="error small">${rs.errors.map(esc).join('<br>')}</div>` : '';
  if (rs?.error) return `<li class="list-tail error">Dohledání na serveru selhalo: ${esc(rs.error)}</li>`;
  if (!state.messages.length && state.accounts.length && !state.listErrors.length)
    return `<li class="list-tail">${activeQuery() || isAdvSearch() ? 'Nic nenalezeno.' : state.unreadOnly ? 'Žádné nepřečtené zprávy.' : 'Žádné zprávy.'}${errs}</li>`;
  const more = state.searchTruncated ? '<div class="small">Zobrazeny jsou nejnovější výsledky – zpřesněte hledání.</div>' : '';
  if (rs && !rs.found) return `<li class="list-tail muted">Na serveru nic dalšího.${more}${errs}</li>`;
  if (more) return `<li class="list-tail muted">${more}</li>`;
  if (isAdvSearch() && state.messages.length)
    return '<li class="list-tail muted small">Hledáno ve stažených zprávách.</li>';
  return errs ? `<li class="list-tail">${errs}</li>` : '';
}

// Klik = otevřít zprávu; Ctrl+klik = přidat/odebrat z výběru; Shift+klik = výběr rozsahu.
$('#messages').addEventListener('click', e => {
  const li = e.target.closest('li[data-i]');
  if (!li) return;
  const i = +li.dataset.i, m = state.messages[i], k = keyOf(m);
  if (e.shiftKey && state.anchor) {
    const a = state.messages.findIndex(x => keyOf(x) === state.anchor);
    if (a >= 0) {
      const [from, to] = a < i ? [a, i] : [i, a];
      state.selected = new Set(state.messages.slice(from, to + 1).map(keyOf));
      return afterSelectionChange();
    }
  }
  if (e.ctrlKey || e.metaKey) {
    if (state.current && !state.selected.size) state.selected.add(curKey(state.current));
    if (state.selected.has(k)) state.selected.delete(k); else state.selected.add(k);
    state.anchor = k;
    return afterSelectionChange();
  }
  openMessage(m);
});

/** Po změně výběru: jedna zpráva = otevřít ji, více zpráv = souhrn místo obsahu. */
function afterSelectionChange() {
  const items = selectedItems();
  if (items.length === 1) {
    if (curKey(state.current) === keyOf(items[0])) return renderList();
    return openMessage(items[0]);
  }
  if (!items.length) return clearCurrent();
  state.current = null;
  showReaderPlaceholder(`<div class="empty"><b>Vybráno ${zprav(items.length)}</b><br>
    Přetáhněte je myší na složku v levém sloupci, nebo je smažte klávesou Delete.</div>`);
  clearContext('');
  updateActions();
  renderList();
}

/** Výběr po obnovení seznamu: zprávy, které už v seznamu nejsou, z něj vypadnou. */
function pruneSelection() {
  const before = state.selected.size;
  const present = new Set(state.messages.map(keyOf));
  state.selected = new Set([...state.selected].filter(k => present.has(k)));
  if (before > 1 && !state.current && state.selected.size !== before) afterSelectionChange();
}
$('#messages').addEventListener('scroll', maybeLoadMore, { passive: true });
$('#messages').addEventListener('click', e => {
  if (e.target.id !== 'retryMore') return;
  state.loadMoreFailed = false;
  maybeLoadMore();
  if (!state.listLoading) { state.page++; loadList({ append: true }); }
});
$('#refreshBtn').addEventListener('click', () => refreshAll());
// Filtr ve 2. sloupci: při psaní hned z uložených zpráv, pak dohledání na serveru.
let filterTimer = null;
function applyFilter() {
  clearTimeout(filterTimer);
  const q = $('#searchInput').value.trim();
  if (q === state.query) return;
  state.query = q;
  loadList({ quiet: true });
}
$('#searchForm').addEventListener('submit', e => { e.preventDefault(); applyFilter(); });
$('#searchInput').addEventListener('input', () => { clearTimeout(filterTimer); filterTimer = setTimeout(applyFilter, 200); });
$('#searchInput').addEventListener('keydown', e => {
  if (e.key === 'Escape' && e.target.value) { e.target.value = ''; applyFilter(); }
});

// Hledání ve veškeré poště (nad seznamem): vlastní seznam výsledků; prázdné pole = zpět, kde uživatel byl.
let globalTimer = null;
function applyGlobalSearch() {
  clearTimeout(globalTimer);
  const q = $('#globalSearchInput').value.trim();
  if (!q) {
    if (state.view.type === 'search') {
      const back = state.beforeSearch || { type: 'unified', filter: 'important' };
      state.beforeSearch = null;
      setView(back);
    }
    return;
  }
  startSearch({ type: 'search', q });
}

/** Zobrazí výsledky hledání; odkud uživatel přišel, si pamatuje pro návrat (prázdné pole hledání). */
function startSearch(view) {
  if (state.view.type === 'search') {
    if (JSON.stringify(state.view) === JSON.stringify(view)) return;
    state.view = view;
    return loadList({ quiet: true });
  }
  const prev = state.view;
  setView(view);
  state.beforeSearch = prev;
}
$('#globalSearch').addEventListener('submit', e => { e.preventDefault(); applyGlobalSearch(); });
$('#globalSearchInput').addEventListener('input', () => { clearTimeout(globalTimer); globalTimer = setTimeout(applyGlobalSearch, 250); });
$('#globalSearchInput').addEventListener('keydown', e => {
  if (e.key === 'Escape' && e.target.value) { e.target.value = ''; applyGlobalSearch(); }
});

// Pokročilé hledání: odesílatel, příjemce, předmět, příloha (všechna vyplněná kritéria zároveň).
const ADV_FIELDS = ['from', 'to', 'subject'];

function advSummary(a) {
  const parts = [];
  if (a.from) parts.push(`od „${a.from}“`);
  if (a.to) parts.push(`komu „${a.to}“`);
  if (a.subject) parts.push(`předmět „${a.subject}“`);
  if (a.att) parts.push(a.att === '1' ? 's přílohou' : 'bez přílohy');
  return parts.join(', ');
}

$('#advSearchBtn').addEventListener('click', () => {
  const f = $('#advSearchForm'), a = state.view.type === 'search' && state.view.adv || {};
  for (const k of ADV_FIELDS) f.elements[k].value = a[k] || '';
  f.elements.att.value = a.att || '';
  $('#advSearchError').textContent = '';
  if (!state.contacts.length) loadContacts();
  $('#advSearchDialog').showModal();
  f.elements.from.focus();
});
$('#advSearchClear').addEventListener('click', () => {
  const f = $('#advSearchForm');
  for (const k of ADV_FIELDS) f.elements[k].value = '';
  f.elements.att.value = '';
  $('#advSearchError').textContent = '';
  f.elements.from.focus();
});
$('#advSearchForm').addEventListener('submit', e => {
  e.preventDefault();
  const f = e.target, adv = {};
  for (const k of ADV_FIELDS) adv[k] = f.elements[k].value.trim().split(/\s+/).join(' ');
  adv.att = f.elements.att.value;
  if (!ADV_FIELDS.some(k => adv[k])) {
    $('#advSearchError').textContent = 'Vyplňte odesílatele, příjemce nebo předmět.';
    return;
  }
  $('#advSearchDialog').close();
  $('#globalSearchInput').value = '';
  startSearch({ type: 'search', q: '', adv });
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
  state.selected = new Set([keyOf(m)]);
  state.anchor = keyOf(m);
  state.showImages = false;
  state.originalColors = false;
  renderList();
  updateActions();
  clearContext('Načítám…');
  showReaderPlaceholder('<div class="empty">Načítám zprávu…</div>');
  const cur = state.current;
  cur.loading = api(`/api/accounts/${m.account_id}/messages/${m.uid}?folder=${enc(m.folder)}`);
  try {
    const data = await cur.loading;
    if (cur.snoozedUntil !== undefined) data.snoozed_until = cur.snoozedUntil;   // odloženo během načítání
    cur.data = data;
    if (state.current !== cur) return;
    if (!m.seen) { m.seen = true; adjustUnseen(m.account_id, m.folder, -1); renderList(); }
    markListedSeen(cur);  // zpráva otevřená z pravého sloupce může být v seznamu jako jiná položka
    renderReader();
    updateActions();
    loadContext();
  } catch (e) {
    if (e.data?.gone) {  // na serveru už není (např. automaticky smazaná notifikace) – zastaralý seznam
      state.messages = state.messages.filter(x => x !== m);
      if (state.current === cur) clearCurrent('Zpráva už neexistuje – byla smazána nebo přesunuta.');
      else renderList();
      loadList({ quiet: true, keepLoaded: true });
      return;
    }
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
  state.selected = new Set();
  showReaderPlaceholder(`<div class="empty">${esc(text)}</div>`);
  clearContext();
  updateActions();
  renderList();
}

function hasRemoteContent(html) {
  return /(<img[^>]+src\s*=\s*["']?\s*(https?:)?\/\/)|url\(\s*["']?\s*(https?:)?\/\//i.test(html);
}

// Značky, které ve zprávě nemají co dělat: načítají cizí obsah mimo CSP (dns-prefetch / preconnect),
// přesměrovávají (meta refresh), mění cíl odkazů (base) nebo by spouštěly kód.
const FRAME_DROP = 'link, meta, base, script, iframe, frame, frameset, object, embed, applet, portal, template';

// Tmavý režim HTML zpráv (řídí se nastavením Windows, stejně jako celá aplikace):
//  - native: zpráva má vlastní tmavou verzi (@media prefers-color-scheme) – jen ji povolíme,
//  - invert: ostatní zprávy se barevně převrátí a obrázky se převrátí zpět (jako Gmail / Outlook),
//  - light: světlý režim, nebo uživatel zvolil „Zobrazit v původních barvách“.
const darkQuery = matchMedia('(prefers-color-scheme: dark)');
const FRAME_BASE = 'body{font-family:system-ui,sans-serif;font-size:14px;line-height:1.5;margin:16px;word-wrap:break-word}'
  + 'img{max-width:100%;height:auto}';
const FRAME_THEMES = {
  light: ':root{color-scheme:light}body{color:#111;background:#fff}',
  native: ':root{color-scheme:light dark}body{color:#111;background:#fff}'
    + '@media (prefers-color-scheme:dark){body{color:#e3e7ee;background:#1c2027}a{color:#8ab4f8}}',
  // bílé pozadí → téměř černé; obrázky, videa a pozadí z obrázků se převrátí zpět do původních barev
  invert: ':root{color-scheme:light;filter:invert(1) hue-rotate(180deg)}body{color:#111;background:#e3e3e3}'
    + 'img,picture,video,canvas,svg image,[background],[style*="background-image"],[style*="url("]'
    + '{filter:invert(1) hue-rotate(180deg)}',
};

/** Jak zprávu v aktuálním režimu zobrazit: light / native / invert. */
function frameTheme(html) {
  if (!darkQuery.matches || state.originalColors) return 'light';
  return /prefers-color-scheme\s*:\s*dark/i.test(html) ? 'native' : 'invert';
}

/** HTML zprávy pro rámec (iframe srcdoc, bez skriptů). Rozebere se stejným parserem, který ho vykreslí
 *  (DOMParser – dokument je inertní, nic nenačítá), nebezpečné značky a atributy se odstraní a na začátek
 *  hlavičky se vloží vlastní CSP, vypnutí předběžných dotazů DNS, cíl odkazů a barvy podle režimu. */
function buildFrameDoc(html, allowRemote, theme = 'light') {
  const remote = allowRemote ? ' https: http:' : '';
  const csp = `default-src 'none'; img-src data:${remote}; style-src 'unsafe-inline'${remote}; font-src data:${remote}`;
  const doc = new DOMParser().parseFromString(html, 'text/html');
  doc.querySelectorAll(FRAME_DROP).forEach(el => el.remove());
  doc.querySelectorAll('form').forEach(form => form.replaceWith(...form.childNodes));  // obsah zůstane
  for (const el of doc.querySelectorAll('*')) {
    for (const { name } of [...el.attributes]) {
      if (name.startsWith('on') || name === 'ping' || name === 'formaction' || name === 'http-equiv') el.removeAttribute(name);
    }
  }
  const head = new DOMParser().parseFromString(`<!doctype html><html><head><meta charset="utf-8">
    <meta http-equiv="Content-Security-Policy" content="${csp}">
    <meta http-equiv="x-dns-prefetch-control" content="off">
    <base target="_blank">
    <style>${FRAME_BASE}${FRAME_THEMES[theme]}</style></head><body></body></html>`, 'text/html').head;
  doc.head.prepend(...[...head.childNodes].map(n => doc.importNode(n, true)));  // CSP musí být úplně první
  return '<!doctype html>' + doc.documentElement.outerHTML;
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
  const theme = data.html ? frameTheme(data.html) : 'light';
  const role = folderRole(accountId, data.folder);
  const folderNote = role && role !== 'inbox'
    ? `<span class="tag folder ${role === 'junk' ? 'spam' : ''}">${esc((state.folders[accountId] || []).find(f => f.name === data.folder)?.display || data.folder)}</span>` : '';
  const bayesNote = data.bayes ? `<span class="tag folder spam" title="${esc('Nejvýraznější znaky: '
      + data.bayes.tokens.map(t => `${t.token} (${Math.round(t.p * 100)} %)`).join(', '))}">Filtr spamu: ${Math.round(data.bayes.score * 100)} %</span>` : '';

  // záhlaví je společné pro obsah, akce i kontext
  $('#messageHead').hidden = false;
  $('#messageHead').innerHTML = `
      <h2>${esc(data.subject)}</h2>
      <div class="meta">
        ${line('Od', data.from)}${line('Komu', data.to)}${line('Kopie', data.cc)}
        <div><span class="muted">${fmtFull(data.date)}</span>
          <span class="acc-tag" style="--c:${accColor(accountId)}">${esc(acc?.name || '')}</span> ${folderNote} ${bayesNote}</div>
      </div>
      ${atts ? `<div class="chips">${atts}</div>` : ''}
      ${data.snoozed_until ? `<div class="banner snooze">⏰ Odloženo – ${esc(fmtSnooze(data.snoozed_until))} se zpráva znovu
          označí jako nepřečtená. <button class="link" data-act="unsnooze">Zrušit odložení</button></div>` : ''}
      ${remote && !state.showImages ? `<div class="banner">Vzdálené obrázky byly zablokovány kvůli soukromí.
          <button class="link" data-act="images">Zobrazit obrázky</button></div>` : ''}
      ${theme === 'invert' ? `<div class="colors-note muted">Barvy zprávy jsou upravené pro tmavý režim.
          <button class="link" data-act="original-colors">Zobrazit v původních barvách</button></div>`
        : data.html && state.originalColors && darkQuery.matches ? `<div class="colors-note muted">Zpráva je v původních barvách.
          <button class="link" data-act="dark-colors">Zobrazit tmavě</button></div>` : ''}`;
  $('#reader').innerHTML = `
    ${data.html ? `<iframe class="body-frame ${theme}" sandbox="allow-popups allow-popups-to-escape-sandbox"></iframe>`
                : '<div class="body-text"></div>'}`;
  if (data.html) $('#reader iframe').srcdoc = buildFrameDoc(data.html, state.showImages, theme);
  else $('#reader .body-text').textContent = data.text || '(prázdná zpráva)';
}

// Přepnutí světlého / tmavého režimu Windows za běhu: otevřenou HTML zprávu vykreslit v novém režimu.
darkQuery.addEventListener('change', () => { if (state.current?.data?.html) renderReader(); });

// V desktopové verzi ukládáme přílohy přes nativní dialog „Uložit jako“.
const desktop = () => window.pywebview?.api;
$('#messageHead').addEventListener('click', async e => {
  if (e.target.closest('[data-act="images"]') && state.current?.data) {
    state.showImages = true;
    return renderReader();
  }
  if (e.target.closest('[data-act="unsnooze"]') && state.current?.data) return unsnooze(state.current);
  for (const [act, value] of [['original-colors', true], ['dark-colors', false]]) {
    if (e.target.closest(`[data-act="${act}"]`) && state.current?.data) {
      state.originalColors = value;
      return renderReader();
    }
  }
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
  const multi = !ready && state.selected.size > 1;  // více vybraných zpráv – jde jen smazat a (ne)spam
  document.querySelectorAll('#actions [data-act]').forEach(b => {
    b.disabled = !(ready || (multi && ['delete', 'spam'].includes(b.dataset.act)));
  });
  const sb = $('#actions [data-act="spam"]');
  const inSpam = state.view.type === 'spam';
  sb.classList.toggle('active', inSpam);
  sb.querySelector('.label').textContent = inSpam ? 'Není spam' : 'Spam';
  sb.title = inSpam ? 'Vrátí zprávu do doručené pošty a odesílatel přestane být označený jako spam'
                    : 'Přesune zprávu do spamu; další zprávy od odesílatele půjdou rovnou do spamu';
  for (const [cat, c] of Object.entries(CATEGORIES)) {
    const b = $(`#actions [data-act="${c.act}"]`);
    const on = !!state.current && inCategory(state.current, cat);
    b.classList.toggle('active', on);
    b.querySelector('.label').textContent = on ? c.off : c.title;
    b.title = on ? `Vrátí zprávu do Důležité; zruší i pravidla, podle kterých se řadila ${c.in}`
                 : `Zařadí tuto zprávu (nebo všechny od odesílatele / se stejným předmětem) ${c.in}`;
  }
}

const MOVE_DONE = { archive: 'Zpráva archivována', spam: 'Zpráva přesunuta do spamu', delete: 'Zpráva přesunuta do Smazaných' };

/** Patří zpráva do kategorie? (přepnuto v této relaci, z načtené zprávy, jinak z položky seznamu) */
function inCategory(cur, cat) {
  if (cur.category !== undefined) return cur.category === cat;
  const flag = CATEGORIES[cat].flag;
  return !!(cur.data ? cur.data[flag] : cur.item?.[flag]);
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
  if (act === 'delete' && !cur && state.selected.size > 1) return moveItems(selectedItems(), { type: 'trash' });
  if (act === 'spam' && (state.view.type === 'spam' || (!cur && state.selected.size > 1))) {
    const items = !cur ? selectedItems() : [cur.item || { account_id: cur.accountId, folder: cur.folder, uid: cur.uid }];
    return moveItems(items, state.view.type === 'spam' ? { type: 'inbox', view: 'all' } : { type: 'spam' });
  }
  if (!cur) return;
  // ve Smazaných se maže definitivně – přes hromadnou cestu s potvrzením
  if (act === 'delete' && state.view.type === 'trash')
    return moveItems([cur.item || { account_id: cur.accountId, folder: cur.folder, uid: cur.uid }], { type: 'trash' });
  if (act === 'replyAll') return withLoadedMessage(cur, () => reply(true));
  if (act === 'forward') return withLoadedMessage(cur, forward);
  if (act === 'notification') return categorize(cur, 'notifications', btn);
  if (act === 'unimportant') return categorize(cur, 'unimportant', btn);
  if (act === 'rule') return openRuleDialog(cur);
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

// Předmět bez Re:/Fw:/Odp: apod. – stejně jako mail.base_subject (podle něj se pravidlo pozná)
const SUBJECT_PREFIX_RE = /^\s*(?:\[[^\]]{0,40}\]\s*)*(?:re|fw|fwd|odp|př|aw|wg|tr|sv|vs|rv|antw|ref|réf)\s*(?:\[\d+\]|\(\d+\))?\s*[:：]\s*/i;
function baseSubject(s) {
  for (let prev = null; prev !== s;) { prev = s; s = s.replace(SUBJECT_PREFIX_RE, ''); }
  return s.split(/\s+/).filter(Boolean).join(' ');
}

/** Dialog „Jen tento email / Vše od tohoto odesílatele / Vše s tímto subjektem“; vrací volbu nebo null. */
function askCategoryScope(cur, cat) {
  const m = cur.data || cur.item || {};
  const sender = m.from?.[0]?.addr || '';
  const subject = m.subject && m.subject !== '(bez předmětu)' ? baseSubject(m.subject) : '';
  $('#categoryTitle').textContent = CATEGORIES[cat].title;
  $('#categoryMessage').textContent = [sender, subject].filter(Boolean).join(' – ');
  $('#categorySender').disabled = !sender;
  $('#categorySenderNote').textContent = sender ? `pravidlo pro všechny zprávy od ${sender}` : 'zpráva nemá odesílatele';
  $('#categorySubject').disabled = !subject;
  $('#categorySubjectNote').textContent = subject ? `pravidlo pro všechny zprávy s předmětem „${subject}“`
                                                  : 'zpráva nemá předmět';
  const dlg = $('#categoryDialog');
  dlg.returnValue = '';
  dlg.showModal();
  dlg.querySelector('button[value="message"]').focus();
  return new Promise(resolve => dlg.addEventListener('close', () =>
    resolve(['message', 'sender', 'subject'].includes(dlg.returnValue) ? dlg.returnValue : null), { once: true }));
}

/** Nedůležité / Notifikace: zařadit (jen zprávu, nebo pravidlem) – nebo, je-li už zařazená, vyřadit. */
async function categorize(cur, cat, btn) {
  const value = !inCategory(cur, cat);
  const scope = value ? await askCategoryScope(cur, cat) : 'message';
  if (!scope || state.current !== cur) return;
  const c = CATEGORIES[cat];
  btn.classList.add('busy');
  try {
    const res = await api(`/api/accounts/${cur.accountId}/messages/${cur.uid}/categorize`,
      { method: 'POST', body: { folder: cur.folder, category: cat, scope, value } });
    // kam zpráva teď patří: odpověď serveru (uložená zpráva v doručené poště), jinak odhad
    const now = res.view || (value ? cat : null);
    cur.category = CATEGORIES[now] ? now : null;
    const other = value && CATEGORIES[now] && now !== cat ? CATEGORIES[now] : null;
    toast(!value ? `Zpráva vyřazena ${c.from}`
      : other ? `Pravidlo uloženo, ale zpráva zůstává ${other.title === 'Notifikace' ? 'v Notifikacích' : 'v Nedůležitých'} – `
                + `odpovídá i jejich pravidlu, které má přednost`
      : scope === 'message' ? `Zpráva zařazena ${c.in}`
      : scope === 'sender' ? `Zprávy od ${res.sender} se teď řadí ${c.in}`
      : `Zprávy s předmětem „${res.subject}“ se teď řadí ${c.in}`);
    const v = state.view;
    const leaves = v.type === 'unified' && cur.folder === 'INBOX' && (now ? now !== v.filter : v.filter === cat);
    if (leaves) {
      const also = !value ? null
        : scope === 'sender' ? m => m.from.some(a => a.addr.toLowerCase() === res.sender)
        : scope === 'subject' ? m => m.base_subject === res.subject : null;
      removeAndAdvance(cur, also);
    } else updateActions();
    if (v.type === 'unified') loadList({ quiet: true, keepLoaded: true });  // další zprávy podle pravidla
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
  if (btn.disabled || btn.classList.contains('busy')) return;  // nic vybraného
  e.preventDefault();
  doAction('delete', btn);
});

// Ctrl+A v seznamu vybere všechny načtené zprávy (ne při psaní).
document.addEventListener('keydown', e => {
  if (e.key.toLowerCase() !== 'a' || !(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey) return;
  if (document.querySelector('dialog[open]')) return;
  const el = document.activeElement;
  if (el && (el.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName))) return;
  if (!state.messages.length) return;
  e.preventDefault();
  state.selected = new Set(state.messages.map(keyOf));
  afterSelectionChange();
});

// ------------------------------------------------------------------ přesun více zpráv (drag & drop, Delete)

/** Dialog s upozorněním; vrací true, pokud uživatel potvrdí. */
function confirmDialog({ title = 'Upozornění', lines = [], question = '', ok = 'Pokračovat' }) {
  const dlg = $('#confirmDialog');
  $('#confirmTitle').textContent = title;
  $('#confirmLines').innerHTML = lines.map(l => `<li>${esc(l)}</li>`).join('');
  $('#confirmQuestion').textContent = question;
  $('#confirmOk').textContent = ok;
  dlg.returnValue = '';
  dlg.showModal();
  $('#confirmCancel').focus();
  return new Promise(resolve => dlg.addEventListener('close', () => resolve(dlg.returnValue === 'ok'), { once: true }));
}

/** Opustí zpráva aktuální pohled, když se přesune na cíl? (pro okamžité odebrání ze seznamu) */
function leavesView(m, target) {
  const v = state.view;
  if (target.type !== 'inbox') return true;                      // Smazané, složka
  if (target.account_id && target.account_id !== m.account_id) return v.type !== 'unified';
  if (m.folder !== 'INBOX') return true;                         // ze složky / Smazaných do doručené
  if (v.type === 'unified' && CATEGORIES[target.view]) return v.filter !== target.view;
  if (v.type === 'unified' && ['important', 'fyi'].includes(target.view)) return !!CATEGORIES[v.filter];
  return false;
}

/** Odebere zprávy ze seznamu; pokud mezi nimi byla otevřená (nebo vybrané), otevře další. */
function removeItems(items) {
  if (!items.length) return;
  const keys = new Set(items.map(keyOf));
  const cur = state.current;
  const idx = state.messages.findIndex(m => keys.has(keyOf(m)));
  const affectsOpen = cur ? keys.has(curKey(cur)) : [...state.selected].some(k => keys.has(k));
  state.messages = state.messages.filter(m => !keys.has(keyOf(m)));
  state.selected = new Set([...state.selected].filter(k => !keys.has(k)));
  if (!affectsOpen) return renderList();
  if (!cur && state.selected.size) return afterSelectionChange();
  const next = idx >= 0 ? (state.messages[idx] || state.messages[idx - 1]) : null;
  if (next) openMessage(next); else clearCurrent('Žádná další zpráva');
}

function targetLabel(target) {
  if (target.type === 'trash') return state.view.type === 'trash' ? 'Definitivně smazáno' : 'Přesunuto do Smazaných';
  if (target.type === 'spam') return 'Přesunuto do spamu';
  if (state.view.type === 'spam' && target.view === 'all' && !target.account_id) return 'Není spam – vráceno do doručené pošty';
  if (target.type === 'ufolder') return `Přesunuto do složky ${state.userFolders.find(f => f.id === target.id)?.name || ''}`;
  if (target.account_id) { const a = accById(target.account_id); return `Přesunuto do doručené pošty účtu ${a?.name || a?.email || ''}`; }
  return `Přesunuto do ${{ important: 'Důležité', fyi: 'Vzít na vědomí', unimportant: 'Nedůležitých', notifications: 'Notifikací',
                           all: 'doručené pošty' }[target.view]}`;
}

/** Přesune (nebo smaže) zprávy. Nejdřív se zeptá serveru, co by se s nimi hned stalo (smazání starých
 *  notifikací, spam, pravidla složek, definitivní smazání), a případně zobrazí upozornění. */
async function moveItems(items, target) {
  if (!items.length) return;
  const body = { target, items: items.map(m => ({ account_id: m.account_id, folder: m.folder, uid: m.uid,
    sender: m.from?.[0]?.addr || '', date: m.date || null, seen: !!m.seen })) };
  let preview;
  try { preview = await api('/api/messages/move/preview', { method: 'POST', body }); }
  catch (err) { return toast(err.message, true); }
  if (preview.warnings.length) {
    const permanent = target.type === 'trash';
    const ok = await confirmDialog({
      title: permanent ? 'Definitivní smazání' : 'Upozornění',
      lines: preview.warnings,
      question: permanent ? 'Opravdu zprávy definitivně smazat?' : `Chcete ${items.length === 1 ? 'zprávu' : 'zprávy'} přesto přesunout?`,
      ok: permanent ? 'Smazat definitivně' : 'Přesto přesunout',
    });
    if (!ok) return;
  }
  removeItems(items.filter(m => leavesView(m, target)));
  toast(`${targetLabel(target)}: ${zprav(items.length)}`);
  try {
    await api('/api/messages/move', { method: 'POST', body });
  } catch (err) { toast(err.message, true); }
  loadList({ quiet: true, keepLoaded: true });
  if (target.type === 'ufolder') loadUserFolders();
  pollSync();
}

// --- drag & drop na položky levého sloupce

/** Cíl přesunu pro položku levého sloupce (nebo null, pokud na ni přetahované zprávy pustit nejde). */
function dropTarget(el) {
  const items = state.dragItems, v = state.view;
  if (!el || !items?.length) return null;
  if (el.hasAttribute('data-trash'))
    return v.type === 'trash' ? null : { type: 'trash' };
  if (el.hasAttribute('data-spam'))
    return v.type === 'spam' ? null : { type: 'spam' };
  if (el.dataset.ufolder) {
    const f = state.userFolders.find(x => x.id === +el.dataset.ufolder);
    if (!f || !accById(f.account_id) || (v.type === 'ufolder' && v.id === f.id)) return null;
    return { type: 'ufolder', id: f.id };
  }
  if (el.dataset.unified) {
    const view = el.dataset.unified;
    if (v.type === 'unified' && v.filter === view) return null;
    return { type: 'inbox', view };
  }
  if (el.dataset.acc) {
    if (items.every(m => m.account_id === el.dataset.acc && m.folder === 'INBOX')) return null;
    return { type: 'inbox', account_id: el.dataset.acc };
  }
  return null;
}

function markDropCandidates() {
  document.querySelectorAll('#accounts .nav-item').forEach(el =>
    el.classList.toggle('drop-candidate', !!state.dragItems && !!dropTarget(el)));
}

function clearDropMarks() {
  document.querySelectorAll('#accounts .drop-over, #accounts .drop-candidate')
    .forEach(el => el.classList.remove('drop-over', 'drop-candidate'));
}

$('#messages').addEventListener('dragstart', e => {
  const li = e.target.closest('li[data-i]');
  if (!li) return;
  const m = state.messages[+li.dataset.i];
  const items = state.selected.has(keyOf(m)) ? selectedItems() : [m];  // vybraná skupina, jinak jen tahle
  state.dragItems = items;
  const label = items.length === 1 ? (m.subject || '(bez předmětu)') : zprav(items.length);
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', label);
  const ghost = $('#dragGhost');
  ghost.textContent = label;
  e.dataTransfer.setDragImage(ghost, 14, 14);
  markDropCandidates();
});

document.addEventListener('dragend', () => {  // i když se mezitím seznam překreslil
  state.dragItems = null;
  clearDropMarks();
});

$('#accounts').addEventListener('dragover', e => {
  const el = e.target.closest('.nav-item');
  const target = dropTarget(el);
  document.querySelectorAll('#accounts .drop-over').forEach(x => { if (x !== el) x.classList.remove('drop-over'); });
  if (!target) return;  // bez preventDefault = sem pustit nelze
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  el.classList.add('drop-over');
});

$('#accounts').addEventListener('dragleave', e => {
  if (!e.relatedTarget || !$('#accounts').contains(e.relatedTarget))
    document.querySelectorAll('#accounts .drop-over').forEach(x => x.classList.remove('drop-over'));
});

$('#accounts').addEventListener('drop', e => {
  const el = e.target.closest('.nav-item');
  const target = dropTarget(el);
  const items = state.dragItems;
  e.preventDefault();
  state.dragItems = null;
  clearDropMarks();
  if (target && items?.length) moveItems(items, target);
});

// --- Smazané: Vyprázdnit

$('#emptyTrashBtn').addEventListener('click', async () => {
  const n = state.trashTotal;
  const ok = await confirmDialog({
    title: 'Vyprázdnit Smazané',
    lines: [`${zprav(n)} ve složce Smazané (ze všech účtů) se natrvalo smaže ze serverů.`],
    question: 'Tuto akci nejde vrátit. Opravdu vyprázdnit?',
    ok: 'Vyprázdnit',
  });
  if (!ok) return;
  if (state.view.type === 'trash') { state.messages = []; state.hasMore = false; clearCurrent('Složka Smazané je prázdná'); }
  state.trashTotal = 0;
  renderSidebar();
  renderList();
  try {
    const res = await api('/api/trash/empty', { method: 'POST' });
    toast(`Smazané vyprázdněny – natrvalo smazáno: ${zprav(res.deleted)}`);
  } catch (err) { toast(err.message, true); }
  pollSync();
  if (state.view.type === 'trash') loadList({ quiet: true });
});

// ------------------------------------------------------------------ sloupec Kontext

const REASONS = { mine: 'ode mě', cc: 'v kopii' };
const HIDDEN_REASONS = ['sender', 'subject'];  // důvody, které se jako štítek nezobrazují
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
    const tags = m.reasons.filter(r => !HIDDEN_REASONS.includes(r)).map(r => `<span class="tag">${REASONS[r] || r}</span>`);
    if (m.folder_role !== 'inbox')
      tags.unshift(`<span class="tag folder ${m.folder_role === 'junk' ? 'spam' : ''}">${esc(m.folder_display)}</span>`);
    return `<div class="ctx ${m.seen ? '' : 'unread'}" data-i="${i}" title="${esc(m.subject)}">
      <div class="ctx-row1"><span class="ctx-who">${esc(who)}</span>${m.has_attachments ? CLIP_ICON : ''}<span class="ctx-date">${fmtDate(m.date)}</span></div>
      <div class="ctx-subject">${esc(m.subject)}</div>
      ${tags.length ? `<div class="ctx-tags">${tags.join('')}</div>` : ''}
    </div>`;
  }).join('');
}

$('#contextBody').addEventListener('click', e => {
  const el = e.target.closest('.ctx[data-i]');
  if (!el) return;
  openMessage(state.context[+el.dataset.i]);
  revealInList(state.current);  // a najít ji v levém sloupci
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
  loadContacts();
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
    if (v.type === 'sent' || (v.type === 'folder' && v.accountId === f.account_id.value
                              && folderRole(v.accountId, v.folder) === 'sent'))
      loadList({ quiet: true });
  } catch (err) {
    $('#composeError').textContent = err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Odeslat';
  }
});

$('#composeBtn').addEventListener('click', () => openCompose());

// ------------------------------------------------------------------ našeptávání adres (Komu / Kopie / Skrytá)

state.contacts = [];  // [{email, name, key}] seřazené od nejčastějších; key = text pro hledání
const foldText = t => t.toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '');

async function loadContacts() {
  try {
    const { contacts } = await api('/api/contacts');
    state.contacts = contacts.map(c => ({ ...c, words: foldText(`${c.name} ${c.email}`).split(/[\s.@_+\-"'<>(),]+/).filter(Boolean),
                                          name0: foldText(c.name), email0: c.email }));
  } catch { /* bez našeptávání */ }
}

/** Rozsah právě psané adresy v poli (text mezi čárkami / středníky kolem kurzoru). */
function addrToken(input) {
  const v = input.value, pos = input.selectionStart ?? v.length;
  const start = Math.max(v.lastIndexOf(',', pos - 1), v.lastIndexOf(';', pos - 1)) + 1;
  let end = v.slice(pos).search(/[,;]/);
  end = end < 0 ? v.length : pos + end;
  return { start, end, text: v.slice(start, end).trim() };
}

function contactMatches(q, input) {
  q = foldText(q);
  if (!q) return [];
  const used = new Set((input.value.match(/[^\s<>,;"']+@[^\s<>,;"']+/g) || []).map(a => a.toLowerCase()));
  const out = [];
  for (const c of state.contacts) {
    if (used.has(c.email)) continue;
    if (c.email0.startsWith(q) || c.name0.startsWith(q) || c.words.some(w => w.startsWith(q))) {
      out.push(c);
      if (out.length >= 8) break;
    }
  }
  return out;
}

/** Zvýrazní začátky slov, které odpovídají hledanému textu. */
function acHighlight(text, q) {
  const f = foldText(text), n = q.length;
  let html = '', i = 0;
  while (i < text.length) {
    const atWord = i === 0 || /[\s.@_+\-"'<>(),]/.test(text[i - 1]);
    if (n && atWord && f.slice(i, i + n) === q) {
      html += `<mark>${esc(text.slice(i, i + n))}</mark>`;
      i += n;
    } else { html += esc(text[i]); i++; }
  }
  return html;
}

const ac = { input: null, items: [], index: 0, el: null };

function acClose() {
  ac.el?.remove();
  Object.assign(ac, { input: null, items: [], index: 0, el: null });
}

function acRender() {
  if (!ac.items.length) return acClose();
  const input = ac.input, q = foldText(addrToken(input).text);
  if (!ac.el) {
    ac.el = document.createElement('div');
    ac.el.className = 'ac-list';
    ac.el.setAttribute('role', 'listbox');
    ac.el.addEventListener('mousedown', e => {  // mousedown: pole nesmí ztratit fokus
      e.preventDefault();
      const it = e.target.closest('.ac-item');
      if (it) acPick(+it.dataset.i);
    });
  }
  ac.el.style.left = input.offsetLeft + 'px';
  ac.el.style.width = input.offsetWidth + 'px';
  ac.el.innerHTML = ac.items.map((c, i) => `
    <div class="ac-item${i === ac.index ? ' active' : ''}" role="option" data-i="${i}">
      ${c.name ? `<span class="ac-name">${acHighlight(c.name, q)}</span>` : ''}
      <span class="${c.name ? 'ac-addr' : 'ac-name'}">${acHighlight(c.email, q)}</span>
    </div>`).join('');
  if (ac.el.parentElement !== input.parentElement) input.parentElement.appendChild(ac.el);
  ac.el.children[ac.index]?.scrollIntoView({ block: 'nearest' });
}

function acUpdate(input) {
  const tok = addrToken(input);
  ac.input = input;
  ac.items = tok.text.length >= 1 ? contactMatches(tok.text, input) : [];
  ac.index = 0;
  acRender();
}

function fmtContact(c) {
  if (!c.name) return c.email;
  const name = /[,;"<>@()[\]:\\]/.test(c.name) ? `"${c.name.replace(/["\\]/g, m => '\\' + m)}"` : c.name;
  return `${name} <${c.email}>`;
}

function acPick(i) {
  const input = ac.input, c = ac.items[i];
  if (!input || !c) return;
  const v = input.value, tok = addrToken(input);
  const before = v.slice(0, tok.start).replace(/\s*$/, '');
  const after = v.slice(tok.end).replace(/^[,;]?\s*/, '');
  if (input.dataset.addr === 'single') input.value = c.email;  // pokročilé hledání: jedna adresa
  else {
    const head = (before ? before + ' ' : '') + fmtContact(c) + ', ';
    input.value = head + after;
    input.setSelectionRange(head.length, head.length);
  }
  acClose();
}

document.querySelectorAll('#composeForm input[data-addr], #advSearchForm input[data-addr]').forEach(input => {
  input.addEventListener('input', () => acUpdate(input));
  input.addEventListener('blur', () => { if (ac.input === input) acClose(); });
  input.addEventListener('keydown', e => {
    if (ac.input !== input || !ac.items.length) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      ac.index = (ac.index + (e.key === 'ArrowDown' ? 1 : -1) + ac.items.length) % ac.items.length;
      acRender();
    } else if (e.key === 'Enter' || (e.key === 'Tab' && !e.shiftKey)) {
      e.preventDefault();
      acPick(ac.index);
    } else if (e.key === 'Escape') {
      e.preventDefault();  // zavře jen našeptávání, ne celé okno
      e.stopPropagation();
      acClose();
    }
  });
});
$('#composeDialog').addEventListener('close', acClose);
$('#advSearchDialog').addEventListener('close', acClose);

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
  updatePasswordRequirement();
}

// Uložené heslo se posílá jen na server a pod jménem, pro které bylo zadané. Po změně je potřeba ho zadat znovu.
const LOGIN_FIELDS = ['username', 'imap_host', 'imap_port', 'imap_security', 'smtp_host', 'smtp_port', 'smtp_security'];
const KEEP_PASSWORD_HINT = '(ponechte prázdné pro zachování stávajícího)';

function loginChanged() {
  const acc = state.editingId ? accById(state.editingId) : null;
  if (!acc || acc.auth === 'oauth2') return false;
  const f = $('#accountForm');
  const norm = v => String(v ?? '').trim().toLowerCase();
  return LOGIN_FIELDS.some(k => norm(f.elements.namedItem(k).value) !== norm(acc[k]));
}

function updatePasswordRequirement() {
  if (!state.editingId) return;
  const changed = loginChanged(), pw = $('#accountForm').elements.namedItem('password'), hint = $('#pwHint');
  pw.required = changed;
  if (changed) hint.textContent = '* – změnili jste server nebo jméno, zadejte heslo znovu';
  else if (hint.textContent.startsWith('*')) hint.textContent = KEEP_PASSWORD_HINT;
}
$('#accountForm').addEventListener('input', updatePasswordRequirement);
$('#accountForm').addEventListener('change', updatePasswordRequirement);

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
  $('#pwHint').textContent = acc ? KEEP_PASSWORD_HINT : '*';
  f.elements.namedItem('password').required = false;
  if (acc) for (const k of Object.keys(acc)) {
    const input = f.elements.namedItem(k);
    if (input && k !== 'preset' && k !== 'password') input.value = acc[k] ?? '';
  }
  // Přihlášení přes Google
  const oauth = acc?.auth === 'oauth2';
  stopGooglePolling();
  $('#passwordLabel').hidden = oauth;
  // účet přes Google: servery a adresa jsou dané Googlem (server je stejně nepřijme jinak)
  for (const k of [...LOGIN_FIELDS, 'email', 'preset']) f.elements.namedItem(k).disabled = oauth;
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

// ------------------------------------------------------------------ uživatelské složky

async function loadUserFolders() {
  try { state.userFolders = await api('/api/user-folders'); } catch { return; }
  renderSettingsFolders();
  if (state.view.type === 'ufolder' && !state.userFolders.some(f => f.id === state.view.id))
    setView({ type: 'unified', filter: 'important' });  // zobrazená složka byla smazána
  else renderSidebar();
}

// --- Nastavit pravidlo: zatřídění zprávy do složky + pravidlo pro další zprávy

let ruleFor = null;  // zpráva (state.current), pro kterou je okno otevřené

const stripPrefixes = s => String(s || '').replace(/^(\s*(re|fw|fwd|odp|vs|aw|wg)\s*(\[\d+\])?\s*:\s*)+/i, '').trim();

function openRuleDialog(cur) {
  if (!cur) return;
  ruleFor = cur;
  const m = cur.data || cur.item || {};
  const f = $('#ruleForm').elements;
  $('#ruleForm').reset();
  $('#ruleError').textContent = '';
  $('#ruleMessage').textContent = `${shortAddr(m.from)} – ${m.subject || '(bez předmětu)'}`;
  $('#ruleMessage').title = m.subject || '';
  const v = state.view;
  const folders = state.userFolders.filter(x => accById(x.account_id));
  $('#ruleFolder').innerHTML = folders.length
    ? folders.map(x => `<option value="${x.id}" ${v.type === 'ufolder' && v.id === x.id ? 'selected' : ''}>${esc(x.name)} · ${esc(accById(x.account_id)?.name || '')}</option>`).join('')
    : '<option value="">Zatím nemáte žádnou složku</option>';
  $('#ruleSave').disabled = !folders.length;
  f.namedItem('sender').value = m.from?.[0]?.addr || '';
  const recips = [...new Set([...(m.to || []), ...(m.cc || [])].map(a => (a.addr || '').toLowerCase()).filter(Boolean))];
  $('#ruleRecipient').innerHTML = recips.length
    ? recips.map(a => `<option value="${esc(a)}">${esc(a)}</option>`).join('')
    : '<option value="">Zpráva nemá žádné příjemce</option>';
  $('#ruleForm').querySelector('input[name="kind"][value="recipient"]').disabled = !recips.length;
  f.namedItem('subject').value = stripPrefixes(m.subject);
  syncRuleForm();
  $('#ruleDialog').showModal();
  $('#ruleFolder').focus();
}

/** Aktivní je jen pole u zvoleného kritéria; poznámka popisuje, co se stane. */
function syncRuleForm() {
  const f = $('#ruleForm').elements;
  const kind = f.namedItem('kind').value;
  f.namedItem('sender').disabled = kind !== 'sender';
  f.namedItem('recipient').disabled = kind !== 'recipient';
  f.namedItem('subject').disabled = kind !== 'subject';
  const folder = state.userFolders.find(x => x.id === +f.namedItem('target').value);
  const inTarget = folder && ruleFor && folder.account_id === ruleFor.accountId && folder.imap_name === ruleFor.folder;
  const who = { sender: 'od tohoto odesílatele', recipient: 'pro tohoto příjemce', subject: 's touto částí předmětu' }[kind];
  const when = f.namedItem('when').value === 'read' ? 'po přečtení' : 'hned po doručení';
  $('#ruleNote').textContent = folder
    ? (inTarget ? 'Zpráva už v této složce je. ' : `Tato zpráva se do složky ${folder.name} přesune hned. `)
      + `Další zprávy ${who} se tam budou přesouvat ${when}.`
    : '';
}

$('#ruleForm').addEventListener('change', syncRuleForm);
$('#ruleCancel').addEventListener('click', () => $('#ruleDialog').close());
$('#ruleNewFolder').addEventListener('click', () => {
  $('#ruleDialog').close();
  openFolderDialog(null);
});

$('#ruleForm').addEventListener('submit', async e => {
  e.preventDefault();
  const cur = ruleFor;
  const f = $('#ruleForm').elements;
  const targetId = +f.namedItem('target').value;
  const target = state.userFolders.find(x => x.id === targetId);
  const kind = f.namedItem('kind').value;
  const value = f.namedItem(kind).value.trim();
  if (!cur || !target) return;
  if (!value) { $('#ruleError').textContent = 'Vyplňte, podle čeho se mají další zprávy poznat.'; return; }
  const btn = $('#ruleSave');
  btn.disabled = true;
  $('#ruleError').textContent = '';
  let res;
  try {
    res = await api(`/api/accounts/${cur.accountId}/messages/${cur.uid}/to-folder`, { method: 'POST',
      body: { folder: cur.folder, target_id: targetId, kind, value, after_read: f.namedItem('when').value === 'read' } });
  } catch (err) {
    $('#ruleError').textContent = err.message;
    return;
  } finally { btn.disabled = false; }
  $('#ruleDialog').close();
  if (res.moved) removeAndAdvance(cur);
  toast((res.moved ? `Přesunuto do složky ${res.folder}. ` : '') + `${RULE_TOASTS[kind](res.value)} ${res.folder}`);
  loadUserFolders();
  loadList({ quiet: true, keepLoaded: true });  // pravidlo mohlo hned přesunout i další zprávy
});

// --- dialog konfigurace složky

function folderForm() { return $('#folderForm').elements; }

/** Dekódovaný název složky na serveru (modifikované UTF-7 → text) – podle seznamu složek účtu. */
function decodeFolderName(imapName) {
  for (const list of Object.values(state.folders)) {
    const hit = (list || []).find(x => x.name === imapName);
    if (hit) return hit.path;
  }
  return imapName;
}

const SYSTEM_ROLES = ['inbox', 'trash', 'junk', 'drafts', 'sent'];

/** Naplní výběr složek na serveru pro zvolený účet (+ volba nové složky). */
async function fillServerFolders(selected) {
  const accId = $('#folderAccount').value;
  if (!state.folders[accId]) await loadFolders(accId);
  const list = (state.folders[accId] || []).filter(x => !SYSTEM_ROLES.includes(x.role));
  $('#folderServer').innerHTML = list.map(x =>
    `<option value="${esc(x.name)}" ${x.name === selected ? 'selected' : ''}>${esc(x.path)}</option>`).join('')
    + `<option value="__new__" ${!list.some(x => x.name === selected) ? 'selected' : ''}>+ Nová složka na serveru…</option>`;
  syncFolderTarget();
}

function syncFolderTarget() {
  const isNew = $('#folderServer').value === '__new__';
  $('#folderNewWrap').hidden = !isNew;
  const f = folderForm();
  if (isNew && !f.namedItem('new_folder').value) f.namedItem('new_folder').value = f.namedItem('name').value;
  const acc = accById($('#folderAccount').value);
  const others = state.accounts.length > 1;
  $('#folderTargetNote').textContent = acc
    ? `Zprávy ze všech účtů se přesouvají sem, na účet ${acc.name || acc.email}.`
      + (others ? ' Zprávy z ostatních účtů se na tento účet přenesou a na původním účtu skončí v koši.' : '')
    : '';
}

/** Konfigurace složky (id) nebo nové složky (null) – obrazovka v Nastavení. */
async function openFolderDialog(id) {
  if (!$('#settingsDialog').open) return openSettings('folder', id);
  const folder = id ? state.userFolders.find(f => f.id === id) : null;
  state.editingFolder = folder ? folder.id : null;
  const f = folderForm();
  $('#folderForm').reset();
  f.namedItem('name').value = folder?.name || '';
  const accId = folder?.account_id && accById(folder.account_id) ? folder.account_id
    : (state.view.type === 'folder' ? state.view.accountId : state.accounts[0]?.id);
  $('#folderAccount').innerHTML = state.accounts.map(a =>
    `<option value="${a.id}" ${a.id === accId ? 'selected' : ''}>${esc(a.name || a.email)}</option>`).join('');
  f.namedItem('mark_read').checked = !!folder?.mark_read;
  $('#folderTitle').textContent = folder ? `Složka ${folder.name}` : 'Nová složka';
  $('#folderSaveBtn').textContent = folder ? 'Uložit změny' : 'Vytvořit složku';
  $('#folderDeleteBtn').hidden = !folder;
  $('#folderDeletePanel').hidden = true;
  $('#folderRuleKind').value = 'sender';
  $('#folderRuleWhen').value = 'now';
  $('#folderRuleInput').placeholder = RULE_PLACEHOLDERS.sender;
  $('#folderError').textContent = folder && !folder.account_id
    ? 'Složka z dřívější verze nemá cílový účet – vyberte účet a složku a uložte. Do té doby se pravidla neuplatňují.' : '';
  $('#folderRuleInput').value = '';
  $('#folderRules').innerHTML = '';
  $('#folderRulesCount').textContent = '';
  showSettingsSection('folder');
  await fillServerFolders(folder?.account_id ? folder.imap_name : null);
  if (folder) renderFolderRules(); else f.namedItem('name').focus();
}

$('#folderAccount').addEventListener('change', () => fillServerFolders(null));
$('#folderServer').addEventListener('change', syncFolderTarget);

async function renderFolderRules(rules) {
  const id = state.editingFolder;
  if (!id) return;
  if (!rules) {
    $('#folderRules').innerHTML = '<li class="empty-rules">Načítám…</li>';
    try { rules = await api(`/api/user-folders/${id}/rules`); } catch (e) {
      $('#folderRules').innerHTML = `<li class="empty-rules error">${esc(e.message)}</li>`;
      return;
    }
  }
  ruleCache.folder = rules;
  $('#folderRulesCount').textContent = rules.length ? `(${rules.length})` : '';
  $('#folderRules').innerHTML = rules.length ? rules.map(r => `
    <li class="rule">
      <div class="rule-main">
        <div class="rule-value" title="${esc(r.value)}">${esc(r.value)}</div>
        <div class="rule-meta">${FOLDER_RULE_KINDS[r.kind] || r.kind} · ${r.after_read ? 'po přečtení' : 'ihned'} · přidáno ${new Date(r.created * 1000).toLocaleDateString('cs-CZ')} · přesunuto ${r.hits}×</div>
      </div>
      ${editRuleButton('folder', r.id)}
      <button type="button" class="btn danger" data-folder-rule="${r.id}">Smazat</button>
    </li>`).join('')
    : '<li class="empty-rules">Zatím žádná pravidla. Přidejte odesílatele, příjemce nebo část předmětu, nebo u zprávy použijte „Nastavit pravidlo…“.</li>';
}

$('#folderForm').addEventListener('submit', async e => {
  e.preventDefault();
  const f = folderForm();
  const isNew = f.namedItem('server_folder').value === '__new__';
  const body = { name: f.namedItem('name').value, account_id: f.namedItem('account_id').value,
                 server_folder: isNew ? null : f.namedItem('server_folder').value,
                 new_folder: isNew ? (f.namedItem('new_folder').value.trim() || f.namedItem('name').value) : null,
                 mark_read: f.namedItem('mark_read').checked };
  const btn = $('#folderSaveBtn');
  btn.disabled = true;
  $('#folderError').textContent = '';
  try {
    if (state.editingFolder) {
      await api(`/api/user-folders/${state.editingFolder}`, { method: 'PUT', body });
      toast('Složka uložena');
      $('#folderTitle').textContent = `Složka ${body.name.trim()}`;
    } else {
      const created = await api('/api/user-folders', { method: 'POST', body });
      toast(`Složka ${created.name} vytvořena – teď jí přidejte pravidla`);
      await loadUserFolders();
      openFolderDialog(created.id);  // pokračovat pravidly
    }
    loadUserFolders();
  } catch (err) {
    $('#folderError').textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

const FOLDER_RULE_KINDS = { sender: 'Odesílatel', recipient: 'Příjemce', subject: 'Část předmětu' };
const RULE_PLACEHOLDERS = { sender: 'odesilatel@example.cz', recipient: 'kolega@example.cz', subject: 'např. projekt Alfa' };
const RULE_TOASTS = {
  sender: v => `Zprávy od ${v} se budou přesouvat do složky`,
  recipient: v => `Zprávy pro ${v} (Komu i Kopie) se budou přesouvat do složky`,
  subject: v => `Zprávy s „${v}“ v předmětu se budou přesouvat do složky`,
};
$('#folderRuleKind').addEventListener('change', e => {
  $('#folderRuleInput').placeholder = RULE_PLACEHOLDERS[e.target.value];
  $('#folderRuleInput').focus();
});

async function addFolderRule() {
  const input = $('#folderRuleInput');
  const kind = $('#folderRuleKind').value;
  const value = input.value.trim();
  if (!value) return;
  try {
    const rules = await api(`/api/user-folders/${state.editingFolder}/rules`, { method: 'POST',
      body: { kind, value, after_read: $('#folderRuleWhen').value === 'read' } });
    input.value = '';
    renderFolderRules(rules);
    toast(RULE_TOASTS[kind](value));
    loadUserFolders();
  } catch (err) { toast(err.message, true); }
}
$('#folderRuleAdd').addEventListener('click', addFolderRule);
$('#folderRuleInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); addFolderRule(); }
});

$('#folderRules').addEventListener('click', async e => {
  const b = e.target.closest('[data-folder-rule]');
  if (!b) return;
  b.disabled = true;
  try {
    await api('/api/user-folder-rules/' + b.dataset.folderRule, { method: 'DELETE' });
    toast('Pravidlo smazáno');
    renderFolderRules();
    loadUserFolders();
  } catch (err) { toast(err.message, true); b.disabled = false; }
});

$('#folderDeleteBtn').addEventListener('click', () => {
  const folder = state.userFolders.find(f => f.id === state.editingFolder);
  const acc = accById(folder?.account_id);
  const server = $('#folderDeleteServer');
  server.disabled = !acc;
  server.textContent = 'Smazat i na serveru včetně všech zpráv';
  delete server.dataset.confirm;
  $('#folderDeleteNote').textContent = acc
    ? `Na serveru: složka ${decodeFolderName(folder.imap_name)} na účtu ${acc.name || acc.email}` +
      ` (${folder.total ?? 0} zpráv). Zprávy se přesunou do koše účtu, pak se složka smaže.`
    : 'Složka nemá platný cílový účet – smazat ji lze jen v aplikaci.';
  $('#folderDeletePanel').hidden = false;
  $('#folderDeleteBtn').hidden = true;
});

$('#folderDeletePanel').addEventListener('click', async e => {
  const b = e.target.closest('[data-del]');
  if (!b) return;
  if (b.dataset.del === 'cancel') {
    $('#folderDeletePanel').hidden = true;
    $('#folderDeleteBtn').hidden = false;
    return;
  }
  const onServer = b.dataset.del === 'server';
  if (onServer && !b.dataset.confirm) {  // nevratnější volba – potvrdit druhým kliknutím
    const folder = state.userFolders.find(f => f.id === state.editingFolder);
    b.dataset.confirm = '1';
    b.textContent = `Opravdu smazat složku i ${folder?.total ?? 0} zpráv na serveru? Klikněte znovu`;
    return;
  }
  try {
    await api('/api/user-folders/' + state.editingFolder + (onServer ? '?server=1' : ''), { method: 'DELETE' });
    toast(onServer ? 'Složka se maže na serveru (zprávy jdou do koše)…'
                   : 'Složka odebrána z aplikace (na serveru i se zprávami zůstává)');
    state.editingFolder = null;
    await loadUserFolders();
    if (state.userFolders.length) openFolderDialog(state.userFolders[0].id);
    else showSettingsSection('unimportant');
  } catch (err) { $('#folderError').textContent = err.message; }
});

// ------------------------------------------------------------------ nastavení

const RULE_KINDS = { sender: 'Odesílatel', subject: 'Předmět' };

let settingsSection = 'unimportant';

/** Přepne téma v levém sloupci nastavení (prostřední a pravý sloupec ukážou jeho obsah). */
function showSettingsSection(section) {
  settingsSection = section;
  const dlg = $('#settingsDialog');
  dlg.querySelectorAll('[data-panel]').forEach(el => { el.hidden = el.dataset.panel !== section; });
  // pravý sloupec jen pokud téma má pravidla (nová, ještě neuložená složka je nemá)
  const hasRules = section === 'folder' ? !!state.editingFolder
    : !!dlg.querySelector(`.settings-rules [data-panel="${section}"]`);
  dlg.querySelector('.settings-body').classList.toggle('no-rules', !hasRules);
  markSettingsNav();
}

/** Složky v levém sloupci Nastavení – každá má svou obrazovku. */
function renderSettingsFolders() {
  $('#settingsFolders').innerHTML = state.userFolders.map(f => {
    const acc = accById(f.account_id);
    return `<button type="button" data-section="folder" data-folder-id="${f.id}" title="${esc(f.name)}">
      <svg class="folder-ico" viewBox="0 0 24 24" style="color:${acc ? accColor(acc.id) : 'var(--danger)'}"><path d="M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>
      <span class="label">${esc(f.name)}</span>${acc ? '' : '<span class="warn-mark" title="Nastavte cílový účet">!</span>'}</button>`;
  }).join('');
  markSettingsNav();
}

function markSettingsNav() {
  const dlg = $('#settingsDialog'), folder = settingsSection === 'folder';
  dlg.querySelectorAll('.settings-nav [data-section]').forEach(b => b.classList.toggle('active',
    b.dataset.section === settingsSection && (!folder || +b.dataset.folderId === state.editingFolder)));
  dlg.querySelector('.settings-nav [data-new-folder]').classList.toggle('active', folder && !state.editingFolder);
}

/** Otevře Nastavení na daném tématu; bez argumentů tam, kde uživatel naposledy skončil. */
async function openSettings(section = settingsSection, folderId = state.editingFolder) {
  if (section === 'folder' && folderId && !state.userFolders.some(f => f.id === folderId)) section = 'unimportant';
  $('#spamRules').innerHTML = $('#notificationsRules').innerHTML = $('#unimportantRules').innerHTML =
    '<li class="empty-rules">Načítám…</li>';
  renderSettingsFolders();
  renderSettingOptions();
  if (section !== 'folder') showSettingsSection(section);
  $('#settingsDialog').showModal();
  if (section === 'folder') openFolderDialog(folderId);
  await Promise.all([renderSpamRules(), ...Object.keys(CATEGORIES).map(renderCategoryRules), renderBayesStatus(),
                     loadSettings().then(renderSettingOptions)]);
}

$('#settingsDialog .settings-nav').addEventListener('click', e => {
  if (e.target.closest('[data-new-folder]')) return openFolderDialog(null);
  const b = e.target.closest('[data-section]');
  if (!b) return;
  if (b.dataset.section === 'folder') return openFolderDialog(+b.dataset.folderId);
  showSettingsSection(b.dataset.section);
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
  const sens = $('#bayesSensitivity');
  sens.value = state.settings?.bayes_sensitivity || 'medium';
  sens.disabled = !state.settings || !state.settings.bayes_enabled;
}

$('#bayesSensitivity').addEventListener('change', async e => {
  try {
    state.settings = await api('/api/settings', { method: 'PUT', body: { bayes_sensitivity: e.target.value } });
  } catch (err) { toast(err.message, true); }
  renderSettingOptions();
});

// --- bayesovský filtr: stav učení (v Nastavení → Spam)
let bayesTimer = null;
async function renderBayesStatus() {
  clearTimeout(bayesTimer);
  const el = $('#bayesStatus');
  let st;
  try { st = await api('/api/bayes'); } catch (e) { el.className = 'note bad'; el.textContent = e.message; return; }
  const b = st.bootstrap;
  let text;
  if (b.status === 'running') text = `Učím se z existujících zpráv… ${b.done} / ${b.total || '?'}`;
  else if (!st.ready) text = `Filtr se zatím učí: ${st.spam} spamových a ${st.ham} dobrých zpráv. Spam začne přesouvat, až bude znát `
    + `aspoň ${st.min_spam} spamových a ${st.min_ham} dobrých zpráv – označujte spam tlačítkem Spam.`;
  else text = `Naučeno: ${st.spam} spamových a ${st.ham} dobrých zpráv (${st.tokens} slov). `
    + `Za posledních 7 dní přesunul do spamu ${zprav(st.moved_week)}.`;
  el.className = 'note' + (st.ready ? ' ok' : '');
  el.textContent = text;
  if (b.status === 'running' && $('#settingsDialog').open) bayesTimer = setTimeout(renderBayesStatus, 1500);
}

$('#bayesReset').addEventListener('click', async () => {
  const ok = await confirmDialog({
    title: 'Naučit filtr znovu',
    lines: ['Filtr zapomene všechno, co se naučil, a naučí se znovu z existujících zpráv: spam = zprávy ve spamu '
      + 'od odesílatelů z pravidel, dobré = přečtené zprávy v doručené poště.'],
    question: 'Pokračovat?', ok: 'Naučit znovu',
  });
  if (!ok) return;
  try { await api('/api/bayes/reset', { method: 'POST' }); } catch (err) { return toast(err.message, true); }
  renderBayesStatus();
});

document.querySelectorAll('[data-setting]').forEach(cb => cb.addEventListener('change', async () => {
  try {
    state.settings = await api('/api/settings', { method: 'PUT', body: { [cb.dataset.setting]: cb.checked } });
  } catch (err) {
    toast(err.message, true);
  }
  renderSettingOptions();
}));

/** Pravidla Nedůležitých / Notifikací v Nastavení (seznam #unimportantRules / #notificationsRules). */
async function renderCategoryRules(cat) {
  const list = $(`#${cat}Rules`);
  let rules;
  try { rules = await api(`/api/categories/${cat}/rules`); } catch (e) {
    list.innerHTML = `<li class="empty-rules error">${esc(e.message)}</li>`;
    return;
  }
  ruleCache[cat] = rules;
  $(`#${cat}RulesCount`).textContent = rules.length ? `(${rules.length})` : '';
  list.innerHTML = rules.length ? rules.map(r => `
    <li class="rule">
      <div class="rule-main">
        <div class="rule-value" title="${esc(categoryRuleText(r))}">${esc(categoryRuleText(r))}</div>
        <div class="rule-meta">přidáno ${new Date(r.created * 1000).toLocaleDateString('cs-CZ')}
          · ${zprav(r.messages)} v doručené poště</div>
      </div>
      <button type="button" class="btn" data-edit-category-rule="${r.id}" data-category="${cat}" title="Upravit pravidlo">Upravit</button>
      <button type="button" class="btn danger" data-category-rule="${r.id}" data-category="${cat}" title="Smazat pravidlo">Smazat</button>
    </li>`).join('')
    : `<li class="empty-rules">Zatím žádná pravidla. Vzniknou tlačítkem ${CATEGORIES[cat].title} u zprávy.</li>`;
}

document.querySelectorAll('#unimportantRules, #notificationsRules').forEach(list => list.addEventListener('click', async e => {
  const btn = e.target.closest('[data-category-rule]');
  if (!btn) return;
  btn.disabled = true;
  const cat = btn.dataset.category;
  try {
    await api(`/api/categories/${cat}/rules/${btn.dataset.categoryRule}`, { method: 'DELETE' });
    toast('Pravidlo smazáno');
    await afterCategoryRuleChange(cat);
  } catch (err) { toast(err.message, true); btn.disabled = false; }
}));

/** Po změně pravidla Nedůležitých / Notifikací: seznam pravidel, zobrazený seznam zpráv a počty. */
async function afterCategoryRuleChange(cat) {
  if (state.current) { delete state.current.category; updateActions(); }  // stav zjistí příští načtení
  await renderCategoryRules(cat);
  if (state.view.type === 'unified') loadList({ quiet: true });
  pollSync();
}

// --- úprava pravidel v Nastavení (přímo v řádku seznamu)

const ruleCache = {};  // poslední vykreslená pravidla podle seznamu: spam / unimportant / notifications / folder

/** Popis pravidla Nedůležitých / Notifikací: vyplněné podmínky. */
const categoryRuleText = r => [r.sender && `od ${r.sender}`, r.recipient && `pro ${r.recipient}`,
                               r.subject && `předmět obsahuje „${r.subject}“`].filter(Boolean).join(' · ');

// --- úprava pravidla Nedůležitých / Notifikací v modálním okně
let editingCategoryRule = null;  // { cat, id }
$('#settingsDialog').addEventListener('click', e => {
  const b = e.target.closest('[data-edit-category-rule]');
  if (!b) return;
  const cat = b.dataset.category, id = +b.dataset.editCategoryRule;
  const r = ruleCache[cat]?.find(x => x.id === id);
  if (!r) return;
  editingCategoryRule = { cat, id };
  const f = $('#categoryRuleForm').elements;
  f.sender.value = r.sender;
  f.recipient.value = r.recipient;
  f.subject.value = r.subject;
  $('#categoryRuleTitle').textContent = `Upravit pravidlo – ${CATEGORIES[cat].title}`;
  $('#categoryRuleError').textContent = '';
  $('#categoryRuleSave').disabled = false;
  $('#categoryRuleDialog').showModal();
  f.sender.focus();
});
$('#categoryRuleCancel').addEventListener('click', () => $('#categoryRuleDialog').close());
$('#categoryRuleForm').addEventListener('submit', async e => {
  e.preventDefault();
  const { cat, id } = editingCategoryRule || {};
  if (!cat) return;
  const f = e.target.elements;
  const body = { sender: f.sender.value.trim(), recipient: f.recipient.value.trim(), subject: f.subject.value.trim() };
  $('#categoryRuleSave').disabled = true;
  try {
    await api(`/api/categories/${cat}/rules/${id}`, { method: 'PUT', body });
    $('#categoryRuleDialog').close();
    toast('Pravidlo upraveno');
    await afterCategoryRuleChange(cat);
  } catch (err) {
    $('#categoryRuleError').textContent = err.message;
    $('#categoryRuleSave').disabled = false;
  }
});
const RULE_EDITORS = {
  spam: { kinds: { sender: 'Odesílatel' }, url: id => `/api/spam-rules/${id}`, render: () => renderSpamRules() },
  folder: { kinds: FOLDER_RULE_KINDS, when: true, url: id => `/api/user-folder-rules/${id}`,
            render: () => renderFolderRules(), after: () => { renderFolderRules(); loadUserFolders(); } },
};
const editRuleButton = (list, id) =>
  `<button type="button" class="btn" data-edit-rule="${id}" data-list="${list}" title="Upravit pravidlo">Upravit</button>`;

$('#settingsDialog').addEventListener('click', e => {
  const b = e.target.closest('[data-edit-rule]');
  if (b) startRuleEdit(b.closest('li'), b.dataset.list, +b.dataset.editRule);
});

/** Řádek pravidla se změní na formulář: typ, (u složek kdy přesouvat), hodnota, Uložit / Zrušit. */
function startRuleEdit(li, list, id) {
  const ed = RULE_EDITORS[list], r = ruleCache[list]?.find(x => x.id === id);
  if (!li || !ed || !r) return;
  const kinds = Object.entries(ed.kinds);
  li.classList.add('editing');
  li.innerHTML = `<form class="rule-edit" autocomplete="off">
    ${kinds.length > 1 ? `<select name="kind" title="Typ pravidla">${kinds.map(([k, label]) =>
      `<option value="${k}" ${k === r.kind ? 'selected' : ''}>${label}</option>`).join('')}</select>` : ''}
    ${ed.when ? `<select name="when" title="Kdy zprávy podle pravidla přesouvat">
      <option value="now">Ihned</option><option value="read" ${r.after_read ? 'selected' : ''}>Po přečtení</option></select>` : ''}
    <input name="value" value="${esc(r.value)}" aria-label="Hodnota pravidla">
    <p class="error rule-edit-error"></p>
    <div class="rule-edit-actions">
      <button type="button" class="btn" data-cancel>Zrušit</button>
      <button type="submit" class="btn primary">Uložit</button>
    </div>
  </form>`;
  const form = li.querySelector('form'), input = form.elements.value;
  input.focus();
  input.select();
  form.querySelector('[data-cancel]').addEventListener('click', () => ed.render());
  form.addEventListener('keydown', e => {
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); ed.render(); }  // nezavírat celé Nastavení
  });
  form.addEventListener('submit', async e => {
    e.preventDefault();
    const body = { kind: form.elements.kind?.value || r.kind, value: input.value.trim() };
    if (ed.when) body.after_read = form.elements.when.value === 'read';
    const save = form.querySelector('[type="submit"]');
    save.disabled = true;
    try {
      await api(ed.url(id), { method: 'PUT', body });
      toast('Pravidlo upraveno');
      await (ed.after || ed.render)();
    } catch (err) {
      form.querySelector('.rule-edit-error').textContent = err.message;
      save.disabled = false;
    }
  });
}

async function renderSpamRules() {
  let rules;
  try { rules = await api('/api/spam-rules'); } catch (e) {
    $('#spamRules').innerHTML = `<li class="empty-rules error">${esc(e.message)}</li>`;
    return;
  }
  ruleCache.spam = rules;
  $('#spamRulesCount').textContent = rules.length ? `(${rules.length})` : '';
  $('#spamRules').innerHTML = rules.length ? rules.map(r => `
    <li class="rule">
      <div class="rule-main">
        <div class="rule-value" title="${esc(r.value)}">${esc(r.value)}</div>
        <div class="rule-meta">${RULE_KINDS[r.kind] || r.kind} · přidáno ${new Date(r.created * 1000).toLocaleDateString('cs-CZ')}
          · zachyceno ${r.hits}×</div>
      </div>
      ${editRuleButton('spam', r.id)}
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

$('#settingsBtn').addEventListener('click', () => openSettings());
document.querySelectorAll('dialog [data-close]').forEach(b =>
  b.addEventListener('click', () => b.closest('dialog').close()));

// Zavírací křížek v pravém horním rohu každého modálního okna (Nastavení má vlastní v záhlaví).
// Přidává se na konec okna, aby po otevření nedostal fokus místo prvního pole.
document.querySelectorAll('dialog').forEach(dlg => {
  if (dlg.querySelector('.close-x')) return;
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'icon-btn close-x dialog-x';
  b.title = 'Zavřít (Esc)';
  b.setAttribute('aria-label', 'Zavřít');
  b.textContent = '✕';
  b.addEventListener('click', () => { dlg.returnValue = ''; dlg.close(); });
  dlg.appendChild(b);
});

// ------------------------------------------------------------------ start

async function refreshAll() {
  api('/api/sync', { method: 'POST' }).catch(() => {});  // synchronizace se serverem na pozadí
  await Promise.all([loadList({ quiet: true }), ...state.accounts.map(a => loadFolders(a.id))]);
}

/** Data se zobrazují z lokální databáze; synchronizace běží na pozadí. Každé 3 s se zeptáme,
 *  zda se databáze změnila (levný lokální dotaz), a případně překreslíme seznam a počty. */
let lastNoticeId = null;  // poslední zobrazené hlášení ze serveru
async function pollSync() {
  if (document.hidden || state.dragItems) return;  // během přetahování nepřekreslovat (zrušilo by tah)
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
  for (const f of state.userFolders) {
    const n = st.folders?.[f.id] ?? f.unread;
    if (n !== f.unread) { f.unread = n; countsChanged = true; }
  }
  if (st.spam != null && st.spam !== state.spamUnread) {
    state.spamUnread = st.spam;
    countsChanged = true;
  }
  if (st.trash != null && st.trash !== state.trashTotal) {
    state.trashTotal = st.trash;
    countsChanged = true;
    renderTrashFooter();
  }
  if (countsChanged) renderSidebar();
  const notices = st.notices || [];
  const newest = notices.length ? notices[notices.length - 1][0] : 0;
  if (lastNoticeId !== null) notices.filter(([id]) => id > lastNoticeId).forEach(([, text]) => toast(text, true));
  lastNoticeId = Math.max(lastNoticeId ?? 0, newest);
  // Seznam vznikl z určité verze databáze; pokud se od té doby změnila, načteme ho znovu.
  if (!state.listLoading && state.listVersion != null && st.version !== state.listVersion && !activeQuery())
    loadList({ quiet: true, keepLoaded: true });
}

// ------------------------------------------------------------------ šířka sloupců

// Šířku lze měnit levému panelu, seznamu a kontextu (tažením úchytu na okraji, dvojklik = výchozí šířka).
// Čtecí část vezme zbytek (nejméně READER_MIN), sloupec Akce má pevnou šířku.
const COLUMNS = {
  sidebar: { el: '#sidebar', prop: '--w-sidebar', edge: 'right', min: 150, max: 420 },
  list:    { el: '#list',    prop: '--w-list',    edge: 'right', min: 220, max: 760 },
  context: { el: '#context', prop: '--w-context', edge: 'left',  min: 180, max: 640 },
};
const READER_MIN = 320;
const COLUMNS_KEY = 'mailer.columns';

function loadColumnWidths() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(COLUMNS_KEY) || '{}') || {}; } catch { /* úložiště nedostupné */ }
  for (const [name, c] of Object.entries(COLUMNS)) {
    const w = +saved[name];
    if (w >= c.min && w <= c.max) $('#app').style.setProperty(c.prop, `${w}px`);
  }
  fitColumns();
}

function saveColumnWidths() {
  const out = {};
  for (const [name, c] of Object.entries(COLUMNS)) {
    const v = $('#app').style.getPropertyValue(c.prop);
    if (v) out[name] = parseFloat(v);
  }
  try { localStorage.setItem(COLUMNS_KEY, JSON.stringify(out)); } catch { /* úložiště nedostupné */ }
}

const colWidth = name => $(COLUMNS[name].el).getBoundingClientRect().width;
/** Místo, které zbývá na čtecí část (její vlastní šířka pod minimum neklesne – rozvržení by přeteklo). */
const readerSpace = () => $('#detail').getBoundingClientRect().width - $('#actions').getBoundingClientRect().width;

/** Když se okno zmenší, aby čtecí část neklesla pod minimum: ubrat nejdřív kontextu, pak seznamu a panelu. */
function fitColumns() {
  if (innerWidth <= 760) return;  // úzké okno: sloupce pod sebou
  let lack = READER_MIN - readerSpace();
  for (const name of ['context', 'list', 'sidebar']) {
    if (lack <= 0.5) break;
    const c = COLUMNS[name], w = colWidth(name);
    const nw = Math.max(c.min, w - lack);
    if (nw < w) {
      $('#app').style.setProperty(c.prop, `${nw}px`);
      lack -= w - nw;
    }
  }
}

for (const [name, c] of Object.entries(COLUMNS)) {
  const handle = document.createElement('div');
  handle.className = `col-resizer ${c.edge}`;
  handle.title = 'Tažením změníte šířku sloupce, dvojklikem vrátíte výchozí';
  $(c.el).append(handle);
  handle.addEventListener('pointerdown', e => {
    if (e.button !== 0) return;
    e.preventDefault();
    const startX = e.clientX, startW = colWidth(name);
    const max = Math.min(c.max, startW + readerSpace() - READER_MIN);
    handle.setPointerCapture(e.pointerId);
    handle.classList.add('dragging');
    document.body.classList.add('resizing');
    const move = ev => {
      const dx = ev.clientX - startX;
      const w = Math.round(Math.min(Math.max(startW + (c.edge === 'right' ? dx : -dx), c.min), Math.max(c.min, max)));
      $('#app').style.setProperty(c.prop, `${w}px`);
    };
    const end = () => {
      handle.removeEventListener('pointermove', move);
      handle.classList.remove('dragging');
      document.body.classList.remove('resizing');
      saveColumnWidths();
    };
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', end, { once: true });
    handle.addEventListener('pointercancel', end, { once: true });
  });
  handle.addEventListener('dblclick', () => {
    $('#app').style.removeProperty(c.prop);
    fitColumns();
    saveColumnWidths();
  });
}
window.addEventListener('resize', fitColumns);
loadColumnWidths();

(async () => {
  try {
    await loadAccounts();  // jen seznam účtů z lokálního úložiště – okamžité
    if (!state.accounts.length) openAccountDialog();
    loadList();            // zprávy z lokální databáze
    pollSync();            // počty nepřečtených hned, ne až za 3 s
    loadSettings();
    loadUserFolders();
  } catch (e) { toast(e.message, true); }
  setInterval(pollSync, 3000);
  // seznam složek (a případné chyby připojení) občas obnovíme ze serveru
  setInterval(() => { if (!document.hidden) state.accounts.forEach(a => loadFolders(a.id)); }, 300000);
})();
