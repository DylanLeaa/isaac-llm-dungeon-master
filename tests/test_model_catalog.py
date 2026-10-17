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
    assert mc.parse(None) == {"text": [], "decisions": []}


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
