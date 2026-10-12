"""The same DM and planners, answered by your local Claude Code in headless mode (`claude -p`).

Each call is one `claude -p` process: no tools, no settings, MCP servers or session files, and the
system prompt replaced with ours. A decision's tool schema goes in as --json-schema, so the answer
comes back already validated as `structured_output`. Billing follows however Claude Code is signed in
(a Claude subscription or an API key), and no OPENROUTER_API_KEY is needed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from dm_agent import LLMCallError, build_user_message
from planners import OpenRouterPlanner
from prompts import system_prompt
from schemas import TOOLS

CLAUDE_CODE_DM_MODEL = "claude-haiku-5-5"
CLAUDE_CODE_PLANNER_MODEL = "claude-sonnet-5-5"

_PROMPT_DIR = Path(tempfile.gettempdir()) / "llmdm_claude_code"


def find_claude(explicit: str | None = None) -> str:
    """Path to the Claude Code executable. On Windows, npm installs a claude.cmd shim; going through it
    would put every argument (JSON schemas included) through cmd.exe's quoting, so use the real
    claude.exe it points at."""
    path = explicit or os.environ.get("LLMDM_CLAUDE_BIN") or shutil.which("claude")
    if not path:
        raise SystemExit("Claude Code not found: install it, or pass --claude-bin / set LLMDM_CLAUDE_BIN.")
    p = Path(path)
    if p.suffix.lower() in (".cmd", ".ps1", "") and os.name == "nt":
        exe = p.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if exe.exists():
            return str(exe)
    return str(p)


def _prompt_file(text: str) -> str:
    """System prompts run to ~20K chars with the campaign, too close to Windows' command-line limit to
    pass inline. Written once per distinct text."""
    _PROMPT_DIR.mkdir(exist_ok=True)
    path = _PROMPT_DIR / (hashlib.sha256(text.encode()).hexdigest()[:20] + ".txt")
    if not path.exists():
        path.write_text(text, encoding="utf-8")
    return str(path)


def _usage(data: dict) -> dict:
    u = data.get("usage") or {}
    cached = int(u.get("cache_read_input_tokens") or 0)
    fresh = int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0)
    return {"in": fresh + cached, "cached": cached, "out": int(u.get("output_tokens") or 0),
            "cost": float(data.get("total_cost_usd") or 0.0)}     # list price; a subscription doesn't bill it


class ClaudeCodeRunner:
    """Runs one headless Claude Code completion per call."""

    def __init__(self, claude_bin: str | None = None, timeout: float = 60.0) -> None:
        self.bin = find_claude(claude_bin)
        self.timeout = timeout

    async def complete(self, model: str, system: str, user: str, schema: dict | None = None,
                       timeout: float | None = None) -> tuple[Any, dict]:
        """(answer, call record). The answer is the parsed object when a schema is given, else the text."""
        argv = [self.bin, "-p", "--output-format", "json", "--model", model,
                "--system-prompt-file", _prompt_file(system),
                "--tools", "", "--setting-sources", "", "--strict-mcp-config",
                "--no-session-persistence", "--disable-slash-commands"]
        if schema is not None:
            argv += ["--json-schema", json.dumps(schema, separators=(",", ":"))]
        record = {"model": model, "provider": "claude-code", "system": [system], "user": user}
        # Run outside the repo so no project CLAUDE.md or settings ride along.
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=tempfile.gettempdir(), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(user.encode("utf-8")), timeout or self.timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            proc.kill()
            await proc.wait()
            raise
        try:
            data = json.loads(out)
        except json.JSONDecodeError as e:
            tail = (err or out).decode("utf-8", "replace").strip()[-400:]
            raise LLMCallError(f"claude exited {proc.returncode} without JSON: {tail}", record) from e
        record.update({"id": data.get("session_id"), "finish": data.get("stop_reason"),
                       "response": data.get("structured_output", data.get("result")),
                       "usage": _usage(data), "error": data.get("result") if data.get("is_error") else None})
        if data.get("is_error") or data.get("subtype") != "success":
            raise LLMCallError(f"claude-code {data.get('subtype')}: {data.get('result')}", record)
        if schema is not None:
            answer = data.get("structured_output")
            if not isinstance(answer, dict):
                raise LLMCallError("no structured output in the response", record)
            return answer, record
        text = (data.get("result") or "").strip()
        if not text:
            raise LLMCallError("empty response", record)
        return text, record


class ClaudeCodeDM:
    """The DM through Claude Code. Same persona, campaign and tool schemas as OpenRouterDM."""

    def __init__(self, cruelty: int, model: str = CLAUDE_CODE_DM_MODEL, claude_bin: str | None = None,
                 timeout: float = 60.0) -> None:
        self.runner = ClaudeCodeRunner(claude_bin, timeout)
        self.model = model
        self.campaign = ""
        self.set_cruelty(cruelty)

    def set_cruelty(self, cruelty: int) -> None:
        self.cruelty = cruelty
        self._build_system()

    def set_campaign(self, text: str) -> None:
        """The planners' narrative + floor brief, shown after the persona."""
        self.campaign = text
        self._build_system()

    def _build_system(self) -> None:
        self.system = system_prompt(self.cruelty) + ("\n\n" + self.campaign if self.campaign else "")

    async def decide(self, kind: str, state: dict, ctx: dict, memory: dict) -> dict[str, Any]:
        tool = TOOLS[kind]
        # The tool's description travels with the question, since --json-schema only carries the shape.
        user = (f"Answer as the `{tool['name']}` decision: {tool['description']}\n\n"
                + build_user_message(kind, state, ctx, memory))
        args, record = await self.runner.complete(self.model, self.system, user, tool["input_schema"])
        return {**args, "_usage": record["usage"], "_call": record}

    async def epitaph(self, prompt: str) -> tuple[str, dict]:
        """Free text in the DM's voice (the after-death screen)."""
        return await self.runner.complete(self.model, self.system, prompt)


class ClaudeCodePlanner(OpenRouterPlanner):
    """Narrative and floor briefs through Claude Code; the prompts are OpenRouterPlanner's."""

    def __init__(self, narrative_model: str = CLAUDE_CODE_PLANNER_MODEL,
                 floor_model: str = CLAUDE_CODE_PLANNER_MODEL, claude_bin: str | None = None,
                 timeout: float = 180.0) -> None:
        self.runner = ClaudeCodeRunner(claude_bin, timeout)
        self.narrative_model = narrative_model
        self.floor_model = floor_model

    async def _complete(self, model: str, system: str, user: str) -> tuple[str, dict]:
        text, record = await self.runner.complete(model, system, user)
        return text, {**record["usage"], "_call": record}
