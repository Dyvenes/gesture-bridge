"""Фальшивый ПК для ручной проверки приёмника.

Подключается к брокеру как обычный агент и по команде изображает захват.
Нужен, чтобы проверять телефон, не вставая к компьютеру.

  python tests/fake_pc.py url          — отправить ссылку
  python tests/fake_pc.py text         — отправить кусок текста
  python tests/fake_pc.py file [путь]  — отправить файл (без пути — создаст сам)
  python tests/fake_pc.py drop         — изобразить ладонь (приём)
  python tests/fake_pc.py watch        — слушать и печатать, что приходит

Адрес и токен берутся из BRIDGE_BROKER и BRIDGE_TOKEN.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import aiohttp

BROKER = os.environ.get("BRIDGE_BROKER", "http://127.0.0.1:8787")
TOKEN = os.environ.get("BRIDGE_TOKEN", "devtoken")
URL = os.environ.get("FAKE_URL", "https://example.com/video?t=42")

SAMPLE_TEXT = (
    "Это кусок текста, который должен приехать в буфер обмена приёмника.\n"
    "Вторая строка — чтобы проверить, что переносы не теряются."
)


async def upload(session, path: Path) -> dict:
    with path.open("rb") as handle:
        async with session.put(f"{BROKER}/blob",
                               params={"token": TOKEN, "name": path.name},
                               data=handle) as resp:
            resp.raise_for_status()
            return await resp.json()


async def main(action: str, argument: str | None) -> None:
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"{BROKER}/ws?token={TOKEN}") as ws:
            await ws.send_json({"t": "hello", "name": "Фальшивый-ПК",
                                "device": "pc",
                                "caps": ["grab_gesture", "drop_gesture",
                                         "open_url", "files", "clipboard"]})

            if action == "url":
                await ws.send_json({
                    "t": "grab", "kind": "url",
                    "payload": {"url": URL, "title": "Тестовое видео"},
                    "preview": "example.com/video — Тестовое видео"})

            elif action == "text":
                await ws.send_json({
                    "t": "grab", "kind": "text",
                    "payload": {"text": SAMPLE_TEXT},
                    "preview": SAMPLE_TEXT.split("\n")[0][:60]})

            elif action == "file":
                if argument:
                    path = Path(argument)
                else:
                    tmp = Path(tempfile.gettempdir()) / "проверка связи.txt"
                    tmp.write_text("Файл, уехавший по жесту.\n" * 40,
                                   encoding="utf-8")
                    path = tmp
                blob = await upload(session, path)
                print(f"  залито: {blob['name']} ({blob['size']} байт)")
                await ws.send_json({
                    "t": "grab", "kind": "files",
                    "payload": {"files": [{"id": blob["id"]}]},
                    "preview": path.name})

            elif action == "drop":
                await ws.send_json({
                    "t": "bid", "role": "drop",
                    "signals": {"hand": 0.40, "conf": 0.95, "idle": 1.0}})

            deadline = asyncio.get_running_loop().time() + (
                60 if action == "watch" else 5)
            while asyncio.get_running_loop().time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if msg.type is not aiohttp.WSMsgType.TEXT:
                    break
                data = msg.json()
                rest = {k: v for k, v in data.items() if k != "t"}
                print(f"  <- {data.get('t')}: {str(rest)[:160]}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "watch",
                     sys.argv[2] if len(sys.argv) > 2 else None))
