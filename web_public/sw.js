/**
 * sw.js — MB-EGX offline support.
 *
 * Two caching strategies, split by what's being requested:
 *
 *   1. App shell (index.html, this script, manifest.json, and anything
 *      else in SHELL_ASSETS) — network-first, cache as fallback ONLY.
 *      index.html carries live app logic (auth, Firestore writes, terms/
 *      consent handling) that gets bug-fixed and redeployed, sometimes
 *      more than once in a day - serving a stale cached copy first (the
 *      old strategy) meant every visit ran code from the *previous*
 *      deploy while silently fetching the new one for NEXT time, so a
 *      user reloading once after a fix shipped would still hit the bug
 *      that was supposedly already fixed. Try the network first; only
 *      fall back to the cached shell when the network request itself
 *      fails (actually offline), so "online" always means "current code."
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
 * Bump CACHE_VERSION whenever SHELL_ASSETS changes (or, as here, whenever
 * this file's own caching logic changes) so returning clients discard
 * whatever they had cached under the old version instead of continuing
 * to fall back to it. skipWaiting()/clients.claim() below mean a bumped
 * version takes over immediately on next load, no tab-closing required.
 */
const CACHE_VERSION = 'mb-egx-v2';
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

    // App shell / static assets: network-first, so an online visitor
    // always gets the currently-deployed code. The cache is only ever
    // used as a fallback when the network request itself fails (i.e.
    // actually offline) - it must never be what an online user sees.
    // {cache: 'no-store'} bypasses the browser's own HTTP disk cache too,
    // so a fetch() here can't be silently answered by a stale disk-cached
    // response out from under the service worker.
    event.respondWith(
        fetch(req, { cache: 'no-store' })
            .then((res) => {
                const copy = res.clone();
                caches.open(SHELL_CACHE).then((cache) => cache.put(req, copy));
                return res;
            })
            .catch(() => caches.match(req))
    );
});
