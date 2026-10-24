"""Распознавание кулака и ладони на веб-камере ПК.

Используется готовая модель MediaPipe Gesture Recognizer: Closed_Fist и
Open_Palm — две из её встроенных категорий, обучать ничего не нужно.

Два приёма против ложных срабатываний:
  * жест должен продержаться HOLD_SECONDS — случайное движение рукой
    не успевает;
  * после срабатывания COOLDOWN_SECONDS тишины, иначе один жест
    отработал бы десятки раз подряд.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import cv2
import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision

log = logging.getLogger("bridge.gestures")

FIST = "Closed_Fist"
PALM = "Open_Palm"

# Гистерезис: войти в удержание труднее, чем в нём остаться. Уверенность
# модели всё время дышит, и требование непрерывной серии кадров выше
# одного порога обнуляет отсчёт на каждом провале. На веб-камере это
# почти незаметно, на фронталке телефона ломает распознавание полностью.
ENTER_CONFIDENCE = 0.55
STAY_CONFIDENCE = 0.35
LOST_GRACE = 0.35
# Льгота переживает потерю руки, но стрелять, когда руки в кадре уже нет,
# нельзя: иначе короткий показ ладони с последующим опусканием руки
# досчитывался бы до срабатывания вхолостую.
SEEN_RECENTLY = 0.12

HOLD_SECONDS = 0.45
COOLDOWN_SECONDS = 1.5


@dataclass
class GestureEvent:
    name: str          # FIST или PALM
    confidence: float
    hand_area: float   # доля кадра, занятая кистью — косвенная мера близости


def _hand_area(landmarks) -> float:
    """Площадь bounding box кисти в долях кадра.

    Чем ближе рука к камере, тем больше. Сравнивать корректно только
    одинаковые жесты между собой (кулак с кулаком, ладонь с ладонью) —
    аукцион на брокере именно так и устроен, у grab и drop разные ключи.
    """
    xs = [p.x for p in landmarks]
    ys = [p.y for p in landmarks]
    return max(0.0, min(1.0, (max(xs) - min(xs)) * (max(ys) - min(ys))))


class GestureWatcher:
    """Читает камеру и зовёт on_gesture на каждый устойчивый жест."""

    def __init__(self, model_path: Path, on_gesture: Callable[[GestureEvent], None],
                 camera_index: int = 0, preview: bool = False) -> None:
        self.model_path = Path(model_path)
        self.on_gesture = on_gesture
        self.camera_index = camera_index
        self.preview = preview

        self._held: str | None = None
        self._held_since = 0.0
        self._last_seen = 0.0
        self._best_score = 0.0
        self._best_area = 0.0
        self._quiet_until = 0.0
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def _build(self) -> vision.GestureRecognizer:
        if not self.model_path.exists():
            raise FileNotFoundError(
                f"нет модели {self.model_path}. Скачай её командой из README")
        options = vision.GestureRecognizerOptions(
            base_options=mp_python.BaseOptions(
                model_asset_path=str(self.model_path)),
            running_mode=vision.RunningMode.VIDEO,
            num_hands=1,
        )
        return vision.GestureRecognizer.create_from_options(options)

    def _consider(self, category: str | None, score: float, area: float) -> None:
        """Накапливает удержание жеста и решает, пора ли стрелять."""
        now = time.monotonic()
        tracked = category if category in (FIST, PALM) else None

        if tracked and score >= ENTER_CONFIDENCE and tracked != self._held:
            self._held = tracked
            self._held_since = now
            self._best_score = 0.0
            self._best_area = 0.0
            self._last_seen = now
        elif tracked and tracked == self._held and score >= STAY_CONFIDENCE:
            self._last_seen = now          # просело, но жест тот же
        elif self._held and now - self._last_seen <= LOST_GRACE:
            pass                           # потеряли на миг — отсчёт не рвём
        else:
            self._held = None
            return

        # В событие уходит лучшее за удержание, а не последний кадр:
        # он может оказаться как раз провалом.
        self._best_score = max(self._best_score, score)
        self._best_area = max(self._best_area, area)

        if now < self._quiet_until:
            return
        if now - self._held_since < HOLD_SECONDS:
            return
        if now - self._last_seen > SEEN_RECENTLY:
            return

        name = self._held
        self._quiet_until = now + COOLDOWN_SECONDS
        self._held = None
        log.info("жест %s (conf=%.2f, рука=%.3f кадра)",
                 name, self._best_score, self._best_area)
        self.on_gesture(GestureEvent(name=name, confidence=self._best_score,
                                     hand_area=self._best_area))

    def run(self) -> None:
        """Блокирующий цикл. Запускать в отдельном потоке."""
        capture = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW)
        if not capture.isOpened():
            raise RuntimeError(f"не открывается камера #{self.camera_index}")

        capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

        recognizer = self._build()
        log.info("камера #%d запущена, слежу за кулаком и ладонью",
                 self.camera_index)

        try:
            while not self._stop:
                ok, frame = capture.read()
                if not ok:
                    time.sleep(0.05)
                    continue

                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                result = recognizer.recognize_for_video(
                    image, int(time.monotonic() * 1000))

                category: str | None = None
                score = 0.0
                area = 0.0

                if result.gestures and result.gestures[0]:
                    top = result.gestures[0][0]
                    category = top.category_name
                    score = float(top.score)
                if result.hand_landmarks and result.hand_landmarks[0]:
                    area = _hand_area(result.hand_landmarks[0])

                self._consider(category, score, area)

                if self.preview:
                    self._draw(frame, category, score, area)
                    if cv2.waitKey(1) & 0xFF in (27, ord("q")):
                        break
        finally:
            capture.release()
            recognizer.close()
            if self.preview:
                cv2.destroyAllWindows()
            log.info("камера остановлена")

    def _draw(self, frame, category: str | None, score: float,
              area: float) -> None:
        """Окно самопроверки: видно, что ловится и насколько уверенно."""
        label = {FIST: "KULAK -> zahvat",
                 PALM: "LADON -> sbros"}.get(self._held or category, "-")
        colour = (90, 200, 120) if self._held else (120, 120, 120)
        confidence = score

        held = 0.0
        if self._held is not None:
            held = min(1.0, (time.monotonic() - self._held_since) / HOLD_SECONDS)

        cv2.rectangle(frame, (0, 0), (frame.shape[1], 76), (24, 24, 28), -1)
        cv2.putText(frame, label, (14, 30), cv2.FONT_HERSHEY_SIMPLEX,
                    0.7, colour, 2)
        cv2.putText(frame,
                    f"conf={confidence:.2f}  hand={area:.3f}  hold={held:.0%}",
                    (14, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (190, 190, 190), 1)
        if held > 0:
            cv2.rectangle(frame, (0, 74),
                          (int(frame.shape[1] * held), 78), (90, 200, 120), -1)
        cv2.imshow("Gesture Bridge - samoproverka (q - vyhod)", frame)
