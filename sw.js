const CACHE = 'virreyes-v2';
const STATIC = ['/manifest.json', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(STATIC)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
  ));
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // Always fetch live from network: API data, HTML page
  if (url.pathname === '/current' || url.pathname === '/' || url.pathname.endsWith('.html')) {
    e.respondWith(
      fetch(e.request).catch(() => {
        // Offline fallback for HTML only
        if (url.pathname === '/' || url.pathname.endsWith('.html')) {
          return caches.match('/');
        }
        return new Response(JSON.stringify({error: 'Sin conexion'}), {
          headers: {'Content-Type': 'application/json'}
        });
      })
    );
    return;
  }

  // Cache-first for icons and manifest
  e.respondWith(
    caches.match(e.request).then(cached =>
      cached || fetch(e.request).then(res => {
        const clone = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone));
        return res;
      })
    )
  );
});
