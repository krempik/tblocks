const CACHE_NAME = 'tblocks-v4.1.0';
const ASSETS = [
    './',
    './index.html',
    './manifest.json'
];

self.addEventListener('install', e => {
    e.waitUntil(
        caches.open(CACHE_NAME)
            .then(c => c.addAll(ASSETS))
            .then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', e => {
    e.waitUntil(
        caches.keys().then(keys =>
            Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
        ).then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', e => {
    if (e.request.url.includes('/api/')) return;
    // Network-first for page navigations so deploys reach players, with the
    // cache as offline fallback. Other assets stay cache-first.
    e.respondWith(
        e.request.mode === 'navigate'
            ? fetch(e.request).catch(() => caches.match(e.request))
            : caches.match(e.request).then(r => r || fetch(e.request))
    );
});