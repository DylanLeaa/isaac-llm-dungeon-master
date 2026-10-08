"""The engine's receipt: every decision, planner call and fallback, with the LLM call behind it.

Stored per run as JSON lines in receipts/<started>_run-<seed>.jsonl:
  {"type": "prompt", "hash": ..., "text": ...}       a system prompt, written once per file
  {"type": "entry", "seq": n, ...}                   one engine action (request, LLM call, result)
  {"type": "update", "ref": n, "status": ...}        what later happened to it (applied / replaced)
A continued run picks up its latest file again. The game gets a compact copy of each entry and
shows it under Mod Config Menu > DM Settings > Receipt.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any

log = logging.getLogger("llmdm")

GAME_KEEP = 40          # entries the game keeps (the menu browses these)
PLAN_CHARS = 600        # how much of a planner's text the game gets


class Receipt:
    def __init__(self, directory: Path | None = None, keep: int = GAME_KEEP) -> None:
        self.dir = directory
        self.keep = keep
        self.start_run(None, fresh=True)

    # ------------------------------------------------------------------ runs
    def start_run(self, run_id: Any, fresh: bool) -> None:
        """A new run starts a new file; a continued one (fresh=False) resumes its latest file."""
        self.run_id = run_id
        self.seq = 0
        self.recent: OrderedDict[int, dict] = OrderedDict()   # seq -> compact entry, newest last
        self.status: dict[int, str] = {}                      # every entry's current status
        self.decisions: dict[int, tuple[str, dict]] = {}      # seq -> (kind, clamped decision)
        self.usage = Counter()
        self.prompts: set[str] = set()                        # system prompt hashes already in the file
        self.path: Path | None = None
        if self.dir is None or run_id is None:
            return
        tag = re.sub(r"[^A-Za-z0-9]+", "", str(run_id)) or "run"
        old = list(self.dir.glob(f"*_run-{tag}.jsonl")) if not fresh and self.dir.exists() else []
        if old:
            self.path = max(old, key=lambda p: p.stat().st_mtime)
            self._replay(self.path)
        else:
            stamp, n = time.strftime("%Y%m%d-%H%M%S"), 1
            self.path = self.dir / f"{stamp}_run-{tag}.jsonl"
            while self.path.exists():                     # same seed restarted within the second
                n += 1
                self.path = self.dir / f"{stamp}-{n}_run-{tag}.jsonl"

    def _replay(self, path: Path) -> None:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as e:
            log.warning("receipt: cannot read %s (%s), starting fresh", path, e)
            return
        for line in lines:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") == "prompt":
                self.prompts.add(rec.get("hash"))
            elif rec.get("type") == "entry":
                self._ingest(rec)
            elif rec.get("type") == "update":
                self._apply_update(rec.get("ref"), rec.get("status"))
        if self.seq:
            log.info("receipt: resumed %s (%d entries)", path.name, self.seq)

    # --------------------------------------------------------------- writing
    def add(self, what: str, status: str, **fields) -> dict:
        """Record one engine action. Returns the compact entry the game is sent."""
        entry = {"type": "entry", "seq": self.seq + 1, "ts": round(time.time(), 3),
                 "run": self.run_id, "what": what, "status": status, **fields}
        llm = entry.get("llm")
        if llm and isinstance(llm.get("system"), list):
            llm["system"] = [self._prompt(text) for text in llm["system"]]
        self._write(entry)
        return self._ingest(entry)

    def update(self, seq: int | None, status: str) -> dict | None:
        """A later outcome for entry `seq` (applied, replaced). Returns its new compact form."""
        if seq is None or seq not in self.status:
            return None
        self._write({"type": "update", "ref": seq, "status": status, "ts": round(time.time(), 3)})
        return self._apply_update(seq, status)

    def _prompt(self, text: str) -> str:
        h = hashlib.sha1(text.encode()).hexdigest()[:12]
        if h not in self.prompts:
            self.prompts.add(h)
            self._write({"type": "prompt", "hash": h, "text": text})
        return h

    def _write(self, rec: dict) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
        except OSError as e:                      # a receipt must never break the DM
            log.warning("receipt: cannot write %s (%s) -> in-memory only", self.path, e)
            self.path = None

    def _ingest(self, entry: dict) -> dict:
        seq = int(entry["seq"])
        self.seq = max(self.seq, seq)
        self.status[seq] = entry["status"]
        if entry.get("decision"):
            self.decisions[seq] = (entry["what"], entry["decision"])
        llm = entry.get("llm")
        if llm:
            self.usage["calls"] += 1
            self.usage.update({k: (llm.get("usage") or {}).get(k, 0) for k in ("in", "cached", "out", "cost")})
        c = compact(entry)
        self.recent[seq] = c
        while len(self.recent) > self.keep:
            self.recent.popitem(last=False)
        return c

    def _apply_update(self, seq, status) -> dict | None:
        if seq not in self.status or not status:
            return None
        self.status[seq] = status
        c = self.recent.get(seq)
        if c is not None:
            c["status"] = status
        return c

    # --------------------------------------------------------------- reading
    def totals(self) -> dict:
        u, s = self.usage, Counter(self.status.values())
        return {"entries": self.seq, "calls": u["calls"], "in": u["in"], "cached": u["cached"], "out": u["out"],
                "cost": round(u["cost"], 6), **{k: s[k] for k in
                ("applied", "replaced", "fallback", "timeout", "error", "superseded")}}

    def applied(self) -> list[tuple[str, dict]]:
        """Every decision the game actually used this run, oldest first, as (kind, decision)."""
        return [self.decisions[s] for s in sorted(self.decisions) if self.status.get(s) == "applied"]

    def snapshot(self) -> dict:
        """Everything the game shows: recent entries (oldest first), run totals, where the file is."""
        return {"entries": list(self.recent.values()), "totals": self.totals(),
                "file": self.path.name if self.path else None}


# ------------------------------------------------------------------ for the game
def compact(entry: dict) -> dict:
    """An entry as the in-game menu shows it: short fields, no prompts or raw payloads."""
    llm = entry.get("llm") or {}
    u = llm.get("usage") or {}
    c = {"seq": entry["seq"], "time": time.strftime("%H:%M:%S", time.localtime(entry.get("ts", 0))),
         "what": entry["what"], "status": entry["status"]}
    for k in ("key", "ms", "intent", "taunt", "scheme", "result", "error"):
        if entry.get(k) not in (None, "", []):
            c[k] = entry[k]
    if llm.get("model"):
        c["model"] = str(llm["model"]).split("/")[-1]
    if llm.get("finish"):
        c["finish"] = llm["finish"]
    if u:
        c["tok"] = [int(u.get("in", 0)), int(u.get("cached", 0)), int(u.get("out", 0))]
        c["cost"] = round(float(u.get("cost", 0.0)), 6)
    d = entry.get("decision") or {}
    for k in ("intent", "taunt", "scheme"):
        if d.get(k) and k not in c:
            c[k] = d[k]
    if d and "result" not in c:
        c["result"] = summarize(entry["what"], d, (entry.get("request") or {}).get("ctx") or {})
    if d.get("guardrails"):
        c["guardrails"] = d["guardrails"]
    return c


def summarize(kind: str, d: dict, ctx: dict) -> str:
    """One readable line for what a clamped decision actually does."""
    if kind == "room_clear":
        return ", ".join(f'{r["count"]}x {r["reward"]}' for r in d.get("rewards", [])) or "nothing"
    if kind == "room_layout":
        bits = [f'+{e["count"]} {e["key"]}' for e in d.get("extra_enemies", [])]
        if d.get("champion_chance"):
            bits.append(f'champions {round(100 * d["champion_chance"])}% {d.get("champion_color", "")}'.strip())
        if d.get("tinted_rocks"):
            bits.append(f'{d["tinted_rocks"]} tinted rock(s)' + (" hidden" if d.get("hide_tinted_rocks") else ""))
        if d.get("layout_pickups") not in (None, "keep"):
            bits.append("pickups " + str(d["layout_pickups"]))
        if d.get("bonus_pickups"):
            bits.append("bait " + ",".join(d["bonus_pickups"]))
        return "; ".join(bits) or "untouched"
    if kind == "item_choice":
        names = {int(c["id"]): c.get("name", str(c["id"])) for c in ctx.get("candidates", []) if "id" in c}
        return "ranked: " + " > ".join(str(names.get(i, i)) for i in d.get("order", [])[:4])
    if kind == "floor_plan":
        curses = ",".join(d.get("add_curses", [])) or "none"
        out = f'curses {curses}; treasure {d.get("treasure_distance", "any")}'
        return out + ("; strip vanilla curses" if d.get("strip_vanilla_curses") else "")
    if kind == "consumables":
        parts = ["cards " + (",".join(c["key"] for c in d.get("cards", [])) or "-")]
        if d.get("trinkets"):
            parts.append(f'{len(d["trinkets"])} trinket(s)')
        if d.get("pills"):
            parts.append("pills " + ",".join(f'{p["color"]}={p["effect"]}' for p in d["pills"]))
        return "; ".join(parts)
    return ""


def plan_excerpt(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= PLAN_CHARS else text[:PLAN_CHARS - 3] + "..."
