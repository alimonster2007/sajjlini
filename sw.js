const CACHE_NAME = "sajjilni-app-shell-v3";
const STATIC_CACHE_NAME = "sajjilni-static-v1";
const APP_SHELL = [
  "/",
  "/manifest.json",
  "/logo.png",
  "/static/icon.svg",
  "/static/icon.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)).then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => (key.startsWith("sajjilni-app-shell-") && key !== CACHE_NAME) || (key.startsWith("sajjilni-static-") && key !== STATIC_CACHE_NAME)).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

function offlineJsonResponse() {
  return new Response(JSON.stringify({
    status: "offline",
    offline: true,
    message: "The server is unavailable while this device is offline.",
  }), {
    status: 503,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  });
}

function isLocalStaticAsset(request, url) {
  if (["style", "script", "image", "font"].includes(request.destination)) return true;
  return url.pathname.startsWith("/static/") && !url.pathname.startsWith("/static/materials/");
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (request.mode === "navigate") {
    event.respondWith(caches.match("/").then((cached) => cached || fetch(request)));
    return;
  }

  if (request.method === "GET" && APP_SHELL.includes(url.pathname)) {
    event.respondWith(caches.match(request).then((cached) => cached || fetch(request)));
    return;
  }

  if (request.method === "GET" && isLocalStaticAsset(request, url)) {
    event.respondWith(caches.open(STATIC_CACHE_NAME).then(async (cache) => {
      const cached = await cache.match(request);
      if (cached) return cached;
      const response = await fetch(request);
      if (response.ok) await cache.put(request, response.clone());
      return response;
    }).catch(() => caches.match(request).then((cached) => cached || Response.error())));
    return;
  }

  // Frontend API calls use both /api/* and older root-level routes. API payloads
  // can contain private student data, so return a generic offline response instead
  // of persisting those responses in Cache Storage.
  if (request.destination === "") {
    event.respondWith(fetch(request).catch(() => offlineJsonResponse()));
  }
});
