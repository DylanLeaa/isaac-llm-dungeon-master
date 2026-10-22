"""Puts the mod in the game's mods folder, so installing the app is all a player has to do.

The exe carries a copy of mod/llm_dungeon_master (run from source, the repo's is used). At start it
copies it into <install>/mods/llm_dungeon_master of every Isaac install found, when what's there
differs, so the mod and the backend always speak the same PROTO. Files the mod no longer has are
removed; Isaac's disable.it (the player turned the mod off in its Mods menu) is kept.

Left alone:
- a mods/llm_dungeon_master that is a symlink or junction: a developer's link to their checkout;
- an install with a Workshop copy (mods/llm_dungeon_master_<id>): two copies would both run.
"""
from __future__ import annotations

import logging
import os
import stat
import sys
from pathlib import Path

import appdata

log = logging.getLogger("llmdm")

MOD_DIR = "llm_dungeon_master"
KEEP = {"disable.it"}


def bundled() -> Path | None:
    """The mod this app was built with."""
    base = Path(getattr(sys, "_MEIPASS", "")) if appdata.frozen() else Path(__file__).resolve().parents[1]
    src = base / "mod" / MOD_DIR
    return src if (src / "metadata.xml").is_file() else None


def _linked(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _files(root: Path) -> dict[str, Path]:
    return {p.relative_to(root).as_posix(): p for p in root.rglob("*") if p.is_file() and p.name not in KEEP}


def install(isaac_dir: Path, src: Path) -> str:
    """Bring one install's copy up to date: "installed", "updated", "current", "linked" or "workshop"."""
    mods = isaac_dir / "mods"
    dest = mods / MOD_DIR
    if _linked(dest):
        return "linked"
    if mods.is_dir() and any(d.is_dir() and d.name.startswith(MOD_DIR + "_") for d in mods.iterdir()):
        return "workshop"
    fresh = not dest.exists()
    want, have = _files(src), (_files(dest) if not fresh else {})
    changed = False
    for rel, path in want.items():
        data = path.read_bytes()
        target = dest / rel
        if rel in have and have[rel].read_bytes() == data:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        changed = True
    for rel in have.keys() - want.keys():
        (dest / rel).unlink()
        changed = True
    for d in sorted((p for p in dest.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if not any(d.iterdir()):
            d.rmdir()
    return "installed" if fresh else "updated" if changed else "current"


def install_all(isaac_dirs: list[Path]) -> dict[Path, str]:
    """install() for each install found; a failure is "failed: <why>"."""
    src = bundled()
    if src is None:
        log.warning("no copy of the mod to install")
        return {}
    out = {}
    for d in isaac_dirs:
        try:
            out[d] = install(d, src)
        except OSError as e:
            out[d] = f"failed: {e}"
        level = logging.WARNING if out[d].startswith("failed") else logging.INFO
        log.log(level, "mod in %s: %s", d / "mods" / MOD_DIR, out[d])
    return out
