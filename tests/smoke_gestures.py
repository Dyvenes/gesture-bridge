"""Проверка удержания жеста без камеры.

Распознаватель отдаёт уверенность, которая всё время дышит. Главный
вопрос к логике: доживает ли удержание до конца, когда отдельные кадры
проваливаются ниже порога входа. Именно на этом ломалось распознавание
на телефоне — `Open_Palm 0.65` при удержании, застрявшем на нуле.

Камера тут не нужна: кормим _consider напрямую и крутим фальшивые часы.

Запуск:  python tests/smoke_gestures.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import gestures                                   # noqa: E402
from agent.gestures import (                                 # noqa: E402
    COOLDOWN_SECONDS, FIST, HOLD_SECONDS, PALM, GestureWatcher,
)

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


class Clock:
    """Фальшивые часы: шагаем кадрами по 1/30 секунды."""

    def __init__(self) -> None:
        self.now = 1000.0

    def tick(self, seconds: float = 1 / 30) -> float:
        self.now += seconds
        return self.now


def make_watcher(clock: Clock):
    fired = []
    watcher = GestureWatcher(ROOT / "models" / "gesture_recognizer.task",
                             fired.append)
    gestures.time.monotonic = lambda: clock.now
    return watcher, fired


def feed(watcher, clock, frames) -> None:
    """frames — список (категория, уверенность, площадь руки)."""
    for category, score, area in frames:
        clock.tick()
        watcher._consider(category, score, area)


def steady(category, score, area, seconds):
    count = max(1, int(seconds * 30))
    return [(category, score, area)] * count


def main() -> int:
    original = gestures.time.monotonic
    try:
        print("[1] Ровная уверенная ладонь срабатывает")
        clock = Clock()
        watcher, fired = make_watcher(clock)
        feed(watcher, clock, steady(PALM, 0.9, 0.2, HOLD_SECONDS + 0.1))
        check("сработало ровно один раз", len(fired), 1)
        check("жест опознан", fired[0].name if fired else None, PALM)

        print("\n[2] Дрожащая уверенность: 0.65 с провалами до 0.45")
        clock = Clock()
        watcher, fired = make_watcher(clock)
        noisy = []
        for i in range(int((HOLD_SECONDS + 0.1) * 30)):
            noisy.append((PALM, 0.65 if i % 2 == 0 else 0.45, 0.18))
        feed(watcher, clock, noisy)
        check("удержание дожило до конца", len(fired), 1)
        check("в событие ушла лучшая уверенность",
              round(fired[0].confidence, 2) if fired else None, 0.65)

        print("\n[3] Рука пропала на мгновение — отсчёт не рвётся")
        clock = Clock()
        watcher, fired = make_watcher(clock)
        frames = steady(PALM, 0.8, 0.2, 0.2)
        frames += steady(None, 0.0, 0.0, 0.2)      # 200 мс потери
        frames += steady(PALM, 0.8, 0.2, 0.2)
        feed(watcher, clock, frames)
        check("сработало несмотря на провал", len(fired), 1)

        print("\n[4] Рука пропала надолго — отсчёт сбрасывается")
        clock = Clock()
        watcher, fired = make_watcher(clock)
        frames = steady(PALM, 0.8, 0.2, 0.3)
        frames += steady(None, 0.0, 0.0, 0.6)      # дольше LOST_GRACE
        frames += steady(PALM, 0.8, 0.2, 0.3)      # меньше HOLD_SECONDS
        feed(watcher, clock, frames)
        check("не сработало", len(fired), 0)

        print("\n[5] Слабый жест не проходит вовсе")
        clock = Clock()
        watcher, fired = make_watcher(clock)
        feed(watcher, clock, steady(PALM, 0.40, 0.2, 1.0))
        check("ниже порога входа — тишина", len(fired), 0)

        print("\n[6] Один жест не стреляет дважды подряд")
        clock = Clock()
        watcher, fired = make_watcher(clock)
        feed(watcher, clock, steady(PALM, 0.9, 0.2, HOLD_SECONDS + 0.3))
        check("за время удержания сработало один раз", len(fired), 1)
        feed(watcher, clock, steady(PALM, 0.9, 0.2, COOLDOWN_SECONDS + 0.3))
        check("после паузы сработало снова", len(fired), 2)

        print("\n[7] Смена жеста перебивает удержание")
        clock = Clock()
        watcher, fired = make_watcher(clock)
        frames = steady(PALM, 0.9, 0.2, 0.3)
        frames += steady(FIST, 0.9, 0.2, HOLD_SECONDS + 0.1)
        feed(watcher, clock, frames)
        check("сработал кулак, а не ладонь",
              fired[0].name if fired else None, FIST)

        print("\n[8] В событие уходит самая крупная рука за удержание")
        clock = Clock()
        watcher, fired = make_watcher(clock)
        frames = [(PALM, 0.9, 0.10)] * 6 + [(PALM, 0.9, 0.31)] * 2
        frames += [(PALM, 0.9, 0.12)] * int(HOLD_SECONDS * 30)
        feed(watcher, clock, frames)
        check("взята лучшая площадь",
              round(fired[0].hand_area, 2) if fired else None, 0.31)
    finally:
        gestures.time.monotonic = original

    print(f"\n{'=' * 46}\nпройдено: {ok_count}   провалено: {fail_count}")
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
