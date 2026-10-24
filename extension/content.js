/*
 * Сообщает фоновому сценарию, что на самом деле происходит на странице.
 *
 * Адресная строка для этого не годится: на сайтах с сериями переключение
 * серии часто идёт через history.pushState, и строка адреса остаётся
 * прежней — агент переносил бы ссылку на первую серию. Позиции
 * воспроизведения в адресе не бывает вовсе.
 */

const REPORT_INTERVAL_MS = 1000;

let lastSignature = "";

function activeVideo() {
  // Берём самое длинное видео на странице: это отсекает рекламные
  // вставки и превью, которые иногда висят рядом с основным плеером.
  const videos = Array.from(document.querySelectorAll("video"));
  if (videos.length === 0) return null;
  return videos.reduce((best, candidate) => {
    const bestLength = best?.duration || 0;
    const candidateLength = candidate.duration || 0;
    return candidateLength > bestLength ? candidate : best;
  }, videos[0]);
}

function snapshot() {
  const video = activeVideo();
  const state = {
    url: location.href,
    title: document.title || "",
    seconds: 0,
    duration: 0,
    playing: false,
  };
  if (video && Number.isFinite(video.currentTime)) {
    state.seconds = Math.max(0, Math.floor(video.currentTime));
    state.duration = Number.isFinite(video.duration) ? Math.floor(video.duration) : 0;
    state.playing = !video.paused && !video.ended;
  }
  return state;
}

function report(force) {
  const state = snapshot();
  const signature = `${state.url}|${state.seconds}`;
  if (!force && signature === lastSignature) return;
  lastSignature = signature;

  try {
    chrome.runtime.sendMessage({ type: "tab-state", state });
  } catch {
    // Расширение перезагрузили — молчим, следующий тик всё поправит.
  }
}

// Переходы внутри страницы адресную строку меняют не всегда, но
// history API дёргается почти везде, поэтому слушаем именно его.
for (const method of ["pushState", "replaceState"]) {
  const original = history[method];
  history[method] = function (...args) {
    const result = original.apply(this, args);
    setTimeout(() => report(true), 50);
    return result;
  };
}

window.addEventListener("popstate", () => report(true));
window.addEventListener("pageshow", () => report(true));
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) report(true);
});

setInterval(() => report(false), REPORT_INTERVAL_MS);
report(true);
