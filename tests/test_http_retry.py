"""Provider errors: 5xx sent again, 429 starts a cooldown the game's re-asks wait out, error bodies kept."""
import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import http_retry  # noqa: E402
from dm_agent import OpenRouterDM  # noqa: E402
from http_retry import Cooldown, CoolingDown, error_fields, retry_after  # noqa: E402
from planners import OpenRouterPlanner  # noqa: E402
from server import Coordinator  # noqa: E402

REAL_SLEEP = asyncio.sleep

GOOGLE_429 = json.dumps([{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded",
                                    "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo",
                                                 "retryDelay": "17s"}]}}])
TOOL_ANSWER = {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "tool_calls": [
    {"function": {"name": "decide_room_clear", "arguments": json.dumps({"intent": "x", "rewards": []})}}]}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
PLAN_ANSWER = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "TITLE: t"}}],
               "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Backoffs are recorded, not waited."""
    slept = []

    async def sleep(s):
        slept.append(s)
        await REAL_SLEEP(0)

    monkeypatch.setattr(http_retry.asyncio, "sleep", sleep)
    return slept


def scripted(responses):
    """An httpx transport that answers with `responses` in order and counts the posts."""
    posts = []

    def handler(request):
        posts.append(request)
        status, body = responses[min(len(posts), len(responses)) - 1]
        return httpx.Response(status, text=body if isinstance(body, str) else json.dumps(body))

    return httpx.MockTransport(handler), posts


def dm_with(transport):
    dm = OpenRouterDM(cruelty=3, model="gemini-test", api_key="k", google=True)
    dm.client = httpx.AsyncClient(transport=transport)
    return dm


def test_retry_after_reads_the_header_or_googles_retry_delay():
    assert retry_after(httpx.Response(429, headers={"Retry-After": "7"})) == 7.0
    assert retry_after(httpx.Response(429, text=GOOGLE_429)) == 17.0
    assert retry_after(httpx.Response(429, text="slow down")) is None


def test_a_503_is_sent_again_once_then_answers(no_sleep):
    transport, posts = scripted([(503, "overloaded"), (200, TOOL_ANSWER)])
    out = asyncio.run(dm_with(transport).decide("room_clear", {}, {}, {}))
    assert out["intent"] == "x" and len(posts) == 2 and len(no_sleep) == 1


def test_a_429_starts_a_cooldown_and_nothing_is_sent_during_it():
    transport, posts = scripted([(429, GOOGLE_429), (200, TOOL_ANSWER)])
    dm = dm_with(transport)
    with pytest.raises(httpx.HTTPStatusError) as err:
        asyncio.run(dm.decide("room_clear", {}, {}, {}))
    assert "RESOURCE_EXHAUSTED" in error_fields(err.value)["body"]       # the receipt keeps why
    assert 16 < dm.cooldown.remaining() <= 17
    with pytest.raises(CoolingDown):
        asyncio.run(dm.decide("room_clear", {}, {}, {}))
    assert len(posts) == 1                                               # the re-ask cost nothing


def test_our_own_backoff_doubles_per_strike_and_resets_on_success(monkeypatch):
    monkeypatch.setattr(http_retry.random, "uniform", lambda a, b: b)   # the top of each jitter range
    cd, resp = Cooldown(), httpx.Response(429, text="no hint")
    waits = []
    for _ in range(3):
        waits.append(round(cd.hit(resp)))
        cd.until = 0.0                                                  # that window ran out
    assert waits == [2, 4, 8]
    cd.ok()
    assert round(cd.hit(resp)) == 2


def test_a_400_keeps_the_providers_reason():
    transport, _ = scripted([(400, '{"error": {"message": "Invalid JSON payload: unknown field"}}')])
    dm = dm_with(transport)
    dm.effort = None                                                     # no thinking fallback in the way
    with pytest.raises(httpx.HTTPStatusError) as err:
        asyncio.run(dm.decide("room_clear", {}, {}, {}))
    assert "unknown field" in error_fields(err.value)["body"]


def test_a_planner_rides_out_503s_and_a_short_429(no_sleep):
    transport, posts = scripted([(503, "overloaded"), (429, GOOGLE_429), (200, PLAN_ANSWER)])
    planner = OpenRouterPlanner(narrative_model="g", floor_model="g", api_key="k", google=True)
    planner.client = httpx.AsyncClient(transport=transport)
    text, _ = asyncio.run(planner.narrative({}, 3))
    assert text == "TITLE: t" and len(posts) == 3 and no_sleep[-1] == 17.0


def test_a_planner_gives_up_after_its_retries():
    transport, posts = scripted([(503, "overloaded")])
    planner = OpenRouterPlanner(narrative_model="g", floor_model="g", api_key="k", google=True)
    planner.client = httpx.AsyncClient(transport=transport)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(planner.narrative({}, 3))
    assert len(posts) == 3


class CoolingDM:
    """A DM whose model is rate-limited for `left` seconds; counts the calls that got through."""

    def __init__(self, left):
        self.cooldown = Cooldown()
        self.cooldown.until = http_retry.time.monotonic() + left
        self.calls = 0

    def set_cruelty(self, cruelty):
        pass

    async def decide(self, kind, state, ctx, memory):
        self.cooldown.check()
        self.calls += 1
        return {"intent": "x", "rewards": []}


def clear_req(deadline_ms):
    return {"t": "req", "id": 1, "key": "clear:1:82", "kind": "room_clear", "deadline_ms": deadline_ms,
            "state": {"player": {"resources": {"keys": 0, "bombs": 1, "coins": 0}}}, "ctx": {"reason": "almost clear"}}


def run_request(dm, deadline_ms, monkeypatch):
    import server
    slept = []

    async def sleep(s):
        slept.append(s)
        dm.cooldown.until -= s                                           # time passes
        await REAL_SLEEP(0)

    monkeypatch.setattr(server.asyncio, "sleep", sleep)
    coord, sent = Coordinator(dm, cruelty=3), []

    async def send(msg):
        sent.append(msg)

    async def go():
        await coord.handle(clear_req(deadline_ms), send)
        for _ in range(200):
            if sent:
                break
            await REAL_SLEEP(0)

    asyncio.run(go())
    return coord, sent, slept


def test_a_short_cooldown_is_waited_out_then_the_call_is_made(monkeypatch):
    dm = CoolingDM(left=2.0)
    coord, sent, slept = run_request(dm, 8000, monkeypatch)
    assert dm.calls == 1 and sent[-1]["ok"] and slept and 1.9 < slept[0] <= 2.0


def test_a_cooldown_past_the_deadline_is_answered_cooldown_at_the_deadline(monkeypatch):
    dm = CoolingDM(left=30.0)
    coord, sent, slept = run_request(dm, 8000, monkeypatch)
    assert dm.calls == 0 and slept == [8.0]
    assert sent[-1]["ok"] is False and sent[-1]["reason"] == "cooldown" and sent[-1]["retry_ms"] > 0
    assert coord.stats["room_clear"]["cooldown"] == 1 and coord.stats["room_clear"]["error"] == 0
