"""Where the backend keeps the player's files: settings, receipts and its own log.

Run from source, receipts stay in backend/receipts as they always have. Packaged as an exe, the code
unpacks to a temp folder that is deleted on exit, so everything lives in the user's app data instead.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_NAME = "LLM Dungeon Master"
DEFAULTS = {"openrouter_api_key": "", "use_claude_code": False}


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "llm-dungeon-master"


def receipts_dir() -> Path:
    return data_dir() / "receipts" if frozen() else Path(__file__).resolve().parent / "receipts"


def config_path() -> Path:
    return data_dir() / "config.json"


def load_config() -> dict:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {**DEFAULTS, **{k: v for k, v in (data if isinstance(data, dict) else {}).items() if k in DEFAULTS}}


def save_config(cfg: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({k: cfg.get(k, v) for k, v in DEFAULTS.items()}, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def api_key(cfg: dict) -> str:
    """The key from Settings, else the OPENROUTER_API_KEY environment variable."""
    return (cfg.get("openrouter_api_key") or "").strip() or os.environ.get("OPENROUTER_API_KEY", "").strip()
