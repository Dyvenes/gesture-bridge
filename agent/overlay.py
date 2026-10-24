"""Оверлей поверх всех окон: что сейчас в воздухе и от кого.

Это не украшение, а то, что делает жест однозначным. Ладонью нельзя
выбрать источник — значит человек должен видеть, что именно он берёт,
ещё до того, как показал ладонь.

Сделано на tkinter из стандартной библиотеки: лишняя зависимость ради
одного окошка не нужна. tkinter обязан жить в главном потоке, поэтому
остальная работа агента идёт в отдельных потоках и общается с оверлеем
через потокобезопасную очередь.
"""

from __future__ import annotations

import queue
import tkinter as tk
from dataclasses import dataclass

BG = "#16181d"
FG = "#e8eaed"
DIM = "#9aa0a6"
ACCENT = "#7cc4ff"
WARN = "#ffcc66"

WIDTH = 380
MARGIN = 24


@dataclass
class Card:
    """Что показать. ttl=None — висит, пока не сменят."""
    title: str
    hint: str = ""
    extra: str = ""
    accent: str = ACCENT
    ttl: float | None = None


class Overlay:
    def __init__(self) -> None:
        self.inbox: "queue.Queue[Card | None]" = queue.Queue()
        self._hide_after: str | None = None

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)          # без рамки и заголовка
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.95)
        self.root.configure(bg=BG)

        frame = tk.Frame(self.root, bg=BG, padx=16, pady=12)
        frame.pack(fill="both", expand=True)

        self._bar = tk.Frame(frame, bg=ACCENT, height=3)
        self._bar.pack(fill="x", pady=(0, 10))

        self._title = tk.Label(frame, text="", bg=BG, fg=FG,
                               font=("Segoe UI", 11, "bold"),
                               wraplength=WIDTH - 40, justify="left",
                               anchor="w")
        self._title.pack(fill="x")

        self._hint = tk.Label(frame, text="", bg=BG, fg=ACCENT,
                              font=("Segoe UI", 10),
                              wraplength=WIDTH - 40, justify="left", anchor="w")
        self._hint.pack(fill="x", pady=(6, 0))

        self._extra = tk.Label(frame, text="", bg=BG, fg=DIM,
                               font=("Segoe UI", 9),
                               wraplength=WIDTH - 40, justify="left", anchor="w")
        self._extra.pack(fill="x", pady=(4, 0))

        self.root.after(80, self._drain)

    # --- наружу (зовут из любого потока) ---------------------------------

    def show(self, card: Card) -> None:
        self.inbox.put(card)

    def hide(self) -> None:
        self.inbox.put(None)

    # --- внутри (главный поток) ------------------------------------------

    def _place(self) -> None:
        self.root.update_idletasks()
        height = self.root.winfo_reqheight()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = screen_w - WIDTH - MARGIN
        y = screen_h - height - MARGIN - 48      # повыше панели задач
        self.root.geometry(f"{WIDTH}x{height}+{x}+{y}")

    def _drain(self) -> None:
        try:
            while True:
                card = self.inbox.get_nowait()
                if card is None:
                    self._do_hide()
                else:
                    self._do_show(card)
        except queue.Empty:
            pass
        self.root.after(80, self._drain)

    def _do_show(self, card: Card) -> None:
        if self._hide_after is not None:
            self.root.after_cancel(self._hide_after)
            self._hide_after = None

        self._title.configure(text=card.title)
        self._hint.configure(text=card.hint, fg=card.accent)
        self._extra.configure(text=card.extra)
        self._bar.configure(bg=card.accent)

        self._place()
        self.root.deiconify()
        self.root.attributes("-topmost", True)

        if card.ttl is not None:
            self._hide_after = self.root.after(int(card.ttl * 1000),
                                               self._do_hide)

    def _do_hide(self) -> None:
        self._hide_after = None
        self.root.withdraw()

    def run(self) -> None:
        self.root.mainloop()

    def stop(self) -> None:
        try:
            self.root.quit()
        except tk.TclError:
            pass
