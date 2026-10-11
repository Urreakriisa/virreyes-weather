/* v5 (11-oct-2026): STRUCTURAL FIX — page-shell requests are network-first
   with cache fallback (fresh page whenever online, cached shell only when
   offline), so a deploy reaches installed PWAs without waiting on a SW
   byte-change. skipWaiting/claim were already in place; the version bump
   stays a per-deploy secondary guarantee. (v4: display parity; v3: Tlaloc
   icons; v2: dropped CARTO watermark tiles.) */
const CACHE = 'virreyes-meteorologist-v5';
const STATIC = ['/manifest.json', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(STATIC)));
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))));
  self.clients.claim();
});

/* item 25: push = verified evidence only (T1 onset / T2 approach / T3 rayo).
   The payload is server-composed; we display it verbatim. */
self.addEventListener('push', event => {
  let d = {};
  try { d = event.data.json(); } catch (e) {}
  event.waitUntil(self.registration.showNotification(d.title || 'Estacion Virreyes', {
    body: d.body || '', icon: '/icon-192.png', badge: '/icon-192.png',
    tag: d.tag || 'virreyes', data: { url: '/' }
  }));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then(ws => {
    for (const w of ws) { if ('focus' in w) return w.focus(); }
    return clients.openWindow('/');
  }));
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  /* PAGE SHELL: network-first, cache fallback. A successful fetch refreshes
     the cached copy (keyed by pathname so ?nc= busts don't fragment it);
     the cache serves ONLY when the network fails (offline). */
  if (event.request.mode === 'navigate' || url.pathname === '/' || url.pathname.endsWith('.html')) {
    event.respondWith(
      fetch(event.request, { cache: 'no-store' }).then(resp => {
        if (resp && resp.ok) {
          const copy = resp.clone();
          caches.open(CACHE).then(c => c.put(new Request(url.pathname), copy)).catch(() => {});
        }
        return resp;
      }).catch(() =>
        caches.match(url.pathname).then(c => c || caches.match('/'))
      )
    );
    return;
  }
  /* API: network-only, NEVER cache-served — stale data reading as current
     is the dangerous failure class (fail-visible doctrine). */
  if (url.pathname.startsWith('/api/') || url.pathname === '/current') {
    event.respondWith(fetch(event.request, { cache: 'no-store' }));
    return;
  }
  /* static assets (icons, libraries, tiles): cache-first, unchanged */
  event.respondWith(caches.match(event.request).then(cached => cached || fetch(event.request)));
});
