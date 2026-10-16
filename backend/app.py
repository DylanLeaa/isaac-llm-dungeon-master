"""LLM Dungeon Master as a desktop app: what the exe runs.

The same backend as server.py, with a window instead of a console:
  Current run   what the DM is doing this run: its bill, its work, every decision, its plans
  History       every past run's receipt
  Settings      OpenRouter key or Claude Code, applied to the running DM straight away
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
from tkinter import messagebox, ttk

import appdata
import bridge
import epilogue
import receipt as receipts
from claude_code import find_claude
from receipt import Receipt
from server import Coordinator, Engine, build_dm

log = logging.getLogger("llmdm")

DEFAULT_CRUELTY = 6           # until the game's DM Settings menu says otherwise
# The mod pings every 2 s, but only while the game renders: a floor's stage transition (a chapter's
# nightmare most of all) goes quiet for a while. This long without a message and the game is gone.
GAME_TIMEOUT_S = 15.0
LOG_LINES = 3000
KEYS_URL = "https://openrouter.ai/keys"
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
    """A run's receipt: summary on top, every entry below, the selected one in full."""

    COLUMNS = (("time", "Time", 65), ("what", "What", 90), ("key", "Key", 105), ("status", "Status", 75),
               ("reason", "Asked because", 150), ("intent", "Intent", 230), ("result", "Result", 220),
               ("effect", "Effect", 240), ("ms", "ms", 55), ("cost", "Cost", 65))
    WIDE = ("intent", "result", "effect")

    def __init__(self, master, empty: str) -> None:
        super().__init__(master, padding=(12, 10))
        self.empty = empty
        self.r: Receipt | None = None

        head = ttk.Frame(self)
        head.pack(fill="x")
        self.title = ttk.Label(head, text=empty, style="Title.TLabel")
        self.title.pack(anchor="w")
        self.subtitle = ttk.Label(head, text="", foreground=MUTED)
        self.subtitle.pack(anchor="w", pady=(0, 8))

        cards = ttk.Frame(self)
        cards.pack(fill="x")
        self.cards = {k: Card(cards, c) for k, c in (
            ("bill", "LLM bill"), ("tokens", "tokens in/cached/out"), ("applied", "decisions used"),
            ("replaced", "paid for, not used"), ("fallback", "DM gave up"), ("problems", "timeouts / errors"))}
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

        panes = ttk.PanedWindow(self, orient="vertical")
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
        panes.add(table, weight=3)

        tabs = ttk.Notebook(panes)
        entry_frame, self.entry_text = text_box(tabs, height=8, wrap="none")
        plan_frame, self.plan_text = text_box(tabs, height=8)
        tabs.add(entry_frame, text="Selected entry")
        tabs.add(plan_frame, text="The DM's plan")
        panes.add(tabs, weight=2)
        self.show(None)

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
        for seq in sorted(r.full) if r else []:
            self._insert(seq)
        set_text(self.entry_text, "Pick an entry above to see everything behind it." if r else "")
        self.refresh()

    def added(self, seq: int) -> None:
        self._insert(seq)

    def updated(self, seq: int) -> None:
        if self.r and self.tree.exists(str(seq)):
            self.tree.item(str(seq), values=self._row(seq), tags=(self.r.full[seq].get("status"),))
            if self.tree.selection() == (str(seq),):
                self._show_selected()

    def _insert(self, seq: int) -> None:
        if not self.tree.exists(str(seq)):
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
        return (c["time"], c["what"], c.get("key", ""), c["status"], c.get("reason", ""), intent, result, effect,
                c.get("ms", ""), cost)

    def _show_selected(self) -> None:
        sel = self.tree.selection()
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

    def refresh(self) -> None:
        """Redo the summary and the plans from the receipt (cheap: a run is a few hundred entries)."""
        r = self.r
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
        bits = [f"seed {r.run_id}" if r.run_id else None,
                (r.source or r.path).name if (r.source or r.path) else "receipt not saved"]
        self.subtitle.configure(text="  |  ".join(b for b in bits if b))
        self.cards["bill"].set(f'${t["cost"]:.4f}')
        self.cards["tokens"].set(f'{_k(t["in"])} / {_k(t["cached"])} / {_k(t["out"])}')
        self.cards["applied"].set(str(t["applied"]))
        self.cards["bill"].caption.configure(text=f'LLM bill, {t["calls"]} calls')
        self.cards["replaced"].set(str(t["replaced"] + t["superseded"]))
        self.cards["fallback"].set(str(t["fallback"]))
        self.cards["problems"].set(f'{t["timeout"]} / {t["error"]}')

        rows, scheme, taunt = epilogue.dm_side(r.applied(), t, [])
        sections = [("THE DM", rows)]
        if summary:
            sections = [(s["title"], s["rows"]) for s in summary.get("sections", [])]
        on_cards = ("LLM bill", "DM gave up")
        sections = [(name, [row for row in rows if row[0] not in on_cards]) for name, rows in sections]
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
            words.append(f'{who}: {"died" if summary.get("outcome") == "death" else "won"}.')
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


def _k(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    return f"{n / 1000:.1f}k" if n >= 10_000 else str(n)


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
        self.current = Receipt(None, keep_full=True)
        self.dirty = False
        self.history_rows: dict[str, Path] = {}

        root.title("LLM Dungeon Master")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{min(1440, sw - 80)}x{min(940, sh - 110)}+{max(0, (sw - min(1440, sw - 80)) // 2)}+20")
        root.minsize(900, 600)
        self._styles()
        self._header()
        self.tabs = ttk.Notebook(root)
        self.tabs.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.run_view = RunView(self.tabs, "No run yet. Start a run in Isaac with the mod enabled.")
        self.tabs.add(self.run_view, text="  Current run  ")
        self._history_tab()
        self._settings_tab()
        self._log_tab()
        root.protocol("WM_DELETE_WINDOW", self.close)

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
        if problems:
            self.problem.configure(text="  ".join(problems))
            self.problem.pack(fill="x")

    def _set_status(self, colour: str, text: str) -> None:
        self.dot.itemconfigure(self.dot_id, fill=colour)
        if self.status.cget("text") != text:
            self.status.configure(text=text)

    # ------------------------------------------------------------ the DM
    def _engine(self, cfg: dict) -> Engine:
        if self.args.mock:
            return build_dm("mock", "", cruelty=DEFAULT_CRUELTY)
        backend = "claude-code" if cfg.get("use_claude_code") else "openrouter"
        return build_dm(backend, appdata.api_key(cfg), cruelty=DEFAULT_CRUELTY)

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
            self._set_status("#ffb86b", "The DM isn't set up: add an OpenRouter key or turn on Claude Code in Settings")
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
            self._set_status("#8a8a8a", "Waiting for the game: start Isaac with the mod enabled")
            if self.current.full or self.current.run_id:    # no game, no current run
                self.current = Receipt(None, keep_full=True)
                self.run_view.show(None)
        if dm is not None:
            self.dm_label.configure(text="DM: " + dm.engine.label.split(" (")[0])
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
                    if self.current.apply_update(data["seq"], data["status"]) and self.run_view.r:
                        self.run_view.updated(int(data["seq"]))
                elif event == "effect":
                    if self.current.apply_effect(data["seq"], data["effect"]) and self.run_view.r:
                        self.run_view.updated(int(data["seq"]))
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

        self.history_view = RunView(panes, "Pick a run on the left.")
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
        tab = ttk.Frame(self.tabs, padding=(20, 16))
        self.settings = tab
        self.tabs.add(tab, text="  Settings  ")

        ttk.Label(tab, text="Who plays the Dungeon Master", style="Title.TLabel").pack(anchor="w")
        ttk.Label(tab, text="Changes apply to the running DM straight away: a run in progress carries on.",
                  foreground=MUTED).pack(anchor="w", pady=(0, 14))

        box = ttk.LabelFrame(tab, text=" OpenRouter ", padding=(14, 10))
        box.pack(fill="x")
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
        env = " (OPENROUTER_API_KEY is set in your environment: it's used while this is blank)" \
            if os.environ.get("OPENROUTER_API_KEY") else ""
        ttk.Label(box, text="Pays for the DM (Claude Haiku) and its planners, usually a few cents a run." + env,
                  foreground=MUTED).grid(row=2, column=1, sticky="w", padx=8, pady=(2, 0))
        box.columnconfigure(1, weight=1)

        box = ttk.LabelFrame(tab, text=" Claude Code ", padding=(14, 10))
        box.pack(fill="x", pady=(14, 0))
        self.cc_var = tk.BooleanVar(value=bool(self.cfg.get("use_claude_code")))
        ttk.Checkbutton(box, text="Use Claude Code instead of OpenRouter", variable=self.cc_var,
                        style="Switch.TCheckbutton", command=self._claude_status).pack(anchor="w")
        ttk.Label(box, text="Every DM call runs through your own signed-in Claude Code (claude -p), billed to "
                            "your Claude plan. Slower than OpenRouter.\nThe OpenRouter key is then optional: "
                            "if set, it only runs the small model that skips needless re-asks.",
                  foreground=MUTED, justify="left").pack(anchor="w", pady=(4, 0))
        self.cc_status = ttk.Label(box, text="")
        self.cc_status.pack(anchor="w", pady=(6, 0))
        self._claude_status()

        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(16, 0))
        ttk.Button(row, text="Save and apply", style="Accent.TButton", command=self.save_settings).pack(side="left")
        self.settings_msg = ttk.Label(row, text="", wraplength=800)
        self.settings_msg.pack(side="left", padx=12)

        ttk.Label(tab, text="Files", style="Header.TLabel").pack(anchor="w", pady=(26, 6))
        files = ttk.Frame(tab)
        files.pack(fill="x")
        log_file = next((p for p in self.logs if p.exists()), self.logs[0] if self.logs else None)
        rows = [("Game", self.isaac_dirs[0] if self.isaac_dirs else None),
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
        try:
            path = find_claude()
        except SystemExit:
            self.cc_status.configure(text="Claude Code not found: install it from claude.com/claude-code and sign in.",
                                     foreground=BAD if self.cc_var.get() else MUTED)
            return
        self.cc_status.configure(text=f"Found: {path}", foreground=GOOD if self.cc_var.get() else MUTED)

    def save_settings(self) -> None:
        cfg = {"openrouter_api_key": self.key_var.get().strip(), "use_claude_code": self.cc_var.get()}
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
        self.cfg = cfg
        if self.dm and self.dm.alive():
            self.dm.apply(engine)
        else:
            self.start_dm()
        self.settings_msg.configure(text="Saved. The DM is now " + engine.label.split(" (")[0] + ".",
                                    foreground=GOOD)

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
