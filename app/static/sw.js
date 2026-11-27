// Kombüse service worker: the app opens without network, Web Push without payload.
//
// Offline: the app shell (index.html, i18n.js, manifest, icons, the barcode scanner) is kept in a cache. Pages and
// i18n.js come from the network first (so updates arrive right away) and from the cache only when the server cannot be
// reached. API answers are never cached here: the shopping list keeps its own copy in the app (localStorage, cleared
// on sign-out and when another account signs in), everything else stays network-only.
// The server fills in the version below, so every release gets a new cache and a new service worker.
//
// Push: messages carry no payload. On a push the worker asks the app for the waiting notifications of this device
// (api/push/pending with the secret the app stored in IndexedDB) and shows them. A tap opens the app at the right view.
const VERSION = '__KOMBUESE_VERSION__';
const REV = 1;  // raise when the list below or the caching changes without a new release
const CACHE = `kombuese-shell-${VERSION}-${REV}`;
const SCOPE = new URL('./', self.location).href;  // the app may live under a path behind a reverse proxy
const SHELL = ['./', 'i18n.js', 'manifest.json', 'icon.svg', 'icon-180.png', 'icon-192.png', 'icon-512.png'];
const NETWORK_FIRST = ['', 'index.html', 'i18n.js'];     // relative to the scope
const STATIC = /^(manifest\.json|icon\.svg|icon-\d+\.png|zxing\.min\.js|zxing-LIZENZ\.txt)$/;
const TIMEOUT = 4000;  // bad reception in the supermarket: after this the cached page wins

self.addEventListener('install', e => {
  e.waitUntil((async () => {
    const c = await caches.open(CACHE);
    await Promise.all(SHELL.map(async u => {
      try {  // redirect "manual": without a session '/' redirects to the login page, which must not land in the cache as the app
        const r = await fetch(new Request(new URL(u, SCOPE), { cache: 'reload', redirect: 'manual', credentials: 'same-origin' }));
        if (r.ok && r.type === 'basic') await c.put(new URL(u, SCOPE).href, r);
      } catch (err) {}
    }));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', e => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) if (k.startsWith('kombuese-') && k !== CACHE) await caches.delete(k);
    await self.clients.claim();
  })());
});

const rel = url => url.href.startsWith(SCOPE) ? url.href.slice(SCOPE.length).split(/[?#]/)[0] : null;

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url), path = rel(url);
  if (path === null) return;                                    // other sites: not ours
  if (req.mode === 'navigate' || NETWORK_FIRST.includes(path)) {
    if (req.mode === 'navigate' && !['', 'index.html'].includes(path)) return;  // login, print view, PDF …: network only
    e.respondWith(networkFirst(req, path === 'i18n.js' ? 'i18n.js' : './'));
  } else if (STATIC.test(path)) {
    e.respondWith(staleWhileRevalidate(req));
  }
  // everything else (api/, auth/, img/, mimg/, rezept/, kalender.ics, mcp): browser default = network only
});

async function networkFirst(req, key) {
  const c = await caches.open(CACHE), keyUrl = new URL(key, SCOPE).href;
  const net = fetch(req).then(async r => {
    if (r.ok && r.type === 'basic' && !r.redirected) await c.put(keyUrl, r.clone());
    return r;
  });
  const cached = await c.match(keyUrl);
  if (!cached) return net;
  // network first, but a slow network (dead spot) loses after TIMEOUT; a failed one right away
  return new Promise(resolve => {
    let done = false;
    const fallback = () => { if (!done) { done = true; resolve(cached.clone()); } };
    const t = setTimeout(fallback, TIMEOUT);
    net.then(r => { clearTimeout(t); if (!done) { done = true; resolve(r); } }, () => { clearTimeout(t); fallback(); });
  });
}

async function staleWhileRevalidate(req) {
  const c = await caches.open(CACHE), cached = await c.match(req);
  const net = fetch(req).then(async r => { if (r.ok && r.type === 'basic') await c.put(req, r.clone()); return r; });
  if (cached) { net.catch(() => {}); return cached; }
  return net;
}

// ----- messages from the app: sign-out clears everything an account left on this device -----
self.addEventListener('message', e => {
  const d = e.data || {};
  if (d.type === 'clear') e.waitUntil((async () => {
    for (const k of await caches.keys()) if (k.startsWith('kombuese-')) await caches.delete(k);
    await kv('delete', 'push');
  })());
});

// ----- tiny key/value store in IndexedDB (shared with the app: database "kombuese", store "kv") -----
function kv(op, key, value) {
  return new Promise(resolve => {
    let open;
    try { open = indexedDB.open('kombuese', 1); } catch (err) { return resolve(null); }
    open.onupgradeneeded = () => open.result.createObjectStore('kv');
    open.onerror = () => resolve(null);
    open.onsuccess = () => {
      const db = open.result, tx = db.transaction('kv', op === 'get' ? 'readonly' : 'readwrite'), st = tx.objectStore('kv');
      const r = op === 'get' ? st.get(key) : op === 'put' ? st.put(value, key) : st.delete(key);
      r.onsuccess = () => { resolve(op === 'get' ? r.result : true); db.close(); };
      r.onerror = () => { resolve(null); db.close(); };
    };
  });
}

// ----- Web Push -----
const FALLBACK = {
  de: { title: 'Kombüse', text: 'Neue Nachricht – tippen zum Öffnen' },
  en: { title: 'Kombüse', text: 'New message – tap to open' },
};
const ICON = new URL('icon-192.png', SCOPE).href;

self.addEventListener('push', e => e.waitUntil(onPush()));

async function onPush() {
  const s = await kv('get', 'push');  // {id, secret, key, lang}
  let items = [];
  if (s && s.id && s.secret) {
    try {
      const r = await fetch(new URL('api/push/pending?id=' + encodeURIComponent(s.id), SCOPE), {
        headers: { 'X-Push-Secret': s.secret }, cache: 'no-store', credentials: 'omit' });
      if (r.ok) items = (await r.json()).items || [];
    } catch (err) {}
  }
  if (items.length) {
    for (const it of items.slice(-5)) {
      await self.registration.showNotification(it.title || 'Kombüse', {
        body: it.text, tag: 'kombuese-' + it.id, icon: ICON, badge: ICON, data: { view: it.view || '' } });
    }
    return;
  }
  // Every push must show something (browsers count silent pushes against the site). Nothing new for us, e.g. a
  // second push for a message the first one already fetched: show the latest notification again, quietly.
  const shown = await self.registration.getNotifications();
  if (shown.length) {
    const n = shown[shown.length - 1];
    return self.registration.showNotification(n.title, { body: n.body, tag: n.tag, icon: ICON, badge: ICON, data: n.data, silent: true });
  }
  const f = FALLBACK[(s && s.lang) || 'de'] || FALLBACK.de;
  return self.registration.showNotification(f.title, { body: f.text, tag: 'kombuese', icon: ICON, badge: ICON, data: { view: '' } });
}

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const view = (e.notification.data || {}).view || '';
  const target = new URL(view ? './?view=' + encodeURIComponent(view) : './', SCOPE).href;
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const win = wins.find(w => w.url.startsWith(SCOPE));
    if (win) {
      if (view) win.postMessage({ type: 'open', view });
      return win.focus();
    }
    return self.clients.openWindow(target);
  })());
});

// The push service renewed the subscription: tell the app's server about the new one (needs the session cookie)
self.addEventListener('pushsubscriptionchange', e => {
  e.waitUntil((async () => {
    const s = await kv('get', 'push');
    if (!s || !s.key) return;
    try {
      const sub = e.newSubscription || await self.registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64u(s.key) });
      const r = await fetch(new URL('api/push/subscribe', SCOPE), { method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-Kombuese': '1' }, body: JSON.stringify(sub.toJSON()) });
      if (r.ok) { const j = await r.json(); await kv('put', 'push', { ...s, id: j.id, secret: j.secret, key: j.key }); }
    } catch (err) {}
  })());
});

function b64u(s) {
  const b = atob(s.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - s.length % 4) % 4));
  return Uint8Array.from(b, ch => ch.charCodeAt(0));
}
