"""Генератор иконок PWA.

Chrome на Android не предложит установить приложение без PNG-иконок
192 и 512 пикселей, а без установки приёмник не попадёт в системное
меню «Поделиться». Рисуем их кодом, чтобы не тащить в проект Pillow:
нужна раскрытая ладонь из прямоугольников с округлыми углами.

Запуск:  python tools/make_icons.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "receiver" / "icons"

BG = (22, 24, 29, 255)        # #16181d
GLYPH = (124, 196, 255, 255)  # #7cc4ff


def _rounded_rect(px: float, py: float, x0: float, y0: float,
                  x1: float, y1: float, radius: float) -> bool:
    """Точка внутри прямоугольника с округлыми углами (координаты 0..1)."""
    radius = min(radius, (x1 - x0) / 2, (y1 - y0) / 2)
    cx = min(max(px, x0 + radius), x1 - radius)
    cy = min(max(py, y0 + radius), y1 - radius)
    return (px - cx) ** 2 + (py - cy) ** 2 <= radius ** 2


# Ладонь: основание, четыре пальца и отставленный большой.
PALM = (0.30, 0.46, 0.72, 0.82, 0.10)
FINGERS = (
    (0.325, 0.26, 0.395, 0.52, 0.035),
    (0.410, 0.20, 0.480, 0.52, 0.035),
    (0.495, 0.21, 0.565, 0.52, 0.035),
    (0.580, 0.27, 0.650, 0.52, 0.035),
)
THUMB = (0.655, 0.44, 0.790, 0.515, 0.037)


def _is_glyph(px: float, py: float) -> bool:
    if _rounded_rect(px, py, *PALM):
        return True
    if _rounded_rect(px, py, *THUMB):
        return True
    return any(_rounded_rect(px, py, *finger) for finger in FINGERS)


def _png(width: int, height: int, pixels: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    header = struct.pack(">2I5B", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(pixels, 9))
            + chunk(b"IEND", b""))


def render(size: int) -> bytes:
    rows = bytearray()
    for y in range(size):
        rows.append(0)                       # фильтр строки: none
        py = (y + 0.5) / size
        for x in range(size):
            px = (x + 0.5) / size
            rows.extend(GLYPH if _is_glyph(px, py) else BG)
    return _png(size, size, bytes(rows))


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for size in (192, 512):
        path = OUT_DIR / f"icon-{size}.png"
        path.write_bytes(render(size))
        print(f"{path.name}: {path.stat().st_size} байт")


if __name__ == "__main__":
    main()
