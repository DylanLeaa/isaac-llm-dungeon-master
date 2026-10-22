"""LLM Dungeon Master as a desktop app: what the exe runs.

The same backend as server.py, with a window instead of a console:
  Current run   what the DM is doing this run: its bill, its work, every decision, its plans, its decks
  History       every past run's receipt
  Settings      OpenRouter / Google AI Studio key or Claude Code, and the models, applied to the running DM straight away
  Log           what the console used to show

Settings live in %APPDATA%/LLM Dungeon Master/config.json, the backend's log next to them.

Run from source:  python app.py            python app.py --mock   (no key needed)
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import logging
import logging.handlers
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import font as tkfont, messagebox, ttk

import appdata
import bridge
import deckview
import epilogue
import model_catalog
import modinstall
import receipt as receipts
from claude_code import find_claude
from receipt import Receipt
from server import Coordinator, Engine, build_dm, coverage_table

log = logging.getLogger("llmdm")

DEFAULT_CRUELTY = 6           # until the game's DM Settings menu says otherwise
# The mod pings every 2 s, but only while the game renders: a floor's stage transition (a chapter's
# nightmare most of all) goes quiet for a while. This long without a message and the game is gone.
GAME_TIMEOUT_S = 15.0
LOG_LINES = 3000
KEYS_URL = "https://openrouter.ai/keys"
from google_ai import KEYS_URL as GOOGLE_KEYS_URL
# Text colours, set on the widget: a colour in a derived ttk style doesn't always survive the theme.
BAD, GOOD, MUTED, LINK = "#ff8a7a", "#7fd18b", "#9a9a9a", "#7fb8ff"

# Treeview row colours, by receipt status.
STATUS_TAGS = {"applied": "#7fd18b", "written": "#7fb8ff", "reused": "#7fd18b", "keep": "#7fd18b",
               "timeout": "#ff8a7a", "error": "#ff8a7a", "fallback": "#ffb86b",
               "replaced": "#8a8a8a", "superseded": "#8a8a8a", "discarded": "#8a8a8a"}


# ====================================================================== the DM, on its own thread
class DMThread:
    """The backend proper (a Coordinator and the game link) on its own asyncio loop."""

    def __init__(self, engine: Engine, rec: Receipt, logs: list[Path], isaac_dirs: list[Path]) -> None:
        self.engine, self.receipt, self.logs, self.isaac_dirs = engine, rec, logs, isaac_dirs
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task | None = None
        self.coord: Coordinator | None = None
        self.error: str | None = None
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, name="dm", daemon=True)

    def start(self) -> None:
        self.thread.start()
        self.ready.wait(5)

    def alive(self) -> bool:
        return self.thread.is_alive()

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except asyncio.CancelledError:
            pass                                  # stopped
        except Exception as e:                    # noqa: BLE001 - shown in the window, with a restart button
            self.error = f"{type(e).__name__}: {e}"
            log.exception("the DM stopped")
        finally:
            self.ready.set()

    async def _main(self) -> None:
        e = self.engine
        self.coord = Coordinator(e.dm, DEFAULT_CRUELTY, e.planner, max_concurrency=e.concurrency,
                                 receipt=self.receipt, gate=e.gate, late_grace_s=e.late_grace)
        self.loop, self.task = asyncio.get_running_loop(), asyncio.current_task()
        self.ready.set()
        e.describe()
        try:
            await bridge.serve(self.coord.handle, self.logs, self.isaac_dirs)
        finally:
            self.coord.summary()

    def apply(self, engine: Engine) -> None:
        """Swap in new models; the run carries on."""
        async def swap() -> None:
            self.coord.reconfigure(engine)
        asyncio.run_coroutine_threadsafe(swap(), self.loop).result(5)
        self.engine = engine

    def stop(self) -> None:
        if self.loop and self.task and self.alive():
            self.loop.call_soon_threadsafe(self.task.cancel)
            self.thread.join(3)

    def game_connected(self) -> bool:
        return bool(self.coord and self.coord.last_seen and time.monotonic() - self.coord.last_seen < GAME_TIMEOUT_S)


# ====================================================================== small widgets
def text_box(master, height: int = 10, wrap: str = "word") -> tuple[ttk.Frame, tk.Text]:
    """A read-only dark Text with a scrollbar."""
    frame = ttk.Frame(master)
    text = tk.Text(frame, height=height, wrap=wrap, relief="flat", borderwidth=0, padx=8, pady=6,
                   bg="#1f1f1f", fg="#e4e4e4", insertbackground="#e4e4e4", selectbackground="#2f5f8f",
                   font=("Consolas", 10), state="disabled")
    bar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=bar.set)
    text.pack(side="left", fill="both", expand=True)
    bar.pack(side="right", fill="y")
    return frame, text


def set_text(widget: tk.Text, value: str) -> None:
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", value)
    widget.configure(state="disabled")


class CellTip:
    """Hover a Treeview cell whose text is cut off and a tooltip shows all of it."""

    DELAY_MS = 400

    def __init__(self, tree: ttk.Treeview) -> None:
        self.tree, self.tip, self.cell, self.job = tree, None, None, None
        tree.bind("<Motion>", self._move, add="+")
        tree.bind("<Leave>", lambda _e: self._cancel(), add="+")
        tree.bind("<ButtonPress>", lambda _e: self._cancel(), add="+")
        tree.bind("<MouseWheel>", lambda _e: self._cancel(), add="+")

    def _move(self, e) -> None:
        row, col = self.tree.identify_row(e.y), self.tree.identify_column(e.x)
        cell = (row, col) if row and col and self.tree.identify_region(e.x, e.y) in ("cell", "tree") else None
        if cell == self.cell:
            return
        self._cancel()
        if cell:
            self.cell = cell
            self.job = self.tree.after(self.DELAY_MS, lambda: self._show(cell, e.x_root, e.y_root))

    def _show(self, cell: tuple[str, str], x: int, y: int) -> None:
        self.job = None
        row, col = cell
        try:
            index = int(col.lstrip("#")) - 1
            if index < 0:                                     # the tree column: indented by depth
                key, text = "#0", str(self.tree.item(row, "text"))
                depth, parent = 1, self.tree.parent(row)
                while parent:
                    depth, parent = depth + 1, self.tree.parent(parent)
                indent = 20 * depth
            else:
                shown = self.tree["displaycolumns"]
                key = self.tree["columns"][index] if shown in ("#all", ("#all",)) else shown[index]
                text, indent = str(self.tree.set(row, key)), 0
            width = self.tree.column(key, "width") - indent
        except (tk.TclError, ValueError, IndexError):
            return
        font = tkfont.nametofont("TkDefaultFont")
        if not text or font.measure(text) <= width - 12:      # it all fits: nothing to add
            return
        self.tip = tip = tk.Toplevel(self.tree)
        tip.wm_overrideredirect(True)
        tk.Label(tip, text=text, justify="left", wraplength=620, bg="#2b2b2b", fg="#e4e4e4", relief="solid",
                 borderwidth=1, padx=6, pady=4, font=("Segoe UI", 9)).pack()
        tip.update_idletasks()
        w, h = tip.winfo_reqwidth(), tip.winfo_reqheight()
        sw, sh = tip.winfo_screenwidth(), tip.winfo_screenheight()
        left = max(0, min(x + 14, sw - w - 8))
        top = y + 18 if y + 18 + h <= sh - 8 else max(0, y - h - 8)    # flip above the cursor near the bottom
        tip.wm_geometry(f"+{left}+{top}")

    def _cancel(self) -> None:
        self.cell = None
        if self.job:
            self.tree.after_cancel(self.job)
            self.job = None
        if self.tip:
            self.tip.destroy()
            self.tip = None


def open_path(path: Path) -> None:
    path = Path(path)
    if path.is_file() or path.is_dir():
        target = path
    else:
        target = path.parent
        target.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(target)                      # noqa: S606 - the player's own folder
    else:
        subprocess.Popen(["xdg-open", str(target)])


class Card(ttk.Frame):
    """A number with a caption."""

    def __init__(self, master, caption: str) -> None:
        super().__init__(master, style="Card.TFrame", padding=(10, 5))
        self.value = ttk.Label(self, text="-", style="CardValue.TLabel")
        self.value.pack(anchor="w")
        self.caption = ttk.Label(self, text=caption, foreground=MUTED, wraplength=130)
        self.caption.pack(anchor="w")

    def set(self, value: str, caption: str | None = None) -> None:
        self.value.configure(text=value)
        if caption is not None:
            self.caption.configure(text=caption)


# A decision's Effect column before the game reports one, by status.
NO_EFFECT = {"ok": "not used yet", "replaced": "never used"}


# ====================================================================== one run's receipt
class RunView(ttk.Frame):
    """A run's receipt: summary on top, every entry below, the selected one in full.

    With spoiler mode off it shows only what the game used, in the order it used it, and none of
    the DM's reasoning: no status, reason, intent or result columns, no entry details, no plans, no decks.

    Its Deck view shows every deck the DM wrote, in draw order (deckview.py): live in Current run, and
    in History as of the row picked in the receipt.
    """

    COLUMNS = (("time", "Generated", 105), ("applied_time", "Applied", 95), ("what", "What", 90),
               ("key", "Key", 105), ("status", "Status", 75), ("reason", "Why", 150),
               ("intent", "Intent", 230), ("result", "Result", 220), ("effect", "Effect", 240),
               ("ms", "ms", 55), ("cost", "Cost", 65))
    WIDE = ("intent", "result", "effect")
    SPOILERS = ("status", "reason", "intent", "result")    # hidden with spoiler mode off

    def __init__(self, master, empty: str, spoilers: bool = False, pin_decks: bool = False) -> None:
        super().__init__(master, padding=(12, 10))
        self.empty = empty
        self.r: Receipt | None = None
        self.spoilers = spoilers
        self.pin_decks = pin_decks          # the Deck view as of the selected row (History), else live
        self.decks_shown: tuple | None = None   # what the Deck view was last drawn from
        self.decks_run: Receipt | None = None   # the run its open / closed decks belong to

        head = ttk.Frame(self)
        head.pack(fill="x")
        self.title = ttk.Label(head, text=empty, style="Title.TLabel")
        self.title.pack(anchor="w")
        self.subtitle = ttk.Label(head, text="", foreground=MUTED)
        self.subtitle.pack(anchor="w", pady=(0, 8))

        cards = ttk.Frame(self)
        cards.pack(fill="x")
        self.cards = {k: Card(cards, c) for k, c in (
            ("bill", "cost"), ("tokens", "tokens in/cached/out"), ("applied", "decisions used"),
            ("replaced", "decisions made"), ("fallback", "DM gave up"), ("problems", "timeouts / errors"))}
        self.card_cols = 0
        cards.bind("<Configure>", lambda e: self._lay_cards(cards, 6 if e.width >= 860 else 3))
        self._lay_cards(cards, 6)

        mid = ttk.Frame(self)
        mid.pack(fill="x", pady=(10, 6))
        self.tally = ttk.Frame(mid)
        self.tally.pack(side="left", anchor="n")
        self.words = ttk.Label(mid, text="", wraplength=520, justify="left")
        self.words.pack(side="left", anchor="n", padx=(24, 0), fill="x", expand=True)
        # Wrap the DM's words to whatever room the tally leaves.
        self.words.bind("<Configure>", lambda e: self.words.configure(wraplength=max(200, e.width - 8)))

        self.panes = panes = ttk.PanedWindow(self, orient="vertical")
        panes.pack(fill="both", expand=True)
        table = ttk.Frame(panes)
        self.tree = ttk.Treeview(table, columns=[c[0] for c in self.COLUMNS], show="headings", height=10,
                                 selectmode="browse")
        for key, label, width in self.COLUMNS:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, stretch=key in self.WIDE, anchor="e" if key in ("ms", "cost") else "w")
        for status, colour in STATUS_TAGS.items():
            self.tree.tag_configure(status, foreground=colour)
        bar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._show_selected())
        CellTip(self.tree)
        panes.add(table, weight=3)

        self.details = tabs = ttk.Notebook(panes)
        entry_frame, self.entry_text = text_box(tabs, height=8, wrap="none")
        plan_frame, self.plan_text = text_box(tabs, height=8)
        tabs.add(entry_frame, text="Selected entry")
        tabs.add(plan_frame, text="The DM's plan")
        self.deck_frame = self._deck_tab(tabs)
        tabs.add(self.deck_frame, text="Deck view")
        tabs.bind("<<NotebookTabChanged>>", lambda _e: self._show_decks())
        panes.add(tabs, weight=2)
        self.set_spoilers(spoilers)

    def set_spoilers(self, on: bool) -> None:
        """Spoiler mode: everything (on), or only what the game has used so far (off)."""
        self.spoilers = on
        self.tree.configure(displaycolumns=[c[0] for c in self.COLUMNS if on or c[0] not in self.SPOILERS])
        shown = str(self.details) in map(str, self.panes.panes())
        if on and not shown:
            self.panes.add(self.details, weight=2)
        elif not on and shown:
            self.panes.forget(self.details)
        self.show(self.r)

    def _lay_cards(self, frame: ttk.Frame, cols: int) -> None:
        """One row of six when there's room, else two rows of three."""
        if cols == self.card_cols:
            return
        self.card_cols = cols
        for i, card in enumerate(self.cards.values()):
            card.grid(row=i // cols, column=i % cols, sticky="nsew",
                      padx=(0 if i % cols == 0 else 6, 0), pady=(0 if i < cols else 6, 0))
        for c in range(6):
            frame.columnconfigure(c, weight=1 if c < cols else 0)

    # ------------------------------------------------------------ filling
    def show(self, r: Receipt | None) -> None:
        self.r = r
        self.tree.delete(*self.tree.get_children())
        rows = sorted(r.full) if r else []
        if r and not self.spoilers:     # in the order the game used them
            rows = sorted(filter(self._visible, rows), key=lambda s: (r.full[s].get("applied_ts") or 0, s))
        for seq in rows:
            self._insert(seq)
        set_text(self.entry_text, "Pick an entry above to see everything behind it." if r else "")
        self.decks_shown = None
        if r is not self.decks_run:                          # another run: open and closed start over
            self.decks_run = r
            self.deck_toggled.clear()
        self.refresh()

    def added(self, seq: int) -> None:
        self._insert(seq)

    def updated(self, seq: int) -> None:
        if not self.r:
            return
        if not self.tree.exists(str(seq)):
            self._insert(seq)                     # just used: spoiler mode off shows it now
        elif not self._visible(seq):
            self.tree.delete(str(seq))
        else:
            self.tree.item(str(seq), values=self._row(seq), tags=(self.r.full[seq].get("status"),))
            if self.tree.selection() == (str(seq),):
                self._show_selected()

    def _visible(self, seq: int) -> bool:
        if self.r.full[seq].get("what") == "rolls":           # the roll audit has its own section above
            return False
        return self.spoilers or self.r.full[seq].get("status") == "applied"

    def _insert(self, seq: int) -> None:
        if self._visible(seq) and not self.tree.exists(str(seq)):
            self.tree.insert("", 0, iid=str(seq), values=self._row(seq), tags=(self.r.full[seq].get("status"),))

    def _row(self, seq: int) -> tuple:
        e = self.r.full[seq]
        c = receipts.compact(e)
        intent = c.get("intent") or ""
        if c.get("taunt"):
            intent = f'{intent}  "{c["taunt"]}"' if intent else f'"{c["taunt"]}"'
        # What was decided (after the guardrails, with what they changed), or what happened.
        result = c.get("result") or c.get("error") or ""
        if c.get("clamped"):
            result = f'{result}  [clamped: {"; ".join(c["clamped"])}]'
        # What a decision did in the game, once the game says.
        if e.get("effect"):
            effect = "; ".join(e["effect"])
        else:
            effect = NO_EFFECT.get(c["status"], "no effect reported") if c.get("intent") else ""
        cost = f'${c["cost"]:.4f}' if c.get("cost") else ""
        return (c["time"], c.get("applied_time", ""), c["what"], c.get("key", ""), c["status"], c.get("reason", ""),
                intent, result, effect, c.get("ms", ""), cost)

    def _show_selected(self) -> None:
        sel = self.tree.selection()
        if self.pin_decks:
            self._show_decks()
        if not sel or not self.r:
            return
        entry = self.r.full.get(int(sel[0]))
        if entry is None:
            return
        text = json.dumps(entry, indent=2, ensure_ascii=False, default=str)
        for h in (entry.get("llm") or {}).get("system") or []:
            if h in self.r.prompt_text:
                text += f"\n\n----- system prompt {h} -----\n{self.r.prompt_text[h]}"
        set_text(self.entry_text, text)

    # ------------------------------------------------------------ the Deck view
    def _deck_tab(self, master) -> ttk.Frame:
        frame = ttk.Frame(master)
        self.deck_note = ttk.Label(frame, text="", foreground=MUTED, padding=(8, 6, 8, 4))
        self.deck_note.pack(fill="x")
        inner = ttk.Frame(frame)
        inner.pack(fill="both", expand=True)
        t = self.decks = ttk.Treeview(inner, columns=("state", "detail"), show="tree headings", selectmode="browse")
        t.heading("#0", text="Deck / what it hands out, in order")
        t.heading("state", text="State")
        t.heading("detail", text="Intent / how it was drawn")
        t.column("#0", width=330, stretch=False)
        t.column("state", width=170, stretch=False)
        t.column("detail", width=400, stretch=True)
        CellTip(t)
        t.tag_configure("floor", foreground="#9fc5ff", font=("Segoe UI Semibold", 10))
        t.tag_configure("next", foreground=STATUS_TAGS["applied"])
        for tag in ("drawn", "skipped", "used", "unused", "spent"):
            t.tag_configure(tag, foreground=MUTED)
        t.tag_configure("locked", foreground=STATUS_TAGS["written"])
        t.tag_configure("focus", background="#2f4f6f")
        bar = ttk.Scrollbar(inner, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=bar.set)
        t.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        # What the player opened or closed stays so across redraws; the rest opens while it has something left.
        self.deck_toggled: dict[str, bool] = {}
        t.bind("<<TreeviewOpen>>", lambda _e: self.deck_toggled.__setitem__(t.focus(), True))
        t.bind("<<TreeviewClose>>", lambda _e: self.deck_toggled.__setitem__(t.focus(), False))
        return frame

    def _show_decks(self) -> None:
        """Redraw the Deck view if it's on screen and what it shows has moved (it's rebuilt from the receipt)."""
        if not self.spoilers or self.details.select() != str(self.deck_frame):
            return                        # hidden: drawn when it's shown
        r = self.r
        upto = None
        if self.pin_decks and r:
            sel = self.tree.selection()
            upto = int(sel[0]) if sel else None
        sig = (id(r), upto, r.seq if r else 0, len(r.draws) if r else 0)
        if sig == self.decks_shown:
            return
        self.decks_shown = sig
        t = self.decks
        top, picked = t.yview()[0], t.selection()
        opened = self.deck_toggled.get
        t.delete(*t.get_children())
        if r is None:
            self.deck_note.configure(text="")
            return
        floors = deckview.build(r, upto)
        if not self.pin_decks:
            note = "Live: what every deck will hand out next, in the order the game draws it. Green is the next draw."
        elif upto is None:
            note = "As of the end of the run. Pick a row in the receipt above to see the decks as they stood then."
        else:
            e = r.full.get(upto) or {}
            when = time.strftime("%H:%M:%S", time.localtime(e.get("ts") or 0))
            note = f'As of #{upto} {e.get("what", "")} {e.get("key") or ""} at {when}, the row picked above.'
        if not floors:
            note += "  No decks yet."
        self.deck_note.configure(text=note)

        focus_key = (r.full.get(upto) or {}).get("key") if upto else None
        for n, fl in enumerate(floors):
            fid = f"floor:{fl.number}"
            open_decks = sum(not d.spent for d in fl.decks)
            t.insert("", "end", iid=fid, text=f"Floor {fl.number}" + (f": {fl.name}" if fl.name else ""),
                     values=(f"{open_decks} open of {len(fl.decks)}" if n == 0 else "over", ""),
                     open=opened(fid, n == 0), tags=("floor",))
            for d in fl.decks:
                did = f"deck:{d.key}"
                about = d.intent + (f'  "{d.taunt}"' if d.taunt else "")
                tags = ("spent",) if d.spent else ()
                if d.key == focus_key:
                    tags += ("focus",)
                t.insert(fid, "end", iid=did, text=d.title, values=(d.status, about),
                         open=opened(did, not d.spent), tags=tags)
                if d.drawn:
                    hid = f"{did}:drawn"
                    gave = sum(s.state == "drawn" for s in d.drawn)
                    t.insert(did, "end", iid=hid, text=f"{gave} drawn so far", values=("", ""),
                             open=opened(hid, False), tags=("drawn",))
                    for i, s in enumerate(d.drawn):
                        t.insert(hid, "end", iid=f"{hid}:{i}", text=s.label, values=self._slot_values(s),
                                 tags=(s.state,))
                for i, s in enumerate(d.left):
                    t.insert(did, "end", iid=f"{did}:{i}", text=s.label, values=self._slot_values(s, i),
                             tags=(s.state,))
        t.yview_moveto(top)
        if picked and t.exists(picked[0]):
            t.selection_set(picked[0])
        if focus_key and t.exists(f"deck:{focus_key}"):     # the picked row's own deck, in sight
            did = f"deck:{focus_key}"
            t.item(t.parent(did), open=True)
            t.item(did, open=True)
            t.see(did)

    @staticmethod
    def _slot_values(s: deckview.Slot, i: int = 0) -> tuple[str, str]:
        at = time.strftime("%H:%M:%S", time.localtime(s.ts)) if s.ts else ""
        state = {"drawn": f"drawn {at}", "skipped": "discarded", "next": "▶ next", "queued": f"then (#{i + 1})",
                 "waiting": "waiting", "used": f"used {at}", "set": "set, not swallowed yet",
                 "locked": f"swallowed {at}, locked", "loaded": "every tinted rock pays this",
                 "unused": "never drawn"}.get(s.state, s.state)
        return state, s.cause

    def refresh(self) -> None:
        """Redo the summary and the plans from the receipt (cheap: a run is a few hundred entries)."""
        r = self.r
        self._show_decks()
        for w in self.tally.winfo_children():
            w.destroy()
        if r is None or not r.full:
            self.title.configure(text=self.empty)
            self.subtitle.configure(text="")
            for card in self.cards.values():
                card.set("-")
            self.words.configure(text="")
            set_text(self.plan_text, "")
            return
        t = r.totals()
        narrative, brief = r.plans()
        summary = r.run_summary()
        title = (summary or {}).get("title") or epilogue.run_title(narrative)
        self.title.configure(text=f'"{title}"' if title else f"Run {r.run_id or '?'}")
        self.subtitle.configure(text=str(r.run_id) if r.run_id else "")
        self.cards["bill"].set(f'${t["cost"]:.4f}')
        self.cards["tokens"].set(f'{_k(t["in"])} / {_k(t["cached"])} / {_k(t["out"])}')
        self.cards["applied"].set(str(t["applied"]))
        self.cards["bill"].caption.configure(text=f'cost, {t["calls"]} calls')
        self.cards["replaced"].set(str(t["applied"] + t["replaced"] + t["superseded"]))
        self.cards["fallback"].set(str(t["fallback"]))
        self.cards["problems"].set(f'{t["timeout"]} / {t["error"]}')

        rows, scheme, taunt = epilogue.dm_side(r.applied(), t, [], drawn=r.drawn or None)
        sections = [("THE DM", rows)]
        if summary:
            sections = [(s["title"], s["rows"]) for s in summary.get("sections", [])]
        on_cards = ("LLM bill", "DM gave up")
        sections = [(name, [row for row in rows if row[0] not in on_cards]) for name, rows in sections]
        # The game's rolls: which the DM's streams owned, and which went to vanilla (worst first).
        cov = coverage_table(r.coverage)
        if cov:
            ctl, free = sum(c[2] for c in cov), sum(c[3] for c in cov)
            sections.append(("ROLLS", [["controlled / vanilla", f"{ctl} / {free}"]] +
                             [[f"{h} {c}", f"{a} / {b}"] for h, c, a, b in cov[:7]]))
        col = 0
        for name, sec_rows in sections:
            ttk.Label(self.tally, text=name, style="Section.TLabel").grid(row=0, column=col, columnspan=2, sticky="w")
            for i, (label, value) in enumerate(sec_rows, start=1):
                ttk.Label(self.tally, text=label, foreground=MUTED).grid(row=i, column=col, sticky="w",
                                                                             padx=(0, 10))
                ttk.Label(self.tally, text=value).grid(row=i, column=col + 1, sticky="w", padx=(0, 28))
            col += 2

        words = []
        if summary:
            who = summary.get("character") or "The player"
            words.append(f'{who} {"died" if summary.get("outcome") == "death" else "won"}.')
            if summary.get("epitaph"):
                words.append(f'Epitaph: "{summary["epitaph"]}"')
            scheme, taunt = summary.get("scheme") or scheme, summary.get("taunt") or taunt
        if scheme:
            words.append(f"Scheme: {scheme}")
        if taunt:
            words.append(f'Last taunt: "{taunt}"')
        self.words.configure(text="\n\n".join(words))

        plan = []
        if narrative:
            plan.append("RUN NARRATIVE\n\n" + narrative.strip())
        if brief:
            plan.append("LATEST FLOOR BRIEF\n\n" + brief.strip())
        set_text(self.plan_text, "\n\n\n".join(plan) or "No plans in this receipt (planners off, or not written yet).")


def engine_text(engine: Engine) -> str:
    """Who plays the DM, for the header: "DM: <model> via <backend>  |  planners: <model>"."""
    text = "DM: " + engine.label.split(" (")[0]
    if engine.planner:
        text += f"  |  planners: {engine.planner.narrative_model}"
    return text


def _k(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    return f"{n / 1000:.1f}k" if n >= 10_000 else str(n)


# ====================================================================== the model picker
ROLE_NAMES = {"major": "Narrative", "minor": "DM", "gate": "Re-ask gate"}
PICKER_TITLES = {"major": "Narrative model selector",
                 "minor": "DM model selector",
                 "gate": "Re-ask gate model selector"}
PICKER_HINTS = {
    "major": "Called once per run and once per floor: it can afford to be slow and clever.",
    "minor": "Called often, needs to be quick.",
    "gate": "Called to check if the DM should be asked again.",
}
PINNED = "#d6a8ff"


class ModelPicker(tk.Toplevel):
    """Every model a role can use: filter, sort by any column (intelligence first), pick one.
    Claude Code's models, when it's on, are pinned to the top whatever the sort."""

    COLUMNS = (("whitelisted", "Whitelisted", 85), ("name", "Model", 300), ("intelligence", "Intelligence", 95),
               ("prompt", "Input $/M", 85), ("completion", "Output $/M", 90), ("cache_read", "Cached $/M", 90),
               ("turn_cost", "Turn cost", 85), ("latency", "Latency", 70))
    NUMERIC = ("intelligence", "prompt", "completion", "cache_read", "turn_cost", "latency")
    PROBED = ("whitelisted", "turn_cost", "latency")   # the DM whitelist's columns: the minor role only

    def __init__(self, master, role: str, pinned: list, rows: list, current: str, on_pick) -> None:
        super().__init__(master)
        self.pinned, self.rows, self.current, self.on_pick = pinned, rows, current, on_pick
        self.by_value = {m.value: m for m in pinned + rows}
        scored = any(m.intelligence is not None for m in rows)
        probed = any(m.whitelisted is not None for m in rows)
        self.sort_key, self.desc = ("intelligence", True) if scored else ("created", True)
        self.title(PICKER_TITLES[role])
        self.transient(master)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{min(1180, sw - 80)}x{min(700, sh - 120)}")
        self.minsize(760, 420)

        body = ttk.Frame(self, padding=(14, 12))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=PICKER_HINTS[role], foreground=MUTED, wraplength=1000, justify="left").pack(
            anchor="w", pady=(0, 8))

        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="Search").pack(side="left")
        self.query = tk.StringVar()
        search = ttk.Entry(bar, textvariable=self.query, width=32)
        search.pack(side="left", padx=(6, 14))
        self.query.trace_add("write", lambda *_: self._fill())
        self.provider = tk.StringVar(value="All providers")
        names = model_catalog.PROVIDERS
        box = ttk.Combobox(bar, textvariable=self.provider, state="readonly", width=22,
                           values=["All providers", *(names[b] for b in names if any(m.backend == b for m in pinned + rows))])
        box.pack(side="left")
        box.bind("<<ComboboxSelected>>", lambda _e: self._fill())
        self.scored = tk.BooleanVar(value=False)
        if scored:
            ttk.Checkbutton(bar, text="Only with intelligence score", variable=self.scored,
                            command=self._fill).pack(side="left", padx=(14, 0))
        self.whitelisted = tk.BooleanVar(value=probed)
        if probed:
            ttk.Checkbutton(bar, text="Whitelist Only", variable=self.whitelisted,
                            command=self._fill).pack(side="left", padx=(14, 0))
        self.count = ttk.Label(bar, text="", foreground=MUTED)
        self.count.pack(side="right")

        table = ttk.Frame(body)
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=[c[0] for c in self.COLUMNS], show="headings", selectmode="browse")
        for key, label, width in self.COLUMNS:
            self.tree.heading(key, text=label, command=lambda k=key: self._sort(k))
            self.tree.column(key, width=width, stretch=key == "name",
                             anchor="e" if key in self.NUMERIC else "center" if key == "whitelisted" else "w")
        hidden = () if probed else self.PROBED
        if not scored:                            # the gate's decisions models: no score, priced on input only
            hidden += ("intelligence", "completion")
        self.tree.configure(displaycolumns=[c[0] for c in self.COLUMNS if c[0] not in hidden])
        self.tree.tag_configure("pinned", foreground=PINNED)
        self.tree.tag_configure("current", foreground=GOOD)
        bar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._show_selected())
        self.tree.bind("<Double-1>", lambda _e: self._use())

        self.about = ttk.Label(body, text="", foreground=MUTED, wraplength=1000, justify="left")
        self.about.pack(fill="x", pady=(8, 0))
        self.about.bind("<Configure>", lambda e: self.about.configure(wraplength=max(300, e.width - 8)))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="Use this model", style="Accent.TButton", command=self._use).pack(side="left")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="left", padx=8)
        self.bind("<Escape>", lambda _e: self.destroy())
        self.bind("<Return>", lambda _e: self._use())

        self._fill()
        if self.tree.exists(current):
            self.tree.selection_set(current)
            self.after(100, self._reveal_current)
        windows_look(self)
        search.focus_set()
        try:
            self.grab_set()                       # modal, while it's open
        except tk.TclError:
            pass

    def _reveal_current(self) -> None:
        """Scroll to the current pick, only if it's out of sight (so the pinned rows stay in view)."""
        if self.tree.exists(self.current) and not self.tree.bbox(self.current):
            self.tree.see(self.current)

    def _sort(self, key: str) -> None:
        """Click a heading to sort by it, again to reverse. Scores and whitelisted first, the rest low first."""
        if key == self.sort_key:
            self.desc = not self.desc
        else:
            self.sort_key, self.desc = key, key in ("intelligence", "whitelisted")
        self._fill()

    def _fill(self) -> None:
        q = self.query.get()
        backend = next((b for b, name in model_catalog.PROVIDERS.items() if name == self.provider.get()), "")
        pinned = model_catalog.filter_rows(self.pinned, q, scored_only=self.scored.get(),
                                           whitelist_only=self.whitelisted.get(), backend=backend)
        rows = model_catalog.filter_rows(self.rows, q, scored_only=self.scored.get(),
                                         whitelist_only=self.whitelisted.get(), backend=backend)
        rows = model_catalog.sort_rows(rows, self.sort_key, self.desc)
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for m in pinned + rows:
            tags = ("current",) if m.value == self.current else ("pinned",) if m.backend == "claude-code" else ()
            self.tree.insert("", "end", iid=m.value, values=self._row(m), tags=tags)
        for key, label, _width in self.COLUMNS:
            arrow = (" ▼" if self.desc else " ▲") if key == self.sort_key else ""
            self.tree.heading(key, text=label + arrow)
        if selected and self.tree.exists(selected[0]):
            self.tree.selection_set(selected[0])
        self.count.configure(text=f"{len(pinned) + len(rows)} of {len(self.pinned) + len(self.rows)} models")

    @staticmethod
    def _row(m: model_catalog.ModelInfo) -> tuple:
        price = model_catalog.price
        if m.backend == "claude-code":
            prices = ("your plan",) * 3
        else:
            prices = (price(m.prompt), price(m.completion), price(m.cache_read))
        listed = "-" if m.whitelisted is None else "✓" if m.whitelisted else "✗"
        score = "-" if m.intelligence is None else f"{m.intelligence:g}"
        latency = "-" if m.latency is None else f"{m.latency:.1f}s"
        return (listed, m.name, score, *prices, model_catalog.turn_price(m.turn_cost), latency)

    def _show_selected(self) -> None:
        sel = self.tree.selection()
        m = self.by_value.get(sel[0]) if sel else None
        text = m.description if m else ""
        self.about.configure(text=text[:1800] + ("..." if len(text) > 1800 else ""))

    def _use(self) -> None:
        sel = self.tree.selection()
        if sel:
            self.on_pick(sel[0])
            self.destroy()


# ====================================================================== the window
class App:
    def __init__(self, root: tk.Tk, args) -> None:
        self.root, self.args = root, args
        self.events: queue.Queue = queue.Queue()      # from the DM thread: receipt events
        self.dm: DMThread | None = None
        self.cfg = appdata.load_config()
        self.receipts_dir: Path = args.receipts
        self.isaac_dirs = args.isaac_dir or bridge.isaac_installs()
        self.logs = args.log or bridge.default_logs()
        self.mod_status = modinstall.install_all(self.isaac_dirs)
        self.current = Receipt(None, keep_full=True)
        self.dirty = False
        self.history_rows: dict[str, Path] = {}
        # The model pickers: OpenRouter's catalogue (cached, refreshed in the background) and the unsaved picks.
        self.catalog_raw = model_catalog.load_cache()
        self.catalog = model_catalog.parse(self.catalog_raw)
        self.catalog_error: str | None = None
        self.fetching = False
        self.model_picks = {role: self.cfg.get(f"{role}_model", "") for role in model_catalog.ROLES}
        self.google_key_var = tk.StringVar(value=self.cfg.get("google_api_key", ""))

        root.title("LLM Dungeon Master")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{min(1440, sw - 80)}x{min(940, sh - 110)}+{max(0, (sw - min(1440, sw - 80)) // 2)}+20")
        root.minsize(900, 600)
        self._styles()
        self._header()
        self.tabs = ttk.Notebook(root)
        self.tabs.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.run_view = RunView(self.tabs, "No run detected.",
                                spoilers=bool(self.cfg.get("spoiler_mode")))
        self.tabs.add(self.run_view, text="  Current run  ")
        self._history_tab()
        self._settings_tab()
        self._log_tab()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh_models()

        self.start_dm(first=True)
        self.refresh_history()
        self.root.after(200, self._drain)
        self.root.after(500, self._tick)

    # ------------------------------------------------------------ look
    def _styles(self) -> None:
        s = ttk.Style()
        s.configure("Title.TLabel", font=("Segoe UI Semibold", 17))
        s.configure("Header.TLabel", font=("Segoe UI Semibold", 12))
        s.configure("Section.TLabel", font=("Segoe UI Semibold", 10), foreground="#9fc5ff")
        s.configure("CardValue.TLabel", font=("Segoe UI Semibold", 15))
        s.configure("Link.TLabel", font=("Segoe UI", 10, "underline"))
        self.bg = s.lookup("TFrame", "background") or "#1c1c1c"

    def _header(self) -> None:
        bar = ttk.Frame(self.root, padding=(14, 10))
        bar.pack(fill="x")
        self.dot = tk.Canvas(bar, width=14, height=14, highlightthickness=0, bg=self.bg)
        self.dot.pack(side="left", padx=(0, 8))
        self.dot_id = self.dot.create_oval(2, 2, 13, 13, fill="#8a8a8a", outline="")
        self.status = ttk.Label(bar, text="Starting...", style="Header.TLabel")
        self.status.pack(side="left")
        self.restart = ttk.Button(bar, text="Restart DM", command=lambda: self.start_dm())
        self.dm_label = ttk.Label(bar, text="", foreground=MUTED)
        self.dm_label.pack(side="right")
        self.problem = ttk.Label(self.root, text="", foreground=BAD, padding=(14, 0, 14, 6))
        problems = []
        if not self.isaac_dirs:
            problems.append("Isaac isn't installed in any Steam library found on this PC.")
        if not any(p.exists() for p in self.logs):
            problems.append("The game's log.txt doesn't exist yet: start Isaac once.")
        statuses = self.mod_status.values()
        problems += [f"Couldn't install the mod: {s[len('failed: '):]}" for s in statuses if s.startswith("failed")]
        if "workshop" in statuses:
            problems.append("There is a Workshop copy of the mod in Isaac's mods folder, so this app's copy isn't installed.")
        note = ""
        if {"installed", "updated"} & set(statuses):
            note = "Installed this version of the mod in Isaac's mods folder: restart Isaac if it's running."
        if problems or note:
            self.problem.configure(text="  ".join(problems) or note, foreground=BAD if problems else GOOD)
            self.problem.pack(fill="x")

    def _set_status(self, colour: str, text: str) -> None:
        self.dot.itemconfigure(self.dot_id, fill=colour)
        if self.status.cget("text") != text:
            self.status.configure(text=text)

    # ------------------------------------------------------------ the DM
    def _engine(self, cfg: dict) -> Engine:
        if self.args.mock:
            return build_dm("mock", "", cruelty=DEFAULT_CRUELTY)
        dm_backend, dm_model = model_catalog.resolve(cfg, "minor")
        planner_backend, planner_model = model_catalog.resolve(cfg, "major")
        return build_dm(dm_backend, appdata.api_key(cfg), google_key=appdata.google_key(cfg),
                        cruelty=DEFAULT_CRUELTY, model=dm_model,
                        planner_backend=planner_backend, narrative_model=planner_model, floor_model=planner_model,
                        gate_model=model_catalog.resolve(cfg, "gate")[1])

    def start_dm(self, first: bool = False) -> None:
        try:
            engine = self._engine(self.cfg)
        except ValueError as e:
            log.warning("the DM isn't set up: %s", e)
            self.settings_msg.configure(text=str(e), foreground=BAD)
            if first:
                self.tabs.select(self.settings)
            return
        if self.dm:
            self.dm.stop()
        rec = Receipt(None if self.args.no_receipts else self.receipts_dir)
        rec.listener = lambda event, data: self.events.put((event, dict(data)))
        self.dm = DMThread(engine, rec, self.logs, self.isaac_dirs)
        self.dm.start()
        log.info("game: %s", ", ".join(map(str, self.isaac_dirs)) or "?")

    def _tick(self) -> None:
        dm = self.dm
        if dm is None:
            self._set_status("#ffb86b", "The DM isn't set up: go to Settings")
            self.dm_label.configure(text="")
        elif not dm.alive():
            self._set_status("#ff6b5b", f"The DM stopped: {dm.error or 'unknown error'}")
        elif dm.game_connected():
            if self.current.run_id is None and dm.receipt.run_id is not None:
                # Back after a silence that looked like the game had gone: the run is still the DM's.
                self._new_run({"run": dm.receipt.run_id, "fresh": False, "path": dm.receipt.path})
            run = self.current.run_id
            self._set_status("#4cc96b", "Game connected" + (f"  |  run {run}" if run else ""))
        else:
            self._set_status("#8a8a8a", "Waiting for Isaac")
            if self.current.full or self.current.run_id:    # no game, no current run
                self.current = Receipt(None, keep_full=True)
                self.run_view.show(None)
        if dm is not None:
            self.dm_label.configure(text=engine_text(dm.engine))
        crashed = dm is not None and not dm.alive()
        if crashed and not self.restart.winfo_ismapped():
            self.restart.pack(side="left", padx=12)
        elif not crashed and self.restart.winfo_ismapped():
            self.restart.pack_forget()
        self.root.after(500, self._tick)

    def _drain(self) -> None:
        """Mirror the DM's receipt into the Current run tab."""
        changed = False
        try:
            while True:
                event, data = self.events.get_nowait()
                changed = True
                if event == "run":
                    self._new_run(data)
                elif event == "entry":
                    if int(data["seq"]) not in self.current.full:
                        self.current.ingest(data)
                        if self.run_view.r is not self.current:    # entry beat the "run" event
                            self.run_view.show(self.current)
                        else:
                            self.run_view.added(int(data["seq"]))
                elif event == "update":
                    if self.current.apply_update(data["seq"], data["status"], data.get("ts")) and self.run_view.r:
                        self.run_view.updated(int(data["seq"]))
                elif event == "effect":
                    if self.current.apply_effect(data["seq"], data["effect"]) and self.run_view.r:
                        self.run_view.updated(int(data["seq"]))
                elif event == "drawn":
                    self.current.apply_drawn(data)
        except queue.Empty:
            pass
        if changed:
            self.run_view.refresh()
        self.root.after(200, self._drain)

    def _new_run(self, data: dict) -> None:
        path = data.get("path")
        if path and not data.get("fresh") and Path(path).exists():
            self.current = receipts.load(Path(path))      # a continued run: everything so far
        else:
            self.current = Receipt(None, keep_full=True)
        self.current.run_id = self.current.run_id or data.get("run")
        self.current.path = None                          # never written from here
        self.current.source = Path(path) if path else None
        self.run_view.show(self.current)
        self.refresh_history()

    # ------------------------------------------------------------ history
    def _history_tab(self) -> None:
        tab = ttk.Frame(self.tabs)
        self.tabs.add(tab, text="  History  ")
        panes = ttk.PanedWindow(tab, orient="horizontal")
        panes.pack(fill="both", expand=True)

        left = ttk.Frame(panes, padding=(10, 10, 0, 10))
        buttons = ttk.Frame(left)
        buttons.pack(fill="x", pady=(0, 8))
        ttk.Button(buttons, text="Refresh", command=self.refresh_history).pack(side="left")
        ttk.Button(buttons, text="Open receipts folder",
                   command=lambda: open_path(self.receipts_dir)).pack(side="left", padx=6)
        cols = (("started", "Started", 118), ("seed", "Seed", 84), ("outcome", "Outcome", 64),
                ("entries", "Entries", 56), ("cost", "Cost", 56))
        self.history = ttk.Treeview(left, columns=[c[0] for c in cols], show="headings", selectmode="browse")
        for key, label, width in cols:
            self.history.heading(key, text=label)
            self.history.column(key, width=width, stretch=False,
                                anchor="e" if key in ("entries", "cost") else "w")
        self.history.tag_configure("death", foreground=STATUS_TAGS["error"])
        self.history.tag_configure("victory", foreground=STATUS_TAGS["applied"])
        bar = ttk.Scrollbar(left, orient="vertical", command=self.history.yview)
        self.history.configure(yscrollcommand=bar.set)
        self.history.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.history.bind("<<TreeviewSelect>>", lambda _e: self._open_history())
        panes.add(left, weight=1)

        self.history_view = RunView(panes, "Select a run.", spoilers=bool(self.cfg.get("spoiler_mode")),
                                    pin_decks=True)
        panes.add(self.history_view, weight=3)
        # The run view asks for more width than there is: without this the list gets squeezed.
        panes.bind("<Map>", lambda _e: panes.sashpos(0, 400), add="+")

    def refresh_history(self) -> None:
        """List every receipt file, newest first. Each is read once per change (they're small)."""
        selected = self.history.selection()
        self.history.delete(*self.history.get_children())
        self.history_rows.clear()
        try:
            files = sorted(self.receipts_dir.glob("*.jsonl"), reverse=True)
        except OSError:
            files = []
        for path in files:
            info = self._history_info(path)
            iid = str(path)
            self.history_rows[iid] = path
            self.history.insert("", "end", iid=iid, tags=(info["outcome"],),
                                values=(info["started"], info["seed"], info["outcome"] or "-",
                                        info["entries"], f'${info["cost"]:.2f}'))
        if selected and self.history.exists(selected[0]):
            self.history.selection_set(selected[0])

    _info_cache: dict[str, tuple[tuple, dict]] = {}

    def _history_info(self, path: Path) -> dict:
        try:
            st = path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        cached = self._info_cache.get(str(path))
        if cached and cached[0] == stamp:
            return cached[1]
        info = receipts.file_info(path)
        r = receipts.load(path)
        summary = r.run_summary() or {}
        info.update(outcome=summary.get("outcome") or "", entries=r.seq, cost=r.totals()["cost"],
                    seed=r.run_id or info["seed"])
        self._info_cache[str(path)] = (stamp, info)
        return info

    def _open_history(self) -> None:
        sel = self.history.selection()
        if sel and sel[0] in self.history_rows:
            self.history_view.show(receipts.load(self.history_rows[sel[0]]))

    # ------------------------------------------------------------ settings
    def _settings_tab(self) -> None:
        # Taller than the window can be: the page scrolls.
        outer = ttk.Frame(self.tabs)
        self.settings = outer
        self.tabs.add(outer, text="  Settings  ")
        canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0, bg=self.bg)
        bar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        tab = ttk.Frame(canvas, padding=(20, 16))
        page = canvas.create_window(0, 0, window=tab, anchor="nw")
        tab.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(page, width=e.width))

        def wheel(e) -> None:
            if str(e.widget).startswith(str(outer)) and canvas.yview() != (0.0, 1.0):
                canvas.yview_scroll(int(-e.delta / 120), "units")
        outer.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", wheel))
        outer.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        ttk.Label(tab, text="AI Settings", style="Header.TLabel").pack(anchor="w", pady=(0, 8))

        box = ttk.LabelFrame(tab, text=" Models ", padding=(14, 10))
        box.pack(fill="x")
        ttk.Label(box, text="Set the models the DM will use. Narrative plans the story, DM implements it. "
                            "A re-ask gate saves DM calls where possible.",
                  foreground=MUTED, justify="left").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))
        self.model_lines: dict[str, ttk.Label] = {}
        for i, role in enumerate(model_catalog.ROLES, start=1):
            ttk.Label(box, text=ROLE_NAMES[role]).grid(row=i, column=0, sticky="w", pady=2)
            self.model_lines[role] = ttk.Label(box, text="")
            self.model_lines[role].grid(row=i, column=1, sticky="w", padx=12)
            ttk.Button(box, text="Change...", command=lambda r=role: self.pick_model(r)).grid(row=i, column=2, pady=2)
            ttk.Button(box, text="Default", command=lambda r=role: self._set_pick(r, "")).grid(
                row=i, column=3, padx=(6, 0), pady=2)
        box.columnconfigure(1, weight=1)
        status = ttk.Frame(box)
        status.grid(row=len(model_catalog.ROLES) + 1, column=0, columnspan=4, sticky="we", pady=(6, 0))
        self.models_status = ttk.Label(status, text="", foreground=MUTED)
        self.models_status.pack(side="left")
        self.models_refresh = ttk.Button(status, text="Refresh list", command=self.refresh_models)
        self.models_refresh.pack(side="left", padx=10)
        box = ttk.LabelFrame(tab, text=" OpenRouter ", padding=(14, 10))
        box.pack(fill="x", pady=(14, 0))
        ttk.Label(box, text="API key").grid(row=0, column=0, sticky="w")
        self.key_var = tk.StringVar(value=self.cfg.get("openrouter_api_key", ""))
        self.key_entry = ttk.Entry(box, textvariable=self.key_var, show="\u2022", width=70)
        self.key_entry.grid(row=0, column=1, sticky="we", padx=8)
        self.show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="Show", variable=self.show_key,
                        command=lambda: self.key_entry.configure(show="" if self.show_key.get() else "\u2022")
                        ).grid(row=0, column=2)
        link = ttk.Label(box, text=f"Get a key at {KEYS_URL}", style="Link.TLabel", foreground=LINK, cursor="hand2")
        link.grid(row=1, column=1, sticky="w", padx=8, pady=(6, 0))
        link.bind("<Button-1>", lambda _e: webbrowser.open(KEYS_URL))
        ttk.Label(box, text="Pays for the models below, a few cents a run with the defaults.",
                  foreground=MUTED).grid(row=2, column=1, sticky="w", padx=8, pady=(2, 0))
        box.columnconfigure(1, weight=1)

        box = ttk.LabelFrame(tab, text=" Google AI Studio ", padding=(14, 10))
        box.pack(fill="x", pady=(14, 0))
        ttk.Label(box, text="API key").grid(row=0, column=0, sticky="w")
        self.google_key_entry = ttk.Entry(box, textvariable=self.google_key_var, show="\u2022", width=70)
        self.google_key_entry.grid(row=0, column=1, sticky="we", padx=8)
        self.show_google_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="Show", variable=self.show_google_key,
                        command=lambda: self.google_key_entry.configure(
                            show="" if self.show_google_key.get() else "\u2022")).grid(row=0, column=2)
        link = ttk.Label(box, text=f"Get a key at {GOOGLE_KEYS_URL}", style="Link.TLabel", foreground=LINK,
                         cursor="hand2")
        link.grid(row=1, column=1, sticky="w", padx=8, pady=(6, 0))
        link.bind("<Button-1>", lambda _e: webbrowser.open(GOOGLE_KEYS_URL))
        ttk.Label(box, text="Runs the Gemini models in the pickers below on your own key, billed by Google. "
                            "The list of models comes from Google, so it needs the key.",
                  foreground=MUTED).grid(row=2, column=1, sticky="w", padx=8, pady=(2, 0))
        box.columnconfigure(1, weight=1)

        box = ttk.LabelFrame(tab, text=" Claude Code ", padding=(14, 10))
        box.pack(fill="x", pady=(14, 0))
        self.cc_var = tk.BooleanVar(value=bool(self.cfg.get("use_claude_code")))
        ttk.Checkbutton(box, text="Enable Claude Code", variable=self.cc_var,
                        style="Switch.TCheckbutton", command=self._claude_status).pack(anchor="w")
        ttk.Label(box, text="Enables routing through your own signed-in Claude Code, billed to your Claude plan.",
                  foreground=MUTED, justify="left").pack(anchor="w", pady=(4, 0))
        self.cc_status = ttk.Label(box, text="")
        self.cc_status.pack(anchor="w", pady=(6, 0))

        self._claude_status()

        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(16, 0))
        ttk.Button(row, text="Save and apply", style="Accent.TButton", command=self.save_settings).pack(side="left")
        self.settings_msg = ttk.Label(row, text="", wraplength=800)
        self.settings_msg.pack(side="left", padx=12)

        box = ttk.LabelFrame(tab, text=" Display Settings ", padding=(14, 10))
        box.pack(fill="x", pady=(20, 0))
        self.spoiler_var = tk.BooleanVar(value=bool(self.cfg.get("spoiler_mode")))
        ttk.Checkbutton(box, text="Spoiler mode", variable=self.spoiler_var, style="Switch.TCheckbutton",
                        command=self._set_spoilers).pack(anchor="w")
        ttk.Label(box, text="On: Current run and History show everything the DM decided, why, and its plans.\n"
                            "Off: they show only what the game has used, as it's used. Applies straight away.",
                  foreground=MUTED, justify="left").pack(anchor="w", pady=(4, 0))

        ttk.Label(tab, text="File Settings", style="Header.TLabel").pack(anchor="w", pady=(26, 6))
        files = ttk.Frame(tab)
        files.pack(fill="x")
        log_file = next((p for p in self.logs if p.exists()), self.logs[0] if self.logs else None)
        rows = [("Game", self.isaac_dirs[0] if self.isaac_dirs else None),
                ("Mod", self.isaac_dirs[0] / "mods" / modinstall.MOD_DIR if self.isaac_dirs else None),
                ("Game's log.txt", log_file),
                ("Receipts", self.receipts_dir),
                ("Settings", appdata.config_path()),
                ("This app's log", appdata.data_dir() / "backend.log")]
        for i, (label, path) in enumerate(rows):
            ttk.Label(files, text=label, foreground=MUTED).grid(row=i, column=0, sticky="w", pady=2)
            ttk.Label(files, text=str(path) if path else "not found").grid(row=i, column=1, sticky="w", padx=12)
            if path:
                ttk.Button(files, text="Open", command=lambda p=path: open_path(p)).grid(row=i, column=2, pady=2)

    def _claude_status(self) -> None:
        self._show_models()                 # Claude Code's models come and go with the switch
        try:
            path = find_claude()
        except SystemExit:
            self.cc_status.configure(text="Claude Code not found: install it from claude.com/claude-code and sign in.",
                                     foreground=BAD if self.cc_var.get() else MUTED)
            return
        self.cc_status.configure(text=f"Found: {path}", foreground=GOOD if self.cc_var.get() else MUTED)

    # ------------------------------------------------------------ the model pickers
    def _show_models(self) -> None:
        """Each role's pick (unsaved until Save and apply), and where the model list came from."""
        cc = self.cc_var.get()
        for role, line in self.model_lines.items():
            pick = self.model_picks[role]
            value = model_catalog.effective_value(pick, role, cc)
            text = model_catalog.describe(self.catalog, value)
            if value != pick:
                text += "  (default)"
            if pick.startswith(model_catalog.CLAUDE_CODE) and not cc:
                text += "  - your Claude Code pick comes back when Claude Code is on"
            line.configure(text=text)
        n = len(self.catalog["text"]) + len(self.catalog["decisions"])
        g = len(self.catalog["google"])
        google_error = (self.catalog_raw or {}).get("google_error")
        when = time.strftime("%d %b %H:%M", time.localtime(self.catalog_raw["fetched"])) \
            if self.catalog_raw and self.catalog_raw.get("fetched") else None
        if self.fetching:
            text, colour = "Fetching the model list from OpenRouter...", MUTED
        elif self.catalog_error:
            text = f"Couldn't fetch the model list ({self.catalog_error})"
            text += f": showing the one from {when}." if when else "."
            colour = BAD
        else:
            text, colour = (f"{n} models found" + (f", {g} from Google AI Studio" if g else "")
                            + f", fetched {when}." if when else "No model list yet."), MUTED
            if google_error:
                text, colour = f"{text}  Google AI Studio's list failed ({google_error}).", BAD
            elif not g and not self.google_key_var.get().strip():
                text += "  Add a Google AI Studio key to list Gemini models."
        self.models_status.configure(text=text, foreground=colour)
        self.models_refresh.configure(state="disabled" if self.fetching else "normal")

    def _set_pick(self, role: str, value: str) -> None:
        self.model_picks[role] = value
        self._show_models()
        self.settings_msg.configure(text="Press Save and apply to use it.", foreground=MUTED)

    def refresh_models(self) -> None:
        """Fetch OpenRouter's model list on a thread; the window carries on meanwhile."""
        if self.fetching:
            return
        self.fetching, self.catalog_error = True, None
        result: queue.Queue = queue.Queue()
        key = self.google_key_var.get().strip()

        def work() -> None:
            try:
                result.put((model_catalog.fetch(google_key=key), None))
            except Exception as e:                # noqa: BLE001 - shown in Settings, the cached list stays
                result.put((None, f"{type(e).__name__}: {e}"))

        threading.Thread(target=work, name="models", daemon=True).start()
        self._show_models()
        self.root.after(200, self._models_fetched, result)

    def _models_fetched(self, result: queue.Queue) -> None:
        try:
            raw, error = result.get_nowait()
        except queue.Empty:
            self.root.after(200, self._models_fetched, result)
            return
        self.fetching, self.catalog_error = False, error
        if raw:
            self.catalog_raw, self.catalog = raw, model_catalog.parse(raw)
            try:
                model_catalog.save_cache(raw)
            except OSError as e:
                log.warning("couldn't save the model list: %s", e)
        else:
            log.warning("couldn't fetch the model list: %s", error)
        self._show_models()

    def pick_model(self, role: str) -> None:
        cc = self.cc_var.get()
        pinned, rows = model_catalog.candidates(self.catalog, role, cc)
        current = model_catalog.effective_value(self.model_picks[role], role, cc)
        ModelPicker(self.root, role, pinned, rows, current, lambda value: self._set_pick(role, value))

    def _set_spoilers(self) -> None:
        on = self.spoiler_var.get()
        self.run_view.set_spoilers(on)
        self.history_view.set_spoilers(on)
        try:
            appdata.save_config({**self.cfg, "spoiler_mode": on})     # the saved DM settings, not unsaved edits
        except OSError as e:
            self.settings_msg.configure(text=f"Couldn't save the settings: {e}", foreground=BAD)
            return
        self.cfg["spoiler_mode"] = on

    def save_settings(self) -> None:
        cfg = {"openrouter_api_key": self.key_var.get().strip(),
               "google_api_key": self.google_key_var.get().strip(), "use_claude_code": self.cc_var.get(),
               "spoiler_mode": self.spoiler_var.get(),
               **{f"{role}_model": pick for role, pick in self.model_picks.items()}}
        try:
            engine = self._engine(cfg)
        except ValueError as e:
            self.settings_msg.configure(text=str(e), foreground=BAD)
            return
        try:
            appdata.save_config(cfg)
        except OSError as e:
            self.settings_msg.configure(text=f"Couldn't save the settings: {e}", foreground=BAD)
            return
        key_changed = cfg["google_api_key"] != self.cfg.get("google_api_key", "")
        self.cfg = cfg
        if key_changed:
            self.refresh_models()                 # Google's model list needs the key
        if self.dm and self.dm.alive():
            self.dm.apply(engine)
        else:
            self.start_dm()
        self.settings_msg.configure(text=f"Saved. {engine_text(engine)}.", foreground=GOOD)

    # ------------------------------------------------------------ log
    def _log_tab(self) -> None:
        tab = ttk.Frame(self.tabs, padding=(10, 10))
        self.tabs.add(tab, text="  Log  ")
        buttons = ttk.Frame(tab)
        buttons.pack(fill="x", pady=(0, 8))
        ttk.Button(buttons, text="Open log file",
                   command=lambda: open_path(appdata.data_dir() / "backend.log")).pack(side="left")
        ttk.Button(buttons, text="Clear", command=lambda: set_text(self.log_text, "")).pack(side="left", padx=6)
        frame, self.log_text = text_box(tab)
        frame.pack(fill="both", expand=True)
        self.log_text.tag_configure("warn", foreground="#ffb86b")
        self.log_text.tag_configure("error", foreground="#ff8a7a")
        self.root.after(250, self._drain_log)

    def _drain_log(self) -> None:
        lines = []
        try:
            while len(lines) < 500:
                lines.append(LOG_QUEUE.get_nowait())
        except queue.Empty:
            pass
        if lines:
            t = self.log_text
            at_end = t.yview()[1] > 0.999
            t.configure(state="normal")
            for level, text in lines:
                t.insert("end", text + "\n", "error" if level >= logging.ERROR else
                         "warn" if level >= logging.WARNING else ())
            extra = int(t.index("end-1c").split(".")[0]) - LOG_LINES
            if extra > 0:
                t.delete("1.0", f"{extra + 1}.0")
            t.configure(state="disabled")
            if at_end:
                t.see("end")
        self.root.after(250, self._drain_log)

    # ------------------------------------------------------------ closing
    def close(self) -> None:
        if self.dm:
            self.dm.stop()
        self.root.destroy()


# ====================================================================== process plumbing
LOG_QUEUE: queue.Queue = queue.Queue()


class _QueueHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            LOG_QUEUE.put((record.levelno, self.format(record)))
        except Exception:                         # noqa: BLE001
            self.handleError(record)


def setup_logging(verbose: bool) -> None:
    fmt = logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    q = _QueueHandler()
    q.setFormatter(fmt)
    root.addHandler(q)
    try:
        appdata.data_dir().mkdir(parents=True, exist_ok=True)
        f = logging.handlers.RotatingFileHandler(appdata.data_dir() / "backend.log", maxBytes=1_000_000,
                                                 backupCount=3, encoding="utf-8")
        f.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root.addHandler(f)
    except OSError:
        pass
    if sys.stderr is not None and not appdata.frozen():
        console = logging.StreamHandler()          # from source, the terminal still gets it too
        console.setFormatter(fmt)
        root.addHandler(console)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def show_errors(root: tk.Tk) -> None:
    """Nothing dies silently: errors go to the log and, from the window's own thread, to a dialog."""
    def report(exc_type, exc, tb) -> None:
        log.error("unexpected error:\n%s", "".join(traceback.format_exception(exc_type, exc, tb)))
        try:
            messagebox.showerror("LLM Dungeon Master", f"{exc_type.__name__}: {exc}\n\nDetails are in the Log tab.")
        except tk.TclError:
            pass

    sys.excepthook = report
    root.report_callback_exception = report
    threading.excepthook = lambda a: log.error("error in thread %s:\n%s", a.thread.name if a.thread else "?",
                                               "".join(traceback.format_exception(a.exc_type, a.exc_value,
                                                                                  a.exc_traceback)))


_mutex = None


def already_running() -> bool:
    """One backend per PC: two would both answer the game."""
    global _mutex
    if os.name != "nt":
        return False
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _mutex = k32.CreateMutexW(None, False, "Local\\LLMDungeonMasterBackend")
    return ctypes.get_last_error() == 183           # ERROR_ALREADY_EXISTS


def windows_look(root: tk.Tk) -> None:
    if os.name != "nt":
        return
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        on = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), ctypes.sizeof(on))   # dark title bar
    except Exception:                             # noqa: BLE001 - cosmetic
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mock", action="store_true", help="the offline rule-based DM (no key needed)")
    ap.add_argument("--isaac-dir", type=Path, action="append", help="the game's install folder (repeatable)")
    ap.add_argument("--log", type=Path, action="append", help="the game's log.txt (repeatable)")
    ap.add_argument("--receipts", type=Path, default=appdata.receipts_dir(), help="folder for the run receipts")
    ap.add_argument("--no-receipts", action="store_true", help="keep receipts in memory only")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)    # sharp text on scaled displays
        except Exception:                         # noqa: BLE001
            pass
    root = tk.Tk()
    try:
        icon = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "icon.ico"
        root.iconbitmap(default=str(icon))        # default=: every window, pickers included
    except Exception:                             # noqa: BLE001 - cosmetic
        pass
    if already_running():
        root.withdraw()
        messagebox.showinfo("LLM Dungeon Master", "The LLM Dungeon Master is already running.")
        return
    setup_logging(args.verbose)
    show_errors(root)
    try:
        import sv_ttk
        sv_ttk.set_theme("dark")
    except Exception as e:                        # noqa: BLE001 - plain ttk still works
        log.warning("no dark theme: %s", e)
    windows_look(root)
    App(root, args)
    root.mainloop()


if __name__ == "__main__":
    main()
