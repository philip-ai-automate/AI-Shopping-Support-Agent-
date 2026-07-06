const CACHE = 'phixtra-v1';
const STATIC = [
  '/static/portal/phixtra-logo.png',
  '/static/portal/favicon.png',
  '/static/portal/pwa-icon-192.png',
  '/static/portal/pwa-icon-512.png'
];

// Install — pre-cache static assets
self.addEventListener('install', function(e) {
  e.waitUntil(
    caches.open(CACHE).then(function(c) { return c.addAll(STATIC); })
  );
  self.skipWaiting();
});

// Activate — delete old caches
self.addEventListener('activate', function(e) {
  e.waitUntil(
    caches.keys().then(function(keys) {
      return Promise.all(
        keys.filter(function(k) { return k !== CACHE; })
            .map(function(k) { return caches.delete(k); })
      );
    })
  );
  self.clients.claim();
});

// Fetch strategy:
// - Static assets (images, fonts) → cache-first
// - Everything else (pages, API) → network-first, fall back to cache
self.addEventListener('fetch', function(e) {
  var url = new URL(e.request.url);

  // Only handle same-origin requests
  if (url.origin !== self.location.origin) return;

  var isStatic = url.pathname.startsWith('/static/');

  if (isStatic) {
    // Cache-first for static files
    e.respondWith(
      caches.match(e.request).then(function(cached) {
        return cached || fetch(e.request).then(function(res) {
          if (res && res.status === 200) {
            var clone = res.clone();
            caches.open(CACHE).then(function(c) { c.put(e.request, clone); });
          }
          return res;
        });
      })
    );
  } else {
    // Network-first for pages — always get fresh data, cache as fallback
    e.respondWith(
      fetch(e.request).then(function(res) {
        if (res && res.status === 200 && e.request.method === 'GET') {
          var clone = res.clone();
          caches.open(CACHE).then(function(c) { c.put(e.request, clone); });
        }
        return res;
      }).catch(function() {
        return caches.match(e.request);
      })
    );
  }
});
