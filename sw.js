const CACHE = 'virreyes-meteorologist-v1';
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
  if (url.pathname === '/' || url.pathname.endsWith('.html') || url.pathname.startsWith('/api/') || url.pathname === '/current') {
    event.respondWith(fetch(event.request, { cache: 'no-store' }));
    return;
  }
  event.respondWith(caches.match(event.request).then(cached => cached || fetch(event.request)));
});
