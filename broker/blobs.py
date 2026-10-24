"""Хранилище файлов посылок.

Байты не ходят в протоколе: отправитель заливает файл сюда и получает id,
посылка несёт только метаданные, приёмник скачивает по id. Так посылка
остаётся маленьким JSON, а большой файл не держит ни соединение, ни память.

Побочная выгода: отправитель может уйти спать сразу после захвата —
файл уже лежит у брокера, и висящая очередь работает для файлов так же,
как для ссылок.
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("bridge.blobs")

CHUNK = 256 * 1024
DEFAULT_MAX_FILE = 1024 * 1024 * 1024        # 1 ГиБ на файл
DEFAULT_MAX_TOTAL = 4 * 1024 * 1024 * 1024   # 4 ГиБ на всё хранилище

_UNSAFE = re.compile(r'[\x00-\x1f<>:"/\\|?*]')


def safe_name(name: str) -> str:
    """Имя файла, пригодное для диска на любой стороне.

    Приходит оно от другого устройства, поэтому убираем разделители путей
    и управляющие символы: иначе имя вида ..\\..\\system32\\x положило бы
    файл куда не просили.
    """
    cleaned = _UNSAFE.sub("_", name).strip(". ")
    cleaned = cleaned.replace("..", "_")
    return cleaned[:160] or "file"


class BlobTooLarge(Exception):
    pass


class StoreFull(Exception):
    pass


@dataclass
class Blob:
    id: str
    name: str
    size: int
    created: float

    def summary(self) -> dict:
        return {"id": self.id, "name": self.name, "size": self.size}


class BlobStore:
    def __init__(self, spool: Path,
                 max_file: int = DEFAULT_MAX_FILE,
                 max_total: int = DEFAULT_MAX_TOTAL) -> None:
        self.spool = spool
        self.max_file = max_file
        self.max_total = max_total
        self._blobs: dict[str, Blob] = {}
        self.spool.mkdir(parents=True, exist_ok=True)
        self._clear_spool()

    def _clear_spool(self) -> None:
        """Остатки прошлого запуска никому не нужны: посылок на них нет."""
        for leftover in self.spool.glob("*.blob"):
            try:
                leftover.unlink()
            except OSError:
                pass

    @property
    def used(self) -> int:
        return sum(b.size for b in self._blobs.values())

    def path(self, blob_id: str) -> Path:
        return self.spool / f"{blob_id}.blob"

    def get(self, blob_id: str) -> Blob | None:
        return self._blobs.get(blob_id)

    async def save(self, chunks, name: str) -> Blob:
        """Пишет поток на диск кусками, не поднимая файл в память целиком.

        `chunks` — любой асинхронный итератор байтов, например
        `request.content.iter_chunked(...)`.
        """
        blob_id = uuid.uuid4().hex
        target = self.path(blob_id)
        written = 0
        budget = self.max_total - self.used

        try:
            with target.open("wb") as handle:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > self.max_file:
                        raise BlobTooLarge(
                            f"файл больше {self.max_file // 1048576} МиБ")
                    if written > budget:
                        raise StoreFull("на брокере кончилось место под посылки")
                    handle.write(chunk)
        except BaseException:
            target.unlink(missing_ok=True)
            raise

        blob = Blob(id=blob_id, name=safe_name(name), size=written,
                    created=time.time())
        self._blobs[blob_id] = blob
        log.info("принят файл %s (%d КБ)", blob.name, written // 1024)
        return blob

    def drop(self, blob_id: str) -> None:
        if self._blobs.pop(blob_id, None) is None:
            return
        try:
            self.path(blob_id).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("не удалось удалить файл %s: %s", blob_id, exc)

    def drop_many(self, blob_ids) -> None:
        for blob_id in blob_ids:
            self.drop(blob_id)

    def sweep_orphans(self, ttl: float = 7200.0) -> int:
        """Файлы, которые залили, но посылку так и не создали."""
        now = time.time()
        stale = [b.id for b in self._blobs.values() if now - b.created > ttl]
        self.drop_many(stale)
        return len(stale)
