/*
 * Service worker нужен по одной причине: без него Chrome на Android не
 * предложит установить приложение, а без установки Gesture Bridge не
 * появится в системном меню «Поделиться».
 *
 * Поэтому он намеренно примитивен: ничего не кэширует, только проксирует.
 * Кэшировать тут нечего — приёмник без брокера всё равно бесполезен,
 * а устаревший кэш приносит больше вреда, чем офлайн-доступ пользы.
 */

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) =>
  event.waitUntil(self.clients.claim()),
);

self.addEventListener("fetch", (event) => {
  // Обработчик обязан существовать, чтобы приложение считалось устанавливаемым.
  event.respondWith(fetch(event.request));
});
