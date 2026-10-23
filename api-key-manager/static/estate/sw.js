// KeyLand (home.phixtra.com) service worker, 2026-10-22 (Phase 4).
// Phone notifications only: shows KeyLand alerts sent by the WhatsApp gateway
// (keyland_alerts_gw.py) and opens the lead when tapped. No page caching.
self.addEventListener('install', function() { self.skipWaiting(); });
self.addEventListener('activate', function(e) { e.waitUntil(self.clients.claim()); });

self.addEventListener('push', function(e) {
  var d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = {body: e.data ? e.data.text() : ''}; }
  e.waitUntil(self.registration.showNotification(d.title || 'KeyLand', {
    body: d.body || '',
    icon: '/estate/app-icon-192.png',
    badge: '/estate/app-icon-192.png',
    tag: d.tag || undefined,
    renotify: !!d.tag,
    data: {url: d.url || '/estate/dashboard'}
  }));
});

self.addEventListener('notificationclick', function(e) {
  e.notification.close();
  var target = new URL((e.notification.data && e.notification.data.url) || '/estate/dashboard', self.location.origin);
  if (target.origin !== self.location.origin) target = new URL('/estate/dashboard', self.location.origin);
  e.waitUntil(
    self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(function(list) {
      for (var i = 0; i < list.length; i++) {
        var c = list[i];
        if (new URL(c.url).origin === self.location.origin && 'focus' in c) {
          return c.navigate(target.href)
            .then(function(w) { return (w || c).focus(); })
            .catch(function() { return self.clients.openWindow(target.href); });
        }
      }
      return self.clients.openWindow(target.href);
    })
  );
});
