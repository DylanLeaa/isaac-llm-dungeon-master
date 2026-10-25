"""The link to the mod, inside Isaac's mod sandbox: the game's log file one way, the mod's save file the other.

Mod -> backend: the mod writes each message with Isaac.DebugString, so it lands in log.txt as
    [INFO] - Lua Debug: [LLMDM:<n>:<i>/<k>]<chunk of JSON>
  split over k lines when it's long. We tail every log.txt the game might be writing and join them up.
Backend -> mod: the mod reads its own save file (mod:LoadData) every few frames. We write our messages
  into it under "dm", numbered; the mod acks the highest number it has handled ("ack" and "boot" on its
  next message) and we drop those. Everything else in the file is the mod's own data (settings, pill
  locks, run stats) and is written back as we last read it. The mod numbers its saves ("rev") and saves
  again if one of our writes carried an older copy.

The mod can't tell us where its save file is, so it saves its session id in it ("link") before saying
hello with that id, and we look for the file that holds it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger("llmdm")

# Must match PROTO in mod/llm_dungeon_master/dm/bridge.lua. Bump both when a message changes shape.
# 2: decks (`drawn` per draw, `abandon`, the backend's `stale` push), cards/trinkets/pills/drops kinds.
PROTO = 3                  # 3: pushed decks (agent mode)

LINE_RE = re.compile(rb"\[LLMDM:(\d+):(\d+)/(\d+)\](.*)")
MAX_OUTBOX = 300            # unacked messages kept for the mod; the oldest go first if the game is gone
GAME_FOLDERS = ("Binding of Isaac Repentance+", "Binding of Isaac Repentance", "Binding of Isaac Afterbirth+",
                "Binding of Isaac Afterbirth")
ISAAC_APP = "The Binding of Isaac Rebirth"


# ---------------------------------------------------------------- where the game is
def _unique(paths) -> list[Path]:
    seen, out = set(), []
    for p in paths:
        k = os.path.normcase(os.path.abspath(p))
        if k not in seen:
            seen.add(k)
            out.append(Path(p))
    return out


def steam_libraries() -> list[Path]:
    roots = []
    if sys.platform == "win32":
        import winreg
        for hive, key, value in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                                 (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    roots.append(Path(winreg.QueryValueEx(k, value)[0]))
            except OSError:
                pass
        roots.append(Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam")
    else:
        home = Path.home()
        roots += [home / ".steam" / "steam", home / ".local" / "share" / "Steam",
                  home / "Library" / "Application Support" / "Steam"]
    libs = []
    for root in roots:
        if not root.is_dir():
            continue
        libs.append(root)
        try:
            text = (root / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        libs += [Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', text)]
    return _unique(libs)


def isaac_installs() -> list[Path]:
    return [d for lib in steam_libraries() if (d := lib / "steamapps" / "common" / ISAAC_APP).is_dir()]


def _documents() -> list[Path]:
    dirs = []
    if sys.platform == "win32":
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0:   # CSIDL_PERSONAL (follows OneDrive)
            dirs.append(Path(buf.value))
    dirs.append(Path.home() / "Documents")
    # Proton keeps the game's Documents inside its prefix.
    dirs += [lib / "steamapps" / "compatdata" / "250900" / "pfx" / "drive_c" / "users" / "steamuser" / "Documents"
             for lib in steam_libraries()]
    return _unique(d for d in dirs if d.is_dir())


def default_logs() -> list[Path]:
    """Every log.txt the game could write (Repentance+, Repentance, ...). They needn't exist yet."""
    return [d / "My Games" / name / "log.txt" for d in _documents() for name in GAME_FOLDERS]


def save_files(isaac_dirs: list[Path]) -> list[Path]:
    """Mods' save files: <install>/data/<mod>/save<slot>.dat."""
    return [p for d in isaac_dirs for p in sorted((d / "data").glob("*/save*.dat"))]


# ---------------------------------------------------------------- mod -> backend
class LogTail:
    """New complete lines of a log file. Starts at its end: what's there already is from before."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.pos: int | None = None
        self.buf = b""
        self.parts: list[bytes] = []
        self.n: int | None = None            # the message whose chunks are being collected

    def lines(self) -> list[bytes]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if self.pos is None:
            self.pos = size
        if size < self.pos:                  # the game started again and cleared its log
            self.pos, self.buf, self.n = 0, b"", None
        if size == self.pos:
            return []
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                chunk = f.read(size - self.pos)
        except OSError:
            return []
        self.pos += len(chunk)
        *lines, self.buf = (self.buf + chunk).split(b"\n")
        return lines

    def messages(self) -> list[dict]:
        out = []
        for line in self.lines():
            if b"[LLMDM:" not in line:
                continue
            m = LINE_RE.search(line.rstrip(b"\r"))
            if not m:
                continue
            n, i, k, chunk = int(m[1]), int(m[2]), int(m[3]), m[4]
            if i == 1:
                self.n, self.parts = n, []
            if n != self.n or i != len(self.parts) + 1:
                self.n = None                    # a chunk went missing: drop the message
                continue
            self.parts.append(chunk)
            if i < k:
                continue
            self.n = None
            try:
                msg = json.loads(b"".join(self.parts))
            except ValueError:
                log.warning("bad json from game: %r", b"".join(self.parts)[:200])
                continue
            if isinstance(msg, dict):
                out.append(msg)
        return out


# ---------------------------------------------------------------- backend -> mod
def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_bytes() or b"{}")
    except (OSError, ValueError):
        return None                              # gone, or caught mid-write: try again later
    return data if isinstance(data, dict) else {}


def _stamp(path: Path):
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


class SaveLink:
    """Our messages to the mod, written into its save file."""

    def __init__(self, isaac_dirs: list[Path]) -> None:
        self.isaac_dirs = isaac_dirs
        self.path: Path | None = None
        # New every start: tells the mod our numbering starts over.
        self.boot = f"{os.getpid():x}-{int(time.time() * 1000):x}"
        self.seq = 0
        self.msgs: list[tuple[int, dict]] = []
        self.fields: dict = {}                   # the mod's own data, as last read
        self.known = None                        # (mtime, size) of the file as we last wrote or read it
        self.dirty = False

    def find(self, session: str) -> bool:
        """Point at the save file holding this session id (the mod saved it there before its hello)."""
        if self.path and self._links(self.path, session):
            return True
        for p in save_files(self.isaac_dirs):
            if self._links(p, session):
                self._switch(p)
                return True
        return False

    @staticmethod
    def _links(path: Path, session: str) -> bool:
        try:
            raw = path.read_bytes()
        except OSError:
            return False
        if session.encode() not in raw:
            return False
        data = _read_json(path)
        return bool(data) and data.get("link") == session

    def _switch(self, path: Path) -> None:
        old = self.path
        if old:
            data = _read_json(old)                # don't leave our messages in a file the mod stopped reading
            if data and "dm" in data:
                data.pop("dm")
                self._write(old, data)
        log.info("game save file: %s", path)
        self.path, self.fields, self.known, self.dirty = path, {}, None, True

    def send(self, msg: dict) -> None:
        self.seq += 1
        self.msgs.append((self.seq, msg))
        del self.msgs[:-MAX_OUTBOX]
        self.dirty = True

    def ack(self, boot, seq) -> None:
        """The mod has handled everything up to seq. Dropped on our next write; no write just for this."""
        if boot == self.boot and isinstance(seq, int):
            self.msgs = [m for m in self.msgs if m[0] > seq]

    def flush(self) -> None:
        if not self.path:
            return
        stamp = _stamp(self.path)
        if stamp != self.known:                  # the mod saved since we last looked: take its data
            data = _read_json(self.path)
            if data is None:
                return
            data.pop("dm", None)
            self.fields, self.known = data, stamp
            self.dirty = self.dirty or bool(self.msgs)   # its save dropped our messages: put them back
        if not self.dirty:
            return
        dm = {"proto": PROTO, "boot": self.boot, "msgs": [list(m) for m in self.msgs]}
        stamp = self._write(self.path, {**self.fields, "dm": dm})
        if stamp:
            self.known, self.dirty = stamp, False

    @staticmethod
    def _write(path: Path, data: dict):
        """Write the whole file at once (the mod must never read half of it). The stamp it will have, or None."""
        tmp = path.with_name(path.name + ".llmdm")
        try:
            tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            stamp = _stamp(tmp)
            os.replace(tmp, path)
        except OSError:
            return None                          # the game has it open this instant: next tick
        return stamp


# ---------------------------------------------------------------- loop
Handler = Callable[[dict, Callable[[dict], Awaitable[None]]], Awaitable[None]]


async def serve(handle: Handler, logs: list[Path], isaac_dirs: list[Path], poll_s: float = 0.015) -> None:
    link = SaveLink(isaac_dirs)
    tails = [LogTail(p) for p in logs]
    lost_at = 0.0

    async def send(msg: dict) -> None:
        link.send(msg)

    log.info("reading the game's log: %s", ", ".join(str(p) for p in logs if p.exists()) or "(not written yet)")
    while True:
        for tail in tails:
            for msg in tail.messages():
                if "ack" in msg:
                    link.ack(msg.pop("boot", None), msg.pop("ack"))
                t = msg.get("t")
                if t == "ack":
                    continue
                if t == "hello" and not link.find(str(msg.get("session"))):
                    if time.monotonic() - lost_at > 30:
                        lost_at = time.monotonic()
                        log.warning("the game says hello, but its save file isn't under %s. Pass --isaac-dir.",
                                    ", ".join(str(d / "data") for d in isaac_dirs) or "any Isaac install found")
                    continue
                await handle(msg, send)
        link.flush()
        await asyncio.sleep(poll_s)
