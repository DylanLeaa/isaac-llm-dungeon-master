"""The app's model pickers: OpenRouter's catalogue parsed, filtered and sorted, and the saved picks resolved."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import model_catalog as mc  # noqa: E402
from server import build_dm  # noqa: E402


def model(id, score=None, prompt="0.000001", completion="0.000005", tools=True, created=100, mandatory=False,
          cache=None):
    params = ["max_tokens", "reasoning"] + (["tools", "tool_choice"] if tools else [])
    m = {"id": id, "name": id.split("/")[-1].title(), "created": created, "context_length": 200_000,
         "pricing": {"prompt": prompt, "completion": completion}, "supported_parameters": params,
         "reasoning": {"mandatory": mandatory}}
    if cache is not None:
        m["pricing"]["input_cache_read"] = cache
    if score is not None:
        m["benchmarks"] = {"artificial_analysis": {"intelligence_index": score}}
    return m


RAW = {"fetched": 1_700_000_000, "text": [
    model("anthropic/claude-haiku-5.5", 43.4, "0.0000001", "0.0000005", created=300, cache="0.00000001"),
    model("anthropic/claude-haiku-5.5:batch", 43.4, created=300),
    model("anthropic/claude-haiku-4.5", 30, created=100),
    model("anthropic/claude-sonnet-5.5", 56, "0.000002", "0.00001", created=290, mandatory=True),
    model("acme/no-tools", 50, tools=False),
    model("acme/unscored", None, "0", "0", created=500),
    model("acme/old-unscored", None, created=50),
], "decisions": [
    model("inception/mercury-decide:free", None, "0", "0", tools=False, created=10),
    model("perplexity/pplx-decider-v1.2", None, "0.00000002", "0", tools=False, created=20),
]}
CAT = mc.parse(RAW)


def test_parse_prices_scores_and_batch():
    ids = [m.id for m in CAT["text"]]
    assert "anthropic/claude-haiku-5.5:batch" not in ids
    haiku = next(m for m in CAT["text"] if m.id == "anthropic/claude-haiku-5.5")
    assert (haiku.prompt, haiku.completion, haiku.cache_read) == (0.1, 0.5, 0.01)
    assert haiku.intelligence == 43.4 and haiku.tools and haiku.author == "anthropic"
    assert haiku.reasoning == "optional"
    assert next(m for m in CAT["text"] if m.id.endswith("sonnet-5.5")).reasoning == "always"
    unscored = next(m for m in CAT["text"] if m.id == "acme/unscored")
    assert unscored.intelligence is None and unscored.prompt == 0 and unscored.cache_read is None
    assert mc.price(0) == "free" and mc.price(None) == "-" and mc.price(0.1) == "$0.10"
    assert mc.parse(None) == {"text": [], "decisions": [], "google": []}


def test_candidates_per_role():
    pinned, rows = mc.candidates(CAT, "minor", claude_code_on=False)
    assert pinned == [] and "acme/no-tools" not in [m.id for m in rows]
    _, rows = mc.candidates(CAT, "major", claude_code_on=False)
    assert "acme/no-tools" in [m.id for m in rows]
    pinned, rows = mc.candidates(CAT, "gate", claude_code_on=True)
    assert pinned == [] and [m.id for m in rows] == ["inception/mercury-decide:free", "perplexity/pplx-decider-v1.2"]


def test_claude_code_rows_pinned_newest_per_family():
    pinned, _ = mc.candidates(CAT, "minor", claude_code_on=True)
    assert [m.value for m in pinned] == ["claude-code:claude-sonnet-5-5", "claude-code:claude-haiku-5-5"]
    assert all(m.backend == "claude-code" and m.prompt is None for m in pinned)
    assert pinned[1].intelligence == 43.4
    # No catalogue: Claude Code's own defaults.
    assert [m.value for m in mc.claude_code_rows([])] == ["claude-code:claude-haiku-5-5",
                                                          "claude-code:claude-sonnet-5-5"]
    assert mc.to_claude_code_id("anthropic/claude-opus-4.1") == "claude-opus-4-1"


def test_sort_and_filter():
    rows = CAT["text"]
    ordered = [m.id for m in mc.sort_rows(rows)]
    assert ordered[0] == "anthropic/claude-sonnet-5.5"
    assert ordered[-2:] == ["acme/unscored", "acme/old-unscored"]         # no score: last, newest first
    cheap = [m.id for m in mc.sort_rows(rows, "prompt", desc=False)]
    assert cheap[0] == "acme/unscored"
    assert [m.id for m in mc.sort_rows(rows, "intelligence", desc=False)][-2:] == ordered[-2:]
    assert {m.id for m in mc.filter_rows(rows, "HAIKU")} == {"anthropic/claude-haiku-5.5", "anthropic/claude-haiku-4.5"}
    assert {m.author for m in mc.filter_rows(rows, author="acme")} == {"acme"}
    assert all(m.intelligence is not None for m in mc.filter_rows(rows, scored_only=True))


def test_whitelist_on_minor_rows():
    wl = {"anthropic/claude-haiku-5.5": {"status": "ok", "calls": 16, "cost": 0.008, "median_s": 1.9},
          "anthropic/claude-sonnet-5.5": {"status": "slow", "calls": 16, "cost": 0.16, "median_s": 13.1},
          "acme/unscored": {"status": "fail", "calls": 1, "cost": 0.002}}
    _, rows = mc.candidates(CAT, "minor", claude_code_on=False, whitelist=wl)
    by_id = {m.id: m for m in rows}
    haiku, sonnet, failed = by_id["anthropic/claude-haiku-5.5"], by_id["anthropic/claude-sonnet-5.5"], by_id["acme/unscored"]
    assert haiku.whitelisted and haiku.turn_cost == pytest.approx(0.0005) and haiku.latency == 1.9
    assert sonnet.whitelisted is False and sonnet.latency == 13.1                  # slow: measured, not listed
    assert failed.whitelisted is False and failed.turn_cost is None and failed.latency is None
    assert by_id["anthropic/claude-haiku-4.5"].whitelisted is False                # never probed
    assert [m.id for m in mc.filter_rows(rows, whitelist_only=True, backend="openrouter")] == ["anthropic/claude-haiku-5.5"]
    assert CAT["text"][0].whitelisted is None                                      # the catalogue itself untouched
    _, major = mc.candidates(CAT, "major", claude_code_on=False, whitelist=wl)
    assert all(m.whitelisted is not None for m in major)
    assert mc.turn_price(0.0005) == "$0.00050" and mc.turn_price(0) == "free" and mc.turn_price(None) == "-"


def test_shipped_whitelist_loads():
    wl = mc.load_whitelist()
    assert wl and all("status" in v for v in wl.values())


def test_resolve_picks():
    assert mc.resolve({}, "minor") == ("openrouter", "anthropic/claude-haiku-5.5")
    assert mc.resolve({}, "major") == ("openrouter", "anthropic/claude-sonnet-5.5")
    assert mc.resolve({"use_claude_code": True}, "minor") == ("claude-code", "claude-haiku-5-5")
    assert mc.resolve({"use_claude_code": True}, "major") == ("claude-code", "claude-sonnet-5-5")
    assert mc.resolve({"use_claude_code": True, "minor_model": "acme/x"}, "minor") == ("openrouter", "acme/x")
    # A Claude Code pick waits for the switch.
    cfg = {"major_model": "claude-code:claude-opus-5-5"}
    assert mc.resolve(cfg, "major") == ("openrouter", "anthropic/claude-sonnet-5.5")
    assert mc.resolve({**cfg, "use_claude_code": True}, "major") == ("claude-code", "claude-opus-5-5")
    assert mc.resolve({}, "gate") == ("openrouter", "inception/mercury-decide:free")
    assert mc.resolve({"gate_model": "perplexity/pplx-decider-v1.2"}, "gate")[1] == "perplexity/pplx-decider-v1.2"


def test_describe():
    assert "intelligence 43.4" in mc.describe(CAT, "anthropic/claude-haiku-5.5")
    assert "$0.10 in / $0.50 out" in mc.describe(CAT, "anthropic/claude-haiku-5.5")
    assert "Claude plan" in mc.describe(CAT, "claude-code:claude-haiku-5-5")
    assert mc.describe(CAT, "gone/model") == "gone/model"


def test_build_dm_separate_models():
    engine = build_dm("openrouter", "sk-test", cruelty=5, model="acme/fast", narrative_model="acme/clever",
                      floor_model="acme/clever", planner_backend="openrouter", gate_model="perplexity/pplx-decider-v1.2")
    assert engine.dm.model == "acme/fast"
    assert engine.planner.narrative_model == engine.planner.floor_model == "acme/clever"
    assert engine.gate.model == "perplexity/pplx-decider-v1.2"
    assert engine.label == "acme/fast via OpenRouter"
    with pytest.raises(ValueError):
        build_dm("openrouter", "", cruelty=5)
    with pytest.raises(ValueError, match="OpenRouter API key"):        # Claude Code DM, OpenRouter planners
        build_dm("claude-code", "", cruelty=5, planner_backend="openrouter")


GOOGLE_RAW = {"google": [
    {"name": "models/gemini-2.5-flash", "displayName": "Gemini 2.5 Flash", "inputTokenLimit": 1048576, "thinking": True},
    {"name": "models/gemini-9-new", "displayName": "Gemini 9 New", "inputTokenLimit": 2000000}]}


def test_google_models_and_provider_filter():
    cat = mc.parse({**RAW, **GOOGLE_RAW})
    wl = {"google:gemini-2.5-flash": {"status": "ok", "calls": 2, "cost": 0.002, "median_s": 1.0}}
    _, rows = mc.candidates(cat, "minor", claude_code_on=False, whitelist=wl)
    google = mc.filter_rows(rows, backend="google")
    assert [m.value for m in google] == ["google:gemini-2.5-flash", "google:gemini-9-new"]
    flash, new = google
    assert flash.prompt == 0.30 and new.prompt is None and new.context == 2_000_000          # price known / unknown
    assert [m.id for m in mc.filter_rows(rows, whitelist_only=True, backend="google")] == ["gemini-2.5-flash"]
    pinned, _ = mc.candidates(cat, "major", claude_code_on=True, whitelist={})
    assert {m.backend for m in pinned} == {"claude-code"} and not any(m.whitelisted for m in pinned)   # no data: no
    assert mc.filter_rows(pinned, whitelist_only=True) == []
    assert mc.candidates(cat, "gate", claude_code_on=False)[1] == CAT["decisions"]      # the gate stays on OpenRouter
    assert mc.parse(None)["google"] == []


def test_google_picks_resolve():
    cfg = {"major_model": "google:gemini-2.5-pro", "minor_model": "google:gemini-2.5-flash", "gate_model": "google:x"}
    assert mc.resolve(cfg, "major") == ("google", "gemini-2.5-pro")
    assert mc.resolve(cfg, "minor") == ("google", "gemini-2.5-flash")
    assert mc.resolve(cfg, "gate")[0] == "openrouter"
    assert "Gemini 2.5 Flash" in mc.describe(mc.parse(GOOGLE_RAW), "google:gemini-2.5-flash")


def test_google_completion_tidied():
    from google_ai import tidy
    data = tidy({"choices": [{"message": {"content": "<thought>hmm ok</thought>Hi there!"}}],
                 "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 36}}, "gemini-2.5-flash")
    assert data["choices"][0]["message"]["content"] == "Hi there!"
    assert data["usage"]["completion_tokens"] == 32                      # the thinking is billed as output
    assert data["usage"]["cost"] == pytest.approx((4 * 0.30 + 32 * 2.50) / 1_000_000)
    assert "cost" not in tidy({"usage": {"prompt_tokens": 5, "total_tokens": 9}}, "unpriced-model")["usage"]


def test_keys_come_only_from_settings(monkeypatch):
    import appdata
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-or")
    monkeypatch.setenv("GOOGLE_API_KEY", "env-g")
    assert appdata.api_key({}) == "" and appdata.google_key({}) == ""
    assert appdata.google_key({"google_api_key": " k "}) == "k"
