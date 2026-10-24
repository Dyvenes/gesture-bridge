"""Аукцион жестов.

Проблема: если камера смотрит сразу у нескольких устройств, один показанный
кулак видят все — и все считают себя источником. То же с ладонью на приёме.

Решение: устройство, увидевшее жест, не действует само, а делает ставку.
Брокер ждёт короткое окно, собирает все ставки и объявляет одного победителя.

Счёт считается здесь, а не на устройстве, — чтобы формула была одна на всех.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


ARBITRATION_WINDOW = 0.25   # сек; человек не успевает сделать два жеста быстрее
MIN_SCORE = 0.05            # ниже — считаем, что жест был случайным


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def score_of(signals: dict) -> float:
    """Превращает сигналы устройства в один сравнимый балл.

    hand — доля кадра, которую занимает кисть (ближе рука = больше балл).
           Это главный признак: жестикулируют у того устройства, к которому
           обращаются.
    conf — уверенность распознавателя.
    idle — секунд с последнего ввода мышью/клавиатурой. Машина, за которой
           ты реально работаешь, выигрывает у фоновой.
    """
    hand = _clamp(float(signals.get("hand", 0.0) or 0.0))
    conf = _clamp(float(signals.get("conf", 0.0) or 0.0))
    idle = max(0.0, float(signals.get("idle", 1e9) or 1e9))

    recency_bonus = 0.15 if idle < 30.0 else 0.0
    return hand + 0.10 * conf + recency_bonus


@dataclass
class Bid:
    client_id: str
    client_name: str
    score: float
    signals: dict
    data: dict = field(default_factory=dict)


class _Auction:
    def __init__(self) -> None:
        self.bids: list[Bid] = []
        self.done = asyncio.Event()
        self.winner: Bid | None = None
        self.opened = time.time()


class Arbiter:
    def __init__(self, window: float = ARBITRATION_WINDOW) -> None:
        self.window = window
        self._auctions: dict[str, _Auction] = {}

    async def bid(self, key: str, bid: Bid) -> Bid | None:
        """Делает ставку и ждёт итога аукциона.

        Возвращает победившую ставку (возможно, чужую) или None,
        если все ставки оказались слабее порога.
        """
        auction = self._auctions.get(key)
        is_opener = auction is None

        if auction is None:
            auction = _Auction()
            self._auctions[key] = auction

        auction.bids.append(bid)

        if is_opener:
            try:
                await asyncio.sleep(self.window)
                strong = [b for b in auction.bids if b.score >= MIN_SCORE]
                auction.winner = max(strong, key=lambda b: b.score) if strong else None
            finally:
                # даже при отмене будим остальных, иначе они зависнут
                self._auctions.pop(key, None)
                auction.done.set()
        else:
            await auction.done.wait()

        return auction.winner
