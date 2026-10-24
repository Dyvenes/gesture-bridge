"""Приём состояния вкладки от расширения браузера.

Зачем это вообще: адресная строка врёт. На сайтах с сериями переключение
идёт через history.pushState, и в строке остаётся адрес первой серии.
Позиции воспроизведения там не бывает никогда. Читая строку через UI
Automation, агент честно переносил не то, что человек смотрит.

Расширение сообщает настоящий адрес и секунду, а этот модуль слушает его
на localhost. Если расширение не установлено, ничего не ломается: агент
просто продолжает читать адресную строку, как раньше.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from aiohttp import WSMsgType, web

log = logging.getLogger("bridge.browser")

DEFAULT_PORT = 8799
STALE_AFTER = 4.0      # сек; дольше — считаем, что расширение молчит
MIN_TIMECODE = 5       # до этого переносить позицию бессмысленно


@dataclass
class TabState:
    url: str
    title: str
    seconds: int
    duration: int
    playing: bool
    at: float

    @property
    def fresh(self) -> bool:
        return time.time() - self.at <= STALE_AFTER


def with_timecode(url: str, seconds: int) -> str:
    """Вшивает позицию воспроизведения в ссылку.

    У YouTube свой формат (`t=90s`), у остальных работает голое число.
    Параметр `t` понимают многие плееры, а те, кто не понимает, просто
    его игнорируют — потерять секунду обиднее, чем передать лишний
    параметр.
    """
    if seconds < MIN_TIMECODE:
        return url

    parsed = urlparse(url)
    host = parsed.netloc.lower()
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))

    if "youtube.com" in host:
        query["t"] = f"{seconds}s"
    else:
        query["t"] = str(seconds)

    return urlunparse(parsed._replace(query=urlencode(query)))


class BrowserLink:
    """Маленький сервер на localhost, которому пишет расширение."""

    def __init__(self, port: int = DEFAULT_PORT) -> None:
        self.port = port
        self._lock = threading.Lock()
        self._state: TabState | None = None
        self._runner: web.AppRunner | None = None

    @property
    def state(self) -> TabState | None:
        """Последнее состояние вкладки, если оно не протухло."""
        with self._lock:
            state = self._state
        return state if state is not None and state.fresh else None

    @property
    def connected(self) -> bool:
        return self.state is not None

    # --- сервер ----------------------------------------------------------

    async def _handler(self, request: web.Request) -> web.WebSocketResponse:
        # Пускаем только расширение: обычная страница сюда не достучится
        # из-за смешанного содержимого, но проверка стоит дёшево.
        origin = request.headers.get("Origin", "")
        if origin and not origin.startswith("chrome-extension://"):
            log.warning("отказ постороннему источнику: %s", origin)
            raise web.HTTPForbidden(text="not an extension")

        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        log.info("расширение браузера подключилось")

        try:
            async for message in ws:
                if message.type is not WSMsgType.TEXT:
                    continue
                try:
                    data = message.json()
                except ValueError:
                    continue
                if not isinstance(data, dict) or not data.get("url"):
                    continue

                state = TabState(
                    url=str(data["url"]),
                    title=str(data.get("title") or ""),
                    seconds=int(data.get("seconds") or 0),
                    duration=int(data.get("duration") or 0),
                    playing=bool(data.get("playing")),
                    at=time.time(),
                )
                with self._lock:
                    self._state = state
        finally:
            log.info("расширение браузера отключилось")

        return ws

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/ext", self._handler)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        # Только петля: наружу этот порт выставлять незачем.
        site = web.TCPSite(self._runner, "127.0.0.1", self.port)
        await site.start()
        log.info("жду расширение на ws://127.0.0.1:%d/ext", self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
