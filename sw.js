// Reggie.Bot service worker.
//
// The app is about 15MB of WebAssembly, so caching it turns a repeat visit
// from a long download into an instant load. That is the entire reason this
// exists; offline is a side effect.
//
// A service worker outlives a deploy, which is what makes it dangerous. Cache
// the wrong thing and a returning visitor is pinned to an old build with no
// route to the new one. Two rules keep that from happening:
//
//   1. Only content-hashed files are cached first. "2e3f29d7...wasm" can never
//      mean anything else, so it is safe forever.
//   2. Everything else - documents, composeApp.js, the manifest - comes from
//      the network first. composeApp.js matters most: its name is stable but
//      its contents name the hashed wasm to load, so a stale copy would ask
//      for files that no longer exist and the app would not start.
//
// Pinned by PwaTest.

const CACHE = 'reggie-v1';

// A webpack content hash: at least eight hex characters before the extension.
const IMMUTABLE = /\/[0-9a-f]{8,}\.(wasm|js|css)$/;

// Never touched: answers are per-question and publication data changes when
// the corpus refreshes, so neither should be served from a cache.
function isOurs(url) {
  return url.origin === self.location.origin;
}

self.addEventListener('install', (event) => {
  // Take over as soon as the new worker is ready rather than waiting for every
  // tab to close, which on a site people leave open is close to never.
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(
      names.filter((n) => n !== CACHE).map((n) => caches.delete(n))
    );
    await self.clients.claim();
  })());
});

async function cacheFirst(request) {
  const cached = await caches.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (response.ok) {
    const cache = await caches.open(CACHE);
    cache.put(request, response.clone());
  }
  return response;
}

async function networkFirst(request) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(CACHE);
      cache.put(request, response.clone());
    }
    return response;
  } catch (err) {
    // Offline. A cached copy is better than a browser error page, and for a
    // document any cached page beats nothing.
    const cached = await caches.match(request);
    if (cached) return cached;
    if (request.mode === 'navigate') {
      const home = await caches.match('/');
      if (home) return home;
    }
    throw err;
  }
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  const url = new URL(request.url);

  // Same-origin GETs only. The API and Supabase are someone else's problem,
  // and a cached answer to a different question would be worse than useless.
  if (request.method !== 'GET' || !isOurs(url)) return;

  if (IMMUTABLE.test(url.pathname)) {
    event.respondWith(cacheFirst(request));
  } else {
    event.respondWith(networkFirst(request));
  }
});
