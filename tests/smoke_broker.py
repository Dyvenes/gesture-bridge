"""Прогон брокера на фальшивых клиентах.

Проверяет ровно те два места, где логика неочевидна:
  1. аукцион — при одновременной ладони у двух ПК выигрывает тот,
     у кого рука ближе к камере;
  2. стек — ладонь забирает самую свежую посылку, а не первую попавшуюся.

Запуск:  python tests/smoke_broker.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiohttp                                      # noqa: E402
from aiohttp.test_utils import TestServer           # noqa: E402

from broker.server import build_app                 # noqa: E402

TOKEN = "test-token"

ok_count = 0
fail_count = 0


def check(label: str, got, want) -> None:
    global ok_count, fail_count
    if got == want:
        ok_count += 1
        print(f"  OK   {label}")
    else:
        fail_count += 1
        print(f"  FAIL {label}\n       получили: {got!r}\n       ждали:    {want!r}")


class FakeClient:
    """Минимальный клиент: шлёт JSON, копит входящие сообщения."""

    def __init__(self, session, url, name, device, caps):
        self.session, self.url = session, url
        self.name, self.device, self.caps = name, device, caps
        self.inbox: list[dict] = []
        self.ws = None

    async def connect(self):
        self.ws = await self.session.ws_connect(f"{self.url}/ws?token={TOKEN}")
        asyncio.create_task(self._pump())
        await self.send(t="hello", name=self.name,
                        device=self.device, caps=self.caps)
        welcome = await self.wait_for("welcome")
        if welcome is None:
            raise RuntimeError(f"{self.name}: брокер не ответил welcome")
        if welcome.get("me", {}).get("name") != self.name:
            raise RuntimeError(f"{self.name}: в welcome нет своего описания: {welcome}")
        return self

    async def _pump(self):
        try:
            async for msg in self.ws:
                if msg.type is aiohttp.WSMsgType.TEXT:
                    self.inbox.append(msg.json())
        except Exception:
            pass

    async def send(self, **payload):
        await self.ws.send_json(payload)

    async def wait_for(self, kind: str, timeout: float = 3.0) -> dict | None:
        async def poll():
            while True:
                for m in self.inbox:
                    if m.get("t") == kind:
                        return m
                await asyncio.sleep(0.01)
        try:
            return await asyncio.wait_for(poll(), timeout)
        except asyncio.TimeoutError:
            return None

    def kinds(self) -> list[str]:
        return [m.get("t") for m in self.inbox]

    def clear(self):
        self.inbox.clear()


async def main() -> int:
    spool = Path(tempfile.mkdtemp(prefix="bridge-spool-"))
    app = build_app(TOKEN, spool)
    server = TestServer(app)
    await server.start_server()
    url = f"http://127.0.0.1:{server.port}"

    async with aiohttp.ClientSession() as session:
        pc_a = await FakeClient(session, url, "PC-A", "pc",
                                ["grab_gesture", "drop_gesture", "open_url"]).connect()
        pc_b = await FakeClient(session, url, "PC-B", "pc",
                                ["grab_gesture", "drop_gesture", "open_url"]).connect()
        phone = await FakeClient(session, url, "Pixel", "phone",
                                 ["grab_share", "open_url"]).connect()

        print("\n[1] Телефон делится ссылкой -> оба ПК поднимают камеру")
        for c in (pc_a, pc_b, phone):
            c.clear()
        await phone.send(t="grab", kind="url",
                         payload={"url": "https://example.com/one"},
                         preview="example.com/one")
        pending_a = await pc_a.wait_for("pending")
        pending_b = await pc_b.wait_for("pending")
        check("PC-A получил pending", pending_a is not None, True)
        check("PC-B получил pending", pending_b is not None, True)
        check("в pending виден источник", pending_a.get("sender"), "Pixel")
        check("отправитель получил подтверждение",
              (await phone.wait_for("grabbed")) is not None, True)
        check("телефон не поднимает камеру сам себе",
              "pending" in phone.kinds(), False)

        print("\n[2] Оба ПК видят ладонь одновременно -> выигрывает ближняя рука")
        parcel_id = pending_a["id"]
        for c in (pc_a, pc_b, phone):
            c.clear()
        await asyncio.gather(
            # у PC-B рука крупнее в кадре => он ближе => он и должен выиграть
            pc_a.send(t="bid", role="drop", id=parcel_id,
                      signals={"hand": 0.20, "conf": 0.9, "idle": 2}),
            pc_b.send(t="bid", role="drop", id=parcel_id,
                      signals={"hand": 0.55, "conf": 0.9, "idle": 2}),
        )
        deliver_b = await pc_b.wait_for("deliver")
        lost_a = await pc_a.wait_for("lost")
        check("PC-B получил посылку", deliver_b is not None, True)
        check("доставлен правильный URL",
              deliver_b["payload"]["url"], "https://example.com/one")
        check("PC-A узнал, что проиграл", lost_a is not None, True)
        check("PC-A сказали, кому ушло", lost_a.get("to"), "PC-B")
        check("PC-A посылку не получил", "deliver" in pc_a.kinds(), False)

        print("\n[3] Две посылки в воздухе -> ладонь берёт самую свежую")
        for c in (pc_a, pc_b, phone):
            c.clear()
        await phone.send(t="grab", kind="url",
                         payload={"url": "https://example.com/older"},
                         preview="example.com/older")
        await pc_b.wait_for("pending")
        await asyncio.sleep(0.05)
        pc_b.clear()
        await phone.send(t="grab", kind="url",
                         payload={"url": "https://example.com/newer"},
                         preview="example.com/newer")
        pending = await pc_b.wait_for("pending")
        check("в стеке две посылки", len(pending.get("stack") or []), 2)
        check("верх стека — свежая",
              pending["stack"][0]["preview"], "example.com/newer")

        pc_a.clear()
        # id не указываем — брокер сам обязан взять верх стека
        await pc_a.send(t="bid", role="drop",
                        signals={"hand": 0.6, "conf": 0.9, "idle": 1})
        deliver = await pc_a.wait_for("deliver")
        check("ладонь взяла свежую, а не старую",
              deliver["payload"]["url"], "https://example.com/newer")

        print("\n[4] Слабый жест игнорируется")
        pc_a.clear()
        await pc_a.send(t="bid", role="drop",
                        signals={"hand": 0.001, "conf": 0.02, "idle": 999})
        ignored = await pc_a.wait_for("ignored")
        check("случайное движение отброшено", ignored is not None, True)

        print("\n[5] Когда брать нечего")
        pc_a.clear()
        await pc_a.send(t="bid", role="drop",
                        signals={"hand": 0.6, "conf": 0.9, "idle": 1})
        await pc_a.wait_for("deliver", timeout=0.6)   # добираем "older"
        pc_a.clear()
        await pc_a.send(t="bid", role="drop",
                        signals={"hand": 0.6, "conf": 0.9, "idle": 1})
        check("пустой воздух -> nothing_to_take",
              (await pc_a.wait_for("nothing_to_take")) is not None, True)

        print("\n[6] Чужой токен не пускают")
        try:
            await session.ws_connect(f"{url}/ws?token=wrong")
            check("отказ по токену", "пустили", "403")
        except aiohttp.WSServerHandshakeError as exc:
            check("отказ по токену", exc.status, 403)

        print("\n[7] /health без токена не выдаёт имена устройств")
        async with session.get(f"{url}/health") as resp:
            anon = await resp.json()
        check("анонимно отвечает 200", resp.status, 200)
        check("живость видна", anon.get("ok"), True)
        check("имён устройств нет", "clients" in anon, False)
        check("число посылок не раскрыто", "flying" in anon, False)

        async with session.get(f"{url}/health?token={TOKEN}") as resp:
            full = await resp.json()
        check("с токеном видно клиентов", len(full.get("clients") or []), 3)

        # не-ASCII в токене роняло compare_digest в TypeError -> 500
        async with session.get(f"{url}/health?token=пароль") as resp:
            weird = await resp.json()
        check("кириллица в токене не роняет сервер", resp.status, 200)
        check("и ничего не выдаёт", "clients" in weird, False)

        print("\n[8] Захват жестом двухфазный: сначала выигрыш, потом добыча")
        for c in (pc_a, pc_b, phone):
            c.clear()
        await pc_a.send(t="bid", role="grab",
                        signals={"hand": 0.5, "conf": 0.9, "idle": 1})
        you_grab = await pc_a.wait_for("you_grab")
        check("победитель получил команду снимать", you_grab is not None, True)
        check("посылку заранее не создали", "pending" in phone.kinds(), False)

        print("\n[9] Файл доезжает байт в байт")
        blob_bytes = bytes(range(256)) * 400          # 100 КБ, заведомо бинарные
        async with session.put(
                f"{url}/blob?token={TOKEN}&name=отчёт%20за%20квартал.bin",
                data=blob_bytes) as resp:
            uploaded = await resp.json()
        check("заливка принята", resp.status, 200)
        check("размер совпал", uploaded.get("size"), len(blob_bytes))
        check("имя сохранено", uploaded.get("name"), "отчёт за квартал.bin")

        phone.clear()
        await pc_a.send(t="grab", kind="files",
                        payload={"files": [{"id": uploaded["id"]}]},
                        preview="отчёт за квартал.bin")
        pending = await phone.wait_for("pending")
        check("телефон увидел посылку с файлом", pending.get("kind"), "files")

        phone.clear()
        await phone.send(t="claim", id=pending["id"])
        delivered = await phone.wait_for("deliver")
        check("в доставке есть файл", len(delivered["payload"]["files"]), 1)

        blob_id = delivered["payload"]["files"][0]["id"]
        async with session.get(f"{url}/blob/{blob_id}?token={TOKEN}") as resp:
            downloaded = await resp.read()
        check("скачалось столько же байт", len(downloaded), len(blob_bytes))
        check("содержимое не испортилось", downloaded == blob_bytes, True)

        async with session.get(f"{url}/blob/{blob_id}") as resp:
            check("без токена файл не отдаётся", resp.status, 403)

        print("\n[10] Посылка не может сослаться на несуществующий файл")
        pc_a.clear()
        await pc_a.send(t="grab", kind="files",
                        payload={"files": [{"id": "0" * 32}]}, preview="обман")
        err = await pc_a.wait_for("error")
        check("брокер отказал", err is not None, True)

        print("\n[11] Отправитель не может забрать свою же посылку")
        for c in (pc_a, pc_b, phone):
            c.clear()
        await pc_a.send(t="grab", kind="url",
                        payload={"url": "https://example.com/mine"},
                        preview="example.com/mine")
        await pc_a.wait_for("grabbed")

        # ладонь у самого отправителя: так выглядит разжатая рука после кулака
        pc_a.clear()
        await pc_a.send(t="bid", role="drop",
                        signals={"hand": 0.9, "conf": 0.99, "idle": 0})
        check("своё жестом не забрать",
              (await pc_a.wait_for("nothing_to_take")) is not None, True)
        check("и посылку себе не доставили", "deliver" in pc_a.kinds(), False)

        # даже с явным id и даже тапом по списку
        pending_b = await pc_b.wait_for("pending")
        pc_a.clear()
        await pc_a.send(t="claim", id=pending_b["id"])
        check("своё тапом тоже не забрать",
              (await pc_a.wait_for("nothing_to_take")) is not None, True)

        # а предназначенное устройство забирает спокойно
        pc_b.clear()
        await pc_b.send(t="bid", role="drop",
                        signals={"hand": 0.2, "conf": 0.8, "idle": 5})
        delivered = await pc_b.wait_for("deliver")
        check("адресат получил, несмотря на ставку отправителя",
              delivered["payload"]["url"], "https://example.com/mine")

        print("\n[12] Текст ездит без заливки файлов")
        phone.clear()
        await pc_a.send(t="grab", kind="text",
                        payload={"text": "скопируй меня"}, preview="скопируй меня")
        pending = await phone.wait_for("pending")
        check("тип посылки", pending.get("kind"), "text")
        await phone.send(t="claim", id=pending["id"])
        delivered = await phone.wait_for("deliver")
        check("текст доехал", delivered["payload"]["text"], "скопируй меня")

    await server.close()
    shutil.rmtree(spool, ignore_errors=True)

    print(f"\n{'=' * 46}\nпройдено: {ok_count}   провалено: {fail_count}")
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
