"""Значок агента в системном трее Windows.

Агент задуман фоновым: он не должен держать открытое окно консоли и не
должен требовать внимания. Значок в трее — единственное место, где он
виден, и единственное место, откуда его можно остановить.

Пункт «Приостановить» не выключает связь с брокером: он только перестаёт
слушать жесты. Так можно убрать реакцию камеры на время созвона, не
теряя возможности принимать посылки из очереди.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

import pystray
from PIL import Image

log = logging.getLogger("bridge.tray")

ICON = Path(__file__).resolve().parent.parent / "receiver" / "icons" / "icon-192.png"


class Tray:
    def __init__(self, *, on_toggle_pause: Callable[[], bool],
                 on_quit: Callable[[], None],
                 status: Callable[[], str]) -> None:
        self._on_toggle_pause = on_toggle_pause
        self._on_quit = on_quit
        self._status = status
        self._paused = False
        self._icon: pystray.Icon | None = None

    # --- пункты меню ------------------------------------------------------

    def _status_text(self, _item) -> str:
        return self._status()

    def _pause_text(self, _item) -> str:
        return "Возобновить жесты" if self._paused else "Приостановить жесты"

    def _toggle_pause(self, icon, _item) -> None:
        self._paused = self._on_toggle_pause()
        icon.update_menu()

    def _quit(self, icon, _item) -> None:
        icon.visible = False
        icon.stop()
        self._on_quit()

    # --- жизненный цикл ---------------------------------------------------

    def _build(self) -> pystray.Icon:
        image = Image.open(ICON)
        menu = pystray.Menu(
            pystray.MenuItem(self._status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self._pause_text, self._toggle_pause),
            pystray.MenuItem("Выход", self._quit),
        )
        return pystray.Icon("gesture-bridge", image, "Gesture Bridge", menu)

    def start(self) -> None:
        """Поднимает значок в своём потоке: главный занят оверлеем."""
        def run() -> None:
            try:
                self._icon = self._build()
                self._icon.run()
            except Exception:
                log.exception("значок в трее не поднялся")

        threading.Thread(target=run, daemon=True, name="tray").start()

    def refresh(self) -> None:
        """Перерисовать подсказку и меню после смены состояния."""
        icon = self._icon
        if icon is None:
            return
        try:
            icon.title = f"Gesture Bridge — {self._status()}"
            icon.update_menu()
        except Exception:
            log.debug("не удалось обновить значок", exc_info=True)

    def stop(self) -> None:
        icon = self._icon
        if icon is None:
            return
        try:
            icon.visible = False
            icon.stop()
        except Exception:
            log.debug("значок не остановился", exc_info=True)
