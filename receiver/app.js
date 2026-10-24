/*
 * Приёмник Gesture Bridge для телефона.
 *
 * Главное правило: камера выключена. Она поднимается только когда брокер
 * сказал "для тебя есть посылка", и гаснет сразу после. Из-за этого батарея
 * не страдает и ложных срабатываний почти нет.
 *
 * Стенд-режим (телефон на зарядке) держит камеру постоянно — тогда ладонь
 * срабатывает мгновенно, без ожидания пуша.
 */

import {
  FilesetResolver,
  GestureRecognizer,
} from "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.21";

const WASM_BASE =
  "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.21/wasm";
const MODEL_URL = "/models/gesture_recognizer.task";

const PALM = "Open_Palm";

// Гистерезис. Уверенность модели всё время дышит, и на телефоне размах
// заметно больше, чем на веб-камере ПК. Если требовать непрерывной серии
// кадров выше одного порога, отсчёт удержания обнуляется на каждом провале
// и до конца не доходит никогда. Поэтому войти в удержание труднее
// (ENTER), чем в нём остаться (STAY), а короткая потеря руки прощается.
const ENTER_CONFIDENCE = 0.55;
const STAY_CONFIDENCE = 0.35;
const LOST_GRACE_MS = 350;
// Льгота переживает потерю руки, но стрелять, когда руки в кадре уже нет,
// нельзя: иначе короткий показ ладони с последующим опусканием руки
// досчитывался бы до срабатывания вхолостую.
const SEEN_RECENTLY_MS = 120;

const HOLD_MS = 450;
const COOLDOWN_MS = 1500;

const el = (id) => document.getElementById(id);

const ui = {
  linkDot: el("link-dot"),
  linkState: el("link-state"),
  standToggle: el("stand-toggle"),
  stage: el("stage"),
  stageIcon: el("stage-icon"),
  stageTitle: el("stage-title"),
  stageText: el("stage-text"),
  countdown: el("countdown"),
  countdownBar: el("countdown-bar"),
  cameraWrap: el("camera-wrap"),
  cameraHint: el("camera-hint"),
  cameraDebug: el("camera-debug"),
  video: el("video"),
  delivered: el("delivered"),
  deliveredTitle: el("delivered-title"),
  deliveredPreview: el("delivered-preview"),
  deliveredText: el("delivered-text"),
  deliveredFiles: el("delivered-files"),
  deliveredLink: el("delivered-link"),
  deliveredCopy: el("delivered-copy"),
  queueWrap: el("queue-wrap"),
  queue: el("queue"),
  peers: el("peers"),
};

// --- состояние ------------------------------------------------------------

const state = {
  ws: null,
  token: null,
  stack: [],
  recognizer: null,
  stream: null,
  loopId: null,
  held: null,
  heldSince: 0,
  quietUntil: 0,
  deadline: 0,
  standMode: localStorage.getItem("standMode") === "1",
  lastTouch: Date.now(),
  reconnectDelay: 1000,
  lastStamp: 0,
  errorShown: false,
  delegate: "unknown",
  lastPalm: 0,
  bestScore: 0,
  bestArea: 0,
};

["pointerdown", "touchstart", "keydown"].forEach((evt) =>
  window.addEventListener(evt, () => (state.lastTouch = Date.now()), {
    passive: true,
  }),
);

// --- токен ----------------------------------------------------------------

function resolveToken() {
  const fromUrl = new URL(location.href).searchParams.get("token");
  if (fromUrl) {
    localStorage.setItem("token", fromUrl);
    // убираем токен из адресной строки, чтобы не утёк в историю и шаринг
    const clean = new URL(location.href);
    clean.searchParams.delete("token");
    history.replaceState(null, "", clean);
    return fromUrl;
  }
  return localStorage.getItem("token");
}

// --- экран ----------------------------------------------------------------

function setStage(icon, title, text, tone = "") {
  ui.stageIcon.textContent = icon;
  ui.stageTitle.textContent = title;
  ui.stageText.textContent = text;
  ui.stage.className = `stage ${tone}`;
}

function idleStage() {
  if (state.standMode) {
    setStage("👁", "Стенд-режим", "Камера работает постоянно. Показывай ладонь в любой момент.");
  } else {
    setStage("⌛", "Ожидание", "Камера выключена. Поднимется, когда для этого устройства появится посылка.");
  }
}

function renderQueue(items) {
  ui.queue.replaceChildren();
  const list = items || [];
  ui.queueWrap.hidden = list.length === 0;
  for (const item of list) {
    const li = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.innerHTML =
      `<span class="q-from">${escapeHtml(item.from || "?")}</span>` +
      `<span class="q-preview">${escapeHtml(item.preview || "")}</span>`;
    button.addEventListener("click", () => send({ t: "claim", id: item.id }));
    li.append(button);
    ui.queue.append(li);
  }
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = String(value);
  return div.innerHTML;
}

function resetDelivered(title, preview) {
  ui.deliveredTitle.textContent = title;
  ui.deliveredPreview.textContent = preview || "";
  ui.deliveredText.hidden = true;
  ui.deliveredFiles.hidden = true;
  ui.deliveredFiles.replaceChildren();
  ui.deliveredLink.hidden = true;
  ui.deliveredCopy.hidden = true;
  ui.delivered.hidden = false;
}

function humanSize(bytes) {
  const units = ["Б", "КБ", "МБ", "ГБ"];
  let value = Number(bytes) || 0;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${unit === 0 ? value : value.toFixed(1)} ${units[unit]}`;
}

function showDeliveredUrl(url, preview) {
  resetDelivered("Принято", preview || url);
  ui.deliveredLink.href = url;
  ui.deliveredLink.hidden = false;
  // Жест — не "user gesture" для браузера, поэтому вкладку могут
  // заблокировать. Тогда остаётся заметная кнопка: один тап вместо нуля.
  const opened = window.open(url, "_blank", "noopener");
  if (opened) {
    setStage("✅", "Открыто", preview || url, "good");
  } else {
    setStage("👆", "Готово — нажми «Открыть»",
      "браузер не дал открыть само", "good");
  }
}

function showDeliveredText(text, preview) {
  resetDelivered("Текст принят", preview || "");
  ui.deliveredText.textContent = text;
  ui.deliveredText.hidden = false;
  ui.deliveredCopy.hidden = false;
  ui.deliveredCopy.textContent = "Скопировать";
  ui.deliveredCopy.onclick = async () => {
    try {
      await navigator.clipboard.writeText(text);
      ui.deliveredCopy.textContent = "Скопировано";
    } catch {
      // Chrome на Android пишет в буфер только по явному касанию; если
      // и оно не прошло, выделяем текст — останется долгий тап.
      const range = document.createRange();
      range.selectNodeContents(ui.deliveredText);
      const sel = getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      ui.deliveredCopy.textContent = "Выделено — скопируй вручную";
    }
  };
  setStage("📋", "Текст принят", `${text.length} символов`, "good");
}

function showDeliveredFiles(files, preview) {
  resetDelivered(files.length === 1 ? "Файл принят" : "Файлы приняты", preview);
  ui.deliveredFiles.hidden = false;

  for (const file of files) {
    const li = document.createElement("li");
    const link = document.createElement("a");
    link.className = "file-link";
    link.href = `/blob/${encodeURIComponent(file.id)}?token=${encodeURIComponent(state.token)}`;
    link.download = file.name || "file";
    link.innerHTML =
      `<span class="q-from">${escapeHtml(file.name || "файл")}</span>` +
      `<span class="q-preview">${humanSize(file.size)}</span>`;
    li.append(link);
    ui.deliveredFiles.append(li);
  }

  // Один файл пробуем забрать сразу; несколько — не сыплем диалогами,
  // пусть человек выберет сам.
  if (files.length === 1) {
    ui.deliveredFiles.querySelector("a").click();
  }
  setStage("📥", files.length === 1 ? "Файл принят" : `Файлов: ${files.length}`,
    "Если загрузка не началась — нажми на имя файла.", "good");
}

// --- камера и распознавание ----------------------------------------------

async function ensureRecognizer() {
  if (state.recognizer) return state.recognizer;
  ui.cameraHint.textContent = "загружаю распознаватель…";
  const vision = await FilesetResolver.forVisionTasks(WASM_BASE);

  // GPU быстрее, но на части Android-устройств делегат создаётся и молча
  // не выдаёт результатов. CPU медленнее и работает везде, поэтому он —
  // запасной вариант, а не отказ.
  for (const delegate of ["GPU", "CPU"]) {
    try {
      state.recognizer = await GestureRecognizer.createFromOptions(vision, {
        baseOptions: { modelAssetPath: MODEL_URL, delegate },
        runningMode: "VIDEO",
        numHands: 1,
      });
      state.delegate = delegate;
      ui.peers.textContent += ` · ${delegate}`;
      return state.recognizer;
    } catch (err) {
      console.warn(`делегат ${delegate} не поднялся:`, err);
    }
  }
  throw new Error("ни GPU, ни CPU не дали создать распознаватель");
}

function handArea(landmarks) {
  const xs = landmarks.map((p) => p.x);
  const ys = landmarks.map((p) => p.y);
  const area =
    (Math.max(...xs) - Math.min(...xs)) * (Math.max(...ys) - Math.min(...ys));
  return Math.min(1, Math.max(0, area));
}

async function cameraOn(ttlSeconds) {
  state.deadline = ttlSeconds ? Date.now() + ttlSeconds * 1000 : 0;

  if (state.stream) {
    tick();
    return;
  }
  try {
    state.stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "user", width: 640, height: 480 },
      audio: false,
    });
  } catch (err) {
    setStage("🚫", "Нет доступа к камере",
      `${err.name}. Нужен HTTPS и разрешение для сайта. Посылку можно забрать тапом ниже.`,
      "warn");
    send({ t: "queue" });
    return;
  }

  ui.video.srcObject = state.stream;
  ui.cameraWrap.hidden = false;
  await ui.video.play().catch(() => {});

  try {
    await ensureRecognizer();
  } catch (err) {
    setStage("🚫", "Распознаватель не загрузился", String(err), "warn");
    cameraOff();
    return;
  }

  ui.cameraHint.textContent = "покажи раскрытую ладонь";
  tick();
}

function cameraOff() {
  if (state.loopId) {
    cancelAnimationFrame(state.loopId);
    state.loopId = null;
  }
  if (state.stream && !state.standMode) {
    state.stream.getTracks().forEach((t) => t.stop());
    state.stream = null;
    ui.video.srcObject = null;
    ui.cameraWrap.hidden = true;
  }
  ui.countdown.hidden = true;
  state.held = null;
}

function tick() {
  if (state.loopId) cancelAnimationFrame(state.loopId);

  const step = () => {
    state.loopId = requestAnimationFrame(step);

    // время вышло — гасим камеру, посылка уйдёт в висящую очередь
    if (state.deadline && Date.now() > state.deadline) {
      if (!state.standMode) {
        cameraOff();
        idleStage();
      } else {
        state.deadline = 0;
        ui.countdown.hidden = true;
      }
      return;
    }
    if (state.deadline) {
      const left = Math.max(0, state.deadline - Date.now());
      ui.countdown.hidden = false;
      ui.countdownBar.style.width = `${(left / 15000) * 100}%`;
    }

    if (!state.recognizer) {
      ui.cameraDebug.textContent = "распознаватель не создан";
      return;
    }
    // Размер кадра — не украшение: на Android видео бывает «играет»
    // визуально, а в элемент приходит 0×0, и распознавателю нечего дать.
    const dims = `${ui.video.videoWidth}×${ui.video.videoHeight}`;
    if (ui.video.readyState < 2 || !ui.video.videoWidth) {
      ui.cameraDebug.textContent =
        `кадра нет: readyState=${ui.video.readyState}, ${dims}`;
      return;
    }

    // Время обязано строго расти, иначе MediaPipe отвергает кадр. Два
    // requestAnimationFrame в одну миллисекунду — реальный случай.
    let stamp = Math.round(performance.now());
    if (stamp <= state.lastStamp) stamp = state.lastStamp + 1;
    state.lastStamp = stamp;

    let result;
    try {
      result = state.recognizer.recognizeForVideo(ui.video, stamp);
      state.errorShown = false;
    } catch (err) {
      // Раньше эта ошибка глоталась молча, и снаружи это выглядело как
      // «камера работает, а жест не ловится». Больше так не делаем.
      if (!state.errorShown) {
        state.errorShown = true;
        console.warn("recognizeForVideo:", err);
      }
      ui.cameraDebug.textContent = `сбой распознавания: ${err?.message || err}`;
      return;
    }

    const hands = result.landmarks?.length || 0;
    const top = result.gestures?.[0]?.[0];
    const area = hands ? handArea(result.landmarks[0]) : 0;

    consider(top?.categoryName ?? null, top?.score ?? 0, area);

    // Считаем после consider: иначе показывали бы удержание до того, как
    // его обновили, и цифра всегда отставала бы на кадр.
    const held = state.held
      ? Math.min(100, Math.round(((Date.now() - state.heldSince) / HOLD_MS) * 100))
      : 0;
    ui.cameraDebug.textContent = hands
      ? `${top ? top.categoryName : "—"} ${(top?.score ?? 0).toFixed(2)} · ` +
        `рука ${area.toFixed(3)} · удержание ${held}%`
      : `руки в кадре нет · ${dims} · ${state.delegate}`;
  };

  step();
}

function consider(category, score, area) {
  const now = Date.now();
  const isPalm = category === PALM;

  if (isPalm && score >= ENTER_CONFIDENCE) {
    if (state.held !== PALM) {
      state.held = PALM;
      state.heldSince = now;
      state.bestScore = 0;
      state.bestArea = 0;
    }
    state.lastPalm = now;
  } else if (state.held === PALM && isPalm && score >= STAY_CONFIDENCE) {
    state.lastPalm = now;                  // просело, но это всё ещё ладонь
  } else if (state.held === PALM && now - state.lastPalm <= LOST_GRACE_MS) {
    // ладонь потерялась на миг — отсчёт не рвём
  } else {
    state.held = null;
    ui.cameraHint.textContent = "покажи раскрытую ладонь";
    return;
  }

  // В ставку уходит лучшее, что видели за удержание, а не случайный
  // последний кадр: он может оказаться как раз провалом.
  state.bestScore = Math.max(state.bestScore, score);
  state.bestArea = Math.max(state.bestArea, area);

  if (now < state.quietUntil) return;
  if (now - state.heldSince < HOLD_MS) {
    ui.cameraHint.textContent = "держи ладонь…";
    return;
  }
  if (now - state.lastPalm > SEEN_RECENTLY_MS) return;

  state.quietUntil = now + COOLDOWN_MS;
  state.held = null;

  // Ставка, а не приказ: брокер сравнит нас с другими устройствами
  send({
    t: "bid",
    role: "drop",
    signals: {
      hand: state.bestArea,
      conf: state.bestScore,
      idle: (now - state.lastTouch) / 1000,
    },
  });
  ui.cameraHint.textContent = "ладонь принята, жду брокера…";
}

// --- связь ----------------------------------------------------------------

function send(payload) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify(payload));
    return true;
  }
  return false;
}

function setLink(ok, text) {
  ui.linkDot.className = `dot ${ok ? "on" : "off"}`;
  ui.linkState.textContent = text;
}

function handle(msg) {
  switch (msg.t) {
    case "welcome":
      state.stack = msg.stack || [];
      renderQueue(msg.queue);
      if (state.stack.length) armForStack();
      break;

    case "peers":
      ui.peers.textContent =
        "устройства: " + (msg.peers || []).map((p) => p.name).join(", ");
      break;

    case "pending":
      state.stack = msg.stack || [];
      armForStack(msg.ttl);
      break;

    case "stack":
      state.stack = msg.stack || [];
      if (!state.stack.length && !state.standMode) {
        cameraOff();
        idleStage();
      }
      break;

    case "grabbed":
      setStage("📦", "Отправлено", "Покажи ладонь в веб-камеру ПК.", "good");
      break;

    case "deliver": {
      state.stack = state.stack.filter((p) => p.id !== msg.id);
      cameraOff();
      // посылка могла прийти из висящей очереди — список надо перечитать,
      // иначе забранное останется в нём мёртвой строкой
      send({ t: "queue" });

      const payload = msg.payload || {};
      if (msg.kind === "files" && payload.files?.length) {
        showDeliveredFiles(payload.files, msg.preview);
      } else if (msg.kind === "text" && payload.text) {
        showDeliveredText(payload.text, msg.preview);
      } else if (payload.url) {
        showDeliveredUrl(payload.url, msg.preview);
      }
      break;
    }

    case "taken":
      state.stack = msg.stack || [];
      send({ t: "queue" });
      if (!state.stack.length) {
        cameraOff();
        setStage("➡️", `Забрал ${msg.by}`, "Посылка ушла на другое устройство.");
        setTimeout(idleStage, 2500);
      }
      break;

    case "lost":
      setStage("➡️", `Жест отдан ${msg.to}`, "Там рука была ближе к камере.");
      setTimeout(idleStage, 2500);
      break;

    case "nothing_to_take":
      state.stack = [];
      cameraOff();
      send({ t: "queue" });
      setStage("🤷", "Брать нечего", "Посылку уже забрали.", "warn");
      setTimeout(idleStage, 2500);
      break;

    case "hovering":
      state.stack = state.stack.filter((p) => p.id !== msg.id);
      cameraOff();
      send({ t: "queue" });
      setStage("🕗", "Посылка зависла", "Ладонь не показали — забери её тапом ниже.", "warn");
      break;

    case "queue":
      renderQueue(msg.items);
      break;

    case "cancelled":
    case "gone":
      state.stack = state.stack.filter((p) => p.id !== msg.id);
      send({ t: "queue" });
      break;

    case "ignored":
      ui.cameraHint.textContent = "жест смазался, покажи ещё раз";
      break;

    case "error":
      console.warn("брокер:", msg.reason);
      break;
  }
}

function armForStack(ttl) {
  const top = state.stack[0];
  if (!top) return;
  ui.delivered.hidden = true;     // предыдущая доставка больше не актуальна
  const extra =
    state.stack.length > 1 ? ` (+${state.stack.length - 1} под ней)` : "";
  setStage("🖐", `${top.from} → ${top.preview}${extra}`,
    "Покажи раскрытую ладонь в фронтальную камеру.");
  cameraOn(ttl || 15);
}

function connect() {
  if (!state.token) {
    setLink(false, "нет токена");
    setStage("🔑", "Нужен токен",
      "Открой приёмник по ссылке с ?token=… из вывода брокера.", "warn");
    return;
  }

  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(
    `${scheme}://${location.host}/ws?token=${encodeURIComponent(state.token)}`,
  );
  state.ws = ws;
  setLink(false, "подключаюсь…");

  ws.addEventListener("open", () => {
    state.reconnectDelay = 1000;
    setLink(true, "на связи");
    send({
      t: "hello",
      name: deviceName(),
      device: "phone",
      caps: ["drop_gesture", "grab_share", "open_url"],
    });
    flushPendingShare();
  });

  ws.addEventListener("message", (event) => {
    try {
      handle(JSON.parse(event.data));
    } catch (err) {
      console.warn("битое сообщение", err);
    }
  });

  ws.addEventListener("close", () => {
    setLink(false, "нет связи, переподключаюсь");
    cameraOff();
    setTimeout(connect, state.reconnectDelay);
    state.reconnectDelay = Math.min(state.reconnectDelay * 2, 30000);
  });

  ws.addEventListener("error", () => setLink(false, "ошибка связи"));
}

function deviceName() {
  const saved = localStorage.getItem("deviceName");
  if (saved) return saved;
  const guess = /Android/i.test(navigator.userAgent) ? "Телефон" : "Браузер";
  localStorage.setItem("deviceName", guess);
  return guess;
}

// --- отправка через системное «Поделиться» -------------------------------

function pendingShareFromUrl() {
  const params = new URL(location.href).searchParams;
  const direct = params.get("url");
  const text = params.get("text") || "";
  // Android часто кладёт ссылку в text, а не в url
  const found = direct || (text.match(/https?:\/\/\S+/) || [])[0];
  if (!found) return null;
  return { url: found, title: params.get("title") || "" };
}

function flushPendingShare() {
  const share = pendingShareFromUrl();
  if (!share) return;
  send({
    t: "grab",
    kind: "url",
    payload: share,
    preview: share.title
      ? `${share.url.replace(/^https?:\/\//, "").slice(0, 60)} — ${share.title.slice(0, 50)}`
      : share.url.replace(/^https?:\/\//, "").slice(0, 60),
  });
  history.replaceState(null, "", "/");
}

// --- стенд-режим ----------------------------------------------------------

ui.standToggle.addEventListener("click", async () => {
  state.standMode = !state.standMode;
  localStorage.setItem("standMode", state.standMode ? "1" : "0");
  ui.standToggle.setAttribute("aria-pressed", String(state.standMode));
  ui.standToggle.classList.toggle("active", state.standMode);

  if (state.standMode) {
    await cameraOn(0);
  } else {
    state.deadline = 0;
    const stream = state.stream;
    state.stream = null;
    if (stream) stream.getTracks().forEach((t) => t.stop());
    ui.video.srcObject = null;
    ui.cameraWrap.hidden = true;
    cameraOff();
  }
  idleStage();
});

// Экран погас или свернули — камеру отпускаем, иначе Android её всё равно
// заберёт, но уже после того, как мы потратим батарею.
document.addEventListener("visibilitychange", () => {
  if (document.hidden) cameraOff();
});

// --- старт ----------------------------------------------------------------

state.token = resolveToken();
ui.standToggle.setAttribute("aria-pressed", String(state.standMode));
ui.standToggle.classList.toggle("active", state.standMode);
idleStage();
connect();

if (state.standMode) cameraOn(0);

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
}
