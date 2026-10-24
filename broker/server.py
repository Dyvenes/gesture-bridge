"""Брокер Gesture Bridge.

Запускается на сервере (или на ПК — всё равно). Делает три вещи:
  1. держит WebSocket со всеми устройствами и разыгрывает жесты на аукционе;
  2. хранит стек посылок и висящую очередь;
  3. раздаёт статику веб-приёмника для телефона.

Транспорт и шифрование — на Tailscale: брокер слушает обычный HTTP, а TLS
(он обязателен для доступа к камере в браузере) навешивает `tailscale serve`.
См. README.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import sys
import uuid
from pathlib import Path
from urllib.parse import quote

from aiohttp import WSMsgType, web

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from broker.arbiter import Arbiter, Bid, score_of          # noqa: E402
from broker.blobs import BlobStore, BlobTooLarge, StoreFull  # noqa: E402
from broker.parcels import Parcel, ParcelStore             # noqa: E402

log = logging.getLogger("bridge.broker")

RECEIVER_DIR = ROOT / "receiver"
TOKEN_FILE = ROOT / ".token"
SPOOL_DIR = ROOT / "spool"

KINDS = {"url", "text", "files"}


# --------------------------------------------------------------------------
# клиенты
# --------------------------------------------------------------------------

class Client:
    def __init__(self, ws: web.WebSocketResponse) -> None:
        self.ws = ws
        self.id = uuid.uuid4().hex[:8]
        self.name = "unknown"
        self.device = "unknown"          # "pc" | "phone"
        self.caps: set[str] = set()

    def can_receive(self) -> bool:
        return bool({"drop_gesture", "open_url"} & self.caps)

    def info(self) -> dict:
        return {"id": self.id, "name": self.name,
                "device": self.device, "caps": sorted(self.caps)}

    async def send(self, **payload) -> None:
        if self.ws.closed:
            return
        try:
            await self.ws.send_json(payload)
        except (ConnectionResetError, RuntimeError):
            pass


# --------------------------------------------------------------------------
# брокер
# --------------------------------------------------------------------------

class Broker:
    def __init__(self, token: str, blobs: BlobStore) -> None:
        self.token = token
        self.blobs = blobs
        self.clients: dict[str, Client] = {}
        self.parcels = ParcelStore()
        self.arbiter = Arbiter()

    # --- рассылка ---------------------------------------------------------

    async def broadcast(self, *, exclude: str | None = None,
                        only_receivers: bool = False, **payload) -> None:
        targets = [c for c in self.clients.values()
                   if c.id != exclude and (not only_receivers or c.can_receive())]
        await asyncio.gather(*(c.send(**payload) for c in targets),
                             return_exceptions=True)

    async def announce_peers(self) -> None:
        await self.broadcast(t="peers",
                             peers=[c.info() for c in self.clients.values()])

    # --- вручение посылки -------------------------------------------------

    async def _create_parcel(self, sender: Client, kind: str,
                             payload: dict, preview: str) -> None:
        parcel = self.parcels.add(Parcel(
            kind=kind, payload=payload, preview=preview[:200],
            origin=sender.id, origin_name=sender.name,
        ))
        log.info("захват от %s: %s", sender.name, parcel.preview)

        await sender.send(t="grabbed", id=parcel.id, preview=parcel.preview)
        # Приёмники получают весь стек, а не только новую посылку: так оверлей
        # на ПК может показать, что именно возьмёт ладонь, и что под ней.
        await self.broadcast(
            exclude=sender.id, only_receivers=True,
            t="pending", id=parcel.id, kind=parcel.kind,
            preview=parcel.preview, sender=parcel.origin_name,
            ttl=int(parcel.expires_at - parcel.created),
            stack=[p.summary() for p in self.parcels.flying()],
        )

    async def _deliver(self, taker: Client, parcel: Parcel) -> None:
        log.info("сброс у %s: %s", taker.name, parcel.preview)
        await taker.send(t="deliver", id=parcel.id, kind=parcel.kind,
                         payload=parcel.payload, preview=parcel.preview)
        await self.broadcast(exclude=taker.id, t="taken", id=parcel.id,
                             by=taker.name,
                             stack=[p.summary() for p in self.parcels.flying()])

    # --- обработчики ------------------------------------------------------

    async def on_hello(self, client: Client, msg: dict) -> None:
        client.name = str(msg.get("name") or "device")[:40]
        client.device = str(msg.get("device") or "unknown")[:16]
        client.caps = {str(c)[:24] for c in (msg.get("caps") or [])}
        log.info("подключился %s (%s) caps=%s",
                 client.name, client.device, sorted(client.caps))

        await client.send(t="welcome", me=client.info(),
                          peers=[c.info() for c in self.clients.values()],
                          stack=[p.summary() for p in self.parcels.flying()],
                          queue=[p.summary() for p in self.parcels.hovering()])
        await self.announce_peers()

    async def on_grab(self, client: Client, msg: dict) -> None:
        """Создание посылки.

        Сюда приходят двумя путями: явным (Share Sheet на телефоне, кнопка)
        и после победы в аукционе жестов. Во втором случае устройство уже
        получило `you_grab`, успело снять содержимое и залить файлы.
        """
        kind = str(msg.get("kind") or "url")
        payload = msg.get("payload") or {}

        if kind not in KINDS or not isinstance(payload, dict):
            await client.send(t="error", reason=f"неизвестный тип посылки: {kind}")
            return

        if kind == "url" and not payload.get("url"):
            await client.send(t="error", reason="пустой payload.url")
            return
        if kind == "text" and not (payload.get("text") or "").strip():
            await client.send(t="error", reason="пустой payload.text")
            return
        if kind == "files":
            files = payload.get("files")
            if not isinstance(files, list) or not files:
                await client.send(t="error", reason="пустой список файлов")
                return
            # Принимаем только те id, что реально лежат у нас: иначе посылка
            # обещала бы приёмнику файл, которого нет.
            resolved = []
            for entry in files:
                blob = (self.blobs.get(str(entry.get("id")))
                        if isinstance(entry, dict) else None)
                if blob is None:
                    await client.send(t="error",
                                      reason="файл не залит или уже удалён")
                    return
                resolved.append(blob.summary())
            payload = {"files": resolved}

        await self._create_parcel(client, kind, payload,
                                  str(msg.get("preview") or kind))

    async def on_bid(self, client: Client, msg: dict) -> None:
        """Жест, увиденный камерой: идём на аукцион, а не действуем сразу."""
        role = str(msg.get("role") or "")
        signals = msg.get("signals") or {}
        if not isinstance(signals, dict):
            signals = {}

        # Ладонь всегда берёт верх стека — самую свежую посылку.
        # Жестом выбрать источник нельзя, поэтому и не пытаемся: оверлей
        # показывает, что именно улетит, а выбор из нескольких — это тап.
        target: Parcel | None = None
        if role == "drop":
            explicit = str(msg.get("id") or "")
            # Свои посылки отсеиваем до аукциона, а не после: иначе
            # отправитель с самой близкой рукой выиграл бы торг и заблокировал
            # устройство, которому посылка предназначалась.
            target = (self.parcels.get(explicit) if explicit
                      else self.parcels.newest_flying(exclude_origin=client.id))
            if (target is None or target.claimed_by is not None
                    or target.origin == client.id):
                await client.send(t="nothing_to_take")
                return
            key = f"drop:{target.id}"
        elif role == "grab":
            key = "grab"
        else:
            await client.send(t="error", reason=f"неизвестная роль ставки: {role}")
            return

        bid = Bid(client_id=client.id, client_name=client.name,
                  score=score_of(signals), signals=signals)

        winner = await self.arbiter.bid(key, bid)

        if winner is None:
            await client.send(t="ignored", role=role,
                              reason="жест слишком слабый, похоже на случайный")
            return
        if winner.client_id != client.id:
            await client.send(t="lost", role=role, to=winner.client_name)
            return

        if role == "grab":
            # Снимать содержимое идём только победителем. Проигравшим это
            # стоило бы Ctrl+C в чужом окне и заливки файлов впустую —
            # поэтому захват двухфазный: сначала выигрываем, потом берём.
            await client.send(t="you_grab")
        else:
            assert target is not None
            parcel = self.parcels.claim(target.id, client.id)
            if parcel is None:
                await client.send(t="nothing_to_take")
                return
            await self._deliver(client, parcel)

    async def on_claim(self, client: Client, msg: dict) -> None:
        """Забрать посылку из очереди руками — тап по списку в приёмнике."""
        parcel_id = str(msg.get("id") or "")
        parcel = self.parcels.claim(parcel_id, client.id)
        if parcel is None:
            await client.send(t="nothing_to_take", id=parcel_id)
            return
        await self._deliver(client, parcel)

    async def on_cancel(self, client: Client, msg: dict) -> None:
        parcel_id = str(msg.get("id") or "")
        parcel = self.parcels.get(parcel_id)
        if parcel and parcel.origin == client.id:
            self.parcels.drop(parcel_id)
            await self.broadcast(t="cancelled", id=parcel_id)

    async def on_queue(self, client: Client, _msg: dict) -> None:
        await client.send(t="queue",
                          items=[p.summary() for p in self.parcels.hovering()])

    # --- служебный цикл ---------------------------------------------------

    async def sweeper(self) -> None:
        ticks = 0
        while True:
            await asyncio.sleep(1.0)
            became_hovering, removed = self.parcels.sweep()

            for parcel in became_hovering:
                log.info("посылка %s зависла в очереди", parcel.id)
                await self.broadcast(t="hovering", **parcel.summary())
            if became_hovering or removed:
                await self.broadcast(
                    t="stack",
                    stack=[p.summary() for p in self.parcels.flying()])
            for parcel in removed:
                # файлы живут ровно столько же, сколько посылка
                self.blobs.drop_many(parcel.blob_ids)
                await self.broadcast(t="gone", id=parcel.id)

            ticks += 1
            if ticks % 300 == 0:
                # залили файл, а посылку так и не создали — такое бывает,
                # если отправитель отвалился между заливкой и grab
                orphans = self.blobs.sweep_orphans()
                if orphans:
                    log.info("удалено осиротевших файлов: %d", orphans)


# --------------------------------------------------------------------------
# HTTP / WebSocket
# --------------------------------------------------------------------------

HANDLERS = {
    "hello": Broker.on_hello,
    "grab": Broker.on_grab,
    "bid": Broker.on_bid,
    "claim": Broker.on_claim,
    "cancel": Broker.on_cancel,
    "queue": Broker.on_queue,
}


def authorized(request: web.Request) -> bool:
    """Токен из query-параметра или заголовка.

    compare_digest на str падает с TypeError, если в строке есть не-ASCII,
    поэтому ловим: иначе подделанный запрос ронял бы обработчик в 500
    вместо честного отказа.
    """
    broker: Broker = request.app["broker"]
    supplied = request.query.get("token") or request.headers.get("X-Bridge-Token", "")
    try:
        return secrets.compare_digest(supplied, broker.token)
    except TypeError:
        return False


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    broker: Broker = request.app["broker"]

    if not authorized(request):
        log.warning("отказ: неверный токен с %s", request.remote)
        raise web.HTTPForbidden(text="bad token")

    ws = web.WebSocketResponse(heartbeat=25.0)
    await ws.prepare(request)

    client = Client(ws)
    broker.clients[client.id] = client

    try:
        async for msg in ws:
            if msg.type is not WSMsgType.TEXT:
                continue
            try:
                data = msg.json()
            except ValueError:
                await client.send(t="error", reason="невалидный JSON")
                continue
            if not isinstance(data, dict):
                continue

            handler = HANDLERS.get(str(data.get("t") or ""))
            if handler is None:
                await client.send(t="error",
                                  reason=f"неизвестный тип: {data.get('t')}")
                continue
            try:
                await handler(broker, client, data)
            except Exception:
                log.exception("ошибка в обработчике %s", data.get("t"))
                await client.send(t="error", reason="внутренняя ошибка брокера")
    finally:
        broker.clients.pop(client.id, None)
        log.info("отключился %s", client.name)
        await broker.announce_peers()

    return ws


async def health(request: web.Request) -> web.Response:
    """Живость — всем, подробности — только по токену.

    Под `tailscale funnel` этот эндпоинт виден всему интернету, а имена
    устройств и число летающих посылок — уже разведданные о доме. Голое
    `{"ok": true}` без токена оставлено намеренно: его хватает мониторингу
    и systemd, и оно не рассказывает ничего.
    """
    broker: Broker = request.app["broker"]
    if not authorized(request):
        return web.json_response({"ok": True})

    return web.json_response({
        "ok": True,
        "clients": [c.info() for c in broker.clients.values()],
        "flying": len(broker.parcels.flying()),
        "hovering": len(broker.parcels.hovering()),
    })


async def blob_upload(request: web.Request) -> web.Response:
    """Заливка файла посылки. Тело запроса — сырые байты, имя в query."""
    if not authorized(request):
        raise web.HTTPForbidden(text="bad token")

    broker: Broker = request.app["broker"]
    name = request.query.get("name") or "file"
    try:
        blob = await broker.blobs.save(
            request.content.iter_chunked(64 * 1024), name)
    except BlobTooLarge as exc:
        return web.json_response({"error": str(exc)}, status=413)
    except StoreFull as exc:
        return web.json_response({"error": str(exc)}, status=507)
    return web.json_response(blob.summary())


async def blob_download(request: web.Request) -> web.StreamResponse:
    if not authorized(request):
        raise web.HTTPForbidden(text="bad token")

    broker: Broker = request.app["broker"]
    blob = broker.blobs.get(request.match_info["blob_id"])
    if blob is None:
        raise web.HTTPNotFound(text="файл не найден или уже удалён")

    path = broker.blobs.path(blob.id)
    if not path.exists():
        raise web.HTTPNotFound(text="файл не найден на диске")

    # filename* с UTF-8: иначе кириллица и эмодзи в имени приедут мусором
    disposition = f"attachment; filename*=UTF-8''{quote(blob.name)}"
    return web.FileResponse(path, headers={
        "Content-Disposition": disposition,
        "Content-Type": "application/octet-stream",
    })


async def index(request: web.Request) -> web.FileResponse:
    """Отдаёт приёмник. Тот же файл обслуживает /share — цель Web Share Target."""
    return web.FileResponse(RECEIVER_DIR / "index.html")


async def service_worker(request: web.Request) -> web.FileResponse:
    return web.FileResponse(
        RECEIVER_DIR / "sw.js",
        headers={"Content-Type": "application/javascript",
                 "Service-Worker-Allowed": "/"})


def load_token() -> str:
    env = os.environ.get("BRIDGE_TOKEN")
    if env:
        return env.strip()
    if TOKEN_FILE.exists():
        existing = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(18)
    TOKEN_FILE.write_text(token, encoding="utf-8")
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    return token


async def on_startup(app: web.Application) -> None:
    app["sweeper"] = asyncio.create_task(app["broker"].sweeper())


async def on_cleanup(app: web.Application) -> None:
    app["sweeper"].cancel()


def build_app(token: str, spool: Path | None = None) -> web.Application:
    target = spool or SPOOL_DIR
    try:
        blobs = BlobStore(target)
    except OSError as exc:
        # Самый частый случай — systemd с ProtectSystem=strict: диск только
        # для чтения, и папку под файлы посылок создать нечем. Голый
        # трейсбек тут ничего не объясняет, поэтому говорим прямо.
        raise SystemExit(
            f"не удалось подготовить папку для файлов {target}: {exc}\n"
            "Если брокер работает службой — проверь, что в юните есть "
            "StateDirectory=gesture-bridge и --spool указывает внутрь "
            "/var/lib/gesture-bridge.") from exc
    # Файлы приходят потоком, но aiohttp по умолчанию рубит тело на 1 МиБ.
    app = web.Application(client_max_size=blobs.max_file + 1024 * 1024)
    app["broker"] = Broker(token, blobs)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/health", health)
    app.router.add_put("/blob", blob_upload)
    app.router.add_get("/blob/{blob_id}", blob_download)
    app.router.add_get("/", index)
    app.router.add_get("/share", index)
    # Service worker обязан лежать в корне: его область действия — это его
    # каталог, а из /static/ он не покрыл бы "/" и установка PWA сорвалась бы.
    app.router.add_get("/sw.js", service_worker)
    app.router.add_static("/static", RECEIVER_DIR)
    # Модель раздаём сами, а не с чужого CDN: телефон тянет её по Tailscale,
    # это быстрее и не зависит от внешней сети.
    app.router.add_static("/models", ROOT / "models")
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    # Консоль Windows по умолчанию не UTF-8, а логи у нас русские.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    parser = argparse.ArgumentParser(description="Брокер Gesture Bridge")
    parser.add_argument("--host", default="0.0.0.0",
                        help="интерфейс; за tailscale serve хватит 127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    # systemd с StateDirectory= кладёт в окружение путь к записываемой папке.
    # Берём его по умолчанию: тогда юнит работает и без явного --spool.
    # Каталогов может быть несколько через разделитель путей — os.pathsep,
    # а не двоеточие: на Windows двоеточие разрезало бы букву диска.
    state_dir = os.environ.get("STATE_DIRECTORY")
    default_spool = (Path(state_dir.split(os.pathsep)[0]) / "spool"
                     if state_dir else SPOOL_DIR)
    parser.add_argument("--spool", default=str(default_spool),
                        help="куда складывать файлы посылок")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")

    token = load_token()
    # Приложение собираем до того, как рапортовать о готовности: иначе в
    # логе падения остаётся бодрое «слушаю», которого не было.
    app = build_app(token, Path(args.spool))

    log.info("токен: %s", token)
    log.info("файлы посылок: %s", args.spool)
    log.info("слушаю http://%s:%d", args.host, args.port)

    web.run_app(app, host=args.host, port=args.port,
                print=None, access_log=None)


if __name__ == "__main__":
    main()
