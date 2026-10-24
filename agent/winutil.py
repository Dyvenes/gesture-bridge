"""Windows-специфика агента: активное окно, URL из браузера, простой ввод.

URL берётся из адресной строки через UI Automation. Это компромисс MVP:
работает без расширения к браузеру, но зависит от того, что адресная
строка — первый Edit-контрол в окне. Для Chromium и Firefox это так.
Надёжный путь — своё расширение на WebSocket; см. README, раздел «Дальше».
"""

from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

import uiautomation as auto

log = logging.getLogger("bridge.win")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Chromium ставит в класс окна своё имя: Chrome_WidgetWin_1, но у форков
# бывает иначе — у Яндекс.Браузера это Chrome_Yandex_WidgetWin_1.
# Поэтому сопоставляем по фрагментам, а не по точному совпадению.
_CHROMIUM_MARKERS = ("chrome_", "widgetwin")
_FIREFOX_CLASS = "mozillawindowclass"


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_seconds() -> float:
    """Сколько секунд не было ввода мышью или клавиатурой.

    Нужно для счёта на аукционе: машина, за которой человек реально
    работает, должна выигрывать у той, что просто стоит рядом.
    """
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 1e9
    return max(0.0, (kernel32.GetTickCount() - info.dwTime) / 1000.0)


@dataclass
class Window:
    hwnd: int
    cls: str
    title: str

    @property
    def is_browser(self) -> bool:
        low = self.cls.lower()
        if low == _FIREFOX_CLASS:
            return True
        return all(marker in low for marker in _CHROMIUM_MARKERS)


def foreground() -> Window:
    hwnd = user32.GetForegroundWindow()
    title = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, title, 512)
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    return Window(hwnd=int(hwnd), cls=cls.value, title=title.value)


class UrlReader:
    """Читает URL из адресной строки, кэшируя найденный элемент.

    Поиск контрола стоит ~130 мс, перечитывание значения — доли миллисекунды.
    Поэтому ищем один раз на окно, дальше только читаем: ставка на аукционе
    должна уходить мгновенно, а не через полсекунды.
    """

    def __init__(self) -> None:
        self._hwnd: int | None = None
        self._pattern = None

    def _attach(self, hwnd: int) -> None:
        self._hwnd = hwnd
        self._pattern = None
        try:
            window = auto.ControlFromHandle(hwnd)
            if window is None:
                return
            edit = window.EditControl(searchDepth=10, foundIndex=1)
            if edit.Exists(1.0, 0.1):
                self._pattern = edit.GetValuePattern()
        except Exception as exc:
            log.debug("не удалось зацепиться за адресную строку: %s", exc)

    def read(self, window: Window) -> str | None:
        if not window.is_browser:
            return None
        if window.hwnd != self._hwnd or self._pattern is None:
            self._attach(window.hwnd)
        if self._pattern is None:
            return None
        try:
            value = (self._pattern.Value or "").strip()
        except Exception:
            # окно закрылось или перестроило дерево — перецепимся в следующий раз
            self._pattern = None
            return None
        return _normalize_url(value)


def _normalize_url(value: str) -> str | None:
    """Адресная строка показывает URL без схемы, а иногда и поисковый запрос."""
    if not value or " " in value.strip():
        return None
    if value.startswith(("http://", "https://")):
        return value
    if "." in value.split("/")[0]:
        return "https://" + value
    return None


def strip_browser_suffix(title: str) -> str:
    """Chromium дописывает к заголовку имя браузера — отрезаем хвост."""
    for tail in (" — Яндекс Браузер", " - Google Chrome",
                 " — Mozilla Firefox", " - Microsoft Edge"):
        if title.endswith(tail):
            return title[: -len(tail)].strip()
    return title.strip()


def open_url(url: str) -> None:
    """Открывает ссылку в браузере по умолчанию."""
    import webbrowser
    webbrowser.open(url, new=2, autoraise=True)


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


_FOLDERID_DOWNLOADS = _GUID(
    0x374DE290, 0x123F, 0x4565,
    (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))


def downloads_dir() -> Path:
    """Папка «Загрузки» — та, что настроена в системе.

    Через ~/Downloads нельзя: путь локализован, да и пользователь мог
    перенести папку на другой диск.
    """
    pointer = ctypes.c_wchar_p()
    result = ctypes.windll.shell32.SHGetKnownFolderPath(
        ctypes.byref(_FOLDERID_DOWNLOADS), 0, None, ctypes.byref(pointer))
    if result == 0 and pointer.value:
        try:
            return Path(pointer.value)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(pointer)
    return Path.home() / "Downloads"


def reveal_in_explorer(path: Path) -> None:
    """Открывает проводник с выделенным файлом."""
    import subprocess
    try:
        subprocess.Popen(["explorer", "/select,", str(path)])
    except OSError as exc:
        log.debug("не удалось открыть проводник: %s", exc)


def unique_path(folder: Path, name: str) -> Path:
    """Путь, который точно не затрёт существующий файл."""
    candidate = folder / name
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for index in range(2, 1000):
        candidate = folder / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    return folder / f"{stem}-{int(time.time())}{suffix}"
