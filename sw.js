// Réseau d'abord, pour toujours afficher la dernière veille ; copie locale si hors ligne.
const CACHE = "fil-v1";
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET" || new URL(event.request.url).origin !== location.origin) return;
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(event.request, copy));
        return response;
      })
      .catch(() => caches.match(event.request))
  );
});

self.addEventListener("push", (event) => {
  const data = event.data ? event.data.json() : { title: "Le Fil Politique", body: "Votre fil politique du matin est prêt.", url: "./" };
  event.waitUntil(self.registration.showNotification(data.title, { body: data.body, icon: "icon-180.png", data: { url: data.url } }));
});
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL(event.notification.data.url, self.registration.scope).href;
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
      const open = windows[0];
      if (open) return open.navigate(url).then((client) => (client || open).focus());
      return self.clients.openWindow(url);
    })
  );
});
