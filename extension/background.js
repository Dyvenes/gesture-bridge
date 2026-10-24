/*
 * Мост между вкладками и агентом на этом же компьютере.
 *
 * Содержимое вкладок собирает content.js, а наружу его отдаёт этот
 * сценарий: WebSocket к ws://127.0.0.1 разрешён только отсюда. Со
 * страницы по https такое соединение браузер заблокировал бы как
 * смешанное содержимое, а localhost для расширения — доверенный адрес.
 *
 * Сервис-воркер в Manifest V3 засыпает. Поэтому соединение не держим
 * любой ценой: при каждом сообщении от вкладки проверяем сокет и при
 * необходимости поднимаем заново. Пробуждает воркер сама же вкладка.
 */

const AGENT_URL = "ws://127.0.0.1:8799/ext";
const RETRY_MS = 3000;

let socket = null;
let connecting = false;
let lastState = null;
let lastSentAt = 0;

function connect() {
  if (connecting) return;
  if (socket && socket.readyState === WebSocket.OPEN) return;
  if (socket && socket.readyState === WebSocket.CONNECTING) return;

  connecting = true;
  try {
    socket = new WebSocket(AGENT_URL);
  } catch {
    connecting = false;
    return;
  }

  socket.addEventListener("open", () => {
    connecting = false;
    if (lastState) push(lastState);
  });

  socket.addEventListener("close", () => {
    connecting = false;
    socket = null;
  });

  socket.addEventListener("error", () => {
    connecting = false;
  });
}

function push(state) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    connect();
    return;
  }
  try {
    socket.send(JSON.stringify(state));
    lastSentAt = Date.now();
  } catch {
    socket = null;
  }
}

chrome.runtime.onMessage.addListener((message, sender) => {
  if (message?.type !== "tab-state") return;

  // Интересует только то, что человек видит прямо сейчас: активная
  // вкладка активного окна. Фоновые вкладки тоже тикают, и без этой
  // проверки агент хватал бы случайную из них.
  if (!sender.tab?.active) return;

  chrome.windows.get(sender.tab.windowId, (window) => {
    if (chrome.runtime.lastError) return;
    if (!window?.focused) return;

    lastState = { ...message.state, at: Date.now() };
    push(lastState);
  });
});

// Смена вкладки руками тоже должна доезжать мгновенно, не дожидаясь
// очередного тика таймера в content.js.
chrome.tabs.onActivated.addListener(() => connect());
chrome.windows.onFocusChanged.addListener(() => connect());

connect();
