/**
 * sw.js — MB-EGX offline support.
 *
 * Two caching strategies, split by what's being requested:
 *
 *   1. App shell (index.html, this script, manifest.json, and anything
 *      else in SHELL_ASSETS) — cache-first. These rarely change within a
 *      single day and the dashboard is unusable at all without them, so
 *      serving the cached copy instantly (falling back to network only
 *      on a cache miss) is the right trade-off.
 *
 *   2. Data shards (web_public/data/*.json - matrix.json, sectors.json,
 *      chart_history.json, strategy_performance.json, etc., written by
 *      export_json.py / export_backtest_summary.py) — network-first.
 *      A visitor should always see today's fresh numbers when online;
 *      the cached copy is ONLY a fallback for when the network request
 *      fails (offline, flaky connection), so the dashboard shows
 *      *something* — the last successfully fetched data — instead of a
 *      blank/broken page.
 *
 * Everything else (CDN scripts, fonts, Firebase calls) passes straight
 * through untouched - this worker never intercepts cross-origin requests,
 * so auth/analytics/CDN behavior is unaffected.
 *
 * Bump CACHE_VERSION whenever SHELL_ASSETS changes so old clients pick up
 * the new shell instead of serving a stale cached index.html forever.
 */
const CACHE_VERSION = 'mb-egx-v1';
const SHELL_CACHE = `${CACHE_VERSION}-shell`;
const DATA_CACHE = `${CACHE_VERSION}-data`;

const SHELL_ASSETS = [
    './',
    './index.html',
    './manifest.json',
    './assets/mb-egx-logo.png',
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(SHELL_CACHE)
            .then((cache) => cache.addAll(SHELL_ASSETS))
            .catch((err) => console.warn('SW: shell precache failed (non-fatal):', err))
    );
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((keys) =>
            Promise.all(
                keys
                    .filter((key) => key.startsWith('mb-egx-') && key !== SHELL_CACHE && key !== DATA_CACHE)
                    .map((key) => caches.delete(key))
            )
        )
    );
    self.clients.claim();
});

function isDataRequest(url) {
    return url.pathname.includes('/data/') && url.pathname.endsWith('.json');
}

self.addEventListener('fetch', (event) => {
    const req = event.request;
    if (req.method !== 'GET') return;

    const url = new URL(req.url);
    if (url.origin !== self.location.origin) return; // never touch cross-origin (CDN/Firebase/etc.)

    if (isDataRequest(url)) {
        // Network-first: always try for fresh data; fall back to the last
        // cached copy only if the network request itself fails.
        event.respondWith(
            fetch(req)
                .then((res) => {
                    const copy = res.clone();
                    caches.open(DATA_CACHE).then((cache) => cache.put(req, copy));
                    return res;
                })
                .catch(() => caches.match(req))
        );
        return;
    }

    // App shell / static assets: cache-first, network fallback, and
    // opportunistically refresh the cache so the next offline session
    // has whatever was last successfully loaded.
    event.respondWith(
        caches.match(req).then((cached) => {
            const network = fetch(req)
                .then((res) => {
                    const copy = res.clone();
                    caches.open(SHELL_CACHE).then((cache) => cache.put(req, copy));
                    return res;
                })
                .catch(() => cached);
            return cached || network;
        })
    );
});
