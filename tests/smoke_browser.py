"""Проверка моста с расширением браузера.

Расширение здесь поддельное — обычный WebSocket-клиент, который шлёт то
же, что слал бы настоящий content.js. Проверяем две вещи: что состояние
вкладки доезжает до агента, и что таймкод вшивается в ссылку правильно.

Запуск:  python tests/smoke_browser.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiohttp  # noqa: E402

from agent.browser_link import BrowserLink, with_timecode  # noqa: E402

ok_count = 0
fail_count = 0

PORT = 8899


def check(label: str, got, want) -> None:
    global ok_count, fail_count
    if got == want:
        ok_count += 1
        print(f"  OK   {label}")
    else:
        fail_count += 1
        print(f"  FAIL {label}\n       получили: {got!r}\n       ждали:    {want!r}")


def test_timecode() -> None:
    print("[1] Таймкод вшивается в ссылку")
    check("YouTube получает формат с буквой s",
          with_timecode("https://www.youtube.com/watch?v=abc", 125),
          "https://www.youtube.com/watch?v=abc&t=125s")
    check("остальным хватает числа",
          with_timecode("https://rutube.ru/video/xyz/", 90),
          "https://rutube.ru/video/xyz/?t=90")
    check("начало ролика не засоряем",
          with_timecode("https://example.com/v?x=1", 3),
          "https://example.com/v?x=1")
    check("существующие параметры сохраняются",
          with_timecode("https://example.com/v?playlist=7", 60),
          "https://example.com/v?playlist=7&t=60")
    check("прежний таймкод заменяется, а не дублируется",
          with_timecode("https://example.com/v?t=10", 200),
          "https://example.com/v?t=200")


async def test_link() -> None:
    link = BrowserLink(port=PORT)
    await link.start()

    try:
        print("\n[2] Состояние вкладки доезжает до агента")
        check("пока расширение молчит, состояния нет", link.state, None)

        async with aiohttp.ClientSession() as session:
            headers = {"Origin": "chrome-extension://abcdefghijklmnop"}
            async with session.ws_connect(f"http://127.0.0.1:{PORT}/ext",
                                          headers=headers) as ws:
                await ws.send_json({
                    "url": "https://example.com/series/s2e5",
                    "title": "Вторая серия",
                    "seconds": 742,
                    "duration": 2700,
                    "playing": True,
                })
                await asyncio.sleep(0.3)

                state = link.state
                check("адрес получен", state.url if state else None,
                      "https://example.com/series/s2e5")
                check("секунда получена", state.seconds if state else None, 742)
                check("заголовок получен", state.title if state else None,
                      "Вторая серия")
                check("расширение считается подключённым", link.connected, True)

                print("\n[3] Переключение серии доезжает новым адресом")
                await ws.send_json({
                    "url": "https://example.com/series/s2e6",
                    "title": "Третья серия",
                    "seconds": 15,
                    "duration": 2700,
                    "playing": True,
                })
                await asyncio.sleep(0.3)
                check("адрес обновился", link.state.url,
                      "https://example.com/series/s2e6")

        print("\n[4] Протухшее состояние не выдаётся за свежее")
        # Отматываем отметку времени назад: так ведёт себя закрытый браузер.
        link._state.at = time.time() - 10
        check("состарившееся состояние скрыто", link.state, None)
        check("и расширение числится отключённым", link.connected, False)

        print("\n[5] Посторонний источник не пускают")
        async with aiohttp.ClientSession() as session:
            try:
                await session.ws_connect(
                    f"http://127.0.0.1:{PORT}/ext",
                    headers={"Origin": "https://evil.example"})
                check("отказ постороннему", "пустили", "403")
            except aiohttp.WSServerHandshakeError as exc:
                check("отказ постороннему", exc.status, 403)

    finally:
        await link.stop()


async def main() -> int:
    test_timecode()
    await test_link()
    print(f"\n{'=' * 46}\nпройдено: {ok_count}   провалено: {fail_count}")
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
