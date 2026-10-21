'use strict';
const VERSION = 'pure-novel-shell-v12';
const SHELL = ['/', '/style.css', '/ebooks.js', '/app.js', '/manifest.webmanifest', '/icon.svg'];
self.addEventListener('install', event => {
  event.waitUntil(caches.open(VERSION).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k.startsWith('pure-novel-shell-') && k !== VERSION).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  if (event.request.method !== 'GET' || new URL(event.request.url).origin !== self.location.origin) return;
  if (!SHELL.includes(new URL(event.request.url).pathname)) return;
  event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
});