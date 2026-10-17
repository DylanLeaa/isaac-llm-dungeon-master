"""The models the app's Settings can pick from: OpenRouter's live catalogue, with prices and intelligence scores.

Three roles, each with its own pick:
  major   the planners (run narrative + floor brief)
  minor   the DM (every decision, and the epitaph)
  gate    the re-ask gate (OpenRouter's typed-decisions models)

A pick is stored in config.json as an OpenRouter id ("anthropic/claude-haiku-5.5"), as "claude-code:<model>"
for Claude Code, or blank for the default. With Claude Code switched on, its models are pinned on top of the
major and minor lists. The catalogue is cached next to the settings, so the pickers work offline.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass

import appdata
from claude_code import CLAUDE_CODE_DM_MODEL, CLAUDE_CODE_PLANNER_MODEL
from dm_agent import OPENROUTER_DEFAULT_MODEL
from gate import GATE_DEFAULT_MODEL
from planners import FLOOR_DEFAULT_MODEL

MODELS_URL = "https://openrouter.ai/api/v1/models"
CLAUDE_CODE = "claude-code:"                      # prefix of a Claude Code pick
ROLES = ("major", "minor", "gate")
_FAMILIES = ("haiku", "sonnet", "opus", "fable")  # one Claude Code row each: the newest


@dataclass
class ModelInfo:
    value: str                    # what config.json stores for this pick
    id: str
    name: str
    author: str
    intelligence: float | None    # Artificial Analysis intelligence index, via OpenRouter
    prompt: float | None          # USD per million tokens
    completion: float | None
    cache_read: float | None
    context: int
    created: int
    tools: bool                   # forced tool calls work (the DM needs them)
    reasoning: str                # "always", "optional" or ""
    description: str = ""
    backend: str = "openrouter"   # or "claude-code"


# ------------------------------------------------------------------ fetching and the cache
def fetch(timeout: float = 15.0) -> dict:
    """The live catalogue: text models, and the decisions models the gate can use. No key needed."""
    import httpx
    with httpx.Client(timeout=timeout, headers={"X-Title": "LLM Dungeon Master"}) as client:
        text = client.get(MODELS_URL)
        text.raise_for_status()
        decisions = client.get(MODELS_URL, params={"output_modalities": "decisions"})
        decisions.raise_for_status()
    return {"fetched": time.time(), "text": text.json()["data"], "decisions": decisions.json()["data"]}


def cache_path():
    return appdata.data_dir() / "models.json"


def load_cache() -> dict | None:
    try:
        raw = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) and "text" in raw else None


def save_cache(raw: dict) -> None:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(raw), encoding="utf-8")
    os.replace(tmp, path)


# ------------------------------------------------------------------ parsing
def _per_m(v) -> float | None:
    try:
        return round(float(v) * 1_000_000, 6)
    except (TypeError, ValueError):
        return None


def _info(m: dict) -> ModelInfo:
    pricing = m.get("pricing") or {}
    params = m.get("supported_parameters") or []
    aa = (m.get("benchmarks") or {}).get("artificial_analysis") or {}
    reasoning = m.get("reasoning") or {}
    score = aa.get("intelligence_index")
    return ModelInfo(
        value=m["id"], id=m["id"], name=m.get("name") or m["id"], author=m["id"].lstrip("~").split("/")[0],
        intelligence=float(score) if isinstance(score, (int, float)) else None,
        prompt=_per_m(pricing.get("prompt")), completion=_per_m(pricing.get("completion")),
        cache_read=_per_m(pricing.get("input_cache_read")),
        context=int(m.get("context_length") or 0), created=int(m.get("created") or 0),
        tools="tools" in params and "tool_choice" in params,
        reasoning="always" if reasoning.get("mandatory") else "optional" if "reasoning" in params else "",
        description=(m.get("description") or "").strip())


def parse(raw: dict | None) -> dict[str, list[ModelInfo]]:
    """{"text": [...], "decisions": [...]}. Batch variants are dropped: they don't answer straight away."""
    out: dict[str, list[ModelInfo]] = {"text": [], "decisions": []}
    for kind in out:
        for m in (raw or {}).get(kind) or []:
            if isinstance(m, dict) and m.get("id") and not m["id"].endswith(":batch"):
                out[kind].append(_info(m))
    return out


def to_claude_code_id(openrouter_id: str) -> str:
    """anthropic/claude-haiku-5.5 -> claude-haiku-5-5 (OpenRouter slugs use dots, Claude Code dashes)."""
    return openrouter_id.split("/", 1)[-1].replace(".", "-")


def _claude_code_row(model: str, base: ModelInfo | None = None) -> ModelInfo:
    name = "Claude Code: " + (base.name.split(": ", 1)[-1] if base else model)
    return ModelInfo(value=CLAUDE_CODE + model, id=model, name=name, author="anthropic",
                     intelligence=base.intelligence if base else None, prompt=None, completion=None, cache_read=None,
                     context=base.context if base else 0, created=base.created if base else 0, tools=True,
                     reasoning=base.reasoning if base else "",
                     description="Runs through your own signed-in Claude Code (claude -p), billed to your Claude plan.",
                     backend="claude-code")


def claude_code_rows(text: list[ModelInfo]) -> list[ModelInfo]:
    """The newest Anthropic model of each family, run through Claude Code. Without a catalogue, the defaults."""
    newest: dict[str, ModelInfo] = {}
    for m in text:
        hit = re.match(r"anthropic/claude-([a-z]+)-", m.id)
        if hit and hit.group(1) in _FAMILIES and ":" not in m.id:
            if hit.group(1) not in newest or m.created > newest[hit.group(1)].created:
                newest[hit.group(1)] = m
    if not newest:
        return [_claude_code_row(m) for m in dict.fromkeys((CLAUDE_CODE_DM_MODEL, CLAUDE_CODE_PLANNER_MODEL))]
    return sort_rows([_claude_code_row(to_claude_code_id(m.id), m) for m in newest.values()])


def candidates(catalog: dict[str, list[ModelInfo]], role: str, claude_code_on: bool) -> tuple[list, list]:
    """(pinned, rows) for one role's picker. Pinned are the Claude Code rows, shown on top."""
    if role == "gate":
        return [], list(catalog["decisions"])
    rows = [m for m in catalog["text"] if m.tools] if role == "minor" else list(catalog["text"])
    return (claude_code_rows(catalog["text"]) if claude_code_on else []), rows


# ------------------------------------------------------------------ filtering and sorting
def filter_rows(rows: list[ModelInfo], query: str = "", author: str = "", scored_only: bool = False) -> list:
    q = query.strip().lower()
    return [m for m in rows
            if (not q or q in m.name.lower() or q in m.id.lower())
            and (not author or m.author == author)
            and (not scored_only or m.intelligence is not None)]


SORT_KEYS = ("intelligence", "name", "id", "prompt", "completion", "cache_read", "context", "reasoning", "created")


def sort_rows(rows: list[ModelInfo], key: str = "intelligence", desc: bool = True) -> list[ModelInfo]:
    """By one column. Rows without a value go last either way; ties go to the newest."""
    def value(m: ModelInfo):
        v = getattr(m, key)
        return v.lower() if isinstance(v, str) else v
    have = [m for m in rows if value(m) not in (None, "")]
    missing = [m for m in rows if value(m) in (None, "")]
    have.sort(key=lambda m: -m.created)
    have.sort(key=value, reverse=desc)
    missing.sort(key=lambda m: -m.created)
    return have + missing


# ------------------------------------------------------------------ the saved picks
def default_value(role: str, claude_code_on: bool) -> str:
    if role == "gate":
        return GATE_DEFAULT_MODEL
    if claude_code_on:
        return CLAUDE_CODE + (CLAUDE_CODE_PLANNER_MODEL if role == "major" else CLAUDE_CODE_DM_MODEL)
    return FLOOR_DEFAULT_MODEL if role == "major" else OPENROUTER_DEFAULT_MODEL


def effective_value(pick: str, role: str, claude_code_on: bool) -> str:
    """The pick, or the default when blank. A Claude Code pick only counts while Claude Code is on."""
    pick = (pick or "").strip()
    if not pick or (pick.startswith(CLAUDE_CODE) and (not claude_code_on or role == "gate")):
        return default_value(role, claude_code_on)
    return pick


def resolve(cfg: dict, role: str) -> tuple[str, str]:
    """(backend, model) for a role from the saved settings: ("claude-code", "claude-haiku-5-5") etc."""
    value = effective_value(cfg.get(f"{role}_model", ""), role, bool(cfg.get("use_claude_code")))
    if value.startswith(CLAUDE_CODE):
        return "claude-code", value[len(CLAUDE_CODE):]
    return "openrouter", value


def price(v: float | None) -> str:
    if v is None:
        return "-"
    if v == 0:
        return "free"
    return f"${v:.2f}" if v >= 0.1 else f"${v:.3f}"


def find(catalog: dict[str, list[ModelInfo]], value: str) -> ModelInfo | None:
    if value.startswith(CLAUDE_CODE):
        model = value[len(CLAUDE_CODE):]
        return next((m for m in claude_code_rows(catalog["text"]) if m.id == model), None) or _claude_code_row(model)
    return next((m for kind in ("text", "decisions") for m in catalog[kind] if m.id == value), None)


def describe(catalog: dict[str, list[ModelInfo]], value: str) -> str:
    """One line for Settings: name, intelligence, input / output price."""
    m = find(catalog, value)
    if m is None:
        return value
    bits = [f"{m.name}  ({m.id})" if m.backend == "openrouter" else m.name]
    if m.intelligence is not None:
        bits.append(f"intelligence {m.intelligence:g}")
    if m.backend == "claude-code":
        bits.append("billed to your Claude plan")
    elif m.completion:
        bits.append(f"{price(m.prompt)} in / {price(m.completion)} out per M tokens")
    else:
        bits.append(f"{price(m.prompt)} per M tokens")
    return "  ·  ".join(bits)
