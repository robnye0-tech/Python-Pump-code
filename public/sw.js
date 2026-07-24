const CACHE = 'signal-desk-shell-v1';
const SHELL = ['/', '/styles.css', '/app.js', '/manifest.json'];

self.addEventListener('install', (evt) => {
  evt.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', (evt) => {
  evt.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
  );
  self.clients.claim();
});

// network-first for everything (dashboard needs live data), falling back to
// the cached shell only if the server is unreachable — keeps the app icon
// opening instantly instead of a blank page while the network resolves
self.addEventListener('fetch', (evt) => {
  if (evt.request.method !== 'GET') return;
  evt.respondWith(
    fetch(evt.request)
      .then(res => {
        const copy = res.clone();
        if (SHELL.includes(new URL(evt.request.url).pathname)) {
          caches.open(CACHE).then(c => c.put(evt.request, copy));
        }
        return res;
      })
      .catch(() => caches.match(evt.request))
  );
});
