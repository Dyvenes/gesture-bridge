"""Агент Gesture Bridge для Windows.

Собирает воедино: камеру с распознаванием жестов, содержимое активного
окна, оверлей поверх всех окон и связь с брокером.

Потоки:
  главный   — tkinter с оверлеем (tkinter требует главного потока);
  камера    — GestureWatcher, блокирующий цикл чтения кадров;
  окно      — GrabWatcher, пассивный опрос активного окна;
  сеть      — asyncio с WebSocket к брокеру.

Захват двухфазный: жест -> ставка на аукционе -> брокер объявляет
победителя -> и только победитель снимает содержимое и заливает файлы.
Сам агент никогда не решает в одиночку, что он источник или приёмник.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiohttp                                                    # noqa: E402
import uiautomation as auto                                       # noqa: E402

from agent.capture import GrabWatcher, set_clipboard_text         # noqa: E402
from agent.gestures import FIST, PALM, GestureEvent, GestureWatcher  # noqa: E402
from agent.overlay import ACCENT, WARN, Card, Overlay             # noqa: E402
from agent.tray import Tray                                       # noqa: E402
from agent.winutil import (                                       # noqa: E402
    downloads_dir, idle_seconds, open_url, reveal_in_explorer, unique_path,
)

log = logging.getLogger("bridge.agent")

MODEL = ROOT / "models" / "gesture_recognizer.task"
TOKEN_FILE = ROOT / ".token"
UPLOAD_CHUNK = 256 * 1024
PALM_QUIET_AFTER_GRAB = 2.5   # сек; столько длится «разжал руку и опустил»


def _human(size: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} ГБ"


class Agent:
    def __init__(self, broker_url: str, token: str, name: str,
                 camera: int, deep: bool) -> None:
        self.base = broker_url.rstrip("/")
        self.token = token
        self.name = name
        self.deep = deep

        self.overlay = Overlay()
        self.content = GrabWatcher()
        self.gestures = GestureWatcher(MODEL, self._on_gesture, camera)

        self.loop = asyncio.new_event_loop()
        self.session: aiohttp.ClientSession | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.stack: list[dict] = []        # летящие посылки, свежая первой
        # Сжав кулак, руку разжимают, опуская её. Брокер своё забрать не даст,
        # но отвечать на каждый такой «жест» отказом — лишний шум в оверлее.
        self.palm_quiet_until = 0.0

        self.paused = False
        self.tray = Tray(
            on_toggle_pause=self._toggle_pause,
            on_quit=self._quit,
            status=self._status_text,
        )

    # --- из потока камеры -------------------------------------------------

    def _on_gesture(self, event: GestureEvent) -> None:
        if self.paused:
            log.debug("жест пропущен: приостановлено из трея")
            return
        self.loop.call_soon_threadsafe(
            lambda: self.loop.create_task(self._bid(event)))

    # --- управление из трея ----------------------------------------------

    def _status_text(self) -> str:
        if self.ws is None or self.ws.closed:
            return "нет связи с брокером"
        if self.paused:
            return "на связи, жесты приостановлены"
        if self.stack:
            return f"в воздухе посылок: {len(self.stack)}"
        return "на связи"

    def _toggle_pause(self) -> bool:
        self.paused = not self.paused
        log.info("жесты %s", "приостановлены" if self.paused else "возобновлены")
        self.overlay.show(Card(
            title="Жесты приостановлены" if self.paused else "Жесты снова ловятся",
            hint="переключается из значка в трее",
            accent=WARN if self.paused else ACCENT, ttl=3))
        return self.paused

    def _quit(self) -> None:
        log.info("выход по команде из трея")
        self.gestures.stop()
        self.content.stop()
        self.overlay.stop()

    # --- сеть -------------------------------------------------------------

    async def _send(self, **payload) -> None:
        if self.ws is None or self.ws.closed:
            self.overlay.show(Card(title="Нет связи с брокером",
                                   hint="жест пропущен", accent=WARN, ttl=3))
            return
        await self.ws.send_json(payload)

    async def _bid(self, event: GestureEvent) -> None:
        signals = {"hand": event.hand_area, "conf": event.confidence,
                   "idle": idle_seconds()}

        if event.name == FIST:
            if self.content.current.empty and not self.deep:
                self.overlay.show(Card(
                    title="Нечего захватывать",
                    hint="нужна вкладка, выделение или что-то в буфере",
                    accent=WARN, ttl=3))
                return
            await self._send(t="bid", role="grab", signals=signals)
        elif event.name == PALM:
            if time.monotonic() < self.palm_quiet_until:
                log.debug("ладонь пропущена: только что сами захватили")
                return
            if not self.stack:
                self.overlay.show(Card(title="В воздухе ничего нет",
                                       hint="сначала захвати кулаком",
                                       accent=WARN, ttl=2.5))
                return
            # id не передаём: ладонь всегда берёт верх стека, это решает брокер
            await self._send(t="bid", role="drop", signals=signals)

    # --- мы выиграли аукцион: снимаем и отправляем -------------------------

    async def _do_grab(self) -> None:
        grab = await asyncio.to_thread(self._snapshot)

        if grab.empty:
            self.overlay.show(Card(title="Нечего захватывать",
                                   hint="ни вкладки, ни выделения, ни буфера",
                                   accent=WARN, ttl=3))
            return

        if grab.kind == "url":
            await self._send(t="grab", kind="url",
                             payload={"url": grab.url, "title": grab.note},
                             preview=grab.preview())
            return

        if grab.kind == "text":
            await self._send(t="grab", kind="text",
                             payload={"text": grab.text},
                             preview=grab.preview())
            return

        if grab.kind == "image":
            blob = await self._upload_bytes(grab.image, "снимок.bmp")
            if blob is None:
                return
            await self._send(t="grab", kind="files",
                             payload={"files": [{"id": blob["id"]}]},
                             preview=grab.preview())
            return

        # файлы
        self.overlay.show(Card(title="Отправляю файлы",
                               hint=grab.preview(), accent=ACCENT))
        uploaded = []
        for path in grab.files:
            if not path.is_file():
                log.warning("пропускаю %s: это не файл", path)
                continue
            blob = await self._upload_file(path)
            if blob is None:
                return
            uploaded.append({"id": blob["id"]})

        if not uploaded:
            self.overlay.show(Card(title="Нечего отправлять",
                                   hint="в выделении не оказалось файлов",
                                   accent=WARN, ttl=3))
            return

        await self._send(t="grab", kind="files", payload={"files": uploaded},
                         preview=grab.preview())

    def _snapshot(self):
        """Выполняется в отдельном потоке: COM требует своей инициализации."""
        with auto.UIAutomationInitializerInThread():
            return self.content.snapshot(deep=self.deep)

    async def _upload(self, data, name: str) -> dict | None:
        assert self.session is not None
        url = f"{self.base}/blob"
        try:
            async with self.session.put(
                    url, params={"token": self.token, "name": name},
                    data=data) as resp:
                if resp.status != 200:
                    body = await resp.json(content_type=None)
                    reason = (body or {}).get("error", f"код {resp.status}")
                    self.overlay.show(Card(title="Файл не ушёл",
                                           hint=str(reason), accent=WARN, ttl=5))
                    return None
                return await resp.json()
        except aiohttp.ClientError as exc:
            log.warning("заливка сорвалась: %s", exc)
            self.overlay.show(Card(title="Файл не ушёл", hint=str(exc),
                                   accent=WARN, ttl=5))
            return None

    async def _upload_bytes(self, blob: bytes, name: str) -> dict | None:
        return await self._upload(blob, name)

    async def _upload_file(self, path: Path) -> dict | None:
        log.info("заливаю %s (%s)", path.name, _human(path.stat().st_size))

        async def chunks():
            # читаем в отдельном потоке, чтобы не морозить цикл событий
            with path.open("rb") as handle:
                while True:
                    piece = await asyncio.to_thread(handle.read, UPLOAD_CHUNK)
                    if not piece:
                        break
                    yield piece

        return await self._upload(chunks(), path.name)

    # --- приём ------------------------------------------------------------

    async def _accept(self, msg: dict) -> None:
        kind = msg.get("kind")
        payload = msg.get("payload") or {}

        if kind == "url" and payload.get("url"):
            log.info("открываю %s", payload["url"])
            open_url(payload["url"])
            self.overlay.show(Card(title="Принято, открываю",
                                   hint=str(msg.get("preview") or ""), ttl=4))
            return

        if kind == "text" and payload.get("text"):
            text = payload["text"]
            ok = await asyncio.to_thread(set_clipboard_text, text)
            self.overlay.show(Card(
                title="Текст в буфере обмена" if ok else "Не смог записать буфер",
                hint=f"{len(text)} символов — вставляй через Ctrl+V" if ok else "",
                extra=str(msg.get("preview") or ""),
                accent=ACCENT if ok else WARN, ttl=6))
            return

        if kind == "files":
            await self._download_files(payload.get("files") or [])
            return

        log.warning("непонятная посылка: %s", kind)

    async def _download_files(self, files: list[dict]) -> None:
        assert self.session is not None
        folder = downloads_dir()
        folder.mkdir(parents=True, exist_ok=True)

        saved: list[Path] = []
        for entry in files:
            blob_id, name = entry.get("id"), entry.get("name") or "file"
            if not blob_id:
                continue
            target = unique_path(folder, name)
            self.overlay.show(Card(title="Скачиваю", hint=name, accent=ACCENT))
            try:
                async with self.session.get(
                        f"{self.base}/blob/{blob_id}",
                        params={"token": self.token}) as resp:
                    if resp.status != 200:
                        raise aiohttp.ClientError(f"код {resp.status}")
                    with target.open("wb") as handle:
                        async for chunk in resp.content.iter_chunked(UPLOAD_CHUNK):
                            await asyncio.to_thread(handle.write, chunk)
            except (aiohttp.ClientError, OSError) as exc:
                target.unlink(missing_ok=True)
                log.warning("не скачался %s: %s", name, exc)
                self.overlay.show(Card(title="Файл не доехал", hint=str(exc),
                                       accent=WARN, ttl=6))
                return
            saved.append(target)
            log.info("сохранён %s", target)

        if not saved:
            return

        title = (saved[0].name if len(saved) == 1
                 else f"{len(saved)} файлов в «Загрузках»")
        self.overlay.show(Card(title="Принято", hint=title,
                               extra=str(folder), ttl=7))
        reveal_in_explorer(saved[0])

    # --- реакция на брокера ----------------------------------------------

    def _refresh_card(self) -> None:
        """Показывает верх стека — то, что заберёт ладонь."""
        if not self.stack:
            self.overlay.hide()
            return
        top = self.stack[0]
        extra = ""
        if len(self.stack) > 1:
            extra = (f"ниже в стеке ещё {len(self.stack) - 1} — "
                     "их можно забрать тапом в приёмнике")
        self.overlay.show(Card(
            title=f"{top.get('from', '?')} → {top.get('preview', '')}",
            hint="покажи ладонь, чтобы забрать",
            extra=extra, accent=ACCENT))

    async def _handle(self, msg: dict) -> None:
        kind = msg.get("t")

        self.tray.refresh()

        if kind == "welcome":
            self.stack = list(msg.get("stack") or [])
            peers = [p["name"] for p in msg.get("peers") or []]
            log.info("на связи: %s", ", ".join(peers) or "только я")
            self._refresh_card()

        elif kind == "you_grab":
            await self._do_grab()

        elif kind in ("pending", "stack"):
            self.stack = list(msg.get("stack") or [])
            self._refresh_card()

        elif kind == "grabbed":
            self.palm_quiet_until = time.monotonic() + PALM_QUIET_AFTER_GRAB
            self.overlay.show(Card(
                title="Захвачено",
                hint="покажи ладонь на другом устройстве",
                extra=str(msg.get("preview") or ""), ttl=6))

        elif kind == "deliver":
            self.stack = [p for p in self.stack if p.get("id") != msg.get("id")]
            await self._accept(msg)

        elif kind == "taken":
            self.stack = list(msg.get("stack") or [])
            self.overlay.show(Card(title=f"Улетело на {msg.get('by')}",
                                   hint="", ttl=3))

        elif kind == "lost":
            self.overlay.show(Card(
                title=f"Жест отдан {msg.get('to')}",
                hint="там рука была ближе к камере", accent=WARN, ttl=3))

        elif kind == "ignored":
            log.debug("жест отброшен: %s", msg.get("reason"))

        elif kind == "nothing_to_take":
            self.stack = []
            self.overlay.show(Card(title="Брать нечего",
                                   hint="посылку уже забрали",
                                   accent=WARN, ttl=2.5))

        elif kind == "hovering":
            self.stack = [p for p in self.stack if p.get("id") != msg.get("id")]
            self.overlay.show(Card(
                title="Посылка зависла в очереди",
                hint="ладонь не показали — забери тапом в приёмнике",
                extra=str(msg.get("preview") or ""), accent=WARN, ttl=6))
            self._refresh_card()

        elif kind in ("cancelled", "gone"):
            self.stack = [p for p in self.stack if p.get("id") != msg.get("id")]
            self._refresh_card()

        elif kind == "error":
            log.warning("брокер: %s", msg.get("reason"))

    async def _session_loop(self) -> None:
        ws_url = f"{self.base}/ws?token={self.token}"
        async with self.session.ws_connect(ws_url, heartbeat=25) as ws:
            self.ws = ws
            log.info("подключился к брокеру")
            await self._send(t="hello", name=self.name, device="pc",
                             caps=["grab_gesture", "drop_gesture",
                                   "open_url", "files", "clipboard"])
            async for raw in ws:
                if raw.type is aiohttp.WSMsgType.TEXT:
                    try:
                        await self._handle(raw.json())
                    except Exception:
                        log.exception("ошибка обработки сообщения")
        self.ws = None

    async def _network(self) -> None:
        delay = 1.0
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            self.session = session
            while True:
                try:
                    await self._session_loop()
                    delay = 1.0
                except aiohttp.WSServerHandshakeError as exc:
                    if exc.status == 403:
                        log.error("брокер отверг токен — проверь .token")
                        self.overlay.show(Card(
                            title="Брокер отверг токен",
                            hint="сверь .token на обеих сторонах", accent=WARN))
                        return
                    log.warning("рукопожатие не удалось: %s", exc)
                except (aiohttp.ClientError, OSError) as exc:
                    log.warning("связь потеряна: %s", exc)

                log.info("переподключение через %.0f с", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._network())

    # --- запуск -----------------------------------------------------------

    def run(self) -> None:
        threading.Thread(target=self.content.run, daemon=True,
                         name="content").start()
        threading.Thread(target=self._run_loop, daemon=True,
                         name="network").start()
        threading.Thread(target=self.gestures.run, daemon=True,
                         name="camera").start()

        self.tray.start()

        log.info("агент «%s» запущен; управление — значок в трее", self.name)
        try:
            self.overlay.run()           # блокирует главный поток
        except KeyboardInterrupt:
            pass
        finally:
            self.gestures.stop()
            self.content.stop()
            self.tray.stop()


def load_token(explicit: str | None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("BRIDGE_TOKEN")
    if env:
        return env.strip()
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            return token
    raise SystemExit(
        "Нет токена. Возьми его из вывода брокера и передай --token, "
        "положи в переменную BRIDGE_TOKEN или в файл .token")


def selftest(camera: int) -> None:
    """Окно с камерой и подписями: видно, ловятся ли кулак и ладонь."""
    logging.info("самопроверка: покажи кулак, потом ладонь. q — выход")

    def report(event: GestureEvent) -> None:
        logging.info("СРАБОТАЛО: %s (conf=%.2f, рука=%.3f кадра)",
                     event.name, event.confidence, event.hand_area)

    GestureWatcher(MODEL, report, camera, preview=True).run()


def what_would_fly(deep: bool) -> None:
    """Печатает, что улетело бы прямо сейчас. Для проверки захвата."""
    watcher = GrabWatcher()
    with auto.UIAutomationInitializerInThread():
        grab = watcher.snapshot(deep=deep)
    print(f"тип:      {grab.kind}")
    print(f"источник: {grab.note or '—'}")
    print(f"подпись:  {grab.preview()}")
    if grab.kind == "files":
        for path in grab.files:
            size = path.stat().st_size if path.is_file() else 0
            print(f"   - {path}  ({_human(size)})")
    elif grab.kind == "text":
        print(f"   длина: {len(grab.text or '')} символов")
    elif grab.kind == "image":
        print(f"   размер: {_human(len(grab.image or b''))}")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    parser = argparse.ArgumentParser(description="Агент Gesture Bridge")
    parser.add_argument("--broker", default="http://127.0.0.1:8787",
                        help="адрес брокера, например https://server.tailnet.ts.net")
    parser.add_argument("--token", default=None)
    parser.add_argument("--name", default=socket.gethostname())
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--selftest", action="store_true",
                        help="только камера и жесты, без брокера")
    parser.add_argument("--what", action="store_true",
                        help="показать, что улетело бы прямо сейчас")
    parser.add_argument("--no-deep", action="store_true",
                        help="не использовать Ctrl+C как последний резерв")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")

    if args.what:
        what_would_fly(deep=not args.no_deep)
        return

    if not MODEL.exists():
        raise SystemExit(f"нет модели {MODEL}. Скачай её командой из README")

    if args.selftest:
        selftest(args.camera)
        return

    Agent(args.broker, load_token(args.token), args.name,
          args.camera, deep=not args.no_deep).run()


if __name__ == "__main__":
    main()
