/* 腸嚐新知 Service Worker
   殼層走快取（開得快），資料走網路優先（永遠拿最新的）
   離線時自動退回上次讀到的資料 */

const SHELL = 'cc-shell-v9';
const DATA  = 'cc-data-v9';

const SHELL_FILES = [
  './',
  './index.html',
  './manifest.json',
  './icons/icon-180.png',
  './icons/icon-192.png',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(SHELL)
      .then(c => c.addAll(SHELL_FILES))
      .then(() => self.skipWaiting())
      .catch(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(k => k !== SHELL && k !== DATA).map(k => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== location.origin) return;   // 字型等外部資源交給瀏覽器

  // 資料：網路優先，離線退回快取
  if (url.pathname.includes('/data/')) {
    e.respondWith(
      fetch(req)
        .then(res => {
          const copy = res.clone();
          caches.open(DATA).then(c => c.put(req, copy));
          return res;
        })
        .catch(() => caches.match(req))
    );
    return;
  }

  // 殼層：快取優先
  e.respondWith(
    caches.match(req).then(hit => hit || fetch(req))
  );
});
