"""Google AI Studio: Gemini through its OpenAI-compatible chat endpoint, with the player's own API key.

It speaks the same chat/tools dialect as OpenRouter, minus OpenRouter's extras: no cache_control, no "usage.include"
(so no cost in the response: it's worked out here from PRICES) and thinking is set with reasoning_effort.
The model list is Google's own (models.list, needs the key); it carries no prices, so those come from PRICES.
"""
from __future__ import annotations

import re

GOOGLE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
KEYS_URL = "https://aistudio.google.com/apikey"
GOOGLE_DEFAULT_MODEL = "gemini-2.5-flash"

# id -> (USD per million tokens: in, out, cached in): Google's list prices for paid use, for the models we know.
# A model missing here still works: its price shows as "-" and receipts count it as free.
# Standard tier, text input, prompts <= 200k (the DM's prompts are ~15k), as listed on 2026-10-22. Scheduled
# increases: gemini-4-argon doubles on 2026-12-01 (4.00 / 20.00 / 0.20), gemini-3.8-flash and 3.6-flash on
# 2027-01-01 (1.50 / 7.50 / 0.15).
PRICES: dict[str, tuple[float, float, float | None]] = {
    "gemini-4-argon": (2.00, 10.00, 0.10),
    "gemini-3.8-flash": (0.75, 3.75, 0.075),
    "gemini-3.6-flash": (0.75, 3.75, 0.075),
    "gemini-3.1-pro-preview": (2.00, 12.00, 0.20),
    "gemini-3-pro-preview": (2.00, 12.00, 0.20),
    "gemini-3-flash-preview": (0.50, 3.00, 0.05),
    "gemini-3.5-flash-lite": (0.30, 2.50, 0.03),
    "gemini-3.1-flash-lite": (0.25, 1.50, 0.025),
    "gemini-2.5-pro": (1.25, 10.00, 0.125),
    "gemini-2.5-flash": (0.30, 2.50, 0.03),
    "gemini-2.5-flash-lite": (0.10, 0.40, 0.01),
    "gemini-2.0-flash": (0.10, 0.40, 0.025),
    "gemini-2.0-flash-lite": (0.075, 0.30, None),
}
_THOUGHT = re.compile(r"<thought>.*?</thought>", re.S)
# Not chat models for the DM, whatever generateContent says.
_NOT_CHAT = re.compile(r"tts|image|embedding|live|audio|robotics|computer-use|aqa|imagen|veo|learnlm|banana|transcribe", re.I)


def price_of(model: str) -> tuple[float, float, float | None] | None:
    model = model.removeprefix("models/")
    return PRICES.get(model) or PRICES.get(re.sub(r"-\d{3}$", "", model))


def fetch_models(key: str, timeout: float = 15.0) -> list[dict]:
    """Google's model list: the Gemini and Gemma models that can chat, as models.list returns them."""
    import httpx
    out, token = [], ""
    with httpx.Client(timeout=timeout) as client:
        while True:
            resp = client.get(MODELS_URL, params={"pageSize": 1000, "pageToken": token}, headers={"x-goog-api-key": key})
            resp.raise_for_status()
            body = resp.json()
            out += body.get("models") or []
            token = body.get("nextPageToken") or ""
            if not token:
                break
    return [m for m in out if str(m.get("name", "")).startswith(("models/gemini", "models/gemma"))
            and "generateContent" in (m.get("supportedGenerationMethods") or []) and not _NOT_CHAT.search(m["name"])]


def strip_cache_control(messages: list[dict]) -> list[dict]:
    """The messages without OpenRouter's cache_control marks (Google rejects fields it doesn't know)."""
    out = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            m = {**m, "content": [{k: v for k, v in part.items() if k != "cache_control"} for part in content]}
        out.append(m)
    return out


def tidy(data: dict, model: str) -> dict:
    """A Google completion made to look like OpenRouter's: thinking tokens counted as output, the thinking text
    (Gemma writes it inline as <thought>...</thought>) taken out of the reply, and usage.cost worked out from PRICES
    (the responses carry tokens only; a model without a price stays free)."""
    for choice in data.get("choices") or []:
        msg = choice.get("message") if isinstance(choice, dict) else None
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            msg["content"] = _THOUGHT.sub("", msg["content"]).strip()
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return data
    prompt = int(usage.get("prompt_tokens") or 0)
    # total_tokens includes the thinking, which completion_tokens leaves out; it's billed as output.
    usage["completion_tokens"] = max(int(usage.get("completion_tokens") or 0), int(usage.get("total_tokens") or 0) - prompt)
    spec = price_of(model)
    if spec:
        cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        fresh = max(prompt - cached, 0)
        usage["cost"] = (fresh * spec[0] + cached * (spec[2] if spec[2] is not None else spec[0])
                         + usage["completion_tokens"] * spec[1]) / 1_000_000
    return data
