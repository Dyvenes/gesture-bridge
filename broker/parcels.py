"""Хранилище посылок.

Посылка живёт в двух состояниях:
  flying  — только что захвачена, приёмники подняли камеру и ждут ладонь
  hovering — ладонь так и не показали; посылка висит, её можно забрать из списка

Это и есть "фолбэк-очередь": если телефон в кармане, посылка не теряется.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


FLYING_TTL = 15.0       # сколько секунд ждём ладонь
HOVERING_TTL = 3600.0   # сколько посылка висит в очереди, прежде чем истлеть
DELIVERED_TTL = 60.0            # хватает, чтобы отсечь повторный claim
DELIVERED_WITH_FILES_TTL = 3600.0   # запас на скачивание большого файла


@dataclass
class Parcel:
    kind: str                      # пока только "url"
    payload: dict
    preview: str                   # короткая строка для UI
    origin: str                    # client_id отправителя
    origin_name: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created: float = field(default_factory=time.time)
    state: str = "flying"
    claimed_by: str | None = None

    @property
    def expires_at(self) -> float:
        ttl = FLYING_TTL if self.state == "flying" else HOVERING_TTL
        return self.created + ttl

    @property
    def blob_ids(self) -> list[str]:
        """Файлы, которые держит эта посылка: их надо удалить вместе с ней."""
        if self.kind != "files":
            return []
        return [f["id"] for f in (self.payload.get("files") or [])
                if isinstance(f, dict) and f.get("id")]

    def summary(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "preview": self.preview,
            "from": self.origin_name,
            "created": self.created,
            "state": self.state,
        }


class ParcelStore:
    def __init__(self) -> None:
        self._parcels: dict[str, Parcel] = {}

    def add(self, parcel: Parcel) -> Parcel:
        self._parcels[parcel.id] = parcel
        return parcel

    def get(self, parcel_id: str) -> Parcel | None:
        return self._parcels.get(parcel_id)

    def claim(self, parcel_id: str, client_id: str) -> Parcel | None:
        """Атомарный захват. Первый, кто показал ладонь, забирает посылку.

        Своё забрать нельзя — ни жестом, ни тапом: содержимое у отправителя
        и так есть, а вот случайно вернуть посылку себе очень легко.
        """
        parcel = self._parcels.get(parcel_id)
        if parcel is None or parcel.claimed_by is not None:
            return None
        if parcel.origin == client_id:
            return None
        parcel.claimed_by = client_id
        parcel.state = "delivered"
        return parcel

    def drop(self, parcel_id: str) -> None:
        self._parcels.pop(parcel_id, None)

    def hovering(self) -> list[Parcel]:
        return sorted((p for p in self._parcels.values() if p.state == "hovering"),
                      key=lambda p: p.created, reverse=True)

    def flying(self) -> list[Parcel]:
        """Летящие посылки, самая свежая первой.

        Это стек, а не очередь: ладонь забирает верхнюю. Выбрать источник
        жестом нельзя, поэтому порядок задаёт время захвата, а человек видит
        его в оверлее до того, как показал ладонь.
        """
        return sorted((p for p in self._parcels.values()
                       if p.state == "flying" and p.claimed_by is None),
                      key=lambda p: p.created, reverse=True)

    def newest_flying(self, exclude_origin: str | None = None) -> Parcel | None:
        """Верх стека, не считая посылок самого спрашивающего.

        Отправитель не может быть приёмником, и это не придирка: сжав
        кулак, человек разжимает руку, опуская её, — и его же камера
        видит раскрытую ладонь. Без этого отсева посылка возвращалась бы
        тому, кто её только что отправил.
        """
        for parcel in self.flying():
            if exclude_origin is None or parcel.origin != exclude_origin:
                return parcel
        return None

    def sweep(self) -> tuple[list[Parcel], list[Parcel]]:
        """Переводит просроченные flying в hovering, выкидывает истлевшие.

        Возвращает (ставшие_hovering, удалённые).
        """
        now = time.time()
        became_hovering: list[Parcel] = []
        removed: list[Parcel] = []

        for parcel in list(self._parcels.values()):
            if parcel.state == "delivered":
                # Доставленную держим, пока приёмник качает файлы: удалить
                # её раньше — значит оборвать скачивание на середине.
                # Посылке без файлов столько времени не нужно.
                grace = DELIVERED_WITH_FILES_TTL if parcel.blob_ids else DELIVERED_TTL
                if now - parcel.created > grace:
                    removed.append(parcel)
                    del self._parcels[parcel.id]
            elif now >= parcel.expires_at:
                if parcel.state == "flying":
                    parcel.state = "hovering"
                    parcel.created = now
                    became_hovering.append(parcel)
                else:
                    removed.append(parcel)
                    del self._parcels[parcel.id]

        return became_hovering, removed
