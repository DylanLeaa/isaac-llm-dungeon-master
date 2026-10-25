"""Agent mode: one call per trigger stacks, shuffles or keeps any number of decks, pushed or answered."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import server  # noqa: E402
from agent import tool_calls  # noqa: E402
from server import Coordinator, Engine, build_dm  # noqa: E402

FLOOR = {"stage": 1, "stage_type": 0, "name": "Basement I", "room_types": {"default": 10}}


class NoDM:
    """Classic-mode DM: records what it was asked (agent mode must never ask it for a deck)."""

    def __init__(self, answers=()):
        self.answers, self.seen = list(answers), []

    def set_cruelty(self, cruelty):
        pass

    async def decide(self, kind, state, ctx, memory):
        self.seen.append((kind, ctx))
        return dict(self.answers.pop(0))


class ScriptedAgent:
    """Each turn answers with the next script: a list of calls, or a function of the turn that returns one."""

    def __init__(self, turns):
        self.turns, self.seen, self.campaign = list(turns), [], ""

    def set_cruelty(self, cruelty):
        pass

    def set_campaign(self, text):
        self.campaign = text

    async def act(self, message):
        turn = json.loads(message)
        self.seen.append(turn)
        script = self.turns.pop(0)
        if isinstance(script, Exception):
            raise script
        calls = script(turn) if callable(script) else script
        return calls, {"model": "scripted", "usage": {"in": 100, "cached": 0, "out": 50, "cost": 0.01}}


def req(rid, key, kind, ctx=None, keys=1):
    return {"t": "req", "id": rid, "key": key, "kind": kind, "deadline_ms": 4000,
            "state": {"player": {"hp": {"red": 6}, "resources": {"keys": keys, "bombs": 1, "coins": 0}},
                      "level": FLOOR},
            "ctx": {"reason": "test", **(ctx or {})}}


def stack(kind, key, **args):
    return {"tool": f"stack_{kind}", "args": {"key": key, "intent": f"{kind} {key}", "taunt": "", **args}}


def drops(*rewards):
    return {"outcomes": [{"reward": r, "count": 1} for r in rewards]}


def clear(*rewards):
    return {"rewards": [{"reward": r, "count": 1} for r in rewards]}


def run(coord, steps, settle=0.3):
    """Hand the coordinator each message (a number: sleep that long), then let the turns finish."""
    sent = []

    async def send(msg):
        sent.append(msg)

    async def go():
        for s in steps:
            if isinstance(s, (int, float)):
                await asyncio.sleep(s)
            else:
                await coord.handle(s, send)
        await asyncio.sleep(settle)

    asyncio.run(go())
    return sent


def results(sent):
    return [m for m in sent if m.get("t") == "res"]


def test_a_burst_of_requests_is_one_turn_and_one_bill():
    agent = ScriptedAgent([[stack("room_clear", "clear:1:85", **clear("bomb")),
                            stack("room_clear", "clear:1:86", **clear()),
                            stack("drops", "drops:1", **drops("nothing", "penny"))]])
    dm = NoDM()
    coord = Coordinator(dm, cruelty=6, agent=agent)
    sent = run(coord, [req(1, "drops:1", "drops", {"wanted": 2}), req(2, "clear:1:85", "room_clear"),
                       req(3, "clear:1:86", "room_clear")])
    assert len(agent.seen) == 1 and not dm.seen
    turn = agent.seen[0]
    assert {p["key"] for p in turn["pending"]} == {"drops:1", "clear:1:85", "clear:1:86"}
    assert "economy" not in turn["pending"][0]["context"] and "keys" in turn["economy"], "one economy, the turn's"
    res = {m["key"]: m for m in results(sent)}
    assert all(m["ok"] for m in res.values()) and {m["id"] for m in res.values()} == {1, 2, 3}
    assert [o["reward"] for o in res["drops:1"]["decision"]["outcomes"]] == ["nothing", "penny"]
    assert not any(m.get("push") for m in res.values())
    assert coord.usage["calls"] == 1 and coord.usage["out"] == 50
    assert coord.stats["agent"]["turn"] == 1 and coord.stats["agent"]["stack"] == 3


def test_an_open_deck_is_restacked_unasked_and_its_draws_count_under_the_push():
    agent = ScriptedAgent([
        [stack("drops", "drops:1", **drops("nothing", "nothing", "nothing"))],
        lambda turn: [stack("room_clear", "clear:1:85", **clear()),
                      stack("drops", "drops:1", **drops("bomb", "nothing"))]])
    coord = Coordinator(NoDM(), cruelty=6, agent=agent)
    sent = run(coord, [req(1, "drops:1", "drops", {"wanted": 3}), 0.3,
                       {"t": "drawn", "key": "drops:1", "id": 1, "outcome": {"reward": "nothing"}, "cause": "enemy_death"},
                       req(2, "clear:1:85", "room_clear"), 0.3,
                       {"t": "drawn", "key": "drops:1", "id": -1, "outcome": {"reward": "bomb", "count": 1},
                        "cause": "enemy_death"}])
    opened = agent.seen[1]["open_decks"]
    assert [d["key"] for d in opened] == ["drops:1"] and opened[0]["left"] == ["nothing", "nothing"]
    assert opened[0]["drawn"] == 1 and opened[0]["context"]["wanted"] == 3 and "deck_left" not in opened[0]["context"]
    push = [m for m in results(sent) if m.get("push")]
    assert len(push) == 1 and push[0]["key"] == "drops:1" and push[0]["id"] == -1 and push[0]["drawn_base"] == 1
    assert [o["reward"] for o in push[0]["decision"]["outcomes"]] == ["bomb", "nothing"]
    assert coord.stats["drops"]["pushed"] == 1 and coord.stats["drops"]["applied"] == 2
    assert coord.memory.economy.delivered["bombs"] == 1


def test_shuffle_reorders_what_is_left_and_leaves_the_budget_alone():
    agent = ScriptedAgent([
        [stack("drops", "drops:1", **drops("key", "nothing", "bomb"))],
        [stack("room_clear", "clear:1:85", **clear()), {"tool": "shuffle_deck", "args": {"key": "drops:1", "order": [2, 0]}}]])
    coord = Coordinator(NoDM(), cruelty=6, agent=agent)
    sent = run(coord, [req(1, "drops:1", "drops", {"wanted": 3}), 0.3])
    before = dict(coord.memory.economy.committed["drops:1"])
    sent += run(coord, [req(2, "clear:1:85", "room_clear")])
    push = [m for m in results(sent) if m.get("push")][0]
    assert [o["reward"] for o in push["decision"]["outcomes"]] == ["bomb", "key", "nothing"], "the rest keep their order"
    assert coord.memory.economy.committed["drops:1"] == before
    assert coord.stats["agent"]["shuffle"] == 1


def test_keep_answers_a_re_ask_with_the_deck_as_it_stands():
    agent = ScriptedAgent([[stack("drops", "drops:1", **drops("nothing", "penny"))],
                           [{"tool": "keep_deck", "args": {"key": "drops:1", "why": "nothing changed"}}]])
    coord = Coordinator(NoDM(), cruelty=6, agent=agent)
    sent = run(coord, [req(1, "drops:1", "drops", {"wanted": 2}), 0.3, req(2, "drops:1", "drops", {"wanted": 2})])
    kept = [m for m in results(sent) if m["id"] == 2][0]
    assert kept["ok"] and kept["drawn_base"] == 0 and [o["reward"] for o in kept["decision"]["outcomes"]] == ["nothing", "penny"]
    assert coord.memory.issued["drops:1"]["rid"] == 2 and coord.stats["drops"]["reused"] == 1
    assert "your_previous_answer" in agent.seen[1]["pending"][0]["context"]


def test_a_pending_deck_the_agent_skips_is_answered_no_decision():
    agent = ScriptedAgent([[stack("room_clear", "clear:1:85", **clear())]])
    coord = Coordinator(NoDM(), cruelty=6, agent=agent)
    sent = run(coord, [req(1, "clear:1:85", "room_clear"), req(2, "clear:1:86", "room_clear")])
    res = {m["key"]: m for m in results(sent)}
    assert res["clear:1:85"]["ok"]
    assert res["clear:1:86"] == {"t": "res", "session": None, "id": 2, "key": "clear:1:86", "kind": "room_clear",
                                 "ok": False, "reason": "agent_skipped"}
    assert coord.stats["agent"]["skipped"] == 1


def test_bad_calls_are_rejected_one_by_one_and_the_rest_apply():
    agent = ScriptedAgent([[stack("drops", "clear:1:85", **drops("key")),          # wrong kind for the key
                            stack("room_clear", "clear:9:99", **clear("key")),     # nobody asked, not open
                            {"tool": "launch_missiles", "args": {"key": "clear:1:85"}},
                            stack("room_clear", "clear:1:85", **clear("bomb")),
                            stack("room_clear", "clear:1:85", **clear("key")),     # decided already this turn
                            {"tool": "keep_deck", "args": {"key": "clear:1:86"}}]])  # nothing to keep
    coord = Coordinator(NoDM(), cruelty=6, agent=agent)
    sent = run(coord, [req(1, "clear:1:85", "room_clear"), req(2, "clear:1:86", "room_clear")])
    res = {m["key"]: m for m in results(sent)}
    assert res["clear:1:85"]["decision"]["rewards"] == [{"reward": "bomb", "count": 1}]
    assert res["clear:1:86"]["reason"] == "agent_skipped"
    assert coord.stats["agent"]["rejected"] == 5
    assert "clear:9:99" not in coord.memory.issued


def test_calls_share_one_budget_in_order_and_a_trimmed_deck_wakes_the_agent_not_the_game(monkeypatch):
    monkeypatch.setattr(server, "AGENT_FLAG_WAIT_S", 0.01)
    agent = ScriptedAgent([[stack("room_clear", "clear:1:85", **clear("key")),
                            stack("drops", "drops:1", **drops("key", "key", "nothing"))],
                           lambda turn: [stack("drops", "drops:1", **drops("nothing", "key", "nothing"))]])
    coord = Coordinator(NoDM(), cruelty=6, agent=agent)
    coord.memory.economy.observe({"level": FLOOR})
    coord._brief_written((1, 0), "ARC BEAT: x\nECONOMY: keys=starve coins=starve\nROOMS: y")
    sent = run(coord, [req(1, "clear:1:85", "room_clear"), req(2, "drops:1", "drops", {"wanted": 3})])
    first = [m for m in results(sent) if m["key"] == "drops:1"][0]["decision"]
    assert [o["reward"] for o in first["outcomes"]] == ["key", "nothing", "nothing"]
    assert any("clear:1:85 took the last one" in g for g in first["guardrails"])
    assert not [m for m in sent if m.get("t") == "stale"], "agent mode restacks itself"
    assert len(agent.seen) == 2
    again = agent.seen[1]
    assert again["pending"] == [] and "drops:1 needs a restack (trimmed to the budget)" in again["trigger"]
    assert [d for d in again["open_decks"] if d["key"] == "drops:1"][0]["restack"] == "trimmed to the budget"
    assert len([m for m in results(sent) if m.get("push")]) == 1


def test_an_overspend_flags_the_other_decks_for_the_agent(monkeypatch):
    monkeypatch.setattr(server, "AGENT_FLAG_WAIT_S", 0.01)
    agent = ScriptedAgent([[stack("drops", "drops:1", **drops("key"))],
                           [stack("payout_plan", "payout:1:7", schedule=[{"reward": "key", "count": 2}] * 2)],
                           lambda turn: []])
    coord = Coordinator(NoDM(), cruelty=6, agent=agent)
    coord.memory.economy.observe({"level": FLOOR})
    coord._brief_written((1, 0), "ARC BEAT: x\nECONOMY: keys=starve coins=starve\nROOMS: y")
    opened = {"t": "drawn", "key": "payout:1:7", "id": 2, "cause": "chest_open", "outcome": {"reward": "key", "count": 2}}
    sent = run(coord, [req(1, "drops:1", "drops", {"wanted": 1}), 0.3,
                       req(2, "payout:1:7", "payout_plan", {"source_class": "chest", "source": "chest"}), 0.3,
                       opened, opened])
    assert not [m for m in sent if m.get("t") == "stale"]
    assert len(agent.seen) == 3 and "drops:1 needs a restack (keys over budget)" in agent.seen[2]["trigger"]


def test_a_failed_turn_answers_every_request_it_held():
    agent = ScriptedAgent([RuntimeError("provider melted")])
    coord = Coordinator(NoDM(), cruelty=6, agent=agent)
    sent = run(coord, [req(1, "clear:1:85", "room_clear"), req(2, "clear:1:86", "room_clear")])
    assert sorted((m["key"], m["ok"], m["reason"]) for m in results(sent)) == [
        ("clear:1:85", False, "RuntimeError"), ("clear:1:86", False, "RuntimeError")]
    assert coord.stats["agent"]["error"] == 1


def test_switching_agent_mode_off_hands_what_waits_to_the_dm():
    dm = NoDM([{"intent": "kills", "outcomes": [{"reward": "nothing"}], "taunt": ""}])
    agent = ScriptedAgent([])
    coord = Coordinator(dm, cruelty=6, agent=agent)
    sent = []

    async def send(msg):
        sent.append(msg)

    async def go():
        await coord.handle(req(1, "drops:1", "drops", {"wanted": 1}), send)   # waits out the batch window
        coord.reconfigure(Engine(dm))                                          # agent mode off
        await asyncio.sleep(0.3)

    asyncio.run(go())
    assert not agent.seen and [k for k, _ in dm.seen] == ["drops"]
    assert [m["ok"] for m in results(sent)] == [True]


def test_the_mock_agent_answers_a_whole_floor_start():
    engine = build_dm("mock", "", cruelty=6, agent=True, mock_latency=(0.01, 0.02))
    assert engine.agent is not None and engine.gate is None
    coord = Coordinator(engine.dm, cruelty=6, agent=engine.agent)
    sent = run(coord, [req(1, "drops:1", "drops", {"wanted": 3}), req(2, "clear:1:85", "room_clear"),
                       req(3, "cards:1", "cards", {"wanted": 4}), 0.3, req(4, "clear:1:86", "room_clear")])
    res = {m["key"]: m for m in results(sent) if not m.get("push")}
    assert set(res) == {"drops:1", "clear:1:85", "cards:1", "clear:1:86"} and all(m["ok"] for m in res.values())


def test_tool_calls_drops_the_unreadable_and_keeps_the_rest():
    calls, dropped = tool_calls({"tool_calls": [
        {"function": {"name": "stack_drops", "arguments": '{"key": "drops:1", "outcomes": []}'}},
        {"function": {"name": "stack_cards", "arguments": '{"key": "cards:1", "car'}},
        {"function": {"name": "keep_deck", "arguments": '["drops:1"]'}}]})
    assert calls == [{"tool": "stack_drops", "args": {"key": "drops:1", "outcomes": []}}]
    assert len(dropped) == 2


def test_claude_code_agent_carries_every_tool_in_its_prompt():
    from agent_tools import AGENT_TOOLS
    from claude_code import _AGENT_SCHEMA, _AGENT_TOOLS_TEXT
    assert all(name in _AGENT_TOOLS_TEXT for name in AGENT_TOOLS)
    assert _AGENT_SCHEMA["properties"]["calls"]["items"]["properties"]["tool"]["enum"] == list(AGENT_TOOLS)
