"""Проверка захвата на ПК: буфер обмена и его форматы.

Тест подставляет в буфер заведомое содержимое, проверяет, что захват его
опознал, и возвращает буфер в исходное состояние. Запускать можно в любой
момент — работу он не испортит.

Проводник и выделенный текст здесь не проверяются: для них нужно живое
окно с выделением. Их смотри через `python agent/main.py --what`.

Запуск:  python tests/smoke_capture.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.capture import (  # noqa: E402
    Clipboard, _from_clipboard, set_clipboard_text,
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


def stage_files_in_clipboard(paths: list[Path]) -> None:
    """Кладёт файлы в буфер так же, как это делает проводник (CF_HDROP)."""
    quoted = ",".join(f"'{p}'" for p in paths)
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         f"Set-Clipboard -Path {quoted}"],
        check=True, capture_output=True,
    )


def main() -> int:
    with Clipboard():
        saved = Clipboard.text()
    print(f"исходный буфер сохранён ({len(saved or '')} символов)\n")

    try:
        print("[1] Текст в буфере опознаётся как текст")
        set_clipboard_text("Привет из Gesture Bridge")
        grab = _from_clipboard()
        check("kind", grab.kind, "text")
        check("содержимое", grab.text, "Привет из Gesture Bridge")
        check("подпись осмысленная", grab.preview(), "Привет из Gesture Bridge")

        print("\n[2] Пробельный буфер не считается добычей")
        set_clipboard_text("   \r\n  ")
        check("kind", _from_clipboard().kind, "none")

        print("\n[3] Файлы в буфере опознаются как файлы")
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "отчёт.txt"
            second = Path(tmp) / "снимок.png"
            first.write_text("данные", encoding="utf-8")
            second.write_bytes(b"\x89PNG\r\n\x1a\n")

            stage_files_in_clipboard([first, second])
            grab = _from_clipboard()
            check("kind", grab.kind, "files")
            check("количество", len(grab.files), 2)
            check("имена на месте",
                  sorted(f.name for f in grab.files),
                  sorted(["отчёт.txt", "снимок.png"]))
            check("пути существуют", all(f.exists() for f in grab.files), True)
            check("подпись упоминает оба",
                  grab.preview().startswith("2 файлов"), True)

            print("\n[4] Один файл подписывается просто именем")
            stage_files_in_clipboard([first])
            grab = _from_clipboard()
            check("kind", grab.kind, "files")
            check("подпись", grab.preview(), "отчёт.txt")

        print("\n[5] Юникод в путях не ломает чтение CF_HDROP")
        with tempfile.TemporaryDirectory() as tmp:
            tricky = Path(tmp) / "файл с пробелом и ёлкой 🎄.txt"
            tricky.write_text("x", encoding="utf-8")
            stage_files_in_clipboard([tricky])
            grab = _from_clipboard()
            check("имя прочитано целиком", grab.files[0].name, tricky.name)

    finally:
        if saved is not None:
            set_clipboard_text(saved)
            print("\nисходный буфер возвращён")

    print(f"\n{'=' * 46}\nпройдено: {ok_count}   провалено: {fail_count}")
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
