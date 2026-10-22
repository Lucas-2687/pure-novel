'use strict';
const VERSION = 'pure-novel-shell-v16';
const SHELL = ['/', '/style.css', '/ebooks.js', '/storage.js', '/app.js', '/manifest.webmanifest', '/icon.svg'];
self.addEventListener('install', event => {
  event.waitUntil(caches.open(VERSION).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k.startsWith('pure-novel-shell-') && k !== VERSION).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
async function shellResponse(request) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5000);
  try {
    const response = await fetch(request, { signal: controller.signal });
    if (!response.ok) throw new Error('Shell unavailable');
    await response.clone().arrayBuffer();
    return response;
  } catch (error) {
    const stored = await caches.match(request, { ignoreSearch: true });
    if (stored) return stored;
    throw error;
  } finally { clearTimeout(timer); }
}
self.addEventListener('fetch', event => {
  if (event.request.method !== 'GET' || new URL(event.request.url).origin !== self.location.origin) return;
  if (!SHELL.includes(new URL(event.request.url).pathname)) return;
  event.respondWith(shellResponse(event.request));
});