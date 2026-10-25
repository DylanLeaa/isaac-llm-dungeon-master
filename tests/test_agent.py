"""Agent mode: one call per trigger stacks, shuffles or keeps any number of decks, pushed or answered."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import server  # noqa: E402
from agent import tool_calls  # noqa: E402
from bridge import PROTO  # noqa: E402
from gate import MockGate  # noqa: E402
from server import Coordinator, Engine, build_dm  # noqa: E402

FLOOR = {"stage": 1, "stage_type": 0, "name": "Basement I", "room_types": {"default": 10}}


@pytest.fixture(autouse=True)
def quick_batches(monkeypatch):
    """The batch windows, short enough for a test's settle time (a burst still shares one turn)."""
    monkeypatch.setattr(server, "AGENT_BATCH_S", {server.URGENT: 0.0, server.NEXT_DOOR: 0.05, server.BACKGROUND: 0.1})


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

    def __init__(self, turns, delay=0.0):
        self.turns, self.seen, self.campaign, self.delay = list(turns), [], "", delay

    def set_cruelty(self, cruelty):
        pass

    def set_campaign(self, text):
        self.campaign = text

    async def act(self, message):
        turn = json.loads(message)
        self.seen.append(turn)
        await asyncio.sleep(self.delay)
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
    assert opened[0]["drawn"] == 1 and "context" not in opened[0], "only a deck flagged for a restack carries it"
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
    assert engine.agent is not None and engine.gate is not None, "the gate screens the agent's re-asks too"
    coord = Coordinator(engine.dm, cruelty=6, agent=engine.agent, gate=engine.gate)
    sent = run(coord, [req(1, "drops:1", "drops", {"wanted": 3}), req(2, "clear:1:85", "room_clear"),
                       req(3, "cards:1", "cards", {"wanted": 4}), 0.3, req(4, "clear:1:86", "room_clear")])
    res = {m["key"]: m for m in results(sent) if not m.get("push")}
    assert set(res) == {"drops:1", "clear:1:85", "cards:1", "clear:1:86"} and all(m["ok"] for m in res.values())


def test_a_re_ask_goes_to_the_gate_first_and_only_a_moved_state_wakes_the_agent():
    agent = ScriptedAgent([[stack("drops", "drops:1", **drops("nothing", "penny"))],
                           [stack("drops", "drops:1", **drops("key"))]])
    coord = Coordinator(NoDM(), cruelty=6, agent=agent, gate=MockGate())
    sent = run(coord, [req(1, "drops:1", "drops", {"wanted": 2}), 0.3,
                       req(2, "drops:1", "drops", {"wanted": 2}), 0.3,              # nothing changed: kept, no turn
                       req(3, "drops:1", "drops", {"wanted": 2}, keys=3)])          # a resource moved: the agent
    res = {m["id"]: m for m in results(sent)}
    assert res[2]["ok"] and res[2]["drawn_base"] == 0
    assert [o["reward"] for o in res[2]["decision"]["outcomes"]] == ["nothing", "penny"]
    assert len(agent.seen) == 2 and agent.seen[1]["pending"][0]["key"] == "drops:1"
    assert [o["reward"] for o in res[3]["decision"]["outcomes"]] == ["key"]
    assert coord.stats["gate"]["reask"] == 1 and coord.stats["drops"]["reused"] == 1


@pytest.mark.parametrize("reason", ["eta under lead time", "player entered an adjacent room",
                                    "player is next to this room", "refresh: player state changed", "boss defeated"])
def test_a_routine_re_ask_is_answered_by_the_deck_with_no_gate_and_no_turn(reason):
    agent = ScriptedAgent([[stack("drops", "drops:1", **drops("nothing", "penny"))]])
    coord = Coordinator(NoDM(), cruelty=6, agent=agent, gate=MockGate())
    sent = run(coord, [req(1, "drops:1", "drops", {"wanted": 2}), 0.3,
                       req(2, "drops:1", "drops", {"wanted": 2, "reason": reason}, keys=3)])   # even with the state moved
    res = {m["id"]: m for m in results(sent)}
    assert res[2]["ok"] and [o["reward"] for o in res[2]["decision"]["outcomes"]] == ["nothing", "penny"]
    assert len(agent.seen) == 1 and coord.stats["gate"]["reask"] == coord.stats["gate"]["keep"] == 0
    assert coord.stats["drops"]["reused"] == 1 and coord.memory.issued["drops:1"]["rid"] == 2


def test_a_routine_re_ask_still_goes_to_the_agent_when_the_deck_is_short():
    agent = ScriptedAgent([[stack("drops", "drops:1", **drops("nothing"))],
                           [stack("drops", "drops:1", **drops("nothing", "key"))]])
    coord = Coordinator(NoDM(), cruelty=6, agent=agent, gate=MockGate())
    sent = run(coord, [req(1, "drops:1", "drops", {"wanted": 1}), 0.3,
                       req(2, "drops:1", "drops", {"wanted": 2, "reason": "refresh: player state changed"})])
    assert len(agent.seen) == 2, "a deck shorter than the game wants is restocked"
    assert [o["reward"] for o in {m["id"]: m for m in results(sent)}[2]["decision"]["outcomes"]] == ["nothing", "key"]


def test_a_re_ask_made_while_its_deck_is_being_written_waits_for_it_then_meets_the_gate():
    agent = ScriptedAgent([[stack("drops", "drops:1", **drops("nothing", "penny"))]], delay=0.2)
    coord = Coordinator(NoDM(), cruelty=6, agent=agent, gate=MockGate())
    sent = run(coord, [req(1, "drops:1", "drops", {"wanted": 2}), 0.1,
                       req(2, "drops:1", "drops", {"wanted": 2})], settle=0.5)   # asked again mid-turn
    assert len(agent.seen) == 1, "no second turn for the same deck"
    res = {m["id"]: m for m in results(sent)}
    assert res[1]["ok"] and res[2]["ok"] and res[2]["drawn_base"] == 0
    assert coord.memory.issued["drops:1"]["rid"] == 2 and coord.stats["drops"]["reused"] == 1


def test_a_turn_writes_a_drops_deck_to_last_several_rooms():
    agent = ScriptedAgent([[stack("drops", "drops:1", **drops(*["nothing"] * 12))]])
    coord = Coordinator(NoDM(), cruelty=6, agent=agent)
    sent = run(coord, [req(1, "drops:1", "drops", {"wanted": 3})])
    assert agent.seen[0]["pending"][0]["context"]["wanted"] == server.AGENT_DROPS_DEPTH
    assert len(results(sent)[0]["decision"]["outcomes"]) == 12


def test_the_game_is_told_when_agent_mode_is_on():
    dm, agent = NoDM(), ScriptedAgent([])
    coord = Coordinator(dm, cruelty=6, agent=agent)
    sent = run(coord, [{"t": "hello", "proto": PROTO, "session": "S", "run": "R"}], settle=0)
    assert [m for m in sent if m["t"] == "welcome"][0]["agent"] is True

    async def switch_off():
        coord.reconfigure(Engine(dm))
        await asyncio.sleep(0.01)

    asyncio.run(switch_off())
    assert sent[-1] == {"t": "mode", "session": "S", "agent": False}


def test_a_floor_start_ask_waits_for_the_rest_of_the_floor():
    assert server._priority("room_clear", {"reason": "floor start"}) == server.BACKGROUND
    assert server._priority("room_layout", {"reason": "floor start"}) == server.BACKGROUND
    assert server._priority("room_clear", {"reason": "eta under lead time"}) == server.URGENT
    assert server._priority("drops", {"reason": "floor start", "owed": 1}) == server.URGENT


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


def test_a_restack_to_the_same_deck_is_a_keep_and_an_open_one_is_not_pushed_again():
    agent = ScriptedAgent([[stack("room_clear", "clear:1:85", **clear("bomb")),
                            stack("room_clear", "clear:1:86", **clear("penny"))],
                           [stack("room_clear", "clear:1:85", **clear("bomb")),        # pending: the same deck
                            stack("room_clear", "clear:1:86", **clear("penny"))]])     # open: the same deck
    coord = Coordinator(NoDM(), cruelty=6, agent=agent)
    sent = run(coord, [req(1, "clear:1:85", "room_clear"), req(2, "clear:1:86", "room_clear"), 0.3,
                       req(3, "clear:1:85", "room_clear")])
    res = {m["id"]: m for m in results(sent)}
    assert res[3]["ok"] and res[3]["decision"]["rewards"] == [{"reward": "bomb", "count": 1}]
    assert not any(m.get("push") for m in results(sent)), "the open deck isn't sent again unchanged"
    a = coord.stats["agent"]
    assert a["unchanged"] == 2 and a["keep"] == 1 and a["stack"] == 2 and a["rejected"] == 0
    assert coord.memory.issued["clear:1:85"]["rid"] == 3 and coord.stats["room_clear"]["ok"] == 2


def test_a_keep_for_a_deck_used_up_while_the_turn_ran_counts_as_kept():
    agent = ScriptedAgent([[stack("room_clear", "clear:1:85", **clear("bomb"))],
                           [{"tool": "keep_deck", "args": {"key": "clear:1:85", "why": "still fits"}}]], delay=0.3)
    coord = Coordinator(NoDM(), cruelty=6, agent=agent)
    sent = run(coord, [req(1, "clear:1:85", "room_clear"), 0.5, req(2, "clear:1:85", "room_clear"), 0.2,
                       {"t": "drawn", "key": "clear:1:85", "id": 1, "outcome": {"applied": True}}], settle=0.5)
    res = {m["id"]: m for m in results(sent)}
    assert res[2] == {"t": "res", "session": None, "id": 2, "key": "clear:1:85", "kind": "room_clear",
                      "ok": False, "reason": "used_up"}
    a = coord.stats["agent"]
    assert a["keep"] == 1 and a["rejected"] == 0 and a["skipped"] == 0


def test_open_decks_are_this_floors_with_no_cap_and_say_how_the_player_moved():
    floor2 = {"stage": 2, "stage_type": 0, "name": "Basement II", "room_types": {"default": 10}}
    def answer(turn):                             # 8 keys to a turn: every turn answers what it holds
        return [stack("room_clear", p["key"], **clear()) for p in turn["pending"]]
    agent = ScriptedAgent([answer] * 8)
    coord = Coordinator(NoDM(), cruelty=6, agent=agent)

    def at(rid, key, level, coins=0, items=()):
        m = req(rid, key, "room_clear")
        m["state"] = {**m["state"], "level": level}
        m["state"]["player"] = {**m["state"]["player"], "resources": {"keys": 1, "bombs": 1, "coins": coins},
                                "items": list(items)}
        return m

    run(coord, [at(100 + i, f"clear:1:{i}", FLOOR) for i in range(20)] + [0.3]
        + [at(200 + i, f"clear:2:{i}", floor2) for i in range(20)] + [0.3]
        + [at(300, "clear:2:99", floor2, coins=4, items=["Libra (q1)"])])
    opened = next(t for t in agent.seen if t["pending"][0]["key"] == "clear:2:99")["open_decks"]
    assert {d["key"] for d in opened} == {f"clear:2:{i}" for i in range(20)}, "every deck, none from floor 1"
    assert opened[0]["since_written"] == "coins 0->4, +Libra (q1)"


def test_state_diff_names_only_what_moved():
    old = {"player": {"resources": {"coins": 0, "keys": 1}, "hp": {"red": 6, "soul": 0}, "items": ["Gemini (q1)"]}}
    new = {"player": {"resources": {"coins": 4, "keys": 1}, "hp": {"red": 3, "soul": 0}, "items": ["Libra (q1)"]}}
    assert server.state_diff(old, new) == "coins 0->4, hp.red 6->3, +Libra (q1), -Gemini (q1)"
    assert server.state_diff(old, old) == ""


def test_item_candidates_carry_what_each_item_does():
    agent = ScriptedAgent([[stack("item_choice", "items:1:0", order=[304])]])
    coord = Coordinator(NoDM(), cruelty=6, agent=agent)
    run(coord, [req(1, "items:1:0", "item_choice",
                    {"candidates": [{"id": 304, "name": "Libra", "quality": 1}, {"id": 99999, "name": "Modded"}]})])
    cands = agent.seen[0]["pending"][0]["context"]["candidates"]
    assert cands[0]["effect"].startswith("Gives 6 Coins, 6 Bombs, and 6 Keys")
    assert "effect" not in cands[1], "an item the table doesn't know is shown as it came"


def test_restored_pickups_are_kept_apart_from_the_roll_audit():
    from collections import Counter
    cov = Counter({("spawn", "room_restore", "restored"): 5, ("spawn", "unknown", "uncontrolled"): 2,
                   ("spawn", "enemy_death", "drops"): 3})
    assert server.coverage_table(cov) == [("spawn", "unknown", 0, 2), ("spawn", "enemy_death", 3, 0)]
    assert server.restored_count(cov) == 5
    assert "restored on re-entry" in server.coverage_lines(cov)[-1]
