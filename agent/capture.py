"""Что именно улетит при захвате: ссылка, файлы, текст или картинка.

Порядок попыток выстроен от самого точного к самому общему, и первые три
шага вообще не имеют побочных эффектов:

  1. браузер в фокусе        -> URL активной вкладки
  2. проводник в фокусе      -> выделенные файлы, через Shell COM
  3. выделенный текст        -> UI Automation, TextPattern.GetSelection
  4. буфер обмена как есть   -> файлы, текст или картинка
  5. Ctrl+C и посмотреть     -> последний резерв, буфер сохраняем и возвращаем

Пятый шаг существует ради приложений, которые не отдают выделение через
UI Automation, и ради выделенной области в графических редакторах —
её нельзя получить ничем, кроме копирования.
"""

from __future__ import annotations

import ctypes
import logging
import struct
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import comtypes.client
import uiautomation as auto

from agent.browser_link import TabState, with_timecode
from agent.winutil import UrlReader, Window, foreground, strip_browser_suffix

log = logging.getLogger("bridge.capture")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32

# Без явных сигнатур ctypes считает, что функция возвращает int — то есть
# 32 бита, — и дескриптор буфера обмена на 64-битной системе обрезается.
# Проявляется это не тихой ошибкой, а OverflowError на следующем вызове.
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
user32.EnumClipboardFormats.restype = wintypes.UINT

kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
kernel32.GlobalSize.argtypes = [wintypes.HANDLE]
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HANDLE
kernel32.GlobalFree.argtypes = [wintypes.HANDLE]
kernel32.GlobalFree.restype = wintypes.HANDLE

shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                   wintypes.LPWSTR, wintypes.UINT]
shell32.DragQueryFileW.restype = wintypes.UINT

CF_TEXT = 1
CF_DIB = 8
CF_UNICODETEXT = 13
CF_HDROP = 15

MAX_INLINE_TEXT = 256 * 1024     # больше — поедет файлом, а не строкой
VK_CONTROL = 0x11
VK_C = 0x43
KEYEVENTF_KEYUP = 0x0002


# --------------------------------------------------------------------------
# буфер обмена
# --------------------------------------------------------------------------

class Clipboard:
    """Контекстный менеджер поверх буфера: его может держать другой процесс."""

    def __enter__(self) -> "Clipboard":
        for _ in range(10):
            if user32.OpenClipboard(None):
                return self
            time.sleep(0.02)
        raise OSError("буфер обмена занят другим процессом")

    def __exit__(self, *_exc) -> None:
        user32.CloseClipboard()

    @staticmethod
    def formats() -> list[int]:
        found, fmt = [], 0
        while True:
            fmt = user32.EnumClipboardFormats(fmt)
            if not fmt:
                return found
            found.append(fmt)

    @classmethod
    def text(cls) -> str | None:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.c_wchar_p(pointer).value
        finally:
            kernel32.GlobalUnlock(handle)

    @classmethod
    def files(cls) -> list[Path]:
        handle = user32.GetClipboardData(CF_HDROP)
        if not handle:
            return []
        count = shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
        paths: list[Path] = []
        for index in range(count):
            length = shell32.DragQueryFileW(handle, index, None, 0)
            buffer = ctypes.create_unicode_buffer(length + 1)
            shell32.DragQueryFileW(handle, index, buffer, length + 1)
            paths.append(Path(buffer.value))
        return paths

    @classmethod
    def image_bmp(cls) -> bytes | None:
        """CF_DIB -> готовый BMP: не хватает только файлового заголовка."""
        handle = user32.GetClipboardData(CF_DIB)
        if not handle:
            return None
        size = kernel32.GlobalSize(handle)
        pointer = kernel32.GlobalLock(handle)
        if not pointer or size < 40:
            return None
        try:
            dib = ctypes.string_at(pointer, size)
        finally:
            kernel32.GlobalUnlock(handle)

        header_size, _w, _h, _planes, bits = struct.unpack("<IiiHH", dib[:16])
        palette = 0
        if bits <= 8:
            used = struct.unpack("<I", dib[32:36])[0]
            palette = (used or (1 << bits)) * 4
        offset = 14 + header_size + palette
        return b"BM" + struct.pack("<IHHI", 14 + size, 0, 0, offset) + dib


def set_clipboard_text(value: str) -> bool:
    """Кладёт текст в буфер. Используется и при приёме посылки."""
    data = ctypes.create_unicode_buffer(value)
    size = ctypes.sizeof(data)
    handle = kernel32.GlobalAlloc(0x0042, size)   # GMEM_MOVEABLE|GMEM_ZEROINIT
    if not handle:
        return False
    pointer = kernel32.GlobalLock(handle)
    ctypes.memmove(pointer, data, size)
    kernel32.GlobalUnlock(handle)
    try:
        with Clipboard():
            user32.EmptyClipboard()
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                kernel32.GlobalFree(handle)
                return False
        return True
    except OSError:
        kernel32.GlobalFree(handle)
        return False


def _press_copy() -> None:
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_C, 0, 0, 0)
    user32.keybd_event(VK_C, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


# --------------------------------------------------------------------------
# проводник
# --------------------------------------------------------------------------

def explorer_selection(hwnd: int) -> list[Path]:
    """Выделенные файлы именно в активном окне проводника.

    Сопоставляем по HWND: открытых окон может быть несколько, и брать
    выделение из неактивного — значит отправить не то.
    """
    try:
        shell = comtypes.client.CreateObject("Shell.Application")
        windows = shell.Windows()
        for index in range(windows.Count):
            window = windows.Item(index)
            try:
                if int(window.HWND) != hwnd or not window.LocationURL:
                    continue
                selected = window.Document.SelectedItems()
            except Exception:
                continue
            paths = []
            for i in range(selected.Count):
                try:
                    paths.append(Path(selected.Item(i).Path))
                except Exception:
                    continue
            return paths
    except Exception as exc:
        log.debug("проводник не опрошен: %s", exc)
    return []


# --------------------------------------------------------------------------
# выделенный текст
# --------------------------------------------------------------------------

def selected_text() -> str | None:
    """Выделение через UI Automation — не трогая буфер обмена."""
    try:
        control = auto.GetFocusedControl()
        if control is None:
            return None
        pattern = control.GetPattern(auto.PatternId.TextPattern)
        if not pattern:
            return None
        ranges = pattern.GetSelection()
        if not ranges:
            return None
        chunks = []
        for index in range(len(ranges)):
            try:
                chunks.append(ranges[index].GetText(-1))
            except Exception:
                continue
        text = "".join(chunks).strip()
        return text or None
    except Exception as exc:
        log.debug("UIA не дал выделение: %s", exc)
        return None


# --------------------------------------------------------------------------
# результат захвата
# --------------------------------------------------------------------------

@dataclass
class Grab:
    kind: str                       # "url" | "files" | "text" | "image" | "none"
    url: str | None = None
    text: str | None = None
    files: list[Path] = field(default_factory=list)
    image: bytes | None = None
    note: str = ""                  # откуда взято, для подписи

    @property
    def empty(self) -> bool:
        return self.kind == "none"

    def preview(self) -> str:
        if self.kind == "url":
            short = (self.url or "").split("://", 1)[-1]
            return short[:57] + "…" if len(short) > 60 else short
        if self.kind == "files":
            if len(self.files) == 1:
                return self.files[0].name
            return f"{len(self.files)} файлов: " + ", ".join(
                f.name for f in self.files[:3])
        if self.kind == "text":
            flat = " ".join((self.text or "").split())
            return flat[:70] + "…" if len(flat) > 73 else flat
        if self.kind == "image":
            size = len(self.image or b"") // 1024
            return f"картинка из буфера, {size} КБ"
        return "нечего захватывать"


EMPTY = Grab(kind="none")


def _from_clipboard() -> Grab:
    with Clipboard():
        available = set(Clipboard.formats())
        if CF_HDROP in available:
            files = Clipboard.files()
            if files:
                return Grab(kind="files", files=files, note="буфер обмена")
        if CF_UNICODETEXT in available:
            text = (Clipboard.text() or "").strip()
            if text:
                return Grab(kind="text", text=text, note="буфер обмена")
        if CF_DIB in available:
            image = Clipboard.image_bmp()
            if image:
                return Grab(kind="image", image=image, note="буфер обмена")
    return EMPTY


def capture_passive(window: Window, browser_url: str | None) -> Grab:
    """Без побочных эффектов — годится для постоянного опроса и для подписи."""
    if browser_url:
        return Grab(kind="url", url=browser_url, note="вкладка браузера")

    if "cabinetwclass" in window.cls.lower() or "progman" in window.cls.lower():
        files = explorer_selection(window.hwnd)
        if files:
            return Grab(kind="files", files=files, note="проводник")

    text = selected_text()
    if text:
        if len(text) > MAX_INLINE_TEXT:
            text = text[:MAX_INLINE_TEXT]
        return Grab(kind="text", text=text, note="выделение")

    try:
        return _from_clipboard()
    except OSError:
        return EMPTY


def capture_deep(window: Window, browser_url: str | None) -> Grab:
    """То же плюс Ctrl+C, если пассивно ничего не нашлось.

    Старое содержимое буфера возвращаем. Возвращается только текст: вернуть
    произвольный набор форматов средствами WinAPI нельзя, их пришлось бы
    пересоздавать по одному. На практике в буфере лежит текст, и этого
    достаточно, чтобы не испортить человеку работу.
    """
    passive = capture_passive(window, browser_url)
    if not passive.empty:
        return passive

    try:
        with Clipboard():
            saved = Clipboard.text()
    except OSError:
        saved = None

    _press_copy()
    time.sleep(0.12)      # приложению нужно время положить данные в буфер

    try:
        grabbed = _from_clipboard()
    except OSError:
        grabbed = EMPTY

    if grabbed.empty and saved is not None:
        set_clipboard_text(saved)
        return EMPTY

    if saved is not None and grabbed.kind == "text" and grabbed.text == saved:
        # ничего не скопировалось: в буфере то же, что и было
        return EMPTY

    if saved is not None:
        # данные забрали, буфер возвращаем владельцу
        payload = grabbed
        set_clipboard_text(saved)
        return payload

    return grabbed


# --------------------------------------------------------------------------
# фоновое слежение
# --------------------------------------------------------------------------

class GrabWatcher:
    """Фоном держит ответ на вопрос «что улетит, если показать кулак».

    Опрашивает активное окно дважды в секунду — только пассивными
    способами, без Ctrl+C. Благодаря этому оверлей всегда знает, что
    показать, а ставка на аукционе уходит мгновенно.
    """

    def __init__(self, interval: float = 0.5,
                 browser_state: "Callable[[], TabState | None] | None" = None) -> None:
        self.interval = interval
        # Расширение браузера, если оно установлено. Без него всё работает
        # по-прежнему, просто без таймкода и без правильного адреса на
        # сайтах, которые меняют его через history.pushState.
        self.browser_state = browser_state
        self._reader = UrlReader()
        self._lock = threading.Lock()
        self._current = EMPTY
        self._window = Window(hwnd=0, cls="", title="")
        self._stop = threading.Event()

    @property
    def current(self) -> Grab:
        with self._lock:
            return self._current

    @property
    def window(self) -> Window:
        with self._lock:
            return self._window

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self, deep: bool = False) -> Grab:
        """Свежий захват прямо сейчас. deep=True разрешает Ctrl+C."""
        window = foreground()

        # Расширение знает о странице больше, чем адресная строка, поэтому
        # спрашиваем сначала его.
        if window.is_browser and self.browser_state is not None:
            state = self.browser_state()
            if state is not None:
                note = state.title or "вкладка браузера"
                if state.seconds >= 5:
                    note = f"{note} · {state.seconds // 60}:{state.seconds % 60:02d}"
                return Grab(kind="url",
                            url=with_timecode(state.url, state.seconds),
                            note=note)

        url = self._reader.read(window)
        grab = (capture_deep if deep else capture_passive)(window, url)
        if grab.kind == "url":
            grab.note = strip_browser_suffix(window.title) or grab.note
        return grab

    def run(self) -> None:
        """Блокирующий цикл. Запускать в отдельном потоке."""
        with auto.UIAutomationInitializerInThread():
            while not self._stop.is_set():
                try:
                    window = foreground()
                    probed = self.snapshot(deep=False)
                except Exception:
                    log.exception("сбой при опросе активного окна")
                    window, probed = self._window, EMPTY
                with self._lock:
                    self._current, self._window = probed, window
                self._stop.wait(self.interval)
